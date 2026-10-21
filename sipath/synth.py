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

from .lef import lef_macro
from .tools import yosys
from .tools.run import run_tool
from .tools.run import runner as _runner

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
    r = _runner(ctx)
    key = getattr(r, "image", None) or getattr(r, "binary", None) or id(r)
    if key not in _SLANG:
        found = None
        for mode, pre in (("slang", ""), ("slang-plugin", "plugin -i slang; ")):
            try:
                out = yosys.command(ctx, ["-p", f"{pre}help read_slang"], ctx.path(f"probe_{mode}.log"), timeout=300)
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


# Yosys flop and latch cell types after `synth`, before dfflibmap (keep_names)
FF_TYPES = ("$_DFF*", "$_DFFE*", "$_SDFF*", "$_SDFFE*", "$_DFFSR*", "$_DFFSRE*", "$_ALDFF*", "$_ALDFFE*",
            "$_DLATCH*", "$_DLATCHSR*")
ADDERS = ("kogge-stone", "han-carlson", "sklansky")
POST_SYNTH = {"opt_dff_sat": "opt_dff -sat", "opt_full": "opt -full"}


def front_end_options(tokens) -> tuple[list[str], list[str]]:
    """(``synth`` flags, commands after it) for ``synth_opts`` tokens: booth
    (Booth-encoded multipliers), adder=kogge-stone|han-carlson|sklansky (parallel
    prefix adders), noshare, nofsm, noalumacc, opt_dff_sat, opt_full (from
    shalan/synth_flow, where their effect was measured and found design-specific)."""
    flags, post = [], []
    for tok in tokens or []:
        t = str(tok).strip().lower()
        if t == "booth":
            flags.append("-booth")
        elif t.startswith("adder="):
            arch = t.split("=", 1)[1]
            if arch not in ADDERS:
                raise ValueError(f"synth_opts: unknown adder {arch!r} (one of {ADDERS})")
            flags.append(f"-extra-map +/choices/{arch}.v")
        elif t in ("noshare", "nofsm", "noalumacc"):
            flags.append(f"-{t}")
        elif t in POST_SYNTH:
            post.append(POST_SYNTH[t])
        elif t:
            raise ValueError(f"synth_opts: unknown option {tok!r} (booth, adder=<{'|'.join(ADDERS)}>, noshare, "
                             f"nofsm, noalumacc, {', '.join(POST_SYNTH)})")
    if post:  # post-synth optimisation can bring coarse cells back: lower them again
        post += ["techmap", "opt -fast"]
    return flags, post


def abc_target_ps(target, period_ps: float | None) -> float | None:
    """The ABC delay target: none (default; synth_flow measured -D to hurt
    timing at the slow corner on most designs), period (the SDC clock), or ps."""
    if target in (None, "", "none"):
        return None
    if target == "period":
        return period_ps
    try:
        v = float(target)
    except (TypeError, ValueError):
        raise ValueError(f"synth: abc_target must be none, period or a number of ps, not {target!r}") from None
    if v <= 0:
        raise ValueError("synth: abc_target must be > 0 ps")
    return v


def _liberty_load_unit_ff(liberty: str) -> float:
    """fF per Liberty capacitance unit (capacitive_load_unit (1, pf) -> 1000)."""
    with open(liberty, errors="replace") as f:
        head = f.read(200000)
    m = re.search(r"capacitive_load_unit\s*\(\s*([0-9.eE+-]+)\s*,\s*\"?(\w+)", head)
    if not m:
        return 1000.0
    return float(m.group(1)) * {"ff": 1.0, "pf": 1000.0, "nf": 1e6}.get(m.group(2).lower(), 1000.0)


def abc_boundary(pdk, d, std: str) -> tuple[str, float] | None:
    """(driving cell, output load in fF) for ABC's constraint file: the SDC's
    first ``set_driving_cell -lib_cell`` and ``set_load`` (Tcl variables
    resolved), else the PDK's boundary defaults for an embedded macro (a die's
    ports are pads); None when neither gives a driving cell. ABC reads only these
    two statements, with the load in fF (an SDC passed to it as is loses the
    load: OpenSTA's ``set_load -pin_load`` reads as 0)."""
    with open(d.sdc) as f:
        sdc = f.read()
    vars_ = dict(re.findall(r"^\s*set\s+(\w+)\s+([0-9.eE+-]+)\s*$", sdc, re.M))
    val = lambda v: float(vars_.get(v.strip("${}"), "nan")) if v.startswith("$") else float(v)  # noqa: E731
    cell = load = None
    m = re.search(r"set_driving_cell\b[^\n]*?-lib_cell\s+(\S+)", sdc)
    if m:
        cell = m.group(1).split("/")[-1]
    for ln in sdc.splitlines():
        toks = ln.split("#", 1)[0].split()
        if toks[:1] == ["set_load"]:
            vals = [x for x in toks[1:] if re.fullmatch(r"\$\{?\w+\}?|[0-9.]+(?:[eE][+-]?\d+)?", x)]
            if vals:
                load = val(vals[0])
                break
    c = dict(pdk.constraints or {}) if d.mode == "macro" else {}
    if cell is None and c.get("driving_cell"):
        cell = c["driving_cell"][0]
    if load is None and c.get("output_load") is not None:
        load = float(c["output_load"])
    if not cell:
        return None
    ff = (load or 0.0) * _liberty_load_unit_ff(std)
    return cell, round(ff, 4)


