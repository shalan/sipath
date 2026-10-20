"""Steps ``floorplan`` (die, rows, pins, pad ring), ``macros`` (hard macro placement) and ``physical``
(power grid, taps, endcaps, supply straps)."""
from __future__ import annotations

from .. import datapath, tcl
from ..tcl import qe, tcl_words
from .common import _via_array, connect_supplies, grid_nets, macro_supply_pins
from .structure import _dp_tap, _snap_box, datapath_check, reabut_all


# ----------------------------------------------------------------------- steps
def floorplan(ctx):
    """Soft macro: floorplan from the given DEF. Die: die box + pad ring."""
    d, s = ctx.design, ctx.session
    if d.mode == "macro" and d.floorplan_def:
        fp = d.floorplan_def
        if d.harness:  # rows to the used pins far from the core region (netlist linked, no DEF read yet)
            from .. import harness
            h = harness.get(d.harness, d.harness_options)
            used = tcl_words(s.value(
                "set o {}; foreach t [[ord::get_db_block] getBTerms] { set n [$t getNet]; "
                "if {$n ne \"NULL\" && [llength [$n getITerms]]} { lappend o [$t getName] } }; set o"))
            path, channels = h.floorplan(ctx.pdk, ctx.path(f"{d.top}.fp.def"), used)
            if channels:
                fp = path
                ctx.outputs["harness_pin_channels"] = [[k, a, b] for k, a, b in channels]
            ctx.metrics["harness_pin_channels"] = len(channels)
        s.run(tcl.floorplan_from_def(ctx.pdk, fp))
        if d.harness:  # the frame's user-supply pins (the netlist has no power ports)
            s.run(h.create_power_pins_tcl())
            ctx.metrics["harness_pins_clipped"] = int(s.value(h.clip_pins_tcl()))  # restored at signoff
            ctx.outputs["harness"] = d.harness
    elif d.mode == "macro":  # die box or utilization; pins at random until placement places them
        s.run(tcl.floorplan_die(ctx.pdk, d))
        s.run(tcl.place_pins(ctx.pdk, random=True))
        ctx.outputs["auto_pins"] = True
    else:
        from .. import chip
        s.run(tcl.floorplan_die(ctx.pdk, d))
        ctx.outputs["pad_ring"] = chip.pad_ring(ctx)
    ctx.metrics["rows"] = int(s.value("llength [[ord::get_db_block] getRows]"))

def macros(ctx):
    """Hard macros: frozen at a location or autoplaced (params: seed, samples)."""
    from .. import chip
    ctx.outputs["macros"] = chip.place_macros(ctx)

def fit_straps(ctx) -> list:
    """The design's core straps; PDK-default straps that do not fit the core
    (a VDD/VSS pair needs offset + pitch/2 + width) are scaled to it: offset
    10 % and pitch 80 % of the core across the straps, recorded in outputs.
    A design's own straps are used as given (pdngen reports a misfit), but
    with pin channels (rows outside the core region, which widen the core)
    their offsets keep them where they are without channels, over the
    region (recorded in outputs)."""
    from ..design import Strap
    d, s = ctx.design, ctx.session
    if not d.pdn.straps_from_pdk:
        return align_straps(ctx, list(d.pdn.straps))
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    x0, y0, x1, y1 = (int(v) / dbu for v in tcl_words(s.value(
        "set b [[ord::get_db_block] getCoreArea]; list [$b xMin] [$b yMin] [$b xMax] [$b yMax]")))
    out, scaled = [], []
    for st in d.pdn.straps:
        vertical = s.value(f"[[ord::get_db_tech] findLayer {st.layer}] getDirection") == "VERTICAL"
        span = (x1 - x0) if vertical else (y1 - y0)
        if span >= st.offset + st.pitch / 2 + 2 * st.width:
            out.append(st)
            continue
        pitch = max(round(span * 0.8, 2), 4 * st.width)
        new = Strap(st.layer, st.width, pitch, round(max(span * 0.1, st.width), 2))
        out.append(new)
        scaled.append({"layer": st.layer, "core_um": round(span, 2), "pitch": [st.pitch, new.pitch],
                       "offset": [st.offset, new.offset]})
    if scaled:
        ctx.outputs["pdn_straps_scaled"] = scaled
    return out

