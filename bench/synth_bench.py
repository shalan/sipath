"""Synthesis benchmark: SiPath's synth step on shalan/synth_flow's 16 designs.

    python3 bench/synth_bench.py [design ...] [--tag NAME] [--set param=value ...]
                                 [--frontend-sweep] [--equiv] [--table]

Each design (bench/synth/manifest.yaml: its RTL, SDC and clock; the four
external ones after bench/synth/fetch_external.sh) goes through the synth step
alone on sky130A: lint, the ABC recipe sweep (abc_sweep, default auto) and the
timing estimate at the slow corner with the Liberty wire-load model, the
netlist picked by synth_select. ``--set`` overrides synth parameters (e.g.
``abc_sweep=delay``, ``map_corner=ss_max``, ``synth_opts=booth``);
``--frontend-sweep`` also runs synth_flow's six front-end variants (plain,
Kogge-Stone, Han-Carlson, Sklansky, Booth, Booth + Kogge-Stone) as attempts.
The equivalence check is off unless ``--equiv``.

Writes runs/bench_synth/<tag>.csv and .md: the selected netlist's slack,
TNS, area and cells per design, next to synth_flow's best pre-repair result
(bench/synth/reference_synth_flow.csv, its baseline-noD run: same slow-corner
Liberty, OpenSTA with wire load, no -D, no post-synthesis repairs).
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BENCH = os.path.join(ROOT, "bench", "synth")
OUT = os.path.join(ROOT, "runs", "bench_synth")
FRONTENDS = [[], ["adder=kogge-stone"], ["adder=han-carlson"], ["adder=sklansky"], ["booth"],
             ["booth", "adder=kogge-stone"]]


def manifest() -> list[dict]:
    from sipath.pdk import read_file
    return read_file(os.path.join(BENCH, "manifest.yaml"))["designs"]


def design(entry: dict):
    """A SiPath Design for a manifest entry (None when its sources are missing)."""
    from sipath import designfile
    files = sorted(f for pat in entry["files"] for f in glob.glob(os.path.join(BENCH, pat)))
    if not files:
        return None
    data = {"name": entry["name"], "top": entry["top"], "mode": "macro", "pdk": "sky130A", "rtl": files,
            "sdc": os.path.join(BENCH, entry["sdc"]), "core_utilization": 0.5,
            "rtl_includes": [os.path.join(BENCH, i) for i in entry.get("includes", [])],
            "rtl_defines": {d: "" for d in entry.get("defines", [])}}
    return designfile.design_from_dict(data, BENCH)


def _value(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if "," in v or v in ("booth",) or v.startswith("adder="):
        return [x for x in v.split(",") if x]
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        return v


def run(entry: dict, tag: str, sets: dict, frontend_sweep: bool, equiv: bool) -> dict:
    from sipath import designfile
    d = design(entry)
    row = {"design": entry["name"], "period_ns": entry["period_ps"] / 1000}
    if d is None:
        return {**row, "status": "missing sources (bench/synth/fetch_external.sh)"}
    variant = {"equiv": "bounded" if equiv else "off", **sets}
    item = {"step": "synth", "variants": [variant]}
    if frontend_sweep:
        item["synth_opts_sweep"] = FRONTENDS
    wd = os.path.join(OUT, tag, entry["name"])
    import shutil
    shutil.rmtree(wd, ignore_errors=True)
    t0 = time.time()
    try:
        f = designfile.run(d, wd, flow_steps=[item])
    except Exception as e:  # a failed design is a row, not the end of the bench
        return {**row, "status": f"failed: {str(e).strip().splitlines()[-1][:160]}", "seconds": round(time.time() - t0, 1)}
    a = f.steps["synth"].selected
    m = a.metrics
    return {**row, "status": "ok", "recipe": m.get("abc_recipe"),
            "frontend": "+".join(a.params.get("synth_opts") or []) or "plain",
            "wns_ns": m.get("synth_wns_ns"), "tns_ns": m.get("synth_tns_ns"), "area_um2": m.get("synth_area_um2"),
            "cells": m.get("synth_cells"), "seconds": round(time.time() - t0, 1),
            "why": (a.outputs.get("abc_sweep") or {}).get("why") or a.outputs.get("selection")}


def reference() -> dict:
    with open(os.path.join(BENCH, "reference_synth_flow.csv")) as f:
        return {r["design"]: r for r in csv.DictReader(f)}


def table(rows: list[dict], tag: str, sets: dict, frontend_sweep: bool) -> str:
    ref = reference()
    out = [f"# Synthesis bench: {tag}", "",
           f"synth parameters: {json.dumps(sets) if sets else 'defaults'}"
           f"{'; front-end sweep (6 variants)' if frontend_sweep else ''}. Slack and area of the selected netlist "
           "at ss_max with the Liberty wire-load model; synth_flow: its best pre-repair result (baseline-noD).", "",
           "| design | period (ns) | SiPath WNS (ns) | TNS (ns) | area (µm²) | cells | recipe | front end | s | "
           "synth_flow WNS | synth_flow area | synth_flow recipe |", "|" + "---|" * 12]
    met = ref_met = 0
    for r in rows:
        rf = ref.get(r["design"], {})
        if r.get("status") != "ok":
            out.append(f"| {r['design']} | {r['period_ns']:g} | {r['status']} |" + " |" * 9)
            continue
        met += r["wns_ns"] is not None and r["wns_ns"] >= 0
        ref_met += bool(rf) and float(rf["wns_ns"]) >= 0

        def f(v, spec):  # a value the run did not give is shown as missing, never as a number
            return "–" if v is None else format(v, spec)
        mine = (f"| {r['design']} | {r['period_ns']:g} | {f(r['wns_ns'], '+.3f')} | {f(r['tns_ns'], '.2f')} | "
                f"{f(r['area_um2'], ',.0f')} | {r['cells']} | {r['recipe']} | {r['frontend']} | {f(r['seconds'], '.0f')} | ")
        out.append(mine + (f"{float(rf['wns_ns']):+.3f} | {float(rf['area_um2']):,.0f} | {rf.get('recipe', '-')} |"
                           if rf else "- | - | - |"))
    out += ["", f"Meeting timing: SiPath {met} of {sum(r.get('status') == 'ok' for r in rows)}, "
                f"synth_flow (pre-repair) {ref_met}."]
    return "\n".join(out) + "\n"


def main(argv) -> int:
    tag, sets, names, it = "default", {}, [], iter(argv)
    flags = {a for a in argv if a in ("--frontend-sweep", "--equiv", "--table")}
    for a in it:
        if a == "--tag":
            tag = next(it)
        elif a == "--set":
            k, _, v = next(it).partition("=")
            sets[k] = _value(v)
        elif a not in flags:
            names.append(a)
    entries = [e for e in manifest() if not names or e["name"] in names]
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{tag}.csv")
    if "--table" in flags:
        with open(path) as f:
            rows = [{**r, **{k: (float(r[k]) if r.get(k) not in (None, "") and k in ("wns_ns", "tns_ns", "area_um2",
                                                                                    "seconds", "period_ns") else r.get(k))
                             for k in r}} for r in csv.DictReader(f)]
    else:
        rows = []
        for e in entries:
            r = run(e, tag, sets, "--frontend-sweep" in flags, "--equiv" in flags)
            print(f"{r['design']:14} {r.get('status')}  wns {r.get('wns_ns')}  area {r.get('area_um2')}  "
                  f"{r.get('recipe')} {r.get('frontend', '')}  {r.get('seconds')} s", flush=True)
            rows.append(r)
        keys = ["design", "period_ns", "status", "recipe", "frontend", "wns_ns", "tns_ns", "area_um2", "cells",
                "seconds", "why"]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    md = table(rows, tag, sets, "--frontend-sweep" in flags)
    with open(os.path.join(OUT, f"{tag}.md"), "w") as f:
        f.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
