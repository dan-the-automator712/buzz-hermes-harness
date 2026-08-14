#!/usr/bin/env python3
"""
check_setup.py — preflight for the Buzz × Hermes harness.

Run this on the machine that will host the harness. It verifies each wire in turn
and tells you exactly what's missing:

    1. buzz-cli is on PATH
    2. keys.json exists and parses (queen + workers)
    3. the relay is reachable AND the queen key authenticates (buzz channels list)
    4. the task channel exists or can be created
    5. Hermes is reachable for the configured backend

    python harness/check_setup.py --config harness/config.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

OK, BAD, WARN = "  ok ", " FAIL", " warn"


def line(status, msg):
    print(f"[{status}] {msg}")


def run_buzz(relay_url, nsec, args):
    env = dict(os.environ, BUZZ_RELAY_URL=relay_url, BUZZ_PRIVATE_KEY=nsec)
    return subprocess.run(["buzz", *args], capture_output=True, text=True, env=env)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="harness/config.yaml")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    relay_url = cfg["relay_url"]
    failures = 0

    # 1) buzz-cli present
    if shutil.which("buzz"):
        line(OK, "buzz-cli found on PATH")
    else:
        line(BAD, "buzz-cli not on PATH — `cargo install --path crates/buzz-cli`")
        failures += 1

    # 2) keys.json
    keys_path = Path(cfg["identities_file"])
    ids = None
    if keys_path.exists():
        try:
            ids = json.loads(keys_path.read_text())
            nw = len(ids.get("workers", []))
            line(OK, f"{keys_path} loaded: queen + {nw} worker identities")
        except Exception as e:  # noqa: BLE001
            line(BAD, f"{keys_path} failed to parse: {e}")
            failures += 1
    else:
        line(BAD, f"{keys_path} missing — run gen_keys.py --workers N")
        failures += 1

    # 3) relay reachable + auth (needs buzz-cli + keys)
    if shutil.which("buzz") and ids:
        if not relay_url.startswith(("http://", "https://")):
            line(WARN, f"relay_url={relay_url!r} — buzz-cli wants http(s), not wss")
        nsec = ids["orchestrator"]["nsec"]
        proc = run_buzz(relay_url, nsec, ["channels", "list"])
        if proc.returncode == 0:
            try:
                chans = json.loads(proc.stdout or "[]")
                names = [c.get("name") for c in chans] if isinstance(chans, list) else []
                line(OK, f"relay {relay_url} reachable & queen key authenticated "
                         f"({len(names)} channel(s): {', '.join(filter(None, names)) or '—'})")
            except json.JSONDecodeError:
                line(OK, f"relay {relay_url} responded (non-JSON body)")
        else:
            code = {2: "network — check URL/TLS/DNS", 3: "auth — key not authorized on this relay"}
            hint = code.get(proc.returncode, proc.stderr.strip()[:200])
            line(BAD, f"buzz channels list failed (exit {proc.returncode}): {hint}")
            failures += 1

        # 4) channel presence
        want = cfg.get("channel_id") or cfg.get("channel_name", "hive-tasks")
        line(WARN if not cfg.get("channel_id") else OK,
             f"task channel = {want!r} (the queen auto-creates it on first run)")

    # 5) Hermes backend
    backend = cfg.get("hermes", {}).get("backend", "hermes_cli")
    if backend == "hermes_cli":
        if shutil.which("hermes"):
            line(OK, "hermes found on PATH (hermes_cli backend)")
            # Validate the configured one-shot invocation actually works for THIS build.
            # This catches a stale/wrong cli_template (e.g. `hermes run` when the build
            # only supports top-level `-z`) before any real job runs.
            tmpl = cfg.get("hermes", {}).get("cli_template", "")
            if "{prompt}" not in tmpl and "{prompt_file}" not in tmpl:
                line(BAD, f"cli_template missing a {{prompt}}/{{prompt_file}} placeholder: {tmpl!r}")
                failures += 1
            else:
                # Verify the first token is on PATH and the template's primary flag is
                # recognized. Extract the head token (before any {prompt} placeholder).
                head = tmpl.split("{prompt}")[0].split("{prompt_file}")[0].strip()
                head = head.replace("{model}", cfg.get("hermes", {}).get("model", ""))
                head_tokens = head.split()
                if head_tokens:
                    if not shutil.which(head_tokens[0]):
                        line(BAD, f"cli_template binary not on PATH: {head_tokens[0]!r}")
                        failures += 1
                    else:
                        # Confirm each flag in the head actually exists by scanning `--help`.
                        flags = [t for t in head_tokens[1:] if t.startswith("-")]
                        help_out = ""
                        if flags:
                            try:
                                help_out = subprocess.run(
                                    [head_tokens[0], "--help"],
                                    capture_output=True, text=True, timeout=30,
                                ).stdout or ""
                            except Exception:  # noqa: BLE001
                                help_out = ""
                        bad_flags = [f for f in flags if f not in help_out]
                        if bad_flags:
                            line(BAD,
                                 f"cli_template flag(s) {', '.join(bad_flags)} not in "
                                 f"`{head_tokens[0]} --help` — check the one-shot invocation "
                                 f"for this Hermes build")
                            failures += 1
                        else:
                            line(OK, f"cli_template flag(s) {', '.join(flags) or '(none)'} "
                                     f"recognized by this Hermes build")
        else:
            line(BAD, "hermes not on PATH for hermes_cli backend")
            failures += 1
    else:
        key = os.environ.get(cfg["hermes"].get("api_key_env", "HERMES_MODEL_API_KEY"), "")
        base = cfg["hermes"].get("base_url")
        line(OK if key else BAD,
             f"openai_compat backend -> {base} (api key {'set' if key else 'MISSING'})")
        failures += 0 if key else 1

    print()
    if failures:
        line(BAD, f"{failures} blocking issue(s) above — fix and re-run")
        return 1
    line(OK, "all wires green — run: python harness/orchestrator.py --jobs jobs.example.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
