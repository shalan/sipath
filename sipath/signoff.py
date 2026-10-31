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
import time
import xml.etree.ElementTree as ET

from .lef import lef_macro, lef_macros, lef_pin_shorts  # noqa: F401 (re-exported)
from .tools import klayout, magic
from .tools.openroad import tcl
from .tools.openroad.tcl import q, qlist, tcl_words
from .tools.run import ToolFailed
from .tools.run import run_tool as _run_tool
from .tools.run import runner as _tool_runner

from .params import SIGNOFF_CHECKS as DEFAULT_CHECKS, SIGNOFF_OPTIONAL, SIGNOFF_REQUIRED  # noqa: E402
ALL_CHECKS = DEFAULT_CHECKS + SIGNOFF_OPTIONAL
DEFAULT_REQUIRED = SIGNOFF_REQUIRED + SIGNOFF_OPTIONAL
HERE = os.path.dirname(os.path.abspath(__file__))
STREAM = os.path.join(HERE, "klayout", "stream.py")


class SignoffError(ToolFailed):
    pass


# --------------------------------------------------------------------- helpers
def _runner(ctx):
    return _tool_runner(ctx, SignoffError)


def run_tool(ctx, args: list[str], log: str, timeout: float | None = None, image: str | None = None,
             env: dict | None = None) -> str:
    """Run a tool of the installation (KLayout; Magic from ``image``); its
    output goes to ``log``. A non-zero exit fails the check; the output is
    returned. ``timeout``: default config.tool_timeout (7200 s)."""
    return _run_tool(ctx, args, log, timeout, image, env, error=SignoffError)


def supplies(ctx) -> list[str]:
    d, pdk = ctx.design, ctx.pdk
    nets = [d.net(gp["net"]) for gp in pdk.power_pins] + list(d.analog_supplies)
    return list(dict.fromkeys(nets))


def _cfg(ctx) -> dict:
    so = ctx.pdk.signoff or {}
    if not so:
        raise SignoffError(f"PDK {ctx.pdk.name} has no signoff section (KLayout tech, decks, cell GDS/CDL)")
    return so


# ----------------------------------------------------------------------- GDS
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
    # standard-cell LEFs too: library cells the PDK compares as black boxes take their pins from them
    return [f for f in dict.fromkeys([*pdk.io_lefs.values(), *d.extra_lefs, *[m.lef for m in d.macros],
                                      *pdk.cell_lefs]) if os.path.isfile(f)]


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
           "out": out, "supplies": supplies(ctx), "label_layers": so.get("label_layers", {}),
           "fill_gds": so.get("fill_gds", {})}
    cfg_path = out + ".cfg.json"
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=1)
    _runner(ctx).mount(STREAM, *lib_gds, *macro_gds)
    log = os.path.basename(out) + ".log"
    klayout.batch(ctx, STREAM, ctx.path(log), {"cfg_file": cfg_path}, mode="-zz", error=SignoffError)
    if not os.path.isfile(out) or os.path.getsize(out) == 0:
        raise SignoffError(f"KLayout wrote no GDS at {out}")
    with open(out + ".json") as f:
        res = json.load(f)
    res["macros_without_gds"] = missing_views
    return res


# ----------------------------------------------------------------------- DRC
_DEF_TOKEN = re.compile(r"\(\s*(-?\d+|\*)\s+(-?\d+|\*)(?:\s+(-?\d+))?\s*\)|NEW\b|;|[^\s()]+")


