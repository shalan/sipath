"""Helpers the steps share: supply connections and checks, reports, design-rule counts, extraction with
wire restore, antenna loops, the timing gate, global routing and the PPA summary."""
from __future__ import annotations

import json
import os
import re

from ..tools.openroad.ops import db, place as place_ops, power as power_ops, report as report_ops, route as route_ops
from ..tools.openroad.ops.report import PPA_TCL  # noqa: F401 (re-exported)
from ..tools.openroad.session import TclError
from ..tools.openroad.tcl import tcl_parse


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
    o, bx0, by0, bx1, by1, W, H, pins, nets = db.supply_pin_data(session, inst)
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
    dbu = db.dbu(ctx.session)
    pd = ctx.pdk.pdn_defaults
    return {"via_pitch": int(round(pd.get("via_pitch", 1.6) * dbu)),
            "via_inset": int(round(pd.get("via_inset", 0.8) * dbu))}

def connect_supplies(ctx):
    """Strap supply tabs pdngen cannot reach to the grid: the supply pads'
    core-side tabs (to the core ring) and macro supply pins left unconnected."""
    from .. import chip, powerconnect
    d, s = ctx.design, ctx.session
    die = db.die_box(s)
    layers = list(ctx.pdk.layers)
    vias = _via_array(ctx)
    done = []
    if d.mode == "die" and d.pdn.rings and d.pdn.ring_connect_to_pads:
        for inst, pin, net in chip.supply_pads(ctx):
            if net in grid_nets(d):
                done += [dict(r, inst=inst) for r in powerconnect.connect(s, inst, f"^{re.escape(pin)}$", layers, die, **vias)]
    for m in d.macros:
        rep = macro_supply_pins(s, m.inst)
        for item in rep["unconnected"]:
            pin, net = item.split("->")
            if net in grid_nets(d):
                done += [dict(r, inst=m.inst) for r in powerconnect.connect(s, m.inst, f"^{re.escape(pin)}$", layers, die, **vias)]
    ctx.outputs["power_connections"] = done
    ctx.metrics["power_straps"] = sum(1 for r in done if r["ok"])
    failed = [f"{r['inst']}/{r['pin']}" for r in done if not r["ok"]]
    if failed:
        ctx.outputs["power_connections_failed"] = failed
    _no_supply_shorts(ctx, layers)

def _no_supply_shorts(ctx, layers):
    """Fail on special wiring of two nets that overlaps or touches (a short
    that DRC does not see and only LVS would)."""
    from .. import powerconnect
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
        has_terms = db.net_has_bterms(s, net)
        r = power_ops.check_grid(s, net, has_terms)
        unconnected_inst = re.findall(r"Unconnected instance (\S+)", r.output)
        out[net] = {"connected": r.rc == 0, "unconnected_instances": sorted(set(i.split("/")[0] for i in unconnected_inst)),
                    "unconnected_shapes": len(re.findall(r"PSM-0038", r.output))}
        v = ctx.pdk.voltages
        volt = 0.0 if ctx.design.is_ground(net) else (v["analog"] if net in ctx.design.analog_supplies else v["core"])
        r = power_ops.ir_drop(s, net, volt, has_terms)
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

def clock_nets(ctx) -> list[str]:
    nets = list(ctx.design.clock_nets)
    for p in ([] if nets else ctx.design.clock_ports):
        nets.append(db.clock_port_net(ctx.session, p))
    for n in nets:
        db.require_net(ctx.session, n)
    bad = set(nets) & set(ctx.design.analog_nets)
    if bad:
        raise ValueError(f"analog nets cannot be clock nets: {sorted(bad)}")
    return nets

