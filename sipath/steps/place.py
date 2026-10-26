"""Steps ``place`` (global and detailed placement with repair) and ``cts``."""
from __future__ import annotations

from ..harness import CHANNEL_ROW
from ..tools.openroad import heatmap as heatmaps
from ..tools.openroad.ops import db, place as place_ops
from ..tools.openroad.tcl import tcl_words
from .common import check_placement, clock_nets, reconnect_supplies, reports, routability_check, tie_max_fanout
from .structure import cluster_spread, compact_cluster, datapath_check, enforce_fences, fence_violations, reabut_all


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
    rows = place_ops.row_boxes(s)
    region = [r for r in rows if not r[0].startswith(CHANNEL_ROW + "_")]
    chans = [r for r in rows if r[0].startswith(CHANNEL_ROW + "_")]
    if not chans or not region:
        return []
    rx0, ry0 = min(r[1] for r in region), min(r[2] for r in region)
    rx1, ry1 = max(r[3] for r in region), max(r[4] for r in region)
    out = []
    for w in place_ops.pin_chains(s):
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
        place_ops.move_centres(s, zip(moved, pts))
        dbu = db.dbu(s)
        out.append({"pin": name, "buffers": len(chain), "path_um": round(total / dbu, 1),
                    "spacing_um": round((total - first) / len(moved) / dbu, 1)})
    if out:
        place_ops.detailed_placement(s)
    return out


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
    n0 = place_ops.inst_count(s)
    nets = place_ops.collect_port_nets(s, sorted(ctx.design.clock_ports))
    place_ops.repair_port_nets(s, wire, m.get("slew", 0), m.get("cap", 0))
    return {"nets": nets, "buffers": place_ops.inst_count(s) - n0}


def repair_legalized(ctx, rounds: int = 4) -> int:
    """repair_design on the legalized placement, then legalization, until a
    round inserts no instance (at most ``rounds``). Returns the rounds that
    inserted some."""
    s, p = ctx.session, ctx.params
    util = f" -max_utilization {p['max_util']}" if p.get("max_util") else ""
    for k in range(rounds):
        n = place_ops.inst_count(s)
        place_ops.repair_design_legalized(s, util, ctx.pdk)
        if place_ops.inst_count(s) == n:
            return k
    return rounds


def placement_blockages(s, blockages) -> int:
    """``create_blockage -region -max_density`` for each {box: [x0, y0, x1, y1] um, max_density: %}, for global
    placement only: ``remove_placement_blockages`` destroys exactly these before legalization (fixed taps in a
    partial blockage fail check_placement) so the checkpoint carries none of them."""
    place_ops.begin_blockages(s)
    for b in blockages or []:
        box = b.get("box")
        md = b.get("max_density", 50)
        if (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            raise ValueError(f"place.blockages: box must be [x0, y0, x1, y1] um with x0 < x1, y0 < y1, not {box!r}")
        if not isinstance(md, (int, float)) or not 0 <= md < 100:
            raise ValueError(f"place.blockages: max_density must be a percentage in [0, 100), not {md!r}")
        place_ops.create_blockage(s, box, md)
    place_ops.record_blockages(s)
    return len(blockages or [])


def remove_placement_blockages(s) -> None:
    place_ops.remove_recorded_blockages(s)


def place(ctx):
    """Timing-driven global placement, repair, detailed placement."""
    d, s, p = ctx.design, ctx.session, ctx.params
    heatmaps.schedule(ctx, p.get("heatmaps"))  # collected after the checkpoint is saved
    if p.get("repair_tie_fanout", True):  # one tie cell per load, placed by gpl next to it
        for master, port in ctx.pdk.tie_cells():
            place_ops.repair_tie_fanout(s, master + "/" + port)
    if p.get("remove_buffers"):
        place_ops.remove_buffers(s)
    ctx.metrics["tie_max_fanout"] = tie_max_fanout(ctx)
    blockages = placement_blockages(s, p.get("blockages"))
    if blockages:  # partial placement blockages: regions gpl and legalization fill to at most max_density
        ctx.metrics["placement_blockages"] = blockages
    fence_groups = [g for g in d.groups if g.kind == "fence"]
    # with fences, this gpl build diverges inside the region after its first
    # timing-driven iteration unless the virtual resizes are discarded
    # (repair_design / repair_timing still run right after placement)
    if d.mode == "macro" and not d.floorplan_def:
        # pins placed by the flow: a placement ignoring them, pins by the cells, then the real one
        place_ops.global_place_skip_io(s, p.get("density", 0.55), p.get("seed"))
        place_ops.place_pins(s, ctx.pdk)
    if d.harness:  # its pin channels are for repair's buffers, not for the logic gpl pulls to the far pins
        ctx.metrics["pin_channel_rows_blocked"] = place_ops.block_pin_channels(s, CHANNEL_ROW)
    place_ops.global_place(s, p.get("density", 0.55), p.get("seed"), p.get("timing_driven", True),
                           not fence_groups, p.get("routability_driven", False))
    if d.harness:
        place_ops.restore_pin_channels(s)
    clusters = [g for g in d.groups if g.kind == "cluster"]
    for g in clusters:
        # spread right after gpl (cells still overlap) vs after packing + legalization
        ctx.outputs.setdefault("clusters", {})[g.name] = {"spread_gpl_um": cluster_spread(s, g)}
        fences = [tuple(ctx.parent_outputs("physical")["groups"][f.name]["box_dbu"]) for f in d.groups
                  if f.kind == "fence"]
        ctx.outputs["clusters"][g.name].update(compact_cluster(s, g, fences))
    place_ops.repair_after_place(s, p.get("max_util"), ctx.pdk)
    ctx.outputs["reabut"] = reabut_all(ctx)  # sizing may have grown FIRM datapath cells
    if blockages:
        remove_placement_blockages(s)
    place_ops.detailed_placement(s)
    if d.harness:  # long pin nets across a mostly empty wrapper: buffers along the rows, then repair again
        ctx.outputs["pin_repeaters"] = space_pin_repeaters(ctx)
        ctx.outputs["port_nets"] = repair_port_nets(ctx)
        ctx.metrics["repair_legalized_rounds"] = repair_legalized(ctx)
    if p.get("improve_placement", True) and not fence_groups:
        # ORFS's placement cleanup: shorter wires, less congestion for the router
        place_ops.improve_placement(s)
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
        out[n] = place_ops.net_terminals(ctx.session, n)
    return out

def cts(ctx):
    s = ctx.session
    heatmaps.schedule(ctx, ctx.params.get("heatmaps"))
    nets = clock_nets(ctx)
    analog_before = _analog_terms(ctx)
    place_ops.clock_tree(s, ctx.pdk, nets, ctx.params.get("sink_clustering_size"),
                         ctx.params.get("sink_clustering_levels"), ctx.params.get("distance_between_buffers"))
    ctx.outputs["reabut"] = reabut_all(ctx)
    place_ops.detailed_placement(s)
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
