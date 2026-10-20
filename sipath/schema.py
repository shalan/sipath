"""The design file, declared: every key with its type and doc.

Used to check a design file before anything is built (unknown keys and wrong
types are errors that name the key path), to write its JSON Schema
(``python3 -m sipath schema``) and the reference (``python3 -m sipath docs``).
A test keeps the declarations in step with the dataclasses they fill
(Design, Macro, Pad, Group, Datapath, PdnSpec, Strap, PadRules, Config).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    type: str  # str int number bool path box point dict object list[...] lib
    doc: str
    required: bool = False
    fields: dict | None = None  # for object / list[object]
    choices: tuple = ()


def F(type_, doc, **kw):
    return Field(type_, doc, **kw)


STRAP = {
    "layer": F("str", "routing layer", required=True),
    "width": F("number", "strap width (um)", required=True),
    "pitch": F("number", "strap pitch (um)", required=True),
    "offset": F("number", "offset of the first strap from the grid origin (um)"),
}
PDN = {
    "rail_layer": F("str", "standard-cell rail layer; default: the PDK's"),
    "rail_width": F("number", "rail width (um); default: the PDK's"),
    "straps": F("list[object]", "core straps, bottom to top; default: the PDK's", fields=STRAP),
    "connects": F("list[pair]", "layer pairs joined by vias; default: the PDK's"),
    "rings": F("bool", "core ring (off unless asked)"),
    "ring_connect_to_pads": F("bool", "join the core ring to pad supply pins of the same net"),
    "ring_pad_layers": F("list[str]", "pad pin layers the ring may connect to"),
    "ring_layers": F("list[str]", "two ring layers (horizontal, vertical); default: the PDK's"),
    "ring_width": F("number", "ring width (um)"),
    "ring_spacing": F("number", "ring spacing (um)"),
    "ring_core_offset": F("number", "ring offset from the core (um)"),
    "macro_connects": F("list[pair]", "layer pairs joining macro pins to the grid"),
}
MACRO = {
    "inst": F("str", "instance name in the netlist", required=True),
    "lef": F("path", "abstract LEF (required unless views gives it)"),
    "sram": F("str", "one of the PDK's SRAM macros (the PDK description's sram table): lef and lib come "
                     "from the PDK"),
    "views": F("path", "a hardened macro's views directory (python3 -m sipath harden, or the views step): "
                       "lef, gds, lib, cdl and supplies come from its macro.yaml unless given here"),
    "lib": F("lib", "Liberty: one file, or {corner or process: file}; null for none"),
    "location": F("point", "lower-left (um); null: autoplaced by the macros step"),
    "orient": F("str", "orientation", choices=("R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90")),
    "orients": F("list[str]", "orientations the autoplacer may try"),
    "halo": F("number", "placement halo (um)"),
    "supplies": F("dict", "supply pin regex -> net"),
    "grid_stripes": F("list[object]", "macro power grid straps", fields=STRAP),
    "grid_connects": F("list[pair]", "macro grid layer pairs"),
    "grid_halo": F("number", "macro grid halo (um)"),
    "gds": F("path", "layout for signoff and the written GDS"),
    "cdl": F("path", "transistor/subcircuit netlist for LVS"),
    "lvs": F("str", "blackbox: LVS checks only the connections to its pins", choices=("blackbox",)),
    "abstract": F("bool", "signoff on the LEF abstract (DRC around it, LVS black box); the written GDS "
                          "still carries its GDS"),
    "analog": F("bool", "analog macro; null: decided from its nets and supplies"),
    "auto_blackbox": F("bool", "no lib: write a black-box Liberty model from the LEF (pins, directions, no timing "
                                "arcs); null: the design's auto_blackbox"),
}
PAD = {
    "inst": F("str", "pad instance name", required=True),
    "edge": F("str", "die edge", required=True, choices=("north", "south", "east", "west")),
    "order": F("int", "position along the edge (0 first)", required=True),
    "master": F("str", "IO cell master"),
}
PAD_RULES = {
    "max_outputs_per_io_pair": F("int", "output pads per VDDIO/VSSIO pair"),
    "min_core_supply_edges": F("int", "edges that need a core supply pair"),
    "require_io_supply_per_edge": F("bool", "a VDDIO/VSSIO pair on every edge with signal pads"),
    "require_supply_pairs_adjacent": F("bool", "supply pads next to their ground pad"),
    "analog_needs_analog_supply": F("bool", "analog pads need a VDDA/VSSA pair on their edge"),
    "analog_not_next_to_outputs": F("bool", "no analog pad next to an output pad"),
}
GROUP = {
    "name": F("str", "group name", required=True),
    "prefix": F("str", "hierarchy prefix of the members", required=True),
    "kind": F("str", "fence: a region; cluster: packed together", required=True, choices=("fence", "cluster")),
    "box": F("box", "fence region (um)"),
    "utilization": F("number", "cluster packing density"),
}
DATAPATH = {
    "name": F("str", "structure name", required=True),
    "kind": F("str", "column, array or slices", required=True, choices=("column", "array", "slices")),
    "cells": F("list", "column: [inst]; array/slices: [[inst, ...], ...]", required=True),
    "origin": F("point", "lower-left of the structure (um)", required=True),
    "stage_gap": F("int", "sites between stage columns (slices)"),
    "rows_per_slice": F("int", "rows per slice (slices)"),
}
CONFIG = {
    "threads": F("int", "OpenROAD threads per process (default: all cores)"),
    "command_timeout": F("number", "seconds before a Tcl command is abandoned"),
    "parallel_cap": F("int", "attempts at once across a step (0: as asked)"),
    "container_cpus": F("number", "CPU limit per container"),
    "container_memory": F("str", "memory limit per container (e.g. 8g)"),
    "image": F("str", "ORFS image (name@sha256:... pins it)"),
    "placer_python": F("str", "Python with PyTorch for the macros method cdlns (sipath setup-placer)"),
    "magic_image": F("str", "image with Magic for the signoff checks magic_drc and xor (docker/magic)"),
    "openroad_binary": F("str", "OpenROAD inside the image (default the ORFS path; openroad for an image with it on PATH)"),
    "platform": F("str", "image platform: linux/amd64, or linux/arm64 for a native arm64 image"),
    "native_tools": F("list[str]", "tools run on the host instead of the image (klayout, yosys, magic): faster than emulation"),
    "tool_timeout": F("number", "seconds one signoff tool run (KLayout DRC/LVS, Magic) may take"),
    "require_digest": F("bool", "refuse an image not pinned by digest (default on)"),
    "precheck_image": F("str", "image with ChipFoundry's cf-precheck and its tools (precheck step)"),
    "require_pdk_release": F("bool", "refuse an open_pdks tree whose release is not the locked one (default on)"),
    "debug_failed": F("bool", "write a debug checkpoint for a failed attempt"),
    "log_level": F("str", "flow logger level", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
}
FLOW_STEP = {
    "step": F("str", "step name: a registered step, any name with fn, or module:function (a step function "
                     "imported by path)", required=True),
    "fn": F("str", "step function when step is another name: a registered step or module:function"),
    "variants": F("list[dict]", "one parameter dict per attempt (see Step parameters)"),
    "score": F("str", "named score that picks the best attempt"),
    "parallel": F("int", "attempts at once"),
    "sweep": F("any", "synth: an objective (delay, area, balanced: its five measured ABC recipes) or a list of "
                      "recipes, one attempt each; selected by synth_select unless score is given"),
    "synth_opts_sweep": F("list", "synth: front-end option sets, e.g. [[], [booth], [adder=kogge-stone]]; every "
                                  "recipe runs with each"),
}
DESIGN = {
    "name": F("str", "run and report name", required=True),
    "top": F("str", "top module", required=True),
    "mode": F("str", "macro: soft macro from floorplan_def; die: pad ring, die, macros", required=True,
              choices=("macro", "die")),
    "pdk": F("str", "PDK description name (sipath/pdks) or path", required=True),
    "description": F("str", "free text"),
    "netlist": F("path", "flattened gate-level Verilog (or give rtl)"),
    "rtl": F("list[path]", "RTL (Verilog/SystemVerilog) files; the synth step writes the netlist"),
    "rtl_defines": F("dict", "RTL defines: name -> value (\"\" for a bare define)"),
    "rtl_includes": F("list[path]", "RTL include directories"),
    "sdc": F("path", "timing constraints", required=True),
    "floorplan_def": F("path", "macro mode: die, rows and pins (else die or core_utilization, pins placed "
                                "by the flow)"),
    "die": F("box", "die area (um); die mode, or macro mode without floorplan_def"),
    "core_margin": F("number", "core inset from the die (um)"),
    "core_utilization": F("number", "macro mode without floorplan_def or die: target core utilization (0..1)"),
    "aspect_ratio": F("number", "core height / width with core_utilization (default 1)"),
    "pads": F("list[object]", "die mode: pad ring, per edge and order", fields=PAD),
    "pad_masters": F("dict", "pad ring roles -> masters (corner, fill, geometry overrides)"),
    "pad_kinds": F("dict", "signal pad -> input, output or analog (pad rules)"),
    "pad_supplies": F("dict", "IO-cell supply pin regex -> net"),
    "pad_rules": F("object", "pad plan rules", fields=PAD_RULES),
    "macros": F("list[object]", "hard macros", fields=MACRO),
    "analog_nets": F("list[str]", "nets routed first with an NDR and frozen"),
    "analog_ndr": F("dict", "analog NDR: {width: {layer: um}, spacing: {layer: um}}"),
    "analog_row_blocked_layers": F("list[str]", "layers analog routes may not use over rows"),
    "analog_supplies": F("list[str]", "supply nets routed like analog nets (e.g. VDDA)"),
    "analog_supply_layers": F("list[str]", "lowest and highest layer of analog supplies"),
    "analog_supply_ndr": F("dict", "NDR of analog supplies"),
    "analog_shield": F("list[str]", "analog nets with grounded shield wires on both sides (route_analog)"),
    "analog_shield_net": F("str", "net of the shields; default: the core ground"),
    "analog_keepaway": F("bool", "no digital wiring on the layers just above and below the shielded nets"),
    "extra_lefs": F("list[path]", "more LEFs (black boxes, pads)"),
    "extra_libs": F("list[path]", "more Liberty files"),
    "is_core": F("bool", "the design is a chip core (routing to the PDK's top layer); false: embedded macro"),
    "min_route_layer": F("str", "lowest routing layer; default: the PDK's"),
    "max_route_layer": F("str", "highest routing layer; default: the PDK's core or macro top"),
    "pdn": F("object", "power grid; unset fields take the PDK's", fields=PDN),
    "groups": F("list[object]", "hierarchy groups (fences, clusters)", fields=GROUP),
    "datapaths": F("list[object]", "structured placement", fields=DATAPATH),
    "clock_ports": F("list[str]", "clock ports (CTS nets)"),
    "clock_nets": F("list[str]", "explicit CTS nets"),
    "keepouts": F("list[box]", "boxes the macro autoplacer avoids (um)"),
    "macro_channel": F("number", "space between facing macros (um)"),
    "macro_pin_clearance": F("number", "space in front of macro edges with signal pins (um)"),
    "analog_guard": F("number", "extra space around analog macros (um)"),
    "layer_adjustments": F("dict", "layer -> global-route capacity reduction (over the PDK's)"),
    "dont_use": F("list[str]", "more cell patterns never used"),
    "allow_excluded": F("list[str]", "PDK-excluded cells used on purpose (waivers)"),
    "step_defaults": F("dict", "{step: {param: value}}: parameter defaults over the PDK's"),
    "auto_blackbox": F("bool", "macros without lib get a black-box Liberty model written from their LEF (off by "
                                "default; a macro's own auto_blackbox overrides)"),
    "harness": F("str", "a frame (sipath/harnesses: openframe, caravel; or a plugin's): the design becomes its "
                        "wrapper (template die and pins, user supplies, PDN); mode, floorplan and die come from it"),
    "harness_options": F("dict", "harness settings, e.g. {core_region: [x0, y0, x1, y1]} (um): standard-cell rows "
                                 "only there (a wrapper is mostly empty)"),
    "supply_names": F("dict", "PDK core supply net -> its name in this design, e.g. {VDD: vccd1, VSS: vssd1} "
                               "(standard-cell supplies, PDN, macro and pad supply maps, signoff)"),
    "plugins": F("list[str]", "Python modules imported before this file is checked; they register steps, scores, "
                              "signoff checks, harnesses and extension keys (sipath.registry)"),
    "extensions": F("dict", "plugin namespace -> its settings, checked against the keys the plugin registered"),
    "config": F("object", "run configuration (also SIPATH_* environment, --config)", fields=CONFIG),
    "inputs": F("object", "generated inputs", fields={"generate": F("str", "module:function that writes them")}),
    "flow": F("list[object]", "steps in order; default: the standard chain", fields=FLOW_STEP),
}

# design-file key -> dataclass field, where they differ (no difference today)
FILE_ONLY = {"description", "config", "inputs", "flow", "views"}  # not Design fields (views: read into a Macro)
INTERNAL = {"straps_from_pdk"}  # dataclass fields set by the flow, not in a design file


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_value(path: str, f: Field, v, errors: list):  # noqa: C901
    if v is None:
        return
    t = f.type
    ok = True
    if t in ("str", "path"):
        ok = isinstance(v, str)
    elif t == "int":
        ok = isinstance(v, int) and not isinstance(v, bool)
    elif t == "number":
        ok = _num(v)
    elif t == "bool":
        ok = isinstance(v, bool)
    elif t == "dict":
        ok = isinstance(v, dict)
    elif t == "box":
        ok = isinstance(v, (list, tuple)) and len(v) == 4 and all(_num(x) for x in v)
    elif t == "point":
        ok = isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(x) for x in v)
    elif t == "lib":
        ok = isinstance(v, str) or (isinstance(v, dict) and all(isinstance(x, str) for x in v.values()))
    elif t == "object":
        if not isinstance(v, dict):
            ok = False
        else:
            check_object(path, f.fields or {}, v, errors)
    elif t.startswith("list"):
        if not isinstance(v, list):
            ok = False
        else:
            item = t[5:-1] if "[" in t else ""
            for k, x in enumerate(v):
                sub = f"{path}[{k}]"
                if item == "object":
                    if not isinstance(x, dict):
                        errors.append(f"{sub}: expected a mapping, got {type(x).__name__}")
                    else:
                        check_object(sub, f.fields or {}, x, errors)
                elif item == "pair":
                    if not (isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(y, str) for y in x)):
                        errors.append(f"{sub}: expected a pair of layer names, got {x!r}")
                elif item:
                    _check_value(sub, Field(item, ""), x, errors)
    if not ok:
        errors.append(f"{path}: expected {t}, got {type(v).__name__} ({str(v)[:40]})")
    elif f.choices and isinstance(v, str) and v not in f.choices:
        errors.append(f"{path}: {v!r} is not one of {list(f.choices)}")


def check_object(path: str, spec: dict, data: dict, errors: list):
    for k in data:
        if k not in spec:
            errors.append(f"{path + '.' if path else ''}{k}: unknown key; known: {sorted(spec)}")
    for k, f in spec.items():
        if f.required and data.get(k) is None:
            errors.append(f"{path + '.' if path else ''}{k}: required")
        if k in data:
            _check_value(f"{path + '.' if path else ''}{k}", f, data[k], errors)


def validate(data: dict, required: bool = True) -> None:
    """Raise ValueError listing every unknown key and wrong type."""
    errors: list = []
    spec = DESIGN if required else {k: Field(f.type, f.doc, False, f.fields, f.choices) for k, f in DESIGN.items()}
    check_object("", spec, data, errors)
    if data.get("harness"):  # the harness gives the floorplan: a die or floorplan here would be ignored
        for k in ("floorplan_def", "die", "core_utilization"):
            if data.get(k) is not None:
                errors.append(f"{k}: not allowed with harness (the frame's wrapper template sets it)")
    for k, m in enumerate(data.get("macros") or []):
        if isinstance(m, dict) and not m.get("lef") and not m.get("views") and not m.get("sram"):
            errors.append(f"macros[{k}]: lef, views or sram is required")
    if required and not data.get("netlist") and not data.get("rtl"):
        errors.append("netlist or rtl: one is required")
    from . import registry
    for ns, val in (data.get("extensions") or {}).items():
        if ns not in registry.EXTENSIONS:
            errors.append(f"extensions.{ns}: no plugin registered this namespace (known: {sorted(registry.EXTENSIONS)})")
        elif not isinstance(val, dict):
            errors.append(f"extensions.{ns}: expected a mapping")
        else:
            check_object(f"extensions.{ns}", registry.EXTENSIONS[ns], val, errors)
    if errors:
        raise ValueError("design file:\n  " + "\n  ".join(errors))


def json_schema() -> dict:
    """JSON Schema (draft 2020-12) of a design file."""
    def one(f: Field) -> dict:
        t = f.type
        base = {"description": f.doc}
        js = {"str": "string", "path": "string", "int": "integer", "number": "number", "bool": "boolean",
              "dict": "object"}
        if t in js:
            out = {"type": js[t]}
        elif t == "box":
            out = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
        elif t == "point":
            out = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
        elif t == "lib":
            out = {"oneOf": [{"type": "string"}, {"type": "object", "additionalProperties": {"type": "string"}},
                             {"type": "null"}]}
        elif t == "object":
            out = obj(f.fields or {})
        elif t == "any":
            out = {}
        else:
            item = t[5:-1] if "[" in t else ""
            if item == "object":
                items = obj(f.fields or {})
            elif item == "pair":
                items = {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2}
            elif item == "box":
                items = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}
            elif item:
                items = one(Field(item, ""))
                items.pop("description", None)
            else:
                items = {}
            out = {"type": "array", "items": items}
        if f.choices:
            out["enum"] = list(f.choices)
        if "type" in out and not f.required and t not in ("object",):
            out["type"] = [out["type"], "null"] if isinstance(out["type"], str) else out["type"]
        return {**base, **out}

    def obj(spec: dict) -> dict:
        return {"type": "object", "additionalProperties": False,
                "properties": {k: one(f) for k, f in spec.items()},
                "required": [k for k, f in spec.items() if f.required]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "flow design file", **obj(DESIGN)}


def markdown() -> str:
    """Reference tables of the design file."""
    out = ["## Design file", "",
           "YAML or JSON; relative paths are relative to the file. Keys not listed are errors.", ""]

    def table(title, spec, level="###"):
        out.extend([f"{level} {title}", "", "| key | type | required | description |", "|---|---|---|---|"])
        nested = []
        for k, f in spec.items():
            doc = f.doc + (f" (one of: {', '.join(f.choices)})" if f.choices else "")
            out.append(f"| `{k}` | {f.type} | {'yes' if f.required else ''} | {doc} |")
            if f.fields:
                nested.append((f"{title} → {k}" if title != "top level" else k, f.fields))
        out.append("")
        for t, s in nested:
            table(t, s)
    table("top level", DESIGN)
    return "\n".join(out)
