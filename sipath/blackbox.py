"""Black-box Liberty models for macros that come without one (opt-in:
design ``auto_blackbox``, or the macro's own ``auto_blackbox``).

The model is written from the macro's LEF: one cell with the macro's area,
its signal pins (indexed pins grouped into buses), directions from the LEF
(a pin without one is ``inout`` and listed as assumed), a default pin
capacitance, and no timing arcs. Power and ground pins (LEF ``USE POWER`` /
``GROUND``, or matching the macro's ``supplies`` patterns) are left out. The
same file lets Yosys resolve the instance during synthesis and OpenROAD link
and time around it; paths through the macro are not timed (its outputs have
no arrival, so what they feed shows as unconstrained in signoff).
"""
from __future__ import annotations

import os
import re

DIRS = {"INPUT": "input", "OUTPUT": "output", "INOUT": "inout", "FEEDTHRU": "inout"}


def lef_pins(path: str, master: str) -> tuple[tuple[float, float], list[dict]]:
    """((width, height) um, [{name, direction or None, use}]) of ``master`` in a LEF."""
    with open(path, errors="replace") as f:
        text = f.read()
    m = re.search(rf"^\s*MACRO\s+{re.escape(master)}\s*$(.*?)^\s*END\s+{re.escape(master)}\s*$", text, re.M | re.S)
    if not m:
        raise ValueError(f"no MACRO {master} in {path}")
    body = m.group(1)
    size = re.search(r"^\s*SIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)", body, re.M)
    pins = []
    for pm in re.finditer(r"^\s*PIN\s+(\S+)(.*?)^\s*END\s+\1\s*$", body, re.M | re.S):
        d = re.search(r"^\s*DIRECTION\s+(\w+)", pm.group(2), re.M)
        u = re.search(r"^\s*USE\s+(\w+)", pm.group(2), re.M)
        pins.append({"name": pm.group(1), "direction": DIRS.get(d.group(1).upper()) if d else None,
                     "use": u.group(1).upper() if u else "SIGNAL"})
    return ((float(size.group(1)), float(size.group(2))) if size else (0.0, 0.0)), pins


def liberty(master: str, lef: str, supplies: dict | None = None, capacitance: float = 0.005,
            nom_voltage: float = 1.8) -> tuple[str, dict]:
    """(Liberty text, report) of a black-box model of ``master``."""
    (w, h), pins = lef_pins(lef, master)
    sup = [re.compile(p) for p in (supplies or {})]
    skipped, assumed, sig = [], [], []
    for p in pins:
        if p["use"] in ("POWER", "GROUND") or any(r.search(p["name"]) for r in sup):
            skipped.append(p["name"])
            continue
        if p["direction"] is None:
            p["direction"] = "inout"
            assumed.append(p["name"])
        sig.append(p)
    # group name[i] pins into buses when the indices are contiguous and share a direction
    groups, singles = {}, []
    for p in sig:
        m = re.fullmatch(r"(.+)\[(\d+)\]", p["name"])
        if m:
            groups.setdefault(m.group(1), []).append((int(m.group(2)), p))
        else:
            singles.append(p)
    buses, types = [], set()
    for base, items in groups.items():
        idx = sorted(i for i, _ in items)
        dirs = {p["direction"] for _, p in items}
        if idx == list(range(idx[0], idx[-1] + 1)) and len(dirs) == 1:
            buses.append((base, idx[-1], idx[0], dirs.pop()))
            types.add((idx[-1], idx[0]))
        else:  # not a clean bus: keep the bits as single pins
            singles += [p for _, p in items]
    out = [f"library ({master}_blackbox) {{",
           "  /* black-box model written by sipath.blackbox from the macro's LEF; no timing arcs */",
           "  delay_model : table_lookup;", '  time_unit : "1ns";', '  voltage_unit : "1V";',
           '  current_unit : "1mA";', "  capacitive_load_unit (1, pf);", '  pulling_resistance_unit : "1kohm";',
           f"  nom_voltage : {nom_voltage:g}; nom_temperature : 25; nom_process : 1;"]
    for hi, lo in sorted(types):
        out.append(f"  type (bus_{hi}_{lo}) {{ base_type : array; data_type : bit; bit_width : {hi - lo + 1}; "
                   f"bit_from : {hi}; bit_to : {lo}; downto : true; }}")
    out += [f'  cell ("{master}") {{', f"    area : {w * h:.3f};", "    dont_touch : true; dont_use : true;"]
    for p in singles:
        out.append(f'    pin ("{p["name"]}") {{ direction : {p["direction"]}; capacitance : {capacitance}; }}')
    for base, hi, lo, d in buses:
        out.append(f'    bus ("{base}") {{ bus_type : bus_{hi}_{lo}; direction : {d}; capacitance : {capacitance}; }}')
    out += ["  }", "}", ""]
    report = {"pins": len(sig), "buses": len(buses), "power_pins_left_out": skipped,
              "direction_assumed_inout": assumed}
    return "\n".join(out), report


def wanted(design, macro) -> bool:
    """A model is generated for a macro without lib when the macro asks for it
    (``auto_blackbox: true``), or when the design does and the macro does not refuse."""
    if macro.lib:
        return False
    return macro.auto_blackbox if macro.auto_blackbox is not None else bool(design.auto_blackbox)


def generate(design, pdk, out_dir: str) -> dict:
    """Write models for every macro that wants one and point its lib there.
    Returns {instance: {"lib": path, **report}}."""
    from .signoff import lef_macro
    made = {}
    for m in design.macros:
        if not wanted(design, m):
            continue
        master = lef_macro(m.lef)[0]
        volts = [v for k, v in (pdk.voltages or {}).items() if str(k).upper().startswith(("VDD", "VCC", "VPWR"))]
        text, rep = liberty(master, m.lef, m.supplies, nom_voltage=float(volts[0]) if volts else 1.8)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{master}.blackbox.lib")
        with open(path, "w") as f:
            f.write(text)
        m.lib = path
        made[m.inst] = {"lib": path, "master": master, **rep}
    return made