def align_straps(ctx, straps: list) -> list:
    """``straps`` with their offsets from the core origin moved by the
    distance from it to the core region's rows (modulo the pitch), when pin
    channel rows widen the core: the region's grid is then the one it has
    without channels."""
    from ..design import Strap
    from ..harness import CHANNEL_ROW
    s = ctx.session
    v = tcl_words(s.value(f"""set __r {{}}; set __c 0
foreach __row [[ord::get_db_block] getRows] {{
  if {{[string match {CHANNEL_ROW}_* [$__row getName]]}} {{ incr __c; continue }}
  set __b [$__row getBBox]; lappend __r [$__b xMin] [$__b yMin]
}}
set __a [[ord::get_db_block] getCoreArea]
list $__c [$__a xMin] [$__a yMin] [::tcl::mathfunc::min {{*}}[lmap {{x y}} $__r {{ set x }}]] \
  [::tcl::mathfunc::min {{*}}[lmap {{x y}} $__r {{ set y }}]] [[ord::get_db_block] getDbUnitsPerMicron]"""))
    nchan, cx0, cy0, rx0, ry0, dbu = map(int, v)
    if not nchan or (cx0, cy0) == (rx0, ry0):
        return straps
    out = []
    for st in straps:
        vertical = s.value(f"[[ord::get_db_tech] findLayer {st.layer}] getDirection") == "VERTICAL"
        shift = ((rx0 - cx0) if vertical else (ry0 - cy0)) % round(st.pitch * dbu) / dbu
        out.append(Strap(st.layer, st.width, st.pitch, round((st.offset + shift) % st.pitch, 3)))
    ctx.outputs["pdn_straps_aligned"] = [{"layer": a.layer, "offset": [a.offset, b.offset]}
                                         for a, b in zip(straps, out)]
    return out

def rows_without_tap(ctx) -> list[str]:
    """Rows with no well-tap instance on them (empty without tap cells)."""
    tap = ctx.pdk.tap_cell
    if not tap:
        return []
    return tcl_words(ctx.session.value(f"""
set __taps {{}}
foreach __i [[ord::get_db_block] getInsts] {{
  if {{[[$__i getMaster] getName] ne {qe(tap)}}} continue
  set __b [$__i getBBox]
  dict lappend __taps [$__b yMin] [list [$__b xMin] [$__b xMax]]
}}
set __out {{}}
foreach __r [[ord::get_db_block] getRows] {{
  set __rb [$__r getBBox]; set __ok 0
  if {{[dict exists $__taps [$__rb yMin]]}} {{
    foreach __t [dict get $__taps [$__rb yMin]] {{
      if {{[lindex $__t 0] >= [$__rb xMin] && [lindex $__t 1] <= [$__rb xMax]}} {{ set __ok 1; break }}
    }}
  }}
  if {{!$__ok}} {{ lappend __out [$__r getName] }}
}}
set __out"""))

