"""Design description files (YAML or JSON) and running them.

    python3 -m sipath run path/to/design.yaml [workdir]

A design file holds the ``Design`` fields (netlist, SDC, mode, floorplan DEF or
die + pads, macros, groups, datapaths, analog nets, PDN, ...), the PDK to use
(``pdk``: a name from ``sipath/pdks`` or a file) and, optionally, the steps to
run (``flow``: a list of ``{step, variants, score, parallel}``; each step
continues from the previous step's selected attempt). Relative paths are
relative to the file. ``inputs.generate: module:function`` names a function
that (re)writes generated inputs before they are checked (the examples
generate their netlists); every input must exist afterwards.

Fields the file leaves out take the Design defaults, and PDK-dependent ones
(routing layers, PDN, analog rules, IO views) the PDK's.
"""
from __future__ import annotations

import importlib
import os

from . import steps as steps_mod
from .design import Datapath, Design, Group, Macro, Pad, PdnSpec, Strap
from .padplan import PadRules
from .pdk import load as load_pdk, read_file

PATH_FIELDS = ("netlist", "sdc", "floorplan_def")
SIMPLE = ("name", "top", "mode", "die", "core_margin", "analog_nets", "analog_ndr", "analog_row_blocked_layers",
          "is_core", "min_route_layer", "max_route_layer", "clock_ports", "clock_nets", "keepouts", "pad_supplies",
          "pad_kinds", "analog_supplies", "analog_supply_layers", "analog_supply_ndr", "macro_channel",
          "analog_shield", "analog_shield_net", "analog_keepaway",
          "macro_pin_clearance", "analog_guard", "layer_adjustments", "pad_masters", "pdk", "dont_use",
          "allow_excluded", "step_defaults", "rtl_defines", "core_utilization", "aspect_ratio", "auto_blackbox",
          "plugins", "extensions", "supply_names", "harness", "harness_options")


def default_flow(d: Design) -> list[dict]:
    chain = (["synth"] if d.rtl else []) + ["floorplan"] + (["macros"] if d.macros else []) + [
        "physical", "place", "cts", "route_analog", "route_digital", "route_repair", "extract"]
    return [{"step": s} for s in chain]


def _path(base, p):
    """Absolute and normalized: OpenROAD may run in a container that mounts
    only the normalized directories, where an "a/../b" path cannot resolve."""
    if p is None:
        return p
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))


def _strap(s):
    return s if isinstance(s, Strap) else Strap(**s)


def hardened_entry(vdir: str) -> dict:
    """The macro entry of a views directory (its macro.yaml, paths relative to it)."""
    path = os.path.join(vdir, "macro.yaml")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{vdir}: no macro.yaml (harden the macro first: python3 -m sipath harden ...)")
    macros = read_file(path).get("macros") or []
    if len(macros) != 1:
        raise ValueError(f"{path}: expected one macro entry, found {len(macros)}")
    return macros[0]


def design_from_dict(data: dict, base: str) -> Design:
    from . import registry, schema
    registry.load_plugins(data.get("plugins") or [])  # they may add steps, checks, extension keys
    schema.validate(data)  # unknown keys, wrong types, missing required keys (all listed)
    from . import params
    params.check_defaults("design file", data.get("step_defaults"))
    kw = {k: data[k] for k in SIMPLE if k in data}
    for k in PATH_FIELDS:
        if k in data:
            kw[k] = _path(base, data[k])
    if "die" in kw and kw["die"] is not None:
        kw["die"] = tuple(kw["die"])
    kw["keepouts"] = [tuple(b) for b in kw.get("keepouts", [])]
    kw["extra_lefs"] = [_path(base, p) for p in data.get("extra_lefs", [])]
    kw["extra_libs"] = [_path(base, p) for p in data.get("extra_libs", [])]
    kw["rtl"] = [_path(base, p) for p in data.get("rtl", [])]
    kw["rtl_includes"] = [_path(base, p) for p in data.get("rtl_includes", [])]
    kw.setdefault("netlist", None)
    kw["pads"] = [Pad(**p) for p in data.get("pads", [])]
    macros = []
    for m in data.get("macros", []):
        m = dict(m)
        roots = {k: base for k in ("lef", "gds", "cdl", "lib")}
        if m.get("views"):  # a hardened macro: what the entry leaves out comes from its macro.yaml
            vdir = _path(base, m.pop("views"))
            got = hardened_entry(vdir)
            for k in ("lef", "gds", "cdl", "lib", "lvs", "supplies"):
                if m.get(k) is None and got.get(k) is not None:
                    m[k] = got[k]
                    roots[k] = vdir
        m["lef"] = _path(roots["lef"], m["lef"]) if m.get("lef") else ""  # sram: from the PDK (build)
        for k in ("gds", "cdl"):
            if m.get(k):
                m[k] = _path(roots[k], m[k])
        lib, lb = m.get("lib"), roots["lib"]
        m["lib"] = {c: _path(lb, v) for c, v in lib.items()} if isinstance(lib, dict) else _path(lb, lib)
        if m.get("location") is not None:
            m["location"] = tuple(m["location"])
        if m.get("grid_stripes") is not None:
            m["grid_stripes"] = [_strap(s) for s in m["grid_stripes"]]
        if m.get("grid_connects") is not None:
            m["grid_connects"] = [tuple(c) for c in m["grid_connects"]]
        macros.append(Macro(**m))
    kw["macros"] = macros
    kw["groups"] = [Group(**{**g, "box": tuple(g["box"]) if g.get("box") else None}) for g in data.get("groups", [])]
    kw["datapaths"] = [Datapath(**{**dp, "origin": tuple(dp["origin"])}) for dp in data.get("datapaths", [])]
    if "pdn" in data:
        p = dict(data["pdn"])
        if p.get("straps") is not None:
            p["straps"] = [_strap(s) for s in p["straps"]]
        for k in ("connects", "macro_connects"):
            if p.get(k) is not None:
                p[k] = [tuple(c) for c in p[k]]
        for k in ("ring_layers", "ring_pad_layers"):
            if p.get(k) is not None:
                p[k] = tuple(p[k])
        kw["pdn"] = PdnSpec(**p)
    if data.get("pad_rules"):
        kw["pad_rules"] = PadRules(**data["pad_rules"])
    if kw.get("pad_masters", {}).get("fill"):
        kw["pad_masters"] = dict(kw["pad_masters"])
    return Design(**kw)


