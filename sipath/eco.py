"""ECO step: resize and buffer from a selected routed checkpoint.

Ops: ``{"op": "resize", "inst": name, "master": cell}``
     ``{"op": "buffer", "net": name, "master": cell, "loads": [inst/pin...]}``

Sequence: snapshot netlist + locations -> apply ops -> legalize (re-abut
datapaths, keep fences) -> structural equivalence (failure raises, so no
ODB is written) -> collect touched nets (op nets + nets of every moved
instance) -> if touched <= ``max_patch_nets``: rip up and re-route only
those nets; else rip up every digital net and run a full digital route ->
fillers -> RCX -> DEF + SPEF.
"""
from __future__ import annotations

import os

from . import analog, equiv, liberty, steps, wires
from .netlist import read_verilog
from .tools.openroad.ops import db as db_ops
from .tools.openroad.ops import eco as eco_ops
from .tools.openroad.ops import place as place_ops
from .tools.openroad.ops import route as route_ops
from .tools.openroad.session import TclError

def _locations(session) -> dict[str, tuple]:
    return eco_ops.locations(session)


def _inst_nets(session, insts) -> set[str]:
    return eco_ops.inst_nets(session, insts)


def check_ops(ops, pdk, design):
    """Refuse an ECO whose op uses a cell the PDK or the design excludes."""
    for op in ops:
        m = op.get("master")
        if m and pdk.excluded(m, design.dont_use) and m not in design.allow_excluded:
            raise ValueError(f"ECO op {op}: {m} is excluded (known layout problems or design dont_use)")


def apply_op(session, op: dict) -> set[str]:
    """Apply one op, return the nets it touched."""
    kind = op["op"]
    if kind == "resize":
        nets = _inst_nets(session, [op["inst"]])
        now = eco_ops.resize(session, op["inst"], op["master"])  # replace_cell does nothing when the pins differ
        if now != op["master"]:
            raise RuntimeError(f"resize of {op['inst']} to {op['master']} did not happen (still {now}): "
                               "replace_cell needs a master with the same pins")
        return nets
    if kind == "buffer":
        before = eco_ops.net_names(session)
        from .tools.openroad.tcl import q, qlist
        args = [f"-buffer_cell {op['master']}", f"-net {q(op['net'])}"]
        if op.get("loads"):
            args.append(f"-load_pins [get_pins {qlist(op['loads'])}]")
        if op.get("name"):
            args.append(f"-buffer_name {q(op['name'])}")
        eco_ops.insert_buffer(session, op["net"], args)
        after = eco_ops.net_names(session)
        return {op["net"], *(after - before)}
    raise ValueError(f"unknown ECO op {kind!r}")


def _destroy_wires(session, nets=None) -> int:
    return eco_ops.destroy_wires(session, nets)


def _wire_signature(session, path) -> dict[str, tuple]:
    """Per-net routed geometry from a DEF written now."""
    route_ops.write_def(session, path)
    with open(path) as f:
        nets = wires.parse_def_paths(f.read())
    names = sorted({it[1] for i in nets.values() for p in i["paths"] for it in p["items"] if it[0] == "via"})
    flat = eco_ops.via_layers(session, analog.query_via_layers_tcl(names)) if names else []
    vias = {flat[k]: (flat[k + 1], flat[k + 2]) for k in range(0, len(flat), 3)}
    return {n: wires.geometry(i, vias) for n, i in nets.items()}


def _equivalent(session, pre_v, post_v) -> dict:
    """Structural equivalence of two netlists, cells described by the Liberty
    loaded in the session (functions, directions)."""
    before, after = read_verilog(pre_v), read_verilog(post_v)
    lib = liberty.query(session, {i.cell for m in (before, after) for i in m.instances})
    return equiv.check(before, after, lib)


