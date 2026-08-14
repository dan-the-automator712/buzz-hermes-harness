#!/usr/bin/env python3
"""Build buzz-hermes-harness.drawio programmatically.

Declares the architecture as DATA (swimlane bands -> child nodes + edges
referencing node ids) and COMPUTES all coordinates on a strict grid so that
overlaps are impossible. Writes valid draw.io XML to
buzz-hermes-harness.drawio.
"""
import os
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Grid constants (pixels)
# ---------------------------------------------------------------------------
CANVAS_W     = 2000
MARGIN       = 40          # space left/right of the bands on the canvas
BAND_GAP     = 50          # vertical gap between bands
BAND_PAD_X   = 20          # inner padding inside a band
BAND_TITLE_H = 46          # band title strip height
NODE_W       = 250
NODE_H       = 66
GAP          = 30          # horizontal gap between sibling boxes (>=30)
ROW_GAP      = 30          # vertical gap between wrapped rows
BAND_BOTTOM  = 20          # inner bottom padding of a band

OUT = os.path.join(os.path.dirname(__file__), os.pardir, "buzz-hermes-harness.drawio")

# ---------------------------------------------------------------------------
# NODE_STYLE defaults & band fill colors
# ---------------------------------------------------------------------------
def node_style(fill, stroke, note=False, dashed=False, bold=False):
    st = ("rounded=1;whiteSpace=wrap;html=1;fillColor=%s;strokeColor=%s;"
          "fontSize=10;fontColor=#1F2328;" % (fill, stroke))
    if bold:
        st += "fontStyle=1;"
    if dashed:
        st += "dashed=1;dashPattern=8 4;"
    if note:
        st += "dashed=1;dashPattern=8 4;fillColor=#FFF8E1;strokeColor=#E0A800;"
    return st

BAND_STYLE = "rounded=1;dashed=1;dashPattern=8 4;fillColor=%s;fontStyle=1;fontSize=14;whiteSpace=wrap;html=1;verticalAlign=top;align=left;spacingTop=-10;"

