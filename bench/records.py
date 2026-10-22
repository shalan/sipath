"""Benchmark results as records: one JSON file per measured result, everything needed to know where a number
came from and to measure it again.

A record (``bench/results/<kind>/<design>__<flow>__<id>.json``) holds the metrics under names that say what
they measure (``wns_tt_ns``: setup slack at the typical corner; ``wns_worst_ns``: the worst over every signoff
corner; ``routed_wirelength_um``: from the final DEF; ``hpwl_um``: a placement's half-perimeter estimate), the
run's verification verdict, the per-step seconds, the SiPath version and commit, the toolchain (image digest,
open_pdks release), the machine (CPU, cores, OS, whether the images ran emulated), the inputs' content hashes,
the overrides and seeds, and the run directory it came from. ``bench/tables.py`` writes the documentation's
tables from these records only.

Records written after the fact from an earlier run's files say so (``backfilled``) and carry what those files
recorded, nothing guessed: a run from before SiPath recorded its commit has ``sipath.commit`` null.
"""
from __future__ import annotations

import glob
import json
import os
import platform
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "bench", "results")
sys.path.insert(0, ROOT)


def machine(image_platform: str | None = None) -> dict:
    arch = platform.machine()
    emulated = bool(image_platform) and image_platform.endswith("amd64") and arch in ("arm64", "aarch64")
    return {"os": f"{platform.system()} {platform.release()}", "arch": arch, "cores": os.cpu_count(),
            "cpu": _cpu(), "python": platform.python_version(),
            **({"image_platform": image_platform, "emulated": emulated} if image_platform else {})}


def _cpu() -> str:
    try:
        if sys.platform == "darwin":
            import subprocess
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        with open("/proc/cpuinfo") as f:
            m = re.search(r"model name\s*:\s*(.+)", f.read())
            return m.group(1).strip() if m else platform.processor()
    except OSError:
        return platform.processor()


def toolchain() -> dict:
    from sipath import toolchain as tc
    lock = tc.lock()
    return {"orfs": lock["images"]["orfs"]["ref"], "pdk": {k: v["version"] for k, v in lock.get("pdks", {}).items()}}


def inputs(paths) -> dict:
    from sipath.fingerprint import file_hash
    return {os.path.relpath(os.path.abspath(p), ROOT): file_hash(p) for p in paths if p and os.path.isfile(p)}


def orfs_stages(orfs_dir: str) -> dict:
    """ORFS's own stage times (seconds): each log's ``Elapsed time``, summed by stage number (1 synth ... 6
    finish), and their total ("tools")."""
    stages: dict[str, float] = {}
    names = {"1": "synth", "2": "floorplan", "3": "place", "4": "cts", "5": "route", "6": "finish"}
    for log in glob.glob(os.path.join(orfs_dir, "logs", "*", "*", "*", "*.log")):
        with open(log, errors="replace") as f:
            m = re.search(r"Elapsed time: (?:(\d+):)?(\d+):([\d.]+)", f.read())
        if not m:
            continue
        sec = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        key = names.get(os.path.basename(log)[0], "other")
        stages[key] = round(stages.get(key, 0.0) + sec, 1)
    if stages:
        stages["tools"] = round(sum(v for k, v in stages.items()), 1)
    return stages


