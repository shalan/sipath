"""Hard-macro placement: candidates, rules and a quick placement-based score.

Methods (param ``method``):

* ``random`` (default): for each autoplaced macro, ``samples`` legal positions on
  the row/site grid drawn from ``random.Random(seed)`` (a share of them pushed
  against a core edge), each tried in every allowed orientation (``orients``
  param, else ``Macro.orients``, else ``Macro.orient``). The candidate with the
  lowest objective is kept: macro-net HPWL + ``analog_weight`` x analog-net
  HPWL, over placed terminals only and pin-accurate, so orientation and the
  side the pins are on count. The same seed gives the same placement.
* ``mpl``: OpenROAD's ``rtl_macro_placer``. Keepouts are passed as placement
  blockages (removed afterwards) and halos widened to the channel and guard
  rules; its result is checked against the same rules as ``random`` and the
  attempt fails if it breaks one.

Rules, on the macro boxes (``Design`` fields, overridable per attempt):

* halo: a macro grown by its halo stays in the core, off keepouts and off
  other grown macros;
* channel (``macro_channel``): two macros facing each other are at least this
  far apart, and a macro is either against the core edge (within halo + one
  row) or this far from it, so no sliver channels are left;
* pin side (``macro_pin_clearance``): in front of a macro edge that carries
  signal pins there is at least this much space to the next macro or the core
  edge, also against the core edge;
* analog guard (``analog_guard``): a macro on an analog net or analog supply
  keeps this much extra space to every other macro, and a placement blockage
  of that width around it keeps standard cells away.

Evaluation (param ``evaluate``, default on): once the macros are placed and
the rows cut, a non-timing global placement of the standard cells, a detailed
placement (legal positions, so no cell sits on a macro) and a global route with ``-allow_congestion`` measure ``quick_hpwl_um`` and the
congestion (``quick_overflow``, ``quick_congested_gcells``). The cells are then
unplaced and the guides cleared, so the checkpoint holds only the macros.
``macro_cost_um`` = quick HPWL + analog_weight x analog HPWL; the
``macro_quick`` score ranks by overflow, then that cost.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

from . import congestion
from .tcl import q, qlist, tcl_parse, tcl_words

ROT90 = ("R90", "R270", "MXR90", "MYR90")
ALL_ORIENTS = ("R0", "MX", "MY", "R180", "R90", "R270", "MXR90", "MYR90")
OPPOSITE = {"left": "right", "right": "left", "bottom": "top", "top": "bottom"}


@dataclass
class Rules:
    channel: float = 0.0  # DBU
    pin_clearance: float = 0.0
    analog_guard: float = 0.0
    hug: float = 0.0  # distance up to which a macro counts as against the core edge (beyond its halo)


@dataclass
class MacroBox:
    inst: str
    box: tuple  # x0 y0 x1 y1 (DBU), the macro itself
    halo: int
    pin_sides: frozenset = frozenset()  # placed sides with signal pins
    analog: bool = False


# ---------------------------------------------------------------- geometry
def oriented_size(w, h, orient):
    return (h, w) if orient in ROT90 else (w, h)


def orient_side(side: str, orient: str, w: int, h: int) -> str:
    """Placed side of a master side ``side`` for orientation ``orient``."""
    from .powerconnect import _rect
    local = {"left": (0, 0, 0, h), "right": (w, 0, w, h), "bottom": (0, 0, w, 0), "top": (0, h, w, h)}[side]
    W, H = oriented_size(w, h, orient)
    r = _rect(orient, 0, 0, w, h, *local)
    if r[0] == r[2]:
        return "left" if r[0] == 0 else "right"
    return "bottom" if r[1] == 0 else "top"


def pin_sides(pins, w, h, frac: float = 0.1) -> frozenset:
    """Master sides carrying at least ``frac`` of the signal pins (each pin
    counted once, on the side nearest its shapes' centre)."""
    count = {s: 0 for s in OPPOSITE}
    total = 0
    for _name, sig, _io, shapes in pins:
        if sig in ("POWER", "GROUND") or not shapes:
            continue
        xs = [(int(a) + int(c)) / 2 for a, _, c, _ in shapes]
        ys = [(int(b) + int(d)) / 2 for _, b, _, d in shapes]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        side = min((("left", cx), ("right", w - cx), ("bottom", cy), ("top", h - cy)), key=lambda t: t[1])[0]
        count[side] += 1
        total += 1
    return frozenset(s for s, n in count.items() if total and n >= max(1, frac * total))


def _grow(b, d):
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def _overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def violation(c: MacroBox, others, core, keepouts, rules: Rules) -> str | None:
    """First rule ``c`` breaks against the core, keepouts and placed macros."""
    x0, y0, x1, y1 = c.box
    h = c.halo
    gaps = {"left": x0 - core[0], "bottom": y0 - core[1], "right": core[2] - x1, "top": core[3] - y1}
    for side, g in gaps.items():
        if g < h:
            return f"halo leaves the core on the {side}"
        if side in c.pin_sides and g < rules.pin_clearance:
            return f"pins on the {side} edge are {g} from the core edge (< {rules.pin_clearance:.0f})"
        if rules.channel and h + rules.hug < g < rules.channel:
            return f"sliver channel of {g} to the core edge on the {side} (< {rules.channel:.0f})"
    grown = _grow(c.box, h)
    for k in keepouts:
        if _overlap(grown, k):
            return f"overlaps keepout {k}"
    for o in others:
        need = c.halo + o.halo + (rules.analog_guard if (c.analog or o.analog) else 0)
        gx = max(o.box[0] - x1, x0 - o.box[2])
        gy = max(o.box[1] - y1, y0 - o.box[3])
        if gy < 0 or gx < 0:  # projections overlap on one axis: they face each other
            horizontal = gy < 0
            g = gx if horizontal else gy
            if horizontal:
                side_c = "right" if o.box[0] >= x1 else "left"
            else:
                side_c = "top" if o.box[1] >= y1 else "bottom"
            req = max(need, rules.channel)
            if side_c in c.pin_sides or OPPOSITE[side_c] in o.pin_sides:
                req = max(req, rules.pin_clearance)
            if g < req:
                return f"{g} from {o.inst} on its {OPPOSITE[side_c]} (< {req:.0f})"
        elif gx < need and gy < need:
            return f"halo overlaps {o.inst}"
    return None


# ---------------------------------------------------------------- OpenROAD side
OBJ_TCL = r"""
proc ::flow_nets_hpwl {names} {
  # HPWL of the named nets over placed terminals only (DBU -> um)
  set block [ord::get_db_block]; set hp 0
  foreach n $names {
    set net [$block findNet $n]; if {$net eq "NULL"} continue
    set xs {}; set ys {}
    foreach t [$net getITerms] {
      if {![[$t getInst] isPlaced]} continue
      set a [$t getAvgXY]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } else {
        set b [[$t getInst] getBBox]; lappend xs [expr {([$b xMin]+[$b xMax])/2}]; lappend ys [expr {([$b yMin]+[$b yMax])/2}] } }
    foreach t [$net getBTerms] { set a [$t getFirstPinLocation]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } }
    if {[llength $xs] > 1} {
      set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
      set hp [expr {$hp + [lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]}]
    }
  }
  expr {double($hp) / [$block getDbUnitsPerMicron]}
}
proc ::flow_macro_try {inst orient x y analog} {
  set i [::flow_inst $inst]
  $i setOrient $orient; $i setLocation $x $y; $i setPlacementStatus PLACED
  list [::flow_macro_hpwl [list $inst]] [::flow_nets_hpwl $analog]
}
proc ::flow_macro_nets {inst} {
  set out {}
  foreach it [[::flow_inst $inst] getITerms] {
    set n [$it getNet]; if {$n ne "NULL"} { lappend out [$n getName] }
  }
  return $out
}
proc ::flow_cut_rows_box {x0 y0 x1 y1 min_width} {
  # split every row that overlaps the box into the parts left and right of it
  # (cut_rows only cuts around instances); parts narrower than min_width (DBU) are dropped
  set block [ord::get_db_block]; set n 0
  foreach r [$block getRows] {
    set rb [$r getBBox]
    if {!([$rb xMin] < $x1 && [$rb xMax] > $x0 && [$rb yMin] < $y1 && [$rb yMax] > $y0)} continue
    set site [$r getSite]; set sw [$r getSpacing]; set o [$r getOrigin]
    set ox [lindex $o 0]; set oy [lindex $o 1]; set ns [$r getSiteCount]
    set name [$r getName]; set orient [$r getOrient]; set dir [$r getDirection]
    set left [expr {($x0 - $ox) / $sw}]
    if {$left > $ns} { set left $ns }
    set rstart [expr {($x1 - $ox + $sw - 1) / $sw}]
    odb::dbRow_destroy $r
    set min_sites [expr {max(1, ($min_width + $sw - 1) / $sw)}]
    if {$left >= $min_sites} { odb::dbRow_create $block ${name}_gl $site $ox $oy $orient $dir $left $sw }
    if {$ns - $rstart >= $min_sites} {
      odb::dbRow_create $block ${name}_gr $site [expr {$ox + $rstart * $sw}] $oy $orient $dir [expr {$ns - $rstart}] $sw
    }
    incr n
  }
  return $n
}
proc ::flow_quick_eval {density report} {
  # quick standard-cell placement + congestion-tolerant global route; cells are
  # unplaced and guides cleared afterwards whatever happens (errors re-raised)
  set block [ord::get_db_block]
  set free {}
  foreach inst [$block getInsts] { if {![$inst isPlaced]} { lappend free $inst } }
  set rc [catch {
    global_placement -density $density -skip_io
    detailed_placement
    set hp [dict get [::flow_metrics] hpwl_um]
    global_route -allow_congestion -congestion_iterations 10 -congestion_report_file $report
  } err opts]
  foreach inst $free { $inst setPlacementStatus NONE }
  foreach net [$block getNets] { $net clearGuides }
  if {$rc == 1} { return -options $opts $err }
  list [llength $free] $hp
}
"""


def _rules(ctx, dbu, row_h) -> Rules:
    d, p = ctx.design, ctx.params
    def pick(name, design_value):  # a parameter left unset (None) falls back to the design's value
        v = p.get(name)
        return design_value if v is None else v
    return Rules(channel=pick("channel", d.macro_channel) * dbu,
                 pin_clearance=pick("pin_clearance", d.macro_pin_clearance) * dbu,
                 analog_guard=pick("analog_guard", d.analog_guard) * dbu, hug=row_h)


def _analog_insts(ctx) -> set[str]:
    d, s = ctx.design, ctx.session
    nets = set(d.analog_nets) | set(d.analog_supplies)
    out = set()
    for m in d.macros:
        if m.analog is not None:
            if m.analog:
                out.add(m.inst)
            continue
        if nets & set(tcl_words(s.value(f"::flow_macro_nets {q(m.inst)}"))):
            out.add(m.inst)
    return out


def _master_sides(ctx, inst):
    from .chip import _master_pins
    master = ctx.session.value(f"[[::flow_inst {q(inst)}] getMaster] getName")
    w, h, pins = _master_pins(ctx.session, master)
    return w, h, pin_sides(pins, w, h)


def _box_of(s, inst):
    return tuple(map(int, tcl_words(s.value(
        f"set b [[::flow_inst {q(inst)}] getBBox]; list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))))


def place(ctx) -> dict:  # noqa: C901
    from .chip import MACRO_HPWL, _core
    d, s, p = ctx.design, ctx.session, ctx.params
    s.run(MACRO_HPWL)
    s.run(OBJ_TCL)
    method = p.get("method", "random")
    if method not in ("random", "mpl"):
        raise ValueError(f"unknown macro placement method {method!r}")
    seed = p.get("seed", 0)
    rng = random.Random(seed)
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    core = tuple(_core(s))
    rx, ry, site_w, row_h = map(int, tcl_words(s.value(
        "set r [lindex [[ord::get_db_block] getRows] 0]; set o [$r getOrigin]; "
        "list [lindex $o 0] [lindex $o 1] [$r getSpacing] [[$r getBBox] dy]")))
    rules = _rules(ctx, dbu, row_h)
    keepouts = [tuple(int(v * dbu) for v in box) for box in d.keepouts]
    analog = _analog_insts(ctx)
    aw = float(p.get("analog_weight", 4.0))
    analog_nets = list(d.analog_nets)
    sides = {m.inst: _master_sides(ctx, m.inst) for m in d.macros}

    def mbox(m, orient, x, y):
        w, h, ps = sides[m.inst]
        W, H = oriented_size(w, h, orient)
        return MacroBox(m.inst, (x, y, x + W, y + H), int(m.halo * dbu),
                        frozenset(orient_side(sd, orient, w, h) for sd in ps), m.inst in analog)

    placed: list[MacroBox] = []
    out = {}
    for m in d.macros:
        if m.location is not None:
            x, y = int(round(m.location[0] * dbu)), int(round(m.location[1] * dbu))
            s.run(f"place_macro -macro_name {q(m.inst)} -location {{{m.location[0]} {m.location[1]}}} "
                  f"-orientation {m.orient} -exact\n[::flow_inst {q(m.inst)}] setPlacementStatus LOCKED")
            placed.append(mbox(m, m.orient, x, y))
            out[m.inst] = {"mode": "frozen", "loc_um": list(m.location), "orient": m.orient}
    autos = [m for m in d.macros if m.location is None]
    if method == "mpl" and autos:
        out.update(_mpl(ctx, autos, placed, keepouts, rules, analog, dbu, mbox, core))
    else:
        for m in autos:
            orients = p.get("orients") or m.orients or [m.orient]
            bad = [o for o in orients if o not in ALL_ORIENTS]
            if bad:
                raise ValueError(f"unknown orientation(s) {bad} for {m.inst}")
            best, tried, rejected = None, 0, {}
            want = p.get("samples", 40)
            for _ in range(want * 50):
                if tried >= want:
                    break
                w, h, _ps = sides[m.inst]
                W, H = oriented_size(w, h, orients[0])
                hl = int(m.halo * dbu)
                x = rng.randint(core[0] + hl, max(core[0] + hl, core[2] - max(w, h) - hl))
                y = rng.randint(core[1] + hl, max(core[1] + hl, core[3] - max(w, h) - hl))
                r = rng.random()  # a share of candidates hug a core edge (channel rule)
                if r < 0.15:
                    x = core[0] + hl
                elif r < 0.30:
                    y = core[1] + hl
                x = rx + -(-(x - rx) // site_w) * site_w  # up to the site grid
                y = ry + -(-(y - ry) // row_h) * row_h  # up to the row grid
                any_legal = False
                for o in orients:
                    c = mbox(m, o, x, y)
                    why = violation(c, placed, core, keepouts, rules)
                    if why:
                        key = why.split(" (")[0].split(" from ")[0][:60]
                        rejected[key] = rejected.get(key, 0) + 1
                        continue
                    any_legal = True
                    hp, ahp = map(float, tcl_words(s.value(
                        f"::flow_macro_try {q(m.inst)} {o} {x} {y} {qlist(analog_nets)}")))
                    obj = hp + aw * ahp
                    if best is None or obj < best[0]:
                        best = (obj, o, x, y, c, hp, ahp)
                tried += any_legal
            if best is None:
                raise RuntimeError(f"no legal position for macro {m.inst}; rejections: {rejected}")
            _, o, x, y, c, hp, ahp = best
            s.run(f"set i [::flow_inst {q(m.inst)}]; $i setOrient {o}; $i setLocation {x} {y}; "
                  "$i setPlacementStatus FIRM")
            placed.append(c)
            out[m.inst] = {"mode": "auto", "method": "random", "seed": seed, "loc_um": [x / dbu, y / dbu],
                           "orient": o, "candidates": tried, "objective_um": round(best[0], 3)}
    # analog guard: no standard cells within the guard band around analog macros
    guards = []
    min_row = row_min_width(s, ctx.pdk)
    if rules.analog_guard:
        for c in placed:
            if c.analog:
                g = _grow(c.box, c.halo + int(rules.analog_guard))
                g = (max(g[0], core[0]), max(g[1], core[1]), min(g[2], core[2]), min(g[3], core[3]))
                s.run(f"odb::dbBlockage_create [ord::get_db_block] {g[0]} {g[1]} {g[2]} {g[3]}")
                # no rows (taps, endcaps) under it; no row pieces too short for a well tap
                s.run(f"::flow_cut_rows_box {g[0]} {g[1]} {g[2]} {g[3]} {int(min_row * dbu)}")
                guards.append([v / dbu for v in g])
    if guards:
        ctx.outputs["analog_guard_blockages"] = guards
    halo = max((m.halo for m in d.macros), default=0)
    if d.macros:
        # a row piece must hold a well tap between its endcaps, or its n-well
        # floats (mcu8: 40 untapped wells beside the SRAM, found by LVS)
        s.run(f"cut_rows -halo_width_x {halo} -halo_width_y {halo} -row_min_width {min_row:g}")
    names = " ".join(q(m.inst) for m in d.macros)
    ctx.metrics["macro_hpwl_um"] = float(s.value(f"::flow_macro_hpwl {{{names}}}")) if d.macros else 0.0
    ctx.metrics["macro_hpwl_unplaced_terms"] = int(s.value("set ::flow_macro_hpwl_skipped")) if d.macros else 0
    ctx.metrics["analog_hpwl_um"] = float(s.value(f"::flow_nets_hpwl {qlist(analog_nets)}")) if analog_nets else 0.0
    ctx.metrics["analog_macros"] = sorted(analog)
    # every macro, frozen or not, checked against the rules (frozen ones are reported, not refused)
    final = []
    for c in placed:
        others = [o for o in placed if o is not c]
        final.append((c.inst, violation(c, others, core, keepouts, rules)))
    ctx.outputs["macro_rule_check"] = {i: (w or "ok") for i, w in final}
    if p.get("evaluate", True) and d.macros:
        _evaluate(ctx, aw)
    return out


def _mpl(ctx, autos, placed, keepouts, rules, analog, dbu, mbox, core) -> dict:
    s, d, p = ctx.session, ctx.design, ctx.params
    s.run("set ::flow_mpl_blk {}")
    for k in keepouts:
        s.run(f"lappend ::flow_mpl_blk [odb::dbBlockage_create [ord::get_db_block] {k[0]} {k[1]} {k[2]} {k[3]}]")
    for m in d.macros:
        h = m.halo * dbu + (rules.analog_guard / 2 if m.inst in analog else 0)
        h = max(h, rules.channel / 2, rules.pin_clearance / 2) / dbu
        s.run(f"set_macro_halo -macro_name {q(m.inst)} -halo {{{h:.3f} {h:.3f}}}")
    # rtl_macro_placer also places the standard cells; they are unplaced again
    # afterwards so every method hands the same kind of checkpoint on
    s.run("set ::flow_mpl_free {}\nforeach i [[ord::get_db_block] getInsts] { "
          "if {![$i isPlaced] && ![[$i getMaster] isBlock]} { lappend ::flow_mpl_free $i } }")
    rep = ctx.path("mpl")
    os.makedirs(rep, exist_ok=True)
    util = p.get("target_util", 0.25)
    try:
        s.run(f"rtl_macro_placer -report_directory {q(rep)} -target_util {util}", timeout=1800)
    finally:
        s.run("foreach b $::flow_mpl_blk { odb::dbBlockage_destroy $b }\nunset ::flow_mpl_blk\n"
              "foreach i $::flow_mpl_free { $i setPlacementStatus NONE }\nunset ::flow_mpl_free")
    out, problems = {}, []
    for m in autos:
        o, st = tcl_words(s.value(f"set i [::flow_inst {q(m.inst)}]; list [$i getOrient] [$i getPlacementStatus]"))
        x0, y0, _, _ = _box_of(s, m.inst)
        if st == "NONE":
            raise RuntimeError(f"rtl_macro_placer left {m.inst} unplaced")
        s.run(f"[::flow_inst {q(m.inst)}] setPlacementStatus FIRM")
        c = mbox(m, o, x0, y0)
        why = violation(c, placed, core, keepouts, rules)
        if why:
            problems.append(f"{m.inst}: {why}")
        placed.append(c)
        out[m.inst] = {"mode": "auto", "method": "mpl", "loc_um": [x0 / dbu, y0 / dbu], "orient": o}
    if problems:
        raise RuntimeError("rtl_macro_placer result breaks the macro rules:\n  " + "\n  ".join(problems))
    return out


def _evaluate(ctx, analog_weight):
    s, d = ctx.session, ctx.design
    from .tcl import routing_layers
    s.run(routing_layers(d, ctx.pdk))
    rpt = ctx.path("quick_congestion.rpt")
    n, hp = tcl_parse(s.value(f"::flow_quick_eval {ctx.params.get('quick_density', 0.6)} {q(rpt)}",
                              timeout=1800))
    cong = congestion.parse(rpt)
    ctx.metrics["quick_cells"] = int(n)
    ctx.metrics["quick_hpwl_um"] = float(hp)
    ctx.metrics["quick_overflow"] = cong["overflow"]
    ctx.metrics["quick_congested_gcells"] = cong["gcells"]
    ctx.metrics["macro_cost_um"] = float(hp) + analog_weight * ctx.metrics["analog_hpwl_um"]
    if cong["gcells"]:
        ctx.outputs.setdefault("reports", {})["quick_congestion"] = rpt


def row_min_width(session, pdk) -> float:
    """Shortest row piece (um) that is sure to get a well tap: taps go every
    2 x tap_distance along a row, plus a tap and the two endcaps. 0 without
    tap cells."""
    if not pdk.tap_cell:
        return 0.0
    def width(m):
        return float(session.value(f"expr {{double([[[ord::get_db] findMaster {m}] getWidth]) / "
                                   f"[[ord::get_db_block] getDbUnitsPerMicron]}}"))
    w = width(pdk.tap_cell) + (2 * width(pdk.endcap_cell) if pdk.endcap_cell else 0.0)
    return round(2 * pdk.tap_distance + w, 3)