# explore mode drops these (signoff-grade work), and keeps the rest of the design's flow
EXPLORE_DROP = {"fill", "route_repair", "extract", "signoff", "views", "integrate"}


def expand_sweep(item: dict) -> dict:
    """A synth step's ``sweep`` (an objective of sipath.recipes.SETS or a list of
    recipes) and ``synth_opts_sweep`` (front-end option sets) as variants: every
    given variant x recipe x option set, picked by ``synth_select`` unless a
    score is given; ``parallel`` defaults to 4."""
    if "sweep" not in item and "synth_opts_sweep" not in item:
        return item
    from . import recipes
    item = dict(item)
    sweep, opts = item.pop("sweep", None), item.pop("synth_opts_sweep", None)
    if isinstance(sweep, str):
        if sweep not in recipes.SETS:
            raise ValueError(f"{item['step']}: sweep {sweep!r} is not one of {sorted(recipes.SETS)} (or give a list)")
        sweep = recipes.SETS[sweep]
    base = item.get("variants") or [{}]
    recs = list(sweep) if sweep else [None]
    sets = [list(o) for o in opts] if opts else [None]
    item["variants"] = [{**b, **({"abc": r} if r else {}), **({"synth_opts": o} if o is not None else {})}
                        for b in base for r in recs for o in sets]
    item.setdefault("score", "synth_select")
    item.setdefault("parallel", 4)
    return item


def explore_flow(spec: list[dict]) -> list[dict]:
    """A design's flow as a fast first cut at PPA (explore mode): no
    equivalence check, global routing only (PPA from its parasitics), no fill,
    post-route repair, extraction, signoff, views or integration; a ``ppa``
    step closes a flow that stops before routing; a routing score that needs
    the detailed router's DRC count becomes ``setup_wns``. With the typical
    corner only (``build(..., explore=True)``)."""
    out = []
    for item in spec:
        fn = item.get("fn", item["step"])
        if fn in EXPLORE_DROP:
            continue
        item = dict(item)
        if fn == "synth":
            item["variants"] = [{**v, "equiv": "off"} for v in (item.get("variants") or [{}])]
        if fn == "route_digital":
            item["variants"] = [{**v, "detailed": False} for v in (item.get("variants") or [{}])]
            if item.get("score") in ("drc_then_slack", "drc_then_wns"):
                item["score"] = "setup_wns"  # global routing only: no detailed-router DRC to rank by
        out.append(item)
    if not any(i.get("fn", i["step"]) == "route_digital" for i in out):
        out.append({"step": "ppa"})
    return out


def file_config(path: str) -> dict:
    """The ``config:`` section of a design file (see sipath.config)."""
    return dict(read_file(os.path.abspath(path)).get("config") or {})


def load(path: str, generate: bool = True) -> tuple[Design, list[dict]]:
    """(Design, flow steps) from a design file."""
    path = os.path.abspath(path)
    data = read_file(path)
    base = os.path.dirname(path)
    gen = (data.get("inputs") or {}).get("generate")
    if gen and generate:
        mod, fn = gen.split(":")
        getattr(importlib.import_module(mod), fn)()
    d = design_from_dict(data, base)
    if not d.pdk:
        raise ValueError(f"{path}: no 'pdk' given")
    return d, list(data.get("flow") or default_flow(d))


