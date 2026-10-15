"""Signoff: physical (GDS, DRC, LVS, antennas) and timing (independent STA).

Physical, with the PDK's tools as named in its ``signoff`` section:

* GDS: the routed DEF streamed with KLayout (``sipath/klayout/stream.py``):
  every cell replaced by its layout from the cell, IO and macro GDS files; a
  cell with no layout anywhere fails the check (a macro given as LEF only
  cannot be signed off). Supply nets get one text label each.
* DRC: the PDK's KLayout DRC deck on that GDS; every marker counts.
* LVS: the PDK's KLayout LVS deck between the GDS and a reference netlist
  written by OpenROAD (``write_cdl``) after the global supply connections are
  re-applied, completed with the cell subcircuits the design uses (from the
  PDK's CDL), tie cells written as the shorts their layout is, and the supply
  nets as ports. Pass = the deck reports matching netlists.
* Antennas: ``check_antennas`` on the final database.

Timing, in a fresh OpenROAD process that reads only the final netlist, the
Liberty files, the SDC and the per-corner SPEF files (no state of the flow's
database): setup and hold per corner, slew/capacitance/fanout limits (on
standard cells; IO/macro pins listed apart), unconstrained endpoints (analog
ports excepted), clock and loop checks, and parasitic annotation (a loaded
net without parasitics fails; partially annotated pins are listed).

``run(ctx)`` writes ``signoff.json`` and the reports, records the results as
metrics, and (by default) fails the attempt when a required check fails.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from . import tcl
from .tcl import q, qlist, tcl_words

from .params import SIGNOFF_CHECKS as ALL_CHECKS, SIGNOFF_REQUIRED as DEFAULT_REQUIRED  # noqa: E402
HERE = os.path.dirname(os.path.abspath(__file__))
STREAM = os.path.join(HERE, "klayout", "stream.py")


class SignoffError(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers
def _runner(ctx):
    r = getattr(ctx, "runner", None) or getattr(ctx.session, "runner", None)
    if r is None or not hasattr(r, "tool_command"):
        raise SignoffError("this step needs a real runner (its tools run next to OpenROAD)")
    return r


def run_tool(ctx, args: list[str], log: str, timeout: float = 7200) -> str:
    """Run a tool of the installation (KLayout); its output goes to ``log``.
    A non-zero exit fails the check; the output is returned."""
    r = _runner(ctx)
    cmd = r.tool_command(ctx.dir, args)
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ctx.dir)
    out = p.stdout + p.stderr
    with open(log, "w") as f:
        f.write(f"# {' '.join(args)}\n# exit {p.returncode} after {time.time() - t0:.1f}s\n{out}")
    if p.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-15:])
        raise SignoffError(f"{os.path.basename(args[0])} exited {p.returncode} (log {log}):\n{tail}")
    return out


def supplies(ctx) -> list[str]:
    d, pdk = ctx.design, ctx.pdk
    nets = [gp["net"] for gp in pdk.power_pins] + list(d.analog_supplies)
    return list(dict.fromkeys(nets))


def _cfg(ctx) -> dict:
    so = ctx.pdk.signoff or {}
    if not so:
        raise SignoffError(f"PDK {ctx.pdk.name} has no signoff section (KLayout tech, decks, cell GDS/CDL)")
    return so


# ----------------------------------------------------------------------- GDS
def lef_macros(path: str) -> dict[str, list[str]]:
    """{macro name: pin names in LEF order} for every MACRO in a LEF."""
    with open(path, errors="replace") as f:
        text = f.read()
    out = {}
    for m in re.finditer(r"^\s*MACRO\s+(\S+)(.*?)^\s*END\s+\1\s*$", text, re.M | re.S):
        # unique, in order (a PDK LEF may declare a pin twice)
        out[m.group(1)] = list(dict.fromkeys(re.findall(r"^\s*PIN\s+(\S+)", m.group(2), re.M)))
    return out


def lef_pin_shorts(path: str, master: str) -> dict[str, str]:
    """Pins of ``master`` whose LEF rectangles overlap or touch on one layer
    (one net in the abstract): {pin: first pin of its group, in LEF order}
    for every pin but the first of each group."""
    with open(path, errors="replace") as f:
        text = f.read()
    m = re.search(rf"^\s*MACRO\s+{re.escape(master)}\s*$(.*?)^\s*END\s+{re.escape(master)}\s*$", text, re.M | re.S)
    if not m:
        return {}
    rects, order = [], []
    for pm in re.finditer(r"^\s*PIN\s+(\S+)(.*?)^\s*END\s+\1\s*$", m.group(1), re.M | re.S):
        name = pm.group(1)
        order.append(name)
        layer = None
        for line in pm.group(2).splitlines():
            t = line.replace(";", " ").split()
            if t[:1] == ["LAYER"]:
                layer = t[1]
            elif t[:1] == ["RECT"] and layer:
                x0, y0, x1, y1 = map(float, t[1:5])
                rects.append((layer, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), name))
    parent = {n: n for n in order}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    rects.sort(key=lambda r: (r[0], r[1]))
    active = []
    for r in rects:
        active = [a for a in active if a[0] == r[0] and a[3] >= r[1]]
        for a in active:
            if a[5] != r[5] and a[2] <= r[4] and r[2] <= a[4]:
                x, y = find(a[5]), find(r[5])
                if x != y:
                    if order.index(x) < order.index(y):
                        parent[y] = x
                    else:
                        parent[x] = y
        active.append(r)
    return {n: find(n) for n in order if find(n) != n}


def lef_macro(path: str) -> tuple[str, list[str]]:
    """(macro name, pin names in LEF order) of a single-macro LEF."""
    macros = lef_macros(path)
    if not macros:
        raise SignoffError(f"no MACRO in {path}")
    name = next(iter(macros))
    return name, macros[name]


def design_masters(ctx) -> list[str]:
    return sorted(set(tcl_words(ctx.session.value(
        "set __m {}\nforeach __i [[ord::get_db_block] getInsts] { lappend __m [[$__i getMaster] getName] }\n"
        "lsort -unique $__m"))))


def blackboxes(ctx, masters: list[str] | None = None) -> dict[str, str]:
    """name -> master of what LVS treats as black boxes: macros with
    ``lvs: blackbox`` or ``abstract`` (keyed by instance) and masters matching the PDK's
    ``signoff.lvs_blackbox`` patterns (keyed by master)."""
    out = {m.inst: lef_macro(m.lef)[0] for m in ctx.design.macros if m.lvs == "blackbox" or m.abstract}
    pats = (ctx.pdk.signoff or {}).get("lvs_blackbox", [])
    if pats:
        for master in (masters if masters is not None else design_masters(ctx)):
            if any(fnmatch.fnmatchcase(master, p) for p in pats):
                out.setdefault(master, master)
    return out


def _lef_files(ctx) -> list[str]:
    pdk, d = ctx.pdk, ctx.design
    return [f for f in dict.fromkeys([*pdk.io_lefs.values(), *d.extra_lefs, *[m.lef for m in d.macros]])
            if os.path.isfile(f)]


def _lef_pins(ctx) -> dict[str, list[str]]:
    pins = {}
    for f in _lef_files(ctx):
        for k, v in lef_macros(f).items():
            pins.setdefault(k, v)
    return pins


def stream_gds(ctx, def_path: str, out: str, abstract: tuple[str, ...] = ()) -> dict:
    """Routed DEF -> GDS. Masters in ``abstract`` keep their LEF view (LVS)."""
    so, d, pdk = _cfg(ctx), ctx.design, ctx.pdk
    lib_gds = list(so.get("cell_gds", [])) + (list(so.get("io_gds", [])) if d.pads else [])
    macro_gds, missing_views = [], []
    for m in d.macros:
        if lef_macro(m.lef)[0] in abstract:
            continue
        if m.gds:
            if m.gds not in macro_gds:
                macro_gds.append(m.gds)
        else:
            missing_views.append(m.inst)
    cfg = {"tech": so["klayout_tech"], "map": so["klayout_map"],
           "lefs": [pdk.tech_lef, *pdk.cell_lefs, *d.extra_lefs, *[m.lef for m in d.macros]],
           "def": def_path, "top": d.top, "gds": lib_gds, "macro_gds": macro_gds, "abstract": sorted(abstract),
           "out": out, "supplies": supplies(ctx), "label_layers": so.get("label_layers", {})}
    cfg_path = out + ".cfg.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=1)
    _runner(ctx).mount(STREAM, *lib_gds, *macro_gds)
    log = os.path.basename(out) + ".log"
    run_tool(ctx, ["klayout", "-zz", "-rd", f"cfg_file={cfg_path}", "-r", STREAM], ctx.path(log))
    if not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise SignoffError(f"KLayout wrote no GDS at {out}")
    with open(out + ".json") as f:
        res = json.load(f)
    res["macros_without_gds"] = missing_views
    return res


# ----------------------------------------------------------------------- DRC
def parse_lyrdb(path: str, owners: dict | None = None, top: str | None = None) -> dict:
    """Marker counts by rule. With ``owners`` ({cell: owner}), markers are also
    counted by the owner of the cell they are reported in (``by_owner``): a
    marker in ``top`` is the design's own geometry ("top"); one inside a cell
    belongs to whoever supplied that cell (a macro, a library)."""
    root = ET.parse(path).getroot()
    items = root.find("items")
    by_rule, by_owner, owner_rules = {}, {}, {}
    for it in (items if items is not None else []):
        rule = (it.findtext("category") or "?").strip("'")
        by_rule[rule] = by_rule.get(rule, 0) + 1
        if owners is not None:
            cell = (it.findtext("cell") or "").strip()
            o = "top" if cell == top else owners.get(cell, f"unknown cell {cell}")
            by_owner[o] = by_owner.get(o, 0) + 1
            r = owner_rules.setdefault(o, {})
            r[rule] = r.get(rule, 0) + 1
    out = {"count": sum(by_rule.values()), "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1]))}
    if owners is not None:
        out["by_owner"] = by_owner
        out["by_owner_rule"] = owner_rules
    return out


def cell_owners(ctx, stream_result: dict) -> dict:
    """{cell: owner} from the stream's per-file record: "macro <inst>" for a
    macro's GDS, "library <file>" for PDK GDS, "def" for cells of the DEF."""
    by_file = {}
    for m in ctx.design.macros:
        if m.gds:
            k = os.path.basename(m.gds)
            by_file[k] = (by_file[k] + "," + m.inst) if k in by_file else f"macro {m.inst}"
    abstract = {lef_macro(m.lef)[0]: m.inst for m in ctx.design.macros if m.abstract}
    return {c: (f"macro {abstract[c]} (abstract)" if c in abstract else o if o == "def" else
                by_file.get(o, f"library {o}"))
            for c, o in (stream_result.get("owners") or {}).items()}


