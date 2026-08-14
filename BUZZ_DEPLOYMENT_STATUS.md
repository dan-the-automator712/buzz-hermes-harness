# Buzz × Hermes Harness — Deployment & Handoff Status

**Author:** Hermes (dizcofuz's agent) · **Date:** 2026-08-14
**Audience:** Claude agent picking up work on this harness
**Location:** `E:\HermesOutput\buzz-hermes-harness` (WSL: `/mnt/e/HermesOutput/buzz-hermes-harness`)

---

## TL;DR

Block's **Buzz** relay (a Nostr message bus) is deployed on the **docker01** Docker host
and is **live at `https://buzz.bhue.org`**. A Hermes-based task harness on the box
`DIZCORYZ` talks to it, dispatches signed tasks from a queen orchestrator to a pool of
Hermes workers, and runs them under a hard 5-turn governor budget. **The full loop has
been proven end-to-end**: a real job was dispatched, executed, and its signed result
was read back off the relay.

Everything the operator's handoff doc (`HERMES1.MD`) required is green. This file tells
you exactly what is set up, what passed, what's ready, and how to use it.

---

## 1. What is set up

### The relay (docker01, `192.168.13.2`)
- **Stack:** official `deploy/compose` production bundle → Compose project `buzz-prod`.
- **Containers** (all healthy):
  - `buzz-relay-1` — the Nostr relay + web UI, host port **9008**→3000
  - `buzz-postgres-1` — Postgres 17
  - `buzz-redis-1` — Redis 7
  - `buzz-minio-1` — S3 media storage
  - `buzz-minio-init-1` — one-shot bucket init (exited 0, expected)
- **External:** `https://buzz.bhue.org` (HTTP 200, returns the relay's NIP-11 info doc),
  reached via **HAProxy** (172.16.5.2). Relay `RELAY_URL=wss://buzz.bhue.org`.
- **Closed relay:** `BUZZ_REQUIRE_RELAY_MEMBERSHIP=true` + `BUZZ_REQUIRE_AUTH_TOKEN=true`.
- **Admin on docker01 (compose v1, NOT v2):** the official `run.sh`/`docker compose`
  don't work there. Use `docker exec` directly:
  ```bash
  sudo docker exec buzz-relay-1 /usr/local/bin/buzz-admin list-members
  sudo docker exec buzz-relay-1 /usr/local/bin/buzz-admin add-member --pubkey <hex> --role member
  ```
  `add-member` requires the **`--pubkey <hex>` flag** (positional npub is rejected).

### Relay membership (authorized)
| identity | role | pubkey (hex) |
|----------|------|--------------|
| queen (orchestrator) | **owner** | `1360dc84e6c6ebae8d894d78a522c18b91a857432ea24276b74cf981c0998060` |
| worker-1 | member | `070bd7883f0eecf78907668dfb4823aae293f44abdae13f29d38375a56cac092` |
| worker-2 | member | `5ee124b0cf79dcf5e10150d0f2bb6ccb2f3d403e1700f005b49571630e8c817a` |
| worker-3 | member | `bfa6e0d377b70544687a8ca8b0f609b1b732b67c3a2e0da80436d995fbbcdfb5` |
| worker-4 | member | `0456bf0bb05a6a3531fafb555ec39d1da6acc14d21e177e5c90b770d97da1d61` |

### Secrets
- **Deploy `.env`** at `deploy/compose/.env` (relay private key, Postgres/Redis/S3
  passwords, HMAC secret, owner pubkey) — generated, no defaults, backed up in
  Bitwarden **"Buzz Relay (docker01)"**.
- **Agent identities** (nsec private keys) in `keys.json` (chmod 600) — backed up in
  Bitwarden **"Buzz Hermes Harness Identities"**. **Never commit or echo `keys.json`.**

### Toolchain (box `DIZCORYZ`)
- Rust 1.95.0 via rustup; **`buzz-cli`** built from the checkout at
  `/home/dizcofuz/buzz` (`cargo install --path crates/buzz-cli`) → `~/.cargo/bin/buzz`.
- Python venv `.venv` with `coincurve`, `PyYAML`, `requests`.

### Harness config (`harness/config.yaml`)
```yaml
relay_url: "https://buzz.bhue.org"            # buzz-cli REST (http/https, NOT wss)
channel_name: "hive-tasks"
identities_file: "keys.json"
max_turns: 5
worker_pool: 4
hermes:
  backend: "hermes_cli"
  model: "deepseek/deepseek-v4-flash-0731"     # MUST be provider-qualified
  cli_template: "hermes -m {model} -z {prompt}"
```

---

## 2. What has been tested successfully

| Check | Result |
|-------|--------|
| `harness/check_setup.py` | ✅ **all wires green** (buzz-cli, keys, relay auth, hermes) |
| `harness/test_smoke.py` | ✅ `ALL SMOKE TESTS PASSED` (crypto + 5-turn governor) |
| Relay reachability | ✅ `https://buzz.bhue.org` → HTTP 200 (NIP-11 doc) |
| LAN relay | ✅ `http://192.168.13.2:9008` → HTTP 200 |
| Queen auth on relay | ✅ owner key authenticates over `https://buzz.bhue.org` |
| **Real end-to-end job** | ✅ `hello-buzz` dispatched, executed, `done in 1 turn(s)` |

### Proof of the end-to-end run (channel `9955a8b2-0698-4bd8-9121-851aeb5b6531`)
Signed audit trail read back off the relay:
```
task      pubkey=1360dc84…  (queen)
progress  pubkey=070bd788…  (worker-1) turn 1
result    pubkey=070bd788…  (worker-1) status=done
```
`proof.txt` was written to the workdir with `STATUS: done` + a Buzz note. ✅

---

## 3. What is ready for work

- **The harness is fully operational.** You can dispatch real jobs right now.
- **Workdir convention:** all job files use **`/mnt/e/TEMP/HermesTemp/projects`**
  (Windows `E:\TEMP\HermesTemp\projects`) — present and writable.
- **Job files:**
  - `jobs.first.json` — self-contained smoke job (writes `proof.txt`), ready to run.
  - `jobs.example.json` — 3 example jobs, but they reference fixture files
    (`./service/auth.py`, `./reports/incidents-2026-Q2.md`, `localhost:8080/health`)
    that **don't exist** in the projects dir → these would checkpoint unless fixtures
    are added first.

---

## 4. How to use it

Run everything from the harness root, with the venv active and buzz-cli on PATH:

```bash
cd /mnt/e/HermesOutput/buzz-hermes-harness
. "$HOME/.cargo/env"                    # puts buzz-cli on PATH (if not already)

# 0) Preflight + offline checks (should be all green / pass)
./.venv/bin/python harness/check_setup.py --config harness/config.yaml
./.venv/bin/python harness/test_smoke.py

# 1) Run a real job (all-in-one: queen + in-process worker pool)
./.venv/bin/python harness/orchestrator.py --config harness/config.yaml --jobs jobs.first.json

# 2) Distributed mode (queen dispatches, standalone workers execute)
./.venv/bin/python harness/orchestrator.py --config harness/config.yaml --jobs jobs.first.json --dispatch-only
# then, in other terminals (one per worker identity):
./.venv/bin/python harness/run_worker.py --config harness/config.yaml --identity worker-1
./.venv/bin/python harness/run_worker.py --config harness/config.yaml --identity worker-2

# 3) Read the signed audit trail for a channel
buzz messages get --channel <channel-uuid> --limit 50 | jq
```

### Writing new jobs
Jobs are JSON: `{"jobs": [{task_id, goal, workdir, max_turns, require_all, success_criteria}]}`.
- `workdir` → use `/mnt/e/TEMP/HermesTemp/projects` (or a subdir under it).
- `max_turns` → keep at `5` unless the operator approves more.
- `success_criteria` → declarative `kind` checks against the worker's text output
  (`contains`, `regex`, `json_path_eq`). The worker ends its reply with `STATUS: done`
  when criteria are met.

---

## 5. Gotchas / things that tripped us up (read before touching)

1. **`docker01` runs Docker Compose v1**, not v2. The official `run.sh` and
   `docker compose` syntax fail there. Use **`docker exec buzz-relay-1 …`** for
   membership/admin ops. (Compose v2 plugin can be installed if desired, but not needed
   for the harness.)
2. **`buzz-admin add-member` needs `--pubkey <hex>`**, not a positional npub.
3. **Model name must be provider-qualified** for `hermes -z`:
   `deepseek/deepseek-v4-flash-0731`, NOT `deepseek-v4-flash-0731` (the bare form fails
   with `No LLM provider configured`).
4. **Hermes one-shot flag is top-level `-z`** (or `-z PROMPT -m MODEL`). There is **no
   `hermes run --input`** subcommand in this build.
5. **Buzz binds community → host from `RELAY_URL`.** It must be the public host
   (`wss://buzz.bhue.org`). If set to the LAN IP, external requests 404 with
   `no community is configured for this host`.
6. **This box's `~/.gitconfig` rewrites all `https://github.com/` → SSH** (`git@github.com:`).
   That breaks cargo git deps — build with `GIT_CONFIG_GLOBAL=/dev/null` or keep
   `.cargo/config.toml` with `net.git-fetch-with-cli = true`.
7. **`keys.json` contains nsec private keys** — chmod 600, never commit/share.

---

## 6. Outstanding / not done

- **Nginx Proxy Manager** on docker01 is the **planned** HAProxy replacement but is
  **not deployed**. Bundle ready at `deploy/nginx-proxy-manager/`. External access
  currently works via HAProxy, so this is not blocking.
- **Cloudflare tunnel** public hostname for `buzz.bhue.org` is dashboard-managed and not
  required for current operation (HAProxy path is live).
- `jobs.example.json` fixture files don't exist yet in the projects dir.
- The harness is **not a git repo yet** (no commits).

---

## 7. Where to look

| Thing | Path |
|-------|------|
| Harness repo | `/mnt/e/HermesOutput/buzz-hermes-harness` |
| Relay deploy bundle | `/mnt/e/HermesOutput/buzz-hermes-harness/deploy/compose/` |
| NPM bundle (not deployed) | `/mnt/e/HermesOutput/buzz-hermes-harness/deploy/nginx-proxy-manager/` |
| Buzz source checkout | `/home/dizcofuz/buzz` |
| Harness docs | `README.md` in the harness root |
| Operator handoff (source) | `HERMES1.MD` |
| Wiki (main) | `E:\OneDrive\Obsidian\Homelab\Buzz.md` + `Nginx Proxy Manager.md` |

**Bottom line: the wire is green and proven. Pick a real task, add a job entry with
`workdir` under the projects dir, and dispatch it.**
