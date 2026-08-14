"""
hermes_adapter.py — the one place the harness talks to the Hermes agent.

Everything else in the harness is transport (Buzz) and control (governor). This
module isolates *how a single turn of work gets executed on Hermes running
deepseek-v4-flash-0731*, so if your Hermes invocation differs you change it here
and nowhere else.

Two backends:

  * "hermes_cli"  — shell out to the installed `hermes` binary for a one-shot,
                    non-interactive turn. This is the real integration: Hermes
                    brings the deepseek model, its tools, and (if you enable the
                    `delegation` toolset) its own sub-subagents. Confirm the exact
                    one-shot flag for your Hermes version with `hermes --help`
                    and adjust `cli_template` in config.yaml if needed. The prompt
                    is passed inline as a single argv element via `{prompt}`.

  * "openai_compat" — call the hosted OpenAI-compatible endpoint that serves the
                    model directly. Self-contained; handy for smoke-testing the
                    harness before wiring the CLI. Needs `requests`.

A "turn" takes a prompt (goal + accumulated context) and returns the model/agent's
text output for that turn. The governor decides whether to spend another one.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional


@dataclass
class HermesConfig:
    backend: str = "hermes_cli"          # "hermes_cli" | "openai_compat"
    model: str = "deepseek/deepseek-v4-flash-0731"
    # hermes_cli: a shell template. {prompt} holds the turn prompt; {model}
    # is substituted. This Hermes build uses top-level `hermes -m MODEL -z PROMPT`.
    cli_template: str = "hermes -m {model} -z {prompt}"
    cli_timeout: int = 600
    # openai_compat: hosted provider serving deepseek-v4-flash-0731
    base_url: str = "https://openrouter.ai/api/v1"   # or your provider's URL
    api_key_env: str = "HERMES_MODEL_API_KEY"
    request_timeout: int = 300


class HermesError(RuntimeError):
    pass


class HermesAdapter:
    def __init__(self, cfg: HermesConfig):
        self.cfg = cfg
        # Real token accounting for the most recent turn. Shape (contract):
        #   {"input_tokens": int, "output_tokens": int, "cache_tokens": int,
        #    "billable_tokens": int, "total_tokens": int,
        #    "api_calls": int, "cost_usd": float, "exact": bool}
        self.last_usage: Optional[dict] = None

    @staticmethod
    def _usage_inputs(input_tokens=0, output_tokens=0, total_tokens=0,
                      cache_tokens=0, api_calls=0, cost_usd=0.0, exact=False) -> dict:
        """Build the canonical usage contract dict.

        `cache_tokens` is the sum of cache read + cache write tokens (may be 0).
        `billable_tokens` = input + output (excludes cache, so a "tokens used"
        counter climbs with real spend, not with the cache-inflated total).
        `total_tokens` is kept exactly as hermes reports it (includes cache).
        """
        return {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_tokens": int(cache_tokens),
            "billable_tokens": int(input_tokens) + int(output_tokens),
            "total_tokens": int(total_tokens),
            "api_calls": int(api_calls),
            "cost_usd": float(cost_usd),
            "exact": bool(exact),
        }

    def pop_last_usage(self) -> dict:
        """Return the last turn's usage and clear it."""
        usage = self.last_usage
        self.last_usage = None
        if usage is None:
            return self._usage_inputs()
        return usage

    def run_turn(self, prompt: str, workdir: Optional[str] = None) -> str:
        if self.cfg.backend == "hermes_cli":
            return self._run_cli(prompt, workdir)
        if self.cfg.backend == "openai_compat":
            return self._run_openai(prompt)
        raise HermesError(f"unknown backend {self.cfg.backend!r}")

    # --- backend: installed Hermes binary --------------------------------

    def _run_cli(self, prompt: str, workdir: Optional[str]) -> str:
        # The cli_template contains {model} and {prompt}. We split on {prompt}
        # and pass the prompt as a single argv element so multi-line content with
        # spaces/quotes can't break shell splitting. {model} is substituted first.
        template = self.cfg.cli_template.replace("{model}", self.cfg.model)
        head, sep, tail = template.partition("{prompt}")
        argv = shlex.split(head) if head.strip() else []
        argv.append(prompt)                       # prompt = exactly one argv element
        argv += shlex.split(tail) if tail.strip() else []
        # Real token accounting: ask hermes to write a per-run usage JSON.
        usage_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as tf:
                usage_path = tf.name
            argv += ["--usage-file", usage_path]
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=workdir,
                timeout=self.cfg.cli_timeout,
            )
            if proc.returncode != 0:
                raise HermesError(
                    f"hermes exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
                )
            self._record_cli_usage(usage_path)
            return proc.stdout.strip()
        finally:
            if usage_path is not None:
                try:
                    os.unlink(usage_path)
                except OSError:
                    pass

    def _record_cli_usage(self, usage_path: str) -> None:
        """Parse the --usage-file JSON into the contract shape (best-effort)."""
        try:
            with open(usage_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            cache_tokens = int(data.get("cache_read_tokens") or 0) \
                + int(data.get("cache_write_tokens") or 0)
            self.last_usage = self._usage_inputs(
                input_tokens=data.get("input_tokens") or 0,
                output_tokens=data.get("output_tokens") or 0,
                total_tokens=data.get("total_tokens") or 0,
                cache_tokens=cache_tokens,
                api_calls=data.get("api_calls") or 0,
                cost_usd=data.get("estimated_cost_usd") or 0.0,
                exact=True,
            )
        except (OSError, ValueError, TypeError):
            # Missing or unparseable -> zeros, flagged inexact.
            self.last_usage = self._usage_inputs()

    # --- backend: hosted OpenAI-compatible endpoint ----------------------

    def _run_openai(self, prompt: str) -> str:
        import requests  # local import so hermes_cli users don't need it

        api_key = os.environ.get(self.cfg.api_key_env, "")
        if not api_key:
            raise HermesError(f"{self.cfg.api_key_env} not set for openai_compat backend")
        resp = requests.post(
            f"{self.cfg.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "model": self.cfg.model,
                    "messages": [
                        {"role": "system", "content": "You are a Hermes worker subagent. "
                         "Work the task. End your reply with a line `STATUS: done` when the "
                         "success criteria are fully met, otherwise `STATUS: working`."},
                        {"role": "user", "content": prompt},
                    ],
                }
            ),
            timeout=self.cfg.request_timeout,
        )
        if resp.status_code >= 400:
            raise HermesError(f"model endpoint {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        # Real usage from the API response `usage` object if present.
        u = data.get("usage") or {}
        if isinstance(u, dict) and u:
            self.last_usage = self._usage_inputs(
                input_tokens=u.get("prompt_tokens") or 0,
                output_tokens=u.get("completion_tokens") or 0,
                total_tokens=u.get("total_tokens") or 0,
                api_calls=1,
                cost_usd=0.0,
                exact=True,
            )
        else:
            self.last_usage = self._usage_inputs()
        return data["choices"][0]["message"]["content"].strip()
