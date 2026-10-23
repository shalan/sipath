"""The README's flow diagram, light and dark: python3 docs/diagrams/flow_diagram.py

Writes docs/diagrams/flow-light.svg and flow-dark.svg (README.md shows the one matching the reader's theme).
Edit the steps, loops and boxes here, not the SVG files.
"""
from __future__ import annotations

import os
from html import escape

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 1280, 612

PALETTES = {
    "light": {"bg": "#ffffff", "panel": "#f6f8fa", "box": "#ffffff", "line": "#d0d7de", "text": "#1f2328",
              "muted": "#59636e", "flow": "#59636e", "adapt": "#0969da", "retry": "#9a6700", "eco": "#bc4c00",
              "keep": "#8250df", "ok": "#1a7f37"},
    "dark": {"bg": "#0d1117", "panel": "#151b23", "box": "#0d1117", "line": "#3d444d", "text": "#f0f6fc",
             "muted": "#9198a1", "flow": "#9198a1", "adapt": "#4493f8", "retry": "#d29922", "eco": "#db6d28",
             "keep": "#ab7df8", "ok": "#3fb950"},
}
SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# the reference chain, two rows; optional steps (dashed) run when the design or the PDK needs them
ROW1 = [("synth", ["Yosys · ABC sweep ↻", "lint · equivalence"], False),
        ("floorplan", ["die · rows · pins", "or a pad ring"], False),
        ("macros", ["macro placers", "if there are macros"], True),
        ("physical", ["power grid", "taps · endcaps"], False),
        ("place", ["global · detailed", "heatmaps"], False),
        ("cts", ["clock tree", "repair"], False)]
ROW2 = [("route_analog", ["analog nets first", "wide rule · shields"], False),
        ("route_digital", ["global · detailed", "repair ↻ antennas ↻"], False),
        ("route_repair", ["timing repair ↻", "on extracted RC"], False),
        ("fill", ["metal fill", "PDK fill rules"], True),
        ("extract", ["RC per corner", "IR drop"], False),
        ("signoff", ["DRC · LVS · density", "nine-corner STA"], False)]
X0, PITCH, BW, BH = 268, 126, 112, 64
Y1, Y2 = 104, 244


