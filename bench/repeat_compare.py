"""Configurations x repeats of SiPath on an ORFS reference design (items 5 and 6).

    python3 bench/repeat_compare.py aes --repeats 3 [--config name:step.param=value[,step.param=value] ...]
                                        [--table]

Each configuration runs the whole flow ``--repeats`` times from the same
RTL; runs are interleaved (repeat 1 of every configuration, then repeat 2, ...)
so slow drift on the machine does not favour one. Every final layout is
measured by bench/orfs_compare.py's code (setup WNS/TNS at the typical corner,
cells, cell area, routed wirelength) and read from its signoff (worst setup
at ss_max, DRC, LVS). Without ``--config`` the configurations are the
synthesis fallbacks when nothing meets timing at synthesis: knee, best_slack
(the default), least_area, and mapping at the typical corner.

Writes runs/bench_repeat/<design>/results.json and results.md: per
configuration the median, min and max of each metric, and per repeat the
selected netlist (cells, a hash) and each step's instance count, WNS and
wirelength, to show where identical inputs start to diverge.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orfs_compare as oc  # noqa: E402

OUT = os.path.join(oc.ROOT, "runs", "bench_repeat")
DEFAULT_CONFIGS = {  # each names what it sets: a record must not depend on what the default was when it ran
    "knee": {"synth": {"synth_fallback": "knee"}},
    "best_slack": {"synth": {"synth_fallback": "best_slack"}},
    "least_area": {"synth": {"synth_fallback": "least_area"}},
    "map_typical": {"synth": {"map_corner": "typical"}},
}
STEPS = ("synth", "floorplan", "physical", "place", "cts", "route_analog", "route_digital", "route_repair", "fill",
         "extract")


def parse_config(spec: str) -> tuple[str, dict]:
    """``name:step.param=value,...`` -> (name, {step: {param: value}})."""
    name, _, rest = spec.partition(":")
    out = {}
    for item in filter(None, rest.split(",")):
        key, _, val = item.partition("=")
        step, _, param = key.partition(".")
        try:
            val = json.loads(val)
        except ValueError:
            pass
        out.setdefault(step, {})[param] = val
    return name, out


def run_one(w: str, d: str, cfg: dict, tag: str, overrides: dict) -> dict:
    """One SiPath run of design ``d`` (as bench/orfs_compare.run_sipath) with
    step parameter ``overrides``; returns its measured result."""
    out = os.path.join(w, tag)
    os.makedirs(out, exist_ok=True)
    flow = [{"step": s, **({"variants": [overrides[s]]} if s in overrides else {})} for s in STEPS]
    flow.append({"step": "signoff", "variants": [{"fail_on_violation": False}]})
    design = {"name": d, "top": cfg["top"], "mode": "macro", "pdk": "sky130A", "rtl": cfg["rtl"], "sdc": cfg["sdc"],
              "core_utilization": cfg["utilization"], "aspect_ratio": cfg["aspect_ratio"],
              "core_margin": cfg["margin"], "clock_ports": ["clk"], "is_core": True,
              "layer_adjustments": cfg.get("layer_adjustments") or {}, "config": {"command_timeout": 14400},
              "flow": flow}
    path = os.path.join(out, f"{d}.json")
    with open(path, "w") as f:
        json.dump(design, f, indent=1)
    import subprocess
    subprocess.run(["rm", "-rf", os.path.join(out, "run")])
    t0 = time.time()
    with open(os.path.join(out, "run.log"), "w") as f:
        rc = subprocess.run([sys.executable, "-m", "sipath", "run", path, os.path.join(out, "run"), "-v"],
                            cwd=oc.ROOT, stdout=f, stderr=subprocess.STDOUT).returncode
    with open(os.path.join(out, "time.json"), "w") as f:
        json.dump({"exit": rc, "seconds": round(time.time() - t0, 1)}, f)
    r = oc.sipath_result(w, tag)
    lib = [c.liberty for c in __import__("sipath.pdk", fromlist=["load"]).load("sky130A").analysis_corners()
           if c.name == "tt_nom"][0]
    m = oc.measure(r, dict(cfg, drc_deck=None), lib, tag, w)  # signoff already ran the DRC deck
    if m.get("error"):
        m["measure_error"] = m.pop("error")
    return {"exit": rc, "seconds": r.get("seconds"), **m, **signoff_summary(out), **trace(out)}


def _chain(out: str) -> list:
    """The run's result chain (sipath.scheduler.run_chain), [] when the run wrote no graph."""
    path = os.path.join(out, "run", "graph.json")
    if not os.path.isfile(path):
        return []
    from sipath.scheduler import run_chain
    with open(path) as f:
        return run_chain(json.load(f))


def signoff_summary(out: str) -> dict:
    """The result chain's signoff (signoff.json), not the latest directory of any branch."""
    from sipath.scheduler import in_chain
    so = (in_chain(_chain(out), "signoff").get("outputs") or {}).get("signoff")
    if not (so and os.path.isfile(so)):
        return {"signoff": None}
    with open(so) as f:
        s = json.load(f)
    corners = (s.get("timing") or {}).get("corners") or {}
    return {"wns_ss_max_ns": round(corners["ss_max"]["wns"], 3) if "ss_max" in corners else None,
            "whs_min_ns": round(min(c["whs"] for c in corners.values()), 3) if corners else None,
            "drc": (s.get("drc") or {}).get("count"), "lvs_match": (s.get("lvs") or {}).get("match"),
            "drv_slew": (s.get("timing") or {}).get("drv_slew")}


