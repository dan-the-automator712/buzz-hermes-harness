#!/usr/bin/env python3
"""Validate buzz-hermes-harness.drawio.

Fails (exit 1) if:
  - any two SIBLING vertices overlap by >0 area
  - any edge source/target id does not exist
  - any edge connects to a NOTE/LEGEND box
  - any required component (from the content list) is missing
Prints 'VALIDATION: PASS' or 'VALIDATION: FAIL' plus details.
"""
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(__file__)
DRAWIO = os.path.join(HERE, os.pardir, "buzz-hermes-harness.drawio")

# Node ids marked as NOTE/LEGEND. Any edge touching these is an error.
NOTES = {"legend"}

# Required content. Each entry is checked as a substring against either a node
# label or an edge label. (key, kind) where kind in {'node','edge','any'}.
REQUIRED = [
    # Band 1 - DIZCORYZ harness host
    ("Claude / operator chat", "node"),
    ("bridge/inbox", "node"),
    ("watcher.py", "node"),
    ("hermes-bridge", "node"),
    ("bridge/processing", "node"),
    ("orchestrator.py", "node"),
    ("buzz_bus.py", "node"),
    ("Schnorr", "node"),
    ("worker.py", "node"),
    ("governor.py", "node"),
    ("file_exists", "node"),
    ("hermes_adapter.py", "node"),
    ("deepseek-v4-flash-0731", "node"),
    ("bridge/outbox", "node"),
    ("OK_ / PROBLEM_", "node"),
    ("bridge/done", "node"),
    ("keys.json", "node"),
    ("watcher.env", "node"),
    ("publisher.py", "node"),
    ("hermes-publisher", "node"),
    # Band 2 - docker01
    ("docker01", "node"),
    ("192.168.13.2", "node"),
    ("dockhand2", "node"),
    ("buzz-relay-1", "node"),
    ("9008", "node"),
    ("buzz-postgres-1", "node"),
    ("buzz-redis-1", "node"),
    ("buzz-minio-1", "node"),
    ("buzz-minio-init", "node"),
    ("buzz-dashboard-dashboard-1", "node"),
    ("9011", "node"),
    ("dash-data", "node"),
    ("snapshots.jsonl", "node"),
    # Band 3 - External
    ("HAProxy", "node"),
    ("172.16.5.2", "node"),
    ("buzz.bhue.org", "node"),
    ("Cloudflare tunnel", "node"),
    ("Buzz desktop app", "node"),
    ("operator browser", "node"),
    # Band 4 - GitHub
    ("dan-the-automator712/buzz-hermes-harness", "node"),
    ("SYNCED", "node"),
    ("NOT SYNCED", "node"),
    # Edge labels
    ("drops job JSON", "edge"),
    ("poll", "edge"),
    ("move", "edge"),
    ("runs --jobs", "edge"),
    ("build signed task", "edge"),
    ("buzz-cli REST", "edge"),
    ("verified task", "edge"),
    ("evaluate <=5 turns", "edge"),
    ("run_turn()", "edge"),
    ("signed progress/result", "edge"),
    ("write report", "edge"),
    ("OK_/PROBLEM_", "edge"),
    ("POST /ingest", "edge"),
    ("Bearer DASH_INGEST_TOKEN", "edge"),
    ("every 10s", "edge"),
    ("Bearer DASH_TOKEN", "edge"),
    ("connect", "edge"),
]


def rect(cell):
    """Return (x,y,w,h) for a vertex cell, or None."""
    geo = cell.find("mxGeometry")
    if geo is None:
        return None
    try:
        return (float(geo.get("x", 0)), float(geo.get("y", 0)),
                float(geo.get("width", 0)), float(geo.get("height", 0)))
    except (TypeError, ValueError):
        return None


