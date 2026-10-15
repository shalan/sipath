"""SiPath vs OpenROAD-flow-scripts on ORFS's own sky130hd reference designs (P1.4).

    python3 bench/orfs_compare.py gcd [aes riscv32i ...] [--skip-orfs] [--skip-sipath]
        [--routability [--only-routability]]   also SiPath with routability-driven placement

For each design, from the same RTL, SDC and core utilization (read from the
ORFS design config in the ORFS image):

* ORFS: ``make DESIGN_CONFIG=designs/sky130hd/<d>/config.mk`` in the ORFS image
  (results under runs/bench/<d>/orfs; LEC_CHECK=0: the kepler-formal binary
  of this image dies with an illegal instruction under Rosetta);
* SiPath: a generated design file on sky130A (open_pdks), a top-level block
  (is_core, routing to met5 as ORFS does), the standard flow with fill, signoff
  not failing the run (fail_on_violation false).

Both final layouts are then measured by the same code, in one OpenROAD
process each: setup WNS/TNS at the typical corner (the open_pdks tt Liberty,
the design's SDC, each flow's own extracted SPEF), routed wirelength, cell
count and area (logic cells: no fill, tap or decap), the detailed router's
DRC count, and the PDK's KLayout DRC deck on each GDS. Wall times are the
whole make / sipath run. Writes runs/bench/<d>/compare.json and prints a
Markdown table (runs/bench/compare.md for all designs given).

Inputs come from the local ORFS image (designs/src, designs/sky130hd); nothing
is downloaded.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BENCH = os.path.join(ROOT, "runs", "bench")
IMAGE = "openroad/orfs:latest"
ORFS = "/OpenROAD-flow-scripts/flow"


def docker(args: list[str], mounts: list[str], log: str | None = None) -> int:
    cmd = ["docker", "run", "--rm", "--platform", "linux/amd64"]
    for m in mounts:
        cmd += ["-v", f"{m}:{m}"]
    cmd += [IMAGE, *args]
    with open(log or os.devnull, "w") as f:
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode


def fetch_design(d: str) -> str:
    """Copy the design's sources and ORFS config out of the image (once)."""
    w = os.path.join(BENCH, d)
    os.makedirs(w, exist_ok=True)
    if not os.path.isdir(os.path.join(w, "cfg")):
        docker(["bash", "-c", f"mkdir -p {w}/cfg {w}/src && cp -r {ORFS}/designs/sky130hd/{d}/. {w}/cfg/ && "
                              f"cp -r {ORFS}/designs/src/{d}/. {w}/src/"], [BENCH])
    return w


def orfs_config(w: str, d: str) -> dict:
    with open(os.path.join(w, "cfg", "config.mk")) as f:
        text = f.read()
    var = dict(re.findall(r"^export\s+(\w+)\s*:?=\s*(.*?)\s*$", text, re.M))
    rtl = sorted(glob.glob(os.path.join(w, "src", "*.v")))
    if "VERILOG_FILES" in var and "wildcard" not in var["VERILOG_FILES"]:
        rtl = [os.path.join(w, "src", os.path.basename(p)) for p in var["VERILOG_FILES"].split()]
    # the design's own global-route capacity reductions (its FASTROUTE_TCL), for both flows
    adj = {}
    fr = os.path.join(w, "cfg", "fastroute.tcl")
    if os.path.isfile(fr):
        m = re.search(r"set_global_routing_layer_adjustment\s+\S+\s+([\d.]+)", open(fr).read())
        if m:
            adj = {ly: float(m.group(1)) for ly in ("met1", "met2", "met3", "met4", "met5")}
    return {"top": var["DESIGN_NAME"], "utilization": float(var.get("CORE_UTILIZATION", 40)) / 100,
            "layer_adjustments": adj,
            "aspect_ratio": float(var.get("CORE_ASPECT_RATIO", 1)), "margin": float(var.get("CORE_MARGIN", 2)),
            "rtl": rtl, "sdc": os.path.join(w, "cfg", "constraint.sdc")}


def orfs_result(w: str, d: str) -> dict:
    """An ORFS run's files (and its exit/wall time, from orfs/time.json)."""
    out = os.path.join(w, "orfs")
    res = os.path.join(out, "results", "sky130hd", d, "base")
    r = {"odb": os.path.join(res, "6_final.odb"), "spef": os.path.join(res, "6_final.spef"),
         "def": os.path.join(res, "6_final.def"),
         "gds": os.path.join(res, "6_final.gds"),
         "drt_rpt": os.path.join(out, "reports", "sky130hd", d, "base", "5_route_drc.rpt")}
    tj = os.path.join(out, "time.json")
    if os.path.isfile(tj):
        r.update(json.load(open(tj)))
    return r


def run_orfs(w: str, d: str) -> dict:
    out = os.path.join(w, "orfs")
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    rc = docker(["bash", "-c", f"cd {ORFS} && make DESIGN_CONFIG=./designs/sky130hd/{d}/config.mk "
                               f"WORK_HOME={out} LEC_CHECK=0"], [BENCH], os.path.join(out, "make.log"))
    with open(os.path.join(out, "time.json"), "w") as f:
        json.dump({"exit": rc, "seconds": round(time.time() - t0, 1)}, f)
    return orfs_result(w, d)


