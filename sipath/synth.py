"""Synthesis: RTL -> flat gate-level netlist with Yosys (a tool step: no
OpenROAD process; its result is the netlist the next step loads).

1. Lint (param ``lint``): read with implicit nets as errors, check the
   hierarchy, then ``check -assert`` (undriven nets, several drivers,
   combinational loops are errors). Warnings are counted; with
   ``lint_strict`` any warning fails.
2. Synthesis on the PDK's typical Liberty: generic synthesis flattened, flops
   mapped (dfflibmap), logic mapped by ABC (with the SDC clock period as the
   delay target unless ``abc: area``), constants on tie cells, port assigns
   buffered. Every cell the PDK or the design excludes is dont_use for both
   mappers.
3. Checks on the written netlist: every instance is a Liberty cell or a macro
   (a latch or other unmapped cell fails), no excluded cell.

Front ends (param ``frontend``): Yosys's own Verilog reader, or slang
(``read_slang``: full SystemVerilog) when the image's Yosys has it, built in
or as the ``slang`` plugin; ``auto`` takes slang for ``.sv`` sources. Lint,
synthesis and the equivalence check read the RTL the same way.

Verilator lint (param ``verilator``, P2.7): ``--lint-only -Wall`` on the RTL
(host binary, a native tool, or the image's); warnings counted by code, errors
fail. Liberty cells and macros the RTL instantiates get empty stub modules.

Timing estimate (param ``timing``): OpenSTA on the netlist at the slow corner
(``timing_corner``) with the SDC and the Liberty's wire-load model:
``synth_wns_ns`` / ``synth_tns_ns``, so a sweep over synthesis variants
(``abc`` goal, ``retime``) can be scored (``synth_wns_then_area``).

Metrics: synth_area_um2, synth_cells, synth_flops, lint_warnings, verilator_warnings, synth_wns_ns, synth_tns_ns.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from .signoff import run_tool

SV_SUFFIXES = (".sv", ".svh")
_SLANG = {}  # runner identity -> "builtin" | "plugin" | None


def _libs(pdk, d) -> tuple[str, list[str]]:
    """(typical standard-cell Liberty, other Liberty files read as black boxes)."""
    std = pdk.typical().liberty if pdk.corners else pdk.liberty
    others = list(d.extra_libs)
    for m in d.macros:
        if isinstance(m.lib, dict):
            proc = pdk.typical().process if pdk.corners else ""
            lib = m.lib.get(pdk.typical().name) or m.lib.get(proc) or next(iter(m.lib.values()))
            others.append(lib)
        elif m.lib:
            others.append(m.lib)
    return std, others


def _read_rtl(d, autowire: bool, frontend: str = "yosys") -> list[str]:
    """Yosys commands reading the RTL: ``yosys`` (read_verilog -sv), ``slang``
    (read_slang, built in) or ``slang-plugin`` (the slang plugin, loaded first)."""
    defs = " ".join(f"-D{k}" + (f"={v}" if v not in ("", None) else "") for k, v in d.rtl_defines.items())
    incs = " ".join(f"-I{i}" for i in d.rtl_includes)
    if frontend.startswith("slang"):
        flags = " ".join(x for x in (f"--top {d.top}", "--empty-blackboxes", defs, incs) if x)
        return (["plugin -i slang"] if frontend == "slang-plugin" else []) + [f"read_slang {flags} {' '.join(d.rtl)}"]
    flags = " ".join(x for x in ("-sv", "" if autowire else "-noautowire", defs, incs) if x)
    return [f"read_verilog {flags} {' '.join(d.rtl)}"]


def slang_support(ctx) -> str | None:
    """How the installation's Yosys reads SystemVerilog with slang: "slang"
    (read_slang built in), "slang-plugin" (the plugin) or None."""
    from .signoff import _runner
    r = _runner(ctx)
    key = getattr(r, "image", None) or getattr(r, "binary", None) or id(r)
    if key not in _SLANG:
        found = None
        for mode, pre in (("slang", ""), ("slang-plugin", "plugin -i slang; ")):
            try:
                out = run_tool(ctx, ["yosys", "-p", f"{pre}help read_slang"], ctx.path(f"probe_{mode}.log"), timeout=300)
            except Exception:
                continue
            if "read_slang [options]" in out:
                found = mode
                break
        _SLANG[key] = found
    return _SLANG[key]


def frontend(ctx) -> str:
    """The RTL reader for this attempt (param ``frontend``: auto|yosys|slang)."""
    want = (ctx.params or {}).get("frontend", "auto")
    if want == "yosys":
        return "yosys"
    sv = any(f.endswith(SV_SUFFIXES) for f in ctx.design.rtl)
    if want == "auto" and not sv:
        return "yosys"
    have = slang_support(ctx)
    if have:
        return have
    if want == "slang":
        raise RuntimeError("synth: frontend slang asked for, but this Yosys has no read_slang (built in or plugin)")
    return "yosys"  # auto with .sv sources and no slang: Yosys's own SystemVerilog subset


def dont_use(pdk, d) -> list[str]:
    """Cell patterns neither mapper may choose."""
    pats = list(pdk.dont_use) + list(d.dont_use) + sorted(c for c in pdk.exclude if c not in d.allow_excluded)
    return list(dict.fromkeys(pats))


def clock_period_ps(sdc: str) -> float | None:
    """First ``create_clock -period`` of the SDC (ns) in ps; a period given as a
    Tcl variable (``set clk_period 4.5`` ... ``-period $clk_period``) is resolved."""
    with open(sdc) as f:
        text = f.read()
    vars_ = dict(re.findall(r"^\s*set\s+(\w+)\s+([0-9.]+)\s*$", text, re.M))
    m = re.search(r"create_clock\b[^\n]*?-period\s+(\$\{?\w+\}?|[0-9.]+)", text)
    if not m:
        return None
    v = m.group(1)
    if v.startswith("$"):
        v = vars_.get(v.strip("${}"))
    return float(v) * 1000 if v else None


def lint_script(pdk, d, fe: str = "yosys") -> str:
    std, others = _libs(pdk, d)
    lines = [f"read_liberty -lib -overwrite {f}" for f in [std, *others]]
    lines += _read_rtl(d, autowire=False, frontend=fe)
    lines += [f"hierarchy -check -top {d.top}", "proc", "opt_clean", "check -assert"]
    return "\n".join(lines) + "\n"


def synth_script(pdk, d, out: str, period_ps: float | None, abc: str, port_buffers: bool = True,
                 fe: str = "yosys", retime: bool = False) -> str:
    std, others = _libs(pdk, d)
    du = " ".join(f"-dont_use {p}" for p in dont_use(pdk, d))
    lines = [f"read_liberty -lib -overwrite -setattr liberty_cell {f}" for f in [std, *others]]
    lines += _read_rtl(d, autowire=True, frontend=fe)
    lines += [f"hierarchy -check -top {d.top}",
              f"synth -top {d.top} -flatten",
              *(["abc -dff -D 1"] if retime else []),  # retiming (what synth -retime ran); async-reset flops stay
              "opt -purge",
              f"dfflibmap -liberty {std} {du}",
              "opt",
              _abc(std, du, abc, period_ps),
              "setundef -zero",
              "splitnets",
              "opt_clean -purge"]
    ties = []
    if pdk.tie_hi:
        ties.append(f"-hicell {pdk.tie_hi[0]} {pdk.tie_hi[1]}")
    if pdk.tie_lo:
        ties.append(f"-locell {pdk.tie_lo[0]} {pdk.tie_lo[1]}")
    if ties:
        lines.append(f"hilomap -singleton {' '.join(ties)}")
    if pdk.synth_buffer and port_buffers:
        lines.append(f"insbuf -buf {' '.join(pdk.synth_buffer)}")
    lines += ["opt_clean -purge", "check", f"tee -o {out}.stat stat -liberty {std}",
              f"write_verilog -noattr -noexpr -nohex -nodec {out}"]
    return "\n".join(lines) + "\n"


# ABC scripts per goal (Yosys replaces {D} with "-D <target ps>"): area-oriented
# mapping; the default delay mapping toward the clock; delay mapping, then buffering
# and gate sizing toward it
ABC_SCRIPTS = {
    "area": "+strash;dch,-f;map,-a;topo",
    "speed": "+strash;dch,-f;map,{D};topo;buffer,-p;upsize,{D};dnsize,{D}",
}


def _abc(std: str, du: str, goal: str, period_ps: float | None) -> str:
    """The ABC mapping command for ``goal`` (delay | area | speed)."""
    cmd = f"abc -liberty {std} {du}"
    if goal in ("delay", "speed") and period_ps:
        cmd += f" -D {period_ps:g}"
    if goal == "area" or (goal == "speed" and period_ps):
        cmd += f" -script {ABC_SCRIPTS[goal]}"
    return cmd


# reset port names and their active level, tried in order when equiv_reset is not given
RESET_NAMES = (("rst_n", 0), ("resetn", 0), ("reset_n", 0), ("rstn", 0), ("nrst", 0), ("rst", 1), ("reset", 1))


def equiv_script(pdk, d, netlist: str, depth: int, reset: tuple[str, int] | None, timeout: int,
                 fe: str = "yosys") -> str:
    """Yosys script: the RTL (as synthesis reads it) against the gate-level
    netlist (cell functions from the Liberty file), as a miter checked from
    reset for ``depth`` clock cycles (bounded: equal outputs in every cycle of
    every input sequence from reset)."""
    std, _ = _libs(pdk, d)
    rst = f" -set-at 1 in_{reset[0]} {reset[1]}" if reset else ""
    return "\n".join([
        *_read_rtl(d, True, fe), f"hierarchy -check -top {d.top}", "proc",
        "memory",  # RTL memories (e.g. lookup tables) to logic: SAT has no model for memory cells
        "flatten", "opt_clean", f"rename {d.top} gold", "design -stash gold",
        f"read_liberty -ignore_miss_func {std}", f"read_verilog {netlist}", f"hierarchy -check -top {d.top}",
        "proc", "flatten", "opt_clean", f"rename {d.top} gate", "design -stash gate",
        "design -copy-from gold -as gold gold", "design -copy-from gate -as gate gate",
        "miter -equiv -flatten -make_assert -ignore_gold_x gold gate miter", "hierarchy -top miter",
        "async2sync", "opt_clean",
        f"sat -verify -prove-asserts -set-init-undef -set-def-inputs{rst} -seq {depth} -prove-skip 1 "
        f"-timeout {timeout} miter", ""])


def equivalence(ctx, netlist: str) -> dict:
    """The synthesized netlist against the RTL (bounded from reset); a
    mismatch fails the step, a timeout is inconclusive (reported)."""
    d, pdk, p = ctx.design, ctx.pdk, ctx.params
    if d.macros:
        return {"result": "skipped", "reason": "macros are black boxes to the check"}
    from .netlist import read_verilog
    ports = {pt.name for pt in read_verilog(netlist, d.top).ports}
    given = p.get("equiv_reset")
    if given:
        name, _, level = given.partition(":")
        if name not in ports:
            raise ValueError(f"synth: equiv_reset port {name!r} is not a port of {d.top}")
        reset = (name, int(level or 0))
    else:
        reset = next(((n, lvl) for n, lvl in RESET_NAMES if n in ports), None)
    depth, timeout = int(p.get("equiv_depth", 10)), int(p.get("equiv_timeout", 600))
    script = ctx.path("equiv.ys")
    with open(script, "w") as f:
        f.write(equiv_script(pdk, d, netlist, depth, reset, timeout, ctx.outputs.get("frontend", "yosys")))
    log = ctx.path("equiv.log")
    try:
        run_tool(ctx, ["yosys", "-q", "-l", log, "-s", script], ctx.path("equiv.out"))
    except Exception:
        pass  # the log tells: a counterexample (FAIL) or a timeout
    text = open(log, errors="replace").read() if os.path.isfile(log) else ""
    res = {"depth": depth, "reset": f"{reset[0]}={reset[1]}" if reset else None, "log": log}
    if "SAT proof finished - no model found: SUCCESS" in text:
        res["result"] = f"equivalent for {depth} cycles from reset"
    elif "FAIL" in text and "model found" in text:
        res["result"] = "mismatch"
        tail = text[text.rfind("Time Signal Name"):][:3000] if "Time Signal Name" in text else text[-3000:]
        raise RuntimeError(f"synth: the netlist differs from the RTL within {depth} cycles from reset "
                           f"(log {log}):\n{tail}")
    elif "timeout" in text.lower() or "Interrupted" in text:
        res["result"] = "inconclusive (timeout)"
    else:
        raise RuntimeError(f"synth: equivalence check did not run to a result (log {log})")
    return res


def liberty_stubs(paths, names) -> str:
    """Empty Verilog modules (ports and directions, buses with their widths)
    for the Liberty cells ``names``: Verilator needs a module for every
    instance, Yosys's black boxes are not visible to it."""
    out, want = [], set(names)
    for p in paths:
        with open(p, errors="replace") as f:
            text = f.read()
        widths = {m.group(1): int(m.group(2)) for m in
                  re.finditer(r'type\s*\(\s*"?(\w+)"?\s*\)\s*\{[^}]*?bit_width\s*:\s*(\d+)', text)}
        marks = list(re.finditer(r'^\s*cell\s*\(\s*"?([^")\s]+)"?\s*\)', text, re.M))
        for k, m in enumerate(marks):
            if m.group(1) not in want:
                continue
            want.discard(m.group(1))
            body = text[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(text)]
            ports = []
            for pm in re.finditer(r'^\s*(pin|bus)\s*\(\s*"?([^")\s]+)"?\s*\)\s*\{', body, re.M):
                rest = body[pm.end():pm.end() + 600]
                dm = re.search(r'direction\s*:\s*"?(\w+)', rest)
                direction = {"input": "input", "output": "output"}.get(dm.group(1) if dm else "", "inout")
                width = 1
                if pm.group(1) == "bus":
                    bt = re.search(r'bus_type\s*:\s*"?(\w+)', rest)
                    width = widths.get(bt.group(1), 1) if bt else 1
                name = pm.group(2)
                if pm.group(1) == "pin" and "[" in name:
                    continue  # a bit of a bus already declared
                ports.append((direction, width, name))
            decl = ", ".join(f"{dr} {'[%d:0] ' % (w - 1) if w > 1 else ''}{n}" for dr, w, n in ports)
            out.append(f"module {m.group(1)}({decl});\nendmodule")
    return "\n".join(out) + ("\n" if out else "")