def svg(theme: str) -> str:
    c = PALETTES[theme]
    out = []

    def add(s):
        out.append(s)

    def text(x, y, s, size=11, color="text", anchor="middle", weight="normal", font=SANS):
        add(f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" font-weight="{weight}" '
            f'fill="{c[color]}" text-anchor="{anchor}">{escape(s)}</text>')

    def box(x, y, w, h, fill="box", stroke="line", dash=False, r=8, width=1.2):
        add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{c[fill]}" stroke="{c[stroke]}" '
            f'stroke-width="{width}"' + (' stroke-dasharray="5 4"' if dash else "") + "/>")

    def path(d, color="flow", dash=False, arrow=True, width=1.4):
        add(f'<path d="{d}" fill="none" stroke="{c[color]}" stroke-width="{width}"'
            + (' stroke-dasharray="5 4"' if dash else "") + (f' marker-end="url(#a-{color})"' if arrow else "") + "/>")

    add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
        'aria-labelledby="t d">')
    add('<title id="t">How SiPath runs a design</title>')
    add('<desc id="d">A design file, the Python API or an AI agent over MCP drives an attempt graph. The reference '
        'chain runs synth, floorplan, macros, physical, place, cts, route_analog, route_digital, route_repair, fill, '
        'extract and signoff. Every step runs its variants as attempts in parallel, a score selects one and the next '
        'step continues from it; others are kept as branches or rejected. Placement can run adaptive rounds, a failed '
        'repair patch is retried with a full re-route, and ECOs patch the signed-off result. Outputs are the '
        'verdict, the layout and reports, hardened views and, for harness designs, precheck and chip '
        'integration.</desc>')
    add("<defs>")
    for name in ("flow", "adapt", "retry", "eco", "keep", "ok", "muted"):
        add(f'<marker id="a-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c[name]}"/></marker>')
    add("</defs>")
    add(f'<rect width="{W}" height="{H}" fill="{c["bg"]}"/>')

    # drivers and inputs (left)
    text(20, 48, "DRIVEN BY", 10, "muted", "start", "600")
    for k, (title, sub) in enumerate((("design file", "sipath run design.yaml"),
                                      ("Python API", "Flow.step · eco · fork"),
                                      ("AI agent over MCP", "run_step · adapt · run_eco"))):
        y = 60 + 72 * k
        box(20, y, 200, 58)
        text(120, y + 24, title, 13, weight="600")
        text(120, y + 42, sub, 11, "muted", font=MONO)
        path(f"M220 {y + 29} L242 {y + 29}")
    text(20, 296, "WITH", 10, "muted", "start", "600")
    box(20, 308, 200, 74)
    text(120, 332, "PDK described as data", 13, weight="600")
    text(120, 350, "tool images locked by digest", 11, "muted")
    text(120, 366, "open_pdks release by commit", 11, "muted")
    path("M220 345 L242 345")

    # the attempt graph
    box(244, 24, 800, 568, fill="panel", r=12)
    text(264, 50, "attempt graph", 14, weight="700", anchor="start")
    text(372, 50, "graph.json keeps every attempt", 11, "muted", "start")
    text(1024, 582, "↻ iterates inside the step  ·  dashed: when the design needs it", 10.5, "muted", "end")
    for row, y in ((ROW1, Y1), (ROW2, Y2)):
        for k, (name, sub, optional) in enumerate(row):
            x = X0 + PITCH * k
            box(x, y, BW, BH, dash=optional)
            text(x + BW / 2, y + 22, name, 13, weight="700", font=MONO)
            for j, s in enumerate(sub):
                text(x + BW / 2, y + 39 + 13 * j, s, 10.5, "muted")
            if k:
                path(f"M{x - PITCH + BW} {y + BH / 2} L{x - 1} {y + BH / 2}")
    cx = X0 + PITCH * 5 + BW / 2  # cts down, back to route_analog
    path(f"M{cx} {Y1 + BH} L{cx} 206 L{X0 + BW / 2} 206 L{X0 + BW / 2} {Y2 - 1}")

    # loops on the chain
    px = X0 + PITCH * 4  # place: adaptive rounds
    path(f"M{px + 92} {Y1} C{px + 92} 74 {px + 20} 74 {px + 20} {Y1 - 1}", "adapt")
    text(px + 56, 68, "adaptive rounds: hotspots → next variants", 11, "adapt", weight="600")
    rx = X0 + PITCH * 2  # route_repair: a failed patch is retried
    path(f"M{rx + 20} {Y2 + BH} C{rx + 20} 340 {rx + 92} 340 {rx + 92} {Y2 + BH + 1}", "retry")
    text(rx + 56, 356, "a failed patch: one retry,", 11, "retry", weight="600")
    text(rx + 56, 370, "with a full re-route", 11, "retry", weight="600")
    sx = X0 + PITCH * 5  # signoff: ECOs from the result
    path(f"M{sx + 20} {Y2 + BH} C{sx + 20} 340 {sx + 92} 340 {sx + 92} {Y2 + BH + 1}", "eco")
    text(sx + 56, 356, "run_eco: resize · buffer,", 11, "eco", weight="600")
    text(sx + 56, 370, "re-routes changed nets", 11, "eco", weight="600")

    # inside every step
    box(268, 384, 742, 172, r=10)
    text(284, 406, "inside every step", 12, weight="700", anchor="start")
    text(300, 428, "parent", 10.5, "muted")
    text(460, 428, "variants run as attempts, in parallel", 10.5, "muted")
    text(670, 428, "a score ranks them", 10.5, "muted")
    text(895, 428, "outcome", 10.5, "muted")
    add(f'<circle cx="300" cy="487" r="8" fill="{c["ok"]}"/>')
    text(300, 512, "selected", 10.5, "muted")
    text(300, 525, "attempt", 10.5, "muted")
    for k, label in enumerate(("place.0 · density 0.6", "place.1 · density 0.7", "place.2 · density 0.8")):
        y = 440 + 34 * k
        box(380, y, 160, 26, r=13)
        text(460, y + 17, label, 11, font=MONO)
        path(f"M310 487 C340 487 350 {y + 13} 379 {y + 13}")
        path(f"M540 {y + 13} C570 {y + 13} 572 487 589 487")
    box(590, 474, 160, 26, r=13)
    text(670, 491, "routability_then_wns", 10.5, font=MONO)
    for k, (label, color) in enumerate((("✓ selected: the next step continues", "ok"),
                                        ("kept: a branch, decided later", "keep"),
                                        ("rejected, still in the graph", "muted"))):
        y = 440 + 34 * k
        add(f'<rect x="784" y="{y}" width="212" height="26" rx="13" fill="{c["box"]}" stroke="{c[color]}" '
            'stroke-width="1.4"/>')
        text(890, y + 17, label, 11, color, weight="600" if color != "muted" else "normal")
        path(f"M750 487 C768 487 766 {y + 13} 783 {y + 13}", color)
    text(639, 546, "each attempt's fingerprint (inputs, parameters, parent) lets --resume reuse it; "
                   "fork starts a new run from any attempt", 10.5, "muted")

    # what a run gives (right)
    text(1064, 48, "GIVES", 10, "muted", "start", "600")
    outs = (("verdict", ["execution: completed or failed", "verification: passed · failed ·", "inconclusive · omitted"]),
            ("layout and reports", ["DEF · SPEF · GDS", "report.html · metrics.json"]),
            ("sipath harden", ["LEF · Liberty · GDS · CDL", "for a parent design"]),
            ("harness designs", ["precheck → integrate", "into the frame · seal ring"]))
    y = 60
    for title, sub in outs:
        h = 30 + 14 * len(sub)
        box(1064, y, 196, h)
        text(1162, y + 22, title, 13, weight="600")
        for j, s in enumerate(sub):
            text(1162, y + 39 + 14 * j, s, 10.5, "muted")
        path(f"M1054 {y + h / 2} L1063 {y + h / 2}")
        last, y = y + h / 2, y + h + 14
    path(f"M{X0 + PITCH * 5 + BW} {Y2 + BH / 2} L1054 {Y2 + BH / 2}", arrow=False)
    path(f"M1054 {60 + 37} L1054 {last}", arrow=False)
    add("</svg>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    for theme in PALETTES:
        p = os.path.join(HERE, f"flow-{theme}.svg")
        with open(p, "w") as f:
            f.write(svg(theme))
        print(p)
