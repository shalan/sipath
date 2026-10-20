"""ChipFoundry's tapeout precheck (cf-precheck) on a signed-off harness design.

SiPath does not re-implement the precheck: it runs ChipFoundry's own tool,
pinned (version, image, golden files), on a project directory exported from
the run, and reads its results.

- The project directory has the layout cf-precheck expects for the harness:
  ``gds/<wrapper>.gds`` (the signed-off wrapper GDS) and
  ``verilog/gl/<wrapper>.v`` (the routed wrapper's netlist with supply pins,
  for its Netgen LVS).
- The golden root is built from the harness's pinned files
  (``precheck.golden``: the Makefile whose ``uncompress`` target cf-precheck
  runs, the frame's top netlist, the empty wrapper GDS for the XOR check).
- cf-precheck runs in the precheck image (``config.precheck_image``,
  ``docker/precheck``: KLayout, Magic, Netgen, CVC and cf-precheck), with the
  PDK tree of the run (``-p``), and writes ``<project>/.cf/project.json``:
  pass, fail or skip per check, with details.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil


def export_project(project: str, wrapper: str, gds: str, netlist: str) -> dict:
    """The project directory cf-precheck reads, from the run's files: returns
    {gds, netlist, lvs_config} as written. The LVS configuration includes
    cf-precheck's own one for the wrapper (``$LVS_ROOT/tech/$PDK/
    lvs_config.<wrapper>.json``), which names the wrapper's netlist and GDS:
    the wrapper is flat (standard cells only)."""
    os.makedirs(os.path.join(project, "gds"), exist_ok=True)
    os.makedirs(os.path.join(project, "verilog", "gl"), exist_ok=True)
    out = {"gds": os.path.join(project, "gds", f"{wrapper}.gds"),
           "netlist": os.path.join(project, "verilog", "gl", f"{wrapper}.v")}
    shutil.copyfile(gds, out["gds"])
    shutil.copyfile(netlist, out["netlist"])
    cfg = os.path.join(project, "lvs", wrapper, "lvs_config.json")
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    with open(cfg, "w") as f:
        json.dump({"INCLUDE_CONFIGS": [f"$LVS_ROOT/tech/$PDK/lvs_config.{wrapper}.json"]}, f, indent=1)
    out["lvs_config"] = cfg
    return out


def golden_root(src: str, dst: str) -> str:
    """The golden root cf-precheck compares against (``-c``), copied from the
    harness's pinned files with ``.gz`` files also decompressed (the XOR check
    reads the uncompressed GDS)."""
    shutil.copytree(src, dst, dirs_exist_ok=True)
    for dp, _, fs in os.walk(dst):
        for f in fs:
            if f.endswith(".gz"):
                with gzip.open(os.path.join(dp, f)) as fi, open(os.path.join(dp, f[:-3]), "wb") as fo:
                    shutil.copyfileobj(fi, fo)
    return dst


def command(project: str, pdk_tree: str, golden: str, out: str, skip: list[str] = (), magic_drc: bool = False) -> list:
    """The cf-precheck command line."""
    cmd = ["cf-precheck", "-i", project, "-p", pdk_tree, "-c", golden, "-o", out]
    if magic_drc:
        cmd.append("--magic-drc")
    if skip:
        cmd += ["--skip-checks", *skip]
    return cmd


def results(project: str) -> dict:
    """cf-precheck's results from ``<project>/.cf/project.json`` (its
    ``precheck`` entry): {version, checks: {name: {status, details?, reason?}},
    failed, passed, skipped, pass}. A missing file or entry, or the tool's own
    ``passed`` disagreeing with its checks, is a failed precheck, never an
    empty pass."""
    path = os.path.join(project, ".cf", "project.json")
    if not os.path.isfile(path):
        return {"pass": False, "error": f"cf-precheck wrote no results ({path})", "checks": {}}
    with open(path) as f:
        blob = (json.load(f) or {}).get("precheck") or {}
    checks = blob.get("checks") or {}
    if not checks:
        return {"pass": False, "error": f"no check results in {path}", "checks": {}}
    by = {s: sorted(n for n, c in checks.items() if c.get("status") == s) for s in ("fail", "pass", "skip")}
    out = {"version": blob.get("version"), "pdk": blob.get("pdk"), "checks": checks, "failed": by["fail"],
           "passed": by["pass"], "skipped": by["skip"], "pass": not by["fail"] and bool(blob.get("passed")),
           "file": path}
    if bool(blob.get("passed")) != (not by["fail"]):
        out["error"] = "cf-precheck's overall result disagrees with its checks"
        out["pass"] = False
    return out