def verilator_binary(ctx) -> list[str] | None:
    """How to run Verilator here: a native tool, the host's, or the image's
    (checked with --version); None when there is none."""
    from .signoff import _runner
    r = _runner(ctx)
    if "verilator" in (getattr(r, "native_tools", None) or {}):
        return ["verilator"]
    host = shutil.which("verilator")
    if host:
        return [host]
    try:
        run_tool(ctx, ["verilator", "--version"], ctx.path("probe_verilator.log"), timeout=300)
        return ["verilator"]
    except Exception:
        return None


def verilator_lint(ctx) -> dict:
    """Verilator --lint-only -Wall on the RTL; Liberty cells and macros the RTL
    instantiates get stub modules. {"warnings": n, "by_code": {...}, "log": ...};
    errors fail the step."""
    d, pdk = ctx.design, ctx.pdk
    binary = verilator_binary(ctx)
    if binary is None:
        return {"skipped": "no verilator (host, native tool or image)"}
    host = binary[0] != "verilator"
    defs = [f"-D{k}" + (f"={v}" if v not in ("", None) else "") for k, v in d.rtl_defines.items()]
    # include paths: the design's, then each source's own directory (where Yosys looks for `include files)
    dirs = list(d.rtl_includes) + [x for x in dict.fromkeys(os.path.dirname(os.path.abspath(f)) for f in d.rtl)
                                   if x not in d.rtl_includes]
    incs = [f"-I{i}" for i in dirs]
    log, stubs = ctx.path("verilator.log"), ctx.path("verilator_stubs.v")
    std, others = _libs(pdk, d)
    extra, out = [], ""
    for _ in range(2):  # a second run with stubs for the library modules it could not find
        args = [*binary, "--lint-only", "-Wall", "-Wno-fatal", "--top-module", d.top, *defs, *incs, *extra, *d.rtl]
        if host:
            p = subprocess.run(args, capture_output=True, text=True, timeout=600, cwd=ctx.dir)
            out = p.stdout + p.stderr
            with open(log, "w") as f:
                f.write(f"# {' '.join(args)}\n# exit {p.returncode}\n{out}")
            rc = p.returncode
        else:
            try:
                out, rc = run_tool(ctx, args, log), 0
            except Exception as e:
                out, rc = open(log, errors="replace").read() if os.path.isfile(log) else str(e), 1
        # Verilator writes some characters as __0<hex> (a double underscore: ___05F)
        missing = sorted({re.sub(r"__0([0-9A-Fa-f]{2})", lambda h: chr(int(h.group(1), 16)), n)
                          for n in re.findall(r"Cannot find file containing module: '([^']+)'", out)})
        known = _liberty_cells([std, *others])
        if missing and not extra and set(missing) <= set(known):
            with open(stubs, "w") as f:
                f.write(liberty_stubs([std, *others], missing))
            extra = [stubs]
            continue
        break
    errors = [ln for ln in out.splitlines() if ln.startswith("%Error")]
    by_code = {}
    for code in re.findall(r"^%Warning-(\w+):", out, re.M):
        by_code[code] = by_code.get(code, 0) + 1
    if errors or rc != 0:
        raise RuntimeError(f"synth: Verilator lint errors (log {log}):\n" + "\n".join(errors[:10] or out.splitlines()[-10:]))
    return {"warnings": sum(by_code.values()), "by_code": by_code, "log": log, "stubs": sorted(extra and missing)}


