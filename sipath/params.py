"""Step parameters, declared once: type, default, unit, allowed values, doc.

Every parameter a step reads is declared here. The declarations are used to

* check a variant before anything runs (unknown name, wrong type, value not
  allowed or out of range -> ValueError naming the step and the parameter);
* resolve the parameters an attempt sees: the variant's value, else the
  design file's ``step_defaults``, else the PDK description's
  ``step_defaults``, else the declared default (``ctx.params`` holds every
  declared parameter; the attempt records only what the variant gave);
* generate the reference documentation (``python3 -m sipath docs``) and the
  MCP ``describe_step`` schema.

A default of ``None`` means "not set": the step then uses what the doc says
(e.g. the design's own value).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

def _recipe_choices():
    from .recipes import ALIASES, names
    return names() + sorted(ALIASES)


TYPES = {"int": int, "float": (int, float), "bool": bool, "str": str, "list": list, "dict": dict}


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # int | float | bool | str | list[str] | list[dict]
    default: Any
    doc: str
    unit: str = ""
    choices: tuple = ()  # allowed values (for a list: allowed items)
    min: float | None = None
    max: float | None = None

    def check(self, step: str, value) -> None:
        where = f"{step}.{self.name}"
        if value is None:
            return
        base, _, item = self.type.partition("[")
        want = TYPES[base]
        if isinstance(value, bool) and base in ("int", "float"):
            raise ValueError(f"{where}: expected {self.type}, got a bool ({value!r})")
        if not isinstance(value, want):
            raise ValueError(f"{where}: expected {self.type}, got {type(value).__name__} ({value!r})")
        values = value if base == "list" else [value]
        if item:
            it = TYPES[item.rstrip("]")]
            bad = [v for v in values if not isinstance(v, it)]
            if bad:
                raise ValueError(f"{where}: items must be {item.rstrip(']')}, got {bad[:3]!r}")
        if self.choices:
            bad = [v for v in values if v not in self.choices]
            if bad:
                raise ValueError(f"{where}: {bad[0]!r} is not one of {list(self.choices)}")
        for v in values:
            if self.min is not None and v < self.min:
                raise ValueError(f"{where}: {v!r} is below the minimum {self.min}")
            if self.max is not None and v > self.max:
                raise ValueError(f"{where}: {v!r} is above the maximum {self.max}")

    def schema(self) -> dict:
        base, _, item = self.type.partition("[")
        js = {"int": "integer", "float": "number", "bool": "boolean", "str": "string", "list": "array",
              "dict": "object"}
        out = {"type": js[base], "description": self.doc + (f" [{self.unit}]" if self.unit else "")}
        if item:
            out["items"] = {"type": js[item.rstrip("]")]}
            if self.choices:
                out["items"]["enum"] = list(self.choices)
        elif self.choices:
            out["enum"] = list(self.choices)
        if self.min is not None:
            out["minimum"] = self.min
        if self.max is not None:
            out["maximum"] = self.max
        if self.default is not None:
            out["default"] = list(self.default) if isinstance(self.default, tuple) else self.default
        return out


SIGNOFF_CHECKS = ("drc", "density", "lvs", "antenna", "setup", "hold", "drv", "annotation", "unconstrained",
                  "harness")
SIGNOFF_REQUIRED = ("drc", "density", "lvs", "antenna", "setup", "hold", "drv", "annotation", "harness")
# opt-in (need the Magic image, config magic_image): a second DRC engine and the stream-out XOR
SIGNOFF_OPTIONAL = ("magic_drc", "xor", "lvs_full")

_ITERS = "detailed-router iterations"
_FAIL = "fail the attempt when setup or hold is not met after the step"

STEP_PARAMS: dict[str, list[Param]] = {
    "synth": [
        Param("abc", "str", None, "one ABC recipe instead of abc_sweep (sipath/synth_recipes: 18 measured in "
              "shalan/synth_flow, plus area_map and speed_sizing; aliases delay, area, speed)",
              choices=tuple(_recipe_choices())),
        Param("abc_sweep", "str", "auto", "recipes mapped in this attempt, the netlist picked by synth_select "
              "(least area meeting timing, else the knee): auto (the delay set plus area_map), delay, area, "
              "balanced (synth_flow's top 5 each), off (delay_map_resyn alone), or names separated by commas"),
        Param("synth_fallback", "str", "best_slack", "when no recipe meets timing at synthesis: best_slack (the "
              "fastest netlist; measured best end to end, docs/synthesis.md), knee (of the slack/area front, else "
              "best slack) or least_area",
              choices=("knee", "best_slack", "least_area")),
        Param("map_corner", "str", None, "corner whose Liberty dfflibmap and ABC map with: a corner name, typical, "
              "or (default) the timing corner, the slow one (bench: 12/16 designs meet timing against 10 at typical)"),
        Param("synth_abc", "bool", False, "keep Yosys's generic ABC pass in synth (default: synth -noabc, as "
              "synth_flow; measured mixed: better on aes, worse on gcd8)"),
        Param("abc_target", "str", "none", "ABC delay target ({D} in the recipe): none (measured best on most "
              "designs), period (the SDC clock), or a number of ps"),
        Param("abc_period_ps", "float", None, "the period abc_target: period stands for; default: the SDC's first "
              "create_clock", unit="ps", min=1),
        Param("abc_wire_load", "bool", False, "ABC sizing (buffer, upsize, dnsize, stime) with the Liberty's "
              "wire-load model (-c; built into delay_map and delay_map_resyn)"),
        Param("abc_boundary", "bool", True, "give ABC the driving cell and output load (the SDC's, else the PDK's "
              "boundary defaults for a macro)"),
        Param("synth_opts", "list[str]", (), "front-end options: booth, adder=kogge-stone|han-carlson|sklansky, "
              "noshare, nofsm, noalumacc, opt_dff_sat, opt_full"),
        Param("keep_names", "bool", True, "name registers after the wire they drive (acc[3]_reg) through "
              "flattening"),
        Param("frontend", "str", "auto", "RTL reader: yosys (read_verilog -sv), slang (read_slang: full "
              "SystemVerilog; the image's Yosys must have it), auto (slang for .sv sources when available)",
              choices=("auto", "yosys", "slang")),
        Param("retime", "bool", False, "retime registers in generic synthesis (abc -dff, what synth -retime "
              "ran); flops with asynchronous set/reset are not moved"),
        Param("verilator", "str", "auto", "Verilator lint (--lint-only -Wall): auto (when Verilator is found: "
              "host, native tool or image), on (required), off; errors fail, warnings counted by code",
              choices=("auto", "on", "off")),
        Param("timing", "bool", True, "estimate the netlist's setup slack (OpenSTA at timing_corner with the SDC "
              "and the Liberty's wire-load model): synth_wns_ns, synth_tns_ns, for scoring synthesis variants"),
        Param("timing_corner", "str", None, "corner of the estimate (default: the worst-setup corner of the "
              "PDK's optimize_corners, e.g. ss_max)"),
        Param("lint", "bool", True, "lint the RTL first (implicit nets, hierarchy, undriven / multiply driven "
              "nets, combinational loops are errors)"),
        Param("lint_strict", "bool", False, "fail on any lint warning (Yosys or Verilator)"),
        Param("equiv", "str", "bounded", "check the netlist against the RTL: bounded (equal outputs for "
              "equiv_depth cycles from reset, every input sequence) or off", choices=("bounded", "off")),
        Param("equiv_depth", "int", 10, "cycles from reset the equivalence covers", min=1),
        Param("equiv_reset", "str", None, "reset port and active level, e.g. rst_n:0 (default: found, in any case, "
              "among rst_n, resetn, reset_n, rstn, nrst, rst_ni, presetn, hresetn, aresetn, arst_n (active 0), "
              "rst, reset, arst (active 1))"),
        Param("equiv_timeout", "int", 600, "SAT time limit; reached: inconclusive (reported, not passed)",
              unit="s", min=1),
        Param("port_buffers", "bool", True, "buffer assignments between ports (off: a port-to-port assignment "
              "stays one net with two pins, e.g. a wrapper output tied to a frame loopback input)"),
    ],
    "floorplan": [],
    "macros": [
        Param("method", "str", "random", "macro placement: seeded random search with orientation search (random), "
              "OpenROAD's rtl_macro_placer (mpl), or Team Macro Polo's CD-LNS placer from the Partcl/HRT challenge "
              "(cdlns; needs placer_python)", choices=("random", "mpl", "cdlns")),
        Param("placer_seconds", "float", 120.0, "time budget of the cdlns placer", unit="s", min=1),
        Param("seed", "int", 0, "random seed of the search"),
        Param("samples", "int", 40, "candidate locations tried per autoplaced macro", min=1),
        Param("orients", "list[str]", None, "orientations to try; default: each macro's orients, else its orient",
              choices=("R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90")),
        Param("channel", "float", None, "minimum space between facing macros; default: the design's "
              "macro_channel", unit="um", min=0),
        Param("pin_clearance", "float", None, "free space in front of a macro edge with signal pins; default: "
              "the design's macro_pin_clearance", unit="um", min=0),
        Param("analog_guard", "float", None, "extra space around analog macros; default: the design's "
              "analog_guard", unit="um", min=0),
        Param("analog_weight", "float", 4.0, "weight of analog-net wirelength in the search objective", min=0),
        Param("evaluate", "bool", True, "score the result with a quick place + route (macro_quick score)"),
        Param("quick_density", "float", 0.6, "placement density of the quick evaluation", min=0.05, max=1.0),
        Param("target_util", "float", 0.25, "target utilization given to rtl_macro_placer (method mpl)",
              min=0.01, max=1.0),
    ],
    "physical": [],
    "place": [
        Param("density", "float", 0.55, "global placement target density", min=0.05, max=1.0),
        Param("blockages", "list[dict]", None, "partial placement blockages: [{box: [x0, y0, x1, y1] (um), "
              "max_density: %}] filled to at most max_density by global placement and legalization (e.g. from a "
              "heatmap's hotspots)"),
        Param("seed", "int", None, "global placement random seed; default: the tool's"),
        Param("routability_driven", "bool", True, "routability-driven global placement (ORFS's default; off "
              "left aes too congested to route)"),
        Param("improve_placement", "bool", True, "after detailed placement: improve_placement -max_displacement "
              "\"5 1\" and optimize_mirroring, as ORFS (skipped with fences)"),
        Param("timing_driven", "bool", True, "timing-driven global placement (off for designs whose pins are far "
              "from the cells, e.g. a mostly empty harness wrapper: repair_design then buffers the long wires)"),
        Param("repair_tie_fanout", "bool", True, "one tie cell per load, placed next to it"),
        Param("remove_buffers", "bool", False, "remove the netlist's buffers before placement"),
        Param("max_util", "float", None, "utilization limit for repair_design / repair_timing", min=0.01, max=1.0),
        Param("pin_max_wire", "float", 400.0, "harness designs: longest wire left on a signal pin's buffer chain "
              "(the nets from each pin through its buffers repaired with this wire length limit: a wrapper's pad "
              "nets run millimetres; longer wires end at the capacitance limit or collect antenna diodes, which "
              "count as fanout); 0: no limit", unit="um", min=0),
        Param("congestion_check", "bool", True, "global route after placement and record the congestion"),
        Param("congestion_margin", "float", 0.0, "extra layer capacity reduction for the congestion check",
              min=0, max=1),
    ],
    "cts": [],
    "route_analog": [
        Param("iters", "int", 20, _ITERS + " for the analog nets", min=1),
        Param("guard", "bool", True, "routing obstructions around the frozen analog wiring so digital wires keep "
              "the analog NDR spacing (the router keeps only the minimum spacing from special wiring)"),
        Param("shield_tie_um", "float", 30.0, "longest strap from a shield to ground when no ground shape crosses "
              "it (analog_shield)", unit="um", min=0),
    ],
    "route_digital": [
        Param("detailed", "bool", True, "detailed routing; off: stop after global routing and its repair, with "
              "PPA from global-route parasitics (explore mode)"),
        Param("iters", "int", 64, _ITERS, min=1),
        Param("max_drc", "int", 0, "routing DRC violations allowed", min=0),
        Param("antenna_rounds", "int", 3, "antenna repair rounds", min=0),
        Param("max_antenna", "int", 0, "antenna violations allowed", min=0),
        Param("pin_access", "bool", False, "pin access analysis before global routing, as ORFS (off: with it "
              "this OpenROAD build segfaults in a post-route repair_antennas after a re-route, mcu8)"),
        Param("repair_after_grt", "bool", True, "repair timing on global-route parasitics, then re-route "
              "incrementally"),
        Param("fail_on_violation", "bool", False, _FAIL),
    ],
    "route_repair": [
        Param("rounds", "int", 2, "post-route timing repair rounds on extracted parasitics (a failure of "
              "OpenROAD's incremental global router inside repair retries the attempt with 0)", min=0),
        Param("max_patch_nets", "int", None, "nets changed by repair above which the design is routed again "
              "instead of patched (default: a quarter of the signal nets, at least 40)", min=0),
        Param("drv_sizing", "bool", False, "also size up the drivers of nets whose standard cells see slew or "
              "capacitance violations on extracted parasitics (estimates miss some: wires doubled by detours)"),
        Param("antenna_rounds", "int", 3, "antenna repair rounds", min=0),
        Param("max_antenna", "int", 0, "antenna violations allowed", min=0),
        Param("fillers", "list[str]", None, "cells for the empty sites (default: the PDK's fill list, decaps "
              "first); e.g. fill cells only where most rows are empty and decaps would only add LVS devices"),
        Param("iters", "int", 64, _ITERS, min=1),
        Param("max_drc", "int", 0, "routing DRC violations allowed", min=0),
        Param("fail_on_violation", "bool", False, _FAIL),
    ],
    "fill": [
        Param("layers", "list[str]", None, "layers to fill (default: the fill rules' layers up to the "
              "design's top routing layer)"),
        Param("area", "list[float]", None, "fill only inside [x0, y0, x1, y1] (default: the die)", unit="um"),
    ],
    "integrate": [
        Param("drc", "bool", False, "full-chip KLayout DRC of the assembled chip (slow; the frame's IO cells "
              "carry their own waivers)"),
        Param("seal_ring", "bool", False, "add the harness's seal ring (frame.seal_ring) as the frame's "
              "compositor does; checked to enclose the die with no shape inside it"),
    ],
    "precheck": [
        Param("skip", "list[str]", [], "cf-precheck checks to skip (its names, e.g. lvs, xor)"),
        Param("magic_drc", "bool", False, "include cf-precheck's optional Magic DRC check"),
        Param("fail_on_violation", "bool", True, "fail the step when a check fails (results are written either way)"),
    ],
    "ppa": [
        Param("margin", "float", 0.05, "margin added to the achievable clock period in the suggested one",
              min=0, max=1),
    ],
    "views": [
        Param("corners", "list[str]", None, "analysis corners to write a timing Liberty for (default: all)"),
        Param("bloat_occupied_layers", "bool", True, "abstract LEF: layers the design uses are blocked whole "
              "(a parent routes over the macro only on layers it leaves free)"),
    ],
    "extract": [
        Param("require_grid_connected", "bool", False, "fail when check_power_grid finds a supply net not "
              "connected to its terminals"),
        Param("max_analog_coupling_ff", "float", None, "fail when an analog net's coupling to digital nets "
              "(sum, worst corner) exceeds this", unit="fF", min=0),
        Param("max_analog_aggressor_ff", "float", None, "fail when one digital net couples more than this onto "
              "an analog net (worst corner)", unit="fF", min=0),
        Param("fail_on_violation", "bool", False, _FAIL),
    ],
    "signoff": [
        Param("checks", "list[str]", SIGNOFF_CHECKS, "checks to run (opt-in: magic_drc and xor need the Magic "
              "image; lvs_full runs the PDK's full-device LVS deck and needs lvs and no black boxes)",
              choices=SIGNOFF_CHECKS + SIGNOFF_OPTIONAL),
        Param("require", "list[str]", SIGNOFF_REQUIRED + SIGNOFF_OPTIONAL, "checks whose failure fails signoff "
              "(of those run)", choices=SIGNOFF_CHECKS + SIGNOFF_OPTIONAL),
        Param("fail_on_violation", "bool", True, "fail the attempt when a required check fails (signoff.json "
              "is written either way)"),
        Param("drc_split", "bool", True, "run the PDK's DRC rule groups (signoff.drc_groups) as concurrent "
              "processes and merge their reports"),
    ],
    "eco": [
        Param("ops", "list[dict]", None, "ECO operations: {op: resize, inst, master} or {op: buffer, net, "
              "master, loads?, name?}"),
        Param("max_patch_nets", "int", 25, "touched nets above which the design is routed again instead of "
              "patched", min=0),
        Param("iters", "int", 64, _ITERS + " for the patch route", min=1),
        Param("max_drc", "int", 0, "routing DRC violations allowed after the ECO", min=0),
    ],
}
# each signoff check (or group of checks) as a step of its own: `- step: drc` runs signoff with those
# checks only (lvs_full brings the LVS reference netlist it compares against)
CHECK_STEPS = {"drc": ("drc",), "density": ("density",), "lvs": ("lvs",), "lvs_full": ("lvs", "lvs_full"),
               "antenna": ("antenna",), "timing": ("setup", "hold", "drv", "annotation", "unconstrained"),
               "magic_drc": ("magic_drc",), "xor": ("xor",), "harness": ("harness",)}
for _name, _checks in CHECK_STEPS.items():
    _req = tuple(c for c in SIGNOFF_REQUIRED + SIGNOFF_OPTIONAL if c in _checks)
    STEP_PARAMS[_name] = [
        Param("require", "list[str]", _req, "of these checks, those whose failure fails the step", choices=_checks),
        Param("fail_on_violation", "bool", True, "fail the attempt when a required check fails (signoff.json "
              "is written either way)")]
    if "drc" in _checks:
        STEP_PARAMS[_name].append(Param("drc_split", "bool", True, "run the PDK's DRC rule groups as concurrent "
                                        "processes and merge their reports"))
# every OpenROAD step (not the tool steps synth and integrate) takes these
COMMON = (Param("threads", "int", None, "OpenROAD threads for this step's attempts (set_thread_count; signoff: "
                "also KLayout DRC); unset: the run's threads, shared among the attempts running at once", min=1),)
TOOL_STEPS = ("synth", "integrate", "precheck")
for _step, _ps in STEP_PARAMS.items():
    if _step not in TOOL_STEPS:
        _ps.extend(p for p in COMMON if p.name not in {q.name for q in _ps})
# heatmaps collected after the checkpoint is saved (sipath.tools.openroad.heatmap): what each step can give
HEATMAP_MAPS = ("placement", "rudy", "routing", "pin", "power", "irdrop")
_HEATMAPS = {"place": ["placement", "rudy", "pin", "power"], "cts": ["placement", "rudy", "pin", "power"],
             "route_digital": ["placement", "rudy", "routing", "pin", "power"],
             "extract": ["placement", "rudy", "routing", "pin", "power", "irdrop"]}
for _step, _maps in _HEATMAPS.items():
    STEP_PARAMS[_step].append(Param(
        "heatmaps", "list[str]", _maps, "heatmaps written after the checkpoint is saved (gui::dump_heatmap; CSVs, "
        "summary metrics heatmap_<map>_peak|p95|hot_share, hotspots with their instances or nets); [] for none",
        choices=HEATMAP_MAPS))

DECLARED = {step: {p.name: p for p in ps} for step, ps in STEP_PARAMS.items()}
BUILTIN = {step: list(ps) for step, ps in STEP_PARAMS.items()}  # before any plugin registers (for the reference)


def check(step: str, variant: dict | None) -> None:
    """Raise ValueError for a parameter the step does not declare, or a value
    of the wrong type, not allowed or out of range."""
    if step not in DECLARED:
        raise ValueError(f"unknown step {step!r}; known: {sorted(DECLARED)}")
    if variant is None:
        return
    if not isinstance(variant, dict):
        raise ValueError(f"{step}: parameters must be a dict, not {variant!r}")
    decl = DECLARED[step]
    bad = sorted(set(variant) - set(decl))
    if bad:
        raise ValueError(f"{step}: unknown parameter(s) {bad}; known: {sorted(decl) or 'none'}")
    for k, v in variant.items():
        decl[k].check(step, v)


def check_defaults(where: str, table: dict | None) -> None:
    """Check a ``step_defaults`` table ({step: {param: value}})."""
    if not table:
        return
    if not isinstance(table, dict):
        raise ValueError(f"{where}: step_defaults must be a mapping of step -> parameters")
    for step, vals in table.items():
        try:
            check(step, vals)
        except ValueError as e:
            raise ValueError(f"{where} step_defaults: {e}") from e


def resolve(step: str, given: dict, pdk=None, design=None) -> dict:
    """Every declared parameter of ``step``: the given value, else the
    design's step_defaults, else the PDK's, else the declared default. A step
    that declares nothing (or an unknown function name) gets ``given``."""
    decl = DECLARED.get(step)
    if not decl:
        return dict(given)
    out = {}
    layers = [given, (getattr(design, "step_defaults", None) or {}).get(step, {}),
              (getattr(pdk, "step_defaults", None) or {}).get(step, {})]
    for name, p in decl.items():
        for layer in layers:
            if name in layer:
                out[name] = layer[name]
                break
        else:
            out[name] = list(p.default) if isinstance(p.default, tuple) else p.default
    out.update({k: v for k, v in given.items() if k not in decl})
    return out


def step_schema(step: str) -> dict:
    """JSON Schema of one variant of ``step``."""
    return {"type": "object", "additionalProperties": False,
            "properties": {p.name: p.schema() for p in STEP_PARAMS[step]}}


def markdown() -> str:
    """Reference tables of the built-in steps' parameters (plugins document their own)."""
    out = ["## Step parameters", "",
           "Set per attempt in `variants` (design file `flow:` or MCP `run_step`), or as defaults in a design "
           "file's or PDK description's `step_defaults: {step: {param: value}}`. Resolution: variant, then "
           "design, then PDK, then the default below.", ""]
    for step, ps in BUILTIN.items():
        out.append(f"### {step}")
        out.append("")
        if not ps:
            out += ["No parameters.", ""]
            continue
        out += ["| parameter | type | default | allowed | description |", "|---|---|---|---|---|"]
        for p in ps:
            d = "–" if p.default is None else (", ".join(p.default) if isinstance(p.default, tuple)
                                               else str(p.default).lower() if isinstance(p.default, bool)
                                               else str(p.default))
            allowed = ", ".join(map(str, p.choices)) if p.choices else (
                f"{'' if p.min is None else p.min}..{'' if p.max is None else p.max}"
                if p.min is not None or p.max is not None else "")
            unit = f" [{p.unit}]" if p.unit else ""
            out.append(f"| `{p.name}` | {p.type} | {d} | {allowed} | {p.doc}{unit} |")
        out.append("")
    return "\n".join(out)