def fix_zero_ext_corners(def_path: str) -> list[dict]:
    """Give the normal half-width extension to every same-layer corner the
    router wrote with extension 0 (``( x y 0 )`` between two perpendicular
    segments): both segments then stop flush at the corner and leave a
    diagonal pinch narrower than the layer's minimum width (a DRC width error).
    With the default extension the corner is the usual square one. Path ends
    and points where the path changes layer keep what the router wrote.
    Rewrites the DEF in place; returns the corners changed."""
    with open(def_path) as f:
        text = f.read()
    start = text.find("\nNETS ")
    if start < 0:
        return []
    end = text.index("\nEND NETS", start)
    body = text[start:end]
    fixed, edits = [], []
    net, path = None, []  # path: [("pt", x, y, match) | ("via",)] since the last ROUTED/NEW
    starts = {"ROUTED", "FIXED", "COVER", "NEW", "SHIELD", "NOSHIELD"}
    words = [m for m in _DEF_TOKEN.finditer(body)]

    def close():
        for k, item in enumerate(path):
            if item[0] != "pt" or item[3].group(3) != "0" or k == 0 or k + 1 >= len(path):
                continue
            if path[k + 1][0] != "pt":
                continue  # the path changes layer here
            j = k - 1
            while j >= 0 and path[j][0] == "via":
                j -= 1
            if j < 0:
                continue
            (_, px, py, _), (_, cx, cy, m), (_, nx, ny, _) = path[j], item, path[k + 1]
            if (px, py) == (cx, cy):
                continue  # a repeated point, not a corner: squaring it would draw a zero-length path
            if (px == cx) == (nx == cx):
                continue  # straight on: the extension does not matter
            edits.append(m)
            fixed.append({"net": net, "at": (cx, cy)})
        path.clear()

    expect_layer = False
    for i, m in enumerate(words):
        tok = m.group(0)
        if m.group(1) is not None:
            x0, y0 = (path_last[1], path_last[2]) if (path_last := next((q for q in reversed(path) if q[0] == "pt"),
                                                                          None)) else (None, None)
            x = x0 if m.group(1) == "*" else int(m.group(1))
            y = y0 if m.group(2) == "*" else int(m.group(2))
            path.append(("pt", x, y, m))
            expect_layer = False
            continue
        if tok == ";":
            close()
            net = None
            continue
        if tok == "-" or (tok.startswith("-") and len(tok) == 1):
            continue
        if net is None and i > 0 and words[i - 1].group(0) == "-":
            net = tok
            continue
        if tok in starts:
            close()
            expect_layer = True
            continue
        if expect_layer:  # the layer name after ROUTED/NEW
            expect_layer = False
            continue
        if path and tok not in ("+", "TAPER", "TAPERRULE", "STYLE", "MASK", "RECT", "VIRTUAL", "USE", "SIGNAL"):
            path.append(("via",))
    close()
    if not edits:
        return []
    out, last = [], 0
    for m in edits:
        out.append(body[last:m.start()] + f"( {m.group(1)} {m.group(2)} )")
        last = m.end()
    out.append(body[last:])
    with open(def_path, "w") as f:
        f.write(text[:start] + "".join(out) + text[end:])
    return fixed


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


def _drc_run(ctx, gds: str, opts: dict, tag: str, top: str | None = None) -> str:
    so = _cfg(ctx)
    rpt = ctx.path(f"drc{tag}.lyrdb")
    args = ["klayout", "-b", "-r", so["drc_deck"], "-rd", f"input={gds}", "-rd", f"topcell={top or ctx.design.top}",
            "-rd", f"report={rpt}"]
    for k, v in opts.items():
        args += ["-rd", f"{k}={str(v).lower() if isinstance(v, bool) else v}"]
    run_tool(ctx, args, ctx.path(f"drc{tag}.log"))
    if not os.path.isfile(rpt):
        raise SignoffError(f"DRC wrote no report at {rpt}")
    return rpt


def _merge_drc(parts: list[dict]) -> dict:
    out = {"count": 0, "by_rule": {}, "by_owner": {}, "by_owner_rule": {}}
    for p in parts:
        out["count"] += p["count"]
        for k in ("by_rule", "by_owner"):
            for r, n in p.get(k, {}).items():
                out[k][r] = out[k].get(r, 0) + n
        for o, rules in p.get("by_owner_rule", {}).items():
            dst = out["by_owner_rule"].setdefault(o, {})
            for r, n in rules.items():
                dst[r] = dst.get(r, 0) + n
    out["by_rule"] = dict(sorted(out["by_rule"].items(), key=lambda kv: -kv[1]))
    return out