STA_TCL = """{lefs}
read_liberty {std}
{others}
read_verilog {netlist}
link_design {top}
read_sdc {sdc}
{wire_load}
puts "SYNTHSTA [sta::worst_slack -max] [sta::total_negative_slack -max]"
"""


def timing_corner(pdk, name: str | None = None):
    """The corner synthesis is judged at: ``name``, else the worst-setup corner
    of the PDK's optimization set (the slow process), else the typical one."""
    if not pdk.corners:
        return None
    cs = pdk.signoff_corners()
    if name:
        by = {c.name: c for c in cs}
        if name not in by:
            raise ValueError(f"synth: timing_corner {name!r} is not a corner of {pdk.name}: {sorted(by)}")
        return by[name]
    slow = [c for c in pdk.analysis_corners() if c.process.lower().startswith("ss")]
    return slow[0] if slow else pdk.typical()


def _corner_libs(pdk, d, corner) -> tuple[str, list[str]]:
    """(standard-cell Liberty, other Liberty files) at ``corner`` (None: the PDK's single Liberty)."""
    if corner is None:
        return _libs(pdk, d)
    others = list(d.extra_libs)
    for m in d.macros:
        if isinstance(m.lib, dict):
            others.append(m.lib.get(corner.name) or m.lib.get(corner.process) or next(iter(m.lib.values())))
        elif m.lib:
            others.append(m.lib)
    return corner.liberty, others