def drc(ctx, gds: str, owners: dict | None = None) -> dict:
    so = _cfg(ctx)
    rpt = ctx.path("drc.lyrdb")
    args = ["klayout", "-b", "-r", so["drc_deck"], "-rd", f"input={gds}", "-rd", f"topcell={ctx.design.top}",
            "-rd", f"report={rpt}"]
    opts = dict(so.get("drc_options") or {})
    opts.setdefault("thr", max(4, int(getattr(_runner(ctx), "threads", 1) or 1)))  # the open_pdks decks read $thr
    for k, v in opts.items():
        args += ["-rd", f"{k}={str(v).lower() if isinstance(v, bool) else v}"]
    run_tool(ctx, args, ctx.path("drc.log"))
    if not os.path.isfile(rpt):
        raise SignoffError(f"DRC wrote no report at {rpt}")
    return {**parse_lyrdb(rpt, owners, ctx.design.top), "report": rpt}


# ----------------------------------------------------------------------- LVS
def _joined(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith("+") and out:
            out[-1] += " " + line[1:]
        else:
            out.append(line)
    return out


def _subckts(text: str) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in
            re.finditer(r"^\.SUBCKT\s+(\S+).*?^\.ENDS.*?$", text, re.S | re.M | re.I)}


def _calls(body: str) -> set[str]:
    out = set()
    for line in _joined(body):
        t = line.split()
        if line[:1] in "Xx" and len(t) >= 2:
            out.add(t[t.index("/") + 1] if "/" in t else t[-1])
    return out


