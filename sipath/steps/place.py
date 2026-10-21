"""Steps ``place`` (global and detailed placement with repair) and ``cts``."""
from __future__ import annotations

from ..tools.openroad import tcl
from ..harness import CHANNEL_ROW
from ..tools.openroad.tcl import q, qlist, tcl_words
from .common import check_placement, clock_nets, reconnect_supplies, reports, routability_check, tie_max_fanout
from .structure import cluster_spread, compact_cluster, datapath_check, enforce_fences, fence_violations, reabut_all


# pin channels out of global placement: their rows blocked, the core area the region's rows (what gpl
# sees without channels; the core is restored after it)
_CHANNEL_BLOCKAGES = f"""set ::flow_pinch_blk {{}}; set __r {{}}
foreach r [[ord::get_db_block] getRows] {{
  set b [$r getBBox]
  if {{![string match {CHANNEL_ROW}_* [$r getName]]}} {{
    lappend __r [$b xMin] [$b yMin] [$b xMax] [$b yMax]
    continue
  }}
  lappend ::flow_pinch_blk [odb::dbBlockage_create [ord::get_db_block] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
}}
set ::flow_core [[ord::get_db_block] getCoreArea]
set ::flow_core [list [$::flow_core xMin] [$::flow_core yMin] [$::flow_core xMax] [$::flow_core yMax]]
if {{[llength $::flow_pinch_blk]}} {{
  lassign [lrange $__r 0 3] x0 y0 x1 y1
  foreach {{a b c d}} $__r {{ set x0 [expr {{min($x0, $a)}}]; set y0 [expr {{min($y0, $b)}}]
                            set x1 [expr {{max($x1, $c)}}]; set y1 [expr {{max($y1, $d)}}] }}
  [ord::get_db_block] setCoreArea [odb::new_Rect $x0 $y0 $x1 $y1]
}}
llength $::flow_pinch_blk"""
_CHANNEL_RESTORE = ("foreach b $::flow_pinch_blk { odb::dbBlockage_destroy $b }\nunset ::flow_pinch_blk\n"
                    "[ord::get_db_block] setCoreArea [odb::new_Rect {*}$::flow_core]")


# per signal pin with cells on its net: {pin x y {buffer chain from the pin} last_x last_y}; the chain:
# single-load nets whose load is a buffer (master *buf*), at least two; last: the last buffer's centre
_PIN_CHAINS = r"""set __o {}
foreach __t [[ord::get_db_block] getBTerms] {
  set __n [$__t getNet]
  if {$__n eq "NULL" || [$__n isSpecial] || ![llength [$__n getITerms]] || ![llength [$__t getBPins]]} continue
  set __b [[lindex [$__t getBPins] 0] getBBox]
  set __chain {}
  while 1 {
    set __loads {}
    foreach __i [$__n getITerms] { if {![$__i isOutputSignal]} { lappend __loads $__i } }
    if {[llength $__loads] != 1} break
    set __inst [[lindex $__loads 0] getInst]
    if {![string match *buf* [[$__inst getMaster] getName]] || [lsearch -exact $__chain [$__inst getName]] >= 0} break
    set __out "NULL"
    foreach __i [$__inst getITerms] { if {[$__i isOutputSignal]} { set __out [$__i getNet] } }
    if {$__out eq "NULL"} break
    lappend __chain [$__inst getName]
    set __n $__out
  }
  if {[llength $__chain] < 2} continue
  set __lb [[::flow_inst [lindex $__chain end]] getBBox]
  lappend __o [list [$__t getName] [expr {([$__b xMin] + [$__b xMax]) / 2}] [expr {([$__b yMin] + [$__b yMax]) / 2}] \
    $__chain [expr {([$__lb xMin] + [$__lb xMax]) / 2}] [expr {([$__lb yMin] + [$__lb yMax]) / 2}]]
}
set __o"""

_ROW_BOXES = ("set __o {}; foreach r [[ord::get_db_block] getRows] { set b [$r getBBox]; "
              "lappend __o [list [$r getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] }; set __o")


