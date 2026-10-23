"""Die mode: pad ring and hard macros.

``pad_ring``: ICeWall IO sites around the die, pads placed per edge in the
caller's order (south/north left to right, west/east bottom to top), corner
cells, IO filler slices, abutment connections, and top-level terminals on
each pad's PAD pin.

``place_macros``: see ``sipath.macroplace`` (frozen macros LOCKED, the rest
autoplaced by seeded random search with orientation and rule checks, or by
rtl_macro_placer; scored after a quick placement). The step-level sweep over
seeds and methods is done by the scheduler.
"""
from __future__ import annotations

from .tools.openroad.tcl import q, qe, tcl_parse, tcl_words

EDGE_ROW = {"south": "IO_SOUTH", "north": "IO_NORTH", "west": "IO_WEST", "east": "IO_EAST"}

MACRO_HPWL = r"""
proc ::flow_macro_hpwl {names} {
  set block [ord::get_db_block]
  set ::flow_macro_hpwl_skipped 0
  set seen {}; set hp 0
  foreach n $names {
    foreach it [[::flow_inst $n] getITerms] {
      set net [$it getNet]
      if {$net eq "NULL" || [$net getSigType] in {POWER GROUND}} continue
      set id [$net getId]; if {[dict exists $seen $id]} continue; dict set seen $id 1
      set xs {}; set ys {}
      # only placed terminals (pads, pins, macros): standard cells are still unplaced
      # at the origin during macro placement and would pull every macro toward (0,0)
      foreach t [$net getITerms] {
        if {![[$t getInst] isPlaced]} { incr ::flow_macro_hpwl_skipped; continue }
        set a [$t getAvgXY]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } else {
          set b [[$t getInst] getBBox]; lappend xs [expr {([$b xMin]+[$b xMax])/2}]; lappend ys [expr {([$b yMin]+[$b yMax])/2}] } }
      foreach t [$net getBTerms] { set a [$t getFirstPinLocation]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } }
      if {[llength $xs] > 1} {
        set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
        set hp [expr {$hp + [lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]}]
      }
    }
  }
  expr {double($hp) / [$block getDbUnitsPerMicron]}
}
"""


