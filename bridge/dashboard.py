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

Auth: Authorization: Bearer ***  or  ?token=<token>
Token: env DASH_TOKEN, else auto-generated once into bridge/dashboard.token (0600).

Run:  .venv/bin/python bridge/dashboard.py     (env: DASH_PORT=8787, DASH_BIND=0.0.0.0)
"""

from __future__ import annotations

import base64
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
DASH_REFRESH_SECONDS = int(os.environ.get("DASH_REFRESH_SECONDS", "60"))

# Directory of static assets served at /static/<name>. Defaults to the repo's
# deploy/dashboard/static so the local (non-container) UI serves the same file
# the container does. Override with DASH_STATIC_DIR when testing.
STATIC_DIR = Path(os.environ.get("DASH_STATIC_DIR", ROOT / "deploy" / "dashboard" / "static"))

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
    out = []
    for j in spec.get("jobs", []):
        entry = {
            "task_id": j.get("task_id"),
            "goal": (j.get("goal") or "")[:400],
            "workdir": j.get("workdir"),
            "max_turns": j.get("max_turns", 5),
        }
        if isinstance(j.get("usage"), dict):
            entry["usage"] = j["usage"]
        out.append(entry)
    return out


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


def job_usage_totals(task_entries: list[dict]) -> dict:
    """Aggregate per-task `usage` into a job-level totals dict (missing usage -> zeros).

    Each task entry's `usage` may be {"input_tokens","output_tokens","cache_tokens",
    "billable_tokens","total_tokens","api_calls","cost_usd","exact"}. A missing usage
    block contributes zeros and does not crash. `exact` is False if ANY contributing
    task reported exact=false.
    """
    agg = {"input": 0, "output": 0, "billable": 0, "cache": 0, "total": 0,
           "api_calls": 0, "cost_usd": 0.0, "exact": True}
    for t in task_entries:
        u = t.get("usage")
        if not isinstance(u, dict):
            continue
        _in = int(u.get("input_tokens") or 0)
        _out = int(u.get("output_tokens") or 0)
        _tot = int(u.get("total_tokens") or 0)
        agg["input"] += _in
        agg["output"] += _out
        agg["total"] += _tot
        # Derive when absent so usage recorded before these keys existed still
        # aggregates correctly: billable = in+out; cache = whatever total carries
        # beyond in+out (hermes' total_tokens includes cache read/write).
        agg["billable"] += int(u.get("billable_tokens") or (_in + _out))
        _cache = u.get("cache_tokens")
        agg["cache"] += int(_cache) if _cache is not None else max(_tot - _in - _out, 0)
        agg["api_calls"] += int(u.get("api_calls") or 0)
        agg["cost_usd"] += float(u.get("cost_usd") or 0.0)
        if not u.get("exact", True):
            agg["exact"] = False
    return {
        "input": agg["input"],
        "output": agg["output"],
        "billable": agg["billable"],
        "cache": agg["cache"],
        "total": agg["total"],
        "input_m": round(agg["input"] / 1_000_000, 4),
        "output_m": round(agg["output"] / 1_000_000, 4),
        "billable_m": round(agg["billable"] / 1_000_000, 4),
        "cache_m": round(agg["cache"] / 1_000_000, 4),
        "total_m": round(agg["total"] / 1_000_000, 4),
        "cost_usd": round(agg["cost_usd"], 4),
        "api_calls": agg["api_calls"],
        "exact": agg["exact"],
    }


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
        tasks = [
            {"task_id": tid, "status": r.get("status"), "turns": r.get("turns"),
             "assigned_to": assigned.get(tid, "unknown"),
             "usage": r.get("usage") if isinstance(r.get("usage"), dict) else None}
            for tid, r in results.items()
        ]
        usage_totals = job_usage_totals(tasks)
        history.append({
            "job": name,
            "outcome": "problem" if data.get("needs_attention") else "ok",
            "reason": data.get("reason", ""),
            "finished": data.get("finished"),
            "started": started.strftime("%Y-%m-%dT%H:%M:%SZ") if started else None,
            "duration_seconds": duration,
            "channel_id": (data.get("summary") or {}).get("channel_id"),
            "tasks": tasks,
            "usage_totals": usage_totals,
            "spec_tasks": job_tasks(done_spec),
            "report_file": report_p.name if report_p.exists() else None,
        })

    running = []
    avg = avg_history_duration(history)
    for p in sorted(PROCESSING.glob("*.json")):
        started_ts = p.stat().st_mtime
        elapsed = int(now - started_ts)
        eta = started_ts + max(avg, 30)
        rtasks = job_tasks(read_json(p))
        running.append({
            "job": p.stem, "state": "running",
            "started_at": iso(started_ts), "elapsed_seconds": elapsed,
            "assigned_pool": [n for n in id_map.values() if n != "queen"],
            "expected_completion_estimate": iso(eta),
            "next_status_update_expected": iso(max(now + POLL_SECONDS, min(eta, started_ts + JOB_TIMEOUT))),
            "timeout_at": iso(started_ts + JOB_TIMEOUT),
            "tasks": rtasks,
            "usage_totals": job_usage_totals(rtasks),
            "has_usage": any(isinstance(t.get("usage"), dict) for t in rtasks),
        })

    watcher_alive = False
    try:
        upd = datetime.strptime((status.get("updated") or ""), "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc)
        watcher_alive = (datetime.now(timezone.utc) - upd).total_seconds() < max(POLL_SECONDS * 6, 30)
    except ValueError:
        pass

    job_totals = [h["usage_totals"] for h in history]
    t_in = sum(t["input"] for t in job_totals)
    t_out = sum(t["output"] for t in job_totals)
    t_bill = sum(t["billable"] for t in job_totals)
    t_cache = sum(t["cache"] for t in job_totals)
    t_tot = sum(t["total"] for t in job_totals)
    t_cost = sum(t["cost_usd"] for t in job_totals)
    tokens = {
        "input": t_in,
        "output": t_out,
        "billable": t_bill,
        "cache": t_cache,
        "total": t_tot,
        "input_m": round(t_in / 1_000_000, 4),
        "output_m": round(t_out / 1_000_000, 4),
        "billable_m": round(t_bill / 1_000_000, 4),
        "cache_m": round(t_cache / 1_000_000, 4),
        "total_m": round(t_tot / 1_000_000, 4),
        "cost_usd": round(t_cost, 4),
    }

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
            "tokens": tokens,
        },
    }


# ---------------------------------------------------------------- HTTP layer

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
<p class="sub">Enter the dashboard token (bridge/dashboard.token or DASH_TOKEN).</p>
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
 const d=await r.json();
 document.getElementById('hb').style.background=d.watcher.alive?'var(--ok)':'var(--bad)';
 document.getElementById('meta').textContent='watcher '+(d.watcher.alive?'alive':'STALE')+' · state '+(d.watcher.state||'?')+' · heartbeat '+(d.watcher.updated||'?')+' · refreshed '+d.generated_at;
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
            return self._send(200, page_html().encode(), "text/html; charset=utf-8")
        m = re.match(r"^/static/([^/]+)$", u.path)
        if m:
            if serve_static(self, m.group(1)) is None:
                return self._json(404, {"error": "not found"})
            return
        if u.path == "/favicon.ico":
            # Prefer the static dir (favicon.svg then favicon.ico); fall back
            # to the embedded base64 bytes if the dir/file is missing.
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
