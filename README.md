# Buzz × Hermes agentic harness

A parallel task harness where a **queen** orchestrator issues jobs to a pool of
**Hermes** worker subagents (running `deepseek-v4-flash-0731`) over a **Buzz**
relay. Every task and result is a Schnorr-signed, timestamped Nostr event, and no
task is allowed to run more than **5 turns** before it is checked against its
success criteria.

```
                 signed task events (Nostr, created_at + Schnorr sig)
   ┌──────────┐  ─────────────────────────────►  ┌──────────────────┐
   │  queen   │        Buzz relay channel         │  worker pool     │
   │ (orch.)  │  ◄─────────────────────────────  │  Hermes+deepseek │
   └──────────┘   signed progress/result events   └──────────────────┘
        │                                                  │
        │  governor: ≤5 turns/task, then checkpoint        │  verifies every
        └──────────────────────────────────────────────────  event before acting
```

Why this shape: Buzz *is* a Nostr relay, so "timestamped + signed for authenticity"
is native — every message carries `created_at` and a BIP-340 signature, and the
queen and each worker hold distinct keypairs, so the channel is a self-auditing
log of who asked for what and who did what. Hermes brings the model, the tools,
and its own `delegation` toolset for further sub-subagents.

## What's here

| File | Role |
|------|------|
| `harness/buzz_bus.py` | Signed bus over `buzz-cli`; **independently** verifies Schnorr sig + freshness + author allowlist |
| `harness/governor.py` | 5-turn budget + success-criteria gate (CONTINUE / COMPLETE / CHECKPOINT) |
| `harness/hermes_adapter.py` | The one place we invoke Hermes/deepseek (CLI or hosted endpoint) |
| `harness/worker.py` | Verifies a task, runs governed turns, posts signed progress/result (shared library) |
| `harness/run_worker.py` | **Standalone** worker process — joins the channel, polls for signed tasks, coordinates via assigned/pull |
| `harness/orchestrator.py` | The queen: parallel signed dispatch + checkpoint policy (`--dispatch-only` for external workers) |
| `harness/gen_keys.py` | Mints Nostr keypairs (`nsec`/`npub`) for queen + workers |
| `harness/config.yaml` | Relay, pool size, turn cap, model backend |
| `jobs.example.json` | Sample parallel jobs with declarative success criteria |
| `harness/test_smoke.py` | Offline proof the crypto + governor behave (no relay needed) |

> `keys.json.SAMPLE-DELETE-ME` is a throwaway from testing — delete it. Real keys
> come from `gen_keys.py` and must stay secret.

## Prerequisites

- **Hermes** — already installed on this box. Point it at your model once:
  ```bash
  hermes model            # select provider + deepseek-v4-flash-0731
  hermes tools            # ensure the `delegation` and `terminal` toolsets are on
  ```
- **Buzz relay** — not installed yet. Stand one up (Docker + the toolchain):
  ```bash
  git clone https://github.com/block/buzz.git && cd buzz
  . ./bin/activate-hermit          # pinned toolchain
  just setup && just build
  just relay                       # serves ws://localhost:3000
  cargo install --path crates/buzz-cli   # puts `buzz` on PATH
  ```
  (One-click hosted relay alternative: the Railway button in the Buzz README.)
- **Python deps** for the harness:
  ```bash
  pip install -r requirements.txt
  ```

## Setup

```bash
# 1. Mint identities: one queen + N workers (one keypair per parallel slot)
python harness/gen_keys.py --workers 4 --out keys.json

# 2. Configure — edit harness/config.yaml:
#    - relay_url            -> your relay (ws://localhost:3000 by default)
#    - worker_pool          -> parallelism (≤ number of worker keys)
#    - max_turns: 5         -> the hard per-task budget
#    - hermes.backend       -> "hermes_cli" (real) or "openai_compat" (smoke)
#    - hermes.cli_template  -> confirm the one-shot flag via `hermes --help`

# 3. (optional) copy env
cp .env.example .env
```

The harness feeds each identity's `nsec` to `buzz-cli` via `BUZZ_PRIVATE_KEY`
automatically — you never export it by hand.

## Run

### A) All-in-one (queen runs the pool in-process)

```bash
python harness/orchestrator.py --config harness/config.yaml --jobs jobs.example.json
```

The queen creates (or reuses) the `hive-tasks` channel, publishes each job as a
signed task event, and runs the worker pool as threads. Per-task status streams;
a JSON summary prints at the end.

