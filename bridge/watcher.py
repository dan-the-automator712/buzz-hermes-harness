#!/usr/bin/env python3
"""
watcher.py — the chat <-> harness bridge.

Runs on your box (WSL). It watches `bridge/inbox/` for job files that Claude drops
there, runs each one through the harness (which dispatches signed tasks to the
Hermes workers over Buzz under the 5-turn governor), then writes a status report
into `bridge/outbox/` that Claude reads back.

Reports are named so problems are obvious at a glance:
    OK_<job>.report.md        every task finished (status=done)
    PROBLEM_<job>.report.md   something failed / needs review / errored

Design goals: keep the private keys and all execution on this machine; never need
Claude to run anything here. Claude only writes plain job JSON and reads reports.

Start it once and leave it running (see bridge/README.md). Requires: the harness
venv (with PyYAML), `buzz` on PATH, Hermes configured, relay reachable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

BRIDGE = Path(__file__).resolve().parent
ROOT = BRIDGE.parent                      # harness repo root
INBOX = BRIDGE / "inbox"
OUTBOX = BRIDGE / "outbox"
PROCESSING = BRIDGE / "processing"
DONE = BRIDGE / "done"
STATUS = BRIDGE / "status.json"
LOG = BRIDGE / "watcher.log"
LOCK = BRIDGE / "watcher.lock"

CONFIG = ROOT / "harness" / "config.yaml"
POLL = int(os.environ.get("BRIDGE_POLL", "5"))
JOB_TIMEOUT = int(os.environ.get("BRIDGE_JOB_TIMEOUT", "3600"))
PY = sys.executable                        # the venv python running this watcher


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(msg: str) -> None:
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def queen_nsec(cfg: dict) -> str | None:
    try:
        keys = json.loads((ROOT / cfg["identities_file"]).read_text())
        return keys["orchestrator"]["nsec"]
    except Exception:  # noqa: BLE001
        return None


def write_status(**fields) -> None:
    base = {"updated": now(), "watcher": "running", "poll_seconds": POLL}
    base.update(fields)
    try:
        STATUS.write_text(json.dumps(base, indent=2))
    except OSError:
        pass


# ---- running one job -------------------------------------------------------

def parse_summary(stdout: str) -> dict:
    """Pull the JSON block the orchestrator prints after '=== SUMMARY ==='."""
    marker = "=== SUMMARY ==="
    if marker in stdout:
        tail = stdout.split(marker, 1)[1].strip()
        try:
            return json.loads(tail)
        except json.JSONDecodeError:
            pass
    return {}


def fetch_audit(cfg: dict, channel_id: str) -> tuple[list[str], list[str]]:
    """
    Best-effort: read the signed event trail for the channel via buzz-cli.
    Returns (audit_lines, failure_details) — the latter pulls any `error` fields
    the workers posted, so a failed job is diagnosable from the report alone.
    """
    nsec = queen_nsec(cfg)
    if not channel_id or not nsec or not shutil.which("buzz"):
        return [], []
    env = dict(os.environ, BUZZ_RELAY_URL=cfg["relay_url"], BUZZ_PRIVATE_KEY=nsec)
    try:
        proc = subprocess.run(
            ["buzz", "messages", "get", "--channel", channel_id, "--limit", "100"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        events = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        return [], []
    lines: list[str] = []
    errors: list[str] = []
    for ev in events if isinstance(events, list) else []:
        pk = (ev.get("pubkey") or "")[:8]
        ts = ev.get("created_at")
        try:
            env_obj = json.loads(ev.get("content", "{}"))
        except json.JSONDecodeError:
            env_obj = {}
        t = env_obj.get("type", "?")
        tid = env_obj.get("task_id", "")
        extra = env_obj.get("status") or env_obj.get("decision") or ""
        err = env_obj.get("error")
        line = f"{ts}  {t:<9} task={tid:<20} {extra:<12} pubkey={pk}…"
        if err:
            line += f"  ERROR={str(err)[:180]}"
            errors.append(f"{tid}: {err}")
        lines.append(line)
    return lines, errors


def run_job(cfg: dict, job_path: Path) -> None:
    name = job_path.stem
    proc_path = PROCESSING / job_path.name
    shutil.move(str(job_path), str(proc_path))
    log(f"running job '{name}'")
    write_status(current_job=name, state="running")

    started = now()
    problem = False
    reason = ""
    summary: dict = {}
    stdout = stderr = ""

    # Validate JSON first — a malformed job is itself a problem to surface.
    try:
        spec = json.loads(proc_path.read_text())
        njobs = len(spec.get("jobs", []))
        if njobs == 0:
            raise ValueError("no 'jobs' array / empty")
    except Exception as e:  # noqa: BLE001
        problem, reason = True, f"invalid job file: {e}"
        njobs = 0

    if not problem:
        try:
            r = subprocess.run(
                [PY, "harness/orchestrator.py", "--config", "harness/config.yaml",
                 "--jobs", str(proc_path)],
                cwd=str(ROOT), capture_output=True, text=True, timeout=JOB_TIMEOUT,
            )
            stdout, stderr = r.stdout, r.stderr
            summary = parse_summary(stdout)
            results = summary.get("results", {})
            bad = {k: v for k, v in results.items()
                   if str(v.get("status")) not in ("done",)}
            if r.returncode != 0:
                problem, reason = True, f"orchestrator exit {r.returncode}"
            elif bad:
                problem = True
                reason = "; ".join(f"{k}={v.get('status')}" for k, v in bad.items())
        except subprocess.TimeoutExpired:
            problem, reason = True, f"timed out after {JOB_TIMEOUT}s"
        except Exception as e:  # noqa: BLE001
            problem, reason = True, f"watcher error: {e}"

    channel_id = summary.get("channel_id", "")
    audit, failures = fetch_audit(cfg, channel_id) if channel_id else ([], [])
    report = render_report(name, started, problem, reason, njobs, summary, audit,
                           failures, stdout, stderr)

    prefix = "PROBLEM_" if problem else "OK_"
    (OUTBOX / f"{prefix}{name}.report.md").write_text(report)
    (OUTBOX / f"{prefix}{name}.result.json").write_text(json.dumps({
        "job": name, "finished": now(), "needs_attention": problem,
        "reason": reason, "summary": summary,
    }, indent=2))
    shutil.move(str(proc_path), str(DONE / proc_path.name))
    write_status(current_job=None, state="idle",
                 last_job=name, last_result=("problem" if problem else "ok"),
                 last_reason=reason, last_finished=now())
    log(f"job '{name}' -> {'PROBLEM' if problem else 'OK'} {('('+reason+')') if reason else ''}")


def render_report(name, started, problem, reason, njobs, summary, audit, failures, stdout, stderr) -> str:
    head = ">> NEEDS ATTENTION <<" if problem else "OK - all tasks completed"
    lines = [
        f"# Job report: {name}",
        "",
        f"**Outcome:** {head}",
        f"**Started:** {started}  ·  **Finished:** {now()}",
        f"**Tasks in job:** {njobs}",
    ]
    if problem:
        lines.append(f"**Problem:** {reason}")
    if failures:
        lines += ["", "## Failure detail (from signed worker events)", "```",
                  *[str(f)[:400] for f in failures], "```"]
    results = summary.get("results", {})
    if results:
        lines += ["", "| task | status | turns |", "|------|--------|-------|"]
        for tid, r in results.items():
            lines.append(f"| {tid} | {r.get('status','?')} | {r.get('turns','—')} |")
    if summary.get("channel_id"):
        lines += ["", f"**Buzz channel:** `{summary['channel_id']}`"]
    if audit:
        lines += ["", "## Signed audit trail", "```", *audit[-40:], "```"]
    tail = (stdout or "").strip().splitlines()[-30:]
    if tail:
        lines += ["", "## Orchestrator output (tail)", "```", *tail, "```"]
    if problem and stderr.strip():
        lines += ["", "## Errors", "```", *stderr.strip().splitlines()[-30:], "```"]
    return "\n".join(lines) + "\n"


# ---- main loop -------------------------------------------------------------

def main() -> int:
    for d in (INBOX, OUTBOX, PROCESSING, DONE):
        d.mkdir(parents=True, exist_ok=True)

    if LOCK.exists():
        # Stale-lock tolerance: if the pid isn't alive, take over.
        try:
            old = int(LOCK.read_text().strip())
            os.kill(old, 0)
            log(f"another watcher (pid {old}) is running; exiting")
            return 1
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    LOCK.write_text(str(os.getpid()))

    cfg = load_cfg()
    log(f"watcher up. root={ROOT} poll={POLL}s python={PY}")
    if not shutil.which("buzz"):
        log("WARNING: 'buzz' not on PATH — audit-trail enrichment will be skipped")
    write_status(state="idle", current_job=None)

    try:
        while True:
            try:
                jobs = sorted(INBOX.glob("*.json"))
                write_status(state="idle" if not jobs else "queued",
                             queue=len(jobs), current_job=None)
                for jp in jobs:
                    run_job(cfg, jp)
            except Exception:  # noqa: BLE001 - never let the daemon die on one bad job
                log("loop error:\n" + traceback.format_exc())
            time.sleep(POLL)
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
