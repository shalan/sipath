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

from . import analog, equiv, liberty, steps, tcl, wires
from .netlist import read_verilog
from .tcl import q, qlist

LOCATIONS_TCL = """
set out {}
foreach inst [[ord::get_db_block] getInsts] {
  set t [[$inst getMaster] getType]
  if {$t eq "CORE_SPACER"} continue
  set l [$inst getLocation]
  lappend out [$inst getName] [lindex $l 0] [lindex $l 1] [$inst getOrient]
}
set out
"""


def _locations(session) -> dict[str, tuple]:
    f = session.value(LOCATIONS_TCL).split()
    return {f[i]: (f[i + 1], f[i + 2], f[i + 3]) for i in range(0, len(f), 4)}


def _inst_nets(session, insts) -> set[str]:
    if not insts:
        return set()
    out = session.value(f"""
set out {{}}
foreach n {qlist(sorted(insts))} {{
  set i [[ord::get_db_block] findInst $n]
  if {{$i eq "NULL"}} continue
  foreach it [$i getITerms] {{
    set net [$it getNet]
    if {{$net eq "NULL" || [$net isSpecial] || [$net getSigType] in {{POWER GROUND}}}} continue
    lappend out [$net getName]
  }}
}}
join $out \\n""")
    return {line for line in out.splitlines() if line}


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
        session.run(f"::flow_inst {q(op['inst'])}\nreplace_cell {q(op['inst'])} {q(op['master'])}")
        # replace_cell does nothing (no error) when the masters' pins differ
        now = session.value(f"[[::flow_inst {q(op['inst'])}] getMaster] getName")
        if now != op["master"]:
            raise RuntimeError(f"resize of {op['inst']} to {op['master']} did not happen (still {now}): "
                               "replace_cell needs a master with the same pins")
        return nets
    if kind == "buffer":
        before = set(session.value("set o {}; foreach n [[ord::get_db_block] getNets] { lappend o [$n getName] }; "
                                   "join $o \\n").splitlines())
        args = [f"-buffer_cell {op['master']}", f"-net {q(op['net'])}"]
        if op.get("loads"):
            args.append(f"-load_pins [get_pins {qlist(op['loads'])}]")
        if op.get("name"):
            args.append(f"-buffer_name {q(op['name'])}")
        session.run(f"::flow_net {q(op['net'])}\ninsert_buffer {' '.join(args)}")
        after = set(session.value("set o {}; foreach n [[ord::get_db_block] getNets] { lappend o [$n getName] }; "
                                  "join $o \\n").splitlines())
        return {op["net"], *(after - before)}
    raise ValueError(f"unknown ECO op {kind!r}")


def _destroy_wires(session, nets=None) -> int:
    sel = "1" if nets is None else f"[lsearch -exact $keep [$net getName]] >= 0"
    keep = f"set keep {qlist(sorted(nets))}" if nets is not None else "set keep {}"
    return int(session.value(f"""
{keep}
set n 0
foreach net [[ord::get_db_block] getNets] {{
  if {{[$net isSpecial] || [$net getSigType] in {{POWER GROUND ANALOG}}}} continue
  if {{!({sel})}} continue
  set w [$net getWire]
  if {{$w ne "NULL"}} {{ odb::dbWire_destroy $w; incr n }}
  $net clearGuides
}}
set n"""))


def _wire_signature(session, path) -> dict[str, tuple]:
    """Per-net routed geometry from a DEF written now."""
    session.run(f"write_def {q(path)}")
    with open(path) as f:
        nets = wires.parse_def_paths(f.read())
    names = sorted({it[1] for i in nets.values() for p in i["paths"] for it in p["items"] if it[0] == "via"})
    flat = session.value(analog.query_via_layers_tcl(names)).split() if names else []
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
    s.run(f"write_verilog {q(pre_v)}")
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
    s.run("remove_fillers")
    touched: set[str] = set()
    for op in ops:
        touched |= apply_op(s, op)
    ctx.outputs["reabut"] = steps.reabut_all(ctx)  # a resized datapath cell re-abuts its row
    s.run("detailed_placement")
    fences = [g.name for g in d.groups if g.kind == "fence"]
    ctx.outputs["fence_evicted"] = steps.enforce_fences(s, fences)
    steps.check_placement(ctx)
    bad = steps.fence_violations(s, fences)
    if bad:
        raise RuntimeError("fence violations after ECO:\n" + "\n".join(bad[:20]))
    steps.datapath_check(ctx, "after the ECO")
    steps.reconnect_supplies(ctx)

    # equivalence before anything is routed or written
    s.run(f"write_verilog {q(post_v)}")
    ctx.outputs["equivalence"] = _equivalent(s, pre_v, post_v)

    loc1 = _locations(s)
    moved = {n for n, v in loc1.items() if n in loc0 and loc0[n] != v} | (set(loc1) - set(loc0))
    touched |= _inst_nets(s, moved)
    touched -= set(d.analog_nets)
    ctx.outputs["moved_insts"] = len(moved)
    ctx.outputs["touched_nets"] = len(touched)

    ctx.outputs.update(patch_route(ctx, touched, sig0, analog_before, max_patch, p.get("iters", 64),
                                   p.get("max_drc", 0)))
    s.run(tcl.filler(ctx.pdk))
    steps.check_placement(ctx)
    steps.extract(ctx)
    for f in (pre_v, post_v):
        if not os.path.getsize(f):
            raise RuntimeError(f"{f} empty")