def trace(out: str) -> dict:
    """The selected netlist (a hash, so repeats can be compared) and each
    selected step's instance count, WNS and wirelength."""
    sel = {a["step"].split("@")[0]: a for a in _chain(out)}  # no graph (an early failure): nothing to trace
    steps = {s: {k: (sel[s].get("metrics") or {}).get(k) for k in ("insts", "wns", "wirelength_um")}
             | {"seconds": round((sel[s].get("finished_at") or 0) - (sel[s].get("started_at") or 0), 1)}
             for s in sel}
    net = (sel.get("synth", {}).get("outputs") or {}).get("netlist")
    digest = None
    if net and os.path.isfile(net):
        with open(net, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()[:12]
    why = ((sel.get("synth", {}).get("outputs") or {}).get("abc_sweep") or {}).get("why")
    return {"netlist_sha": digest, "synth_recipe": (sel.get("synth", {}).get("metrics") or {}).get("abc_recipe"),
            "synth_why": why, "steps": steps}


KEYS = ("wns_ns", "tns_ns", "wns_ss_max_ns", "cell_area_um2", "cells", "wirelength_um", "seconds")


def table(design: str, results: dict) -> str:
    lines = [f"# {design}: configurations x repeats", "",
             "Median (min … max) over the repeats; WNS/TNS at tt_nom by bench/orfs_compare.py's measurement, "
             "ss_max from signoff.", "",
             "| configuration | runs | " + " | ".join(KEYS) + " | netlists | DRC | LVS |",
             "|---|---|" + "---|" * (len(KEYS) + 3)]
    for name, runs in results.items():
        ok = [r for r in runs if r.get("wns_ns") is not None]
        cells = []
        for k in KEYS:
            v = [r[k] for r in ok if r.get(k) is not None]
            cells.append(f"{statistics.median(v):,.3f} ({min(v):,.3f} … {max(v):,.3f})" if v else "–")
        nets = len({r.get("netlist_sha") for r in ok})
        drc = sorted({r.get("drc") for r in ok}, key=str)
        lvs = sorted({r.get("lvs_match") for r in ok}, key=str)
        lines.append(f"| {name} | {len(ok)}/{len(runs)} | " + " | ".join(cells) + f" | {nets} | {drc} | {lvs} |")
    lines += ["", "Per repeat, where runs diverge (instances / WNS ns / wirelength um after each step):", ""]
    for name, runs in results.items():
        for k, r in enumerate(runs):
            st = r.get("steps") or {}
            trail = "; ".join(f"{s} {v.get('insts')}/{v.get('wns') if v.get('wns') is None else round(v['wns'], 3)}"
                              f"/{v.get('wirelength_um')}" for s, v in st.items() if s in ("place", "cts",
                                                                                            "route_digital",
                                                                                            "route_repair"))
            lines.append(f"- {name} #{k + 1}: netlist {r.get('netlist_sha')} ({r.get('synth_recipe')}); {trail}")
    return "\n".join(lines) + "\n"


def main(argv) -> int:
    designs = [a for a in argv if not a.startswith("-") and ":" not in a and not a.isdigit()]
    d = designs[0] if designs else "aes"
    repeats = int(argv[argv.index("--repeats") + 1]) if "--repeats" in argv else 3
    configs = dict(parse_config(argv[i + 1]) for i, a in enumerate(argv) if a == "--config") or DEFAULT_CONFIGS
    w = os.path.join(OUT, d)
    os.makedirs(w, exist_ok=True)
    path = os.path.join(w, "results.json")
    results = {}
    if os.path.isfile(path):
        with open(path) as f:
            results = json.load(f)
    if "--table" not in argv:
        import records
        source = records.archive_source()  # the code these runs run, archived when uncommitted
        src = oc.fetch_design(d)
        cfg = oc.orfs_config(src, d)
        for k in range(repeats):
            for name, ov in configs.items():
                runs = results.setdefault(name, [])
                if len(runs) > k:  # recorded: resumable
                    continue
                print(f"{d} {name} #{k + 1} ...", flush=True)
                try:
                    r = run_one(w, d, cfg, f"{name}_r{k + 1}", ov)
                except Exception as e:  # noqa: BLE001  (one run failing does not stop the others; it is recorded)
                    r = {"exit": None, "driver_error": f"{type(e).__name__}: {e}"}
                r["source_snapshot"], r["overrides"] = source, ov
                runs.append(r)
                with open(path, "w") as f:
                    json.dump(results, f, indent=1)
                print(f"  wns {r.get('wns_ns')} ns, area {r.get('cell_area_um2')}, {r.get('seconds')} s", flush=True)
        for rec in records.from_repeat(w):  # the tables read records, not results.json
            if rec["flow"] in configs:
                print("record:", records.write(rec, tag=f"r{rec['repeat']}"))
    md = table(d, {n: results[n] for n in configs if n in results})
    with open(os.path.join(w, "results.md"), "w") as f:
        f.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
