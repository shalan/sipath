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

Metrics: synth_area_um2, synth_cells, synth_flops, lint_warnings.
"""
from __future__ import annotations

import os
import re

from .signoff import run_tool


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


def _read_rtl(d, autowire: bool) -> list[str]:
    defs = " ".join(f"-D{k}" + (f"={v}" if v not in ("", None) else "") for k, v in d.rtl_defines.items())
    incs = " ".join(f"-I{i}" for i in d.rtl_includes)
    flags = " ".join(x for x in ("-sv", "" if autowire else "-noautowire", defs, incs) if x)
    return [f"read_verilog {flags} {' '.join(d.rtl)}"]


def dont_use(pdk, d) -> list[str]:
    """Cell patterns neither mapper may choose."""
    pats = list(pdk.dont_use) + list(d.dont_use) + sorted(c for c in pdk.exclude if c not in d.allow_excluded)
    return list(dict.fromkeys(pats))


def clock_period_ps(sdc: str) -> float | None:
    """First ``create_clock -period`` of the SDC (ns) in ps."""
    with open(sdc) as f:
        m = re.search(r"create_clock\b[^\n]*?-period\s+([0-9.]+)", f.read())
    return float(m.group(1)) * 1000 if m else None


def lint_script(pdk, d) -> str:
    std, others = _libs(pdk, d)
    lines = [f"read_liberty -lib -overwrite {f}" for f in [std, *others]]
    lines += _read_rtl(d, autowire=False)
    lines += [f"hierarchy -check -top {d.top}", "proc", "opt_clean", "check -assert"]
    return "\n".join(lines) + "\n"


def synth_script(pdk, d, out: str, period_ps: float | None, abc: str) -> str:
    std, others = _libs(pdk, d)
    du = " ".join(f"-dont_use {p}" for p in dont_use(pdk, d))
    lines = [f"read_liberty -lib -overwrite -setattr liberty_cell {f}" for f in [std, *others]]
    lines += _read_rtl(d, autowire=True)
    lines += [f"hierarchy -check -top {d.top}",
              f"synth -top {d.top} -flatten",
              "opt -purge",
              f"dfflibmap -liberty {std} {du}",
              "opt",
              f"abc -liberty {std} {du}" + (f" -D {period_ps:g}" if abc == "delay" and period_ps else ""),
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
    if pdk.synth_buffer:
        lines.append(f"insbuf -buf {' '.join(pdk.synth_buffer)}")
    lines += ["opt_clean -purge", "check", f"tee -o {out}.stat stat -liberty {std}",
              f"write_verilog -noattr -noexpr -nohex -nodec {out}"]
    return "\n".join(lines) + "\n"


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
    """RTL to a flat gate-level netlist (Yosys). params: abc (delay|area),
    abc_period_ps, lint, lint_strict."""
    from .netlist import read_verilog
    from .signoff import lef_macro
    d, pdk, p = ctx.design, ctx.pdk, ctx.params
    if not d.rtl:
        raise ValueError("synth: the design has no rtl files")
    for f in d.rtl:
        if not os.path.isfile(f):
            raise FileNotFoundError(f"synth: RTL file {f} not found")
    if p.get("lint", True):
        script = ctx.path("lint.ys")
        with open(script, "w") as f:
            f.write(lint_script(pdk, d))
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
    period = p.get("abc_period_ps") or clock_period_ps(d.sdc)
    out = ctx.path(f"{d.top}.synth.v")
    script = ctx.path("synth.ys")
    with open(script, "w") as f:
        f.write(synth_script(pdk, d, out, period, p.get("abc", "delay")))
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
                       abc_period_ps=period if p.get("abc", "delay") == "delay" else None)
    ctx.outputs.update(checkpoint=out, netlist=out, synth_log=log, synth_script=script)


synth.tool = True  # the scheduler runs it without an OpenROAD process