def _along(path: list[tuple[int, int]], dist: float) -> tuple[int, int]:
    """The point ``dist`` along a Manhattan polyline."""
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        seg = abs(x1 - x0) + abs(y1 - y0)
        if dist <= seg and seg:
            f = dist / seg
            return round(x0 + (x1 - x0) * f), round(y0 + (y1 - y0) * f)
        dist -= seg
    return path[-1]


def space_pin_repeaters(ctx) -> list[dict]:
    """The buffer chains repair built on the nets of pins served by a pin
    channel, spaced evenly along the path the rows offer: from the pin into
    its channel, along it to the x of the chain's last buffer (clamped to the
    core region), up or down the region to that buffer, which stays where
    repair put it (next to the loads it was sized for). Repair spaces them on
    the straight line to the loads, across the empty wrapper, and
    legalization moves them to the nearest rows, some millimetres apart. Then
    legalization. Returns one record per chain moved."""
    s = ctx.session
    rows = [(w[0], *map(int, w[1:])) for w in map(tcl_words, tcl_words(s.value(_ROW_BOXES)))]
    region = [r for r in rows if not r[0].startswith(CHANNEL_ROW + "_")]
    chans = [r for r in rows if r[0].startswith(CHANNEL_ROW + "_")]
    if not chans or not region:
        return []
    rx0, ry0 = min(r[1] for r in region), min(r[2] for r in region)
    rx1, ry1 = max(r[3] for r in region), max(r[4] for r in region)
    out = []
    for w in map(tcl_words, tcl_words(s.value(_PIN_CHAINS))):
        name, px, py, chain, lx, ly = w[0], int(w[1]), int(w[2]), tcl_words(w[3]), int(w[4]), int(w[5])
        if rx0 <= px <= rx1 and ry0 <= py <= ry1:
            continue
        def dist(x0, y0, x1, y1):
            return max(x0 - px, 0, px - x1) + max(y0 - py, 0, py - y1)
        near = min(chans, key=lambda r: dist(*r[1:]))  # the channel row nearest the pin
        if dist(rx0, ry0, rx1, ry1) <= dist(*near[1:]):
            continue  # nearer the region than any channel: not a channel's pin
        yb = (near[2] + near[4]) // 2
        xa = min(max(px, near[1]), near[3])
        xr = min(max(lx, rx0), rx1)
        path = [(px, py), (xa, yb), (xr, yb), (xr, ly), (lx, ly)]
        total = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(path, path[1:]))
        first = abs(xa - px) + abs(yb - py)
        moved = chain[:-1]
        pts = [_along(path, first + (total - first) * i / len(moved)) for i in range(len(moved))]
        s.run("\n".join(f"set __i [::flow_inst {q(b)}]; set __m [$__i getMaster]\n"
                        f"$__i setLocation [expr {{{x} - [$__m getWidth] / 2}}] [expr {{{y} - [$__m getHeight] / 2}}]\n"
                        "$__i setPlacementStatus PLACED" for b, (x, y) in zip(moved, pts)))
        dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
        out.append({"pin": name, "buffers": len(chain), "path_um": round(total / dbu, 1),
                    "spacing_um": round((total - first) / len(moved) / dbu, 1)})
    if out:
        s.run("detailed_placement")
    return out