def drc(ctx, gds: str, owners: dict | None = None, top: str | None = None) -> dict:
    """The PDK's KLayout DRC deck on ``gds``. With ``signoff.drc_groups`` (deck
    options selecting a subset of the rules each, e.g. FEOL / BEOL / off-grid)
    and the signoff parameter ``drc_split`` (default on), one KLayout process
    per group runs at once and their reports are merged: the groups' rules
    together are the deck's, each process reads the layout itself."""
    so = _cfg(ctx)
    base = dict(so.get("drc_options") or {})
    share = getattr(ctx, "drc_threads", None) or getattr(ctx, "job_threads", None)  # DRC's share in signoff
    total = int(share) if share else max(4, int(getattr(ctx, "threads", None) or getattr(_runner(ctx), "threads", 1) or 1))
    groups = list(so.get("drc_groups") or []) if (ctx.params or {}).get("drc_split", True) else []
    if len(groups) < 2:
        base.setdefault("thr", total)  # the open_pdks decks read $thr
        rpt = _drc_run(ctx, gds, base, "", top)
        return {**parse_lyrdb(rpt, owners, top or ctx.design.top), "report": rpt}
    from concurrent.futures import ThreadPoolExecutor
    thr = max(1, total // len(groups)) if share else max(2, -(-total // len(groups)))  # the groups run at once
    runs = [{**base, "thr": thr, **g} for g in groups]
    with ThreadPoolExecutor(max_workers=len(runs)) as ex:
        rpts = list(ex.map(lambda kv: _drc_run(ctx, gds, kv[1], f"_{kv[0]}", top), enumerate(runs)))
    parts = [parse_lyrdb(r, owners, top or ctx.design.top) for r in rpts]
    merged = _merge_drc(parts)
    if owners is None:
        merged.pop("by_owner"), merged.pop("by_owner_rule")
    names = [",".join(k for k, v in g.items() if v is True) or str(g) for g in groups]
    return {**merged, "report": rpts[0], "reports": rpts,
            "groups": [{"options": n, "count": p["count"]} for n, p in zip(names, parts)]}


def density(ctx, gds: str) -> dict:
    """The PDK's metal density deck (signoff.density_deck) on the GDS: its
    markers fail the check for a top-level layout (die, harness wrapper,
    is_core); for an embedded macro they are reported only (the rule is the
    chip's). The per-layer densities it logs are reported."""
    so = _cfg(ctx)
    rpt = ctx.path("density.lyrdb")
    log = run_tool(ctx, ["klayout", "-b", "-r", so["density_deck"], "-rd", f"input={gds}",
                         "-rd", f"top_cell={ctx.design.top}", "-rd", f"report={rpt}"], ctx.path("density.log"))
    if not os.path.isfile(rpt):
        raise SignoffError(f"density check wrote no report at {rpt}")
    text = log or ""
    # "<layer>_ca_density is <clear area fraction>": metal density = 1 - clear area
    dens = {m.group(1): round(1 - float(m.group(2)), 4)
            for m in re.finditer(r"(\w+?)_ca_density is (-?[\d.eE+-]+)", text)}
    return {**parse_lyrdb(rpt, None, ctx.design.top), "report": rpt, "metal_density": dens}


# ----------------------------------------------------------------- Magic
XOR = os.path.join(HERE, "klayout", "xor.py")


def _magic(ctx, script: str, name: str) -> str:
    """Run a Magic Tcl script batch (no display) with the PDK's rc file, in
    the Magic image (config.magic_image); returns its output."""
    rc = _cfg(ctx).get("magic_rc")
    if not rc:
        raise SignoffError(f"PDK {ctx.pdk.name} gives no signoff.magic_rc: no Magic checks")
    return magic.run(ctx, rc, script, name, error=SignoffError)


def magic_cell_problem(out: str, top: str, log: str):
    """Raise unless Magic loaded the PDK's technology and the GDS gave it ``top``: a GDS read that failed, or a top
    name the GDS lacks, gives Magic an empty new cell, whose DRC count is 0 (no check at all)."""
    m = re.search(r"SIPATH_MAGIC_CELL (\S+) (\S+)", out)
    if not m:
        raise SignoffError(f"Magic did not report the loaded cell (log {log})")
    if m.group(1) == "minimum":  # Magic's built-in technology: the rc file loaded none
        raise SignoffError(f"Magic loaded no PDK technology (log {log})")
    if m.group(2) in ("0", ""):
        raise SignoffError(f"Magic has no cell {top}: the GDS was not read or does not contain it (log {log})")


def magic_drc(ctx, gds: str) -> dict:
    """Magic's DRC (style drc(full), euclidean) on the GDS: a second DRC
    engine beside KLayout, as in the Efabless/ChipFoundry precheck."""
    rpt = ctx.path("magic_drc.rpt")
    script = f"""crashbackups stop
drc off
gds readonly true
gds read {{{gds}}}
puts "SIPATH_MAGIC_CELL [tech name] [cellname list exists {{{ctx.design.top}}}]"
load {{{ctx.design.top}}} -dereference
select top cell
drc euclidean on
drc style drc(full)
drc check
drc catchup
set n 0
set f [open {{{rpt}}} w]
foreach {{why boxes}} [drc listall why] {{
  foreach b $boxes {{ puts $f "$why\\t$b"; incr n }}
}}
close $f
puts "SIPATH_MAGIC_DRC $n"
quit -noprompt
"""
    out = _magic(ctx, script, "magic_drc")
    magic_cell_problem(out, ctx.design.top, ctx.path("magic_drc.log"))
    m = re.search(r"SIPATH_MAGIC_DRC (\d+)", out)
    if not m:
        raise SignoffError(f"Magic DRC did not finish (log {ctx.path('magic_drc.log')})")
    by_rule: dict = {}
    with open(rpt) as f:
        for line in f:
            why = line.split("\t")[0].strip()
            if why:
                by_rule[why] = by_rule.get(why, 0) + 1
    return {"count": int(m.group(1)), "by_rule": by_rule, "report": rpt}


def xor(ctx, def_path: str, klayout_gds: str) -> dict:
    """Stream-out XOR: the routed DEF written to GDS by Magic (with the cell
    and macro layouts, no hierarchical CIF corrections) against the KLayout
    stream-out; every differing layer fails the check (two independent DEF
    readers must agree). Metal fill is not compared: Magic does not read DEF
    FILLS."""
    so, d = _cfg(ctx), ctx.design
    lib_gds = list(so.get("cell_gds", [])) + (list(so.get("io_gds", [])) if d.pads else [])
    lib_gds += [m.gds for m in d.macros if m.gds]
    _runner(ctx).mount(XOR, *lib_gds)
    mgds = ctx.path(f"{d.top}.magic.gds")
    reads = "\n".join(f"gds read {{{g}}}" for g in dict.fromkeys(lib_gds))
    script = f"""crashbackups stop
drc off
gds readonly true
gds rescale false
cif *hier write disable
cif *array write disable
lef read {{{ctx.pdk.tech_lef}}}
{reads}
def read {{{def_path}}}
load {{{d.top}}} -dereference
select top cell
gds write {{{mgds}}}
puts SIPATH_MAGIC_GDS_DONE
quit -noprompt
"""
    out = _magic(ctx, script, "magic_stream")
    if "SIPATH_MAGIC_GDS_DONE" not in out or not os.path.isfile(mgds):
        raise SignoffError(f"Magic wrote no GDS (log {ctx.path('magic_stream.log')})")
    res = ctx.path("xor.json")
    cfg_path = ctx.path("xor.cfg.json")
    with open(cfg_path, "w") as f:
        # Magic's DEF reader skips the FILLS section: the fill datatypes are not compared
        fill = [f"{g}/{dt}" for g, dt in (so.get("fill_gds") or {}).values()]
        json.dump({"a": klayout_gds, "b": mgds, "top": d.top, "out": res,
                   "ignore": list(so.get("xor_ignore", [])) + fill}, f, indent=1)
    klayout.batch(ctx, XOR, ctx.path("xor.log"), {"cfg_file": cfg_path}, error=SignoffError)
    with open(res) as f:
        r = json.load(f)
    return {"count": len(r["differences"]), "layers_compared": r["layers_compared"],
            "differences": r["differences"], "magic_gds": mgds, "report": res}


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


def _share(ctx, wanted) -> int:
    """A check's threads: what it asks for, at most its share among signoff's concurrent checks."""
    share = getattr(ctx, "job_threads", None)
    return max(1, min(int(wanted), int(share))) if share else int(wanted)


def lvs_compare(ctx, prep: dict) -> dict:
    """The KLayout comparison of a prepared LVS (no OpenROAD session needed)."""
    so, d = _cfg(ctx), ctx.design
    gds, cdl, bb, joins = prep["gds"], prep["cdl"], prep["bb"], prep["joins"]
    abstract_shorts, info = prep["abstract_shorts"], prep["info"]
    rpt = ctx.path("lvs.lvsdb")
    out = run_tool(ctx, ["klayout", "-b", "-rd", f"in_gds={gds}", "-rd", f"cdl_file={cdl}", "-rd", f"report_file={rpt}",
                         "-rd", f"target_netlist={ctx.path(d.top + '.extracted.cir')}",
                         "-rd", f"threads={_share(ctx, so.get('lvs_threads', 4))}",
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


def lvs_full(ctx, gds: str) -> dict:
    """Full-device LVS (P0.4): the PDK's own deck (every device class) on ``gds``
    against the design's netlist (``write_cdl``, tie cells kept whole) with
    each cell from the PDK's cell SPICE (``lvs_full_cell_spice``: every device,
    tap diodes and tie resistors included; the CDL for the rest), device
    models prefixed and aliased as the deck spells them."""
    from . import lvs_full as lf
    so, d = _cfg(ctx), ctx.design
    libs = []
    for f in list(so.get("cell_cdl", [])) + list(so.get("lvs_full_cell_spice", [])):  # later definitions win
        with open(f, errors="replace") as fh:
            libs.append(fh.read())
    with open(ctx.path(f"{d.top}.top.cdl")) as fh:
        ref, ref_info = reference_cdl(fh.read(), libs, {}, supplies(ctx), so.get("device_prefix", ""))
    if ref_info["missing"]:
        return {"match": False, "reason": f"no subcircuit for {ref_info['missing']}", "errors": 0, "report": None}
    text, info = lf.prepare([ref], so.get("lvs_full_aliases"), so.get("device_prefix", ""))
    sch = ctx.path(f"{d.top}.full.spice")
    with open(sch, "w") as fh:
        fh.write(text)
    cfg = getattr(ctx, "config", None)
    res = lf.run(_runner(ctx), gds, d.top, sch, ctx.dir, so["lvs_full_deck"], _share(ctx, so.get("lvs_threads", 4)),
                 so.get("lvs_full_options"), timeout=getattr(cfg, "tool_timeout", None),
                 tracker=getattr(ctx, "processes", None))
    return {**res, "schematic": sch, **info}


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
    from .tools.openroad.session import OpenRoadSession
    from .steps import design_rule_counts
    d, pdk = ctx.design, ctx.pdk
    r = _runner(ctx)
    wd = ctx.path("sta")
    os.makedirs(wd, exist_ok=True)
    every = pdk.signoff_corners()  # signoff checks every corner, not only those the flow repaired against
    corners = [c.name for c in every]
    res = {"corners": {}, "expected_corners": corners}
    with OpenRoadSession(wd, r, timeout=getattr(ctx.session, "timeout", 3600)) as s:
        if getattr(ctx, "job_threads", None):  # its share among signoff's concurrent checks
            s.run(f"set_thread_count {int(ctx.job_threads)}")
        s.run(tcl.HELPERS)
        out = [*tcl._define_corners(pdk, every)]
        out += [f"read_lef {q(f)}" for f in [pdk.tech_lef, *pdk.cell_lefs, *d.extra_lefs, *[m.lef for m in d.macros]]]
        out += tcl._read_libs(pdk, d, every)
        out += [f"read_verilog {q(verilog)}", f"link_design {d.top}", tcl._timing_setup(pdk, d, every, sdc=d.signoff_sdc, library_slew=ctx.params.get("library_slew", False)),
                "set_propagated_clock [all_clocks]"]
        s.run("\n".join(out))
        applied = os.path.join(wd, "applied.sdc")
        s.run(f"write_sdc -no_timestamp -digits 9 {q(applied)}")
        if ctx.params.get("library_slew"):
            with open(applied) as fh:
                if re.search(r"^\s*set_max_transition\s", fh.read(), re.M):
                    raise ValueError("library_slew signoff refuses SDC transition overrides; supply a separate signoff_sdc")
        res["applied_sdc"] = applied
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
                          ("design_rules", "report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 9"),
                          ("clock_skew", "report_clock_skew"),
                          ("checks", "check_setup -verbose -unconstrained_endpoints -multiple_clock -no_clock -loops "
                                     "-generated_clocks"),
                          ("annotation", "report_parasitic_annotation -report_unannotated")):
            f = os.path.join(wd, f"sta_{name}.rpt")
            s.run(f"sta::redirect_file_begin {q(f)}\nset ::__rc [catch {{{cmd}}} ::__m]\nsta::redirect_file_end\n"
                  "if {$::__rc} { error $::__m }")
            files[name] = f
        res["reports"] = files
        res.update(design_rule_counts(_View(ctx, s), files["design_rules"],
                                     include_all=ctx.params.get("drv_scope", "core") == "all",
                                     slew_waiver_ns=ctx.params.get("slew_waiver_ns", 0.0)))
        # unconstrained endpoints and clock/loop issues
        with open(files["checks"]) as fh:
            checks = fh.read()
        analog = set(d.analog_nets) | {n.split("[")[0] for n in d.analog_nets}
        uncon, other = check_setup_sections(checks)
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


def sta_failures(st: dict, checks, expected=None) -> list[str]:
    """Judge every expected corner, using the report's ordered ``expected_corners``
    when no explicit list is supplied, or its recorded corners for older reports.
    ``timing()`` already normalizes nonfinite STA values to None; the finite
    check here is defensive for reports supplied outside that reader. Missing
    results fail (omit setup/hold from ``require`` for a design without timing).
    """
    import math
    out = []
    corners = st["corners"]
    expected = list(expected) if expected is not None else (st.get("expected_corners") or list(corners))
    for key, kind in (("wns", "setup"), ("whs", "hold")):
        if kind not in checks:
            continue
        vals = {c: corners[c][key] for c in expected
                if (corners.get(c) or {}).get(key) is not None and math.isfinite(corners[c][key])}
        missing = [c for c in expected if c not in vals]
        if missing:
            out.append(f"{kind}: no result at {len(missing)} of {len(expected)} corners ({missing})"
                       + ("" if vals else ": no clock reaches an endpoint"))
        if vals and min(vals.values()) < 0:
            out.append(f"{kind}: worst {min(vals.values()):.3f} ns ({[c for c, v in vals.items() if v < 0]})")
    if "drv" in checks and not st["drv_clean"]:
        out.append(f"drv: slew {st['drv_slew']}, cap {st['drv_cap']}, fanout {st['drv_fanout']} in {st.get('drv_scope', 'core')} scope")
    if "annotation" in checks and st["unannotated_loaded"]:
        out.append(f"annotation: loaded nets without parasitics at {st['unannotated_loaded'][:8]}")
    if "unconstrained" in checks and st["unconstrained"]:
        out.append(f"unconstrained: {len(st['unconstrained'])} endpoints, e.g. {st['unconstrained'][:5]}")
    return out


def check_setup_sections(text: str) -> tuple[list[str], list[str]]:
    """``check_setup -verbose``: (the unconstrained endpoints, every other warning line). OpenSTA says "There is 1
    unconstrained endpoint." or "There are N unconstrained endpoints."."""
    uncon, other, sect = [], [], None
    for line in text.splitlines():
        if line.startswith("Warning:"):
            sect = "uncon" if "unconstrained endpoint" in line else line
            if sect != "uncon":
                other.append(line.strip())
        elif line.startswith("  ") and sect == "uncon":
            uncon.append(line.strip())
    return uncon, other


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
      set i [::flow_find_inst [string range $pin 0 [expr {$k - 1}]]]
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
    checks = list(p.get("checks", DEFAULT_CHECKS))
    required = [c for c in p.get("require", DEFAULT_REQUIRED) if c in checks]
    from . import registry
    bad = sorted(set(checks) - set(ALL_CHECKS) - set(registry.CHECKS))
    if bad:
        raise ValueError(f"unknown signoff checks {bad}; known: {list(ALL_CHECKS) + sorted(registry.CHECKS)}")
    s.run(tcl.global_connect(d, ctx.pdk))  # cells added after the PDN (CTS, fill, ties) on the supplies
    if d.harness:  # the template's pin shapes, clipped to the die for routing since floorplan
        from . import harness as harness_mod
        ctx.metrics["harness_pins_restored"] = int(
            s.value(harness_mod.get(d.harness, d.harness_options).restore_pins_tcl()))
    final_def, final_v = ctx.path(f"{d.top}.def"), ctx.path(f"{d.top}.v")
    final_pwr_v = ctx.path(f"{d.top}.pwr.v")  # with supply pins: a precheck's LVS (sipath.precheck) reads it
    s.run(f"write_def {q(final_def)}\nwrite_verilog {q(final_v)}\nwrite_verilog -include_pwr_gnd {q(final_pwr_v)}")
    corners = fix_zero_ext_corners(final_def)  # the outputs (DEF, GDS) get square corners
    ctx.metrics["corners_squared"] = len(corners)
    summary, failures, t0 = {"checks": checks, "required": required}, [], time.time()
    if corners:
        summary["corners_squared"] = corners[:20]
    timings = {}
    phys = [c for c in ("drc", "lvs") if c in checks]
    if phys or any(c in checks for c in ("density", "magic_drc", "xor")):  # every check on the GDS streams it
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
    if "density" in checks:
        if not _cfg(ctx).get("density_deck"):
            summary["density"] = {"pass": True, "skipped": "the PDK gives no signoff.density_deck"}
        elif "gds" not in summary:
            failures.append("density: no GDS (density needs drc or lvs among the checks)")
        else:
            jobs["density"] = lambda: density(ctx, summary["gds"]["file"])
    if "magic_drc" in checks or "xor" in checks:
        if "gds" not in summary:
            failures += [f"{c}: no GDS (needs drc or lvs among the checks)" for c in ("magic_drc", "xor")
                         if c in checks]
        else:
            if "magic_drc" in checks:
                jobs["magic_drc"] = lambda: magic_drc(ctx, summary["gds"]["file"])
            if "xor" in checks:
                jobs["xor"] = lambda: xor(ctx, final_def, summary["gds"]["file"])
    lvs_prep = None
    if "lvs" in phys:
        t = time.time()
        lvs_prep = lvs_prepare(ctx, summary["gds"]["file"])
        timings["lvs_prepare"] = round(time.time() - t, 1)
        if "_compare" in lvs_prep:
            jobs["lvs"] = lambda: lvs_compare(ctx, lvs_prep["_compare"])
        else:
            summary["lvs"] = lvs_prep
    if "lvs_full" in checks:
        so = _cfg(ctx)
        if not so.get("lvs_full_deck"):
            failures.append("lvs_full: the PDK gives no signoff.lvs_full_deck")
        elif not (lvs_prep and "_compare" in lvs_prep):
            failures.append("lvs_full: no reference netlist (needs lvs among the checks, prepared without errors)")
        elif lvs_prep["_compare"]["bb"]:
            failures.append(f"lvs_full: black boxes {sorted(set(lvs_prep['_compare']['bb'].values()))} have no "
                            "device netlist (full-device LVS needs every cell's devices)")
        else:
            jobs["lvs_full"] = lambda: lvs_full(ctx, summary["gds"]["file"])
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
    # the checks running at once share the attempt's threads (config threads: the run's cores), not each all of them
    threads = int(getattr(ctx, "threads", None) or getattr(_runner(ctx), "threads", 1) or 1)
    ctx.job_threads = max(1, threads // max(1, len(jobs)))
    ctx.drc_threads = ctx.job_threads + max(0, threads - ctx.job_threads * max(1, len(jobs)))  # the rest: DRC, longest
    timings["threads_per_check"] = ctx.job_threads
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
    if "magic_drc" in results:
        summary["magic_drc"] = results["magic_drc"]
        if summary["magic_drc"]["count"]:
            failures.append(f"magic_drc: {summary['magic_drc']['count']} violations "
                            f"{dict(list(summary['magic_drc']['by_rule'].items())[:8])}")
    if "lvs_full" in results:
        summary["lvs_full"] = results["lvs_full"]
        if not summary["lvs_full"]["match"]:
            failures.append(f"lvs_full: {summary['lvs_full'].get('reason') or 'netlists do not match'} "
                            f"({summary['lvs_full']['errors']} mismatches, report {summary['lvs_full']['report']})")
    if "xor" in results:
        summary["xor"] = results["xor"]
        if summary["xor"]["count"]:
            failures.append(f"xor: Magic and KLayout stream-outs differ on {sorted(summary['xor']['differences'])}")
    if "density" in results:
        summary["density"] = results["density"]
        # the deck measures each layer over the whole layout: a chip-level rule.
        # An embedded macro's density counts in its parent's (reported, not gating)
        top_level = d.mode == "die" or bool(d.harness) or d.is_core
        summary["density"]["scope"] = "top level" if top_level else "embedded macro: informational"
        if summary["density"]["count"] and top_level:
            failures.append(f"density: {summary['density']['count']} violations {summary['density']['by_rule']} "
                            f"(metal density {summary['density']['metal_density']})")
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
        failures += sta_failures(st, checks, [c.name for c in ctx.pdk.signoff_corners()])
    if "harness" in checks:  # the routed wrapper against the frame's template
        if d.harness:
            from . import harness as harness_mod
            summary["harness"] = harness_mod.get(d.harness, d.harness_options).conformance(final_def)
            summary["harness"]["name"] = d.harness
            if not summary["harness"]["pass"]:
                failures.append(f"harness: {summary['harness']['reason']}, e.g. {summary['harness']['missing'][:3]}")
        else:
            summary["harness"] = {"pass": True, "skipped": "no harness"}
    for name, fn in registry.CHECKS.items():  # plugin checks, after the built-in ones
        if name not in checks:
            continue
        t = time.time()
        res = fn(ctx, summary)
        if not isinstance(res, dict) or "pass" not in res:
            raise ValueError(f"signoff check {name} returned {res!r}; expected a dict with 'pass'")
        summary[name] = res
        timings[name] = round(time.time() - t, 1)
        if not res["pass"]:
            failures.append(f"{name}: {res.get('reason') or 'failed'}")
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
    if "lvs_full" in summary:
        m["lvs_full_match"] = summary["lvs_full"].get("match", False)
    for k in ("magic_drc", "xor"):
        if summary.get(k, {}).get("count") is not None:
            m[f"{k}_violations"] = summary[k]["count"]
    if summary.get("density", {}).get("count") is not None:
        m["density_violations"] = summary["density"]["count"]
        for layer, v in summary["density"]["metal_density"].items():
            m[f"metal_density_{layer}"] = v
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
    ctx.outputs.update(signoff=path, def_=final_def, verilog=final_v, verilog_pwr=final_pwr_v,
                       gds=summary.get("gds", {}).get("file"))
    rep = ctx.outputs.setdefault("reports", {})
    for k in ("drc", "density", "lvs", "lvs_full", "magic_drc", "xor"):
        if summary.get(k, {}).get("report"):
            rep[f"signoff_{k}"] = summary[k]["report"]
    for k, f in summary.get("timing", {}).get("reports", {}).items():
        rep[f"signoff_sta_{k}"] = f
    if failed_required and p.get("fail_on_violation", True):
        raise SignoffError("signoff failed (" + path + "):\n  " + "\n  ".join(failed_required))
    return summary