def reference_cdl(top_text: str, lib_texts: list[str], tie_shorts: dict, supply_nets: list[str],
                  device_prefix: str = "") -> tuple[str, dict]:
    """Reference netlist for LVS: the top circuit from ``write_cdl`` plus the
    library subcircuits it uses (and only those), tie cells reduced to their
    supply pins with each tie output merged into the supply it is shorted to,
    supplies added as top ports, valueless resistors given a value."""
    lib = {}
    for t in lib_texts:
        lib.update(_subckts(t))
    pins = {n: _joined(b)[0].split()[2:] for n, b in lib.items()}

    def shorts_of(cell):
        for pat, sh in tie_shorts.items():
            if fnmatch.fnmatchcase(cell, pat):
                return sh
        return None

    top = _joined(top_text)
    alias = {}

    def find(n):
        while alias.get(n, n) != n:
            n = alias[n]
        return n
    ties = 0
    for line in top:
        t = line.split()
        if line[:1] in "Xx" and len(t) >= 2:
            cell = t[-1]
            sh = shorts_of(cell)
            if sh and cell in pins:
                ties += 1
                conn = dict(zip(pins[cell], t[1:-1]))
                for a, b in sh.items():
                    x, y = find(conn[a]), find(conn[b])
                    if x != y:
                        alias[x] = y
    out = []
    for line in top:
        t = line.split()
        if line[:1] in "Xx" and len(t) >= 2:
            cell = t[-1]
            sh = shorts_of(cell) or {}
            nets = [find(n) for n in t[1:-1]]
            keep = [n for p, n in zip(pins.get(cell, [None] * len(nets)), nets) if p not in sh]
            line = " ".join([t[0], *keep, cell])
        elif t and t[0].upper() == ".SUBCKT":
            ports = [find(n) for n in t[2:]]
            ports += [s for s in supply_nets if s not in ports]
            line = " ".join([*t[:2], *ports])
        out.append(line)
    top_out = "\n".join(out)
    need, todo = set(), list(_calls(top_out))
    while todo:
        c = todo.pop()
        if c in need or c not in lib:
            continue
        need.add(c)
        todo += list(_calls(lib[c]))
    missing = sorted(_calls(top_out) - set(lib))
    subs = []
    for c in sorted(need):
        sh = shorts_of(c)
        if sh:
            subs.append(f".SUBCKT {c} {' '.join(p for p in pins[c] if p not in sh)}\n.ENDS {c}")
        else:
            subs.append(lib[c])
    text = "\n".join(subs) + "\n" + top_out + "\n"
    pre = f"(?:{re.escape(device_prefix)})?" if device_prefix else ""
    text = re.sub(rf"^([rR]\S*\s+\S+\s+\S+\s+)({pre}res_\S+)\s*$", r"\g<1>1 \2", text, flags=re.M)
    # some PDK CDLs write a MOS whose source is its drain with three nodes
    # (D G B, e.g. hs decaps): restore the fourth node (S = D)
    fixed = 0

    def mos4(m):
        nonlocal fixed
        fixed += 1
        return f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(2)} {m.group(4)} {m.group(5)}"
    text = re.sub(rf"^(M\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+({pre}(?:special_)?[np]fet\S*)",
                  mos4, text, flags=re.M | re.I)
    return text, {"subckts": len(need), "missing": missing, "tie_cells": ties, "tie_nets_merged": len(alias),
                  "mos_3node_fixed": fixed}