RING_CHECK = r"""
set block [ord::get_db_block]
set nets {}; set open {}; set cells 0
foreach inst [$block getInsts] {
  set t [[$inst getMaster] getType]
  if {![string match PAD* $t] && ![string match ENDCAP* $t]} continue
  incr cells
  foreach it [$inst getITerms] {
    set pin [[$it getMTerm] getName]
    if {[lsearch -exact $::flow_ring_pins $pin] < 0} continue
    set n [$it getNet]
    if {$n eq "NULL"} { lappend open "[$inst getName]/$pin"; continue }
    dict set nets $pin [$n getName] 1
  }
}
set split {}
dict for {pin ns} $nets { if {[dict size $ns] != 1} { lappend split "$pin:[dict size $ns]" } }
list cells $cells supplies [dict size $nets] split $split open [llength $open] first_open [lrange $open 0 4]
"""
def pad_ring(ctx) -> dict:
    d, s = ctx.design, ctx.session
    m = d.pad_masters
    from . import padplan
    plan = padplan.check(d.pads, ctx.pdk.io_roles, d.pad_kinds, d.pad_rules) if ctx.pdk.io_roles else {}
    # IO-cell supply pins onto their nets before abutment (pad ring bus = core supply)
    for pat, net in d.pad_supplies.items():
        net = d.net(net)
        kind = "-ground" if net.upper().startswith(("VSS", "GND")) else "-power"
        s.run(f"add_global_connection -net {net} -inst_pattern {{.*}} -pin_pattern {q(pat)} {kind}")
    if d.pad_supplies:
        s.run("global_connect")
    # IO geometry from the library itself (overridable in pad_masters):
    # corner site = corner cell size, S/N rows as deep as the corner is tall,
    # W/E rows as deep as it is wide; corner rotation from its bus-exit edges;
    # pad rotations by trial (core-side pins must face the core)
    geo = io_geometry(ctx)
    cw, ch = m.get("corner_site", geo["corner_site"])
    depth_ns, depth_ew = m.get("row_depth_ns", geo["row_depth_ns"]), m.get("row_depth_ew", geo["row_depth_ew"])
    rot_corner = m.get("rotation_corner", geo["rotation_corner"])
    rot_ns, rot_ew = m.get("rotation_ns"), m.get("rotation_ew")
    if rot_ns is None or rot_ew is None:
        found = choose_pad_rotations(ctx, cw, ch, depth_ns, depth_ew, rot_corner)
        rot_ns, rot_ew = rot_ns or found[0], rot_ew or found[1]
    s.run(_io_sites(cw, ch, depth_ns, depth_ew, rot_ns, rot_ew, rot_corner))
    io_geo = {"corner_site": [cw, ch], "row_depth_ns": depth_ns, "row_depth_ew": depth_ew,
              "rotation_ns": rot_ns, "rotation_ew": rot_ew, "rotation_corner": rot_corner}
    x0, y0, x1, y1 = d.die
    placed = {}
    for edge in ("south", "north", "west", "east"):
        pads = sorted((p for p in d.pads if p.edge == edge), key=lambda p: p.order)
        if not pads:
            continue
        length = (x1 - x0) if edge in ("south", "north") else (y1 - y0)
        corner = cw if edge in ("south", "north") else ch
        lo, hi = corner, length - corner
        widths = [float(s.value(f"expr {{double([[[::flow_inst {q(p.inst)}] getMaster] getWidth]) / "
                                f"[[ord::get_db_block] getDbUnitsPerMicron]}}")) for p in pads]
        if sum(widths) > hi - lo:
            raise ValueError(f"{edge}: pads need {sum(widths):.1f}um, edge has {hi - lo:.1f}um")
        gap = (hi - lo - sum(widths)) / len(pads)
        pos = lo + gap / 2
        script = []
        for p, w in zip(pads, widths):
            loc = round(pos)
            script.append(f"place_pad -row {EDGE_ROW[edge]} -location {loc} {q(p.inst)}")
            placed[p.inst] = (edge, loc)
            pos += w + gap
        s.run("\n".join(script))
    s.run(f"place_corners {m['corner']}")
    for row in EDGE_ROW.values():
        s.run(f"place_io_fill -row {row} {' '.join(m['fill'])}")
    s.run("connect_by_abutment")
    # the ring must be closed: one net per IO supply, every IO cell on it
    s.run(f"set ::flow_ring_pins {{{' '.join(m.get('ring_pins') or ring_pins(ctx))}}}")
    ring = tcl_parse(s.value(RING_CHECK))
    ring = dict(zip(ring[0::2], ring[1::2]))
    if ring["split"] or ring["open"] != "0":
        raise RuntimeError(f"pad ring is broken: supplies split into several nets {ring['split']}, "
                           f"{ring['open']} unconnected supply pins {ring['first_open']}")
    # no floating pad inputs: every INPUT signal pin of every pad has a net
    floating = tcl_words(s.value("""
set o {}
foreach inst [[ord::get_db_block] getInsts] {
  if {![string match PAD* [[$inst getMaster] getType]]} continue
  foreach it [$inst getITerms] {
    set mt [$it getMTerm]
    if {[$mt getSigType] ne "SIGNAL" || [$mt getIoType] ne "INPUT"} continue
    if {[$it getNet] eq "NULL"} { lappend o "[$inst getName]/[$mt getName]" }
  }
}
set o"""))
    if floating and m.get("require_tied_pad_inputs", True):
        raise RuntimeError(f"{len(floating)} floating pad input pins, e.g. {floating[:8]}")
    corners = tcl_parse(s.value(
        "set o {}; foreach i [[ord::get_db_block] getInsts] { if {[[$i getMaster] getName] eq "
        + qe(m["corner"]) + "} "
        "{ set b [$i getBBox]; lappend o [list [$i getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] } }; set o"))
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    die = [int(round(v * dbu)) for v in d.die]
    for name, *bb in corners:
        bx0, by0, bx1, by1 = map(int, bb)
        if not ((bx0 == die[0] or bx1 == die[2]) and (by0 == die[1] or by1 == die[3])):
            raise RuntimeError(f"corner {name} is not on a die corner: {bb}")
    terms = m.get("terminal_pin", "PAD")
    s.run(f"place_io_terminals {{*/{terms}}}")
    s.run("remove_io_rows")
    # order check: read back pad positions and verify edge + order
    for edge in ("south", "north", "west", "east"):
        pads = sorted((p for p in d.pads if p.edge == edge), key=lambda p: p.order)
        coords = []
        for p in pads:
            b = s.value(f"set b [[::flow_inst {q(p.inst)}] getBBox]; list [$b xMin] [$b yMin] [$b xMax] [$b yMax]").split()
            bx0, by0, bx1, by1 = map(int, b)
            coords.append(bx0 if edge in ("south", "north") else by0)
            dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
            if edge == "south" and by0 != int(round(y0 * dbu)) or edge == "north" and by1 != int(round(y1 * dbu)) \
                    or edge == "west" and bx0 != int(round(x0 * dbu)) or edge == "east" and bx1 != int(round(x1 * dbu)):
                raise RuntimeError(f"pad {p.inst} is not on the {edge} edge: {b}")
        if coords != sorted(coords):
            raise RuntimeError(f"{edge} pads out of order: {[p.inst for p in pads]}")
        for p in pads:
            ok = pad_faces_core(s, p.inst, edge)
            if ok is False:
                raise RuntimeError(f"pad {p.inst}: core-side pins do not face the core")
    terms = supply_terminals(ctx)
    return {"pads": len(placed), "ring_cells": int(ring["cells"]), "ring_supplies": int(ring["supplies"]),
            "supply_terminals": terms,
            "pad_plan": plan, "floating_pad_inputs": len(floating), "io_geometry": io_geo,
            "fill": int(s.value("set n 0; foreach i [[ord::get_db_block] getInsts] { "
                                "if {[[$i getMaster] getType] eq \"PAD_SPACER\"} { incr n } }; set n"))}