def physical(ctx):
    """Datapath structures, hierarchy groups, endcaps + taps, PDN."""
    d, s = ctx.design, ctx.session
    for dp in d.datapaths:
        pl = datapath.place_datapath(s, dp, _dp_tap(ctx))
        ctx.outputs.setdefault("datapaths", {})[dp.name] = len([p for p in pl if not p.tap])
    for g in d.groups:
        if g.kind == "fence":
            box = _snap_box(s, g.box)
            hit = s.value(f"""
set hit {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{![[$inst getMaster] isBlock]}} continue
  set b [$inst getBBox]; set h [expr {{int({max((m.halo for m in d.macros), default=0)} * [[ord::get_db_block] getDbUnitsPerMicron])}}]
  if {{[$b xMin] - $h < {box[2]} && [$b xMax] + $h > {box[0]} && [$b yMin] - $h < {box[3]} && [$b yMax] + $h > {box[1]}}} {{
    lappend hit [$inst getName]
  }}
}}
set hit""")
            if hit.strip():
                raise ValueError(f"fence {g.name} overlaps macro(s) {hit} (with halo)")
            n = s.value(tcl.fence(g, box))
            ctx.outputs.setdefault("groups", {})[g.name] = {"kind": "fence", "insts": int(n), "box_dbu": box}
        elif g.kind == "cluster":
            n = s.value(tcl.cluster_group(g))
            ctx.outputs.setdefault("groups", {})[g.name] = {"kind": "cluster", "insts": int(n)}
        else:
            raise ValueError(f"unknown group kind {g.kind}")
    s.run(tcl.endcaps_and_taps(ctx.pdk))
    if d.datapaths:  # a tap in each structure's tap slots; grid taps under a structure moved past it
        ctx.outputs["datapath_taps"] = reabut_all(ctx)
        datapath_check(ctx, "after tap insertion")
    untapped = rows_without_tap(ctx)
    ctx.metrics["rows_without_tap"] = len(untapped)
    if untapped:
        raise RuntimeError(f"{len(untapped)} row(s) have no well tap (their n-well would float), e.g. "
                           f"{untapped[:5]}; cut rows that short (macroplace.row_min_width)")
    s.run(tcl.pdn(d, ctx.pdk, fit_straps(ctx)))
    connect_supplies(ctx)
    if d.harness:  # the frame's user-supply pins to the grid
        from .. import powerconnect
        die = tuple(map(int, tcl_words(s.value("set b [[ord::get_db_block] getDieArea]; "
                                               "list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))))
        recs = powerconnect.connect_bterms(s, sorted(grid_nets(d)), list(ctx.pdk.layers), die, **_via_array(ctx))
        ctx.outputs["harness_supply_straps"] = recs
        # the frame feeds every supply pin; the grid needs each supply reached once
        reached = {r["net"] for r in recs if r["ok"]}
        missing = sorted(grid_nets(d) - reached)
        ctx.outputs["harness_supply_pins_unreached"] = [r["pin"] for r in recs if not r["ok"]]
        ctx.metrics["harness_supply_straps"] = sum(1 for r in recs if r["ok"])
        if missing:
            raise RuntimeError(f"harness supplies {missing} reach the grid through none of their pins: "
                               f"{[(r['pin'], r.get('why')) for r in recs if r['net'] in missing][:4]}")
    ctx.metrics["taps"] = int(s.value(
        f"set n 0; foreach i [[ord::get_db_block] getInsts] {{ if {{[[$i getMaster] getName] eq {qe(ctx.pdk.tap_cell)}}} {{ incr n }} }}; set n"))
    ctx.metrics["endcaps"] = int(s.value(
        "set n 0; foreach i [[ord::get_db_block] getInsts] { if {[string match PHY_EDGE* [$i getName]]} { incr n } }; set n"))
    ctx.metrics["rows"] = int(s.value("llength [[ord::get_db_block] getRows]"))
    supply = {m.inst: macro_supply_pins(s, m.inst) for m in d.macros}
    if supply:
        ctx.outputs["macro_supply_pins"] = supply
        ctx.metrics["macro_supply_pins_unconnected"] = sum(len(v["unconnected"]) for v in supply.values())
    if ctx.pdk.endcap_cell and ctx.metrics["endcaps"] != 2 * ctx.metrics["rows"]:
        raise RuntimeError(f"{ctx.metrics['endcaps']} endcaps for {ctx.metrics['rows']} rows; "
                           "expected one on the left and one on the right of each row")