# ---------------------------------------------------------------------------
# ARCHITECTURE AS DATA
# ---------------------------------------------------------------------------
BANDS = [
    {
        "id": "band1",
        "title": "DIZCORYZ — Harness Host (WSL)  ·  E:\\HermesOutput\\buzz-hermes-harness",
        "fill": "#F0F4F8",
        "children": [
            {"id": "opchat",  "label": "Claude / operator chat", "fill": "#E3F2FD", "stroke": "#1976D2"},
            {"id": "inbox",   "label": "bridge/inbox", "fill": "#E8F5E9", "stroke": "#2E7D32"},
            {"id": "watcher", "label": "watcher.py\n(systemd hermes-bridge)", "fill": "#E8F5E9", "stroke": "#2E7D32"},
            {"id": "processing", "label": "bridge/processing", "fill": "#E8F5E9", "stroke": "#2E7D32"},
            {"id": "orchestrator", "label": "orchestrator.py\n(queen)", "fill": "#E3F2FD", "stroke": "#1976D2"},
            {"id": "buzz_bus", "label": "buzz_bus.py\n(Schnorr sign/verify +\ntimestamp + author allowlist)", "fill": "#E3F2FD", "stroke": "#1976D2"},
            {"id": "worker", "label": "worker.py\n(governed worker)", "fill": "#E3F2FD", "stroke": "#1976D2"},
            {"id": "governor", "label": "governor.py\n(5-turn cap + ground-truth\nfile_exists / file_contains criteria)", "fill": "#E3F2FD", "stroke": "#1976D2"},
            {"id": "hermes_adapter", "label": "hermes_adapter.py\n(hermes -m deepseek/deepseek-v4-flash-0731 -z --usage-file)", "fill": "#E3F2FD", "stroke": "#1976D2"},
            {"id": "outbox", "label": "bridge/outbox\n(OK_ / PROBLEM_ reports)", "fill": "#E8F5E9", "stroke": "#2E7D32"},
            {"id": "done",   "label": "bridge/done", "fill": "#E8F5E9", "stroke": "#2E7D32"},
            {"id": "keys",   "label": "keys.json\n(queen + 4 worker nsec, GITIGNORED)", "fill": "#FFF3E0", "stroke": "#EF6C00", "dashed": True},
            {"id": "watcherenv", "label": "bridge/watcher.env\n(PATH + DASH tokens, GITIGNORED)", "fill": "#FFF3E0", "stroke": "#EF6C00", "dashed": True},
            {"id": "publisher", "label": "publisher.py\n(systemd hermes-publisher)", "fill": "#E8F5E9", "stroke": "#2E7D32"},
        ],
    },
    {
        "id": "band2",
        "title": "docker01  192.168.13.2  —  Dockhand-managed (dockhand2 container)",
        "fill": "#F3E8F6",
        "children": [
            {"id": "relay",  "label": "buzz-relay-1\n(host 9008 -> 3000)", "fill": "#F3E5F5", "stroke": "#8E24AA"},
            {"id": "postgres", "label": "buzz-postgres-1", "fill": "#F3E5F5", "stroke": "#8E24AA"},
            {"id": "redis",  "label": "buzz-redis-1", "fill": "#F3E5F5", "stroke": "#8E24AA"},
            {"id": "minio",  "label": "buzz-minio-1", "fill": "#F3E5F5", "stroke": "#8E24AA"},
            {"id": "minio_init", "label": "buzz-minio-init (one-shot)", "fill": "#F3E5F5", "stroke": "#8E24AA"},
            {"id": "dashboard", "label": "buzz-dashboard-dashboard-1\n(host 9011 -> 8787)\nvolume dash-data -> /data\n(snapshots.jsonl)", "fill": "#F3E5F5", "stroke": "#8E24AA"},
            {"id": "dockhand", "label": "dockhand2 container\n(Dockhand-managed)", "fill": "#EDE7F6", "stroke": "#5E35B1"},
        ],
    },
    {
        "id": "band3",
        "title": "External",
        "fill": "#E0F2F1",
        "children": [
            {"id": "haproxy", "label": "HAProxy 172.16.5.2\nbuzz.bhue.org -> relay:9008\nbuzz-dashboard.bhue.org -> dashboard:9011", "fill": "#E0F7FA", "stroke": "#00838F"},
            {"id": "cf_tunnel", "label": "Cloudflare tunnel\n(token-managed, not routing yet)", "fill": "#ECEFF1", "stroke": "#546E7A", "dashed": True},
            {"id": "desktop", "label": "Buzz desktop app", "fill": "#E0F7FA", "stroke": "#00838F"},
            {"id": "opbrowser", "label": "operator browser", "fill": "#E0F7FA", "stroke": "#00838F"},
        ],
    },
    {
        "id": "band4",
        "title": "GitHub",
        "fill": "#E8EEF9",
        "children": [
            {"id": "repo", "label": "private repo\ndan-the-automator712/buzz-hermes-harness", "fill": "#E8EAF6", "stroke": "#3949AB", "bold": True},
            {"id": "synced", "label": "SYNCED:\nharness/*.py, bridge/*.py,\ndeploy/**, jobs*.json", "fill": "#E8F5E9", "stroke": "#2E7D32"},
            {"id": "notsynced", "label": "NOT SYNCED:\nkeys.json, watcher.env,\ndeploy/compose/.env, bridge runtime dirs", "fill": "#FFEBEE", "stroke": "#C62828"},
        ],
    },
]

# Legend box (a NOTE - the validator must ensure no edge touches it)
LEGEND = {"id": "legend", "note": True,
          "label": "LEGEND  —  arrows show data flow; dashed outline = gitignored / not-synced / not-active"}

