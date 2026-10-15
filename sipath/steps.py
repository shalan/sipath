"""Step functions for ``Flow.step``. Each takes a ``Ctx`` and drives the
attempt's OpenROAD session. Nothing here fabricates a number: metrics are
read from the session or from files that this process wrote."""
from __future__ import annotations

import json
import os
import re

from . import analog, datapath, params, tcl
from .session import TclError
from .tcl import q, qe, qlist, tcl_parse, tcl_words  # noqa: F401


# --------------------------------------------------------------------- helpers
def _drt_numbers(transcript: str) -> dict:
    out = {}
    v = re.findall(r"Number of violations = (\d+)", transcript)
    if v:
        out["drc"] = int(v[-1])
    w = re.findall(r"Total wire length = (\d+) um", transcript)
    if w:
        out["wirelength_um"] = int(w[-1])
    vias = re.findall(r"Total number of vias = (\d+)", transcript)
    if vias:
        out["vias"] = int(vias[-1])
    return out


def _orient_xy(o, x, y, X, Y, W, H):
    """Master coords -> design coords (DEF orientation, (X, Y) = placed bbox lower-left)."""
    return {"R0": (X + x, Y + y), "R90": (X + H - y, Y + x), "R180": (X + W - x, Y + H - y),
            "R270": (X + y, Y + W - x), "MX": (X + x, Y + H - y), "MY": (X + W - x, Y + y),
            "MXR90": (X + y, Y + x), "MYR90": (X + H - y, Y + W - x)}[o]


def macro_supply_pins(session, inst) -> dict:
    """Which supply pins of a macro touch a special (PDN) shape of their net
    on the same layer. Pin shapes are transformed from master coordinates."""
    from .tcl import tcl_parse
    o, bx0, by0, bx1, by1, W, H, pins, nets = tcl_parse(session.value(f"::flow_supply_pin_data {q(inst)}"))
    bx0, by0, bx1, by1, W, H = map(int, (bx0, by0, bx1, by1, W, H))
    nets = dict(zip(nets[0::2], nets[1::2]))
    out = {"connected": [], "unconnected": []}
    for pin, net, shapes in pins:
        hit = False
        for layer, *xy in shapes:
            x0, y0, x1, y1 = map(int, xy)
            (ax, ay), (cx, cy) = _orient_xy(o, x0, y0, bx0, by0, W, H), _orient_xy(o, x1, y1, bx0, by0, W, H)
            r = (min(ax, cx), min(ay, cy), max(ax, cx), max(ay, cy))
            if not (bx0 <= r[0] and r[2] <= bx1 and by0 <= r[1] and r[3] <= by1):
                raise RuntimeError(f"{inst}/{pin}: transformed pin shape {r} outside instance bbox")
            for bl, *bxy in nets.get(net, []):
                b = tuple(map(int, bxy))
                if bl == layer and b[0] < r[2] and b[2] > r[0] and b[1] < r[3] and b[3] > r[1]:
                    hit = True
        out["connected" if hit else "unconnected"].append(f"{pin}->{net}")
    return {k: sorted(v) for k, v in out.items()}


def grid_nets(d) -> set[str]:
    return {d.net("VDD"), d.net("VSS")}  # the voltage domain pdn() builds


def _via_array(ctx) -> dict:
    """Via array pitch and inset (DBU) for drawn supply straps, from the PDK."""
    dbu = int(ctx.session.value("[ord::get_db_block] getDbUnitsPerMicron"))
    pd = ctx.pdk.pdn_defaults
    return {"via_pitch": int(round(pd.get("via_pitch", 1.6) * dbu)),
            "via_inset": int(round(pd.get("via_inset", 0.8) * dbu))}