def synth_script(pdk, d, out: str, abc_script: str, port_buffers: bool = True, fe: str = "yosys",
                 retime: bool = False, opts=(), keep_names: bool = True, constr: str | None = None,
                 synth_abc: bool = False, corner=None) -> str:
    """Generic synthesis (``synth -noabc``, front-end options), register names
    kept, flops mapped (dfflibmap), logic mapped by ABC with ``abc_script`` (a
    file with the recipe, {D} resolved) and the boundary model ``constr``, on
    the Liberty of ``corner`` (None: the typical one)."""
    std, others = _corner_libs(pdk, d, corner) if corner is not None else _libs(pdk, d)
    du = " ".join(f"-dont_use {p}" for p in dont_use(pdk, d))
    flags, post = front_end_options(opts)
    lines = [f"read_liberty -lib -overwrite -setattr liberty_cell {f}" for f in [std, *others]]
    lines += _read_rtl(d, autowire=True, frontend=fe)
    lines += [f"hierarchy -check -top {d.top}",
              " ".join([f"synth -top {d.top} -flatten" + ("" if synth_abc else " -noabc"), *flags]),
              *post,
              *(["abc -dff -D 1"] if retime else []),  # retiming (what synth -retime ran); async-reset flops stay
              *([f"rename -wire -suffix _reg {' '.join('t:' + x for x in FF_TYPES)}"] if keep_names else []),
              "opt -purge",
              f"dfflibmap -liberty {std} {du}",
              "opt",
              f"abc -liberty {std} {du}" + (f" -constr {constr}" if constr else "") + f" -script {abc_script}",
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


# reset port names (any case) and their active level, tried in order when equiv_reset is not given: common
# names, AMBA's (PRESETn, HRESETn, ARESETn) and lowRISC's rst_ni
RESET_NAMES = (("rst_n", 0), ("resetn", 0), ("reset_n", 0), ("rstn", 0), ("nrst", 0), ("rst_ni", 0), ("presetn", 0),
               ("hresetn", 0), ("aresetn", 0), ("arst_n", 0), ("rst", 1), ("reset", 1), ("arst", 1))


def find_reset(ports) -> tuple[str, int] | None:
    """(port, active level) of the first RESET_NAMES entry among ``ports``, matched without case."""
    by_lower = {p.lower(): p for p in ports}
    return next(((by_lower[n], lvl) for n, lvl in RESET_NAMES if n in by_lower), None)


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
        reset = find_reset(ports)
    depth, timeout = int(p.get("equiv_depth", 10)), int(p.get("equiv_timeout", 600))
    script = ctx.path("equiv.ys")
    with open(script, "w") as f:
        f.write(equiv_script(pdk, d, netlist, depth, reset, timeout, ctx.outputs.get("frontend", "yosys")))
    log = ctx.path("equiv.log")
    try:
        yosys.script(ctx, script, log, ctx.path("equiv.out"))
    except Exception:
        pass  # the log tells: a counterexample (FAIL) or a timeout
    text = open(log, errors="replace").read() if os.path.isfile(log) else ""
    res = {"depth": depth, "reset": f"{reset[0]}={reset[1]}" if reset else None, "log": log}
    if "SAT proof finished - no model found: SUCCESS" in text:
        res["result"] = f"equivalent for {depth} cycles from reset"
    elif "FAIL" in text and "model found" in text:
        res["result"] = "mismatch"
        tail = text[text.rfind("Time Signal Name"):][:3000] if "Time Signal Name" in text else text[-3000:]
        hint = "" if reset else (" (no reset port recognised, so the registers start undefined; name it with "
                                 "equiv_reset, e.g. PRESETn:0)")
        raise RuntimeError(f"synth: the netlist differs from the RTL within {depth} cycles from reset{hint} "
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
        # an unknown /*verilator ...*/ comment (another tool version's pragma, e.g. ORFS's
        # "-xinit: zeros") is a counted warning, not a design error
        args = [*binary, "--lint-only", "-Wall", "-Wno-fatal", "-Wwarn-BADVLTPRAGMA", "--top-module", d.top,
                *defs, *incs, *extra, *d.rtl]
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
{constraints}
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


def map_corner(pdk, name: str | None = None):
    """The corner whose Liberty synthesis maps with: ``name``, ``typical`` for
    the typical one, else (default) the corner it is judged at, the slow one:
    on the 16-design bench that met timing on 12 designs against 10 when
    mapping at the typical corner, slack better on 14."""
    if not pdk.corners:
        return None
    if name == "typical":
        return pdk.typical()
    return timing_corner(pdk, name)


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
    from .tools.openroad.tcl import constraints_setup
    d, pdk = ctx.design, ctx.pdk
    corner = timing_corner(pdk, (ctx.params or {}).get("timing_corner"))
    std, others = _corner_libs(pdk, d, corner)
    wl = default_wire_load(std)
    script = ctx.path("synth_sta.tcl")
    with open(script, "w") as f:
        lefs = [pdk.tech_lef, *pdk.cell_lefs, *d.extra_lefs, *[m.lef for m in d.macros]]
        f.write(STA_TCL.format(lefs="\n".join(f"read_lef {x}" for x in lefs), std=std,
                               others="\n".join(f"read_liberty {o}" for o in others), netlist=netlist,
                               top=d.top, sdc=d.sdc, constraints="\n".join(constraints_setup(pdk, d)),
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
    against the RTL (equivalence from reset, bounded). ABC maps with each
    recipe of ``abc_sweep`` (or ``abc`` alone) and ``synth_select`` keeps one.
    params: see sipath.params (synth)."""
    from .netlist import read_verilog
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
            yosys.script(ctx, script, log, ctx.path("lint.out"))
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
    from . import scores
    period = p.get("abc_period_ps") or clock_period_ps(d.sdc)
    target = abc_target_ps(p.get("abc_target", "none"), period)
    std, _ = _libs(pdk, d)
    constr = None  # written into each recipe's own directory (its container's working mount)
    bound = abc_boundary(pdk, d, std) if p.get("abc_boundary", True) else None
    if bound:
        constr = f"set_driving_cell {bound[0]}\nset_load {bound[1]}\n"
        ctx.outputs["abc_boundary"] = {"driving_cell": bound[0], "load_ff": bound[1]}
    names = sweep_recipes(p)
    if len(names) == 1:
        results = [_synth_one(ctx, names[0], ctx.dir, fe, target, constr, period)]
    else:  # one mapping per recipe, side by side; synth_select picks, the winner alone is proven equivalent
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(names), 6)) as ex:
            results = list(ex.map(lambda n: _synth_one(ctx, n, ctx.path(os.path.join("abc", n)), fe, target, constr,
                                                       period), names))
    ok = [r for r in results if not r.error]
    if not ok:
        raise RuntimeError("synth: no recipe produced a netlist:\n" + "\n".join(f"  {r.params['abc']}: {r.error}"
                                                                             for r in results))
    win, why = (ok[0], "the only recipe") if len(results) == 1 else \
        scores.synth_select(ok, p.get("synth_fallback", "knee"))
    if win is None:
        raise RuntimeError(f"synth: no recipe selected ({why})")
    out = ctx.path(f"{d.top}.synth.v")
    if win.out != out:
        import shutil as _sh
        _sh.copyfile(win.out, out)
        _sh.copyfile(win.out + ".stat", out + ".stat")
    ctx.metrics.update(win.metrics)
    ctx.outputs.update(checkpoint=out, netlist=out, synth_log=win.log, synth_script=win.script)
    if win.timing:
        ctx.outputs["synth_timing"] = win.timing
    if len(results) > 1:
        ctx.outputs["abc_sweep"] = {"selected": win.params["abc"], "why": why,
                                    "results": [{"recipe": r.params["abc"], "error": r.error,
                                                 **{k: r.metrics.get(k) for k in ("synth_wns_ns", "synth_tns_ns",
                                                                                  "synth_area_um2", "synth_cells")}}
                                                for r in results]}
    if p.get("equiv", "bounded") != "off":
        eq = equivalence(ctx, out)
        ctx.outputs["equiv"] = eq
        ctx.metrics["equiv"] = eq["result"]


synth.tool = True  # the scheduler runs it without an OpenROAD process


def sweep_recipes(p) -> list[str]:
    """The ABC recipes one synth attempt maps with: ``abc`` alone when given,
    else ``abc_sweep``: auto (synth_flow's five delay recipes plus area_map, so a
    loose design lands on a lean netlist and a tight one on a fast one), delay,
    area, balanced, off (delay_map_resyn alone), or recipe names separated by
    commas."""
    from . import recipes
    if p.get("abc"):
        return [recipes.resolve(p["abc"])]
    sw = str(p.get("abc_sweep") or "auto")
    if sw == "off":
        return ["delay_map_resyn"]
    if sw == "auto":
        return recipes.SETS["delay"] + ["area_map"]
    if sw in recipes.SETS:
        return list(recipes.SETS[sw])
    return [recipes.resolve(n.strip()) for n in sw.split(",") if n.strip()]


_SIGNED = re.compile(r"^(\s*(?:input|output|inout|wire|reg)\s+)signed\s+", re.M)


def strip_signed(netlist: str) -> int:
    """Drop ``signed`` from the gate-level netlist's declarations: Yosys keeps
    the RTL's signedness on ports (``input signed [15:0] a;``), which a gate
    netlist does not need and OpenSTA's (and SiPath's) Verilog reader rejects.
    Returns the declarations changed."""
    with open(netlist) as f:
        text = f.read()
    new, n = _SIGNED.subn(r"\1", text)
    if n:
        with open(netlist, "w") as f:
            f.write(new)
    return n


def _synth_one(ctx, recipe: str, wdir: str, fe: str, target, constr, period):
    """Map with one recipe in ``wdir`` (``constr``: ABC's constraint text or
    None): netlist, checks, area, timing estimate. Returns a candidate
    (metrics, params, out, log, script, timing, error)."""
    import types
    from . import recipes
    from .netlist import read_verilog
    d, pdk, p = ctx.design, ctx.pdk, ctx.params
    os.makedirs(wdir, exist_ok=True)
    view = types.SimpleNamespace(dir=wdir, path=lambda n: os.path.join(wdir, n), design=d, pdk=pdk, params=p,
                                 runner=getattr(ctx, "runner", None), config=getattr(ctx, "config", None),
                                 session=getattr(ctx, "session", None))
    cand = types.SimpleNamespace(params={"abc": recipe}, metrics={}, out=None, log=None, script=None, timing=None,
                                 error=None)
    try:
        abc_file = view.path(f"{recipe}.abc")
        with open(abc_file, "w") as f:
            f.write(recipes.materialize(recipe, target, bool(p.get("abc_wire_load", False))) + "\n")
        out, script, log = view.path(f"{d.top}.synth.v"), view.path("synth.ys"), view.path("synth.log")
        constr_file = None
        if constr:  # a file next to the recipe: Docker Desktop did not always show one written a level up
            constr_file = view.path("abc.constr")
            with open(constr_file, "w") as f:
                f.write(constr)
        with open(script, "w") as f:
            f.write(synth_script(pdk, d, out, abc_file, p.get("port_buffers", True), fe, bool(p.get("retime", False)),
                                 p.get("synth_opts") or (), bool(p.get("keep_names", True)), constr_file,
                                 bool(p.get("synth_abc", False)), map_corner(pdk, p.get("map_corner"))))
        yosys.script(view, script, log, view.path("synth.out"))
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError(f"Yosys wrote no netlist at {out}")
        strip_signed(out)
        std, others = _libs(pdk, d)  # every instance a Liberty cell or a macro; nothing excluded
        lib_cells = _liberty_cells([std, *others])
        known = set(lib_cells) | {lef_macro(m.lef)[0] for m in d.macros}
        insts = read_verilog(out, d.top).instances
        unmapped = sorted({i.cell for i in insts if i.cell not in known})
        if unmapped:
            raise RuntimeError(f"cells left unmapped (no Liberty cell: latches or unsupported logic?): {unmapped[:10]}")
        bad = d.excluded_in(out, pdk)
        if bad:
            raise RuntimeError(f"netlist uses excluded cells {bad}")
        with open(out + ".stat") as f:
            area = re.findall(r"Chip area for (?:top )?module\s+'?\\?\S+?'?:\s*([0-9.]+)", f.read())
        cand.metrics.update(synth_cells=len(insts), synth_area_um2=float(area[-1]) if area else None,
                            synth_flops=sum(1 for i in insts if lib_cells.get(i.cell)), abc_recipe=recipe,
                            abc_target_ps=target, synth_period_ns=(period / 1000) if period else None)
        if p.get("timing", True):
            cand.timing = synth_timing(view, out)
            cand.metrics.update(synth_wns_ns=cand.timing["wns_ns"], synth_tns_ns=cand.timing["tns_ns"],
                                synth_timing_corner=cand.timing["corner"])
        cand.out, cand.log, cand.script = out, log, script
    except Exception as e:  # one recipe failing does not fail the sweep
        cand.error = f"{type(e).__name__}: {str(e).strip().splitlines()[-1][:300] if str(e).strip() else ''}"
    return cand