def run_eco(ctx):
    s, d, p = ctx.session, ctx.design, ctx.params
    ops, max_patch = p["ops"], p["max_patch_nets"]
    check_ops(ops, ctx.pdk, d)
    pre_v, post_v = ctx.path("pre_eco.v"), ctx.path("post_eco.v")
    eco_ops.write_verilog(s, pre_v)
    loc0 = _locations(s)
    if d.analog_nets and not analog.is_frozen(s, d.analog_nets):
        # the parent was extracted (analog wiring restored): freeze again so the
        # ECO route cannot rip them up; extract() restores them at the end
        ctx.outputs["analog_shapes"] = analog.freeze_nets(s, d.analog_nets, ctx.path("analog_routed.def"))
        ctx.outputs["analog_def"] = ctx.path("analog_routed.def")
    frozen = list(d.analog_nets) + list(d.analog_supplies)
    analog_before = analog.signature(s, frozen) if frozen else ""
    ctx.outputs["wires_normalized"] = wires.normalize(s, ctx.path("checkpoint.def"))
    sig0 = _wire_signature(s, ctx.path("pre_eco.def"))
    eco_ops.remove_fillers(s)
    touched: set[str] = set()
    for op in ops:
        touched |= apply_op(s, op)
    ctx.outputs["reabut"] = steps.reabut_all(ctx)  # a resized datapath cell re-abuts its row
    place_ops.detailed_placement(s)
    fences = [g.name for g in d.groups if g.kind == "fence"]
    ctx.outputs["fence_evicted"] = steps.enforce_fences(s, fences)
    steps.check_placement(ctx)
    bad = steps.fence_violations(s, fences)
    if bad:
        raise RuntimeError("fence violations after ECO:\n" + "\n".join(bad[:20]))
    steps.datapath_check(ctx, "after the ECO")
    steps.reconnect_supplies(ctx)

    # equivalence before anything is routed or written
    eco_ops.write_verilog(s, post_v)
    ctx.outputs["equivalence"] = _equivalent(s, pre_v, post_v)

    loc1 = _locations(s)
    moved = {n for n, v in loc1.items() if n in loc0 and loc0[n] != v} | (set(loc1) - set(loc0))
    touched |= _inst_nets(s, moved)
    touched -= set(d.analog_nets)
    ctx.outputs["moved_insts"] = len(moved)
    ctx.outputs["touched_nets"] = len(touched)

    ctx.outputs.update(patch_route(ctx, touched, sig0, analog_before, max_patch, p.get("iters", 64),
                                   p.get("max_drc", 0)))
    eco_ops.fill(s, ctx.pdk)
    steps.check_placement(ctx)
    steps.extract(ctx)
    for f in (pre_v, post_v):
        if not os.path.getsize(f):
            raise RuntimeError(f"{f} empty")


def patch_route(ctx, touched: set[str], sig0: dict, analog_before: str, max_patch: int, iters: int = 64,
                max_drc: int = 0, tag: str = "", retry_full: bool = False) -> dict:
    """Re-route after a netlist/placement change. ``touched`` nets are ripped
    up and routed alone when there are at most ``max_patch`` of them, else all
    digital wiring is ripped up for a full route. Fails on DRC violations,
    unrouted nets or a changed analog net. ``sig0`` is the routed geometry
    before the change (taken after ``wires.normalize``)."""
    s, d = ctx.session, ctx.design
    out = {}
    route_ops.set_routing_layers(s, d, ctx.pdk)
    drc = ctx.path(f"drc{tag}.rpt")
    if len(touched) > max_patch:
        out["route_mode"] = "full"
        out["ripped"] = _destroy_wires(s)
        r = eco_ops.route_all_digital(s, drc, iters)
    else:
        out["route_mode"] = "incremental"
        out["ripped"] = _destroy_wires(s, touched)
        r = eco_ops.route_nets(s, touched, drc, iters)
    nums = steps._drt_numbers(r.output)
    if "drc" not in nums:
        raise RuntimeError("detailed_route reported no violation count")
    ctx.metrics.update(nums)
    if nums["drc"] > max_drc:
        raise RuntimeError(f"{nums['drc']} DRC violations after patch route{tag}")
    frozen = list(d.analog_nets) + list(d.analog_supplies)
    if frozen and analog.signature(s, frozen) != analog_before:
        raise RuntimeError("patch route modified frozen analog nets")
    sig1 = _wire_signature(s, ctx.path(f"post_route{tag}.def"))
    # re-routed = wires or vias differ; patch-only differences (the router
    # regenerating its minimum-area RECTs on a net it did not re-route) are not
    def routing(sig):
        return sig[:2] if sig else sig
    changed = {n for n in set(sig0) | set(sig1) if routing(sig0.get(n)) != routing(sig1.get(n))}
    out_patch_only = sorted(n for n in set(sig0) | set(sig1)
                            if n not in changed and sig0.get(n) != sig1.get(n))
    out["nets_rerouted"] = len(changed)
    out["nets_patch_only"] = len(out_patch_only)
    collateral = sorted(changed - touched)
    out["collateral_nets"] = collateral  # neighbours the router ripped up and re-routed
    if out["route_mode"] == "incremental" and len(collateral) > max_patch:
        raise RuntimeError(f"incremental route re-routed {len(collateral)} nets outside the patch")
    # nets without wiring, with their terminal counts: one terminal needs no wire
    unrouted = eco_ops.unwired_digital(s)
    out["unwired_single_terminal"] = [n for n, i, b in unrouted if int(i) + int(b) < 2]
    unrouted = [(n, int(i), int(b)) for n, i, b in unrouted if int(i) + int(b) >= 2]
    if unrouted:
        msg = (f"{len(unrouted)} nets unrouted after patch route{tag} (net, instance pins, ports): {unrouted[:8]}")
        if out["route_mode"] == "incremental" and retry_full:
            # the detailed router ripped up neighbours that had no global-route guides and could not route
            # them back; a second global route in this process fails (GRT-0229): a fresh attempt routes fully
            from .scheduler import RetryWith
            raise RetryWith({"max_patch_nets": 0}, msg)
        raise RuntimeError(msg)
    from .minarea import repair as _min_area
    out["min_area"] = _min_area(ctx, ctx.path(f"minarea{tag}.def"))
    return out


