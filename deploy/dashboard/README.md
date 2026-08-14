# buzz-dashboard — snapshot dashboard (web UI + JSON API)

Containerized, read-only dashboard for the Buzz × Hermes harness. It runs as a
**separate stack** (`buzz-dashboard`) on **docker01**, alongside — but entirely
independent of — the buzz relay stack. It never touches the relay.

- Host port **9011** (the next free port in the 9000 range on docker01; 9008 is
  the buzz relay — do **not** touch the relay stack or its port).
- Container port **8787** (the dashboard's default `DASH_PORT`).
- Data volume **`dash-data`** mounted at `/data` (published snapshots live here).

## Architecture (publisher → container)

```
DIZCORYZ box                         docker01
─────────────                        ─────────
hermes-publisher.service  ──push──▶  buzz-dashboard container (python:3.12-alpine)
  (bridge/watcher.env)               server.py serves web UI + JSON API
  reads bridge/*.json                reads snapshots from /data (dash-data volume)
  snapshots + POSTs to DASH_URL
```

- **Publisher** runs on the DIZCORYZ box (WSL) as a systemd unit. It snapshots the
  bridge state and pushes it to the container.
- **Container** serves those snapshots to browsers (web UI) and automations
  (JSON API). It holds no private keys — only public identity fields.
- The container is **stateless about the harness**: it just reads whatever
  snapshots have been pushed into `/data`.

## Environment variables

| Variable            | Where            | Purpose                                          |
|---------------------|------------------|--------------------------------------------------|
| `DASH_TOKEN`        | container env    | Read token for the web UI + JSON API (Bearer / `?token=`). |
| `DASH_INGEST_TOKEN` | container env    | Token the publisher must send to POST snapshots. |
| `DASH_URL`          | publisher env    | Full container ingest URL the publisher pushes to (e.g. `http://docker01:9011/ingest`). |
| `PUBLISH_INTERVAL`  | publisher env    | How often (seconds) the publisher snapshots & pushes. |

## Deploy on docker01 (Dockhand) — as a NEW stack

Dockhand is the management container on docker01: **dockhand2** manages stacks
from `/app/data/stacks/Docker01/`.

1. On this box, put the deploy files where Dockhand can stage them, then get the
   `compose.yml` (and `server.py`) onto docker01:

   ```
   # e.g. scp the directory up, then install as a new stack on docker01:
   scp -r deploy/dashboard docker01:/app/data/stacks/Docker01/buzz-dashboard
   ```

   Replace `docker01` with the management target as your Dockhand setup expects.

2. Create the stack entry with Dockhand so it becomes a **new** stack named
   **`buzz-dashboard`** at `/app/data/stacks/Docker01/buzz-dashboard/compose.yml`.
   Do **not** modify the existing buzz relay stack (leave its directory and port
   9008 alone).

3. Provide the two required env vars to the stack (Dockhand's stack env / your
   secrets mechanism). Both are mandatory — compose fails fast if unset:

   - `DASH_TOKEN`
   - `DASH_INGEST_TOKEN`

4. Apply / start the stack (per your Dockhand workflow: `docker compose -f
   .../compose.yml up -d` or the Dockhand equivalent). Confirm:

   ```
   docker port buzz-dashboard-dashboard-1    # expect 0.0.0.0:9011 -> 8787/tcp
   curl -s http://docker01:9011/healthz      # expect 200 OK
   ```

## docker-run fallback (no Dockhand)

If Dockhand isn't available for this stack, a plain run works too:

```
docker run -d --name buzz-dashboard \
  -p 9011:8787 \
  -v "$PWD/server.py:/srv/server.py:ro" \
  -v buzz-dash-data:/data \
  -e DASH_TOKEN="$DASH_TOKEN" \
  -e DASH_INGEST_TOKEN="$DASH_INGEST_TOKEN" \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)\"" \
  --restart unless-stopped \
  python:3.12-alpine \
  python /srv/server.py
```

(`--health-cmd` uses urllib because the alpine image has no wget/curl.)

## DIZCORYZ-side publisher setup

On the DIZCORYZ box the publisher runs as a systemd unit:

- **Unit:** `bridge/hermes-publisher.service` (copy to
  `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now
  hermes-publisher`).
- **Env:** all publisher variables live in `bridge/watcher.env` (loaded via
  `EnvironmentFile` in the unit). At minimum set:

  ```
  DASH_URL=http://docker01:9011/ingest
  DASH_INGEST_TOKEN=<same ingest token set on the container>
  PUBLISH_INTERVAL=30
  ```

The publisher reads the bridge state on this box, snapshots it, and POSTs to
`DASH_URL` with the ingest token. The container validates the token, stores the
snapshot under `/data`, and serves it to the web UI / JSON API.

## Verification

- `curl -s http://docker01:9011/healthz` → `200 OK`.
- Browse the dashboard UI at `http://docker01:9011/` (read token = `DASH_TOKEN`).
- The `dash-data` volume persists snapshots across container restarts.

---
STATUS: done
