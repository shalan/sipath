"""Steps ``route_digital`` (global and detailed routing with repair) and ``route_repair`` (post-route
timing repair on extracted parasitics)."""
from __future__ import annotations

import re

from .. import analog, tcl
from ..tcl import q
from .common import _check_unrouted, _drt_numbers, _global_route, _timing_gate, antenna_loop, check_antennas, check_placement, min_area_repair, ppa_summary, reconnect_supplies, reports, run_rcx
from .structure import datapath_check, enforce_fences, fence_violations, reabut_all


def route_digital(ctx):
    """Global route -> timing repair on global-route parasitics -> global
    route again -> antenna repair -> detailed route -> antenna check -> RCX
    (all corners) -> reports.

    params: detailed (True; off: stop after global routing with a PPA summary),
    iters, max_drc, max_antenna (default 0), pin_access (True),
    repair_after_grt (True), fail_on_violation. Post-route repair and fillers
    are the next step (route_repair)."""
    d, s, p = ctx.design, ctx.session, ctx.params
    frozen = list(d.analog_nets) + list(d.analog_supplies)
    before = analog.signature(s, frozen) if frozen else ""
    s.run(tcl.routing_layers(d, ctx.pdk))
    if p.get("pin_access", True):  # as ORFS: pin access points for the global router (and reused by DRT);
        s.run("pin_access")         # the layers come from set_routing_layers (its own options are deprecated)
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
    if not p.get("detailed", True):  # explore: PPA on global-route parasitics, no detailed route
        s.run("estimate_parasitics -global_routing\nset ::flow_grt_parasitics 1")
        ctx.metrics["routed"] = "global"
        ppa_summary(ctx, "global routing")
        reports(ctx, "route")
        return
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
    max_patch_nets (a quarter of the signal nets, at least 40), drv_sizing,
    fail_on_violation."""
    s, p = ctx.session, ctx.params
    for k in ("drc", "wirelength_um", "vias"):  # unchanged unless a patch route runs
        if k in (ctx.parent.metrics if ctx.parent else {}):
            ctx.metrics[k] = ctx.parent.metrics[k]
    s.run(tcl.routing_layers(ctx.design, ctx.pdk))
    run_rcx(ctx, ctx.path("route_rcx.spef"), tag="repair_rcx")
    from ..eco import post_route_repair
    ctx.outputs["post_route_repair"] = post_route_repair(ctx, p.get("rounds", 2), p.get("max_patch_nets"),
                                                         p.get("drv_sizing", False))
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
