#!/usr/bin/env python3
"""
server.py — containerized snapshot-driven dashboard + JSON API for the buzz bridge.

STANDALONE, STD-LIB ONLY (python 3.12). Designed to run INSIDE a docker container.
Does NOT read bridge/ files or keys.json — it only consumes snapshots pushed to it.

Flow:
    bridge/bridge.py  --POST /ingest-->  this server  (Bearer DASH_INGEST_TOKEN)
    browser/users    --GET /api/*------>  this server  (Bearer DASH_TOKEN or ?token=)

Endpoints:
    POST /ingest                       accept a snapshot (build_model() shape), store it
                                       in memory, append one JSON line to /data/snapshots.jsonl
    GET  /healthz                      docker healthcheck, no auth -> 200 'ok'
    GET  /                             dashboard UI (same HTML as bridge/dashboard.py PAGE,
                                       plus snapshot age + red heartbeat when stale > 30s)
    GET  /api/all                      latest snapshot (with snapshot_age metadata)
    GET  /api/status                   snapshot['watcher']
    GET  /api/jobs                     {queued, running}
    GET  /api/history                  snapshot['history']
    GET  /api/jobs/<name>              one job resolved from snapshot history/running/queued
                                       (history entries already carry report_markdown)

Auth:  all /api/* require `Authorization: Bearer *** or `?token=<DASH_TOKEN>`
       compared in constant time (hmac.compare_digest). CORS + OPTIONS preflight supported.
       If no snapshot received yet, /api/* return 503 {"error":"no snapshot yet"}.

Run:   python3 server.py     (env: DASH_INGEST_TOKEN, DASH_TOKEN, DASH_PORT=8787, DASH_BIND=0.0.0.0)
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BIND = os.environ.get("DASH_BIND", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8787"))
DASH_REFRESH_SECONDS = int(os.environ.get("DASH_REFRESH_SECONDS", "60"))

# Auth tokens (both required for full function).
TOKEN = os.environ.get("DASH_TOKEN", "")               # clients reading /api/*
INGEST_TOKEN = os.environ.get("DASH_INGEST_TOKEN", "")  # bridge pushing /ingest

# Where ingested snapshots are appended (one JSON line each). Defaults to the
# docker volume mount; override with DASH_DATA_DIR when testing locally.
DATA_DIR = Path(os.environ.get("DASH_DATA_DIR", "/data"))
SNAPSHOT_LOG = DATA_DIR / "snapshots.jsonl"

# Directory of static assets served at /static/<name> (mounted read-only in
# the container). Override with DASH_STATIC_DIR when testing locally.
STATIC_DIR = Path(os.environ.get("DASH_STATIC_DIR", "/srv/static"))

# Static content-type map (basename extension -> MIME). Anything unknown is
# served as application/octet-stream.
STATIC_TYPES = {
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".css": "text/css",
    ".js": "application/javascript",
}
STATIC_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def serve_static(handler, name: str):
    """Serve one file from STATIC_DIR, securely.

    Rules:
      * basename must match ^[A-Za-z0-9._-]+$  (rejects '/' and '..')
      * after resolving, the real path must still live inside STATIC_DIR
      * extension maps to a content type (default application/octet-stream)
      * Cache-Control: public, max-age=86400
    Returns None if the request is rejected or the file is missing (the caller
    decides the status/body).
    """
    if not STATIC_NAME_RE.match(name):
        return None
    base = STATIC_DIR.resolve()
    candidate = (base / name).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        return None
    body = candidate.read_bytes()
    ctype = STATIC_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return True


# Official Buzz favicon embedded as base64 so it ships inside this single
# file (the container bind-mounts only the .py source). Source:
# https://buzz.xyz/sites/buzz/icon.svg (HTTP 200, 1273 bytes, image/svg+xml)
FAVICON_MIME = "image/svg+xml"
FAVICON_B64 = "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA0NjYgMzA5IiB3aWR0aD0iMzIiIGhlaWdodD0iMzIiPgogIDwhLS0gQnV6eiBiZWUgbWFyayBmYXZpY29uIOKAlCBUUkFOU1BBUkVOVCBiYWNrZ3JvdW5kIHNvIGl0IGZsb2F0cyBvbiB0aGUKICAgICAgIGJyb3dzZXIgY2hyb21lLiBDb2xvciBhZGFwdHMgdG8gdGhlIGJyb3dzZXIncyBjb2xvciBzY2hlbWUgdmlhIHRoZQogICAgICAgZW1iZWRkZWQgbWVkaWEgcXVlcnk6IGluayAoYmxhY2spIGluIGxpZ2h0IG1vZGUsIGNoYXJ0cmV1c2UgKHRoZSB0b3AKICAgICAgIGNvbG9yIG9mIHRoZSBzaXRlIGdyYWRpZW50KSBpbiBkYXJrIG1vZGUuIEdlb21ldHJ5IG1hdGNoZXMgdGhlIGNocm9tZQogICAgICAgYmVlIGxvZ28gaW4gQ2hyb21lLnRzeCAodHdvIHNpZGUgY2lyY2xlcyArIHJvdW5kZWQgYm9keSwgd2l0aCBleWVzIGFuZAogICAgICAgc2xvdHMga25vY2tlZCBvdXQgYnkgdGhlIG1hc2spLiAtLT4KICA8c3R5bGU+CiAgICAubWFyayB7IGZpbGw6ICMyMzFlMWU7IH0KICAgIEBtZWRpYSAocHJlZmVycy1jb2xvci1zY2hlbWU6IGRhcmspIHsKICAgICAgLm1hcmsgeyBmaWxsOiAjZDdkNzJlOyB9CiAgICB9CiAgPC9zdHlsZT4KICA8ZGVmcz4KICAgIDxtYXNrIGlkPSJiZWUtbWFzayI+CiAgICAgIDxjaXJjbGUgY3g9IjkxLjciIGN5PSIxNTQuNSIgcj0iOTEuNyIgZmlsbD0id2hpdGUiLz4KICAgICAgPGNpcmNsZSBjeD0iMzc0LjMiIGN5PSIxNTQuNSIgcj0iOTEuNyIgZmlsbD0id2hpdGUiLz4KICAgICAgPHJlY3QgeD0iMTI4IiB5PSIwIiB3aWR0aD0iMjEwIiBoZWlnaHQ9IjMwOSIgcng9IjM0IiBmaWxsPSJ3aGl0ZSIvPgogICAgICA8ZWxsaXBzZSBjeD0iMTkzLjMiIGN5PSI4NC40IiByeD0iMjciIHJ5PSIyNyIgZmlsbD0iYmxhY2siLz4KICAgICAgPGVsbGlwc2UgY3g9IjI3NiIgY3k9Ijg0LjQiIHJ4PSIyNyIgcnk9IjI3IiBmaWxsPSJibGFjayIvPgogICAgICA8cmVjdCB4PSIxNjYuMyIgeT0iMTU3LjIiIHdpZHRoPSIxMzYuOSIgaGVpZ2h0PSIzOC4zIiByeD0iNSIgZmlsbD0iYmxhY2siLz4KICAgICAgPHJlY3QgeD0iMTY2LjkiIHk9IjIzNS4xIiB3aWR0aD0iMTM2LjIiIGhlaWdodD0iMzcuNiIgcng9IjUiIGZpbGw9ImJsYWNrIi8+CiAgICA8L21hc2s+CiAgPC9kZWZzPgogIDxyZWN0IGNsYXNzPSJtYXJrIiB4PSIwIiB5PSIwIiB3aWR0aD0iNDY2IiBoZWlnaHQ9IjMwOSIgbWFzaz0idXJsKCNiZWUtbWFzaykiLz4KPC9zdmc+Cg=="
FAVICON_BYTES = base64.b64decode(FAVICON_B64)



def iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ state

class SnapshotStore:
    """Holds the latest snapshot in memory and appends every ingest to the log."""

    def __init__(self, log_path: Path):
        self._lock = threading.Lock()
        self._log = log_path
        self._snapshot: dict | None = None
        self._received_at: float | None = None
        log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def snapshot(self):
        with self._lock:
            return self._snapshot

    def ingest(self, snapshot: dict) -> dict:
        """Validate-ish, store, and persist one line. Returns the received record."""
        now = time.time()
        received_iso = iso(now)
        record = {"received_at": received_iso, "snapshot": snapshot}
        with self._lock:
            self._snapshot = snapshot
            self._received_at = now
            try:
                with self._log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                # Logging is best-effort; the in-memory snapshot still updates.
                pass
        return record

    def age_seconds(self) -> float | None:
        with self._lock:
            return (time.time() - self._received_at) if self._received_at is not None else None


STORE = SnapshotStore(SNAPSHOT_LOG)


def build_api_all(snapshot: dict) -> dict:
    """Latest snapshot plus the snapshot-age metadata the UI needs for its header."""
    out = dict(snapshot)
    age = STORE.age_seconds()
    out["snapshot_received_at"] = iso(time.time() - age) if age is not None else None
    out["snapshot_age_seconds"] = int(age) if age is not None else None
    return out


def resolve_job(snapshot: dict, name: str) -> dict | None:
    """Find one job across history (report_markdown already present) / running / queued."""
    for h in snapshot.get("history", []):
        if h.get("job") == name:
            return dict(h)
    for j in list(snapshot.get("running", [])) + list(snapshot.get("queued", [])):
        if j.get("job") == name:
            return dict(j)
    return None


# ------------------------------------------------------------------ HTTP

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="alternate icon" href="/favicon.ico">
<title>Buzz × Hermes — Bridge Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1218;--card:#171c26;--txt:#dfe6f2;--dim:#8a93a6;--ok:#3fbf7f;--bad:#e05555;--run:#e8b93e;--acc:#5b8dee}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--txt);padding:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 10px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--dim);font-size:12px}.card{background:var(--card);border-radius:10px;padding:14px 16px;margin-bottom:10px}
table{width:100%;border-collapse:collapse}th{color:var(--dim);text-align:left;font-weight:500;font-size:12px;padding:6px 10px;border-bottom:1px solid #232a38}
td{padding:7px 10px;border-bottom:1px solid #1d2330;vertical-align:top}
.b{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px}
.ok{background:#12321f;color:var(--ok)}.bad{background:#361414;color:var(--bad)}.run{background:#332a10;color:var(--run)}.q{background:#1a2233;color:var(--acc)}
.mono{font-family:ui-monospace,monospace;font-size:12px}.dim{color:var(--dim)}
#hb{width:9px;height:9px;border-radius:99px;display:inline-block;margin-right:6px}
.stats{display:flex;gap:10px;flex-wrap:wrap}.stat{background:var(--card);border-radius:10px;padding:10px 18px}.stat .n{font-size:22px;font-weight:600}
details summary{cursor:pointer;color:var(--acc)}pre{white-space:pre-wrap;background:#10141c;padding:10px;border-radius:8px;font-size:12px;max-height:420px;overflow:auto}
#tok{position:fixed;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center}
#tok .card{width:340px}input{width:100%;padding:8px;border-radius:6px;border:1px solid #2a3242;background:#10141c;color:var(--txt)}
button{margin-top:10px;padding:8px 14px;border-radius:6px;border:0;background:var(--acc);color:#fff;cursor:pointer}
#ctrl{display:flex;align-items:center;gap:8px;margin:6px 0 14px;color:var(--dim);font-size:12px;flex-wrap:wrap}
#ctrl select{padding:3px 6px;border-radius:6px;border:1px solid #2a3242;background:#10141c;color:var(--txt);font-size:12px}
#ctrl button{margin-top:0;padding:3px 12px;font-size:12px}#countdown{min-width:120px}
</style></head><body>
<div id="tok" style="display:none"><div class="card"><h1>API token</h1>
<p class="sub">Enter the dashboard token (DASH_TOKEN).</p>
<input id="tokin" type="password" placeholder="token"><button onclick="saveTok()">Connect</button></div></div>
<h1><span id="hb"></span>Buzz × Hermes — Bridge Dashboard</h1>
<div class="sub" id="meta">connecting…</div>
<div id="ctrl">Auto-refresh <select id="refresh"><option value="10">10s</option><option value="30">30s</option><option value="60">60s</option><option value="300">300s</option></select><span id="countdown"></span><button id="refreshbtn">Refresh</button></div>
<div class="stats" id="stats" style="margin-top:14px"></div>
<h2>Running</h2><div class="card"><table id="run"><thead><tr><th>Job</th><th>Tasks</th><th>Worker pool</th><th>Started</th><th>Elapsed</th><th>Next update (est.)</th><th>Timeout at</th><th>Tokens (M)</th></tr></thead><tbody></tbody></table></div>
<h2>Queued</h2><div class="card"><table id="qd"><thead><tr><th>Job</th><th>Tasks</th><th>Queued at</th><th>Waiting</th></tr></thead><tbody></tbody></table></div>
<h2>History (audit)</h2><div class="card"><table id="hist"><thead><tr><th>Job</th><th>Outcome</th><th>Tasks (status · turns · worker)</th><th>Started</th><th>Duration</th><th>Tokens (M)</th><th>Reason</th><th>Report</th></tr></thead><tbody></tbody></table></div>
<script>
let T=localStorage.getItem('dash_token')||'';
const REFRESH_OPTS=[10,30,60,300];
const REFRESH_DEFAULT=__REFRESH_DEFAULT__;
let refreshSecs=parseInt(localStorage.getItem('dash_refresh'))||REFRESH_DEFAULT;
if(!REFRESH_OPTS.includes(refreshSecs))refreshSecs=REFRESH_DEFAULT;
let countdown=0,refreshTimer=null,countTimer=null;
function saveTok(){T=document.getElementById('tokin').value.trim();localStorage.setItem('dash_token',T);document.getElementById('tok').style.display='none';tick();}
function needTok(){document.getElementById('tok').style.display='flex';}
function fmts(s){if(s==null)return'—';if(s<90)return s+'s';if(s<5400)return Math.round(s/60)+'m '+(s%60)+'s';return (s/3600).toFixed(1)+'h';}
function esc(x){return (''+(x??'')).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function tokM(v){return (v||0).toFixed(4)+'M';}
function tokCell(t){if(!t)return'<td class="dim">—</td>';const tilde=t.exact===false?' <span title="estimated">~</span>':'';return'<td class="mono">'+tokM(t.input_m)+' in / '+tokM(t.output_m)+' out'+tilde+'</td>';}
function runTokCell(j){if(!j.has_usage)return'<td class="dim">—</td>';const t=j.usage_totals||{};const tilde=t.exact===false?' <span title="estimated">~</span>':'';return'<td class="mono">'+tokM(t.input_m)+' in / '+tokM(t.output_m)+' out'+tilde+'</td>';}
function setRefresh(){refreshSecs=parseInt(document.getElementById('refresh').value);localStorage.setItem('dash_refresh',refreshSecs);restartCountdown();}
function restartCountdown(){countdown=refreshSecs;const cd=document.getElementById('countdown');if(cd)cd.textContent='next refresh in '+refreshSecs+'s';if(refreshTimer)clearInterval(refreshTimer);refreshTimer=setInterval(tick,refreshSecs*1000);if(countTimer)clearInterval(countTimer);countTimer=setInterval(()=>{countdown--;const cd=document.getElementById('countdown');if(cd)cd.textContent='next refresh in '+Math.max(0,countdown)+'s';},1000);}
async function tick(){
 if(!T){needTok();return;}
 let r;try{r=await fetch('/api/all',{headers:{Authorization:'Bearer '+T}});}catch(e){document.getElementById('meta').textContent='fetch failed: '+e;return;}
 if(r.status===401){localStorage.removeItem('dash_token');T='';needTok();return;}
 if(r.status===503){document.getElementById('hb').style.background='var(--bad)';document.getElementById('meta').textContent='no snapshot yet — bridge has not pushed an update';return;}
 const d=await r.json();
 const age=(d.snapshot_age_seconds==null?null:d.snapshot_age_seconds);
 const stale=age!=null&&age>30;
 document.getElementById('hb').style.background=stale?'var(--bad)':'var(--ok)';
 document.getElementById('meta').textContent='watcher '+(d.watcher.alive?'alive':'STALE')+' · state '+(d.watcher.state||'?')+' · snapshot age '+(age==null?'—':fmts(age))+((stale)?' · <b>STALE</b>':'')+' · heartbeat '+(d.watcher.updated||'?')+' · received '+(d.snapshot_received_at||'?');
 const tk=d.stats&&d.stats.tokens?d.stats.tokens:{billable_m:0,total_m:0,cost_usd:0};
 const statCards=[
  {l:'total completed',v:d.stats.total_completed},
  {l:'ok',v:d.stats.ok},
  {l:'problems',v:d.stats.problems},
  {l:'avg duration',v:fmts(d.stats.avg_duration_seconds)},
  {l:'Tokens I/O (M)',v:tokM(tk.billable_m)},
  {l:'Cache (M)',v:tokM(tk.cache_m)},
  {l:'est. cost (usd)',v:'$'+(tk.cost_usd||0).toFixed(4)},
 ];
 document.getElementById('stats').innerHTML=statCards.map(s=>'<div class="stat"><div class="n">'+s.v+'</div><div class="sub">'+s.l+'</div></div>').join('');
 document.querySelector('#run tbody').innerHTML=d.running.length?d.running.map(j=>'<tr><td class="mono">'+esc(j.job)+' <span class="b run">running</span></td><td>'+j.tasks.map(t=>esc(t.task_id)).join('<br>')+'</td><td>'+j.assigned_pool.join(', ')+'</td><td class="mono">'+esc(j.started_at)+'</td><td>'+fmts(j.elapsed_seconds)+'</td><td class="mono">'+esc(j.next_status_update_expected)+'</td><td class="mono dim">'+esc(j.timeout_at)+'</td>'+runTokCell(j)+'</tr>').join(''):'<tr><td colspan="8" class="dim">nothing running</td></tr>';
 document.querySelector('#qd tbody').innerHTML=d.queued.length?d.queued.map(j=>'<tr><td class="mono">'+esc(j.job)+' <span class="b q">queued</span></td><td>'+j.tasks.map(t=>esc(t.task_id)).join('<br>')+'</td><td class="mono">'+esc(j.queued_at)+'</td><td>'+fmts(j.waiting_seconds)+'</td></tr>').join(''):'<tr><td colspan="4" class="dim">queue empty</td></tr>';
 document.querySelector('#hist tbody').innerHTML=d.history.length?d.history.map(h=>'<tr><td class="mono">'+esc(h.job)+'</td><td><span class="b '+(h.outcome==='ok'?'ok':'bad')+'">'+h.outcome+'</span></td><td>'+h.tasks.map(t=>esc(t.task_id)+' · <span class="'+(t.status==='done'?'':'dim')+'">'+esc(t.status)+'</span> · '+esc(t.turns)+'t · '+esc(t.assigned_to)).join('<br>')+'</td><td class="mono">'+esc(h.started||h.finished)+'</td><td>'+fmts(h.duration_seconds)+'</td>'+tokCell(h.usage_totals)+'<td class="dim">'+esc(h.reason)+'</td><td><details><summary>view</summary><pre data-job="'+esc(h.job)+'">loading…</pre></details></td></tr>').join(''):'<tr><td colspan="8" class="dim">no history yet</td></tr>';
 document.querySelectorAll('#hist details').forEach(el=>{el.addEventListener('toggle',async()=>{const pre=el.querySelector('pre');if(el.open&&pre.textContent==='loading…'){const rr=await fetch('/api/jobs/'+encodeURIComponent(pre.dataset.job),{headers:{Authorization:'Bearer '+T}});const dd=await rr.json();pre.textContent=dd.report_markdown||'(no report)';}},{once:false});});
}
document.getElementById('refresh').value=String(refreshSecs);
document.getElementById('refresh').addEventListener('change',setRefresh);
document.getElementById('refreshbtn').addEventListener('click',tick);
tick();restartCountdown();
</script></body></html>"""


