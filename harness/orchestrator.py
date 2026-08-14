#!/usr/bin/env python3
"""
orchestrator.py — the "queen".

Reads jobs.json, publishes each as a *signed* task event onto a Buzz channel, then
dispatches a pool of Hermes workers to run them in parallel. Each worker is capped
at `max_turns` (5) and checkpoints back to the queen when the budget is spent.

Run:
    python harness/orchestrator.py --config harness/config.yaml --jobs jobs.json

Every message on the relay is Schnorr-signed and timestamped; the queen and each
worker have distinct Nostr keypairs, so the channel is a self-auditing record of
who asked for what and who produced what.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from buzz_bus import BuzzBus, Identity
from governor import Governor
from hermes_adapter import HermesAdapter, HermesConfig
from worker import run_worker_task


def load_identities(path: Path) -> dict:
    data = json.loads(path.read_text())
    orch = data["orchestrator"]
    workers = data["workers"]
    return {
        "orchestrator": Identity(orch["name"], orch["nsec"], orch["pubkey"]),
        "workers": [Identity(w["name"], w["nsec"], w["pubkey"]) for w in workers],
    }


def build_allowlist(ids: dict) -> dict[str, str]:
    allow = {ids["orchestrator"].pubkey_hex: ids["orchestrator"].name}
    for w in ids["workers"]:
        allow[w.pubkey_hex] = w.name
    return allow


def _extract_channel_id(obj: Any) -> str | None:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("channel_id", "id", "uuid", "channel"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) >= 8:
                return v
    return None


def ensure_channel(bus: BuzzBus, cfg: dict) -> str:
    if cfg.get("channel_id"):
        return cfg["channel_id"]
    name = cfg.get("channel_name", "hive-tasks")
    # Try to find an existing channel of that name first.
    try:
        channels = bus._run(["channels", "list"])  # JSON array
        if isinstance(channels, list):
            for ch in channels:
                if isinstance(ch, dict) and ch.get("name") == name:
                    cid = _extract_channel_id(ch)
                    if cid:
                        return cid
    except Exception:
        pass
    created = bus._run(
        ["channels", "create", "--name", name, "--type", "stream", "--visibility", "open"]
    )
    cid = _extract_channel_id(created)
    if not cid:
        raise SystemExit(f"could not determine channel id from: {created!r}")
    return cid


def make_hermes(cfg: dict) -> HermesAdapter:
    h = cfg.get("hermes", {})
    return HermesAdapter(
        HermesConfig(
            backend=h.get("backend", "hermes_cli"),
            model=h.get("model", "deepseek-v4-flash-0731"),
            cli_template=h.get("cli_template", HermesConfig.cli_template),
            cli_timeout=int(h.get("cli_timeout", 600)),
            base_url=h.get("base_url", HermesConfig.base_url),
            api_key_env=h.get("api_key_env", HermesConfig.api_key_env),
            request_timeout=int(h.get("request_timeout", 300)),
        )
    )


def publish_task(queen, channel_id, job, max_turns, assigned_to):
    """Sign + publish one task event. Returns (task_event, task_env)."""
    task_env = queen.make_envelope(
        "task",
        job["task_id"],
        goal=job["goal"],
        success_criteria=job.get("success_criteria", []),
        require_all=job.get("require_all", True),
        workdir=job.get("workdir"),
        max_turns=max_turns,
        assigned_to=assigned_to,
    )
    return queen.publish(channel_id, task_env), task_env


def dispatch_only(cfg, ids, allowlist, channel_id, jobs):
    """Publish all tasks as signed events and exit — external workers pick them up."""
    queen = BuzzBus(cfg["relay_url"], ids["orchestrator"], allowlist)
    mode = cfg.get("claim_mode", "assigned")
    workers = ids["workers"]
    for i, job in enumerate(jobs):
        max_turns = int(job.get("max_turns", cfg.get("max_turns", 5)))
        assigned_to = None if mode == "pull" else workers[i % len(workers)].pubkey_hex
        publish_task(queen, channel_id, job, max_turns, assigned_to)
        who = "any (pull)" if assigned_to is None else allowlist.get(assigned_to, assigned_to[:12])
        print(f"[queen] published {job['task_id']} -> {who}")
    print(f"[queen] dispatched {len(jobs)} task(s) to channel {channel_id}; "
          f"run run_worker.py processes to execute them.")


def dispatch_one(cfg, ids, allowlist, channel_id, worker_id, hermes, job, extra_turns=0):
    """Publish the signed task (as queen) then run it to completion (as worker)."""
    max_turns = int(job.get("max_turns", cfg.get("max_turns", 5))) + extra_turns

    queen = BuzzBus(cfg["relay_url"], ids["orchestrator"], allowlist)
    task_event, task_env = publish_task(queen, channel_id, job, max_turns, worker_id.pubkey_hex)
    task_event_id = task_event.get("id") if isinstance(task_event, dict) else None

    # The worker verifies the queen's signed task before acting on it.
    worker_bus = BuzzBus(cfg["relay_url"], worker_id, allowlist)
    verified_env = worker_bus.verify_event(task_event) if task_event_id else task_env

    return run_worker_task(
        bus=worker_bus,
        hermes=hermes,
        governor=Governor(),
        channel_id=channel_id,
        task_env=verified_env,
        task_event_id=task_event_id,
        workdir=job.get("workdir"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="harness/config.yaml")
    ap.add_argument("--jobs", default="jobs.json")
    ap.add_argument("--dispatch-only", action="store_true",
                    help="publish signed tasks and exit; external run_worker.py processes execute them")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    ids = load_identities(Path(cfg["identities_file"]))
    allowlist = build_allowlist(ids)
    hermes = make_hermes(cfg)

    jobs = json.loads(Path(args.jobs).read_text())["jobs"]
    if len(ids["workers"]) == 0:
        raise SystemExit("need at least one worker identity in keys.json")

    queen = BuzzBus(cfg["relay_url"], ids["orchestrator"], allowlist)
    channel_id = ensure_channel(queen, cfg)

    if args.dispatch_only:
        dispatch_only(cfg, ids, allowlist, channel_id, jobs)
        return 0

    print(f"[queen] channel={channel_id}  jobs={len(jobs)}  "
          f"pool={cfg.get('worker_pool', 4)}  max_turns={cfg.get('max_turns', 5)}")

    pool = int(cfg.get("worker_pool", 4))
    results: dict[str, Any] = {}

    with cf.ThreadPoolExecutor(max_workers=pool) as ex:
        futures = {}
        for i, job in enumerate(jobs):
            worker_id = ids["workers"][i % len(ids["workers"])]
            fut = ex.submit(dispatch_one, cfg, ids, allowlist, channel_id, worker_id, hermes, job)
            futures[fut] = job["task_id"]
        for fut in cf.as_completed(futures):
            tid = futures[fut]
            try:
                res = fut.result()
                results[tid] = {"status": res.status, "turns": res.turns_used}
                print(f"[queen] {tid}: {res.status} in {res.turns_used} turn(s)")
            except Exception as e:  # noqa: BLE001
                results[tid] = {"status": "error", "error": str(e)}
                print(f"[queen] {tid}: ERROR {e}")

    # Checkpoint policy: decide whether stalled tasks are worth continuing.
    policy = cfg.get("checkpoint_policy", "stop")
    if policy == "grant_once":
        stalled = [j for j in jobs if results.get(j["task_id"], {}).get("status") == "needs_review"]
        if stalled:
            extra = int(cfg.get("grant_extra_turns", 5))
            print(f"[queen] granting {extra} more turns to {len(stalled)} stalled task(s)")
            with cf.ThreadPoolExecutor(max_workers=pool) as ex:
                futures = {}
                for i, job in enumerate(stalled):
                    worker_id = ids["workers"][i % len(ids["workers"])]
                    fut = ex.submit(dispatch_one, cfg, ids, allowlist, channel_id,
                                    worker_id, hermes, job, extra)
                    futures[fut] = job["task_id"]
                for fut in cf.as_completed(futures):
                    tid = futures[fut]
                    res = fut.result()
                    results[tid] = {"status": res.status, "turns": res.turns_used, "extended": True}
                    print(f"[queen] {tid} (extended): {res.status} in {res.turns_used} turn(s)")

    summary = {"channel_id": channel_id, "ts": int(time.time()), "results": results}
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