def patch_route(ctx, touched: set[str], sig0: dict, analog_before: str, max_patch: int, iters: int = 64,
                max_drc: int = 0, tag: str = "") -> dict:
    """Re-route after a netlist/placement change. ``touched`` nets are ripped
    up and routed alone when there are at most ``max_patch`` of them, else all
    digital wiring is ripped up for a full route. Fails on DRC violations,
    unrouted nets or a changed analog net. ``sig0`` is the routed geometry
    before the change (taken after ``wires.normalize``)."""
    s, d = ctx.session, ctx.design
    out = {}
    s.run(tcl.routing_layers(d, ctx.pdk))
    drc = ctx.path(f"drc{tag}.rpt")
    if len(touched) > max_patch:
        out["route_mode"] = "full"
        out["ripped"] = _destroy_wires(s)
        r = s.run(tcl.route_digital(drc, iters))  # config.command_timeout
    else:
        out["route_mode"] = "incremental"
        out["ripped"] = _destroy_wires(s, touched)
        r = s.run(tcl.route_subset(sorted(touched), drc, iters))  # config.command_timeout: a hang fails the attempt
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
    unrouted = s.value(tcl.DIGITAL_NETS + """
set n 0
foreach name $::flow_digital { if {[[::flow_net $name] getWire] eq "NULL"} { incr n } }
set n""")
    if int(unrouted):
        raise RuntimeError(f"{unrouted} nets unrouted after patch route{tag}")
    from .minarea import repair as _min_area
    out["min_area"] = _min_area(ctx, ctx.path(f"minarea{tag}.def"))
    return out


MASTERS_TCL = """
set out {}
foreach inst [[ord::get_db_block] getInsts] {
  if {[[$inst getMaster] getType] eq "CORE_SPACER"} continue
  lappend out [$inst getName] [[$inst getMaster] getName]
}
set out
"""


def _masters(session) -> dict[str, str]:
    f = tcl.tcl_words(session.value(MASTERS_TCL))
    return dict(zip(f[0::2], f[1::2]))


def _slacks(session):
    v = tcl.tcl_words(session.value("list [sta::worst_slack -max] [sta::worst_slack -min]"))
    return tuple(float(x) if abs(float(x)) < 1e30 else None for x in v)


def post_route_repair(ctx, max_iter: int = 2, max_patch: int = 40) -> dict:
    """Timing repair on extracted parasitics after detailed routing.

    While setup or hold fails (all corners): repair_timing (resize/buffer only:
    no pin swap, gate cloning or buffer removal, so the structural equivalence
    check applies), legalize, check equivalence, re-route the changed and moved
    instances' nets (``patch_route``), extract again. Stops when timing is met,
    nothing changes, after ``max_iter`` rounds, or after one re-route (this
    OpenROAD build's global router cannot route twice in one process)."""
    s, d, pdk = ctx.session, ctx.design, ctx.pdk
    history = []
    rerouted = False
    for it in range(1, max_iter + 1):
        wns, whs = _slacks(s)
        history.append({"wns": wns, "whs": whs})
        if (wns is None or wns >= 0) and (whs is None or whs >= 0):
            break
        if rerouted:
            # this global router keeps the previous global_route's edge usage:
            # a second one in the same process fails (GRT-0229, usage 65535)
            history[-1]["note"] = "stopped: one re-route per process (run route_repair again for more)"
            break
        pre_v, post_v = ctx.path(f"pre_repair{it}.v"), ctx.path(f"post_repair{it}.v")
        s.run(f"write_verilog {q(pre_v)}")
        loc0, m0 = _locations(s), _masters(s)
        frozen = list(d.analog_nets) + list(d.analog_supplies)
        analog_before = analog.signature(s, frozen) if frozen else ""
        wires.normalize(s, ctx.path(f"repair{it}_checkpoint.def"))
        sig0 = _wire_signature(s, ctx.path(f"repair{it}_pre.def"))
        # setup: sizing only (post-route setup buffering crashes this OpenROAD build,
        # and perturbs routes most); hold: buffering allowed (hold needs delay cells)
        s.run("repair_timing -setup -repair_tns 100 -skip_pin_swap -skip_gate_cloning -skip_buffer_removal "
              "-skip_buffering" + tcl.margins(pdk))
        s.run("repair_timing -hold -skip_pin_swap -skip_gate_cloning -skip_buffer_removal"
              + tcl.margins(pdk, "hold_route"))
        steps.reabut_all(ctx)
        s.run("detailed_placement")
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
        s.run(f"write_verilog {q(post_v)}")
        _equivalent(s, pre_v, post_v)
        touched = (_inst_nets(s, changed | moved)) - set(d.analog_nets)
        r = patch_route(ctx, touched, sig0, analog_before, max_patch, tag=f"_repair{it}")
        rerouted = True
        history[-1].update({"resized_or_added": len(changed), "moved": len(moved), "touched_nets": len(touched),
                            "route_mode": r["route_mode"]})
        steps.run_rcx(ctx, ctx.path("route_rcx.spef"), tag=f"repair{it}_rcx")
    else:
        wns, whs = _slacks(s)
        history.append({"wns": wns, "whs": whs})
    return {"rounds": history}