def from_run(run_dir: str) -> dict:
    """What a SiPath run's own files say: the verdict, each selected step's seconds, the provenance's identity."""
    from sipath import verdict
    with open(os.path.join(run_dir, "graph.json")) as f:
        g = json.load(f)
    v = g.get("verification") or verdict.verify(g)
    by_id = {a["id"]: a for a in g["attempts"]}
    steps = {}
    for name, s in (g.get("steps") or {}).items():  # every attempt of the step: a failed try costs time too
        atts = [a for a in g["attempts"] if a["step"] == name]
        steps[name] = round(sum(a.get("seconds") or 0 for a in atts), 1)
    head = by_id.get(v.get("head")) if v.get("head") else None
    prov = g.get("provenance") or {}
    return {"verification": {"execution": v["execution"], "verification": v["verification"],
                             "failed": sorted(c for c, x in v["checks"].items() if x["status"] == "failed" and x["required"]),
                             "inconclusive": sorted(c for c, x in v["checks"].items() if x["status"] == "inconclusive"),
                             "omitted": v["omitted"], "notes": v["notes"]},
            "steps": steps, "sipath": prov.get("sipath") or {"version": None, "commit": None},
            "toolchain": {"orfs": prov.get("digest") or prov.get("image"),
                          "pdk": {(prov.get("pdk_release") or {}).get("family") or "pdk":
                                  (prov.get("pdk_release") or {}).get("version")}},
            "image_platform": prov.get("platform"), "router_drc": _router_drc(g),
            "worst_setup": _worst_setup(g), "head_metrics": (head or {}).get("metrics") or {},
            **({"failure": _failure(g)} if v["execution"] != "completed" else {})}


def _repeat_inputs(run_dir: str, run: dict, export_hashes: dict) -> dict:
    """A repeat's inputs: as its run recorded them; without a graph of its own, hashed at export, said so."""
    g = os.path.join(run_dir, "graph.json")
    if run.get("verification") and os.path.isfile(g):
        with open(g) as f:
            ins, note = run_inputs(json.load(f))
        return {"inputs_": ins, **({"inputs_note": note} if note else {})}
    return {"inputs_": export_hashes, "inputs_note": "hashed at export: no graph of this run to read them from"}


def _log_failure(log: str) -> str | None:
    """A failed run's reason from its own log: the last tool error, else the exception's last line."""
    if not os.path.isfile(log):
        return None
    with open(log, errors="replace") as f:
        text = f.read()
    errs = re.findall(r"\[ERROR [A-Z]+-\d+\][^\n]*", text)
    if errs:
        return errs[-1][:200]
    tail = [ln for ln in text.strip().splitlines() if ln.strip()]
    return tail[-1][:200] if tail else None


def _failure(g) -> str | None:
    """Why the run stopped: the latest failed attempt's error, first line (the tool's own message when there)."""
    for a in reversed(g["attempts"]):
        if a.get("status") in ("failed", "interrupted", "cancelled", "budget") and a.get("error"):
            err = a["error"]
            m = re.search(r"\[ERROR [A-Z]+-\d+\][^\n]*", err)
            return f"{a['id']}: {m.group(0) if m else err.splitlines()[0][:200]}"
    return None


def _selected(g, step: str) -> dict:
    """The run's result chain's attempt of ``step`` (a branch's too): every metric comes from the winner's
    ancestry (sipath.scheduler.run_chain), never from another branch's selection."""
    from sipath.scheduler import in_chain, run_chain
    return in_chain(run_chain(g), step)


def run_metrics(run_dir: str) -> dict:
    """What a design-file run measured itself: slow-corner and worst setup slack (signoff), routed wirelength,
    instances, the synthesized netlist's area, slew violations."""
    with open(os.path.join(run_dir, "graph.json")) as f:
        g = json.load(f)
    # the latest signoff attempt's own file: a signoff that failed its checks (fail_on_violation) wrote it too
    so_path = (_selected(g, "signoff").get("outputs") or {}).get("signoff")
    if not so_path:
        tried = [a for a in g["attempts"] if a["step"] == "signoff" and a.get("dir")]
        so_path = os.path.join(tried[-1]["dir"], "signoff.json") if tried else None
    corners, tim = {}, {}
    if so_path and os.path.isfile(so_path):
        with open(so_path) as f:
            tim = json.load(f).get("timing") or {}
        corners = tim.get("corners") or {}
    routed = _selected(g, "route_repair") or _selected(g, "route_digital")
    ws = [c["wns"] for c in corners.values() if c.get("wns") is not None]
    return {"wns_ss_max_ns": (corners.get("ss_max") or {}).get("wns"), "wns_worst_ns": round(min(ws), 3) if ws else None,
            "routed_wirelength_um": (routed.get("metrics") or {}).get("wirelength_um"),
            "instances": (routed.get("metrics") or {}).get("insts"),
            "synth_area_um2": (_selected(g, "synth").get("metrics") or {}).get("synth_area_um2"),
            "synth_recipe": (_selected(g, "synth").get("metrics") or {}).get("abc_recipe"),
            "drv_slew": tim.get("drv_slew"), "final_def_sha256": final_def_digest(g)}