VARIANTS = {"sipath": ("SiPath", {}), "sipath_rd": ("SiPath, routability-driven place", {"routability_driven": True})}


def run_sipath(w: str, d: str, cfg: dict, tag: str = "sipath") -> dict:
    out = os.path.join(w, tag)
    place = VARIANTS[tag][1]
    os.makedirs(out, exist_ok=True)
    design = {"name": d, "top": cfg["top"], "mode": "macro", "pdk": "sky130A", "rtl": cfg["rtl"], "sdc": cfg["sdc"],
              "core_utilization": cfg["utilization"], "aspect_ratio": cfg["aspect_ratio"],
              "core_margin": cfg["margin"], "clock_ports": ["clk"],
              # a top-level block like ORFS's: routing up to met5 and a met4/met5 grid
              "is_core": True,
              "layer_adjustments": cfg.get("layer_adjustments") or {},
              # detailed routing of the larger designs takes over an hour under Rosetta
              "config": {"command_timeout": 14400},
              "flow": [{"step": s, **({"variants": [place]} if s == "place" and place else {})}
                       for s in ("synth", "floorplan", "physical", "place", "cts", "route_analog",
                                 "route_digital", "route_repair", "fill", "extract")]
              + [{"step": "signoff", "variants": [{"fail_on_violation": False}]}]}
    path = os.path.join(out, f"{d}.json")
    with open(path, "w") as f:
        json.dump(design, f, indent=1)
    t0 = time.time()
    with open(os.path.join(out, "run.log"), "w") as f:
        rc = subprocess.run([sys.executable, "-m", "sipath", "run", path, os.path.join(out, "run"), "-v"], cwd=ROOT,
                            stdout=f, stderr=subprocess.STDOUT).returncode
    with open(os.path.join(out, "time.json"), "w") as f:
        json.dump({"exit": rc, "seconds": round(time.time() - t0, 1)}, f)
    return sipath_result(w, tag)


def sipath_result(w: str, tag: str = "sipath") -> dict:
    """A SiPath run's final files, from its attempt graph (and <tag>/time.json)."""
    out = os.path.join(w, tag)
    r = {}
    tj = os.path.join(out, "time.json")
    if os.path.isfile(tj):
        r.update(json.load(open(tj)))
    graph = os.path.join(out, "run", "graph.json")
    if os.path.isfile(graph):
        g = json.load(open(graph))
        atts = {a["step"]: a for a in g["attempts"] if a.get("status") == "selected"}
        so, ex = atts.get("signoff", {}), atts.get("extract", {})
        r.update(odb=so.get("checkpoint"), spef=(ex.get("outputs", {}).get("spef_corners") or {}).get("tt_nom"),
                 gds=so.get("outputs", {}).get("gds"), drt_drc=atts.get("route_digital", {}).get("metrics", {}).get("drc"))
        r["def"] = so.get("outputs", {}).get("def_")
    return r


MEASURE = r"""
read_db {odb}
read_liberty {lib}
read_sdc {sdc}
read_spef {spef}
set_propagated_clock [all_clocks]
set cells 0; set area 0.0
set dbu [[ord::get_db_block] getDbUnitsPerMicron]
foreach i [[ord::get_db_block] getInsts] {{
  set m [$i getMaster]
  if {{[$m getType] ne "CORE" || [regexp {{fill|tap|decap|diode}} [$m getName]]}} continue
  incr cells; set area [expr {{$area + double([$m getWidth]) * [$m getHeight] / $dbu / $dbu}}]
}}
puts "BENCH [worst_slack -max] [total_negative_slack -max] $cells $area"
"""


def measure(r: dict, cfg: dict, lib: str, tag: str, w: str) -> dict:
    from sipath.signoff import parse_lyrdb
    out = {}
    if not (r.get("odb") and os.path.isfile(r["odb"]) and r.get("spef") and os.path.isfile(r["spef"])):
        return {"error": "no final database/SPEF"}
    script = os.path.join(w, f"measure_{tag}.tcl")
    with open(script, "w") as f:
        f.write(MEASURE.format(odb=r["odb"], lib=lib, sdc=cfg["sdc"], spef=r["spef"]))
    log = os.path.join(w, f"measure_{tag}.log")
    mounts = sorted({BENCH, os.path.dirname(lib), os.path.dirname(r["odb"]), os.path.dirname(r["spef"]),
                     os.path.dirname(cfg["sdc"])})
    docker([f"{ORFS}/../tools/install/OpenROAD/bin/openroad", "-no_splash", "-no_init", "-exit", script], mounts, log)
    m = re.search(r"BENCH (\S+) (\S+) (\d+) (\S+)", open(log).read())
    if m:
        out.update(wns_ns=round(float(m.group(1)), 3), tns_ns=round(float(m.group(2)), 3),
                   cells=int(m.group(3)), cell_area_um2=round(float(m.group(4)), 1))
    else:
        out["error"] = f"measurement failed ({log})"
    out["wirelength_um"] = def_wirelength(r.get("def"))
    # KLayout DRC with the PDK deck on the flow's GDS
    from sipath.pdk import load
    pdk = load("sky130A")
    deck, gds = pdk.signoff["drc_deck"], r.get("gds")
    if gds and os.path.isfile(gds):
        rpt = os.path.join(w, f"drc_{tag}.lyrdb")
        top = cfg["top"]
        docker(["klayout", "-b", "-r", deck, "-rd", f"input={gds}", "-rd", f"topcell={top}", "-rd", f"report={rpt}",
                "-rd", "feol=true", "-rd", "beol=true", "-rd", "offgrid=true", "-rd", "floating_met=false",
                "-rd", "seal=false", "-rd", "thr=4"], sorted({BENCH, os.path.dirname(deck), os.path.dirname(gds)}),
               os.path.join(w, f"drc_{tag}.log"))
        if os.path.isfile(rpt):
            p = parse_lyrdb(rpt, None, top)
            out.update(klayout_drc=p["count"], klayout_drc_rules=p["by_rule"])
    return out