def check_placement(ctx_or_session, design=None):
    """``check_placement`` with one precise exception: DPL's blocked-layer
    check may flag a hard macro that is an endpoint of a frozen analog net
    (the analog wire must land on the macro's own pin). Any standard cell
    flagged, or any other placement violation, fails."""
    s = getattr(ctx_or_session, "session", ctx_or_session)
    d = design or getattr(ctx_or_session, "design", None)
    r = place_ops.check_placement(s)
    if r.rc == 0:
        return []
    failed = re.findall(r"\[WARNING (DPL-\d+)\] (.+?) check failed \((\d+)\)", r.output)
    other = [f for f in failed if f[0] != "DPL-0010"]
    if other or not failed or d is None or not d.analog_nets:
        raise RuntimeError(f"check_placement failed: {r.value}\n" + "\n".join(r.output.strip().splitlines()[-15:]))
    after = r.output.split("Blocked layers check failed", 1)[1].splitlines()[1:]
    flagged = [ln.strip() for ln in after if ln.startswith(" ") and ln.strip()]
    allowed = db.macro_endpoints(s, d.analog_nets)
    bad = [f for f in flagged if f not in allowed]
    if bad or not flagged:
        raise RuntimeError(f"check_placement: blocked layers on {bad or 'unparsed instances'}")
    return flagged

def tie_max_fanout(ctx) -> int:
    ties = sorted({m for m, _ in ctx.pdk.tie_cells()})
    return db.tie_max_fanout(ctx.session, ties)

def reports(ctx, tag: str) -> dict:
    """Timing (setup/hold paths per corner), clock skew and power reports for
    this attempt, written to files next to its checkpoint."""
    s = ctx.session
    files, failed = {}, []
    corners = [c.name for c in ctx.pdk.analysis_corners()]
    power = ("report_power" if len(corners) == 1 else
             "; ".join(f"puts {{== corner {c}}}; report_power -scene {c}" for c in corners))
    for name, cmd in (("timing_setup", "report_checks -path_delay max -group_path_count 5 -format full_clock"),
                      ("timing_hold", "report_checks -path_delay min -group_path_count 5 -format full_clock"),
                      ("clock_skew", "report_clock_skew"),
                      ("power", power),
                      ("design_rules", "report_check_types -max_slew -max_capacitance -max_fanout -violators")):
        f = ctx.path(f"{tag}_{name}.rpt")
        r = report_ops.to_file(s, f, cmd)
        if not (r.rc == 0 and os.path.isfile(f)):
            with open(f, "w") as fh:
                fh.write(f"{cmd} failed: {r.value}\n")
            failed.append(name)
        files[name] = f
    ctx.outputs.setdefault("reports", {}).update({f"{tag}_{k}": v for k, v in files.items()})
    if failed:
        ctx.outputs[f"{tag}_reports_failed"] = failed
    if "design_rules" in failed:  # no report is not a clean one
        ctx.metrics.update({"drv_slew": None, "drv_cap": None, "drv_fanout": None, "drv_excluded": None,
                            "drv_clean": None})
    else:
        ctx.metrics.update(design_rule_counts(ctx, files["design_rules"]))
    ctx.metrics.update(power_metrics(files["power"]))
    return files