def _core(s):
    return list(map(int, s.value("set c [[ord::get_db_block] getCoreArea]; list [$c xMin] [$c yMin] [$c xMax] [$c yMax]").split()))


def _size(s, inst, orient="R0"):
    w, h = map(int, s.value(f"set m [[::flow_inst {q(inst)}] getMaster]; list [$m getWidth] [$m getHeight]").split())
    return (h, w) if "90" in orient or "270" in orient else (w, h)


def place_macros(ctx) -> dict:
    """Hard macros: frozen ones at their location, the rest autoplaced (see sipath.macroplace)."""
    from . import macroplace
    return macroplace.place(ctx)


def supply_pads(ctx):
    """(pad inst, pin, net) for every supply pad whose role pin maps to a net."""
    import re as _re
    from . import padplan
    out = []
    for p in ctx.design.pads:
        role = padplan.role_of(p.master, ctx.pdk.io_roles) if ctx.pdk.io_roles else "signal"
        if role == "signal":
            continue
        pin = role.upper()
        for pat, net in ctx.design.pad_supplies.items():
            if _re.fullmatch(pat.strip("^$"), pin) or _re.search(pat, pin):
                out.append((p.inst, pin, ctx.design.net(net)))
                break
    return out


def supply_terminals(ctx) -> dict:
    """Top-level supply terminals on the supply pads' bond pins (where supply
    enters the chip), so power-grid analysis has real sources."""
    s = ctx.session
    by_net = {}
    for inst, pin, net in supply_pads(ctx):
        by_net.setdefault(net, []).append(f"{inst}/{pin}")
    for net in by_net:
        s.run(f"set n [::flow_net {q(net)}]\n"
              f"if {{[[ord::get_db_block] findBTerm {q(net)}] eq \"NULL\"}} {{ set bt [odb::dbBTerm_create $n {q(net)}]; "
              "$bt setSigType [$n getSigType]; $bt setIoType INOUT }")
    pins = [p for v in by_net.values() for p in v]
    if pins:
        s.run(f"place_io_terminals {{{' '.join(pins)}}}")
    return {net: len(v) for net, v in by_net.items()}