def lvs(ctx, gds: str) -> dict:
    """LVS end to end (prepare in the session, then compare)."""
    prep = lvs_prepare(ctx, gds)
    return lvs_compare(ctx, prep["_compare"]) if "_compare" in prep else prep


def lvs_prepare(ctx, gds: str) -> dict:
    """LVS of ``gds`` against the reference netlist. Black-box macros
    (``lvs: blackbox``) are compared as their pins only: the layout side is
    re-streamed with their LEF abstracts and the reference gets pin-only
    subcircuits, so the chip's connections to them are checked but not their
    insides (reported under ``blackboxed``)."""
    so, d, pdk, s = _cfg(ctx), ctx.design, ctx.pdk, ctx.session
    cdl_files = list(so.get("cell_cdl", [])) + (list(so.get("io_cdl", [])) if d.pads else [])
    bb = blackboxes(ctx)
    no_netlist = [m.inst for m in d.macros if not m.cdl and m.inst not in bb]
    if no_netlist:
        return {"match": False, "reason": f"macros without cdl {no_netlist} (give cdl, or lvs: blackbox to check "
                                          f"only their connections)", "blackboxed": bb}
    for m in d.macros:
        if m.cdl and m.inst not in bb and m.cdl not in cdl_files:
            cdl_files.append(m.cdl)
    texts = []
    for f in cdl_files:
        with open(f, errors="replace") as fh:
            texts.append(fh.read())
    joins, abstract_shorts = (0, ""), {}
    if bb:
        lef_pins = _lef_pins(ctx)
        unknown = sorted(set(bb.values()) - set(lef_pins))
        if unknown:
            return {"match": False, "reason": f"black boxes without a LEF to take pins from: {unknown}",
                    "blackboxed": bb}
        # pin-only subcircuits (LEF pin order) replace the masters' own ones
        names = sorted(set(bb.values()))
        drop = re.compile(r"^\.SUBCKT\s+(?:" + "|".join(map(re.escape, names)) + r")\s.*?^\.ENDS.*?$\n?",
                          re.S | re.M | re.I)
        texts = [drop.sub("", t) for t in texts]
        texts.append("".join(f".SUBCKT {n} {' '.join(lef_pins[n])}\n.ENDS {n}\n" for n in names))
        # pins whose LEF shapes overlap are one net in the abstract: write
        # them like tie cells (one pin, the nets merged) and report them
        abstract_shorts = {}
        for n in names:
            for f in _lef_files(ctx):
                sh = lef_pin_shorts(f, n)
                if sh:
                    abstract_shorts[n] = sh
                    break
        lvs_gds = ctx.path(f"{d.top}.lvs.gds")
        g = stream_gds(ctx, ctx.path(f"{d.top}.def"), lvs_gds, abstract=tuple(names))
        if g["empty"] or g["orphan"]:
            return {"match": False, "reason": f"LVS layout has cells without layout {g['empty']} "
                                              f"orphan {g['orphan']}", "blackboxed": bb}
        gds = lvs_gds
        joins = (g.get("joins", 0), ",".join(g.get("join_metals", [])))
    masters = ctx.path("masters.cdl")
    with open(masters, "w") as fh:
        fh.write("\n".join(texts) + "\n")
    top_cdl = ctx.path(f"{d.top}.top.cdl")
    s.run(f"write_cdl -include_fillers -masters {q(masters)} {q(top_cdl)}")
    with open(top_cdl) as fh:
        text, info = reference_cdl(fh.read(), texts, {**so.get("tie_shorts", {}), **abstract_shorts}, supplies(ctx),
                                   so.get("device_prefix", ""))
    cdl = ctx.path(f"{d.top}.cdl")
    with open(cdl, "w") as fh:
        fh.write(text)
    if info["missing"]:
        return {"match": False, "reason": f"no subcircuit for {info['missing']}", "cdl": cdl, "blackboxed": bb, **info}
    return {"_compare": dict(gds=gds, cdl=cdl, bb=bb, joins=joins, abstract_shorts=abstract_shorts, info=info)}


