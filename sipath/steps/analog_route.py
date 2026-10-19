"""Step ``route_analog``: analog nets and supplies routed first with their NDRs, frozen as special
wiring, guarded and optionally shielded; and the analog coupling report ``extract`` writes."""
from __future__ import annotations

import json

from .. import analog, tcl
from ..tcl import q, tcl_words
from .common import _drt_numbers, _no_supply_shorts, _via_array, check_placement, macro_supply_pins


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
    from .. import powerconnect
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

def shield_nets(ctx, sh_rules) -> dict:
    """Grounded shield wires beside the analog_shield nets (sipath.shield),
    drawn as special wiring of the shield net once the analog nets are frozen;
    with analog_keepaway, obstructions on the layers above and below them.
    Fails on a short between special nets; reports coverage per net."""
    from .. import shield
    d, s = ctx.design, ctx.session
    rules, vias = sh_rules
    layers = list(ctx.pdk.layers)
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    net = d.analog_shield_net or d.net("VSS")
    special, insts, bterms, core_top = shield.dump(s)
    lo, hi = layers.index(d.min_route_layer), layers.index(d.max_route_layer)
    # above the standard cells' own shapes (a layer the PDK does not route lies below its routing layers)
    core_top, core_cell = core_top
    cell_top = layers.index(core_top) if core_top in layers else -1
    allowed = {ly for k, ly in enumerate(layers) if k > cell_top and lo <= k <= hi}
    ctx.outputs["analog_shield_layers"] = sorted(allowed, key=layers.index)
    ctx.outputs["standard_cell_top_layer"] = [core_top, core_cell] if core_top else None
    straps = allowed - set(d.analog_row_blocked_layers)  # straps cross rows: never on the pin-access layers
    plan = shield.plan(list(d.analog_shield), net, special, insts, bterms, rules, layers, allowed, vias,
                       strap_layers=straps, reach=int(round(float(ctx.params.get("shield_tie_um", 30.0)) * dbu)))
    ctx.outputs["analog_shield_shapes"] = shield.draw(s, net, plan, rules.grid)
    rep = shield.summary(plan, dbu)
    ctx.outputs["analog_shield"] = rep
    ctx.metrics["analog_shield_coverage"] = round(min(r["coverage"] for r in rep.values()), 4)
    if d.analog_keepaway:
        barred = set(d.analog_row_blocked_layers) | {ly for k, ly in enumerate(layers) if not lo <= k <= hi}
        special = shield.dump(s)[0]
        boxes = shield.keepaway(set(d.analog_shield), special, insts, bterms, rules, layers, barred)
        ctx.outputs["analog_keepaway_obstructions"] = shield.obstruct(s, boxes)
    _no_supply_shorts(ctx, layers)
    return rep

def route_analog(ctx):
    d, s = ctx.design, ctx.session
    if not d.analog_nets and not d.analog_supplies:
        return
    s.run(tcl.analog_ndr(d))
    from .. import shield
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    sh_rules = shield.tech(s, list(ctx.pdk.layers)) if d.analog_nets else None
    ndr_sp = {n: dict(d.analog_ndr["spacing"]) for n in d.analog_nets}  # um, per net and layer
    if d.analog_shield:  # room for a shield on each side of the shielded nets
        sp = shield.ndr_spacing(sh_rules[0], d.analog_ndr, dbu)
        w = " ".join(f"{k} {v}" for k, v in d.analog_ndr["width"].items() if k in sp)
        s.run(f"create_ndr -name ANALOG_SH -width {{{w}}} -spacing {{{' '.join(f'{k} {v}' for k, v in sp.items())}}}")
        for n in d.analog_shield:
            s.run(f"assign_ndr -ndr ANALOG_SH -net {q(n)}")
            ndr_sp[n] = dict(sp)
        ctx.outputs["analog_shield_ndr_spacing_um"] = sp
    s.run(tcl.routing_layers(d, ctx.pdk))
    supplies = list(d.analog_supplies)
    if supplies:
        w = " ".join(f"{k} {v}" for k, v in d.analog_supply_ndr["width"].items())
        sp = " ".join(f"{k} {v}" for k, v in d.analog_supply_ndr["spacing"].items())
        s.run(f"create_ndr -name ASUPPLY -width {{{w}}} -spacing {{{sp}}}")
        from .. import chip
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
    if d.analog_shield:
        shield_nets(ctx, sh_rules)
    if d.analog_nets and ctx.params.get("guard", True):
        special, insts, bterms, _ = shield.dump(s)
        boxes = shield.guard({n: {ly: int(round(v * dbu)) for ly, v in sp.items()} for n, sp in ndr_sp.items()},
                             special, insts, bterms, sh_rules[0])
        ctx.outputs["analog_guard_obstructions"] = shield.obstruct(s, boxes)
    if supplies:  # strap each supply pad's tab to its routed wire
        from .. import powerconnect
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

def analog_coupling(ctx, spefs: list[str]) -> dict:
    """Coupling onto the analog nets from the SPEFs (analog_coupling.json /
    .md): metrics analog_coupling_ff (worst net's digital coupling) and
    analog_aggressor_ff (largest single digital aggressor); the limits
    max_analog_coupling_ff / max_analog_aggressor_ff fail the attempt."""
    import json
    rep = analog.coupling_report(list(dict.fromkeys(spefs)), ctx.design.analog_nets)
    path = ctx.path("analog_coupling.json")
    with open(path, "w") as f:
        json.dump(rep, f, indent=1)
    lines = ["| analog net | total (fF) | from digital (fF) | largest aggressors (fF) | other analog nets (fF) |",
             "|---|---|---|---|---|"]
    for n, r in rep.items():
        ag = ", ".join(f"{o} {v:.2f}" for o, v in r["aggressors"][:5]) or "-"
        an = ", ".join(f"{o} {v:.2f}" for o, v in r["analog_ff"].items()) or "-"
        lines.append(f"| {n} | {r['total_ff']:.2f} | {r['digital_ff']:.2f} | {ag} | {an} |")
    with open(ctx.path("analog_coupling.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    ctx.outputs["analog_coupling"] = path
    worst = max(rep.items(), key=lambda kv: kv[1]["digital_ff"])
    aggr = max(((n, *r["aggressors"][0]) for n, r in rep.items() if r["aggressors"]), key=lambda t: t[2],
               default=None)
    ctx.metrics["analog_coupling_ff"] = float(worst[1]["digital_ff"])
    ctx.metrics["analog_aggressor_ff"] = aggr[2] if aggr else 0.0
    bad = []
    lim = ctx.params.get("max_analog_coupling_ff")
    if lim is not None:
        bad += [f"{n}: {r['digital_ff']:.2f} fF from digital nets (> {lim:g})" for n, r in rep.items()
                if r["digital_ff"] > lim]
    lim = ctx.params.get("max_analog_aggressor_ff")
    if lim is not None:
        bad += [f"{n}: {o} couples {v:.2f} fF (> {lim:g})" for n, r in rep.items() for o, v in r["aggressors"]
                if v > lim]
    if bad:
        raise RuntimeError(f"analog coupling over the limit ({path}):\n  " + "\n  ".join(bad))
    return rep
