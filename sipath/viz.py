"""Pictures of a checkpoint.

    python3 -m sipath render <workdir> [step|attempt|all] [-o out.png]   OpenROAD's own layout render (save_image)
    python3 -m sipath draw <workdir> [step|attempt|all] [-o out.svg] [--ring]   schematic SVG (pads, corners,
                                                                               fillers, macros)

A run is named by its work directory and a step (its selected attempt), an
attempt id (``place.1``, selected or not) or ``all`` (every selected step with
a database); without one, the last selected step. ``checkpoints`` resolves
them from ``graph.json``. A checkpoint ``.odb`` can be given instead of a
work directory.

Boxes are the instances' bounding boxes as OpenROAD reports them (after
orientation), so the picture is what the database holds: IO pads, corners,
IO fillers, hard macros, the core area.
"""
from __future__ import annotations

from .tools.openroad.tcl import q, tcl_parse

DUMP = r"""
set block [ord::get_db_block]
set d [$block getDieArea]; set c [$block getCoreArea]
set out [list [list die [$d xMin] [$d yMin] [$d xMax] [$d yMax]] [list core [$c xMin] [$c yMin] [$c xMax] [$c yMax]] [list dbu [$block getDbUnitsPerMicron]]]
foreach i [$block getInsts] {
  set m [$i getMaster]; set t [$m getType]
  if {[string match PAD* $t] || [string match ENDCAP* $t] || [$m isBlock]} {
    set b [$i getBBox]
    lappend out [list [$i getName] [$m getName] $t [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
  }
}
set out
"""

COLORS = {"corner": "#7f8c8d", "fill": "#d5dbdb", "gpio": "#2e86c1", "power": "#c0392b", "macro": "#8e44ad"}


def _kind(master, mtype):
    if mtype.startswith("ENDCAP"):
        return "corner"
    if mtype == "PAD_SPACER":
        return "fill"
    if mtype.startswith("PAD"):
        return "power" if mtype == "PAD_POWER" else "gpio"
    return "macro"


def layout_svg(session, scale: float = 0.6, ring_only: bool = False) -> str:
    items = tcl_parse(session.value(DUMP))
    dbu = int(items[2][1])
    items = items[:2] + items[3:]
    if ring_only:  # IO ring only: drop hard macros
        items = items[:2] + [it for it in items[2:] if _kind(it[1], it[2]) != "macro"]
    die = list(map(int, items[0][1:]))
    core = list(map(int, items[1][1:]))
    H = (die[3] - die[1]) / dbu

    def xy(x, y):
        return x / dbu * scale, (H - y / dbu) * scale

    W = (die[2] - die[0]) / dbu
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * scale + 20:.0f}" height="{H * scale + 40:.0f}" '
           'font-family="sans-serif">', '<rect width="100%" height="100%" fill="white"/>',
           '<g transform="translate(10,10)">',
           f'<rect x="0" y="0" width="{W * scale}" height="{H * scale}" fill="#fbfcfc" stroke="#333"/>']
    x0, y1 = xy(core[0], core[3])
    x1, y0 = xy(core[2], core[1])
    svg.append(f'<rect x="{x0}" y="{y1}" width="{x1 - x0}" height="{y0 - y1}" fill="none" stroke="#999" '
               'stroke-dasharray="4 3"/>')
    svg.append(f'<text x="{x0 + 4}" y="{y1 + 12}" font-size="10" fill="#666">core</text>')
    counts = {}
    for name, master, mtype, *bb in items[2:]:
        k = _kind(master, mtype)
        counts[k] = counts.get(k, 0) + 1
        bx0, by0, bx1, by1 = map(int, bb)
        a, b2 = xy(bx0, by1)
        c, d = xy(bx1, by0)
        svg.append(f'<rect x="{a:.2f}" y="{b2:.2f}" width="{c - a:.2f}" height="{d - b2:.2f}" fill="{COLORS[k]}" '
                   'stroke="#222" stroke-width="0.3"/>')
        cx, cy = (a + c) / 2, (b2 + d) / 2
        if k in ("gpio", "power"):
            rot = -90 if (c - a) < (d - b2) else 0
            svg.append(f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="8" fill="white" text-anchor="middle" '
                       f'dominant-baseline="middle" transform="rotate({rot} {cx:.1f} {cy:.1f})">'
                       f'{name.replace("pad_", "")}</text>')
        elif k == "macro":
            svg.append(f'<text x="{cx:.1f}" y="{cy:.1f}" font-size="11" fill="white" text-anchor="middle">'
                       f'{name} ({master})</text>')
    lx = 0
    for k, lab in (("gpio", "GPIO pad"), ("power", "power pad"), ("corner", "corner"), ("fill", "IO filler"),
                   ("macro", "hard macro")):
        svg.append(f'<rect x="{lx}" y="{H * scale + 8}" width="10" height="10" fill="{COLORS[k]}"/>'
                   f'<text x="{lx + 14}" y="{H * scale + 17}" font-size="10">{lab} ({counts.get(k, 0)})</text>')
        lx += 125
    svg += ["</g>", "</svg>"]
    return "\n".join(svg) + "\n"