def page_html() -> str:
    return PAGE.replace("__REFRESH_DEFAULT__", str(DASH_REFRESH_SECONDS))


class Handler(BaseHTTPRequestHandler):
    server_version = "DeployDash/1.0"

    def _authed_read(self, q) -> bool:
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else (q.get("token", [""])[0])
        return hmac.compare_digest(tok, TOKEN)

    def _authed_ingest(self) -> bool:
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else ""
        return hmac.compare_digest(tok, INGEST_TOKEN)

    def _send(self, code: int, body: bytes, ctype: str, cors: bool = True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, indent=2).encode(), "application/json")

    def do_OPTIONS(self):  # CORS preflight
        self._send(204, b"", "text/plain")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if u.path == "/" or u.path == "/index.html":
            return self._send(200, page_html().encode(), "text/html; charset=utf-8")
        m = re.match(r"^/static/([^/]+)$", u.path)
        if m:
            if serve_static(self, m.group(1)) is None:
                return self._json(404, {"error": "not found"})
            return
        if u.path == "/favicon.ico":
            # Prefer the mounted static dir (favicon.svg then favicon.ico);
            # fall back to the embedded base64 bytes if the mount is missing.
            for fname in ("favicon.svg", "favicon.ico"):
                if serve_static(self, fname) is not None:
                    return
            self.send_response(200)
            self.send_header("Content-Type", FAVICON_MIME)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(FAVICON_BYTES)))
            self.end_headers()
            self.wfile.write(FAVICON_BYTES)
            return

        if not u.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authed_read(q):
            return self._json(401, {"error": "unauthorized"})
        snap = STORE.snapshot
        if snap is None:
            return self._json(503, {"error": "no snapshot yet"})
        try:
            if u.path == "/api/all":
                return self._json(200, build_api_all(snap))
            if u.path == "/api/status":
                return self._json(200, snap.get("watcher", {}))
            if u.path == "/api/jobs":
                return self._json(200, {"queued": snap.get("queued", []),
                                        "running": snap.get("running", [])})
            if u.path == "/api/history":
                return self._json(200, snap.get("history", []))
            m = re.match(r"^/api/jobs/([\w.\-]+)$", u.path)
            if m:
                job = resolve_job(snap, m.group(1))
                if job is not None:
                    return self._json(200, job)
                return self._json(404, {"error": f"job {m.group(1)!r} not found"})
            return self._json(404, {"error": "unknown endpoint"})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": str(e)})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path != "/ingest":
            return self._json(404, {"error": "not found"})
        if not self._authed_ingest():
            return self._json(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            snapshot = json.loads(raw.decode("utf-8"))
            if not isinstance(snapshot, dict):
                raise ValueError("ingest body must be a JSON object")
            record = STORE.ingest(snapshot)
            return self._json(200, {"ok": True, "received_at": record["received_at"]})
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": f"bad ingest: {e}"})

    def log_message(self, fmt, *args):  # quiet
        pass


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[deploy-dashboard] serving on http://{BIND}:{PORT}  "
          f"log: {SNAPSHOT_LOG}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
