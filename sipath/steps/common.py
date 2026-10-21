"""Helpers the steps share: supply connections and checks, reports, design-rule counts, extraction with
wire restore, antenna loops, the timing gate, global routing and the PPA summary."""
from __future__ import annotations

import json
import os
import re

from ..tools.openroad import tcl
from ..tools.openroad.session import TclError
from ..tools.openroad.tcl import q, qlist, tcl_parse, tcl_words


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
    from ..tools.openroad.tcl import tcl_parse
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
    from .. import chip, powerconnect
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

def _global_route(ctx, script: str, report: str):
    """Run a global route; on congestion (GRT-0116) fail with the congestion
    report summarised (regions, overflow, worst box, nets) instead of a bare
    error. Records ``congested`` / ``grt_overflow`` when it succeeds."""
    from .. import congestion
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
    from .. import congestion
    s, d = ctx.session, ctx.design
    s.run(tcl.routing_layers(d, ctx.pdk, margin=margin))
    rpt = ctx.path("place_congestion.rpt")
    s.run(f"global_route -allow_congestion -congestion_iterations 30 -congestion_report_file {q(rpt)}\n"
          "foreach net [[ord::get_db_block] getNets] { $net clearGuides }")  # config.command_timeout
    c = congestion.parse(rpt)
    ctx.metrics["grt_overflow"] = c["overflow"]
    ctx.metrics["grt_congested_gcells"] = c["gcells"]
    if c["gcells"]:
        ctx.outputs.setdefault("reports", {})["place_congestion"] = rpt
    ctx.outputs["routability"] = {"margin": margin, **{k: c[k] for k in ("gcells", "overflow", "max", "worst", "nets")}}
    return c

PPA_TCL = r"""
set __o {}
set __b [ord::get_db_block]; set __dbu [$__b getDbUnitsPerMicron]
set __std 0.0; set __mac 0.0; set __n 0
foreach __i [$__b getInsts] {
  set __m [$__i getMaster]
  set __a [expr {double([$__m getWidth]) * [$__m getHeight] / $__dbu / $__dbu}]
  if {[$__m isBlock]} { set __mac [expr {$__mac + $__a}]; continue }
  if {[$__m getType] ne "CORE" || [regexp {fill|tap|decap} [$__m getName]]} continue
  set __std [expr {$__std + $__a}]; incr __n
}
set __c [$__b getCoreArea]
set __core [expr {double([$__c dx]) * [$__c dy] / $__dbu / $__dbu}]
lappend __o std_area $__std macro_area $__mac cells $__n core_area $__core
lappend __o wns [sta::worst_slack -max] tns [sta::total_negative_slack -max] whs [sta::worst_slack -min]
set __cl {}
foreach __k [all_clocks] { lappend __cl [list [get_name $__k] [get_property $__k period]] }
lappend __o clocks $__cl
set __o
"""

def ppa_summary(ctx, parasitics: str) -> dict:
    """A first cut at power, performance and area from the session's current
    parasitics: cell and macro area, utilization, WNS/TNS, the achievable clock
    period (period - WNS) and fmax, a suggested period for the next SDC
    (achievable + ``margin``), total power and its parts, wirelength. Written
    to ppa.json / ppa.md and into the metrics (ppa_*)."""
    import math
    s, p = ctx.session, ctx.params
    v = tcl_parse(s.value(PPA_TCL))
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
    pw = s.run("report_power").output
    power = {}
    m = re.search(r"^Total\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)", pw, re.M)
    if m:
        power = dict(zip(("internal_w", "switching_w", "leakage_w", "total_w"), (float(x) for x in m.groups())))
    std, mac, core = float(kv["std_area"]), float(kv["macro_area"]), float(kv["core_area"])
    from ..tools.openroad.checkpoint import parse_metrics
    wl = ctx.metrics.get("wirelength_um")  # routed, when a detailed route ran
    wl_kind = "routed"
    if not wl:
        wl, wl_kind = parse_metrics(s.value("::flow_metrics")).get("hpwl_um"), "HPWL"
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
        r = s.run(f"repair_antennas {q(ctx.pdk.diode_cell)} -iterations 1")
        m = re.findall(r"Inserted (\d+) diodes", r.output)
        ctx.metrics["antenna_diodes"] = ctx.metrics.get("antenna_diodes", 0) + (int(m[-1]) if m else 0)
        check_placement(ctx)
        from .. import wires
        wires.normalize(s, ctx.path(f"{tag}{rounds}.def"))
        # route every digital net (a previous patch route may have narrowed the
        # router's net list, which would leave the new diode pins unconnected)
        r = s.run(tcl.DIGITAL_NETS + "::flow_route_nets $::flow_digital\n"
                  + tcl.detailed_route_only(ctx.path("drc.rpt"), p.get("iters", 64)))  # config.command_timeout
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
