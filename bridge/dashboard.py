#!/usr/bin/env python3
"""
dashboard.py — real-time web dashboard + JSON API for the bridge.

Read-only. Serves what the bridge already writes (inbox/processing/outbox/done,
status.json) plus worker attribution parsed from job reports. Never reads nsec
private keys — only identity names/pubkeys.

Endpoints (all /api/* require the token; the / page asks for it once):
    GET /                       dashboard UI
    GET /api/all                everything below in one payload (the UI uses this)
    GET /api/status             watcher heartbeat/state
    GET /api/jobs               queued + running jobs
    GET /api/history            finished jobs (audit list, newest first)
    GET /api/jobs/<name>        one job's full detail incl. report markdown

Auth: Authorization: Bearer <token>  or  ?token=<token>
Token: env DASH_TOKEN, else auto-generated once into bridge/dashboard.token (0600).

Run:  .venv/bin/python bridge/dashboard.py     (env: DASH_PORT=8787, DASH_BIND=0.0.0.0)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BRIDGE = Path(__file__).resolve().parent
ROOT = BRIDGE.parent
INBOX, PROCESSING, OUTBOX, DONE = (BRIDGE / d for d in ("inbox", "processing", "outbox", "done"))
STATUS = BRIDGE / "status.json"
POLL_SECONDS = int(os.environ.get("BRIDGE_POLL", "5"))
JOB_TIMEOUT = int(os.environ.get("BRIDGE_JOB_TIMEOUT", "3600"))
BIND = os.environ.get("DASH_BIND", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8787"))


def get_token() -> str:
    tok = os.environ.get("DASH_TOKEN", "").strip()
    if tok:
        return tok
    tf = BRIDGE / "dashboard.token"
    if tf.exists():
        return tf.read_text().strip()
    tok = secrets.token_urlsafe(24)
    tf.write_text(tok)
    try:
        tf.chmod(0o600)
    except OSError:
        pass
    print(f"[dashboard] generated API token -> {tf}")
    return tok


TOKEN = get_token()


def identities() -> dict[str, str]:
    """pubkey-hex -> friendly name. Reads ONLY name/pubkey, never nsec."""
    out: dict[str, str] = {}
    try:
        d = json.loads((ROOT / "keys.json").read_text())
        o = d.get("orchestrator", {})
        if o.get("pubkey"):
            out[o["pubkey"]] = o.get("name", "queen")
        for w in d.get("workers", []):
            if w.get("pubkey"):
                out[w["pubkey"]] = w.get("name", "worker")
    except Exception:  # noqa: BLE001
        pass
    return out


def iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def job_tasks(spec) -> list[dict]:
    if not isinstance(spec, dict):
        return []
    return [
        {"task_id": j.get("task_id"), "goal": (j.get("goal") or "")[:400],
         "workdir": j.get("workdir"), "max_turns": j.get("max_turns", 5)}
        for j in spec.get("jobs", [])
    ]


ASSIGN_RE = re.compile(r"task=(\S+)\s+(\S*)\s*pubkey=([0-9a-f]{6,})…?")


def assignments_from_report(report_text: str, id_map: dict[str, str]) -> dict[str, str]:
    """task_id -> worker name, parsed from the report's signed audit lines."""
    out: dict[str, str] = {}
    for line in report_text.splitlines():
        if "progress" in line or "result" in line or "checkpoint" in line:
            m = ASSIGN_RE.search(line)
            if m:
                tid, prefix = m.group(1), m.group(3)
                for pk, name in id_map.items():
                    if pk.startswith(prefix) and name != "queen":
                        out[tid] = name
    return out


def avg_history_duration(hist: list[dict]) -> float:
    durs = [h["duration_seconds"] for h in hist if h.get("duration_seconds")]
    return sum(durs) / len(durs) if durs else 60.0