def connect_supplies(ctx):
    """Strap supply tabs pdngen cannot reach to the grid: the supply pads'
    core-side tabs (to the core ring) and macro supply pins left unconnected."""
    from . import chip, powerconnect
    d, s = ctx.design, ctx.session
    die = tuple(map(int, tcl_words(s.value("set b [[ord::get_db_block] getDieArea]; "
                                           "list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))))
    layers = list(ctx.pdk.layers)
    vias = _via_array(ctx)
    done = []
    if d.mode == "die" and d.pdn.rings and d.pdn.ring_connect_to_pads:
        for inst, pin, net in chip.supply_pads(ctx):
            if net in grid_nets(d):
                done += [dict(r, inst=inst) for r in powerconnect.connect(s, inst, f"^{pin}$", layers, die, **vias)]
    for m in d.macros:
        rep = macro_supply_pins(s, m.inst)
        for item in rep["unconnected"]:
            pin, net = item.split("->")
            if net in grid_nets(d):
                done += [dict(r, inst=m.inst) for r in powerconnect.connect(s, m.inst, f"^{pin}$", layers, die, **vias)]
    ctx.outputs["power_connections"] = done
    ctx.metrics["power_straps"] = sum(1 for r in done if r["ok"])
    failed = [f"{r['inst']}/{r['pin']}" for r in done if not r["ok"]]
    if failed:
        ctx.outputs["power_connections_failed"] = failed
    _no_supply_shorts(ctx, layers)


def _no_supply_shorts(ctx, layers):
    """Fail on special wiring of two nets that overlaps or touches (a short
    that DRC does not see and only LVS would)."""
    from . import powerconnect
    found = powerconnect.shorts(ctx.session, layers)
    ctx.metrics["special_shorts"] = len(found)
    if found:
        raise RuntimeError(f"special wiring shorts {len(found)} net pair(s), e.g. {found[:3]}")


def power_checks(ctx) -> dict:
    """check_power_grid and IR drop for each grid net. With supply terminals
    (die) the grid must reach them; otherwise connectivity is checked without
    terminals and IR uses the top straps as sources."""
    s, out = ctx.session, {}
    for net in sorted(grid_nets(ctx.design)) + list(ctx.design.analog_supplies):
        has_terms = s.value(f"llength [[::flow_net {q(net)}] getBTerms]") != "0"
        r = s.run(f"check_power_grid -net {net}" + ("" if has_terms else " -dont_require_terminals"), check=False)
        unconnected_inst = re.findall(r"Unconnected instance (\S+)", r.output)
        out[net] = {"connected": r.rc == 0, "unconnected_instances": sorted(set(i.split("/")[0] for i in unconnected_inst)),
                    "unconnected_shapes": len(re.findall(r"PSM-0038", r.output))}
        v = ctx.pdk.voltages
        volt = 0.0 if net.upper().startswith("VSS") else (v["analog"] if net in ctx.design.analog_supplies else v["core"])
        src = "" if has_terms else " -source_type STRAPS"
        r = s.run(f"set_pdnsim_net_voltage -net {net} -voltage {volt}\nanalyze_power_grid -net {net}{src}", check=False)
        worst = re.findall(r"Worstcase IR drop: ([0-9.eE+-]+) V", r.output)
        avg = re.findall(r"Average IR drop  : ([0-9.eE+-]+) V", r.output)
        out[net]["ir_worst_mV"] = round(float(worst[-1]) * 1000, 4) if worst else None
        out[net]["ir_avg_mV"] = round(float(avg[-1]) * 1000, 4) if avg else None
        if r.rc:
            out[net]["ir_error"] = r.value
    ctx.outputs["power_grid"] = out
    ctx.metrics["grid_connected"] = all(v["connected"] for v in out.values())
    worst = [v["ir_worst_mV"] for v in out.values() if v["ir_worst_mV"] is not None]
    ctx.metrics["ir_worst_mV"] = max(worst) if worst else None
    return out


def _snap_box(session, box_um):
    """Snap a box (microns) to rows vertically and to the site grid horizontally."""
    rows = datapath.query_rows(session)
    dbu = datapath.dbu_per_micron(session)
    x0, y0, x1, y1 = (int(v * dbu) for v in box_um)
    r = rows[0]
    sx0 = r.x + ((x0 - r.x + r.site_w - 1) // r.site_w) * r.site_w
    sx1 = r.x + ((x1 - r.x) // r.site_w) * r.site_w
    ys = [row.y for row in rows]
    top = [row.y + row.height for row in rows]
    sy0 = min((y for y in ys if y >= y0), default=ys[-1])
    sy1 = max((t for t in top if t <= y1), default=top[0])
    if sx1 <= sx0 or sy1 <= sy0:
        raise ValueError(f"box {box_um} has no whole rows/sites")
    return sx0, sy0, sx1, sy1


def fence_violations(session, names) -> list[str]:
    tcl_ = f"""
set bad {{}}
set block [ord::get_db_block]
foreach rn {qlist(names)} {{
  set r [$block findRegion $rn]
  set bs [$r getBoundaries]
  foreach inst [$r getRegionInsts] {{
    set ib [$inst getBBox]; set inside 0
    foreach b $bs {{ if {{[$ib xMin] >= [$b xMin] && [$ib xMax] <= [$b xMax] && [$ib yMin] >= [$b yMin] && [$ib yMax] <= [$b yMax]}} {{ set inside 1 }} }}
    if {{!$inside}} {{ lappend bad "[$inst getName] outside $rn" }}
  }}
  foreach inst [$block getInsts] {{
    if {{[$inst getRegion] ne "NULL"}} continue
    if {{[[$inst getMaster] getType] ne "CORE"}} continue
    set ib [$inst getBBox]
    foreach b $bs {{ if {{[$ib xMin] < [$b xMax] && [$ib xMax] > [$b xMin] && [$ib yMin] < [$b yMax] && [$ib yMax] > [$b yMin]}} {{ lappend bad "[$inst getName] intrudes $rn" }} }}
  }}
}}
join $bad \\n
"""
    out = session.value(tcl_)
    return [line for line in out.splitlines() if line.strip()]


def enforce_fences(session, names, rounds: int = 4) -> dict:
    """Keep fence regions (dbRegion EXCLUSIVE) exclusive.

    OpenROAD keeps members inside a fence but does not keep later-inserted
    cells (repair buffers, clock buffers) out. A non-member found inside a
    fence joins it when it drives a member (it serves the fence); otherwise
    it is moved just outside and the design is re-legalized."""
    stats = {"adopted": 0, "evicted": 0}
    if not names:
        return stats
    for rnd in range(rounds):
        res = session.value(f"""
set round {rnd}
set adopted 0; set moved 0
set block [ord::get_db_block]
set core [$block getCoreArea]
foreach rn {qlist(names)} {{
  set r [$block findRegion $rn]
  set grp [lindex [$r getGroups] 0]
  foreach b [$r getBoundaries] {{
    foreach inst [$block getInsts] {{
      if {{[$inst getRegion] ne "NULL" || [[$inst getMaster] getType] ne "CORE" || [$inst isFixed]}} continue
      set ib [$inst getBBox]
      if {{!([$ib xMin] < [$b xMax] && [$ib xMax] > [$b xMin] && [$ib yMin] < [$b yMax] && [$ib yMax] > [$b yMin])}} continue
      set serves 0
      foreach it [$inst getITerms] {{
        if {{![$it isOutputSignal]}} continue
        set net [$it getNet]; if {{$net eq "NULL"}} continue
        foreach ld [$net getITerms] {{ if {{[[$ld getInst] getRegion] eq $r}} {{ set serves 1 }} }}
      }}
      if {{$serves}} {{
        $r addInst $inst; if {{$grp ne ""}} {{ $grp addInst $inst }}; incr adopted; continue
      }}
      set w [$ib getDX]; set h [$ib getDY]
      set dl [expr {{[$ib xMax] - [$b xMin]}}]; set dr [expr {{[$b xMax] - [$ib xMin]}}]
      set left [expr {{[$b xMin] - $w >= [$core xMin]}}]; set right [expr {{[$b xMax] + $w <= [$core xMax]}}]
      set nx [$ib xMin]; set ny [$ib yMin]
      if {{$round == 0}} {{
        if {{($dl <= $dr && $left) || !$right}} {{ set nx [expr {{[$b xMin] - $w}}] }} else {{ set nx [$b xMax] }}
      }} elseif {{$round == 1}} {{
        if {{($dl > $dr && $left) || !$right}} {{ set nx [expr {{[$b xMin] - $w}}] }} else {{ set nx [$b xMax] }}
      }} elseif {{[$b yMin] - $h >= [$core yMin]}} {{
        set ny [expr {{[$b yMin] - $h}}]
      }} else {{ set ny [$b yMax] }}
      $inst setLocation $nx $ny
      incr moved
    }}
  }}
}}
list $adopted $moved""").split()
        stats["adopted"] += int(res[0])
        stats["evicted"] += int(res[1])
        if res == ["0", "0"]:
            break
        session.run("detailed_placement")
    return stats


def compact_cluster(session, g, avoid=()):
    """Pull a cluster's cells into a packed block at their centroid."""
    flat = session.value(f"""
set out {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[string first {q(g.prefix)} [$inst getName]] == 0}} {{
    set l [$inst getLocation]; set m [$inst getMaster]
    lappend out [$inst getName] [lindex $l 0] [lindex $l 1] [$m getWidth] [$m getHeight]
  }}
}}
set out""").split()
    cells, sizes, cx, cy = [], {}, 0, 0
    for i in range(0, len(flat), 5):
        n, x, y, w, h = flat[i], *map(int, flat[i + 1:i + 5])
        cells.append(n)
        sizes[n] = (w, h)
        cx += x + w / 2
        cy += y + h / 2
    if not cells:
        raise ValueError(f"cluster {g.name}: no cells")
    rows = datapath.query_rows(session)
    center = [cx / len(cells), cy / len(cells)]
    pl = datapath.pack_block(cells, center, rows, sizes, g.utilization, avoid=list(avoid))
    datapath.apply(session, pl, status="PLACED")
    xs = [p.x for p in pl] + [p.x + p.w for p in pl]
    ys = [p.y for p in pl] + [p.y + p.h for p in pl]
    return {"cells": len(cells), "bbox_um": [v / datapath.dbu_per_micron(session) for v in
                                             (min(xs), min(ys), max(xs), max(ys))]}


def cluster_spread(session, g) -> float:
    """Bounding-box half perimeter (microns) of a cluster's cells."""
    v = session.value(f"""
set xs {{}}; set ys {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[string first {q(g.prefix)} [$inst getName]] == 0}} {{ set b [$inst getBBox]; lappend xs [$b xMin] [$b xMax]; lappend ys [$b yMin] [$b yMax] }}
}}
set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
expr {{double([lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]) / [[ord::get_db_block] getDbUnitsPerMicron]}}""")
    return float(v)


def _dp_tap(ctx):
    """(well-tap master, tap distance um) for datapath tap slots, or None."""
    p = ctx.pdk
    return (p.tap_cell, p.tap_distance) if p.tap_cell and p.tap_distance else None


def reabut_all(ctx) -> dict:
    out = {}
    for dp in ctx.design.datapaths:
        out[dp.name] = datapath.reabut(ctx.session, dp, _dp_tap(ctx))
    return out


def datapath_check(ctx, when: str = "") -> int:
    """Every datapath structure still sits exactly where its packing with the
    current masters puts it (rows abutted, stage columns aligned). Called after
    each resize + legalize; a broken structure fails the attempt."""
    bad = {}
    for dp in ctx.design.datapaths:
        problems = datapath.check_structure(ctx.session, dp, _dp_tap(ctx))
        if problems:
            bad[dp.name] = problems[:10]
    if bad:
        raise RuntimeError(f"datapath structure broken{' ' + when if when else ''}: {bad}")
    ctx.metrics["datapaths_intact"] = len(ctx.design.datapaths)
    return len(ctx.design.datapaths)


def clock_nets(ctx) -> list[str]:
    nets = list(ctx.design.clock_nets)
    for p in ([] if nets else ctx.design.clock_ports):
        n = ctx.session.value(f"set b [[ord::get_db_block] findBTerm {q(p)}]; "
                              f"if {{$b eq \"NULL\"}} {{ error {q('no clock port ' + p)} }}; [$b getNet] getName")
        nets.append(n)
    for n in nets:
        ctx.session.run(f"::flow_net {q(n)}")
    bad = set(nets) & set(ctx.design.analog_nets)
    if bad:
        raise ValueError(f"analog nets cannot be clock nets: {sorted(bad)}")
    return nets


# ----------------------------------------------------------------------- steps
def floorplan(ctx):
    """Soft macro: floorplan from the given DEF. Die: die box + pad ring."""
    d, s = ctx.design, ctx.session
    if d.mode == "macro" and d.floorplan_def:
        s.run(tcl.floorplan_from_def(ctx.pdk, d.floorplan_def))
        if d.harness:  # the frame's user-supply pins (the netlist has no power ports)
            from . import harness
            h = harness.get(d.harness, d.harness_options)
            s.run(h.create_power_pins_tcl())
            ctx.metrics["harness_pins_clipped"] = int(s.value(h.clip_pins_tcl()))  # restored at signoff
            ctx.outputs["harness"] = d.harness
    elif d.mode == "macro":  # die box or utilization; pins at random until placement places them
        s.run(tcl.floorplan_die(ctx.pdk, d))
        s.run(tcl.place_pins(ctx.pdk, random=True))
        ctx.outputs["auto_pins"] = True
    else:
        from . import chip
        s.run(tcl.floorplan_die(ctx.pdk, d))
        ctx.outputs["pad_ring"] = chip.pad_ring(ctx)
    ctx.metrics["rows"] = int(s.value("llength [[ord::get_db_block] getRows]"))


def macros(ctx):
    """Hard macros: frozen at a location or autoplaced (params: seed, samples)."""
    from . import chip
    ctx.outputs["macros"] = chip.place_macros(ctx)


def fit_straps(ctx) -> list:
    """The design's core straps; PDK-default straps that do not fit the core
    (a VDD/VSS pair needs offset + pitch/2 + width) are scaled to it: offset
    10 % and pitch 80 % of the core across the straps, recorded in outputs.
    A design's own straps are used as given (pdngen reports a misfit)."""
    from .design import Strap
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
        from . import powerconnect
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
    s.run(tcl.global_place(p.get("density", 0.55), p.get("seed"), timing_driven=p.get("timing_driven", True),
                           keep_resize=not fence_groups, routability_driven=p.get("routability_driven", False)))
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


def check_placement(ctx_or_session, design=None):
    """``check_placement`` with one precise exception: DPL's blocked-layer
    check may flag a hard macro that is an endpoint of a frozen analog net
    (the analog wire must land on the macro's own pin). Any standard cell
    flagged, or any other placement violation, fails."""
    s = getattr(ctx_or_session, "session", ctx_or_session)
    d = design or getattr(ctx_or_session, "design", None)
    r = s.run("check_placement -verbose", check=False)
    if r.rc == 0:
        return []
    failed = re.findall(r"\[WARNING (DPL-\d+)\] (.+?) check failed \((\d+)\)", r.output)
    other = [f for f in failed if f[0] != "DPL-0010"]
    if other or not failed or d is None or not d.analog_nets:
        raise RuntimeError(f"check_placement failed: {r.value}\n" + "\n".join(r.output.strip().splitlines()[-15:]))
    after = r.output.split("Blocked layers check failed", 1)[1].splitlines()[1:]
    flagged = [ln.strip() for ln in after if ln.startswith(" ") and ln.strip()]
    allowed = set(tcl_words(s.value(f"""
set o {{}}
foreach n {qlist(d.analog_nets)} {{
  foreach t [[::flow_net $n] getITerms] {{ if {{[[[$t getInst] getMaster] isBlock]}} {{ lappend o [[$t getInst] getName] }} }}
}}
lsort -unique $o""")))
    bad = [f for f in flagged if f not in allowed]
    if bad or not flagged:
        raise RuntimeError(f"check_placement: blocked layers on {bad or 'unparsed instances'}")
    return flagged


def tie_max_fanout(ctx) -> int:
    ties = sorted({m for m, _ in ctx.pdk.tie_cells()})
    return int(ctx.session.value(f"""
set worst 0
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[[$inst getMaster] getName] ni {qlist(ties)}}} continue
  foreach it [$inst getITerms] {{
    if {{![$it isOutputSignal]}} continue
    set n [$it getNet]; if {{$n eq "NULL"}} continue
    set f [expr {{[llength [$n getITerms]] - 1 + [llength [$n getBTerms]]}}]
    if {{$f > $worst}} {{ set worst $f }}
  }}
}}
set worst"""))


def reports(ctx, tag: str) -> dict:
    """Timing (setup/hold paths per corner), clock skew and power reports for
    this attempt, written to files next to its checkpoint."""
    s = ctx.session
    files = {}
    corners = [c.name for c in ctx.pdk.analysis_corners()]
    power = ("report_power" if len(corners) == 1 else
             "; ".join(f"puts {{== corner {c}}}; report_power -scene {c}" for c in corners))
    for name, cmd in (("timing_setup", "report_checks -path_delay max -group_path_count 5 -format full_clock"),
                      ("timing_hold", "report_checks -path_delay min -group_path_count 5 -format full_clock"),
                      ("clock_skew", "report_clock_skew"),
                      ("power", power),
                      ("design_rules", "report_check_types -max_slew -max_capacitance -max_fanout -violators")):
        f = ctx.path(f"{tag}_{name}.rpt")
        r = s.run(f"sta::redirect_file_begin {q(f)}\n"
                  f"set ::__rc [catch {{{cmd}}} ::__msg]\n"
                  "sta::redirect_file_end\n"
                  "if {$::__rc} { error $::__msg }", check=False)
        if r.rc == 0 and os.path.isfile(f):
            files[name] = f
        else:
            with open(f, "w") as fh:
                fh.write(f"{cmd} failed: {r.value}\n")
            files[name] = f
    ctx.outputs.setdefault("reports", {}).update({f"{tag}_{k}": v for k, v in files.items()})
    ctx.metrics.update(design_rule_counts(ctx, files["design_rules"]))
    return files


def design_rule_counts(ctx, report: str) -> dict:
    """Slew / capacitance / fanout violators from ``report_check_types
    -violators``. The PDK limits are standard-cell rules: violators on
    standard-cell pins are counted (``drv_*``); those on IO-ring cells, hard
    macros and top-level ports (bond pads, analog buses) are counted apart in
    ``drv_excluded`` and never dropped silently."""
    kinds = {"max slew": "slew", "max capacitance": "cap", "max fanout": "fanout"}
    viol, kind = [], None
    with open(report) as fh:
        for line in fh:
            s_ = line.strip()
            if s_ in kinds:
                kind = kinds[s_]
            elif kind and s_.endswith("(VIOLATED)"):
                viol.append((kind, s_.split()[0]))
    insts = sorted({p.rsplit("/", 1)[0] for _, p in viol if "/" in p})
    types = {}
    if insts:
        flat = tcl_words(ctx.session.value(
            f"set o {{}}; foreach n {qlist(insts)} {{ set i [[ord::get_db_block] findInst $n]; "
            "lappend o $n [expr {$i eq \"NULL\" ? \"UNKNOWN\" : [[$i getMaster] getType]}] }; set o"))
        types = dict(zip(flat[0::2], flat[1::2]))
    out = {"drv_slew": 0, "drv_cap": 0, "drv_fanout": 0, "drv_excluded": 0}
    excluded = {}
    for k, p in viol:
        t_ = types.get(p.rsplit("/", 1)[0], "PORT") if "/" in p else "PORT"
        if t_.startswith("CORE"):
            out[f"drv_{k}"] += 1
        else:
            out["drv_excluded"] += 1
            excluded[t_] = excluded.get(t_, 0) + 1
    out["drv_clean"] = not (out["drv_slew"] or out["drv_cap"] or out["drv_fanout"])
    if excluded:
        ctx.outputs["drv_excluded_by_type"] = excluded
    return out


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


ROW_BLOCKAGES = r"""
set block [ord::get_db_block]; set tech [ord::get_db_tech]
# merge vertically adjacent row segments with the same x extent into one box
set boxes {}
foreach row [$block getRows] {
  set b [$row getBBox]
  lappend boxes [list [$b xMin] [$b xMax] [$b yMin] [$b yMax]]
}
set boxes [lsort -index 2 -integer [lsort -index 0 -integer $boxes]]
set merged {}
foreach bx $boxes {
  lassign $bx x0 x1 y0 y1
  set hit 0
  for {set k 0} {$k < [llength $merged]} {incr k} {
    lassign [lindex $merged $k] mx0 mx1 my0 my1
    if {$mx0 == $x0 && $mx1 == $x1 && $my1 == $y0} { lset merged $k [list $mx0 $mx1 $my0 $y1]; set hit 1; break }
  }
  if {!$hit} { lappend merged $bx }
}
set ::flow_row_obs {}
foreach lname {@LAYERS@} {
  set l [$tech findLayer $lname]
  foreach m $merged { lassign $m x0 x1 y0 y1; lappend ::flow_row_obs [odb::dbObstruction_create $block $l $x0 $y0 $x1 $y1] }
}
llength $::flow_row_obs
"""


SUPPLY_PARK = r"""
# route an analog supply like a signal: IO-ring bus pins (connected by
# abutment, not by routing) move to a scratch net, the net becomes a regular
# signal net with a wide NDR; ::flow_park keeps the moved pins
set net [::flow_net @NET@]
set park [odb::dbNet_create [ord::get_db_block] "@NET@__ring"]
$park setSigType [[::flow_net @NET@] getSigType]; $park setSpecial  ;# routers ignore it
set ::flow_park(@NET@) {}
foreach it [$net getITerms] {
  set inst [$it getInst]; set t [[$inst getMaster] getType]
  if {[string match PAD* $t] || [string match ENDCAP* $t]} {
    $it disconnect; $it connect $park; lappend ::flow_park(@NET@) $it
  }
}
# the supply's own top-level terminal (on the pad's bond pin) is parked too
set ::flow_park_bt(@NET@) {}
foreach bt [$net getBTerms] { $bt connect $park; lappend ::flow_park_bt(@NET@) $bt }
set ::flow_supply_sig(@NET@) [$net getSigType]
$net clearSpecial
$net setSigType SIGNAL
# the detailed router routes only signal nets and refuses a signal net on a
# POWER pin: the remaining (macro) pins are typed SIGNAL for this route only
set ::flow_mterm_sig(@NET@) {}
foreach it [$net getITerms] {
  set mt [$it getMTerm]
  lappend ::flow_mterm_sig(@NET@) $mt [$mt getSigType]
  $mt setSigType SIGNAL
}
assign_ndr -ndr ASUPPLY -net @NET@
llength [$net getITerms]
"""
def _launch_pin(ctx, net, inst, pin):
    """A temporary top-level pin (3x3 um, on the PDK's launch layer) a few
    microns inside the core side of the pad's largest core-side tab on the
    layer below: the router's endpoint for the supply. Returns the box (DBU)."""
    from . import powerconnect
    s = ctx.session
    s.run(powerconnect.DUMP)
    o, bx0, by0, bx1, by1, W, H, pins, obs = tcl.tcl_parse(s.value(f"::flow_pc_dump {q(inst)} {q('^' + pin + '$')}"))
    bx0, by0, bx1, by1, W, H = map(int, (bx0, by0, bx1, by1, W, H))
    edge = next(p.edge for p in ctx.design.pads if p.inst == inst)
    launch = ctx.pdk.analog_defaults.get("launch_layer") or ctx.pdk.layers[-2]
    tab_layer = ctx.pdk.layers[ctx.pdk.layer_index(launch) - 1]
    tabs = []
    for name, n, shapes in pins:
        for ly, *r in shapes:
            if ly == tab_layer:
                tabs.append(powerconnect._rect(o, bx0, by0, W, H, *map(int, r)))
    inner = {"south": lambda t: t[3] >= by1 - 15000, "north": lambda t: t[1] <= by0 + 15000,
             "west": lambda t: t[2] >= bx1 - 15000, "east": lambda t: t[0] <= bx0 + 15000}[edge]
    tabs = [t for t in tabs if inner(t)]
    if not tabs:
        raise RuntimeError(f"{inst}/{pin}: no core-side {tab_layer} tab")
    t = max(tabs, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    half, off = 1500, 5000
    if edge in ("south", "north"):
        cx = (t[0] + t[2]) // 2
        cy = by1 + off if edge == "south" else by0 - off
    else:
        cy = (t[1] + t[3]) // 2
        cx = bx1 + off if edge == "west" else bx0 - off
    box = (cx - half, cy - half, cx + half, cy + half)
    s.run(f"set bt [odb::dbBTerm_create [::flow_net {q(net)}] {q(net + '__launch')}]\n"
          "$bt setIoType INOUT\nset bp [odb::dbBPin_create $bt]\n"
          f"odb::dbBox_create $bp [[ord::get_db_tech] findLayer {launch}] {box[0]} {box[1]} {box[2]} {box[3]}\n"
          "$bp setPlacementStatus FIRM")
    return list(box)


SUPPLY_UNPARK = r"""
set net [::flow_net @NET@]
foreach {mt st} $::flow_mterm_sig(@NET@) { $mt setSigType $st }
$net setSigType $::flow_supply_sig(@NET@)
foreach it $::flow_park(@NET@) { $it disconnect; $it connect $net }
foreach bt $::flow_park_bt(@NET@) { $bt connect $net }
odb::dbNet_destroy [::flow_net "@NET@__ring"]
llength [$net getITerms]
"""


def route_analog(ctx):
    d, s = ctx.design, ctx.session
    if not d.analog_nets and not d.analog_supplies:
        return
    s.run(tcl.analog_ndr(d))
    s.run(tcl.routing_layers(d, ctx.pdk))
    supplies = list(d.analog_supplies)
    if supplies:
        w = " ".join(f"{k} {v}" for k, v in d.analog_supply_ndr["width"].items())
        sp = " ".join(f"{k} {v}" for k, v in d.analog_supply_ndr["spacing"].items())
        s.run(f"create_ndr -name ASUPPLY -width {{{w}}} -spacing {{{sp}}}")
        from . import chip
        pads = {net: (inst, pin) for inst, pin, net in chip.supply_pads(ctx)}
        ctx.outputs["analog_supply_pins"] = {}
        for net in supplies:
            if net not in pads:
                raise RuntimeError(f"analog supply {net} has no supply pad")
            # every IO-cell pin (pad included) is parked: the router ends at a
            # launch pin in front of the pad's core-side tab instead
            n = s.value(SUPPLY_PARK.replace("@NET@", q(net)).replace("@KEEP@", "{}"))
            ctx.outputs["analog_supply_pins"][net] = int(n)
            ctx.outputs.setdefault("analog_supply_launch", {})[net] = _launch_pin(ctx, net, *pads[net])
    routed = list(d.analog_nets) + supplies
    # temporary routing blockages over the rows on the pin-access layers: analog
    # wires go over standard cells only on higher layers
    nobs = int(s.value(ROW_BLOCKAGES.replace("@LAYERS@", " ".join(d.analog_row_blocked_layers))))
    ctx.outputs["analog_row_blockages"] = nobs
    nums = {}
    try:
        passes = []
        if d.analog_nets:
            passes.append((list(d.analog_nets), None, "analog"))
        if supplies:  # supplies on the PDK's wide upper layers only (no thin low-metal runs)
            passes.append((supplies, d.analog_supply_layers, "supply"))
        for nets, layers, tag in passes:
            if layers:
                s.run(f"set_routing_layers -signal {layers[0]}-{layers[1]} -clock {layers[0]}-{layers[1]}")
            r = s.run(tcl.route_subset(nets, ctx.path(f"{tag}_drc.rpt"), ctx.params.get("iters", 20),
                                       ctx.path(f"{tag}_congestion.rpt")))
            n = _drt_numbers(r.output)
            if "drc" not in n:
                raise RuntimeError(f"{tag} detailed_route reported no violation count")
            if n["drc"] != 0:
                raise RuntimeError(f"{tag} routing left {n['drc']} DRC violations")
            nums[tag] = n
    finally:
        s.run("foreach o $::flow_row_obs { odb::dbObstruction_destroy $o }; set ::flow_row_obs {}")
    s.run(tcl.routing_layers(d, ctx.pdk))
    nums = nums.get("analog", nums.get("supply", {}))
    for net in supplies:  # launch pin gone; back to a power net with all its pins
        s.run(f"odb::dbBTerm_destroy [[ord::get_db_block] findBTerm {q(net + '__launch')}]")
        s.run(SUPPLY_UNPARK.replace("@NET@", q(net)))
    ctx.outputs["analog_shapes"] = analog.freeze_nets(s, routed, ctx.path("analog_routed.def"))
    ctx.outputs["analog_def"] = ctx.path("analog_routed.def")  # analog nets restored from this before RCX
    if supplies:  # strap each supply pad's tab to its routed wire
        from . import powerconnect
        die = tuple(map(int, tcl_words(s.value("set b [[ord::get_db_block] getDieArea]; "
                                               "list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))))
        layers = list(ctx.pdk.layers)
        vias = _via_array(ctx)
        recs = []
        for net in supplies:
            inst, pin = pads[net]
            recs += [dict(r, inst=inst) for r in powerconnect.connect(s, inst, f"^{pin}$", layers, die, **vias)]
        ctx.outputs["analog_supply_pad_straps"] = recs
        if not all(r["ok"] for r in recs):
            raise RuntimeError(f"could not strap analog supply pads to their wires: {recs}")
        _no_supply_shorts(ctx, layers)
    ctx.outputs["analog_signature"] = analog.signature(s, routed)
    if supplies:
        ctx.outputs["macro_supply_pins"] = {m.inst: macro_supply_pins(s, m.inst) for m in d.macros}
        ctx.metrics["macro_supply_pins_unconnected"] = sum(len(v["unconnected"])
                                                           for v in ctx.outputs["macro_supply_pins"].values())
    ctx.metrics["analog_wirelength_um"] = nums.get("wirelength_um")
    ctx.outputs["analog_macro_pin_overlaps"] = check_placement(ctx)  # analog wiring must not block any cell


def _global_route(ctx, script: str, report: str):
    """Run a global route; on congestion (GRT-0116) fail with the congestion
    report summarised (regions, overflow, worst box, nets) instead of a bare
    error. Records ``congested`` / ``grt_overflow`` when it succeeds."""
    from . import congestion
    rpt = ctx.path(report)
    try:
        ctx.session.run(script)
    except TclError as e:
        if "GRT-0116" not in str(e):
            raise
        c = congestion.parse(rpt)
        ctx.outputs.setdefault("reports", {})["congestion"] = rpt
        raise RuntimeError(f"global route left congestion: {congestion.describe(c)}; report {rpt}. "
                           "Lower the placement density, try routability-driven placement, or give the router "
                           "more layers / less layer adjustment") from e
    c = congestion.parse(rpt)  # written only when congestion is left
    if c["gcells"]:
        ctx.outputs.setdefault("reports", {})["congestion"] = rpt
    ctx.metrics["congested"] = bool(c["gcells"])
    ctx.metrics["grt_overflow"] = c["overflow"]


def routability_check(ctx, margin: float = 0.0) -> dict:
    """Global route of the placed design with ``-allow_congestion``: overflow
    as a metric (``grt_overflow``, ``grt_congested_gcells``), never a crash.
    ``margin`` reserves that share of every routing layer's capacity on top of
    the configured adjustments (head-room for the PDN, clock and ECOs). Guides
    are cleared afterwards; the checkpoint keeps only the placement."""
    from . import congestion
    s, d = ctx.session, ctx.design
    s.run(tcl.routing_layers(d, ctx.pdk, margin=margin))
    rpt = ctx.path("place_congestion.rpt")
    s.run(f"global_route -allow_congestion -congestion_iterations 30 -congestion_report_file {q(rpt)}\n"
          "foreach net [[ord::get_db_block] getNets] { $net clearGuides }", timeout=1800)
    c = congestion.parse(rpt)
    ctx.metrics["grt_overflow"] = c["overflow"]
    ctx.metrics["grt_congested_gcells"] = c["gcells"]
    if c["gcells"]:
        ctx.outputs.setdefault("reports", {})["place_congestion"] = rpt
    ctx.outputs["routability"] = {"margin": margin, **{k: c[k] for k in ("gcells", "overflow", "max", "worst", "nets")}}
    return c


def route_digital(ctx):
    """Global route -> timing repair on global-route parasitics -> global
    route again -> antenna repair -> detailed route -> antenna check -> RCX
    (all corners) -> reports.

    params: iters, max_drc, max_antenna (default 0), repair_after_grt (True),
    fail_on_violation. Post-route repair and fillers are the next step (route_repair)."""
    d, s, p = ctx.design, ctx.session, ctx.params
    frozen = list(d.analog_nets) + list(d.analog_supplies)
    before = analog.signature(s, frozen) if frozen else ""
    s.run(tcl.routing_layers(d, ctx.pdk))
    _global_route(ctx, tcl.global_route_digital(ctx.path("congestion.rpt")), "congestion.rpt")
    if p.get("repair_after_grt", True):
        # 1.1: repair with global-route parasitics, legalize, then let the global
        # router re-route only what changed (incremental mode)
        s.run("global_route -start_incremental\nestimate_parasitics -global_routing\n"
              f"repair_design{tcl.design_margins(ctx.pdk)}\n"
              "repair_timing -setup -hold -repair_tns 100" + tcl.margins(ctx.pdk, "hold_route"))
        ctx.outputs["reabut_grt"] = reabut_all(ctx)
        s.run("detailed_placement")
        fences = [g.name for g in d.groups if g.kind == "fence"]
        ctx.outputs["fence_evicted_grt"] = enforce_fences(s, fences)
        check_placement(ctx)
        bad = fence_violations(s, fences)
        if bad:
            raise RuntimeError("fence violations after post-global-route repair:\n" + "\n".join(bad[:20]))
        datapath_check(ctx, "after post-global-route repair")
        _global_route(ctx, f"global_route -end_incremental -congestion_report_file {q(ctx.path('congestion.rpt'))}",
                      "congestion.rpt")
    if ctx.pdk.diode_cell:  # 1.4: antenna repair on the global routes
        r = s.run(f"repair_antennas {q(ctx.pdk.diode_cell)} -iterations 3")
        m = re.findall(r"Inserted (\d+) diodes", r.output)
        ctx.metrics["antenna_diodes"] = int(m[-1]) if m else 0
        s.run("detailed_placement")
        check_placement(ctx)
    r = s.run(tcl.detailed_route_only(ctx.path("drc.rpt"), p.get("iters", 64)))
    after = analog.signature(s, frozen) if frozen else ""
    if before != after:
        raise RuntimeError("digital routing modified frozen analog nets")
    nums = _drt_numbers(r.output)
    if "drc" not in nums:
        raise RuntimeError("detailed_route reported no violation count")
    ctx.metrics.update(nums)
    if nums["drc"] > p.get("max_drc", 0):
        raise RuntimeError(f"{nums['drc']} DRC violations after detailed routing (see drc.rpt)")
    _check_unrouted(ctx)
    ctx.metrics["antenna_violations"] = check_antennas(ctx)
    rounds = antenna_loop(ctx, p.get("antenna_rounds", 3))
    ctx.metrics["antenna_rounds"] = rounds
    if frozen and analog.signature(s, frozen) != before:
        raise RuntimeError("antenna repair modified frozen analog nets")
    if ctx.metrics["antenna_violations"] > p.get("max_antenna", 0):
        raise RuntimeError(f"{ctx.metrics['antenna_violations']} antenna violations (see antenna.rpt)")
    min_area_repair(ctx)
    # rank route variants on extracted parasitics, not placement estimates
    run_rcx(ctx, ctx.path("route_rcx.spef"), tag="route_rcx")
    _timing_gate(ctx)
    reports(ctx, "route")
    reconnect_supplies(ctx)  # cells added by this step on the supply nets


def route_repair(ctx):
    """Post-route timing repair on extracted parasitics (1.2), in a fresh
    process: patch routes after many incremental global routes in one process
    can hang this OpenROAD build. Then fillers. params: rounds (2),
    max_patch_nets (40), fail_on_violation."""
    s, p = ctx.session, ctx.params
    for k in ("drc", "wirelength_um", "vias"):  # unchanged unless a patch route runs
        if k in (ctx.parent.metrics if ctx.parent else {}):
            ctx.metrics[k] = ctx.parent.metrics[k]
    s.run(tcl.routing_layers(ctx.design, ctx.pdk))
    run_rcx(ctx, ctx.path("route_rcx.spef"), tag="repair_rcx")
    from .eco import post_route_repair
    ctx.outputs["post_route_repair"] = post_route_repair(ctx, p.get("rounds", 2), p.get("max_patch_nets", 40))
    _check_unrouted(ctx)
    ctx.metrics["antenna_violations"] = check_antennas(ctx)
    d = ctx.design
    frozen = list(d.analog_nets) + list(d.analog_supplies)
    before = analog.signature(s, frozen) if frozen else ""
    ctx.metrics["antenna_rounds"] = antenna_loop(ctx, p.get("antenna_rounds", 3), "repair_antenna")
    if frozen and analog.signature(s, frozen) != before:
        raise RuntimeError("antenna repair modified frozen analog nets")
    if ctx.metrics["antenna_violations"] > p.get("max_antenna", 0):
        raise RuntimeError(f"{ctx.metrics['antenna_violations']} antenna violations after post-route repair")
    min_area_repair(ctx, "repair")
    s.run(tcl.filler(ctx.pdk, p.get("fillers")))
    check_placement(ctx)
    _timing_gate(ctx)
    reports(ctx, "repair")
    reconnect_supplies(ctx)  # cells added by this step on the supply nets


def fill(ctx):
    """Metal fill: OpenROAD density_fill with the PDK's fill rules
    (signoff.fill_rules), squares on each routing layer kept apart from the
    design's shapes; the signoff density check measures the result. Fill is
    floating metal in the DEF FILLS section, streamed to the PDK's fill
    datatypes. params: layers (default: those in the rules up to the design's
    top routing layer), area (um; default: the die)."""
    s, p = ctx.session, ctx.params
    rules = (ctx.pdk.signoff or {}).get("fill_rules")
    if not rules:
        raise ValueError(f"PDK {ctx.pdk.name} has no signoff.fill_rules: no metal fill")
    with open(rules) as f:
        data = json.load(f)
    if p.get("layers"):
        unknown = sorted(set(p["layers"]) - set(data["layers"]))
        if unknown:
            raise ValueError(f"fill.layers {unknown} not in the fill rules ({sorted(data['layers'])})")
        keep = set(p["layers"])
    else:  # up to the design's top routing layer: an embedded macro leaves the layers above to its parent
        stack = list(ctx.pdk.layers)
        top = ctx.design.max_route_layer
        keep = {ly for ly in data["layers"] if ly not in stack or top not in stack
                or stack.index(ly) <= stack.index(top)}
    if keep != set(data["layers"]):
        data["layers"] = {k: v for k, v in data["layers"].items() if k in keep}
        rules = ctx.path("fill_rules.json")
        with open(rules, "w") as f:
            json.dump(data, f, indent=1)
    area = f" -area {{{' '.join(f'{v:g}' for v in p['area'])}}}" if p.get("area") else ""
    s.run(f"density_fill -rules {q(rules)}{area}")
    per = tcl_parse(s.value("set c [dict create]; foreach f [[ord::get_db_block] getFills] "
                            "{ dict incr c [[$f getTechLayer] getName] }; set c"))
    counts = {per[i]: int(per[i + 1]) for i in range(0, len(per), 2)}
    ctx.outputs["fill"] = counts
    ctx.metrics["fill_shapes"] = sum(counts.values())
    if not counts:
        raise RuntimeError("density_fill inserted no fill")


def views(ctx):
    """Hardened-macro views of the signed-off design, in views/ (P2.1): the
    abstract LEF (write_abstract_lef), one timing Liberty per analysis corner
    from the extracted parasitics (write_timing_model), the GDS and the LVS
    subcircuit from signoff, the SPEF per corner, the netlist with and without
    supply pins, and macro.yaml: the entry a parent design lists under
    ``macros`` (inst and location left to fill in). Each view is checked
    against the design's ports before the step passes. params: corners
    (default: all analysis corners), bloat_occupied_layers (True)."""
    import shutil
    d, s, p = ctx.design, ctx.session, ctx.params
    so = ctx.parent_outputs("signoff")
    if not (so.get("gds") and os.path.isfile(so["gds"])):
        raise RuntimeError("views need a signed-off GDS (the parent signoff wrote none)")
    out = ctx.path("views")
    os.makedirs(out, exist_ok=True)
    top = d.top
    lef = os.path.join(out, f"{top}.lef")
    s.run(f"write_abstract_lef{' -bloat_occupied_layers' if p.get('bloat_occupied_layers', True) else ''} {q(lef)}")
    v, pv = os.path.join(out, f"{top}.v"), os.path.join(out, f"{top}.pwr.v")
    s.run(f"write_verilog {q(v)}\nwrite_verilog -include_pwr_gnd {q(pv)}")
    spefs = ctx.ancestor_output("spef_corners") or {}
    names = [c.name for c in ctx.pdk.analysis_corners()]
    corners = list(p.get("corners") or names)
    unknown = sorted(set(corners) - set(names))
    if unknown:
        raise ValueError(f"views.corners {unknown}: not analysis corners ({names})")
    missing = [c for c in corners if not spefs.get(c)]
    if missing:
        raise RuntimeError(f"no extracted SPEF for corners {missing}: the timing views need extract's parasitics")
    libs = {}
    multi = len(names) > 1
    for c in corners:
        s.run(f"read_spef{f' -corner {c}' if multi else ''} {q(spefs[c])}")
        libs[c] = os.path.join(out, f"{top}__{c}.lib")
        s.run(f"write_timing_model{f' -scene {c}' if multi else ''} -library_name {q(top + '__' + c)} "
              f"-cell_name {q(top)} {q(libs[c])}")
    gds = os.path.join(out, f"{top}.gds")
    shutil.copyfile(so["gds"], gds)
    spef_out = {}
    for c in corners:
        spef_out[c] = os.path.join(out, f"{top}__{c}.spef")
        shutil.copyfile(spefs[c], spef_out[c])
    cdl = None
    ref = os.path.join(os.path.dirname(so["signoff"]), f"{top}.cdl")
    if os.path.isfile(ref):  # the top subcircuit of signoff's LVS reference (cells come from the PDK)
        with open(ref) as f:
            text = f.read()
        m = re.search(r"^\.SUBCKT\s+" + re.escape(top) + r"\b.*?^\.ENDS\b.*?$", text, re.S | re.M | re.I)
        if m:
            cdl = os.path.join(out, f"{top}.cdl")
            with open(cdl, "w") as f:
                f.write(m.group(0) + "\n")
    # checks: every signal port in the LEF and in each Liberty; supply pins in the LEF
    ports = set(tcl_words(s.value("set o {}; foreach b [[ord::get_db_block] getBTerms] "
                                  "{ if {![[$b getNet] isSpecial]} { lappend o [$b getName] } }; set o")))
    with open(lef) as f:
        lef_text = f.read()
    lef_pins = set(re.findall(r"^\s*PIN\s+(\S+)", lef_text, re.M))
    # supply pins: the abstract makes them from the grid's special wires (USE POWER / GROUND)
    supplies = {m.group(1) for m in re.finditer(r"^\s*PIN\s+(\S+)\s*\n(?:(?!\s*END\b).*\n)*?\s*USE\s+(?:POWER|GROUND)\b",
                                                 lef_text, re.M)}
    bad = {"lef_missing_ports": sorted(ports - lef_pins)[:20],
           "lef_missing_supplies": sorted(grid_nets(d) - supplies)}
    for c, f in libs.items():
        with open(f) as fh:
            lib_pins = set(re.findall(r"^\s*pin\s*\(\s*\"?([^\")]+)\"?\s*\)", fh.read(), re.M))
        # a bus pin is written as a bus with its bits inside; plain names otherwise
        gone = sorted(x for x in ports - lib_pins if re.sub(r"\[\d+\]$", "", x) not in lib_pins
                      and x.replace("[", "\\[").replace("]", "\\]") not in lib_pins)
        if gone:
            bad[f"lib_{c}_missing_ports"] = gone[:20]
    bad = {k: x for k, x in bad.items() if x}
    if bad:
        raise RuntimeError(f"hardened views incomplete: {bad}")
    sup_map = {f"^{re.escape(n)}$": n for n in sorted(supplies)}
    rel = os.path.basename  # paths relative to macro.yaml: the directory can be copied as a whole
    entry = {"inst": "<instance name>", "lef": rel(lef), "gds": rel(gds), "lib": {c: rel(f) for c, f in libs.items()},
             "location": None, "supplies": sup_map, **({"cdl": rel(cdl)} if cdl else {"lvs": "blackbox"})}
    import json as _json
    with open(os.path.join(out, "macro.yaml"), "w") as f:
        f.write(f"# {top} hardened by SiPath ({ctx.dir}); a parent design lists it with\n"
                f"#   macros: [{{inst: <name>, views: <this directory>, location: [x, y]}}]\n")
        f.write(_json.dumps({"macros": [entry]}, indent=1) + "\n")  # JSON is YAML
    ctx.outputs["views"] = {"dir": out, "lef": lef, "gds": gds, "lib": libs, "spef": spef_out, "verilog": v,
                            "verilog_pwr": pv, "cdl": cdl, "macro_yaml": os.path.join(out, "macro.yaml"),
                            "supplies": sorted(supplies), "ports": len(ports)}
    ctx.metrics["view_corners"] = len(libs)


def integrate(ctx):
    """The hardened wrapper in its frame (H2): Magic puts the signed-off
    wrapper GDS (copied unchanged) into the harness's frame layout and writes
    the chip GDS, as the frame's own build does; KLayout then checks it (one
    top cell, the frame's die, the wrapper placed once at its origin and
    identical to the signed-off GDS). params: drc (full-chip KLayout DRC,
    default off: the frame's IO cells carry their own waivers). The seal ring
    is not added (the frame's build leaves it to tape-out)."""
    import json as _json
    from . import harness, signoff
    d = ctx.design
    if not d.harness:
        raise ValueError("integrate needs a harness design (harness: ...)")
    h = harness.get(d.harness, d.harness_options)
    f = h.spec.get("frame") or {}
    if not f.get("magic"):
        raise ValueError(f"harness {d.harness} gives no frame layout to integrate into (frame.magic)")
    wrapper_gds = ctx.ancestor_output("gds")
    if not (wrapper_gds and os.path.isfile(wrapper_gds)):
        raise RuntimeError("integrate needs the signed-off wrapper GDS (run signoff first)")
    work = ctx.path("frame_work")
    os.makedirs(work, exist_ok=True)
    ctx.outputs["frame_files"] = h.prepare_frame(work)
    top = f["magic"]["top"]
    chip = ctx.path(f"{top}.gds")
    rc = (ctx.pdk.signoff or {}).get("magic_rc")
    if rc:  # the rc file's cell libraries and their GDS live under the PDK root
        signoff._runner(ctx).mount(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(rc)))))
    signoff._runner(ctx).mount(os.path.dirname(wrapper_gds), work)
    out = signoff._magic(ctx, h.integrate_script(work, wrapper_gds, chip), "integrate")
    if "SIPATH_DONE" not in out or not os.path.isfile(chip) or os.path.getsize(chip) == 0:
        raise RuntimeError(f"Magic wrote no chip GDS (log {ctx.path('integrate.log')})")
    missing = sorted(set(re.findall(r"Cell (\S+) is unavailable", out)))
    if missing:
        raise RuntimeError(f"frame cells without a view: {missing[:10]} (log {ctx.path('integrate.log')})")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "klayout", "chipcheck.py")
    cfg = ctx.path("chipcheck.cfg.json")
    with open(cfg, "w") as fh:
        _json.dump({"chip": chip, "top": top, "die": f["die"], "wrapper": wrapper_gds,
                    "wrapper_cell": f["magic"]["wrapper_cell"], "origin": f["wrapper_origin"],
                    "out": ctx.path("chipcheck.json")}, fh, indent=1)
    signoff._runner(ctx).mount(script)
    signoff.run_tool(ctx, ["klayout", "-b", "-rd", f"cfg_file={cfg}", "-r", script], ctx.path("chipcheck.log"))
    with open(ctx.path("chipcheck.json")) as fh:
        check = _json.load(fh)
    ctx.outputs.update(checkpoint=chip, chip_gds=chip, chipcheck=check)
    ctx.metrics["chip_check_pass"] = check["pass"]
    if not check["pass"]:
        raise RuntimeError(f"assembled chip fails its checks: {check}")
    if ctx.params.get("drc"):
        r = signoff.drc(ctx, chip, top=top)
        ctx.outputs["chip_drc"] = r
        ctx.metrics["chip_drc"] = r["count"]


integrate.tool = True


def antenna_loop(ctx, max_rounds: int = 3, tag: str = "antenna") -> int:
    """Repair antennas on the detailed routes and re-route, until clean or
    ``max_rounds``. Updates metrics (drc, antenna_violations, antenna_diodes)."""
    s, p = ctx.session, ctx.params
    rounds = 0
    while ctx.pdk.diode_cell and ctx.metrics["antenna_violations"] and rounds < max_rounds:
        rounds += 1
        r = s.run(f"repair_antennas {q(ctx.pdk.diode_cell)} -iterations 1")
        m = re.findall(r"Inserted (\d+) diodes", r.output)
        ctx.metrics["antenna_diodes"] = ctx.metrics.get("antenna_diodes", 0) + (int(m[-1]) if m else 0)
        check_placement(ctx)
        from . import wires
        wires.normalize(s, ctx.path(f"{tag}{rounds}.def"))
        # route every digital net (a previous patch route may have narrowed the
        # router's net list, which would leave the new diode pins unconnected)
        r = s.run(tcl.DIGITAL_NETS + "::flow_route_nets $::flow_digital\n"
                  + tcl.detailed_route_only(ctx.path("drc.rpt"), p.get("iters", 64)), timeout=1800)
        nums = _drt_numbers(r.output)
        if "drc" not in nums:
            raise RuntimeError("detailed_route reported no violation count")
        ctx.metrics.update(nums)
        if nums["drc"] > p.get("max_drc", 0):
            raise RuntimeError(f"{nums['drc']} DRC violations after antenna re-route")
        _check_unrouted(ctx)
        ctx.metrics["antenna_violations"] = check_antennas(ctx)
    return rounds


def _check_unrouted(ctx):
    unrouted = ctx.session.value("""
set n 0
foreach net [[ord::get_db_block] getNets] {
  if {[$net isSpecial] || [$net getSigType] in {POWER GROUND}} continue
  if {[llength [$net getITerms]] + [llength [$net getBTerms]] < 2} continue
  if {[$net getWire] eq "NULL"} { incr n }
}
set n""")
    ctx.metrics["unrouted_nets"] = int(unrouted)
    if int(unrouted):
        raise RuntimeError(f"{unrouted} nets left unrouted")


def check_antennas(ctx) -> int:
    rpt = ctx.path("antenna.rpt")
    r = ctx.session.run(f"check_antennas -report_file {q(rpt)}")
    m = re.findall(r"Found (\d+) net violations", r.output)
    if not m:
        raise RuntimeError("check_antennas reported no violation count")
    ctx.outputs.setdefault("reports", {})["antenna"] = rpt
    return int(m[-1])


def _timing_gate(ctx):
    """Record whether setup and hold are met (with the parasitics loaded now);
    fail when params ``fail_on_violation`` is set."""
    m = tcl_words(ctx.session.value("list [sta::worst_slack -max] [sta::worst_slack -min]"))
    wns, whs = (float(v) if abs(float(v)) < 1e30 else None for v in m)
    met = (wns is None or wns >= 0) and (whs is None or whs >= 0)
    ctx.metrics["timing_met"] = met
    if not met and ctx.params.get("fail_on_violation"):
        raise RuntimeError(f"timing not met after extraction: setup {wns} ns, hold {whs} ns")


def extract(ctx):
    s = ctx.session
    nets = ctx.design.analog_nets
    if nets and analog.is_frozen(s, nets):
        # special nets are skipped by OpenRCX: restore their regular wiring first
        ctx.outputs["analog_restored"] = analog.thaw_nets(s, nets, ctx.ancestor_output("analog_def"),
                                                          ctx.path("analog_restored.def"))
    spef, deff = ctx.path(f"{ctx.design.name}.spef"), ctx.path(f"{ctx.design.name}.def")
    run_rcx(ctx, spef, deff, tag="extract_rcx")
    corner_spefs = tcl.spef_paths(ctx.pdk, spef)
    for f in (deff, *corner_spefs.values()):
        if not os.path.isfile(f) or os.path.getsize(f) == 0:
            raise RuntimeError(f"{f} was not written")
    _timing_gate(ctx)
    ctx.outputs.update(spef=spef, def_=deff, spef_corners=corner_spefs)
    reports(ctx, "signoff")
    power_checks(ctx)
    if ctx.params.get("require_grid_connected") and not ctx.metrics["grid_connected"]:
        raise RuntimeError(f"power grid not connected: {ctx.outputs['power_grid']}")
    with open(spef) as f:
        text = f.read()
    ctx.metrics["spef_nets"] = text.count("\n*D_NET")
    names = dict(re.findall(r"^\*(\d+) (\S+)$", text[text.find("*NAME_MAP"):text.find("*PORTS")], re.M))
    in_spef = {names.get(i, i).replace("\\", "") for i in re.findall(r"^\*D_NET \*(\d+) ", text, re.M)}
    absent = [n for n in nets if n not in in_spef]
    if absent:
        raise RuntimeError(f"analog nets missing from the SPEF: {absent}")
    ctx.metrics["analog_nets_in_spef"] = len(nets)


def signoff(ctx):
    """Physical (GDS, DRC, LVS, antennas) and timing (independent STA) signoff
    of the parent's final database (see sipath.signoff). params: checks, require,
    fail_on_violation (default True)."""
    from . import signoff as so
    ctx.outputs["signoff_summary"] = so.run(ctx)


def min_area_repair(ctx, tag: str = "route") -> dict:
    """Patch routed islands under their layer's minimum area (sipath.minarea)."""
    from . import minarea
    r = minarea.repair(ctx, ctx.path(f"{tag}_minarea.def"))
    ctx.outputs[f"{tag}_min_area"] = r
    ctx.metrics["min_area_patches"] = ctx.metrics.get("min_area_patches", 0) + r["patched"]
    ctx.metrics["min_area_unfixed"] = len([b for b in r["blocked"] if "no room" in b])
    if r.get("refused"):
        ctx.outputs[f"{tag}_min_area_refused"] = r["refused"]
    return r


def run_rcx(ctx, spef: str, def_out: str | None = None, tag: str = "rcx") -> int:
    """OpenRCX (all corners) without losing routing: extraction re-orders each
    net's wire and drops its patches (the router's minimum-area RECTs among
    them), so every net is restored from a DEF written just before and the
    geometry is checked. Returns the number of nets restored."""
    from . import wires
    s = ctx.session
    before = ctx.path(f"{tag}_before.def")
    s.run(f"write_def {q(before)}")
    s.run(tcl.rcx(ctx.pdk, spef))
    n = wires.restore(s, before, ctx.path(f"{tag}_after.def"))
    ctx.metrics["rcx_restored_nets"] = n
    if def_out:
        s.run(f"write_def {q(def_out)}")
    return n


def reconnect_supplies(ctx):
    """Global supply connections again: cells added after the PDN (CTS buffers,
    repair buffers, tie cells, diodes, fillers) get their supply pins on the
    supply nets in the database (DEF, netlists, LVS)."""
    ctx.session.run(tcl.global_connect(ctx.design, ctx.pdk))


# step name -> function, for design files, the CLI and the MCP server
from .synth import synth  # noqa: E402  (tool step: Yosys, no OpenROAD process)

STEPS = {
    "synth": synth, "floorplan": floorplan, "macros": macros, "physical": physical, "place": place, "cts": cts,
    "route_analog": route_analog, "route_digital": route_digital, "route_repair": route_repair, "fill": fill, "extract": extract, "views": views, "integrate": integrate,
    "signoff": signoff,
}

# parameters each step reads from its variant dict; anything else is a mistake
# parameters are declared (type, default, doc) in sipath.params
STEP_PARAMS = {step: set(decl) for step, decl in params.DECLARED.items() if step in STEPS}


def check_params(step: str, variants) -> None:
    """Raise ValueError for a variant that is not a dict, names a parameter
    the step does not declare, or gives a value of the wrong type or range."""
    if step not in STEPS or step not in params.DECLARED:  # live tables: plugin steps included
        raise ValueError(f"unknown step {step!r}; known: {sorted(STEPS)}")
    if variants is None:
        return
    if not isinstance(variants, list) or not variants:
        raise ValueError(f"{step}: variants must be a non-empty list of parameter dicts")
    for k, v in enumerate(variants):
        if not isinstance(v, dict):
            raise ValueError(f"{step}: variant {k} is not a dict: {v!r}")
        try:
            params.check(step, v)
        except ValueError as e:
            raise ValueError(f"variant {k}: {e}") from e