def lvs_compare(ctx, prep: dict) -> dict:
    """The KLayout comparison of a prepared LVS (no OpenROAD session needed)."""
    so, d = _cfg(ctx), ctx.design
    gds, cdl, bb, joins = prep["gds"], prep["cdl"], prep["bb"], prep["joins"]
    abstract_shorts, info = prep["abstract_shorts"], prep["info"]
    rpt = ctx.path("lvs.lvsdb")
    out = run_tool(ctx, ["klayout", "-b", "-rd", f"in_gds={gds}", "-rd", f"cdl_file={cdl}", "-rd", f"report_file={rpt}",
                         "-rd", f"target_netlist={ctx.path(d.top + '.extracted.cir')}",
                         "-rd", f"threads={so.get('lvs_threads', 4)}",
                         "-rd", f"blackbox={','.join(sorted(set(bb.values())))}",
                         "-rd", f"bb_joins={joins[0]}", "-rd", f"bb_join_metals={joins[1]}", "-r", so["lvs_deck"]],
                   ctx.path("lvs.log"))
    match = "Netlists match" in out and "don't match" not in out
    errors = []
    if os.path.isfile(rpt):
        with open(rpt, errors="replace") as fh:
            errors = [m.group(1) for m in re.finditer(r"M\(E B\('([^']*)'\)\)", fh.read())]
    return {"match": match, "report": rpt, "cdl": cdl, "log": ctx.path("lvs.log"), "errors": len(errors),
            "first_errors": errors[:8], "gds": gds, "blackboxed": bb, "abstract_shorts": abstract_shorts, **info}


# -------------------------------------------------------------------- timing
class _View:
    """Minimal context for helpers that take ``ctx`` (session + outputs)."""

    def __init__(self, ctx, session):
        self.session, self.design, self.pdk, self.dir = session, ctx.design, ctx.pdk, ctx.dir
        self.outputs, self.metrics = {}, {}

    def path(self, name):
        return os.path.join(self.dir, name)