# the nets from each signal port (clock ports skipped) through its buffer chain: forward from an input
# (while a net's one load is a buffer), backward from an output (while its driver is a buffer whose
# input net has that one load), the net past the chain included; as STA nets
_PORT_NETS = r"""set __o {}
foreach __t [[ord::get_db_block] getBTerms] {
  set __n [$__t getNet]
  if {$__n eq "NULL" || [$__n isSpecial] || ![llength [$__n getITerms]]} continue
  if {[$__t getName] in $::flow_skip_ports} continue
  set __seen {}
  while {[lsearch -exact $__seen $__n] < 0} {
    lappend __seen $__n
    set __next "NULL"
    if {[$__t getIoType] eq "OUTPUT"} {
      foreach __i [$__n getITerms] {
        if {![$__i isOutputSignal] || ![string match *buf* [[[$__i getInst] getMaster] getName]]} continue
        foreach __j [[$__i getInst] getITerms] { if {[$__j isInputSignal]} { set __next [$__j getNet] } }
      }
    } else {
      set __loads {}
      foreach __i [$__n getITerms] { if {![$__i isOutputSignal]} { lappend __loads $__i } }
      if {[llength $__loads] == 1 && [string match *buf* [[[[lindex $__loads 0] getInst] getMaster] getName]]} {
        foreach __j [[[lindex $__loads 0] getInst] getITerms] {
          if {[$__j isOutputSignal]} { set __next [$__j getNet] }
        }
      }
    }
    if {$__next eq "NULL"} break
    set __loads 0
    foreach __i [$__next getITerms] { if {![$__i isOutputSignal]} { incr __loads } }
    set __n $__next
    if {$__loads != 1} { lappend __seen $__n; break }
  }
  foreach __n $__seen { if {[lsearch -exact $__o $__n] < 0} { lappend __o $__n } }
}
set ::flow_port_nets [lmap __n $__o { sta::db_net_to_sta $__n }]
llength $::flow_port_nets"""


def repair_port_nets(ctx) -> dict:
    """The nets of every signal port's buffer chain (``_PORT_NETS``)
    repaired with the ``pin_max_wire`` wire length limit (``rsz::repair_net_cmd``,
    repair_design's repair of one net), then legalized: a
    wrapper's pad nets run up to millimetres along its rows, and a buffer
    repair leaves on one may drive a millimetre of wire at the capacitance
    limit (other nets keep repair_design's own limits)."""
    s, p = ctx.session, ctx.params
    wire = p.get("pin_max_wire")
    if not wire:
        return {}
    m = getattr(ctx.pdk, "repair_margins", None) or {}
    count = "llength [[ord::get_db_block] getInsts]"
    n0 = int(s.value(count))
    nets = int(s.value(f"set ::flow_skip_ports {qlist(sorted(ctx.design.clock_ports))}\n" + _PORT_NETS))
    s.run(f"estimate_parasitics -placement\nforeach __n $::flow_port_nets {{ rsz::repair_net_cmd $__n "
          f"[sta::distance_ui_sta {wire:g}] {m.get('slew', 0)} {m.get('cap', 0)} }}\ndetailed_placement")
    return {"nets": nets, "buffers": int(s.value(count)) - n0}


def repair_legalized(ctx, rounds: int = 4) -> int:
    """repair_design on the legalized placement, then legalization, until a
    round inserts no instance (at most ``rounds``). Returns the rounds that
    inserted some."""
    s, p = ctx.session, ctx.params
    util = f" -max_utilization {p['max_util']}" if p.get("max_util") else ""
    count = "llength [[ord::get_db_block] getInsts]"
    for k in range(rounds):
        n = int(s.value(count))
        s.run(f"estimate_parasitics -placement\nrepair_design{util}{tcl.design_margins(ctx.pdk)}\ndetailed_placement")
        if int(s.value(count)) == n:
            return k
    return rounds