def final_def_digest(g) -> str | None:
    """The layout the run ended with: sha256 (16 hex) of the result chain's final DEF (signoff's, else
    extraction's), so two results can be told to be the same layout, not only the same numbers."""
    import hashlib
    from sipath.scheduler import in_chain, run_chain
    ch = run_chain(g)
    for step in ("signoff", "extract"):
        p = (in_chain(ch, step).get("outputs") or {}).get("def_")
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
    return None


def from_design_run(run_dir: str, kind: str, design: str, flow: str, backfilled: bool = False, **extra) -> dict:
    """A record of any design-file run directory: its own measurements, verdict and identity."""
    run = from_run(run_dir)
    with open(os.path.join(run_dir, "graph.json")) as f:
        g = json.load(f)
    ins, note = run_inputs(g)
    return make(kind, design, flow, run_metrics(run_dir), run=run, source=run_dir, backfilled=backfilled,
                measured=_mtime_date(os.path.join(run_dir, "graph.json")), image_platform=run["image_platform"],
                toolchain_=run["toolchain"], inputs_=ins, **({"inputs_note": note} if note else {}),
                **({"failure": run["failure"]} if run.get("failure") else {}), **extra)


def _router_drc(g) -> int | None:
    """The detailed router's violations on the result chain's last routing attempt."""
    for name in ("route_repair", "route_digital"):
        a = _selected(g, name)
        if a and isinstance((a.get("metrics") or {}).get("drc"), (int, float)):
            return int(a["metrics"]["drc"])
    return None


def _worst_setup(g) -> float | None:
    """The worst setup slack over every signoff corner of the result chain's signoff (signoff.json), or None."""
    p = (_selected(g, "signoff").get("outputs") or {}).get("signoff")
    if p and os.path.isfile(p):
        with open(p) as f:
            corners = ((json.load(f).get("timing") or {}).get("corners") or {})
        ws = [c["wns"] for c in corners.values() if c.get("wns") is not None]
        return round(min(ws), 3) if ws else None
    return None


def _mtime_date(path: str) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))


def from_orfs_compare(design_dir: str, backfilled: bool = False) -> list[dict]:
    """Records of one ``bench/orfs_compare.py`` design: ORFS's result and SiPath's, as compare.json and the runs
    measured them (the same measurement code for both)."""
    with open(os.path.join(design_dir, "compare.json")) as f:
        c = json.load(f)
    cfg, out = c["config"], []
    design = os.path.basename(os.path.normpath(design_dir))
    ins = inputs([*(cfg.get("rtl") or []), cfg.get("sdc"), cfg.get("design_file")])
    for flow in ("orfs", "sipath"):
        r = c.get(flow)
        if not r:
            continue
        m = r.get("measured") or {}
        metrics = {"wns_tt_ns": m.get("wns_ns"), "tns_tt_ns": m.get("tns_ns"),
                   "routed_wirelength_um": m.get("wirelength_um"), "cells": m.get("cells"),
                   "cell_area_um2": m.get("cell_area_um2"), "drc_klayout": m.get("klayout_drc"),
                   "drc_router": r.get("drt_drc"), "wall_s": r.get("seconds"), "exit": r.get("exit")}
        run_dir = os.path.join(design_dir, flow, "run")
        if flow == "orfs":
            stages = orfs_stages(os.path.join(design_dir, "orfs"))
            when = os.path.join(design_dir, "orfs", "time.json")
            out.append(make("orfs_compare", design, "orfs", metrics, stages=stages,
                            measured=_mtime_date(when) if os.path.isfile(when) else None,
                            source=os.path.join(design_dir, "orfs"), inputs_=ins, backfilled=backfilled,
                            inputs_note="hashed at export: ORFS's run records no input hashes",
                            toolchain_={"orfs": c.get("orfs_image") or toolchain()["orfs"]}))
        elif os.path.isfile(os.path.join(run_dir, "graph.json")):
            run = from_run(run_dir)
            metrics.update(drc_router=run["router_drc"], wns_worst_ns=run["worst_setup"])
            with open(os.path.join(run_dir, "graph.json")) as f:
                rins, rnote = run_inputs(json.load(f))
            out.append(make("orfs_compare", design, "sipath", metrics, run=run, source=run_dir, inputs_=rins,
                            **({"inputs_note": rnote} if rnote else {}),
                            measured=_mtime_date(os.path.join(run_dir, "graph.json")), backfilled=backfilled,
                            overrides={"flow": cfg.get("flow")} if cfg.get("flow") else {},
                            image_platform=run["image_platform"], toolchain_=run["toolchain"]))
    return out