def default_wire_load(liberty: str) -> str | None:
    """The Liberty's ``default_wire_load`` model name, if it has one."""
    with open(liberty, errors="replace") as f:
        head = f.read(200000)
    m = re.search(r'default_wire_load\s*:\s*"?([^";\s]+)', head)
    return m.group(1) if m else None


def synth_timing(ctx, netlist: str) -> dict:
    """Setup WNS/TNS of the synthesized netlist: OpenSTA at the slow corner
    (``timing_corner``) with the SDC and the Liberty's wire-load model (a
    pre-placement estimate, comparable across synthesis variants, judged the
    way synth_flow found reliable). Unconstrained designs give None."""
    from .signoff import _runner
    d, pdk = ctx.design, ctx.pdk
    corner = timing_corner(pdk, (ctx.params or {}).get("timing_corner"))
    std, others = _corner_libs(pdk, d, corner)
    wl = default_wire_load(std)
    script = ctx.path("synth_sta.tcl")
    with open(script, "w") as f:
        lefs = [pdk.tech_lef, *pdk.cell_lefs, *d.extra_lefs, *[m.lef for m in d.macros]]
        f.write(STA_TCL.format(lefs="\n".join(f"read_lef {x}" for x in lefs), std=std,
                               others="\n".join(f"read_liberty {o}" for o in others), netlist=netlist,
                               top=d.top, sdc=d.sdc,
                               wire_load=f"set_wire_load_mode top\nset_wire_load_model -name {wl}" if wl else ""))
    binary = getattr(_runner(ctx), "binary", None) or "openroad"
    out = run_tool(ctx, [binary, "-no_init", "-no_splash", "-exit", script], ctx.path("synth_sta.log"), timeout=1800)
    m = re.search(r"SYNTHSTA (\S+) (\S+)", out)
    if not m:
        raise RuntimeError(f"synth: no timing estimate (log {ctx.path('synth_sta.log')})")
    num = lambda x: round(float(x), 4) if abs(float(x)) < 1e29 else None  # noqa: E731
    return {"wns_ns": num(m.group(1)), "tns_ns": num(m.group(2)), "corner": corner.name if corner else None,
            "wire_load": wl}


