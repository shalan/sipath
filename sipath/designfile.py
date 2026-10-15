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
          "macro_pin_clearance", "analog_guard", "layer_adjustments", "pad_masters", "pdk", "dont_use",
          "allow_excluded", "step_defaults", "rtl_defines", "core_utilization", "aspect_ratio")


def default_flow(d: Design) -> list[dict]:
    chain = (["synth"] if d.rtl else []) + ["floorplan"] + (["macros"] if d.macros else []) + [
        "physical", "place", "cts", "route_analog", "route_digital", "route_repair", "extract"]
    return [{"step": s} for s in chain]


def _path(base, p):
    if p is None or os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(base, p))


def _strap(s):
    return s if isinstance(s, Strap) else Strap(**s)


def design_from_dict(data: dict, base: str) -> Design:
    from . import schema
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
        m["lef"] = _path(base, m["lef"])
        for k in ("gds", "cdl"):
            if m.get(k):
                m[k] = _path(base, m[k])
        lib = m.get("lib")
        m["lib"] = {c: _path(base, v) for c, v in lib.items()} if isinstance(lib, dict) else _path(base, lib)
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


def build(d: Design, workdir: str, runner, config=None, resume: bool = False):
    """A Flow for ``d`` on its PDK, with the runner's mounts in place. With
    ``resume`` the attempt graph in ``workdir`` is picked up (see Flow)."""
    from .config import Config
    from .scheduler import Flow
    from .session import OpenRoadSession
    cfg = config or Config.load()
    pdk = load_pdk(d.pdk, runner)
    d.resolve(pdk)
    runner.mount(workdir, *d.input_files(), *d.rtl_includes, *pdk.files())
    prov = runner.provenance() if hasattr(runner, "provenance") else {}
    return Flow(d, pdk, workdir, lambda w: OpenRoadSession(w, runner, timeout=cfg.command_timeout),
                runner_prefixes=runner.runner_prefixes(), config=cfg, resume=resume, provenance=prov, runner=runner)


def run(path_or_design, workdir: str, runner=None, overrides: dict | None = None, flow_steps=None,
        config=None, resume: bool = False, progress=None):
    """Run every step of a design file (or a Design with ``flow_steps``); each
    step continues from the previous step's selected attempt. ``overrides``:
    step -> dict of {variants, score, parallel} replacing the file's. With
    ``resume`` steps already recorded in ``workdir`` are not run again."""
    from .config import Config
    from .session import find_openroad
    if isinstance(path_or_design, Design):
        d, spec, fcfg = path_or_design, list(flow_steps or default_flow(path_or_design)), {}
    else:
        d, spec = load(path_or_design)
        fcfg = file_config(path_or_design)
    cfg = config or Config.load(**fcfg)
    runner = runner or find_openroad(cfg)
    if runner is None:
        raise FileNotFoundError("OpenROAD not available")
    flow = build(d, workdir, runner, cfg, resume)
    flow.progress = progress
    parent = None
    for item in spec:
        item = dict(item, **(overrides or {}).get(item["step"], {}))
        name = item["step"]
        fn = steps_mod.STEPS.get(item.get("fn", name))
        if fn is None:
            raise ValueError(f"unknown step {name!r}; known: {sorted(steps_mod.STEPS)}")
        steps_mod.check_params(item.get("fn", name), item.get("variants"))
        r = flow.step(name, fn, parent=parent, variants=item.get("variants"), score=item.get("score"),
                      parallel=item.get("parallel", 1))
        parent = r.selected
    return flow
