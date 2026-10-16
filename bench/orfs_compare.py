"""SiPath vs OpenROAD-flow-scripts on ORFS's own sky130hd reference designs (P1.4).

    python3 bench/orfs_compare.py gcd [aes riscv32i ...] [--skip-orfs] [--skip-sipath]
        [--routability [--only-routability]]   also SiPath with routability-driven placement
    python3 bench/orfs_compare.py sipath/examples/dp8.yaml [...]   SiPath's own designs through ORFS

A design file (``.yaml``/``.json``) goes the other way: ORFS gets a generated
config.mk (runs/bench/<name>/cfg) with the design's netlist
(``SYNTH_NETLIST_FILES``) or RTL, its SDC, its floorplan DEF (or core
utilization and margin) and its top routing layer, on the ORFS platform of its
PDK (sky130A: sky130hd, sky130A_hs: sky130hs, ihp-sg13g2); SiPath runs the
design's own flow with signoff appended when it has none (not failing the
run). What only SiPath does (analog nets first on a non-default rule,
structured datapath placement, clusters, a pad ring) ORFS does not see: it
treats the netlist as plain logic. Full dies (pad ring, hard macros) are not
supported this way.

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


ORFS_PLATFORM = {"sky130A": "sky130hd", "sky130A_hs": "sky130hs", "ihp-sg13g2": "ihp-sg13g2"}


def sipath_design(path: str) -> tuple[str, dict]:
    """(name, cfg) for one of SiPath's design files, with an ORFS config.mk
    written for it under runs/bench/<name>/cfg."""
    from sipath import designfile
    from sipath.pdk import load
    d, spec = designfile.load(path)  # generates the design's inputs when it has a generator
    if d.mode != "macro" or d.macros:
        raise SystemExit(f"{path}: only soft macros (no pad ring, no hard macros) go through ORFS this way")
    if d.pdk not in ORFS_PLATFORM:
        raise SystemExit(f"{path}: no ORFS platform for PDK {d.pdk}")
    platform = ORFS_PLATFORM[d.pdk]
    pdk = load(d.pdk, platforms=f"{ORFS}/platforms")  # IHP: the image's platform files
    top_layer = pdk.max_layer_core if d.is_core else pdk.max_layer_macro
    w = os.path.join(BENCH, d.name)
    os.makedirs(os.path.join(w, "cfg"), exist_ok=True)
    src = [d.netlist] if d.netlist else list(d.rtl)
    mk = [f"export DESIGN_NAME = {d.top}", f"export DESIGN_NICKNAME = {d.name}", f"export PLATFORM = {platform}",
          f"export VERILOG_FILES = {' '.join(src)}", f"export SDC_FILE = {d.sdc}",
          f"export MAX_ROUTING_LAYER = {top_layer}"]
    if d.netlist:
        mk.append(f"export SYNTH_NETLIST_FILES = {d.netlist}")
    if d.floorplan_def:
        mk.append(f"export FLOORPLAN_DEF = {d.floorplan_def}")
    else:
        mk += [f"export CORE_UTILIZATION = {round(d.core_utilization * 100)}",
               f"export CORE_MARGIN = {d.core_margin}"]
    if getattr(d, "rtl_defines", None):
        mk.append("export VERILOG_DEFINES = " + " ".join(f"-D{k}={v}" for k, v in d.rtl_defines.items()))
    # what SiPath knows from the design file, given to ORFS the way it takes it:
    # analog nets left alone by repair (SiPath routes them first on a non-default
    # rule; ORFS's repair otherwise breaks on an input-to-output analog net,
    # RSZ-0074), and for an embedded macro the same power grid (rails and
    # straps, no core ring) where it is fixed
    if d.analog_nets:
        hook = os.path.join(w, "cfg", "post_floorplan.tcl")
        with open(hook, "w") as f:
            f.write("set_dont_touch [get_nets {" + " ".join("{%s}" % n for n in d.analog_nets) + "}]\n")
        mk.append(f"export POST_FLOORPLAN_TCL = {hook}")
    pdn = d.resolve(pdk).pdn
    # the design's own straps, or IHP (its platform ring does not fit a macro's
    # margin); straps SiPath takes from the PDK it scales to the design, so
    # there ORFS keeps its platform grid
    if not d.is_core and (not pdn.straps_from_pdk or platform == "ihp-sg13g2"):
        gc = ([("VDD", "VPWR", True), ("VDD", "VPB", False), ("VSS", "VGND", True), ("VSS", "VNB", False)]
              if platform.startswith("sky130") else [("VDD", "VDD", True), ("VSS", "VSS", True)])
        top = max((s.layer for s in pdn.straps), key=pdk.layer_index) if pdn.straps else pdn.rail_layer
        lines = [f"add_global_connection -net {{{n}}} -inst_pattern {{.*}} -pin_pattern {{^{pin}$}}"
                 + ((" -power" if n == "VDD" else " -ground") if prim else "") for n, pin, prim in gc]
        lines += ["global_connect", "set_voltage_domain -name {CORE} -power {VDD} -ground {VSS}",
                  f"define_pdn_grid -name {{grid}} -voltage_domains {{CORE}} -pins {{{top}}}",
                  f"add_pdn_stripe -grid {{grid}} -layer {{{pdn.rail_layer}}} -width {{{pdn.rail_width}}} -followpins"]
        lines += [f"add_pdn_stripe -grid {{grid}} -layer {{{s.layer}}} -width {{{s.width}}} -pitch {{{s.pitch}}} "
                  f"-offset {{{s.offset}}}" for s in pdn.straps or []]
        lines += [f"add_pdn_connect -grid {{grid}} -layers {{{a} {b}}}" for a, b in pdn.connects or []]
        pdn_tcl = os.path.join(w, "cfg", "pdn.tcl")
        with open(pdn_tcl, "w") as f:
            f.write("# SiPath's grid for this design (rails, straps, no ring)\n" + "\n".join(lines) + "\n")
        mk.append(f"export PDN_TCL = {pdn_tcl}")
    with open(os.path.join(w, "cfg", "config.mk"), "w") as f:
        f.write("# generated by bench/orfs_compare.py from " + os.path.relpath(path, ROOT) + "\n"
                + "\n".join(mk) + "\n")
    flow = [dict(s) for s in spec]
    if pdk.signoff and not any(s.get("fn", s["step"]) == "signoff" for s in flow):  # IHP: no signoff decks
        flow.append({"step": "signoff", "variants": [{"fail_on_violation": False}]})
    return d.name, {"top": d.top, "sdc": d.sdc, "platform": platform, "pdk": d.pdk, "design_file": os.path.abspath(path),
                    "flow": flow, "lib": pdk.typical().liberty, "typical": pdk.typical().name,
                    "drc_deck": (pdk.signoff or {}).get("drc_deck"), "top_layer": top_layer}


def run_orfs_config(w: str, d: str) -> dict:
    """ORFS on a generated config.mk (a SiPath design)."""
    out = os.path.join(w, "orfs")
    subprocess.run(["rm", "-rf", out])  # make would reuse an earlier run's results
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    rc = docker(["bash", "-c", f"cd {ORFS} && make DESIGN_CONFIG={w}/cfg/config.mk WORK_HOME={out} LEC_CHECK=0"],
                sorted({BENCH, os.path.join(ROOT, "sipath", "examples")}), os.path.join(out, "make.log"))
    with open(os.path.join(out, "time.json"), "w") as f:
        json.dump({"exit": rc, "seconds": round(time.time() - t0, 1)}, f)


def run_sipath_design(w: str, cfg: dict) -> dict:
    """SiPath on the design's own file and flow (signoff appended)."""
    out = os.path.join(w, "sipath")
    os.makedirs(out, exist_ok=True)
    code = ("import json, sys; from sipath import designfile; "
            "d, _ = designfile.load(sys.argv[1]); "
            "designfile.run(d, sys.argv[2], flow_steps=json.loads(sys.argv[3]))")
    subprocess.run(["rm", "-rf", os.path.join(out, "run")])
    t0 = time.time()
    with open(os.path.join(out, "run.log"), "w") as f:
        rc = subprocess.run([sys.executable, "-c", code, cfg["design_file"], os.path.join(out, "run"),
                             json.dumps(cfg["flow"])], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT).returncode
    with open(os.path.join(out, "time.json"), "w") as f:
        json.dump({"exit": rc, "seconds": round(time.time() - t0, 1)}, f)