def build(d: Design, workdir: str, runner, config=None, resume: bool = False, explore: bool = False):
    """A Flow for ``d`` on its PDK, with the runner's mounts in place. With
    ``resume`` the attempt graph in ``workdir`` is picked up (see Flow); with
    ``explore`` the sessions use the typical corner only."""
    from .config import Config
    from .scheduler import Flow
    from .tools.openroad.session import OpenRoadSession
    cfg = config or Config.load()
    pdk = load_pdk(d.pdk, runner)
    if explore and pdk.corners:
        pdk.optimize_corners = [pdk.typical().name]
    harness_info = None
    if d.harness:  # before resolve: the harness sets supplies, PDN and floorplan
        from . import harness
        harness_info = harness.get(d.harness, d.harness_options).apply(d, pdk, os.path.abspath(workdir))
    resolve_srams(d, pdk)
    d.resolve(pdk)
    from . import blackbox
    models = blackbox.generate(d, pdk, os.path.join(os.path.abspath(workdir), "models"))  # opt-in only
    runner.mount(workdir, *d.input_files(), *d.rtl_includes, *pdk.files())
    prov = runner.provenance() if hasattr(runner, "provenance") else {}
    from . import toolchain
    release = toolchain.require_pdk(pdk, cfg)
    if release:
        prov = dict(prov, pdk_release=release)
    if models:
        prov = dict(prov, blackbox_models=models)
    if harness_info:
        prov = dict(prov, harness=harness_info)
    from . import registry
    ext = registry.loaded()
    if any(ext.values()):
        prov = dict(prov, plugins=ext)
    return Flow(d, pdk, workdir, lambda w: OpenRoadSession(w, runner, timeout=cfg.command_timeout),
                runner_prefixes=runner.runner_prefixes(), config=cfg, resume=resume, provenance=prov, runner=runner)


def resolve_srams(d, pdk) -> None:
    """A macro that names one of the PDK's SRAMs (``sram:``) gets that SRAM's
    LEF and Liberty unless the macro gives its own."""
    for m in d.macros:
        if m.sram:
            entry = pdk.sram.get(m.sram)
            if entry is None:
                raise ValueError(f"macro {m.inst}: sram {m.sram!r} is not one of {d.pdk}'s: {sorted(pdk.sram)}")
            m.lef = m.lef or entry.get("lef", "")
            if m.lib is None:
                m.lib = entry.get("lib")


def run(path_or_design, workdir: str, runner=None, overrides: dict | None = None, flow_steps=None,
        config=None, resume: bool = False, progress=None, explore: bool = False):
    """Run every step of a design file (or a Design with ``flow_steps``); each
    step continues from the previous step's selected attempt. ``overrides``:
    step -> dict of {variants, score, parallel} replacing the file's. With
    ``resume`` steps already recorded in ``workdir`` are not run again. With
    ``explore`` the flow is a fast first cut at PPA (``explore_flow``)."""
    from .config import Config
    from .tools.openroad.session import find_openroad
    if isinstance(path_or_design, Design):
        d, spec, fcfg = path_or_design, list(flow_steps or default_flow(path_or_design)), {}
    else:
        d, spec = load(path_or_design)
        fcfg = file_config(path_or_design)
    cfg = config or Config.load(**fcfg)
    runner = runner or find_openroad(cfg)
    if runner is None:
        raise FileNotFoundError("OpenROAD not available")
    if explore:  # a first cut at PPA: see explore_flow
        spec = explore_flow(spec)
    flow = build(d, workdir, runner, cfg, resume, explore=explore)
    flow.progress = progress
    parent = None
    for item in spec:
        item = expand_sweep(dict(item, **(overrides or {}).get(item["step"], {})))
        name = item["step"]
        ref = item.get("fn", name)
        if ":" in ref:  # a step function by import path
            from . import registry
            registry.import_step(ref)
            if "fn" not in item:
                name = ref.rpartition(":")[2]  # the attempt directories take the function's name
        fn = steps_mod.STEPS.get(ref)
        if fn is None:
            raise ValueError(f"unknown step {name!r}; known: {sorted(steps_mod.STEPS)}")
        steps_mod.check_params(ref, item.get("variants"))
        r = flow.step(name, fn, parent=parent, variants=item.get("variants"), score=item.get("score"),
                      parallel=item.get("parallel", 1))
        parent = r.selected
    return flow


def harden(path: str, outdir: str, workdir: str | None = None, runner=None, config=None,
           resume: bool = False) -> str:
    """Run a design file through signoff and the views step and copy its
    hardened views (LEF, Liberty per corner, GDS, CDL, SPEF, netlists,
    macro.yaml) to ``outdir``; a parent lists the macro with ``views: outdir``."""
    import shutil
    from .config import Config
    d, spec = load(path)
    names = [s["step"] for s in spec]
    if "signoff" not in names:
        spec.append({"step": "signoff"})
    if "views" not in names:
        spec.append({"step": "views"})
    wd = workdir or os.path.join(os.getcwd(), "runs", d.name + "_harden")
    cfg = config or Config.load(**file_config(path))
    flow = run(d, wd, runner=runner, flow_steps=spec, config=cfg, resume=resume)
    views = flow.steps["views"].selected.outputs["views"]
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(views["dir"]):
        shutil.copy2(os.path.join(views["dir"], f), os.path.join(outdir, f))
    return outdir