### B) Distributed (queen dispatches, standalone workers execute)

Run the queen in dispatch-only mode — it publishes signed tasks and exits:

```bash
python harness/orchestrator.py --config harness/config.yaml --jobs jobs.example.json --dispatch-only
```

Then run one worker process per identity, on this box or on separate machines
(each needs `keys.json`, `config.yaml`, and its own Hermes/deepseek):

```bash
# machine A
python harness/run_worker.py --config harness/config.yaml --identity worker-1
# machine B
python harness/run_worker.py --config harness/config.yaml --identity worker-2
```

Workers subscribe to the channel, verify each signed task, run it under the same
5-turn governor, and post signed results. The relay is the only coordination
point — workers never talk to the queen or each other directly. Add `--once` for
a single pass (handy under cron/systemd).

**Coordination mode** (`claim_mode` in config, or `--mode`):

- `assigned` (default) — the queen routes each task to one worker's pubkey; a
  worker only takes tasks addressed to it. No races.
- `pull` — the queen leaves tasks unassigned; any idle worker posts a signed
  `claim` and the earliest claim (by `created_at`, then event id) wins off the
  shared log. Best for heterogeneous machines taking whatever's next.

Either way, workers skip tasks that already have a `result`/`checkpoint` on the
channel and persist processed ids to `.seen-<worker>.json` so a restart doesn't
redo finished work.

### Follow the audit trail

```bash
buzz messages get --channel <channel-uuid> --limit 50 | jq
```

## The 5-turn rule (governor)

After **every** worker turn the governor evaluates the task's `success_criteria`
and returns one of:

- **COMPLETE** — criteria satisfied → post signed `result` (status `done`), stop.
- **CONTINUE** — not yet met, budget remains → run another turn.
- **CHECKPOINT** — `max_turns` (5) reached without success → stop and post a signed
  `checkpoint` (status `needs_review`). The task is **not** silently continued.

What happens at a checkpoint is your call via `checkpoint_policy` in config:
`stop` (default — hand back for human review) or `grant_once` (extend by
`grant_extra_turns` a single time, then stop for good). This is the "check whether
it's worth continuing or already good enough" gate you asked for.

Success criteria are declarative (in `jobs.json`):

```json
{"kind": "contains",      "value": "STATUS: done"}
{"kind": "regex",         "value": "def test_.*token"}
{"kind": "json_path_eq",  "path": "tests.passed", "expected": true}
```

Cheap deterministic probes run first; an optional fuzzy judge (a Hermes/deepseek
call) is consulted only when the probes are inconclusive, so you don't burn model
calls to decide "good enough."

## Security model

1. **Signed** — every event is BIP-340 Schnorr-signed by the sender's Nostr key
   (Buzz native). `buzz_bus.verify_event` recomputes the NIP-01 event id and
   verifies the signature itself — it does not merely trust that the relay
   accepted the message.
2. **Timestamped** — each event's `created_at` is checked against a freshness
   window (reject stale or future-dated events; defaults ±120s future / 24h age).
3. **Authenticated** — only keys in the allowlist (queen + registered workers) are
   trusted; anything else is dropped before a worker acts on it.
4. **Fail-closed** — set `HARNESS_REQUIRE_SIG=1` to refuse any event whose raw
   Nostr fields (and thus signature) can't be independently verified, with no
   fallback to relay-only trust.

Keys live in `keys.json` (chmod 600). Anyone with an `nsec` can post as that
identity — treat it like an SSH private key.

## Verify it works without a relay

```bash
python harness/test_smoke.py
```

Forges real signed events and asserts the harness accepts a valid one and rejects
tampered content, an unknown author, and stale/future timestamps — plus that the
governor stops at exactly 5 turns and completes when criteria are met.

## Two things to confirm for your box

- **Hermes one-shot invocation.** `hermes.cli_template` in `config.yaml` assumes a
  non-interactive run flag. Check `hermes --help` and adjust if your build differs.
  To validate the rest of the pipeline first, set `hermes.backend: openai_compat`
  and point `base_url`/`HERMES_MODEL_API_KEY` at your hosted deepseek endpoint.
- **Buzz event JSON shape.** `verify_event` does full signature verification when
  `buzz-cli` returns the raw Nostr fields (`id`, `pubkey`, `sig`, …). If your relay
  build returns a reduced object, it falls back to allowlist + freshness unless
  `HARNESS_REQUIRE_SIG=1` — in which case run one `buzz messages get` and confirm
  the fields are present.
```