def build_model() -> dict:
    now = time.time()
    id_map = identities()

    status = read_json(STATUS) or {}

    queued = []
    for p in sorted(INBOX.glob("*.json")):
        queued.append({
            "job": p.stem, "state": "queued", "queued_at": iso(p.stat().st_mtime),
            "waiting_seconds": int(now - p.stat().st_mtime),
            "tasks": job_tasks(read_json(p)),
        })

    history = []
    for rj in sorted(OUTBOX.glob("*_*.result.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        data = read_json(rj) or {}
        name = data.get("job", rj.stem.replace(".result", ""))
        prefix = "PROBLEM_" if rj.name.startswith("PROBLEM_") else "OK_"
        report_p = OUTBOX / f"{prefix}{name}.report.md"
        report_text = report_p.read_text(errors="replace") if report_p.exists() else ""
        started = finished = None
        m = re.search(r"\*\*Started:\*\* (\S+ \S+?)Z?\s+·\s+\*\*Finished:\*\* (\S+ \S+?)Z?$",
                      report_text, re.MULTILINE)
        if m:
            try:
                started = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                finished = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        duration = int((finished - started).total_seconds()) if started and finished else None
        results = (data.get("summary") or {}).get("results", {})
        assigned = assignments_from_report(report_text, id_map)
        done_spec = read_json(DONE / f"{name}.json")
        history.append({
            "job": name,
            "outcome": "problem" if data.get("needs_attention") else "ok",
            "reason": data.get("reason", ""),
            "finished": data.get("finished"),
            "started": started.strftime("%Y-%m-%dT%H:%M:%SZ") if started else None,
            "duration_seconds": duration,
            "channel_id": (data.get("summary") or {}).get("channel_id"),
            "tasks": [
                {"task_id": tid, "status": r.get("status"), "turns": r.get("turns"),
                 "assigned_to": assigned.get(tid, "unknown")}
                for tid, r in results.items()
            ],
            "spec_tasks": job_tasks(done_spec),
            "report_file": report_p.name if report_p.exists() else None,
        })

    running = []
    avg = avg_history_duration(history)
    for p in sorted(PROCESSING.glob("*.json")):
        started_ts = p.stat().st_mtime
        elapsed = int(now - started_ts)
        eta = started_ts + max(avg, 30)
        running.append({
            "job": p.stem, "state": "running",
            "started_at": iso(started_ts), "elapsed_seconds": elapsed,
            "assigned_pool": [n for n in id_map.values() if n != "queen"],
            "expected_completion_estimate": iso(eta),
            "next_status_update_expected": iso(max(now + POLL_SECONDS, min(eta, started_ts + JOB_TIMEOUT))),
            "timeout_at": iso(started_ts + JOB_TIMEOUT),
            "tasks": job_tasks(read_json(p)),
        })

    watcher_alive = False
    try:
        upd = datetime.strptime((status.get("updated") or ""), "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc)
        watcher_alive = (datetime.now(timezone.utc) - upd).total_seconds() < max(POLL_SECONDS * 6, 30)
    except ValueError:
        pass

    return {
        "generated_at": iso(now),
        "watcher": {**status, "alive": watcher_alive,
                    "poll_seconds": POLL_SECONDS,
                    "next_heartbeat_expected": iso(now + POLL_SECONDS)},
        "identities": [{"name": n, "pubkey": pk} for pk, n in id_map.items()],
        "queued": queued,
        "running": running,
        "history": history,
        "stats": {
            "total_completed": len(history),
            "ok": sum(1 for h in history if h["outcome"] == "ok"),
            "problems": sum(1 for h in history if h["outcome"] == "problem"),
            "avg_duration_seconds": int(avg),
        },
    }


# ---------------------------------------------------------------- HTTP layer

PAGE = """<!doctype html><html><head><meta charset="utf-8">
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
</style></head><body>
<div id="tok" style="display:none"><div class="card"><h1>API token</h1>
<p class="sub">Enter the dashboard token (bridge/dashboard.token or DASH_TOKEN).</p>
<input id="tokin" type="password" placeholder="token"><button onclick="saveTok()">Connect</button></div></div>
<h1><span id="hb"></span>Buzz × Hermes — Bridge Dashboard</h1>
<div class="sub" id="meta">connecting…</div>
<div class="stats" id="stats" style="margin-top:14px"></div>
<h2>Running</h2><div class="card"><table id="run"><thead><tr><th>Job</th><th>Tasks</th><th>Worker pool</th><th>Started</th><th>Elapsed</th><th>Next update (est.)</th><th>Timeout at</th></tr></thead><tbody></tbody></table></div>
<h2>Queued</h2><div class="card"><table id="qd"><thead><tr><th>Job</th><th>Tasks</th><th>Queued at</th><th>Waiting</th></tr></thead><tbody></tbody></table></div>
<h2>History (audit)</h2><div class="card"><table id="hist"><thead><tr><th>Job</th><th>Outcome</th><th>Tasks (status · turns · worker)</th><th>Started</th><th>Duration</th><th>Reason</th><th>Report</th></tr></thead><tbody></tbody></table></div>
<script>
let T=localStorage.getItem('dash_token')||'';
function saveTok(){T=document.getElementById('tokin').value.trim();localStorage.setItem('dash_token',T);document.getElementById('tok').style.display='none';tick();}
function needTok(){document.getElementById('tok').style.display='flex';}
function fmts(s){if(s==null)return'—';if(s<90)return s+'s';if(s<5400)return Math.round(s/60)+'m '+(s%60)+'s';return (s/3600).toFixed(1)+'h';}
function esc(x){return (''+(x??'')).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function tick(){
 if(!T){needTok();return;}
 let r;try{r=await fetch('/api/all',{headers:{Authorization:'Bearer '+T}});}catch(e){document.getElementById('meta').textContent='fetch failed: '+e;return;}
 if(r.status===401){localStorage.removeItem('dash_token');T='';needTok();return;}
 const d=await r.json();
 document.getElementById('hb').style.background=d.watcher.alive?'var(--ok)':'var(--bad)';
 document.getElementById('meta').textContent='watcher '+(d.watcher.alive?'alive':'STALE')+' · state '+(d.watcher.state||'?')+' · heartbeat '+(d.watcher.updated||'?')+' · refreshed '+d.generated_at;
 document.getElementById('stats').innerHTML=['total_completed','ok','problems','avg_duration_seconds'].map(k=>'<div class="stat"><div class="n">'+(k==='avg_duration_seconds'?fmts(d.stats[k]):d.stats[k])+'</div><div class="sub">'+k.replaceAll('_',' ')+'</div></div>').join('');
 document.querySelector('#run tbody').innerHTML=d.running.length?d.running.map(j=>'<tr><td class="mono">'+esc(j.job)+' <span class="b run">running</span></td><td>'+j.tasks.map(t=>esc(t.task_id)).join('<br>')+'</td><td>'+j.assigned_pool.join(', ')+'</td><td class="mono">'+esc(j.started_at)+'</td><td>'+fmts(j.elapsed_seconds)+'</td><td class="mono">'+esc(j.next_status_update_expected)+'</td><td class="mono dim">'+esc(j.timeout_at)+'</td></tr>').join(''):'<tr><td colspan="7" class="dim">nothing running</td></tr>';
 document.querySelector('#qd tbody').innerHTML=d.queued.length?d.queued.map(j=>'<tr><td class="mono">'+esc(j.job)+' <span class="b q">queued</span></td><td>'+j.tasks.map(t=>esc(t.task_id)).join('<br>')+'</td><td class="mono">'+esc(j.queued_at)+'</td><td>'+fmts(j.waiting_seconds)+'</td></tr>').join(''):'<tr><td colspan="4" class="dim">queue empty</td></tr>';
 document.querySelector('#hist tbody').innerHTML=d.history.length?d.history.map(h=>'<tr><td class="mono">'+esc(h.job)+'</td><td><span class="b '+(h.outcome==='ok'?'ok':'bad')+'">'+h.outcome+'</span></td><td>'+h.tasks.map(t=>esc(t.task_id)+' · <span class="'+(t.status==='done'?'':'dim')+'">'+esc(t.status)+'</span> · '+esc(t.turns)+'t · '+esc(t.assigned_to)).join('<br>')+'</td><td class="mono">'+esc(h.started||h.finished)+'</td><td>'+fmts(h.duration_seconds)+'</td><td class="dim">'+esc(h.reason)+'</td><td><details><summary>view</summary><pre data-job="'+esc(h.job)+'">loading…</pre></details></td></tr>').join(''):'<tr><td colspan="7" class="dim">no history yet</td></tr>';
 document.querySelectorAll('#hist details').forEach(el=>{el.addEventListener('toggle',async()=>{const pre=el.querySelector('pre');if(el.open&&pre.textContent==='loading…'){const rr=await fetch('/api/jobs/'+encodeURIComponent(pre.dataset.job),{headers:{Authorization:'Bearer '+T}});const dd=await rr.json();pre.textContent=dd.report_markdown||'(no report)';}},{once:false});});
}
tick();setInterval(tick,5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BridgeDash/1.0"

    def _authed(self, q) -> bool:
        h = self.headers.get("Authorization", "")
        tok = h[7:] if h.startswith("Bearer ") else (q.get("token", [""])[0])
        return hmac.compare_digest(tok, TOKEN)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, indent=2).encode(), "application/json")

    def do_OPTIONS(self):  # CORS preflight for automations
        self._send(204, b"", "text/plain")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/" or u.path == "/index.html":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authed(q):
            return self._json(401, {"error": "unauthorized"})
        try:
            model = build_model()
            if u.path == "/api/all":
                return self._json(200, model)
            if u.path == "/api/status":
                return self._json(200, model["watcher"])
            if u.path == "/api/jobs":
                return self._json(200, {"queued": model["queued"], "running": model["running"]})
            if u.path == "/api/history":
                return self._json(200, model["history"])
            m = re.match(r"^/api/jobs/([\w.\-]+)$", u.path)
            if m:
                name = m.group(1)
                for h in model["history"]:
                    if h["job"] == name:
                        rep = OUTBOX / (h["report_file"] or "")
                        h = dict(h)
                        h["report_markdown"] = rep.read_text(errors="replace") if rep.exists() else None
                        return self._json(200, h)
                for j in model["running"] + model["queued"]:
                    if j["job"] == name:
                        return self._json(200, j)
                return self._json(404, {"error": f"job {name!r} not found"})
            return self._json(404, {"error": "unknown endpoint"})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": str(e)})

    def log_message(self, fmt, *args):  # quiet
        pass


def main():
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[dashboard] serving on http://{BIND}:{PORT}  (token file: {BRIDGE/'dashboard.token'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
