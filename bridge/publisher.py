#!/usr/bin/env python3
"""
publisher.py — periodic snapshot publisher for the Buzz x Hermes bridge.

Runs on this machine next to bridge/dashboard.py. Every PUBLISH_INTERVAL
seconds it imports build_model from the sibling bridge/dashboard.py, builds the
current snapshot, ENRICHES each history entry by reading its report file from
bridge/outbox/ into a new key report_markdown, then POSTs the snapshot as JSON
to DASH_URL with 'Authorization: Bearer <DASH_INGEST_TOKEN>'.

Uses only urllib.request (stdlib). Network errors never crash it — they are
logged to bridge/publisher.log and retried on the next cycle.

Env:
    PUBLISH_INTERVAL    seconds between publishes (default 10)
    DASH_URL            ingest endpoint (default http://192.168.13.2:9009/ingest)
    DASH_INGEST_TOKEN   bearer token, sent as 'Authorization: Bearer <token>'

Note: importing bridge/dashboard.py must NOT start its web server. dashboard.py
only calls main() under `if __name__ == "__main__":`, so loading it here as a
module is safe (it reads/generates a token file but serves nothing).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urllib_request

BRIDGE = Path(__file__).resolve().parent
OUTBOX = BRIDGE / "outbox"
LOG = BRIDGE / "publisher.log"

PUBLISH_INTERVAL = int(os.environ.get("PUBLISH_INTERVAL", "10"))
DASH_URL = os.environ.get("DASH_URL", "http://192.168.13.2:9009/ingest")
DASH_INGEST_TOKEN = os.environ.get("DASH_INGEST_TOKEN", "")

logging.basicConfig(
    filename=str(LOG),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_build_model():
    """Import build_model from the sibling bridge/dashboard.py by path.

    Using importlib (rather than a plain `import dashboard`) keeps the module
    loaded under a unique name so it cannot shadow or collide with anything on
    sys.path. dashboard.py only serves HTTP under `if __name__ == "__main__":`,
    so importing it never starts a web server.
    """
    spec = importlib.util.spec_from_file_location(
        "bridge_dashboard", BRIDGE / "dashboard.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {BRIDGE / 'dashboard.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_model


def enrich(snapshot: dict) -> dict:
    """Read each history entry's report file from bridge/outbox into
    report_markdown (None when absent). Mutates and returns the snapshot."""
    for h in snapshot.get("history", []):
        rfile = h.get("report_file")
        h["report_markdown"] = None
        if rfile:
            rep = OUTBOX / rfile
            if rep.exists():
                h["report_markdown"] = rep.read_text(errors="replace")
    return snapshot


def publish(payload: dict) -> None:
    """POST the snapshot JSON to DASH_URL with a bearer token."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        DASH_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + DASH_INGEST_TOKEN,
        },
    )
    with urllib_request.urlopen(req, timeout=30) as resp:
        resp.read()


def main() -> None:
    logging.info("publisher starting: interval=%ss url=%s", PUBLISH_INTERVAL, DASH_URL)
    try:
        build_model = load_build_model()
    except Exception as e:  # noqa: BLE001
        logging.error("failed to import dashboard.build_model: %s", e)
        raise
    while True:
        try:
            snapshot = enrich(build_model())
            publish(snapshot)
            logging.info(
                "published %d history entries to %s",
                len(snapshot.get("history", [])),
                DASH_URL,
            )
        except (urlerror.URLError, TimeoutError, OSError, ValueError) as e:
            logging.error("publish failed (retrying next cycle): %s", e)
        except Exception as e:  # noqa: BLE001 — never let an unexpected error kill us
            logging.error("unexpected error (retrying next cycle): %s", e)
        time.sleep(PUBLISH_INTERVAL)


if __name__ == "__main__":
    main()
