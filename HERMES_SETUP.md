# Handoff: configure the Buzz × Hermes harness

**Audience:** Hermes (the executing agent), running on the box that hosts this
harness and has `deepseek-v4-flash-0731` configured.
**Goal:** wire the harness to the Buzz relay at `https://buzz.bhue.org` and prove a
task can be dispatched, signed, executed under the 5-turn budget, and verified.

Work top to bottom. Each step has a command and an **Expect** line. If a step's
result doesn't match Expect, stop and report — do not continue past a red wire.

---

## 0. Context (read once)

- **Buzz** is a Nostr relay = the signed, timestamped message bus. Every message is
  Schnorr-signed and carries a `created_at` timestamp.
- **You (Hermes)** are the worker. A worker process holds a Buzz keypair, pulls
  signed tasks off the relay, runs them on deepseek, and posts signed results.
  Buzz does not call Hermes directly — the worker process is the bridge.
- **Hard rule:** no task runs more than **5 turns** before the governor checks it
  against its success criteria and either completes it or checkpoints it for review.
- `buzz-cli` speaks the relay's **REST** API over `https://` — **not** `wss://`.

Run everything from the harness root:

```bash
cd buzz-hermes-harness
```

---

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

**Expect:** `coincurve`, `PyYAML`, `requests` install without error.

Confirm `buzz-cli` is on PATH (it is what the harness shells out to):

```bash
command -v buzz || echo "MISSING"
```

**Expect:** a path. If `MISSING`, build it from the Buzz checkout:
`cargo install --path crates/buzz-cli`, then re-check.

---

## 2. Mint agent identities

One orchestrator ("queen") + a pool of workers, each a Nostr keypair.

```bash
python harness/gen_keys.py --workers 4 --out keys.json
```

**Expect:** `wrote keys.json with 1 orchestrator + 4 worker identities`, followed by
one `npub…` per identity. **Record every printed npub** — you need them for step 3.
Treat `keys.json` as secret (chmod 600, never commit, never post its contents).

---

## 3. Authorize the keys on the relay

This depends on how `buzz.bhue.org` was deployed:

- **Open relay:** nothing to do — the first signed post auto-registers each identity.
- **Access-controlled relay** (community membership / allowed-pubkey list): add the
  queen npub and all worker npubs from step 2 to the relay's allowlist. Without this,
  every `buzz-cli` call returns auth error (exit code 3).

If you cannot tell which mode the relay is in, proceed to step 4 — an auth failure
there tells you it's access-controlled and the npubs must be authorized.

---

## 4. Verify the Buzz wire (reachability + auth)

```bash
export BUZZ_RELAY_URL=https://buzz.bhue.org
export BUZZ_PRIVATE_KEY=$(python -c "import json;print(json.load(open('keys.json'))['orchestrator']['nsec'])")
buzz channels list
```

**Expect:** JSON on stdout (an array, possibly `[]`).
- Exit **2** = network/TLS/DNS — check the URL is `https://buzz.bhue.org` and reachable.
- Exit **3** = auth — the queen key isn't authorized; do step 3.

Unset the exported key when done so it isn't inherited by unrelated processes:
`unset BUZZ_PRIVATE_KEY`.

---

## 5. Point the harness config at the relay

Edit `harness/config.yaml` and confirm:

```yaml
relay_url: "https://buzz.bhue.org"     # http/https, NOT wss
channel_name: "hive-tasks"
identities_file: "keys.json"
max_turns: 5                            # do not raise without the operator's approval
worker_pool: 4
```

Leave `max_turns: 5` — it is the operator's hard governance requirement.

---

## 6. Configure the model backend

Two options. **Option A** is the real integration; **Option B** is a fast smoke test.

### Option A — Hermes CLI backend (recommended)

Make sure Hermes is pointed at the model and delegation is on:

```bash
hermes model     # select the hosted provider + deepseek-v4-flash-0731
hermes tools     # enable the `delegation` and `terminal` toolsets
```

In `harness/config.yaml`:

```yaml
hermes:
  backend: "hermes_cli"
  model: "deepseek-v4-flash-0731"
  cli_template: "hermes run --model {model} --no-interactive --input {prompt_file}"
```

**Confirm the one-shot flag** for this Hermes version:

```bash
hermes --help
```

**Expect:** a non-interactive / one-shot run flag. If it differs from
`run --no-interactive --input`, update `cli_template` to match — `{model}` and
`{prompt_file}` are substituted at call time. This is the single most likely thing
to need adjustment; get it right before running real jobs.

### Option B — Hosted endpoint backend (smoke test only)

In `harness/config.yaml`:

```yaml
hermes:
  backend: "openai_compat"
  model: "deepseek-v4-flash-0731"
  base_url: "https://<your-provider>/v1"
  api_key_env: "HERMES_MODEL_API_KEY"
```

```bash
export HERMES_MODEL_API_KEY=<key>
```

---

## 7. Preflight

```bash
python harness/check_setup.py --config harness/config.yaml
```

**Expect:** every line `[  ok ]` and a final
`all wires green — run: python harness/orchestrator.py ...`.
Any `[ FAIL]` line names the exact wire to fix; resolve it and re-run before step 8.

---

## 8. Offline self-test (proves the security + governance layers)

```bash
python harness/test_smoke.py
```

**Expect:** `ALL SMOKE TESTS PASSED`. This confirms, without touching the relay,
that valid signed events verify; tampered content, unknown authors, and
stale/future timestamps are rejected; and the governor stops at exactly 5 turns
and completes when criteria are met.

---

## 9. Run a real job

Start with a single, low-risk job. Either edit `jobs.example.json` or make a
`jobs.json` with one entry, then:

### All-in-one (simplest)

```bash
python harness/orchestrator.py --config harness/config.yaml --jobs jobs.example.json
```

The queen creates the `hive-tasks` channel, publishes signed tasks, and runs the
worker pool. **Expect:** per-task status lines and a final JSON summary.

### Distributed (queen dispatches, standalone workers execute)

```bash
# terminal 1 — publish signed tasks and exit
python harness/orchestrator.py --config harness/config.yaml --jobs jobs.example.json --dispatch-only

# terminal 2..N — one long-running worker per identity (same or separate machines)
python harness/run_worker.py --config harness/config.yaml --identity worker-1
python harness/run_worker.py --config harness/config.yaml --identity worker-2
```

Add `--once` to a worker for a single pass (cron/systemd). Coordination is set by
`claim_mode` in config: `assigned` (queen routes each task to one worker) or `pull`
(any idle worker claims the next task; earliest signed claim wins).

---

## 10. Watch the audit trail

```bash
buzz messages get --channel <channel-uuid> --limit 50 | jq
```

**Expect:** a chronological, signed log of `task` → `claim`/`progress` → `result` or
`checkpoint` events, each with a distinct author pubkey (queen vs. each worker).

---

## Stop-and-report conditions

Report back to the operator (do not silently work around) if any of these occur:

- `buzz channels list` returns exit 3 after the npubs were added to the relay.
- `check_setup.py` still shows a `[ FAIL]` you cannot resolve.
- A task returns `needs_review` (hit the 5-turn budget without meeting criteria) —
  the operator decides whether it's worth continuing.
- You would need to raise `max_turns` above 5, disable signature verification
  (`HARNESS_REQUIRE_SIG`), or bypass the author allowlist to make something pass.

## Definition of done

- `check_setup.py` is all green.
- `test_smoke.py` passes.
- One real job runs end-to-end and its signed `result` (or `checkpoint`) is visible
  in the channel via step 10.
