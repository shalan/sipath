"""The documentation's benchmark tables, written from the records in bench/results only.

    python3 bench/tables.py            rewrite every generated table in place
    python3 bench/tables.py --check    exit 1 when a document's table differs from what the records give

A generated table sits between ``<!-- table:<name> -->`` and ``<!-- /table -->`` in its document; anything
there is overwritten. SiPath rows of a repeated benchmark show the current default configuration's runs: how
many completed, the median of those that did, and why the others did not.
"""
from __future__ import annotations

import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, ROOT)
import records  # noqa: E402

DOCS = {"orfs": "docs/benchmarks.md", "orfs_readme": "README.md", "aes_steps": "docs/benchmarks.md",
        "sipath_designs": "docs/benchmarks.md", "fallback": "docs/synthesis.md"}


def _num(v, nd=0, sign=False):
    if v is None:
        return "–"
    if isinstance(v, float) and nd:
        s = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
    else:
        s = f"{round(v):,}" if isinstance(v, (int, float)) else str(v)
    return s.replace("-", "−")


def _default_fallback() -> str:
    from sipath.params import DECLARED
    return DECLARED["synth"]["synth_fallback"].default


def _by(kind):
    return records.load(kind)


def _one(recs, design, flow):
    return next((r for r in recs if r["design"] == design and r["flow"] == flow), None)


def _repeats(design):
    """The default configuration's repeats of ``design``: (merged metrics of the completed runs, completed, all)."""
    runs = sorted((r for r in _by("repeat") if r["design"] == design and r["flow"] == _default_fallback()),
                  key=lambda r: r.get("repeat") or 0)
    done = [r for r in runs if not r["metrics"].get("exit")]
    merged = {}
    for k in {k for r in done for k in r["metrics"]}:
        vals = [r["metrics"][k] for r in done if isinstance(r["metrics"].get(k), (int, float))
                and not isinstance(r["metrics"].get(k), bool)]
        if vals:
            merged[k] = statistics.median(vals)
    return merged, done, runs


def _runs_cell(done, runs) -> str:
    if len(runs) <= 1:
        return "1"
    out = f"{len(done)} of {len(runs)}"
    fails = [r.get("failure") for r in runs if r not in done]
    if fails:
        codes = sorted({re.search(r"[A-Z]+-\d+", f).group(0) if f and re.search(r"[A-Z]+-\d+", f) else "failed"
                        for f in fails})
        out += f" ({', '.join(codes)})"
    return out


def _verdict(r) -> str:
    v = (r or {}).get("verification")
    if not v:
        return "—"
    if v["verification"] == "failed":
        return "fails: " + ", ".join(v["failed"])
    return v["verification"]


def table_orfs(short: bool = False) -> str:
    oc = _by("orfs_compare")
    head = ("| design | flow | WNS (ns) | TNS (ns) | wirelength (µm) | cell area (µm²) | DRC | wall (s) | runs | signoff |"
            if short else
            "| design | flow | measured | WNS (ns) | TNS (ns) | wirelength (µm) | cells | cell area (µm²) | "
            "DRC (router / KLayout) | wall (s) | runs | signoff |")
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for design, label in (("gcd", "gcd (1.1 ns)"), ("aes", "aes")):
        o = _one(oc, design, "orfs")
        m, done, runs = _repeats(design)
        rows = []
        if o:
            om = o["metrics"]
            rows.append(("ORFS", o["measured"][5:], om, "1", "—"))
        if runs:
            rows.append(("SiPath", (done or runs)[-1]["measured"][5:], m, _runs_cell(done, runs),
                         _verdict(done[-1] if done else None)))
        for flow, when, x, nruns, ver in rows:
            wns, tns = _num(x.get("wns_tt_ns"), 3, sign=True), _num(x.get("tns_tt_ns"), 2)
            if short:
                lines.append(f"| {label} | {flow} | {wns} | {tns} | {_num(x.get('routed_wirelength_um'))} | "
                             f"{_num(x.get('cell_area_um2'))} | {_num(x.get('drc_klayout'))} | {_num(x.get('wall_s'))} | "
                             f"{nruns} | {ver} |")
            else:
                lines.append(f"| {label} | {flow} | {when} | {wns} | {tns} | {_num(x.get('routed_wirelength_um'))} | "
                             f"{_num(x.get('cells'))} | {_num(x.get('cell_area_um2'))} | "
                             f"{_num(x.get('drc_router'))} / {_num(x.get('drc_klayout'))} | {_num(x.get('wall_s'))} | "
                             f"{nruns} | {ver} |")
    return "\n".join(lines)


