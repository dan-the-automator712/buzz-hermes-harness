#!/usr/bin/env python3
"""
run_worker.py — a standalone, long-running Hermes worker process.

Instead of the queen running workers as in-process threads, you run one of these
per worker — on the same box or on separate machines — each subscribing to the
Buzz channel and pulling signed tasks off it. The relay is the coordination point;
workers never talk to the queen directly.

    # machine A
    python harness/run_worker.py --config harness/config.yaml --identity worker-1
    # machine B
    python harness/run_worker.py --config harness/config.yaml --identity worker-2

Coordination modes:

  * assigned (default) — a worker only picks up tasks whose signed envelope has
    `assigned_to == my pubkey`. The queen round-robins assignments, so there is no
    race: each task is destined for exactly one worker.

  * pull — any idle worker may grab any unclaimed task. Before working, the worker
    posts a signed `claim` event; the claim with the lowest (created_at, id) wins,
    so double-work is resolved deterministically off the shared log. Good for
    heterogeneous machines where you want whoever's free to take the next job.

Every task is verified (author + freshness + Schnorr) before any work happens, and
already-finished tasks (a `result`/`checkpoint` exists on the channel) are skipped.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

import yaml

from buzz_bus import BuzzBus, Identity
from governor import Governor
from hermes_adapter import HermesAdapter, HermesConfig
from worker import run_worker_task


# ---- identity / config loading (shared shape with orchestrator) -----------

def load_identity(keys_path: Path, name: str) -> tuple[Identity, dict[str, str]]:
    data = json.loads(keys_path.read_text())
    allow: dict[str, str] = {}
    chosen: Optional[Identity] = None

    orch = data["orchestrator"]
    allow[orch["pubkey"]] = orch["name"]
    if orch["name"] == name:
        chosen = Identity(orch["name"], orch["nsec"], orch["pubkey"])

    for w in data["workers"]:
        allow[w["pubkey"]] = w["name"]
        if w["name"] == name:
            chosen = Identity(w["name"], w["nsec"], w["pubkey"])

    if chosen is None:
        raise SystemExit(f"identity {name!r} not found in {keys_path}")
    return chosen, allow


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


# ---- channel resolution (workers join, they don't create) -----------------

def _extract_channel_id(obj) -> Optional[str]:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("channel_id", "id", "uuid", "channel"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) >= 8:
                return v
    return None


def resolve_channel(bus: BuzzBus, cfg: dict) -> str:
    if cfg.get("channel_id"):
        cid = cfg["channel_id"]
    else:
        name = cfg.get("channel_name", "hive-tasks")
        channels = bus._run(["channels", "list"])
        cid = None
        if isinstance(channels, list):
            for ch in channels:
                if isinstance(ch, dict) and ch.get("name") == name:
                    cid = _extract_channel_id(ch)
                    break
        if not cid:
            raise SystemExit(
                f"channel {name!r} not found — start the queen (orchestrator) first "
                f"to create it, or set channel_id in config.yaml"
            )
    # Make sure we're a member so we receive messages.
    try:
        bus._run(["channels", "join", "--channel", cid])
    except Exception:
        pass  # already a member, or open channel
    return cid


# ---- claim coordination (pull mode) ---------------------------------------

def pick_claim_winner(claims: list[dict]) -> Optional[str]:
    """
    Given claim records [{created_at, id, pubkey}, ...] for one task, return the
    winning pubkey deterministically: earliest created_at, then lexicographically
    smallest event id as the tie-breaker.
    """
    if not claims:
        return None
    winner = min(claims, key=lambda c: (c["created_at"], c["id"]))
    return winner["pubkey"]


def _scan_channel(bus: BuzzBus, channel_id: str, limit: int):
    """Return (tasks, terminal_ids, claims_by_task) from verified channel events."""
    tasks: list[tuple[dict, dict]] = []        # (event, envelope) for type==task
    terminal_ids: set[str] = set()             # task_ids with a result/checkpoint
    claims_by_task: dict[str, list[dict]] = {}  # task_id -> [claim records]

    for ev, env in bus.fetch_verified(channel_id, limit=limit):
        t = env.get("type")
        tid = env.get("task_id")
        if not tid:
            continue
        if t == "task":
            tasks.append((ev, env))
        elif t in ("result", "checkpoint"):
            terminal_ids.add(tid)
        elif t == "claim":
            claims_by_task.setdefault(tid, []).append(
                {"created_at": ev.get("created_at", 0), "id": ev.get("id", ""),
                 "pubkey": ev.get("pubkey", "")}
            )
    return tasks, terminal_ids, claims_by_task


def _claim_task(bus: BuzzBus, channel_id: str, task_id: str, task_event_id: Optional[str]) -> bool:
    """Post a signed claim, then confirm we hold the winning claim. Returns True if won."""
    bus.publish(
        channel_id,
        bus.make_envelope("claim", task_id, claimer=bus.identity.pubkey_hex),
        reply_to=task_event_id,
    )
    time.sleep(1.0)  # let the relay settle competing claims
    _, terminal, claims = _scan_channel(bus, channel_id, limit=200)
    if task_id in terminal:
        return False
    return pick_claim_winner(claims.get(task_id, [])) == bus.identity.pubkey_hex


# ---- main poll loop -------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="harness/config.yaml")
    ap.add_argument("--identity", required=True, help="worker name from keys.json, e.g. worker-1")
    ap.add_argument("--mode", choices=["assigned", "pull"], default=None,
                    help="override config claim_mode")
    ap.add_argument("--once", action="store_true", help="single pass then exit")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    ident, allow = load_identity(Path(cfg["identities_file"]), args.identity)
    mode = args.mode or cfg.get("claim_mode", "assigned")
    poll = int(cfg.get("poll_interval", 5))
    limit = int(cfg.get("fetch_limit", 200))

    bus = BuzzBus(cfg["relay_url"], ident, allow)
    hermes = make_hermes(cfg)
    governor = Governor()
    channel_id = resolve_channel(bus, cfg)

    # Persist processed task ids so a restart doesn't redo finished work.
    seen_path = Path(f".seen-{ident.name}.json")
    seen: set[str] = set(json.loads(seen_path.read_text())) if seen_path.exists() else set()

    print(f"[{ident.name}] mode={mode} channel={channel_id} "
          f"pubkey={ident.pubkey_hex[:12]}… polling every {poll}s")

    while True:
        try:
            tasks, terminal, _ = _scan_channel(bus, channel_id, limit=limit)
            for ev, env in tasks:
                tid = env["task_id"]
                if tid in seen or tid in terminal:
                    continue
                if mode == "assigned" and env.get("assigned_to") != ident.pubkey_hex:
                    continue

                task_event_id = ev.get("id")
                if mode == "pull" and not _claim_task(bus, channel_id, tid, task_event_id):
                    seen.add(tid)  # someone else won it
                    continue

                print(f"[{ident.name}] taking {tid}")
                res = run_worker_task(
                    bus=bus, hermes=hermes, governor=governor,
                    channel_id=channel_id, task_env=env,
                    task_event_id=task_event_id, workdir=env.get("workdir"),
                )
                print(f"[{ident.name}] {tid}: {res.status} in {res.turns_used} turn(s)")
                seen.add(tid)
                seen_path.write_text(json.dumps(sorted(seen)))
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            print(f"[{ident.name}] poll error: {e}")

        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