def orfs_result(w: str, d: str, platform: str = "sky130hd") -> dict:
    """An ORFS run's files (and its exit/wall time, from orfs/time.json)."""
    out = os.path.join(w, "orfs")
    res = os.path.join(out, "results", platform, d, "base")
    r = {"odb": os.path.join(res, "6_final.odb"), "spef": os.path.join(res, "6_final.spef"),
         "def": os.path.join(res, "6_final.def"),
         "gds": os.path.join(res, "6_final.gds"),
         "drt_rpt": os.path.join(out, "reports", platform, d, "base", "5_route_drc.rpt")}
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


def sipath_result(w: str, tag: str = "sipath", typical: str = "tt_nom") -> dict:
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
        if not so:  # no signoff (IHP): the extracted checkpoint; its DEF is written by the measurement
            so = {"checkpoint": ex.get("checkpoint"), "outputs": {}}
        r.update(odb=so.get("checkpoint"), spef=(ex.get("outputs", {}).get("spef_corners") or {}).get(typical),
                 gds=so.get("outputs", {}).get("gds"), drt_drc=atts.get("route_digital", {}).get("metrics", {}).get("drc"))
        r["def"] = so.get("outputs", {}).get("def_")
        r["steps"] = {a["step"]: round(a.get("seconds") or 0, 1) for a in g["attempts"] if a.get("status") == "selected"}
        r["failed"] = [f"{a['id']}: {(a.get('error') or '').strip().splitlines()[-1:]}" for a in g["attempts"]
                       if a.get("status") == "failed"]
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
{write_def}
"""


def measure(r: dict, cfg: dict, lib: str, tag: str, w: str) -> dict:
    from sipath.signoff import parse_lyrdb
    out = {}
    if not (r.get("odb") and os.path.isfile(r["odb"]) and r.get("spef") and os.path.isfile(r["spef"])):
        return {"error": "no final database/SPEF"}
    script = os.path.join(w, f"measure_{tag}.tcl")
    if not (r.get("def") and os.path.isfile(r["def"])):  # e.g. no signoff step: the DEF from the checkpoint
        r["def"] = os.path.join(w, f"final_{tag}.def")
        wd = f"write_def {r['def']}"
    else:
        wd = ""
    with open(script, "w") as f:
        f.write(MEASURE.format(odb=r["odb"], lib=lib, sdc=cfg["sdc"], spef=r["spef"], write_def=wd))
    log = os.path.join(w, f"measure_{tag}.log")
    mounts = sorted({BENCH, os.path.dirname(lib), os.path.dirname(r["odb"]), os.path.dirname(r["spef"]),
                     os.path.dirname(cfg["sdc"])})
    mounts = [m for m in mounts if os.path.exists(m)]  # an image path (IHP's platform files) is not mounted over
    docker([f"{ORFS}/../tools/install/OpenROAD/bin/openroad", "-no_splash", "-no_init", "-exit", script], mounts, log)
    m = re.search(r"BENCH (\S+) (\S+) (\d+) (\S+)", open(log).read())
    if m:
        out.update(wns_ns=round(float(m.group(1)), 3), tns_ns=round(float(m.group(2)), 3),
                   cells=int(m.group(3)), cell_area_um2=round(float(m.group(4)), 1))
    else:
        out["error"] = f"measurement failed ({log})"
    out["wirelength_um"] = def_wirelength(r.get("def"))
    # KLayout DRC with the PDK deck on the flow's GDS (when SiPath has a deck for the PDK)
    from sipath.pdk import load
    deck = cfg["drc_deck"] if "drc_deck" in cfg else load("sky130A").signoff["drc_deck"]
    gds = r.get("gds")
    if deck and gds and os.path.isfile(gds):
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


def main_sipath_designs(paths: list[str], argv) -> list:
    rows = []
    for path in paths:
        d, cfg = sipath_design(path)
        w = os.path.join(BENCH, d)
        if "--skip-orfs" not in argv:
            run_orfs_config(w, d)
        if "--skip-sipath" not in argv:
            run_sipath_design(w, cfg)
        res = {"orfs": orfs_result(w, d, cfg["platform"]), "sipath": sipath_result(w, "sipath", cfg["typical"])}
        res["orfs"]["drt_drc"] = drt_count(res["orfs"].get("drt_rpt", ""))
        for tag in ("orfs", "sipath"):
            res[tag]["measured"] = measure(res[tag], cfg, cfg["lib"], tag, w)
        with open(os.path.join(w, "compare.json"), "w") as f:
            json.dump({"config": cfg, **res}, f, indent=1)
        rows.append((d, cfg, res))
    return rows


def main(argv):
    files = [a for a in argv if not a.startswith("-") and a.endswith((".yaml", ".yml", ".json"))]
    designs = [a for a in argv if not a.startswith("-") and a not in files] or ([] if files else ["gcd"])
    from sipath.pdk import load
    lib = [c.liberty for c in load("sky130A").analysis_corners() if c.name == "tt_nom"][0]
    rows = main_sipath_designs(files, argv) if files else []
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
            if r.get("exit") or r.get("failed") or m.get("error"):
                label += f" (failed: {(r.get('failed') or [m.get('error') or 'exit %s' % r.get('exit')])[0][:80]})"
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