def def_wirelength(path: str) -> int | None:
    """Routed signal wirelength (um) in a DEF: the length of every wire segment
    of the NETS section (vias and patches not counted)."""
    if not path or not os.path.isfile(path):
        return None
    text = open(path).read()
    s = text.find("\nNETS ")
    if s < 0:
        return None
    body = text[s:text.index("\nEND NETS", s)]
    dbu = int(re.search(r"UNITS DISTANCE MICRONS (\d+)", text).group(1))
    total = 0
    for path_text in re.split(r"\bNEW\b|\+\s*ROUTED\b|;", body):
        last = None
        for m in re.finditer(r"\(\s*(-?\d+|\*)\s+(-?\d+|\*)(?:\s+-?\d+)?\s*\)", path_text):
            x = last[0] if m.group(1) == "*" and last else (int(m.group(1)) if m.group(1) != "*" else None)
            y = last[1] if m.group(2) == "*" and last else (int(m.group(2)) if m.group(2) != "*" else None)
            if last is not None and x is not None and y is not None:
                total += abs(x - last[0]) + abs(y - last[1])
            last = (x, y)
    return round(total / dbu)


def drt_count(rpt: str) -> int | None:
    if not os.path.isfile(rpt):
        return None
    return len(re.findall(r"violation type:", open(rpt).read()))


def main(argv):
    designs = [a for a in argv if not a.startswith("-")] or ["gcd"]
    from sipath.pdk import load
    lib = [c.liberty for c in load("sky130A").analysis_corners() if c.name == "tt_nom"][0]
    rows = []
    for d in designs:
        w = fetch_design(d)
        cfg = orfs_config(w, d)
        res = {}
        if "--skip-orfs" not in argv:
            res["orfs"] = run_orfs(w, d)
        elif os.path.isdir(os.path.join(w, "orfs", "results")):
            res["orfs"] = orfs_result(w, d)
        tags = ["sipath"] + (["sipath_rd"] if "--routability" in argv else [])
        for tag in tags:
            if "--skip-sipath" not in argv and not (tag == "sipath" and "--only-routability" in argv):
                res[tag] = run_sipath(w, d, cfg, tag)
            elif os.path.isdir(os.path.join(w, tag, "run")):
                res[tag] = sipath_result(w, tag)
        if "--routability" not in argv and os.path.isdir(os.path.join(w, "sipath_rd", "run")):
            res["sipath_rd"] = sipath_result(w, "sipath_rd")
        for tag in ("orfs", "sipath", "sipath_rd"):
            if tag in res:
                res[tag]["measured"] = measure(res[tag], cfg, lib, tag, w)
        if "orfs" in res:
            res["orfs"]["drt_drc"] = drt_count(res["orfs"].get("drt_rpt", ""))
        with open(os.path.join(w, "compare.json"), "w") as f:
            json.dump({"config": cfg, **res}, f, indent=1)
        rows.append((d, cfg, res))
    lines = ["| design | flow | WNS (ns) | TNS (ns) | wirelength (um) | cells | cell area (um2) | DRT DRC | KLayout DRC | wall (s) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for d, cfg, res in rows:
        for tag in ("orfs", "sipath", "sipath_rd"):
            if tag not in res:
                continue
            r = res.get(tag, {})
            m = r.get("measured", {})
            label = "ORFS" if tag == "orfs" else VARIANTS[tag][0]
            lines.append(f"| {d} | {label} | {m.get('wns_ns', '-')} | {m.get('tns_ns', '-')} | "
                         f"{m.get('wirelength_um', '-')} | {m.get('cells', '-')} | {m.get('cell_area_um2', '-')} | "
                         f"{r.get('drt_drc', '-')} | {m.get('klayout_drc', '-')} | {r.get('seconds', '-')} |")
    table = "\n".join(lines)
    with open(os.path.join(BENCH, "compare.md"), "w") as f:
        f.write(table + "\n")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