def from_repeat(design_dir: str, backfilled: bool = False) -> list[dict]:
    """Records of ``bench/repeat_compare.py``: every configuration's every repeat, with its run's verdict."""
    with open(os.path.join(design_dir, "results.json")) as f:
        res = json.load(f)
    design = os.path.basename(os.path.normpath(design_dir))
    out = []
    for cfg, runs in res.items():
        for i, r in enumerate(runs, 1):
            tag = f"{cfg}_r{i}"
            run_dir = os.path.join(design_dir, tag, "run")
            spec = os.path.join(design_dir, tag, f"{design}.json")
            design_file = {}
            if os.path.isfile(spec):
                with open(spec) as f:
                    design_file = json.load(f)
            ins = inputs([*(design_file.get("rtl") or []), design_file.get("sdc")])
            overrides = {s["step"]: s["variants"][0] for s in design_file.get("flow") or [] if s.get("variants")}
            metrics = {"wns_tt_ns": r.get("wns_ns"), "tns_tt_ns": r.get("tns_ns"), "wns_ss_max_ns": r.get("wns_ss_max_ns"),
                       "whs_min_ns": r.get("whs_min_ns"), "routed_wirelength_um": r.get("wirelength_um"),
                       "cells": r.get("cells"), "cell_area_um2": r.get("cell_area_um2"), "drc_klayout": r.get("drc"),
                       "lvs_match": r.get("lvs_match"), "drv_slew": r.get("drv_slew"), "wall_s": r.get("seconds"),
                       "exit": r.get("exit"), "synth_recipe": r.get("synth_recipe"), "netlist_sha": r.get("netlist_sha")}
            run = from_run(run_dir) if os.path.isfile(os.path.join(run_dir, "graph.json")) else {}
            log_failure = _log_failure(os.path.join(design_dir, tag, "run.log")) if r.get("exit") else None
            if run and r.get("exit") and run["verification"]["execution"] == "completed":
                # the directory was resumed later: its graph is not this result's; the run's own log says what failed
                run = {"graph_not_this_result": True}
            if run.get("graph_not_this_result"):
                metrics["note"] = "run directory resumed after this result; its graph describes the later run"
            elif run:
                metrics.update(drc_router=run["router_drc"], wns_worst_ns=run["worst_setup"])
                rm = run_metrics(run_dir)
                metrics.update(synth_area_um2=rm["synth_area_um2"], instances=rm["instances"])
            failure = run.get("failure") or log_failure
            extra = {"failure": failure} if failure else {}
            src = run_dir if run and not run.get("graph_not_this_result") else os.path.join(design_dir, tag)
            when = os.path.join(design_dir, tag, "time.json")
            out.append(make("repeat", design, cfg, metrics, run=run if "verification" in run else None, repeat=i,
                            source=src, **extra, overrides=overrides, backfilled=backfilled,
                            **_repeat_inputs(run_dir, run, ins),
                            measured=_mtime_date(when) if os.path.isfile(when) else None,
                            image_platform=run.get("image_platform", "linux/amd64"),
                            toolchain_=run.get("toolchain")))
    return out