def _liberty_cells(paths) -> dict[str, bool]:
    """{cell: sequential} of the Liberty files (sequential: has an ff or latch group)."""
    cells = {}
    head = re.compile(r'^\s*cell\s*\(\s*"?([^")\s]+)"?\s*\)', re.M)
    for p in paths:
        with open(p, errors="replace") as f:
            text = f.read()
        marks = list(head.finditer(text))
        for k, m in enumerate(marks):
            body = text[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(text)]
            cells[m.group(1)] = bool(re.search(r"^\s*(ff|latch|ff_bank|latch_bank)\s*\(", body, re.M))
    return cells


def synth(ctx):
    """RTL to a flat gate-level netlist (Yosys), then the netlist checked
    against the RTL (equivalence from reset, bounded). params: frontend
    (auto|yosys|slang), abc (delay|area), abc_period_ps, retime, lint,
    lint_strict, verilator (auto|on|off), timing, equiv, equiv_depth,
    equiv_reset, equiv_timeout."""
    from .netlist import read_verilog
    from .signoff import lef_macro
    d, pdk, p = ctx.design, ctx.pdk, ctx.params
    if not d.rtl:
        raise ValueError("synth: the design has no rtl files")
    for f in d.rtl:
        if not os.path.isfile(f):
            raise FileNotFoundError(f"synth: RTL file {f} not found")
    fe = frontend(ctx)
    ctx.outputs["frontend"] = fe
    ctx.metrics["frontend"] = fe.split("-")[0]
    if p.get("lint", True):
        script = ctx.path("lint.ys")
        with open(script, "w") as f:
            f.write(lint_script(pdk, d, fe))
        log = ctx.path("lint.log")
        try:
            run_tool(ctx, ["yosys", "-q", "-l", log, "-s", script], ctx.path("lint.out"))
        except Exception as e:
            nolib = [m.inst for m in d.macros if not m.lib]
            if nolib and "is not part of the design" in str(e):
                raise RuntimeError(f"{e}\nmacros without a Liberty model: {nolib}; give each a lib, define it in "
                                   "the RTL, or set auto_blackbox: true (design or macro) for a black-box model "
                                   "written from its LEF") from e
            raise
        with open(log, errors="replace") as f:
            warnings = [ln.strip() for ln in f if ln.startswith("Warning:")]
        ctx.metrics["lint_warnings"] = len(warnings)
        ctx.outputs["lint_warnings"] = warnings[:50]
        if warnings and p.get("lint_strict", False):
            raise RuntimeError(f"lint: {len(warnings)} warning(s) (lint_strict), e.g. {warnings[:3]}")
    if p.get("verilator", "auto") != "off":
        v = verilator_lint(ctx)
        if "skipped" in v and p.get("verilator") == "on":
            raise RuntimeError(f"synth: verilator: on, but {v['skipped']}")
        ctx.outputs["verilator"] = v
        if "warnings" in v:
            ctx.metrics["verilator_warnings"] = v["warnings"]
            if v["warnings"] and p.get("lint_strict", False):
                raise RuntimeError(f"lint: {v['warnings']} Verilator warning(s) (lint_strict): {v['by_code']}")
    period = p.get("abc_period_ps") or clock_period_ps(d.sdc)
    out = ctx.path(f"{d.top}.synth.v")
    script = ctx.path("synth.ys")
    with open(script, "w") as f:
        f.write(synth_script(pdk, d, out, period, p.get("abc", "delay"), p.get("port_buffers", True), fe,
                             bool(p.get("retime", False))))
    log = ctx.path("synth.log")
    run_tool(ctx, ["yosys", "-q", "-l", log, "-s", script], ctx.path("synth.out"))
    if not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"synth: Yosys wrote no netlist at {out}")
    # every instance must be a Liberty cell or a macro; nothing excluded
    std, others = _libs(pdk, d)
    lib_cells = _liberty_cells([std, *others])
    known = set(lib_cells) | {lef_macro(m.lef)[0] for m in d.macros}
    insts = read_verilog(out, d.top).instances
    unmapped = sorted({i.cell for i in insts if i.cell not in known})
    if unmapped:
        raise RuntimeError(f"synth: cells left unmapped (no Liberty cell: latches or unsupported logic?): "
                           f"{unmapped[:10]}")
    bad = d.excluded_in(out, pdk)
    if bad:
        raise RuntimeError(f"synth: netlist uses excluded cells {bad}")
    with open(out + ".stat") as f:
        stat = f.read()
    area = re.findall(r"Chip area for (?:top )?module\s+'?\\?\S+?'?:\s*([0-9.]+)", stat)
    ctx.metrics.update(synth_cells=len(insts), synth_area_um2=float(area[-1]) if area else None,
                       synth_flops=sum(1 for i in insts if lib_cells.get(i.cell)),
                       abc_period_ps=period if p.get("abc", "delay") in ("delay", "speed") else None)
    ctx.outputs.update(checkpoint=out, netlist=out, synth_log=log, synth_script=script)
    if p.get("timing", True):
        st = synth_timing(ctx, out)
        ctx.metrics.update(synth_wns_ns=st["wns_ns"], synth_tns_ns=st["tns_ns"], synth_timing_corner=st["corner"])
        ctx.outputs["synth_timing"] = st
    if p.get("equiv", "bounded") != "off":
        eq = equivalence(ctx, out)
        ctx.outputs["equiv"] = eq
        ctx.metrics["equiv"] = eq["result"]


synth.tool = True  # the scheduler runs it without an OpenROAD process