def overlap_area(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ox = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    oy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ox * oy


def main():
    errors = []
    if not os.path.exists(DRAWIO):
        print("VALIDATION: FAIL — missing %s" % DRAWIO)
        sys.exit(1)

    tree = ET.parse(DRAWIO)
    root = tree.getroot()
    cells = {}
    for cell in root.iter("mxCell"):
        cells[cell.get("id")] = cell

    # --- gather vertices and their parent / rect / label / style ---
    vertices = {}      # id -> (parent, rect, label, style)
    edges = []         # (id, source, target, label)
    for cid, cell in cells.items():
        if cell.get("edge") == "1" or "edge" in (cell.get("style") or ""):
            edges.append((cid, cell.get("source"), cell.get("target"),
                          cell.get("value", "")))
        elif cell.get("vertex") == "1":
            r = rect(cell)
            if r is not None:
                vertices[cid] = (cell.get("parent"), r,
                                 cell.get("value", ""), cell.get("style", ""))

    # --- 1. sibling overlap ---
    by_parent = {}
    for cid, (parent, r, label, style) in vertices.items():
        by_parent.setdefault(parent, []).append((cid, r))
    for parent, items in by_parent.items():
        n = len(items)
        for i in range(n):
            for j in range(i + 1, n):
                a_id, a_r = items[i]
                b_id, b_r = items[j]
                ov = overlap_area(a_r, b_r)
                if ov > 0:
                    errors.append("OVERLAP: siblings %s and %s (parent %s) overlap by %.0f px2"
                                  % (a_id, b_id, parent, ov))

    # --- 1.5 containment: child vertices must stay inside their container ---
    # Child mxGeometry is RELATIVE to its parent band, so compare it directly
    # against the parent band's own width/height.
    for cid, (parent, r, label, style) in vertices.items():
        if parent not in vertices:
            continue  # parent is the root layer, not a container
        pw = vertices[parent][1][2]
        ph = vertices[parent][1][3]
        rx, ry, rw, rh = r
        bad = []
        if rx < 0:
            bad.append("rel_x=%.1f < 0" % rx)
        if ry < 0:
            bad.append("rel_y=%.1f < 0" % ry)
        if rx + rw > pw:
            bad.append("rel_x+width=%.1f > parent_width=%.1f" % (rx + rw, pw))
        if ry + rh > ph:
            bad.append("rel_y+height=%.1f > parent_height=%.1f" % (ry + rh, ph))
        if bad:
            errors.append("CONTAINMENT: child %s (parent %s) %s"
                          % (cid, parent, "; ".join(bad)))

    # --- 2. edge source/target exist ---
    vertex_ids = set(vertices.keys())
    for eid, src, tgt, label in edges:
        for role, ref in (("source", src), ("target", tgt)):
            if ref is None or ref not in vertex_ids:
                errors.append("DANGLING %s: edge %s (%r) -> %s (id=%r)"
                              % (role, eid, label, ref, ref))

    # --- 3. edges must not touch NOTE/LEGEND boxes ---
    for eid, src, tgt, label in edges:
        for role, ref in (("source", src), ("target", tgt)):
            if ref in NOTES:
                errors.append("EDGE TO NOTE: edge %s connects to note box %s"
                              % (eid, ref))

    # --- 4. required components present ---
    node_text = " | ".join(v[2] for v in vertices.values()).lower()
    edge_text = " | ".join(l for (_, _, _, l) in edges).lower()
    all_text = node_text + " ||| " + edge_text
    for needle, kind in REQUIRED:
        low = needle.lower()
        if kind == "node" and low not in node_text:
            errors.append("MISSING NODE content: %r" % needle)
        elif kind == "edge" and low not in edge_text:
            errors.append("MISSING EDGE label: %r" % needle)

    # --- 5. hermes_adapter -> relay must NOT exist (spec says it is WRONG) ---
    for eid, src, tgt, label in edges:
        if src == "hermes_adapter" and tgt == "relay":
            errors.append("FORBIDDEN EDGE: hermes_adapter -> relay must not exist")

    if errors:
        print("VALIDATION: FAIL")
        for e in errors:
            print("  - " + e)
        sys.exit(1)
    print("VALIDATION: PASS")
    print("  bands=%d nodes=%d edges=%d" % (len(by_parent), len(vertices), len(edges)))
    sys.exit(0)


if __name__ == "__main__":
    main()