def _masters(session) -> dict[str, str]:
    return eco_ops.masters(session)


def _slacks(session):
    v = db_ops.worst_slacks(session)
    return tuple(float(x) if abs(float(x)) < 1e30 else None for x in v)


def patch_limit(session, max_patch: int | None) -> int:
    """Nets a repair may change and still be patched: ``max_patch``, or by
    default a quarter of the digital nets (at least 40). A fixed 40 sent every
    larger repair to a full re-route (aes: ~2,300 s of the post-route step)."""
    if max_patch is not None:
        return int(max_patch)
    n = eco_ops.digital_net_count(session)
    return max(40, n // 4)


def drv_drivers(session, report: str) -> list[tuple[str, str, list[str]]]:
    """(instance, master, liberty cell names) of the drivers of the
    standard-cell pins in ``report_check_types -violators`` output (slew and
    capacitance sections); each driver once."""
    pins, kind = [], None
    with open(report) as f:
        for line in f:
            s_ = line.strip()
            if s_ in ("max slew", "max capacitance", "max fanout"):
                kind = s_
            elif kind in ("max slew", "max capacitance") and s_.endswith("(VIOLATED)") and "/" in s_.split()[0]:
                pins.append(s_.split()[0])
    if not pins:
        return []
    drivers, cells = eco_ops.drv_driver_list(session, pins)
    from .tools.openroad.tcl import tcl_words
    names = sorted(set(tcl_words(cells)))
    return [(*tcl_words(w), names) for w in tcl_words(drivers)]


def upsize(master: str, cells: list[str], excluded=lambda c: False) -> str | None:
    """The next drive strength of ``master`` (same name, larger ``_<n>``
    suffix: ``buf_1`` -> ``buf_2``) among ``cells``, not
    ``excluded``; None at the largest."""
    import re
    m = re.fullmatch(r"(.*_)(\d+)", master)
    if not m:
        return None
    base, n = m.group(1), int(m.group(2))
    up = sorted((int(c[len(base):]), c) for c in cells
                if c.startswith(base) and c[len(base):].isdigit() and int(c[len(base):]) > n and not excluded(c))
    return up[0][1] if up else None


def drv_upsize(ctx, report: str) -> list[list[str]]:
    """Drivers of nets with standard-cell slew or capacitance violations on
    extracted parasitics one drive strength up (``replace_cell``; PDK and
    design exclusions honoured). Returns [instance, from, to] per change."""
    import fnmatch
    s, pdk, d = ctx.session, ctx.pdk, ctx.design
    eco_ops.report_slew_cap_violators(s, report)
    pats = list(pdk.dont_use) + list(getattr(d, "dont_use", []) or [])

    def excluded(c):
        return pdk.excluded(c, pats) or any(fnmatch.fnmatchcase(c, p) for p in pats)
    out = []
    for inst, master, cells in drv_drivers(s, report):
        new = upsize(master, cells, excluded)
        if new:
            eco_ops.replace_with_lib_cell(s, inst, new)
            out.append([inst, master, new])
    return out


def post_route_repair(ctx, max_iter: int = 2, max_patch: int | None = None, drv_sizing: bool = False) -> dict:
    """Timing repair on extracted parasitics after detailed routing.

    While setup or hold fails (all corners): repair_timing (resize/buffer only:
    no pin swap, gate cloning or buffer removal, so the structural equivalence
    check applies), legalize, check equivalence, re-route the changed and moved
    instances' nets (``patch_route``), extract again. With ``drv_sizing``,
    also while standard cells see slew or capacitance violations: their nets'
    drivers one drive strength up (``drv_upsize``) in the same round. Stops
    when timing is met (and no driver was sized), nothing changes, after
    ``max_iter`` rounds, or after one re-route (this OpenROAD build's global
    router cannot route twice in one process).

    On a routed design repair_timing re-estimates the nets it changes with
    the incremental global router, rebuilt from the routes; this build can fail
    there when a resized cell's pin leaves its net's guides (aes with the
    typical-corner netlist: GRT-0304, then GRT-0229 with usage 65534), and no
    parasitics source (placement, detailed_routing, guides cleared) keeps the
    resizer from it. The attempt then ends with RetryWith and a fresh one runs
    without post-route timing repair (``rounds: 0``): the step completes and
    signoff judges the timing."""
    s, d, pdk = ctx.session, ctx.design, ctx.pdk
    history = []
    if max_iter < 1:
        wns, whs = _slacks(s)
        return {"rounds": [{"wns": wns, "whs": whs, "note": "post-route timing repair off (rounds 0)"}]}
    rerouted = False
    max_patch = patch_limit(s, max_patch)
    for it in range(1, max_iter + 1):
        wns, whs = _slacks(s)
        history.append({"wns": wns, "whs": whs})
        timing_met = (wns is None or wns >= 0) and (whs is None or whs >= 0)
        if timing_met and not drv_sizing:
            break
        if rerouted:
            # this global router keeps the previous global_route's edge usage:
            # a second one in the same process fails (GRT-0229, usage 65535)
            history[-1]["note"] = "stopped: one re-route per process (run route_repair again for more)"
            break
        pre_v, post_v = ctx.path(f"pre_repair{it}.v"), ctx.path(f"post_repair{it}.v")
        eco_ops.write_verilog(s, pre_v)
        loc0, m0 = _locations(s), _masters(s)
        frozen = list(d.analog_nets) + list(d.analog_supplies)
        analog_before = analog.signature(s, frozen) if frozen else ""
        upsized = drv_upsize(ctx, ctx.path(f"repair{it}_drv.rpt")) if drv_sizing else []
        if upsized:
            history[-1]["drv_upsized"] = upsized
        elif timing_met:
            history[-1]["note"] = "timing met, no driver to size"
            break
        wires.normalize(s, ctx.path(f"repair{it}_checkpoint.def"))
        sig0 = _wire_signature(s, ctx.path(f"repair{it}_pre.def"))
        if not timing_met:
            # setup: sizing only (post-route setup buffering crashes this OpenROAD build,
            # and perturbs routes most); hold: buffering allowed (hold needs delay cells)
            try:
                eco_ops.repair_timing_routed(s, pdk)
            except TclError as e:
                if "GRT-" in str(e):  # the resizer's incremental global router failed
                    from .scheduler import RetryWith
                    raise RetryWith({"rounds": 0}, "post-route timing repair failed in the incremental global "
                                    f"router: {str(e).splitlines()[0]}") from e
                raise
        steps.reabut_all(ctx)
        place_ops.detailed_placement(s)
        fences = [g.name for g in d.groups if g.kind == "fence"]
        steps.enforce_fences(s, fences)
        steps.check_placement(ctx)
        bad = steps.fence_violations(s, fences)
        if bad:
            raise RuntimeError("fence violations after post-route repair:\n" + "\n".join(bad[:20]))
        steps.datapath_check(ctx, "after post-route repair")
        loc1, m1 = _locations(s), _masters(s)
        changed = {n for n in m1 if m0.get(n) != m1[n]}
        moved = {n for n, v in loc1.items() if loc0.get(n) != v}
        if not changed and not moved:
            history[-1]["note"] = "repair_timing changed nothing"
            break
        eco_ops.write_verilog(s, post_v)
        _equivalent(s, pre_v, post_v)
        touched = (_inst_nets(s, changed | moved)) - set(d.analog_nets)
        try:
            r = patch_route(ctx, touched, sig0, analog_before, max_patch, tag=f"_repair{it}", retry_full=True)
        except TclError as e:  # the router itself failed in the patch (e.g. DRT-0206 checkConnectivity)
            if len(touched) <= max_patch:
                from .scheduler import RetryWith
                raise RetryWith({"max_patch_nets": 0}, f"patch route failed: {str(e).splitlines()[0]}") from e
            raise
        rerouted = True
        history[-1].update({"resized_or_added": len(changed), "moved": len(moved), "touched_nets": len(touched),
                            "route_mode": r["route_mode"]})
        steps.run_rcx(ctx, ctx.path("route_rcx.spef"), tag=f"repair{it}_rcx")
    else:
        wns, whs = _slacks(s)
        history.append({"wns": wns, "whs": whs})
    return {"rounds": history}