def _fake_sites(cw, ch, depth_ns, depth_ew):
    # created once per session: re-creating an existing fake site crashes OpenROAD
    return (f"if {{![info exists ::flow_io_sites]}} {{\nset ::flow_io_sites 1\n"
            f"make_fake_io_site -name IO_HSITE -width 1 -height {depth_ew}\n"
            f"make_fake_io_site -name IO_VSITE -width 1 -height {depth_ns}\n"
            f"make_fake_io_site -name IO_CSITE -width {cw} -height {ch}\n}}")


def _io_sites(cw, ch, depth_ns, depth_ew, rot_ns, rot_ew, rot_corner):
    return (_fake_sites(cw, ch, depth_ns, depth_ew) + "\n"
            "make_io_sites -horizontal_site IO_HSITE -vertical_site IO_VSITE -corner_site IO_CSITE -offset 0 "
            f"-rotation_vertical {rot_ns} -rotation_horizontal {rot_ew} -rotation_corner {rot_corner}")


MASTER_PINS = r"""
proc ::flow_master_pins {master} {
  set m [[ord::get_db] findMaster $master]
  if {$m eq "NULL"} { error "no master $master" }
  set out {}
  foreach mt [$m getMTerms] {
    set shapes {}
    foreach mp [$mt getMPins] { foreach g [$mp getGeometry] {
      if {[$g getTechLayer] eq "NULL"} continue
      lappend shapes [list [$g xMin] [$g yMin] [$g xMax] [$g yMax]]
    } }
    lappend out [list [$mt getName] [$mt getSigType] [$mt getIoType] $shapes]
  }
  return [list [$m getWidth] [$m getHeight] $out]
}
"""


def _master_pins(s, master):
    s.run(MASTER_PINS)
    w, h, pins = tcl_parse(s.value(f"::flow_master_pins {q(master)}"))
    return int(w), int(h), pins


def io_geometry(ctx) -> dict:
    """Corner site, row depths and corner rotation from the corner cell's LEF."""
    s = ctx.session
    w, h, pins = _master_pins(s, ctx.design.pad_masters["corner"])
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    # count distinct pins touching each edge; the buses exit on the adjacent
    # pair with the most (a stray pin touching another edge does not count)
    touch = {"left": set(), "right": set(), "top": set(), "bottom": set()}
    for name, sig, io, shapes in pins:
        for x0, y0, x1, y1 in (tuple(map(int, r)) for r in shapes):
            if x0 <= 0:
                touch["left"].add(name)
            if y0 <= 0:
                touch["bottom"].add(name)
            if x1 >= w:
                touch["right"].add(name)
            if y1 >= h:
                touch["top"].add(name)
    # ICeWall's corner site is the south-west corner: the buses must leave
    # toward the south row (right) and the west column (top)
    pairs = {("top", "right"): "R0", ("left", "bottom"): "R180", ("left", "top"): "MY", ("bottom", "right"): "MX"}
    best = max(pairs, key=lambda pr: len(touch[pr[0]]) + len(touch[pr[1]]))
    if not touch[best[0]] or not touch[best[1]]:
        raise RuntimeError(f"corner {ctx.design.pad_masters['corner']}: cannot tell its bus exits "
                           f"{ {k: len(v) for k, v in touch.items()} }; set pad_masters['rotation_corner']")
    rot = pairs[best]
    return {"corner_site": (w / dbu, h / dbu), "row_depth_ns": h / dbu, "row_depth_ew": w / dbu,
            "rotation_corner": rot}


