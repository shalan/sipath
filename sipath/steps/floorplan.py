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
        s.run(tcl.floorplan_from_def(ctx.pdk, d.floorplan_def))
        if d.harness:  # the frame's user-supply pins (the netlist has no power ports)
            from .. import harness
            h = harness.get(d.harness, d.harness_options)
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
    A design's own straps are used as given (pdngen reports a misfit)."""
    from ..design import Strap
    d, s = ctx.design, ctx.session
    if not d.pdn.straps_from_pdk:
        return list(d.pdn.straps)
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