RING_SUMMARY = r"""
set nets {}; set cells 0
foreach i [[ord::get_db_block] getInsts] {
  set t [[$i getMaster] getType]
  if {![string match PAD* $t] && ![string match ENDCAP* $t]} continue
  incr cells
  foreach it [$i getITerms] {
    set p [[$it getMTerm] getName]
    if {$p ni {VCCD VSSD VDDIO VSSIO VDDIO_Q VSSIO_Q VDDA VSSA VCCHIB VSWITCH AMUXBUS_A AMUXBUS_B}} continue
    set n [$it getNet]; dict set nets $p [expr {$n eq "NULL" ? "-" : [$n getName]}] 1
  }
}
set split 0; dict for {p ns} $nets { if {[dict size $ns] != 1} { incr split } }
list $cells [dict size $nets] $split
"""


def checkpoints(workdir: str, which: str | None = None) -> list[tuple[str, str]]:
    """``(attempt id, checkpoint .odb)`` for ``which`` in a run's ``graph.json``.

    ``which``: a step (its selected attempt), an attempt id, ``all`` (every
    selected attempt with a database, in flow order) or None (the last of those).
    Raises ValueError naming what the run has when nothing matches."""
    import json
    import os
    path = os.path.join(workdir, "graph.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{workdir}: no graph.json (not a run directory)")
    with open(path) as f:
        attempts = json.load(f)["attempts"]
    odb = [a for a in attempts if str(a.get("checkpoint") or "").endswith(".odb")]
    sel = [a for a in odb if a.get("status") == "selected"]
    if which in (None, "all"):
        hits = sel if which == "all" else sel[-1:]
    elif any(a["id"] == which for a in attempts):
        hits = [a for a in odb if a["id"] == which]
        if not hits:
            st = next(a.get("status") for a in attempts if a["id"] == which)
            raise ValueError(f"attempt {which} {st}: no checkpoint" if st in ("failed", "cancelled") else
                             f"attempt {which} has no layout database (only steps after synthesis do)")
    else:
        hits = [a for a in sel if a["step"] == which]
        if not hits:
            steps = sorted({a["step"] for a in sel}, key=[a["step"] for a in sel].index)
            raise ValueError(f"no selected {which!r} checkpoint in {workdir}; steps with one: {', '.join(steps)}"
                             " (or an attempt id such as place.1, or all)")
    if not hits:
        raise ValueError(f"{workdir}: no selected checkpoint with a layout database yet")
    return [(a["id"], a["checkpoint"]) for a in hits]


def picture_args(argv: list[str], ext: str) -> list[tuple[str, str]]:
    """``(checkpoint, out)`` pairs for ``render``/``draw`` command-line arguments.

    ``<checkpoint.odb> <out>`` as before, or ``<workdir> [which] [-o out]``;
    pictures go to ``<workdir>/_pictures/<attempt>.<ext>`` (the MCP tools' place)
    unless ``-o`` names one file."""
    import os
    args = [a for a in argv if not a.startswith("--")]
    out = None
    if "-o" in args:
        i = args.index("-o")
        out, args = args[i + 1], args[:i] + args[i + 2:]
    if not args:
        raise ValueError(f"usage: <workdir> [step|attempt|all] [-o out.{ext}]")
    if args[0].endswith(".odb"):
        return [(args[0], out or (args[1] if len(args) > 1 else os.path.splitext(args[0])[0] + "." + ext))]
    found = checkpoints(args[0], args[1] if len(args) > 1 else None)
    if out and len(found) > 1:
        raise ValueError("-o names one file; 'all' writes one picture per step into <workdir>/_pictures")
    return [(odb, out or os.path.join(args[0], "_pictures", f"{aid}.{ext}")) for aid, odb in found]