def timing(ctx, verilog: str, spef_corners: dict) -> dict:
    """Independent STA: a fresh OpenROAD process with only the final netlist,
    Liberty, SDC and SPEF files."""
    from .session import OpenRoadSession
    from .steps import design_rule_counts
    d, pdk = ctx.design, ctx.pdk
    r = _runner(ctx)
    wd = ctx.path("sta")
    os.makedirs(wd, exist_ok=True)
    corners = [c.name for c in pdk.analysis_corners()]
    res = {"corners": {}}
    with OpenRoadSession(wd, r, timeout=getattr(ctx.session, "timeout", 3600)) as s:
        s.run(tcl.HELPERS)
        out = [*tcl._define_corners(pdk)]
        out += [f"read_lef {q(f)}" for f in [pdk.tech_lef, *pdk.cell_lefs, *d.extra_lefs, *[m.lef for m in d.macros]]]
        out += tcl._read_libs(pdk, d)
        out += [f"read_verilog {q(verilog)}", f"link_design {d.top}", tcl._timing_setup(pdk, d),
                "set_propagated_clock [all_clocks]"]
        s.run("\n".join(out))
        multi = len(corners) > 1
        for c, f in spef_corners.items():
            s.run(f"read_spef {'-corner ' + c + ' ' if multi else ''}{q(f)}")
        for c in corners:
            sel = f"-corner {c}" if multi else ""
            v = tcl_words(s.value(f"list [sta::worst_slack -max {sel}] [sta::total_negative_slack -max {sel}] "
                                  f"[sta::worst_slack -min {sel}] [sta::total_negative_slack -min {sel}]"))
            vals = [float(x) if abs(float(x)) < 1e30 else None for x in v]
            res["corners"][c] = dict(zip(("wns", "tns", "whs", "ths"), vals))
        files = {}
        for name, cmd in (("setup", "report_checks -path_delay max -group_path_count 10 -format full_clock_expanded"),
                          ("hold", "report_checks -path_delay min -group_path_count 10 -format full_clock_expanded"),
                          ("design_rules", "report_check_types -max_slew -max_capacitance -max_fanout -violators"),
                          ("clock_skew", "report_clock_skew"),
                          ("checks", "check_setup -verbose -unconstrained_endpoints -multiple_clock -no_clock -loops "
                                     "-generated_clocks"),
                          ("annotation", "report_parasitic_annotation -report_unannotated")):
            f = os.path.join(wd, f"sta_{name}.rpt")
            s.run(f"sta::redirect_file_begin {q(f)}\nset ::__rc [catch {{{cmd}}} ::__m]\nsta::redirect_file_end\n"
                  "if {$::__rc} { error $::__m }")
            files[name] = f
        res["reports"] = files
        res.update(design_rule_counts(_View(ctx, s), files["design_rules"]))
        # unconstrained endpoints and clock/loop issues
        with open(files["checks"]) as fh:
            checks = fh.read()
        analog = set(d.analog_nets) | {n.split("[")[0] for n in d.analog_nets}
        uncon, other = [], []
        sect = None
        for line in checks.splitlines():
            if line.startswith("Warning:"):
                sect = "uncon" if "unconstrained endpoints" in line else line
                if sect != "uncon":
                    other.append(line.strip())
            elif line.startswith("  ") and sect == "uncon":
                uncon.append(line.strip())
        analog_ports = _analog_ports(s, d)
        s.run(NET_KIND)
        io_ports = {e for e in uncon if "/" not in e and e not in analog_ports
                    and tcl_words(s.value(f"::flow_pin_net_kind {q(e)}"))[1] == "1"}
        res["unconstrained"] = [e for e in uncon if e.split("/")[0] not in analog_ports and e not in io_ports]
        res["unconstrained_analog"] = [e for e in uncon if e.split("/")[0] in analog_ports]
        res["unconstrained_pad_ports"] = sorted(io_ports)
        res["check_setup_other"] = other
        # parasitic annotation
        with open(files["annotation"]) as fh:
            ann = fh.read()
        res.update(_annotation(s, ann))
    return res


def _analog_ports(session, d) -> set[str]:
    """Top-level ports on analog nets (feed-throughs are untimed on purpose)."""
    if not d.analog_nets:
        return set()
    ports = tcl_words(session.value(
        f"set o {{}}; foreach n {qlist(d.analog_nets)} {{ set net [[ord::get_db_block] findNet $n]; "
        "if {$net eq \"NULL\"} continue; foreach b [$net getBTerms] { lappend o [$b getName] } }; set o"))
    return set(ports) | set(d.analog_nets)


NET_KIND = r"""
proc ::flow_pin_net_kind {pin} {
  # loads on the pin's net and whether the net only touches IO-ring cells
  # (pads, corners, fillers) and ports: "<loads> <io_only>"; "-1 0" if not found
  set b [ord::get_db_block]
  set net NULL
  set it [$b findITerm $pin]
  if {$it ne "NULL"} { set net [$it getNet] } else {
    set k [string last "/" $pin]
    if {$k > 0} {
      set i [$b findInst [string range $pin 0 [expr {$k - 1}]]]
      if {$i ne "NULL"} { set it [$i findITerm [string range $pin [expr {$k + 1}] end]]
        if {$it ne "NULL"} { set net [$it getNet] } }
    } else {
      set bt [$b findBTerm $pin]; if {$bt ne "NULL"} { set net [$bt getNet] }
    }
  }
  if {$net eq "NULL"} { return [list [expr {$it eq "NULL" && [$b findBTerm $pin] eq "NULL" ? -1 : 0}] 0] }
  set io 1
  foreach t [$net getITerms] {
    set ty [[[$t getInst] getMaster] getType]
    if {![string match PAD* $ty] && ![string match ENDCAP* $ty]} { set io 0; break }
  }
  list [expr {[llength [$net getITerms]] + [llength [$net getBTerms]] - 1}] $io
}
"""


