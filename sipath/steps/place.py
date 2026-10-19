"""Steps ``place`` (global and detailed placement with repair) and ``cts``."""
from __future__ import annotations

from .. import tcl
from ..tcl import q, tcl_words
from .common import check_placement, clock_nets, reconnect_supplies, reports, routability_check, tie_max_fanout
from .structure import cluster_spread, compact_cluster, datapath_check, enforce_fences, fence_violations, reabut_all


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