def power_metrics(report: str) -> dict:
    """Power from ``report_power`` (one corner, or ``== corner <name>`` sections): the typical corner's (else the
    first's) internal, switching, leakage and total in W, and every corner's total. {} when there is none."""
    import re
    try:
        with open(report, errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    parts = re.split(r"^== corner (\S+)\s*$", text, flags=re.M)
    sections = [("", parts[0])] + list(zip(parts[1::2], parts[2::2]))
    found = {}
    for corner, body in sections:
        m = re.search(r"^Total\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", body, re.M)
        if m:
            found[corner] = [float(x) for x in m.groups()]
    if not found:
        return {}
    pick = next((c for c in found if c.startswith("tt")), next(iter(found)))
    internal, switching, leakage, total = found[pick]
    out = {"power_internal_w": internal, "power_switching_w": switching, "power_leakage_w": leakage,
           "power_total_w": total}
    out.update({f"power_total_w_{c}": v[3] for c, v in found.items() if c})
    return out

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
        types = db.inst_master_types(ctx.session, insts)
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

def _global_route(ctx, script: str, report: str):
    """Run a global route; on congestion (GRT-0116) fail with the congestion
    report summarised (regions, overflow, worst box, nets) instead of a bare
    error. Records ``congested`` / ``grt_overflow`` when it succeeds."""
    from .. import congestion
    rpt = ctx.path(report)
    try:
        route_ops.run_global_route(ctx.session, script)
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
    from .. import congestion
    s, d = ctx.session, ctx.design
    route_ops.set_routing_layers(s, d, ctx.pdk, margin=margin)
    rpt = ctx.path("place_congestion.rpt")
    route_ops.global_route_allow_congestion(s, rpt)
    c = congestion.parse(rpt)
    ctx.metrics["grt_overflow"] = c["overflow"]
    ctx.metrics["grt_congested_gcells"] = c["gcells"]
    if c["gcells"]:
        ctx.outputs.setdefault("reports", {})["place_congestion"] = rpt
    ctx.outputs["routability"] = {"margin": margin, **{k: c[k] for k in ("gcells", "overflow", "max", "worst", "nets")}}
    return c

def ppa_summary(ctx, parasitics: str) -> dict:
    """A first cut at power, performance and area from the session's current
    parasitics: cell and macro area, utilization, WNS/TNS, the achievable clock
    period (period - WNS) and fmax, a suggested period for the next SDC
    (achievable + ``margin``), total power and its parts, wirelength. Written
    to ppa.json / ppa.md and into the metrics (ppa_*)."""
    import math
    s, p = ctx.session, ctx.params
    v = report_ops.ppa_values(s)
    kv = dict(zip(v[0::2], v[1::2]))
    num = lambda x: (round(float(x), 4) if abs(float(x)) < 1e29 else None)  # noqa: E731
    wns, tns, whs = num(kv["wns"]), num(kv["tns"]), num(kv["whs"])
    clocks = []
    margin = float(p.get("margin", 0.05))
    for name, period in (kv["clocks"] or []):  # tcl_parse nests: [[name, period], ...]
        period = float(period)
        ach = period - wns if wns is not None else None
        sug = math.ceil(ach * (1 + margin) * 100) / 100 if ach else None
        clocks.append({"clock": name, "period_ns": period, "achievable_period_ns": round(ach, 3) if ach else None,
                       "fmax_mhz": round(1000.0 / ach, 1) if ach and ach > 0 else None, "suggested_period_ns": sug,
                       "sdc": f"create_clock -name {name} -period {sug} ..." if sug else None})
    pw = report_ops.power(s)
    power = {}
    m = re.search(r"^Total\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)", pw, re.M)
    if m:
        power = dict(zip(("internal_w", "switching_w", "leakage_w", "total_w"), (float(x) for x in m.groups())))
    std, mac, core = float(kv["std_area"]), float(kv["macro_area"]), float(kv["core_area"])
    from ..tools.openroad.checkpoint import parse_metrics
    wl = ctx.metrics.get("wirelength_um")  # routed, when a detailed route ran
    wl_kind = "routed"
    if not wl:
        wl, wl_kind = parse_metrics(db.flow_metrics(s)).get("hpwl_um"), "HPWL"
    res = {"parasitics": parasitics, "corner": ctx.pdk.typical().name if len(ctx.pdk.analysis_corners()) == 1 else
           [c.name for c in ctx.pdk.analysis_corners()],
           "area": {"std_cell_um2": round(std, 1), "macro_um2": round(mac, 1), "cells": int(kv["cells"]),
                    "core_um2": round(core, 1), "utilization": round((std + mac) / core, 4) if core else None},
           "timing": {"wns_ns": wns, "tns_ns": tns, "whs_ns": whs, "clocks": clocks},
           "power": power, "wirelength_um": wl, "wirelength_kind": wl_kind}
    with open(ctx.path("ppa.json"), "w") as f:
        json.dump(res, f, indent=1)
    lines = [f"# PPA (first cut: {parasitics} parasitics)", "",
             f"- area: {res['area']['std_cell_um2']} um2 in {res['area']['cells']} cells"
             + (f", macros {res['area']['macro_um2']} um2" if mac else "")
             + f"; utilization {res['area']['utilization']}",
             f"- timing: WNS {wns} ns, TNS {tns} ns, WHS {whs} ns"]
    for c in clocks:
        lines.append(f"- clock {c['clock']}: period {c['period_ns']} ns, achievable {c['achievable_period_ns']} ns "
                     f"(fmax {c['fmax_mhz']} MHz); suggested `{c['sdc']}`")
    if power:
        lines.append(f"- power: total {power['total_w']:.3e} W (internal {power['internal_w']:.3e}, "
                     f"switching {power['switching_w']:.3e}, leakage {power['leakage_w']:.3e})")
    if wl:
        lines.append(f"- wirelength ({wl_kind}): {round(wl)} um")
    with open(ctx.path("ppa.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    ctx.outputs["ppa"] = res
    ctx.outputs.setdefault("reports", {})["ppa"] = ctx.path("ppa.md")
    ctx.metrics.update(ppa_std_area_um2=res["area"]["std_cell_um2"], ppa_utilization=res["area"]["utilization"],
                       ppa_power_w=power.get("total_w"),
                       ppa_fmax_mhz=clocks[0]["fmax_mhz"] if clocks else None)
    return res

def antenna_loop(ctx, max_rounds: int = 3, tag: str = "antenna") -> int:
    """Repair antennas on the detailed routes and re-route, until clean or
    ``max_rounds``. Updates metrics (drc, antenna_violations, antenna_diodes)."""
    s, p = ctx.session, ctx.params
    rounds = 0
    while ctx.pdk.diode_cell and ctx.metrics["antenna_violations"] and rounds < max_rounds:
        rounds += 1
        r = route_ops.repair_antennas(s, ctx.pdk.diode_cell, 1)
        m = re.findall(r"Inserted (\d+) diodes", r.output)
        ctx.metrics["antenna_diodes"] = ctx.metrics.get("antenna_diodes", 0) + (int(m[-1]) if m else 0)
        check_placement(ctx)
        from .. import wires
        wires.normalize(s, ctx.path(f"{tag}{rounds}.def"))
        # route every digital net (a previous patch route may have narrowed the
        # router's net list, which would leave the new diode pins unconnected)
        r = route_ops.detailed_route_all_digital(s, ctx.path("drc.rpt"), p.get("iters", 64))
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
    unrouted = db.unrouted_net_count(ctx.session)
    ctx.metrics["unrouted_nets"] = int(unrouted)
    if int(unrouted):
        raise RuntimeError(f"{unrouted} nets left unrouted")

def check_antennas(ctx) -> int:
    rpt = ctx.path("antenna.rpt")
    r = route_ops.check_antennas(ctx.session, rpt)
    m = re.findall(r"Found (\d+) net violations", r.output)
    if not m:
        raise RuntimeError("check_antennas reported no violation count")
    ctx.outputs.setdefault("reports", {})["antenna"] = rpt
    return int(m[-1])

def timing_met(wns, whs):
    """True/False for the constrained slacks; None when neither setup nor hold has a constrained path."""
    if wns is None and whs is None:
        return None
    return (wns is None or wns >= 0) and (whs is None or whs >= 0)


def _timing_gate(ctx):
    """Record whether setup and hold are met (with the parasitics loaded now): None when no path is constrained
    (nothing was judged, not met); fail when params ``fail_on_violation`` is set and they are not met."""
    m = db.worst_slacks(ctx.session)
    wns, whs = (float(v) if abs(float(v)) < 1e30 else None for v in m)
    met = timing_met(wns, whs)
    ctx.metrics["timing_met"] = met
    if met is False and ctx.params.get("fail_on_violation"):
        raise RuntimeError(f"timing not met after extraction: setup {wns} ns, hold {whs} ns")

def min_area_repair(ctx, tag: str = "route") -> dict:
    """Patch routed islands under their layer's minimum area (sipath.minarea)."""
    from .. import minarea
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
    from .. import wires
    s = ctx.session
    before = ctx.path(f"{tag}_before.def")
    route_ops.write_def(s, before)
    route_ops.rcx(s, ctx.pdk, spef)
    n = wires.restore(s, before, ctx.path(f"{tag}_after.def"))
    ctx.metrics["rcx_restored_nets"] = n
    if def_out:
        route_ops.write_def(s, def_out)
    return n

def reconnect_supplies(ctx):
    """Global supply connections again: cells added after the PDN (CTS buffers,
    repair buffers, tie cells, diodes, fillers) get their supply pins on the
    supply nets in the database (DEF, netlists, LVS)."""
    power_ops.global_connect(ctx.session, ctx.design, ctx.pdk)