def ring_pins(ctx) -> list[str]:
    """Abutment bus pins: the pin names every IO filler master has."""
    names = None
    for f in ctx.design.pad_masters["fill"]:
        _, _, pins = _master_pins(ctx.session, f)
        cur = {p[0] for p in pins}
        names = cur if names is None else names & cur
    return sorted(names or [])


def _core_side(s, master):
    """Master edge nearest the centroid of its core-side signal pins (INPUT or
    OUTPUT signal pins; the INOUT bond pin is excluded). None if it has none."""
    w, h, pins = _master_pins(s, master)
    pts = []
    for name, sig, io, shapes in pins:
        if sig == "SIGNAL" and io in ("INPUT", "OUTPUT"):
            for x0, y0, x1, y1 in (tuple(map(int, r)) for r in shapes):
                pts.append(((x0 + x1) / 2, (y0 + y1) / 2))
    if not pts:
        return None
    cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return min((("bottom", cy), ("top", h - cy), ("left", cx), ("right", w - cx)), key=lambda t: t[1])[0], w, h


def pad_faces_core(s, inst, edge):
    """True/False whether the placed pad's core-side pins face the core;
    None for pads without core-side signal pins (supply pads)."""
    from .powerconnect import _rect
    master = s.value(f"[[::flow_inst {q(inst)}] getMaster] getName")
    cs = _core_side(s, master)
    if cs is None:
        return None
    side, w, h = cs
    o, bx0, by0, bx1, by1 = tcl_words(s.value(
        f"set i [::flow_inst {q(inst)}]; set b [$i getBBox]; list [$i getOrient] [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))
    bx0, by0, bx1, by1 = map(int, (bx0, by0, bx1, by1))
    local = {"bottom": (0, 0, w, 1), "top": (0, h - 1, w, h), "left": (0, 0, 1, h), "right": (w - 1, 0, w, h)}[side]
    r = _rect(o, bx0, by0, w, h, *local)
    want = {"south": r[3] >= by1 - 1, "north": r[1] <= by0 + 1, "west": r[2] >= bx1 - 1, "east": r[0] <= bx0 + 1}
    return want[edge]


def choose_pad_rotations(ctx, cw, ch, depth_ns, depth_ew, rot_corner):
    """Try ICeWall row rotations with one probe pad per edge; keep those that
    make the pads' core-side pins face the core. Probes are unplaced after."""
    s, d = ctx.session, ctx.design
    probes = {}
    for edge in ("south", "north", "west", "east"):
        for p in sorted((p for p in d.pads if p.edge == edge), key=lambda p: p.order):
            master = s.value(f"[[::flow_inst {q(p.inst)}] getMaster] getName")
            if _core_side(s, master) is not None:
                probes[edge] = p.inst
                break
    x0, y0, x1, y1 = d.die
    choice = {}
    for group, cands, fixed in (("ns", ["R180", "R0", "MX", "MY"], "R90"),
                                ("ew", ["R90", "R270", "MXR90", "MYR90"], None)):
        edges = ("south", "north") if group == "ns" else ("west", "east")
        for rot in cands:
            rns = rot if group == "ns" else choice["ns"]
            rew = fixed if group == "ns" else rot
            s.run(_io_sites(cw, ch, depth_ns, depth_ew, rns, rew, rot_corner))
            ok = True
            for edge in edges:
                if edge not in probes:
                    continue
                length = (x1 - x0) if edge in ("south", "north") else (y1 - y0)
                s.run(f"place_pad -row {EDGE_ROW[edge]} -location {round(length / 2)} {q(probes[edge])}")
                ok = ok and pad_faces_core(s, probes[edge], edge) is True
            for edge in edges:
                if edge in probes:
                    s.run(f"[::flow_inst {q(probes[edge])}] setPlacementStatus NONE")
            s.run("remove_io_rows")
            if ok:
                choice[group] = rot
                break
        if group not in choice:
            raise RuntimeError(f"no IO row rotation makes the {group} pads face the core")
    return choice["ns"], choice["ew"]