def place(ctx):
    """Timing-driven global placement, repair, detailed placement."""
    d, s, p = ctx.design, ctx.session, ctx.params
    if p.get("repair_tie_fanout", True):  # one tie cell per load, placed by gpl next to it
        for master, port in ctx.pdk.tie_cells():
            s.run(f"repair_tie_fanout -separation 5 {q(master + '/' + port)}")
    if p.get("remove_buffers"):
        s.run("remove_buffers")
    ctx.metrics["tie_max_fanout"] = tie_max_fanout(ctx)
    fence_groups = [g for g in d.groups if g.kind == "fence"]
    # with fences, this gpl build diverges inside the region after its first
    # timing-driven iteration unless the virtual resizes are discarded
    # (repair_design / repair_timing still run right after placement)
    if d.mode == "macro" and not d.floorplan_def:
        # pins placed by the flow: a placement ignoring them, pins by the cells, then the real one
        s.run(tcl.global_place(p.get("density", 0.55), p.get("seed"), timing_driven=False) + " -skip_io")
        s.run(tcl.place_pins(ctx.pdk))
    if d.harness:  # its pin channels are for repair's buffers, not for the logic gpl pulls to the far pins
        ctx.metrics["pin_channel_rows_blocked"] = int(s.value(_CHANNEL_BLOCKAGES))
    s.run(tcl.global_place(p.get("density", 0.55), p.get("seed"), timing_driven=p.get("timing_driven", True),
                           keep_resize=not fence_groups, routability_driven=p.get("routability_driven", False)))
    if d.harness:
        s.run(_CHANNEL_RESTORE)
    clusters = [g for g in d.groups if g.kind == "cluster"]
    for g in clusters:
        # spread right after gpl (cells still overlap) vs after packing + legalization
        ctx.outputs.setdefault("clusters", {})[g.name] = {"spread_gpl_um": cluster_spread(s, g)}
        fences = [tuple(ctx.parent_outputs("physical")["groups"][f.name]["box_dbu"]) for f in d.groups
                  if f.kind == "fence"]
        ctx.outputs["clusters"][g.name].update(compact_cluster(s, g, fences))
    s.run(tcl.repair_after_place(p.get("max_util"), ctx.pdk))
    ctx.outputs["reabut"] = reabut_all(ctx)  # sizing may have grown FIRM datapath cells
    s.run("detailed_placement")
    if d.harness:  # long pin nets across a mostly empty wrapper: buffers along the rows, then repair again
        ctx.outputs["pin_repeaters"] = space_pin_repeaters(ctx)
        ctx.outputs["port_nets"] = repair_port_nets(ctx)
        ctx.metrics["repair_legalized_rounds"] = repair_legalized(ctx)
    if p.get("improve_placement", True) and not fence_groups:
        # ORFS's placement cleanup: shorter wires, less congestion for the router
        s.run('improve_placement -max_displacement "5 1"\noptimize_mirroring')
    ctx.outputs["fence_evicted"] = enforce_fences(s, [g.name for g in d.groups if g.kind == "fence"])
    check_placement(ctx)
    for g in clusters:
        ctx.outputs["clusters"][g.name]["spread_legal_um"] = cluster_spread(s, g)
    fences = [g.name for g in d.groups if g.kind == "fence"]
    bad = fence_violations(s, fences)
    if bad:
        raise RuntimeError("fence violations:\n" + "\n".join(bad[:20]))
    ctx.metrics["fence_violations"] = 0
    datapath_check(ctx)
    if p.get("congestion_check", True):
        routability_check(ctx, p.get("congestion_margin", 0.0))
    reconnect_supplies(ctx)  # cells added by this step on the supply nets

def _analog_terms(ctx) -> dict[str, list[str]]:
    out = {}
    for n in ctx.design.analog_nets:
        v = ctx.session.value(f"set o {{}}; set net [::flow_net {q(n)}]; "
                              "foreach t [$net getITerms] { lappend o \"[[$t getInst] getName]/[[$t getMTerm] getName]\" }; "
                              "foreach t [$net getBTerms] { lappend o [$t getName] }; lsort $o")
        out[n] = tcl_words(v)
    return out

def cts(ctx):
    s = ctx.session
    nets = clock_nets(ctx)
    analog_before = _analog_terms(ctx)
    s.run(tcl.cts(ctx.pdk, nets))
    ctx.outputs["reabut"] = reabut_all(ctx)
    s.run("detailed_placement")
    ctx.outputs["fence_evicted"] = enforce_fences(s, [g.name for g in ctx.design.groups if g.kind == "fence"])
    check_placement(ctx)
    bad = fence_violations(s, [g.name for g in ctx.design.groups if g.kind == "fence"])
    if bad:
        raise RuntimeError("fence violations after CTS:\n" + "\n".join(bad[:20]))
    datapath_check(ctx, "after CTS")
    # the clock tree and repairs must not have touched analog nets (both modes)
    analog_after = _analog_terms(ctx)
    if analog_after != analog_before:
        changed = sorted(n for n in analog_before if analog_before[n] != analog_after.get(n))
        raise RuntimeError(f"analog nets changed during CTS/repair: {changed}")
    reports(ctx, "cts")
    reconnect_supplies(ctx)  # cells added by this step on the supply nets
