"""Exploration strategies judged by the final result, not by the step they change.

    python3 bench/strategy_compare.py gcd8_crowded dp8 [aes] [--repeats 2] [--strategies defaults,sweep,...]

Each strategy runs a design to signoff (fail_on_violation false, so every run is measured): ``defaults`` (the
reference flow), ``sweep`` (placement densities in parallel, chosen at placement), ``adaptive`` (two adaptive
placement rounds), ``search`` (the sweep with keep: 2, decided after extraction) and ``adaptive_search``
(adaptive rounds, then the best two and the baseline carried to extraction). Every run is measured as the
other benchmarks are (bench/orfs_compare.measure: typical-corner slack, cells, cell area, routed wirelength
from the final DEF), plus the worst corner's slack, slew violations and DRC from signoff, the wall time and
the core-seconds the scheduler allocated (each attempt's seconds times its threads). Runs go one at a time, so
their wall times compare. One record per run: bench/results/strategy (bench/tables.py writes the table).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orfs_compare as oc  # noqa: E402
import records  # noqa: E402

ROOT = oc.ROOT
OUT = os.path.join(ROOT, "runs", "bench_strategy")
EX = os.path.join(ROOT, "sipath", "examples")
DENSITIES = {"gcd8_crowded": [0.65, 0.7], "dp8": [0.45, 0.7], "aes": [0.5, 0.6]}
SOURCE: dict = {}  # records.archive_source() at the start of main


def _abs(d: dict, base: str) -> dict:
    for k in ("netlist", "sdc", "floorplan_def"):
        if d.get(k) and not os.path.isabs(d[k]):
            d[k] = os.path.normpath(os.path.join(base, d[k]))
    if d.get("rtl"):
        d["rtl"] = [p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p)) for p in d["rtl"]]
    return d


def design(name: str) -> tuple[dict, list[str]]:
    """The design file (paths absolute) and its flow's step names, before the strategy's placement."""
    import yaml
    if name == "gcd8_crowded":  # gcd8 made crowded, as the adaptive rounds were first measured on
        with open(os.path.join(EX, "gcd8.yaml")) as f:
            d = _abs(yaml.safe_load(f), EX)
        d.update(name="gcd8_crowded", core_utilization=0.65, step_defaults={"place": {"density": 0.8}})
    elif name == "dp8":
        with open(os.path.join(EX, "dp8.yaml")) as f:
            d = _abs(yaml.safe_load(f), EX)
        d.pop("inputs", None)  # the generated inputs already exist next to the examples
    elif name == "aes":
        with open(os.path.join(ROOT, "runs", "bench", "aes", "compare.json")) as f:
            cfg = json.load(f)["config"]
        d = {"name": "aes", "top": cfg["top"], "mode": "macro", "pdk": "sky130A", "rtl": cfg["rtl"], "sdc": cfg["sdc"],
             "core_utilization": cfg["utilization"], "aspect_ratio": cfg["aspect_ratio"], "core_margin": cfg["margin"],
             "clock_ports": ["clk"], "is_core": True, "layer_adjustments": cfg.get("layer_adjustments") or {},
             "config": {"command_timeout": 14400}}
    else:
        raise ValueError(f"unknown design {name}")
    steps = [s["step"] for s in d.pop("flow", None) or []] or \
        (["synth"] if d.get("rtl") else []) + ["floorplan", "physical", "place", "cts", "route_analog",
                                               "route_digital", "route_repair", "fill", "extract", "signoff"]
    return d, steps


def strategy_flow(steps: list[str], strategy: str, densities: list[float]) -> list[dict]:
    sweep = {"variants": [{}] + [{"density": x} for x in densities], "parallel": 1 + len(densities),
             "score": "routability_then_wns"}
    place = {"defaults": {}, "sweep": sweep, "adaptive": {"adapt": {"rounds": 2}},
             "search": {**sweep, "keep": 2, "decide_at": "extract"},
             "adaptive_search": {"adapt": {"rounds": 2}, "keep": 2, "decide_at": "extract"}}[strategy]
    flow = []
    for s in steps:
        if s == "place":
            flow.append({"step": "place", **place})
        elif s == "signoff":
            flow.append({"step": "signoff", "variants": [{"fail_on_violation": False}]})
        else:
            flow.append({"step": s})
    if "signoff" not in steps:
        flow.append({"step": "signoff", "variants": [{"fail_on_violation": False}]})
    return flow


def core_seconds(run_dir: str) -> float | None:
    """Allocated core-seconds of the OpenROAD attempts: Σ each attempt's seconds × the threads it was given (its
    own record; graphs from before attempts recorded threads: the step's allocation, else the run's threads).
    Tool attempts (synthesis: no OpenROAD process) are left out: tool_seconds. Allocation, not measured CPU."""
    with open(os.path.join(run_dir, "graph.json")) as f:
        g = json.load(f)
    alloc, threads = g.get("allocation") or {}, (g.get("config") or {}).get("threads") or 1
    total = 0.0
    for a in g["attempts"]:
        if a.get("kind") == "tool":
            continue
        n = a.get("threads") or alloc.get(a["step"], {}).get("threads_each") or threads
        total += (a.get("seconds") or 0) * n
    return round(total, 1)


def tool_seconds(run_dir: str) -> float:
    with open(os.path.join(run_dir, "graph.json")) as f:
        g = json.load(f)
    return round(sum(a.get("seconds") or 0 for a in g["attempts"] if a.get("kind") == "tool"), 1)


def run(name: str, strategy: str, repeat: int) -> dict:
    d, steps = design(name)
    tag = f"{strategy}_r{repeat}"
    w = os.path.join(OUT, name)
    out = os.path.join(w, tag)
    subprocess.run(["rm", "-rf", out])
    os.makedirs(out)
    d["flow"] = strategy_flow(steps, strategy, DENSITIES[name])
    path = os.path.join(out, f"{name}.json")
    with open(path, "w") as f:
        json.dump(d, f, indent=1)
    t0 = time.time()
    with open(os.path.join(out, "run.log"), "w") as f:
        rc = subprocess.run([sys.executable, "-m", "sipath", "run", path, os.path.join(out, "run"), "-v"], cwd=ROOT,
                            stdout=f, stderr=subprocess.STDOUT).returncode
    wall = round(time.time() - t0, 1)
    with open(os.path.join(out, "time.json"), "w") as f:
        json.dump({"exit": rc, "seconds": wall}, f)
    run_dir = os.path.join(out, "run")
    log = os.path.join(out, "run.log")
    if not os.path.isfile(os.path.join(run_dir, "graph.json")):  # it failed before the run had a graph
        rec = records.make("strategy", name, strategy, {"exit": rc, "wall_s": wall}, repeat=repeat, source=out,
                           overrides={"place": d["flow"]}, outcome="execution_failed",
                           failure=records._log_failure(log) or f"exit {rc}, no graph.json", source_snapshot=SOURCE)
        print("record:", records.write(rec, tag=f"r{repeat}"), flush=True)
        return rec
    rec = records.from_design_run(run_dir, "strategy", name, strategy, repeat=repeat, overrides={"place": d["flow"]},
                                  source_snapshot=SOURCE)
    m = rec["metrics"]
    m.update(exit=rc, wall_s=wall, core_seconds=core_seconds(run_dir), tool_seconds=tool_seconds(run_dir))
    # the run's own execution status decides, not its exit code: a run that completed and failed verification
    # exits 1 and is measured like any other (its verdict is the record's verification)
    execution = (rec.get("verification") or {}).get("execution")
    rec["outcome"] = "completed" if execution == "completed" else "execution_failed"
    if execution != "completed" and not rec.get("failure"):
        rec["failure"] = records._log_failure(log) or f"exit {rc}, execution {execution}"
    if execution == "completed":
        from sipath.pdk import load
        lib = [c.liberty for c in load("sky130A").analysis_corners() if c.name == "tt_nom"][0]
        meas = oc.measure(oc.sipath_result(w, tag), {"sdc": d["sdc"], "top": d["top"], "drc_deck": None}, lib, tag, w)
        missing = [k for k in ("wns_ns", "cells", "cell_area_um2", "wirelength_um") if meas.get(k) is None]
        if meas.get("error") or missing:
            rec["outcome"] = "measurement_failed"
            rec["failure"] = meas.get("error") or f"the measurement gave no {', '.join(missing)}"
        # every number from the one measurement of the final layout (none filled in from another source)
        m.update(wns_tt_ns=meas.get("wns_ns"), tns_tt_ns=meas.get("tns_ns"), cells=meas.get("cells"),
                 cell_area_um2=meas.get("cell_area_um2"), routed_wirelength_um=meas.get("wirelength_um"))
        rec["metrics"]["drc_klayout"] = _signoff_drc(run_dir)
    print("record:", records.write(rec, tag=f"r{repeat}"), flush=True)
    return rec


def _signoff_drc(run_dir: str):
    """The KLayout DRC count of the result chain's signoff (not another branch's)."""
    from sipath.scheduler import in_chain, run_chain
    with open(os.path.join(run_dir, "graph.json")) as f:
        g = json.load(f)
    p = (in_chain(run_chain(g), "signoff").get("outputs") or {}).get("signoff")
    if p and os.path.isfile(p):
        with open(p) as fh:
            return (json.load(fh).get("drc") or {}).get("count")
    return None


def main(argv) -> int:
    reps = int(argv[argv.index("--repeats") + 1]) if "--repeats" in argv else 1
    strategies = (argv[argv.index("--strategies") + 1].split(",") if "--strategies" in argv else
                  ["defaults", "sweep", "adaptive", "search", "adaptive_search"])
    skip = {argv[argv.index(o) + 1] for o in ("--repeats", "--strategies") if o in argv}
    designs = [a for a in argv if not a.startswith("--") and a not in skip]
    global SOURCE
    SOURCE = records.archive_source()  # the source as it is now, archived when uncommitted
    failed = []
    for i in range(1, reps + 1):  # repeat 1 of everything, then repeat 2: drift in the machine spreads evenly
        for name in designs:
            for s in strategies:
                try:
                    r = run(name, s, i)
                except Exception as e:  # noqa: BLE001  (one case failing does not stop the others)
                    print(f"{name} {s} r{i}: driver error: {type(e).__name__}: {e}", flush=True)
                    failed.append(f"{name} {s} r{i}")
                    continue
                m = r["metrics"]
                if r.get("outcome") != "completed":
                    failed.append(f"{name} {s} r{i}: {r.get('outcome')}: {r.get('failure')}")
                print(f"{name} {s} r{i}: {r.get('outcome')} wns_tt {m.get('wns_tt_ns')} worst {m.get('wns_worst_ns')} "
                      f"area {m.get('cell_area_um2')} wire {m.get('routed_wirelength_um')} {m.get('wall_s')} s",
                      flush=True)
    for f in failed:
        print("not completed:", f, flush=True)
    return 1 if failed else 0  # a signoff that fails its checks is a result, not a failure (its verdict says so)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