# Edges: (source_id, target_id, label)
EDGES = [
    ("opchat",      "inbox",       "drops job JSON"),
    ("inbox",       "watcher",     "poll"),
    ("watcher",     "processing",  "move"),
    ("watcher",     "orchestrator","runs --jobs"),
    ("orchestrator","buzz_bus",    "build signed task"),
    ("buzz_bus",    "relay",       "buzz-cli REST: signed+timestamped events"),
    ("relay",       "worker",      "verified task"),
    ("worker",      "governor",    "evaluate <=5 turns"),
    ("worker",      "hermes_adapter","run_turn()"),
    ("worker",      "relay",       "signed progress/result"),
    ("watcher",     "outbox",      "write report"),
    ("outbox",      "opchat",      "OK_/PROBLEM_ + tokens"),
    ("processing",  "done",        "archive after run"),
    ("outbox",      "publisher",   "reads bridge state (build_model, every 10s)"),
    ("publisher",   "dashboard",   "POST /ingest (Bearer DASH_INGEST_TOKEN) every 10s"),
    ("haproxy",     "dashboard",   "proxy dashboard:9011"),
    ("opbrowser",   "haproxy",     "https buzz-dashboard.bhue.org (Bearer DASH_TOKEN)"),
    ("haproxy",     "relay",       "proxy relay:9008"),
    ("desktop",     "haproxy",     "connect"),
]

EDGE_STYLE = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"
              "exitX=1;exitY=0.5;entryX=0;entryY=0.5;"
              "strokeColor=#616161;endArrow=classic;")


# ---------------------------------------------------------------------------
# Layout: compute every coordinate on the grid
# ---------------------------------------------------------------------------
def layout():
    """Return dict: node_id -> (rel_x, rel_y, w, h) plus band rects (absolute).

    Child node coordinates are RELATIVE to their parent band origin (draw.io
    semantics): we subtract the band's absolute origin so children stay inside
    their container. Each band's height is grown so every child fits with
    >=20px bottom padding: band_h >= max(child_rel_y + child_h) + 20.
    """
    canvas_w = CANVAS_W
    x0 = MARGIN
    inner_w = canvas_w - 2 * MARGIN          # band width
    avail = inner_w - 2 * BAND_PAD_X         # node area width
    per_row = (avail + GAP) // (NODE_W + GAP)
    per_row = max(1, per_row)

    node_rects = {}      # child id -> (rel_x, rel_y, w, h) relative to band
    band_rects = {}      # band id -> (x, y, w, h) absolute
    y = MARGIN
    for band in BANDS:
        n = len(band["children"])
        rows = (n + per_row - 1) // per_row
        # compute each child's position relative to its band origin
        rel_children = []
        for idx, ch in enumerate(band["children"]):
            col = idx % per_row
            row = idx // per_row
            rx = BAND_PAD_X + col * (NODE_W + GAP)
            ry = BAND_TITLE_H + row * (NODE_H + ROW_GAP)
            rel_children.append((ch, rx, ry, NODE_W, NODE_H))
        # grow band height so every child fits with >=20px bottom padding
        max_child_bottom = max((ry + NODE_H) for (_, _, ry, _, _) in rel_children)
        band_h = max_child_bottom + 20
        band_rects[band["id"]] = (x0, y, inner_w, band_h)
        for (ch, rx, ry, cw, chh) in rel_children:
            node_rects[ch["id"]] = (rx, ry, cw, chh)
        y += band_h + BAND_GAP
    # legend box below the bands (parented to root "1": keep absolute coords)
    legend_y = y
    band_rects["legend_band"] = (x0, legend_y, inner_w, NODE_H + 30)
    node_rects[LEGEND["id"]] = (x0 + BAND_PAD_X, legend_y + 12, inner_w - 2 * BAND_PAD_X, NODE_H)
    return node_rects, band_rects, per_row