def _annotation(session, text: str) -> dict:
    """Unannotated drivers whose net has loads among standard cells or macros
    fail. Listed apart: drivers of dangling nets (an unused tie output), nets
    that only touch IO-ring cells and ports (connected by abutment or inside
    the pads, no routed wire to extract), and partially annotated pins."""
    unann, partial, sect = [], [], None
    for line in text.splitlines():
        if line.startswith("Found") and "partially" in line:
            sect = "partial"
        elif line.startswith("Found"):
            sect = "unann"
        elif line.startswith(" ") and sect:
            (unann if sect == "unann" else partial).append(line.strip())
    session.run(NET_KIND)
    loaded, dangling, io = [], [], []
    for pin in unann:
        n, io_only = map(int, tcl_words(session.value(f"::flow_pin_net_kind {q(pin)}")))
        if io_only and n != 0:
            io.append(pin)
        elif n != 0:
            loaded.append(pin)
        else:
            dangling.append(pin)
    return {"unannotated_loaded": loaded, "unannotated_dangling": dangling, "unannotated_io_ring": io,
            "partially_annotated": partial}


# -------------------------------------------------------------------- driver
def run(ctx) -> dict:
    """All signoff checks on the parent's final database; returns the summary."""
    d, s, p = ctx.design, ctx.session, ctx.params
    checks = list(p.get("checks", ALL_CHECKS))
    required = [c for c in p.get("require", DEFAULT_REQUIRED) if c in checks]
    bad = sorted(set(checks) - set(ALL_CHECKS))
    if bad:
        raise ValueError(f"unknown signoff checks {bad}; known: {list(ALL_CHECKS)}")
    s.run(tcl.global_connect(d, ctx.pdk))  # cells added after the PDN (CTS, fill, ties) on the supplies
    final_def, final_v = ctx.path(f"{d.top}.def"), ctx.path(f"{d.top}.v")
    s.run(f"write_def {q(final_def)}\nwrite_verilog {q(final_v)}")
    summary, failures, t0 = {"checks": checks, "required": required}, [], time.time()
    timings = {}
    phys = [c for c in ("drc", "lvs") if c in checks]
    if phys:
        t = time.time()
        g = stream_gds(ctx, final_def, ctx.path(f"{d.top}.gds"))
        summary["gds"] = {"file": ctx.path(f"{d.top}.gds"), **{k: g[k] for k in ("cells", "empty", "orphan",
                                                                              "labels", "macros_without_gds", "empty_defined",
                                                                              "renamed")}}
        timings["gds"] = round(time.time() - t, 1)
        if g["empty"] or g["orphan"]:
            # LVS can still run when the only cells without layout are black
            # boxes (LVS uses their LEF abstracts)
            bb = set(blackboxes(ctx).values())
            abstract = {lef_macro(m.lef)[0] for m in d.macros if m.abstract}
            ok = {"lvs": not g["orphan"] and set(g["empty"]) <= bb,
                  "drc": not g["orphan"] and set(g["empty"]) <= abstract}
            for c in list(phys):
                if ok.get(c):
                    continue
                failures.append(f"{c}: not checkable, cells without layout {g['empty'][:8]} orphan {g['orphan'][:4]}")
                summary[c] = {"run": False, "reason": f"cells without layout: {g['empty']}"}
                phys.remove(c)
    # DRC, the LVS comparison and STA are separate processes: run them at once.
    # What needs this OpenROAD session (LVS reference netlist, antennas) runs here.
    jobs = {}
    if "drc" in phys:
        def run_drc():
            drc_gds, drc_stream = summary["gds"]["file"], g
            abstract = sorted({lef_macro(m.lef)[0] for m in d.macros if m.abstract})
            if abstract:  # DRC sees these macros as their LEF abstracts
                drc_gds = ctx.path(f"{d.top}.drc.gds")
                drc_stream = stream_gds(ctx, final_def, drc_gds, abstract=tuple(abstract))
            out = drc(ctx, drc_gds, cell_owners(ctx, drc_stream))
            out.update(gds=drc_gds, abstract_macros=[m.inst for m in d.macros if m.abstract])
            return out
        jobs["drc"] = run_drc
    lvs_prep = None
    if "lvs" in phys:
        t = time.time()
        lvs_prep = lvs_prepare(ctx, summary["gds"]["file"])
        timings["lvs_prepare"] = round(time.time() - t, 1)
        if "_compare" in lvs_prep:
            jobs["lvs"] = lambda: lvs_compare(ctx, lvs_prep["_compare"])
        else:
            summary["lvs"] = lvs_prep
    tchecks = [c for c in ("setup", "hold", "drv", "annotation", "unconstrained") if c in checks]
    if tchecks:
        spefs = ctx.ancestor_output("spef_corners")
        jobs["sta"] = lambda: timing(ctx, final_v, spefs)
    results, errors = {}, {}

    def timed(name, fn):
        t = time.time()
        try:
            results[name] = fn()
        except Exception as e:  # re-raised below, after every job has finished
            errors[name] = e
        timings[name] = round(time.time() - t, 1)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = [ex.submit(timed, k, f) for k, f in jobs.items()]
        if "antenna" in checks:  # needs this session: runs here while the others run
            from .steps import check_antennas
            n = check_antennas(ctx)
            summary["antenna"] = {"violations": n}
        for fu in futs:
            fu.result()
    if errors:
        name, e = next(iter(errors.items()))
        raise e
    if "drc" in results:
        summary["drc"] = results["drc"]
        if summary["drc"]["count"]:
            failures.append(f"drc: {summary['drc']['count']} violations {summary['drc']['by_rule']} "
                            f"(by owner {summary['drc'].get('by_owner')})")
    if "lvs" in results:
        summary["lvs"] = results["lvs"]
    if "lvs" in summary and not summary["lvs"].get("match"):
        failures.append(f"lvs: netlists do not match ({summary['lvs'].get('reason') or summary['lvs'].get('errors')} "
                        f"errors, e.g. {summary['lvs'].get('first_errors', [])[:3]})")
    if summary.get("antenna", {}).get("violations"):
        failures.append(f"antenna: {summary['antenna']['violations']} violations")
    if "sta" in results:
        st = results["sta"]
        summary["timing"] = st
        wns = [v["wns"] for v in st["corners"].values() if v["wns"] is not None]
        whs = [v["whs"] for v in st["corners"].values() if v["whs"] is not None]
        if "setup" in checks and wns and min(wns) < 0:
            failures.append(f"setup: worst {min(wns):.3f} ns ({[c for c, v in st['corners'].items() if v['wns'] is not None and v['wns'] < 0]})")
        if "hold" in checks and whs and min(whs) < 0:
            failures.append(f"hold: worst {min(whs):.3f} ns ({[c for c, v in st['corners'].items() if v['whs'] is not None and v['whs'] < 0]})")
        if "drv" in checks and not st["drv_clean"]:
            failures.append(f"drv: slew {st['drv_slew']}, cap {st['drv_cap']}, fanout {st['drv_fanout']} on standard cells")
        if "annotation" in checks and st["unannotated_loaded"]:
            failures.append(f"annotation: loaded nets without parasitics at {st['unannotated_loaded'][:8]}")
        if "unconstrained" in checks and st["unconstrained"]:
            failures.append(f"unconstrained: {len(st['unconstrained'])} endpoints, e.g. {st['unconstrained'][:5]}")
    timings["wall"] = round(time.time() - t0, 1)
    summary["failures"] = failures
    failed_required = [f for f in failures if f.split(":")[0] in required]
    summary["pass"] = not failed_required
    summary["seconds"] = timings
    path = ctx.path("signoff.json")
    with open(path, "w") as fh:
        json.dump(summary, fh, indent=1, default=str)
    m = ctx.metrics
    m["signoff_pass"] = summary["pass"]
    if "drc" in summary and summary["drc"].get("count") is not None:
        m["drc_signoff"] = summary["drc"]["count"]
    if "lvs" in summary:
        m["lvs_match"] = summary["lvs"].get("match", False)
    if "antenna" in summary:
        m["antenna_signoff"] = summary["antenna"]["violations"]
    if "timing" in summary:
        st = summary["timing"]
        for c, v in st["corners"].items():
            m[f"sta_wns_{c}"], m[f"sta_whs_{c}"] = v["wns"], v["whs"]
        m["sta_drv_clean"] = st["drv_clean"]
        m["unannotated_loaded"] = len(st["unannotated_loaded"])
        m["unannotated_io_ring"] = len(st["unannotated_io_ring"])
        m["partially_annotated"] = len(st["partially_annotated"])
        m["unconstrained_endpoints"] = len(st["unconstrained"])
    ctx.outputs.update(signoff=path, def_=final_def, verilog=final_v,
                       gds=summary.get("gds", {}).get("file"))
    rep = ctx.outputs.setdefault("reports", {})
    for k in ("drc", "lvs"):
        if summary.get(k, {}).get("report"):
            rep[f"signoff_{k}"] = summary[k]["report"]
    for k, f in summary.get("timing", {}).get("reports", {}).items():
        rep[f"signoff_sta_{k}"] = f
    if failed_required and p.get("fail_on_violation", True):
        raise SignoffError("signoff failed (" + path + "):\n  " + "\n  ".join(failed_required))
    return summary