def render(odb: str, out: str, runner=None, area: tuple | None = None, resolution: float | None = None,
           timeout: float = 600) -> dict:
    """OpenROAD's own layout picture of a checkpoint (``save_image``, headless GUI).

    ``area`` is in microns (default: the die); ``resolution`` is microns per pixel
    (default: about 1600 pixels across)."""
    import os

    from .tools.openroad.session import find_openroad
    runner = runner or find_openroad()
    if runner is None:
        raise FileNotFoundError("OpenROAD not available")
    odb, out = os.path.abspath(odb), os.path.abspath(out)
    work = os.path.join(os.path.dirname(out), "_render")
    os.makedirs(work, exist_ok=True)
    runner.mount(os.path.dirname(odb), os.path.dirname(out))
    script = os.path.join(work, "render.tcl")
    area_tcl = ("set d [[ord::get_db_block] getDieArea]; set u [[ord::get_db_block] getDbUnitsPerMicron]\n"
                "set a [list [expr {double([$d xMin])/$u}] [expr {double([$d yMin])/$u}] "
                "[expr {double([$d xMax])/$u}] [expr {double([$d yMax])/$u}]]\n") if area is None else \
        f"set a {{{area[0]} {area[1]} {area[2]} {area[3]}}}\n"
    res = (f"set r {resolution}\n" if resolution else
           "set r [expr {max([lindex $a 2]-[lindex $a 0], [lindex $a 3]-[lindex $a 1]) / 1600.0}]\n")
    with open(script, "w") as f:
        # save_image without -area fails in this build, so the area is always given
        f.write(f"read_db {{{odb}}}\n{area_tcl}{res}"
                f"if {{[catch {{save_image -resolution $r -area $a {{{out}}}}} e]}} {{ puts stderr $e; exit 1 }}\nexit\n")
    from .tools import process
    name = process.container_name("sipath-render") if getattr(runner, "kind", "") == "docker" else None
    try:
        cmd, env = runner.gui_command(work, script, name=name)
    except TypeError:  # a runner without container names
        cmd, env, name = (*runner.gui_command(work, script), None)
    p = process.run(cmd, os.path.join(work, "render.log"), env=env, timeout=timeout, container=name)
    if p.timed_out or p.rc != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0 or "GUI-0078" in p.output:
        tail = "\n".join(p.output.strip().splitlines()[-10:])
        raise RuntimeError(f"save_image did not write {out}" + (f" (stopped after {timeout} s)" if p.timed_out
                                                                 else "") + f"\n{tail}")
    with open(out, "rb") as f:
        head = f.read(24)
    import struct
    w, h = struct.unpack(">II", head[16:24])
    return {"png": out, "bytes": os.path.getsize(out), "pixels": [w, h]}


def draw(odb: str, out: str, runner=None, ring_only: bool = False) -> dict:
    import os

    from .tools.openroad.session import OpenRoadSession, find_openroad
    runner = runner or find_openroad()
    if runner is None:
        raise FileNotFoundError("OpenROAD not available")
    runner.mount(os.path.dirname(os.path.abspath(odb)))
    work = os.path.join(os.path.dirname(os.path.abspath(odb)), "_draw")
    with OpenRoadSession(work, runner) as s:
        s.run(f"read_db {q(os.path.abspath(odb))}")
        svg = layout_svg(s, scale=0.8 if ring_only else 0.6, ring_only=ring_only)
        cells, supplies, split = map(int, tcl_parse(s.value(RING_SUMMARY)))
    note = (f"IO ring: {cells} cells, {supplies} supply rails, "
            + ("all continuous" if split == 0 else f"{split} rails SPLIT"))
    svg = svg.replace("</g>\n</svg>", f'<text x="0" y="-1" font-size="10" fill="#333">{note}</text></g>\n</svg>')
    svg = svg.replace('<g transform="translate(10,10)">', '<g transform="translate(10,20)">', 1)
    with open(out, "w") as f:
        f.write(svg)
    return {"svg": out, "bytes": len(svg), "ring_cells": cells, "ring_supplies": supplies, "ring_split": split}
