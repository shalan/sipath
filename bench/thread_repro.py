"""Fresh serial repeats across thread counts; byte identity is not semantic equality.

python3 bench/thread_repro.py sipath/examples/gcd8.yaml --threads 1 2 4 --repeats 2 --out runs/thread-repro
Every run has its own directory/log; existing output directories are refused.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
METRICS = ("wns", "whs", "tns", "hpwl_um", "insts", "wirelength_um", "drc", "signoff_pass")


def sha(path):
    if not path or not Path(path).is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def snapshot(run):
    from sipath.scheduler import run_chain
    path = Path(run)/"graph.json"
    if not path.exists():
        return {"stages": {}, "verification": None}
    g = json.loads(path.read_text())
    stages = {}
    for a in run_chain(g):
        out = a.get("outputs") or {}; m = a.get("metrics") or {}
        stages[a["step"]] = {"checkpoint_sha256": sha(a.get("checkpoint")),
                             "def_sha256": sha(out.get("def_")),
                             "metrics": {k: m[k] for k in METRICS if k in m},
                             "requested_threads": a.get("threads"),
                             "observed_threads": m.get("openroad_threads")}
    return {"stages": stages, "verification": g.get("verification"),
            "inputs": g.get("inputs"), "config": g.get("config"), "provenance": g.get("provenance")}


def compare(a, b):
    """Missing artifacts are unknown, never evidence of equality."""
    result = {}
    for name in sorted(set(a["stages"]) | set(b["stages"])):
        x, y = a["stages"].get(name), b["stages"].get(name)
        if x is None or y is None:
            result[name] = {"missing_stage": True}
            continue
        result[name] = {key: (x.get(key) == y.get(key) if x.get(key) and y.get(key) else None)
                        for key in ("checkpoint_sha256", "def_sha256")}
        result[name]["metric_differences"] = {k: [x["metrics"].get(k), y["metrics"].get(k)]
            for k in sorted(set(x["metrics"]) | set(y["metrics"]))
            if x["metrics"].get(k) != y["metrics"].get(k)}
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("design")
    ap.add_argument("--threads", nargs="+", type=int, default=[1, 2, 4])
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.repeats < 2 or any(n < 1 for n in args.threads) or len(set(args.threads)) != len(args.threads):
        ap.error("need >=2 repeats and distinct positive thread counts")
    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=False)
    design = str(Path(args.design).resolve())
    child = '''import sys
from sipath import designfile
from sipath.config import Config
cfg=Config.load(**{**designfile.file_config(sys.argv[1]), "threads":int(sys.argv[3]), "strict_reproducibility":True})
f=designfile.run(sys.argv[1],sys.argv[2],config=cfg)
f.close()
v=f.verification()
sys.exit(0 if v["execution"]=="completed" and v["verification"]=="passed" else 1)
'''
    from bench import records
    from sipath.config import Config
    report = {"source": records.archive_source(), "machine": records.machine(Config.load().platform),
              "design": design, "threads": args.threads, "repeats": args.repeats,
              "comparison": "exact checkpoint/DEF bytes and recorded metrics; not semantic equivalence", "runs": []}
    failed = False
    for n in args.threads:
        for i in range(1, args.repeats+1):
            work = out/f"t{n}-r{i}"; start = time.monotonic()
            with open(out/f"t{n}-r{i}.log", "w") as log:
                rc = subprocess.run([sys.executable, "-c", child, design, str(work), str(n)], cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT).returncode
            record = {"threads": n, "repeat": i, "exit": rc, "seconds": time.monotonic()-start, **snapshot(work)}
            if report["runs"]:
                record["against_first_run"] = compare(report["runs"][0], record)
                previous = next((r for r in report["runs"] if r["threads"] == n), None)
                if previous:
                    record["against_same_threads"] = compare(previous, record)
            report["runs"].append(record)
            (out/"comparison.json").write_text(json.dumps(report, indent=2)+"\n")
            print(f"threads={n} repeat={i} exit={rc} seconds={record['seconds']:.1f}", flush=True)
            failed |= rc != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
