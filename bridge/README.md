# The chat ↔ harness bridge

This is the piece that lets Claude push jobs to your Hermes workers while you just
give feedback in chat. Claude writes a job file into `inbox/`; the **watcher**
(running here on your box) executes it through the harness and writes a status
report into `outbox/`; Claude reads the report and tells you what happened.

Everything runs on this machine. Claude never runs commands here and never touches
your private keys — it only writes plain job JSON and reads reports.

```
Claude (chat)  ──writes──▶  bridge/inbox/job-*.json
                                   │
                            watcher.py  ──runs──▶  harness/orchestrator.py
                                   │                 (signed dispatch to Hermes
                                   │                  over Buzz, 5-turn governor)
                                   ▼
Claude (chat)  ◀──reads──  bridge/outbox/OK_*.report.md   (all good)
                           bridge/outbox/PROBLEM_*.report.md  (needs you)
```

## Folders

- `inbox/` — new jobs land here (Claude writes them).
- `processing/` — the job currently running.
- `outbox/` — reports Claude reads. `OK_…` = done, `PROBLEM_…` = needs attention.
- `done/` — finished job files, for history.
- `status.json` — live watcher state (idle/running, last result).
- `watcher.log` — running log.

## Start it (pick one)

**Option 1 — run as a service (recommended, auto-starts):**

```bash
# optional: cp bridge/watcher.env.example bridge/watcher.env  and edit
sudo cp bridge/hermes-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-bridge
systemctl status hermes-bridge          # confirm it's active
```

**Option 2 — run it in a terminal (quick test):**

```bash
cp bridge/watcher.env.example bridge/watcher.env   # optional
./bridge/start-watcher.sh                          # foreground, Ctrl-C to stop
# or background:
nohup ./bridge/start-watcher.sh > bridge/watcher.out 2>&1 &
```

## Requirements (all already true on your box)

- The harness venv at `.venv` (with PyYAML), and `buzz` on PATH.
- Hermes configured for the model, and the Buzz relay reachable.
- If the relay needs an auth token, set `BUZZ_AUTH_TOKEN` in `bridge/watcher.env`.

## Day-to-day

You don't touch this folder. You talk to Claude:

- *"Have Hermes do X"* → Claude drops a job in `inbox/`, the watcher runs it,
  Claude reports back. Auto-run is on: Claude dispatches without asking each time
  and only comes back to you when a `PROBLEM_` report shows up (a task failed or hit
  the 5-turn budget and needs review).
- *"What's the status?"* → Claude reads `status.json` and the latest reports.

To pause everything: `sudo systemctl stop hermes-bridge` (or Ctrl-C the terminal).

## Dashboard (web UI + JSON API)

`bridge/dashboard.py` serves a read-only live dashboard of the bridge: running and
queued jobs (with elapsed time and estimated next update), watcher heartbeat, and
the full job history with per-task status, turns, worker attribution, and the
original reports — plus a token-gated JSON API for outside automations.

```bash
sudo cp bridge/hermes-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-dashboard
```

Browse **http://localhost:8787** (or `http://<DIZCORYZ-LAN-IP>:8787`). The page asks
once for the API token: set `DASH_TOKEN` in `bridge/watcher.env`, or use the
auto-generated `bridge/dashboard.token` (chmod 600, gitignored).

API (send `Authorization: Bearer <token>` or `?token=<token>`):

| Endpoint | Returns |
|----------|---------|
| `GET /api/all` | everything in one payload |
| `GET /api/status` | watcher heartbeat/state |
| `GET /api/jobs` | queued + running |
| `GET /api/history` | finished jobs, newest first |
| `GET /api/jobs/<name>` | one job incl. full report markdown |

The server reads only public identity fields (names/pubkeys) — never nsec keys.
WSL note: Windows reaches WSL via localhost automatically; for other LAN devices
add a portproxy (`netsh interface portproxy add v4tov4 listenport=8787 connectport=8787 connectaddress=<wsl-ip>`).