def build_xml():
    node_rects, band_rects, per_row = layout()
    cells = []
    cells.append('<mxCell id="0" />')
    cells.append('<mxCell id="1" parent="0" />')

    # bands (containers)
    for band in BANDS:
        x, y, w, h = band_rects[band["id"]]
        style = BAND_STYLE % band["fill"]
        cells.append(
            '<mxCell id="%s" value="%s" style="%s" vertex="1" parent="1">'
            '<mxGeometry x="%d" y="%d" width="%d" height="%d" as="geometry"/>'
            '</mxCell>' % (band["id"], escape(band["title"]), style, x, y, w, h))

    # child nodes
    for band in BANDS:
        for ch in band["children"]:
            cx, cy, cw, chh = node_rects[ch["id"]]
            style = node_style(ch.get("fill", "#ECEFF1"), ch.get("stroke", "#455A64"),
                               note=ch.get("note", False), dashed=ch.get("dashed", False),
                               bold=ch.get("bold", False))
            cells.append(
                '<mxCell id="%s" value="%s" style="%s" vertex="1" parent="%s">'
                '<mxGeometry x="%d" y="%d" width="%d" height="%d" as="geometry"/>'
                '</mxCell>' % (ch["id"], escape(ch["label"]), style, band["id"], cx, cy, cw, chh))

    # legend (note box)
    lx, ly, lw, lh = node_rects[LEGEND["id"]]
    lstyle = node_style("#FFF8E1", "#E0A800", note=True, bold=True)
    cells.append(
        '<mxCell id="%s" value="%s" style="%s" vertex="1" parent="1">'
        '<mxGeometry x="%d" y="%d" width="%d" height="%d" as="geometry"/>'
        '</mxCell>' % (LEGEND["id"], escape(LEGEND["label"]), lstyle, lx, ly, lw, lh))

    # edges
    for src, tgt, label in EDGES:
        cells.append(
            '<mxCell id="e_%s_%s" value="%s" style="%s" edge="1" parent="1" '
            'source="%s" target="%s">'
            '<mxGeometry relative="1" as="geometry"/>'
            '</mxCell>' % (src, tgt, escape(label), EDGE_STYLE, src, tgt))

    body = "\n".join(cells)
    xml = (
        '<mxfile host="app.diagrams.net" modified="2026-08-14T00:00:00.000Z" '
        'agent="buzz-hermes-harness" version="24.0.0" type="device">\n'
        '  <diagram id="buzz-harness" name="buzz-hermes-harness">\n'
        '    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" '
        'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
        'pageScale="1" pageWidth="2000" pageHeight="2400" math="0" '
        'shadow="0">\n'
        '      <root>\n'
        '        <mxCell id="0" />\n'
        '        <mxCell id="1" parent="0" />\n'
        + body.replace('<mxCell id="0" />\n<mxCell id="1" parent="0" />\n', '') +
        '      </root>\n'
        '    </mxGraphModel>\n'
        '  </diagram>\n'
        '</mxfile>\n'
    )
    # we already emitted ids 0/1 above in cells; the template re-injects them.
    # Simpler: rebuild cleanly.
    xml = (
        '<mxfile host="app.diagrams.net" modified="2026-08-14T00:00:00.000Z" '
        'agent="buzz-hermes-harness" version="24.0.0" type="device">\n'
        '  <diagram id="buzz-harness" name="buzz-hermes-harness">\n'
        '    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" '
        'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
        'pageScale="1" pageWidth="2000" pageHeight="2400" math="0" shadow="0">\n'
        '      <root>\n'
        + "\n".join(cells) +
        '\n      </root>\n'
        '    </mxGraphModel>\n'
        '  </diagram>\n'
        '</mxfile>\n'
    )
    return xml


def main():
    xml = build_xml()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(xml)
    print("Wrote %s (%d bytes)" % (OUT, len(xml)))
    node_rects, band_rects, per_row = layout()
    print("Band list (height, child count):")
    for band in BANDS:
        _, _, _, h = band_rects[band["id"]]
        print("  %-6s h=%-4d children=%d  %s"
              % (band["id"], h, len(band["children"]), band["title"]))


if __name__ == "__main__":
    main()
