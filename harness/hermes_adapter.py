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
from dataclasses import dataclass
from typing import Optional


@dataclass
class HermesConfig:
    backend: str = "hermes_cli"          # "hermes_cli" | "openai_compat"
    model: str = "deepseek-v4-flash-0731"
    # hermes_cli: a shell template. {prompt_file} holds the turn prompt; {model}
    # is substituted. Verify the flag against your `hermes --help`.
    cli_template: str = "hermes run --model {model} --no-interactive --input {prompt_file}"
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
        return proc.stdout.strip()

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
        return data["choices"][0]["message"]["content"].strip()