def make(kind: str, design: str, flow: str, metrics: dict, *, measured: str | None = None, source: str | None = None,
         run: dict | None = None, inputs_: dict | None = None, overrides: dict | None = None, seeds=None,
         image_platform: str | None = "linux/amd64", backfilled: bool = False, notes: str = "",
         toolchain_: dict | None = None, **extra) -> dict:
    run = run or {}
    return {"kind": kind, "design": design, "flow": flow, "measured": measured or time.strftime("%Y-%m-%d"),
            "metrics": metrics, "verification": run.get("verification"), "steps": run.get("steps"),
            "sipath": run.get("sipath") if flow != "orfs" else None, "toolchain": toolchain_ or toolchain(),
            "machine": machine(image_platform), "inputs": inputs_, "overrides": overrides or {},
            "seeds": seeds, "source": os.path.relpath(source, ROOT) if source else None, "backfilled": backfilled,
            "exported": {"at": time.strftime("%Y-%m-%d %H:%M"), "by": _exporter()},  # not the run's own identity
            **({"notes": notes} if notes else {}), **extra}


def _exporter() -> dict:
    from sipath.designfile import sipath_identity
    return sipath_identity()


def run_inputs(g: dict) -> tuple[dict | None, str | None]:
    """The input hashes the run recorded when it ran (graph.json "inputs"), never hashed again at export: a file
    changed or deleted since would otherwise lend its new identity to old measurements."""
    files = (g.get("inputs") or {}).get("files")
    if files is None:
        return None, "unknown: the run predates recorded input hashes"
    return {os.path.relpath(k, ROOT) if k.startswith(ROOT) else k: v for k, v in files.items()}, None


def archive_source() -> dict:
    """The working tree's identity now (before a benchmark runs); with uncommitted changes, the diff and the
    untracked sources are archived under bench/results/sources/<key>, the key a digest of the base commit, the
    tracked diff and every untracked file's path and content: two different trees never share an archive, and an
    archive once written is never rewritten (the same key is the same content)."""
    import hashlib
    import shutil
    import subprocess
    from sipath.designfile import sipath_identity
    ident = sipath_identity()
    if not (ident.get("diff_sha256") or ident.get("untracked")):
        return ident
    h = hashlib.sha256()
    h.update((ident.get("commit") or "").encode())
    h.update((ident.get("diff_sha256") or "").encode())
    for rel, digest in sorted((ident.get("untracked") or {}).items()):
        h.update(f"{rel}\0{digest}\0".encode())
    key = h.hexdigest()[:16]
    d = os.path.join(RESULTS, "sources", key)
    if not os.path.isdir(d):  # immutable: written once
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        if ident.get("diff_sha256"):
            diff = subprocess.run(["git", "-C", ROOT, "diff", "HEAD", "--binary"], capture_output=True, text=True).stdout
            with open(os.path.join(tmp, "diff.patch"), "w") as f:
                f.write(diff)
        for rel in ident.get("untracked") or {}:
            dst = os.path.join(tmp, "untracked", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(ROOT, rel), dst)
        with open(os.path.join(tmp, "identity.json"), "w") as f:
            json.dump(ident, f, indent=1, sort_keys=True)
        os.replace(tmp, d)
    ident["archive"] = os.path.relpath(d, ROOT)
    ident["archive_key"] = key
    return ident


def write(rec: dict, tag: str = "") -> str:
    d = os.path.join(RESULTS, rec["kind"])
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{rec['design']}__{rec['flow']}{('__' + tag) if tag else ''}.json")
    with open(path, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
        f.write("\n")
    return path


def load(kind: str | None = None) -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(RESULTS, kind or "*", "*.json"))):
        with open(p) as f:
            out.append(dict(json.load(f), _file=os.path.relpath(p, ROOT)))
    return out
