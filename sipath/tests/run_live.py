"""The live test classes in parallel processes (P1.3).

    python3 -m sipath.tests.run_live [-j N] [Class ...] [--keep DIR]

Each class of ``sipath.tests.test_live`` runs in its own ``python3 -m unittest``
process, longest first (mcu8, dominated by its LVS, starts at once), at most
``N`` at a time (default 4). The run's OpenROAD threads are shared among them:
each process gets ``threads // N`` unless ``SIPATH_THREADS`` is set. ``--keep``
keeps every run's outputs under DIR (``SIPATH_TEST_KEEP``). Prints each class's
result and wall time and the slowest tests; exits 1 when any class fails.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

# rough wall times (s) on the reference machine, for the order only (longest first)
EXPECTED = {"LiveMcuDie": 2900, "LiveSoftMacro": 300, "LiveHs": 110, "LiveRtl": 90, "LiveResume": 40,
            "LiveMcp": 30, "LiveIhp": 30, "LiveScale": 10, "LiveIhpRing": 5, "OpenRoad": 5}


def live_classes() -> list[str]:
    from . import test_live
    return sorted(n for n, c in vars(test_live).items()
                  if isinstance(c, type) and issubclass(c, unittest.TestCase) and c.__module__ == test_live.__name__)


def run_class(name: str, env: dict) -> dict:
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "unittest", "-v", "--durations", "0", f"sipath.tests.test_live.{name}"],
                       env=env, capture_output=True, text=True)
    out = p.stdout + p.stderr
    ran = re.search(r"^Ran (\d+) tests? in", out, re.M)
    durations = [(float(s), t) for s, t in re.findall(r"^([\d.]+)s\s+(test_\w+)", out, re.M)]
    return {"class": name, "rc": p.returncode, "seconds": round(time.time() - t0, 1),
            "tests": int(ran.group(1)) if ran else 0, "durations": durations, "output": out}


def main(argv) -> int:
    jobs, keep, names, it = 4, None, [], iter(argv)
    for a in it:
        if a == "-j":
            jobs = int(next(it))
        elif a == "--keep":
            keep = next(it)
        else:
            names.append(a)
    names = names or live_classes()
    names.sort(key=lambda n: -EXPECTED.get(n, 60))
    from sipath.config import Config
    env = dict(os.environ)
    if not env.get("SIPATH_THREADS"):
        env["SIPATH_THREADS"] = str(max(1, Config().threads // max(1, min(jobs, len(names)))))
    if keep:
        env["SIPATH_TEST_KEEP"] = os.path.abspath(keep)
    print(f"{len(names)} live classes, {jobs} at a time, {env['SIPATH_THREADS']} OpenROAD threads each", flush=True)
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:  # reported as each class finishes
        for fu in as_completed([ex.submit(run_class, n, env) for n in names]):
            r = fu.result()
            print(f"  {r['class']:<16} {'ok' if r['rc'] == 0 else 'FAILED'}  {r['tests']:>3} tests  {r['seconds']:>8.1f} s",
                  flush=True)
            results.append(r)
    wall = time.time() - t0
    failed = [r for r in results if r["rc"] != 0]
    print(f"\n{sum(r['tests'] for r in results)} tests in {wall:.0f} s wall "
          f"({sum(r['seconds'] for r in results):.0f} s of class time); {len(failed)} classes failed")
    slow = sorted(((d, f"{r['class']}.{t}") for r in results for d, t in r["durations"]), reverse=True)[:10]
    if slow:
        print("slowest tests (setUpClass not included):")
        for d, t in slow:
            print(f"  {d:8.1f} s  {t}")
    for r in failed:
        print(f"\n===== {r['class']} =====\n" + r["output"][-6000:])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
