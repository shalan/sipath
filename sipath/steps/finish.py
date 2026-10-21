"""Steps ``fill``, ``extract``, ``signoff``, ``views`` (hardened macro views), ``integrate`` (the
wrapper in its frame) and ``ppa`` (explore mode)."""
from __future__ import annotations

import json
import os
import re

from .. import analog, params
from ..tools.openroad import heatmap as heatmaps
from ..tools.openroad import tcl
from ..tools.openroad.ops import finish as finish_ops
from .common import _timing_gate, grid_nets, power_checks, ppa_summary, reports, run_rcx
from .analog_route import analog_coupling


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
    finish_ops.density_fill(s, rules, area)
    per = finish_ops.fill_counts(s)
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
    finish_ops.write_abstract_lef(s, lef, p.get("bloat_occupied_layers", True))
    v, pv = os.path.join(out, f"{top}.v"), os.path.join(out, f"{top}.pwr.v")
    finish_ops.write_netlists(s, v, pv)
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
        finish_ops.read_spef(s, spefs[c], c if multi else None)
        libs[c] = os.path.join(out, f"{top}__{c}.lib")
        finish_ops.write_timing_model(s, libs[c], top + "__" + c, top, c if multi else None)
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
    ports = finish_ops.signal_ports(s)
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
    lib_map = {c: rel(f) for c, f in libs.items()}
    # also keyed by process (the first corner of each): a parent's other corners (e.g. ss_nom
    # when the views have ss_max) find a model of their process
    for c in ctx.pdk.analysis_corners():
        if c.name in libs and getattr(c, "process", "") and c.process not in lib_map:
            lib_map[c.process] = rel(libs[c.name])
    entry = {"inst": "<instance name>", "lef": rel(lef), "gds": rel(gds), "lib": lib_map,
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
    from .. import harness, signoff
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
    seal = None
    if ctx.params.get("seal_ring"):
        seal = h.seal_ring(work)
        if not seal:
            raise ValueError(f"harness {d.harness} gives no seal ring (frame.seal_ring)")
    out = signoff._magic(ctx, h.integrate_script(work, wrapper_gds, chip), "integrate")
    if "SIPATH_DONE" not in out or not os.path.isfile(chip) or os.path.getsize(chip) == 0:
        raise RuntimeError(f"Magic wrote no chip GDS (log {ctx.path('integrate.log')})")
    missing = sorted(set(re.findall(r"Cell (\S+) is unavailable", out)))
    if missing:
        raise RuntimeError(f"frame cells without a view: {missing[:10]} (log {ctx.path('integrate.log')})")
    kdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "klayout")
    if seal:  # centred on the die (its opening is smaller than the die: it sits on the die's outer band)
        scfg = ctx.path("sealring.cfg.json")
        with open(scfg, "w") as fh:
            _json.dump({"chip": chip, "top": top, "die": f["die"], "seal": seal, "out": ctx.path("sealring.json")}, fh)
        signoff._runner(ctx).mount(kdir)
        signoff.run_tool(ctx, ["klayout", "-b", "-rd", f"cfg_file={scfg}", "-r", os.path.join(kdir, "sealring.py")],
                         ctx.path("sealring.log"))
        with open(ctx.path("sealring.json")) as fh:
            ctx.outputs["seal_ring"] = _json.load(fh)
    script = os.path.join(kdir, "chipcheck.py")
    cfg = ctx.path("chipcheck.cfg.json")
    with open(cfg, "w") as fh:
        _json.dump({"chip": chip, "top": top, "die": f["die"], "wrapper": wrapper_gds,
                    "wrapper_cell": f["magic"]["wrapper_cell"], "origin": f["wrapper_origin"],
                    "seal_cell": h.seal_cell(seal) if seal else None,
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


integrate.tool = True  # Magic and KLayout only: no OpenROAD process


def precheck(ctx):
    """ChipFoundry's tapeout precheck (cf-precheck, sipath.precheck) on the
    signed-off wrapper of a harness design, in the precheck image: the project
    directory exported from the run, the harness's pinned golden root, the
    run's PDK tree. Results per check in ``outputs["precheck"]``; a failed
    check fails the step (``fail_on_violation``)."""
    from .. import harness, precheck as pc, signoff, toolchain
    d, p = ctx.design, ctx.params
    if not d.harness:
        raise ValueError("precheck needs a harness design (harness: ...)")
    h = harness.get(d.harness, d.harness_options)
    golden = (h.spec.get("precheck") or {}).get("golden")
    if not golden:
        raise ValueError(f"harness {d.harness} gives no precheck golden files (precheck.golden)")
    image = getattr(ctx.config, "precheck_image", "") or toolchain.lock()["images"].get("precheck", {}).get("ref")
    if not image:
        raise RuntimeError("no precheck image: build docker/precheck and set precheck_image")
    gds, netlist = ctx.ancestor_output("gds"), ctx.ancestor_output("verilog_pwr")
    for what, f in (("wrapper GDS", gds), ("netlist with supply pins", netlist)):
        if not (f and os.path.isfile(f)):
            raise RuntimeError(f"precheck needs the signed-off {what} (run signoff first)")
    wrapper = h.spec["wrapper"]
    project = ctx.path("project")
    ctx.outputs["precheck_project"] = pc.export_project(project, wrapper, gds, netlist)
    root = pc.golden_root(golden if os.path.isabs(golden) else os.path.join(h.root, golden), ctx.path("golden"))
    tree = (ctx.pdk.release or {}).get("tree")
    if not tree:
        raise RuntimeError(f"PDK {ctx.pdk.name} names no PDK tree (release.tree) for cf-precheck's -p")
    out = ctx.path("precheck_results")
    signoff._runner(ctx).mount(tree, project, root, out if os.path.isdir(out) else ctx.dir)
    try:
        signoff.run_tool(ctx, pc.command(project, os.path.realpath(tree), root, out, p.get("skip") or [],
                                         bool(p.get("magic_drc"))),
                         ctx.path("precheck.log"), image=image, env={"PDK_ROOT": os.path.dirname(os.path.realpath(tree))})
    except signoff.SignoffError as e:  # a failing check exits non-zero: its results are still read below
        ctx.outputs["precheck_exit"] = str(e).splitlines()[0]
    res = pc.results(project)
    ctx.outputs["precheck"] = res
    if res.get("file"):
        ctx.outputs["checkpoint"] = res["file"]  # cf-precheck's own results: the step's result file
    ctx.metrics["precheck_pass"] = res["pass"]
    ctx.metrics["precheck_failed"] = len(res.get("failed", []))
    if not res["pass"] and p.get("fail_on_violation", True):
        raise RuntimeError(f"precheck failed: {res.get('error') or res.get('failed')} ({res.get('file') or project})")


precheck.tool = True  # cf-precheck and its tools in their image: no OpenROAD process


def check_step(name: str):
    """A step running the signoff checks ``params.CHECK_STEPS[name]`` alone
    (signoff.json and the reports as the signoff step writes them)."""
    checks = list(params.CHECK_STEPS[name])

    def run(ctx):
        from .. import signoff as so
        ctx.params = dict(ctx.params, checks=checks)
        ctx.outputs["signoff_summary"] = so.run(ctx)
    run.__name__ = name
    run.__doc__ = f"Signoff check step: {', '.join(checks)} on the parent's final database (see sipath.signoff)."
    return run


def ppa(ctx):
    """PPA summary (first cut) on the best parasitics the parent left: its
    extracted SPEF when there is one (typical corner), else placement
    estimates. params: margin (suggested period = achievable x (1 + margin))."""
    spefs = ctx.ancestor_output("spef_corners") or {}
    typ = ctx.pdk.typical().name
    if spefs.get(typ):
        multi = len(ctx.pdk.analysis_corners()) > 1
        finish_ops.load_extracted_parasitics(ctx.session, spefs[typ], typ if multi else None)
        ppa_summary(ctx, "extracted")
    else:
        finish_ops.timing_prep(ctx.session)
        ppa_summary(ctx, "placement-estimate")

def extract(ctx):
    s = ctx.session
    heatmaps.schedule(ctx, ctx.params.get("heatmaps"))  # collected after the checkpoint is saved
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
    if nets:
        analog_coupling(ctx, [spef, *corner_spefs.values()])

def signoff(ctx):
    """Physical (GDS, DRC, LVS, antennas) and timing (independent STA) signoff
    of the parent's final database (see sipath.signoff). params: checks, require,
    fail_on_violation (default True)."""
    from .. import signoff as so
    ctx.outputs["signoff_summary"] = so.run(ctx)