def table_aes_steps() -> str:
    o = _one(_by("orfs_compare"), "aes", "orfs")
    _, done, _ = _repeats("aes")
    s = done[-1] if done else None
    st, ss = (o or {}).get("stages") or {}, (s or {}).get("steps") or {}
    rows = [("synthesis", st.get("synth"), ss.get("synth")),
            ("floorplan, power, analog routing, fill", (st.get("floorplan") or 0) or None,
             sum(ss.get(k) or 0 for k in ("floorplan", "physical", "route_analog", "fill")) or None),
            ("place", st.get("place"), ss.get("place")), ("CTS", st.get("cts"), ss.get("cts")),
            ("routing (ORFS: global, detailed, fill cells)", st.get("route"), ss.get("route_digital")),
            ("post-route repair", None, ss.get("route_repair")), ("extraction", None, ss.get("extract")),
            ("signoff (SiPath: DRC, LVS, nine-corner STA)", st.get("finish"), ss.get("signoff")),
            ("**tools, summed**", st.get("tools"), sum(v for v in ss.values() if v) or None),
            ("**whole run (wall)**", (o or {}).get("metrics", {}).get("wall_s"), (s or {}).get("metrics", {}).get("wall_s"))]
    lines = ["| step | ORFS | SiPath |", "|---|---|---|"]
    lines += [f"| {name} | {_num(a)} | {_num(b)} |" for name, a, b in rows]
    return "\n".join(lines)


def table_sipath_designs() -> str:
    oc = _by("orfs_compare")
    lines = ["| design | flow | WNS (ns) | wirelength (µm) | cells | cell area (µm²) | DRT / KLayout DRC | wall (s) | "
             "signoff |", "|---|---|---|---|---|---|---|---|---|"]
    for design, label in (("gcd8", "gcd8 (RTL)"), ("dp8", "dp8"), ("dp8_hs", "dp8_hs"), ("ihp_add8", "ihp_add8")):
        for flow in ("orfs", "sipath"):
            r = _one(oc, design, flow)
            if not r:
                continue
            m = r["metrics"]
            wall = _num(m.get("wall_s"))
            if flow == "orfs" and (r.get("stages") or {}).get("tools"):
                wall += f" (tools {_num(r['stages']['tools'])})"
            lines.append(f"| {label} | {'ORFS' if flow == 'orfs' else 'SiPath'} | {_num(m.get('wns_tt_ns'), 3)} | "
                         f"{_num(m.get('routed_wirelength_um'))} | {_num(m.get('cells'))} | {_num(m.get('cell_area_um2'))} | "
                         f"{_num(m.get('drc_router'))} / {_num(m.get('drc_klayout'))} | {wall} | "
                         f"{_verdict(r) if flow == 'sipath' else '—'} |")
    return "\n".join(lines)


def table_fallback() -> str:
    def pick(design, fb):
        if design in ("aes", "gcd"):
            runs = [r for r in _by("repeat") if r["design"] == design and r["flow"] == fb and not r["metrics"].get("exit")]
            return runs[-1] if runs else None
        return _one(_by("fallback"), design, fb)
    lines = ["| design | slack at ss_max: knee | best_slack | synthesized area (µm²) | final cell area (µm²) | "
             "routed wirelength (µm) | slew violations |", "|---|---|---|---|---|---|---|"]
    for design, label in (("aes", "aes (ORFS)"), ("gcd", "gcd (ORFS, 1.1 ns)"), ("alu32", "alu32 (10 ns)"),
                          ("apb_timer", "apb_timer (10 ns)")):
        k, b = pick(design, "knee"), pick(design, "best_slack")
        if not (k and b):
            continue
        km, bm = k["metrics"], b["metrics"]

        def arrow(key, nd=0):
            x, y = km.get(key), bm.get(key)
            if x is None and y is None:
                return "–"
            return "same" if x == y else f"{_num(x, nd)} → {_num(y, nd)}"
        lines.append(f"| {label} | {_num(km.get('wns_ss_max_ns'), 3)} ns | {_num(bm.get('wns_ss_max_ns'), 3)} ns | "
                     f"{arrow('synth_area_um2')} | {arrow('cell_area_um2')} | {arrow('routed_wirelength_um')} | "
                     f"{arrow('drv_slew')} |")
    return "\n".join(lines)


TABLES = {"orfs": lambda: table_orfs(False), "orfs_readme": lambda: table_orfs(True), "aes_steps": table_aes_steps,
          "sipath_designs": table_sipath_designs, "fallback": table_fallback}


def render(text: str, only: str | None = None) -> str:
    def sub(m):
        name = m.group(1)
        if only and name != only:
            return m.group(0)
        return f"<!-- table:{name} -->\n{TABLES[name]()}\n<!-- /table -->"
    return re.sub(r"<!-- table:(\w+) -->\n.*?<!-- /table -->", sub, text, flags=re.S)


def main(argv) -> int:
    stale = []
    for doc in sorted(set(DOCS.values())):
        path = os.path.join(ROOT, doc)
        with open(path) as f:
            text = f.read()
        new = render(text)
        if new != text:
            stale.append(doc)
            if "--check" not in argv:
                with open(path, "w") as f:
                    f.write(new)
    if "--check" in argv and stale:
        print("stale tables in: " + ", ".join(stale) + " (python3 bench/tables.py)")
        return 1
    print(("rewrote: " + ", ".join(stale)) if stale else "tables current")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
