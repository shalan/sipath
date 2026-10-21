"""Heatmaps from OpenROAD (``gui::dump_heatmap``, headless), collected after an
attempt's checkpoint is saved.

A step calls ``schedule(ctx, maps)``; the scheduler runs the collection once the
checkpoint is written (``Ctx.after_save``), in the same process: RUDY and the
routing map start the global router, which must touch neither the checkpoint
nor the metrics read before it. Each map becomes ``<attempt>/heatmaps/<map>.csv``,
``outputs["heatmaps"][map]`` (unit, peak, p95, hot share, hotspot boxes with the
instances or nets inside, all from the full map) and metrics
``heatmap_<map>_peak|p95|hot_share``. A map finer than 100 x 100 bins (RUDY and
routing on a large die: one bin per gcell) is stored coarsened to at most 100
per side, each bin the largest value it covers (``bins`` is the full map's
count, ``stored_bins`` the file's). A map
OpenROAD cannot give here (Power without Liberty, IR drop without
``analyze_power_grid``) is recorded as unavailable, with OpenROAD's reason.

RUDY and the routing map are taken with the design's routing layers and
capacity adjustments set (as the routing steps set them), so their percentages
mean the same at every step. The IR-drop map is the core power net (the PDK's
``power`` pin net) on the PDK's rail layer, after ``extract``'s analysis.
Hotspots list instances without the physical-only cells (fill, taps, endcaps).
"""
from __future__ import annotations

import math
import os

from ... import heatmap
from . import tcl
from .tcl import q

MAX_BINS = 100  # bins per side at most, for the maps with a settable grid
NETS = {"rudy", "routing"}  # maps whose hotspots are listed by net; the others by instance

OBJECTS_TCL = r"""
set __b [ord::get_db_block]
set __u [expr {double([$__b getDbUnitsPerMicron])}]
set __out {}
foreach __i [$__b getInsts] {
  set __t [[$__i getMaster] getType]
  if {$__t in {CORE_SPACER CORE_WELLTAP CORE_ANTENNACELL} || [string match ENDCAP* $__t]} continue
  set __bb [$__i getBBox]
  lappend __out "I [$__i getName] [expr {[$__bb xMin]/$__u}] [expr {[$__bb yMin]/$__u}] [expr {[$__bb xMax]/$__u}] [expr {[$__bb yMax]/$__u}]"
}
foreach __n [$__b getNets] {
  if {[$__n isSpecial]} continue
  set __xs {}; set __ys {}
  foreach __t [$__n getITerms] { set __bb [[$__t getInst] getBBox]; lappend __xs [$__bb xMin] [$__bb xMax]; lappend __ys [$__bb yMin] [$__bb yMax] }
  foreach __t [$__n getBTerms] { set __bb [$__t getBBox]; lappend __xs [$__bb xMin] [$__bb xMax]; lappend __ys [$__bb yMin] [$__bb yMax] }
  if {![llength $__xs]} continue
  set __xs [lsort -integer $__xs]; set __ys [lsort -integer $__ys]
  lappend __out "N [$__n getName] [expr {[lindex $__xs 0]/$__u}] [expr {[lindex $__ys 0]/$__u}] [expr {[lindex $__xs end]/$__u}] [expr {[lindex $__ys end]/$__u}]"
}
join $__out "\n"
"""


def schedule(ctx, maps) -> None:
    """Collect ``maps`` (heatmap.MAPS names) after this attempt's checkpoint is saved; [] collects none."""
    maps = [m for m in (maps or []) if m]
    unknown = [m for m in maps if m not in heatmap.OPENROAD_NAMES]
    if unknown:
        raise ValueError(f"heatmaps: unknown map(s) {unknown}; known: {list(heatmap.MAPS)}")
    if maps:
        ctx.after_save.append(lambda c: collect(c, maps))


def _bin(s) -> float:
    w, h = (float(x) for x in s.value(
        "set __d [[ord::get_db_block] getDieArea]; set __u [[ord::get_db_block] getDbUnitsPerMicron]\n"
        "list [expr {([$__d xMax]-[$__d xMin])/double($__u)}] [expr {([$__d yMax]-[$__d yMin])/double($__u)}]"
    ).split())
    return float(max(10, math.ceil(max(w, h) / MAX_BINS)))


def _power_net(ctx) -> str | None:
    for gp in ctx.pdk.power_pins:
        if gp.get("kind") == "power":
            return ctx.design.net(gp["net"])
    return None


def _objects(s) -> tuple[dict, dict]:
    insts, nets = {}, {}
    for line in s.value(OBJECTS_TCL).splitlines():
        t = line.split()
        if len(t) == 6:
            (insts if t[0] == "I" else nets)[t[1]] = tuple(float(x) for x in t[2:])
    return insts, nets


def collect(ctx, maps) -> dict:
    s = ctx.session
    out_dir = ctx.path("heatmaps")
    os.makedirs(out_dir, exist_ok=True)
    grid = _bin(s)
    found, objects = {}, None
    if NETS & set(maps):  # the routing steps' layer range and capacities: comparable percentages
        s.run(tcl.routing_layers(ctx.design, ctx.pdk))
    for name in maps:
        orname, path = heatmap.OPENROAD_NAMES[name], os.path.join(out_dir, f"{name}.csv")
        if name not in NETS:  # RUDY and routing follow the global router's gcells
            s.run(f"gui::set_heatmap {orname} GridX {grid}\ngui::set_heatmap {orname} GridY {grid}")
        if name == "irdrop":
            net = _power_net(ctx)
            if net is None:
                found[name] = {"unavailable": "the PDK names no power net"}
                continue
            s.run(f"gui::set_heatmap IRDrop Net {net}\ngui::set_heatmap IRDrop Layer {ctx.design.pdn.rail_layer}")
        err = s.value(f"if {{[catch {{gui::dump_heatmap {orname} {q(path)}}} __e]}} {{ set __e }} else {{ list }}")
        if err or not os.path.isfile(path):
            found[name] = {"unavailable": err or "no CSV written"}
            continue
        unit, bins = heatmap.read(path)
        if not bins:
            found[name] = {"unavailable": "OpenROAD wrote no bins (no data for this map here)"}
            continue
        summ = heatmap.summary(name, unit, bins)
        if summ["hotspots"]:
            if objects is None:
                objects = _objects(s)
            kind, objs = ("nets", objects[1]) if name in NETS else ("instances", objects[0])
            for h in summ["hotspots"]:
                h[kind] = heatmap.inside(h["box"], objs)
        stored = heatmap.coarsen(bins, MAX_BINS)  # summary and hotspots from the raw map; the file at most 100x100
        if len(stored) < len(bins):
            heatmap.write(path, unit, stored)
            summ["stored_bins"] = len(stored)
        found[name] = {"csv": path, **summ}
        ctx.metrics.update({f"heatmap_{name}_peak": summ["peak"], f"heatmap_{name}_p95": summ["p95"],
                            f"heatmap_{name}_hot_share": summ["hot_share"]})
    ctx.outputs["heatmaps"] = found
    return found
