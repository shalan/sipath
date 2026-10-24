"""Writes notebooks/sipath_tutorial.ipynb: every recipe of docs/examples.md, runnable.

    python3 notebooks/build_tutorial.py

The notebook runs in Google Colab (the locked ORFS image's tools unpacked by
``sipath install-tools``, no Docker) and in GitHub Codespaces or any Linux
machine with Docker (the locked images, as the CI runs them). Edit the cells
here, not in the notebook; a unit test checks that the two agree.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sipath_tutorial.ipynb")
REPO = "shalan/sipath"
COLAB = f"https://colab.research.google.com/github/{REPO}/blob/main/notebooks/sipath_tutorial.ipynb"
CODESPACES = f"https://codespaces.new/{REPO}?quickstart=1"
DOCS = f"https://github.com/{REPO}/blob/main/docs"

CELLS: list[tuple[str, str]] = []


def md(text: str):
    CELLS.append(("markdown", text.strip("\n")))


def code(text: str):
    CELLS.append(("code", text.strip("\n")))


def recipe(n: int, title: str, anchor: str, text: str):
    md(f"## {n}. {title}\n\n{text.strip()}\n\n([recipe {n}]({DOCS}/examples.md#{anchor}))")


# ----------------------------------------------------------------------------- intro
md(f"""
# SiPath tutorial: every example flow, runnable

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)]({COLAB})
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)]({CODESPACES})

[SiPath](https://github.com/{REPO}) is flow infrastructure for physical design: a
design file in YAML says which steps run, with which parameter variants and
scores, and SiPath runs them as an attempt graph on OpenROAD, Yosys and
KLayout. Its reference flow takes RTL to a signed-off layout on Sky130. This
notebook runs the [example flows]({DOCS}/examples.md) one after another, on
small designs.

**Where it runs**
- **Google Colab** (the badge above): the setup unpacks the locked ORFS
  image's tools without Docker (`sipath install-tools`): the same OpenROAD,
  Yosys and KLayout builds the CI uses. The free tier has 2 CPUs, so runs take
  a few times longer than on a workstation. The OpenFrame precheck (recipe 8)
  needs Docker and is skipped.
- **GitHub Codespaces** (the second badge) or any Linux machine with Docker:
  the setup pulls the locked images; everything runs, the precheck included
  once its image is built.

Run the setup cells first, then any recipe; recipes 9 to 14 reuse the run of
recipe 1. Each run writes to `runs/<name>`; `report.html` in a run directory
shows every step, its attempts and metrics. The runs use `--resume`, so
running a cell again reuses what already finished.

**How long** (measured in an Ubuntu 22.04 container limited to 2 CPUs, like
free Colab, on the unpacked tools; not on Colab itself): the tools take about
5 minutes to download and unpack, the PDK a few more; the recipes then take
about 13 minutes together, from seconds (synthesis only) to about 3 minutes
(recipe 15's sweeps). The optional cells (the whole `mcu8` die, the OpenFrame
chip) take much longer.
""")

# ----------------------------------------------------------------------------- setup
md("## Setup\n\nThe repository, then the tools, then the Sky130 PDK (the open_pdks release SiPath locks).")
code("""
import os, sys, shutil, subprocess
IN_COLAB = "google.colab" in sys.modules
if IN_COLAB:
    if not os.path.isdir("/content/sipath"):
        subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/shalan/sipath", "/content/sipath"],
                       check=True)
    os.chdir("/content/sipath")
else:  # Codespaces or a local clone: the repository root
    while not os.path.isfile("pyproject.toml") and os.getcwd() != "/":
        os.chdir("..")
sys.path.insert(0, os.getcwd())  # import sipath from the clone in this kernel too
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", ".", "ciel", "pandas"], check=True)
print("SiPath in", os.getcwd())
""")
code("""
# Tools. With Docker: the locked images (as the CI). Without (Colab): the locked ORFS image's
# OpenROAD, Yosys and KLayout, downloaded and unpacked by `sipath install-tools` (1.6 GB download, 4 GB on disk).
import json
DOCKER = (shutil.which("docker") is not None and not os.environ.get("SIPATH_TUTORIAL_NATIVE")  # CI: the native path
          and subprocess.run(["docker", "info"], capture_output=True).returncode == 0)
if DOCKER:
    ref = json.load(open("sipath/toolchain.json"))["images"]["orfs"]["ref"]
    !docker pull -q --platform linux/amd64 {ref}
else:
    !python3 -m sipath install-tools
    root = os.path.expanduser("~/.cache/sipath/orfs-root")
    os.environ["PATH"] = f"{root}/sipath-bin:" + os.environ["PATH"]
    os.environ["SIPATH_PLATFORMS"] = f"{root}/OpenROAD-flow-scripts/flow/platforms"
print("runtime:", "docker" if DOCKER else "native (unpacked image)")
""")
code("""
# The Sky130 PDK through ciel (about 1 GB); retried, since a large download can time out
# (a failed try is not this cell's failure: only the last one counts)
for attempt in range(3):
    r = subprocess.run([sys.executable, "-m", "sipath", "install-pdk"], capture_output=True, text=True)
    print((r.stdout + r.stderr)[-2000:])
    if r.returncode == 0:
        break
    print(f"install-pdk failed (try {attempt + 1} of 3, exit {r.returncode})")
else:
    raise RuntimeError("install-pdk failed three times")
""")
code("""
!python3 -m sipath validate
""")
md("Helpers used below: design files derived from the examples, attempt tables, layout pictures.")
code('''
import base64, glob, json
import pandas as pd
import yaml
from IPython.display import HTML, Markdown, display
from sipath import viz

EX = "sipath/examples"


def derive(base, name, **changes):
    """A design file next to the examples (its relative paths still hold): ``base`` with ``changes``."""
    with open(f"{EX}/{base}.yaml") as f:
        d = yaml.safe_load(f)
    d.update(changes)
    path = f"{EX}/tut_{name}.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(d, f, sort_keys=False)
    return path


def attempts(workdir, steps=None, metrics=()):
    """Every attempt of a run (or of some steps): status, parameters, wall time, chosen metrics."""
    with open(f"{workdir}/graph.json") as f:
        g = json.load(f)
    rows = [{"attempt": a["id"], "status": a["status"], "seconds": round(a.get("seconds") or 0, 1),
             "params": json.dumps(a.get("params") or {}),
             **{k: (a.get("metrics") or {}).get(k) for k in metrics}}
            for a in g["attempts"] if not steps or a["step"] in steps]
    return pd.DataFrame(rows)


def outputs(workdir, attempt):
    with open(f"{workdir}/graph.json") as f:
        return next(a for a in json.load(f)["attempts"] if a["id"] == attempt)["outputs"]


def pictures(workdir, *which, width=300):
    """Render steps or attempts of a run (``sipath render``) and show them side by side."""
    cells = []
    for w in which:
        try:
            found = viz.checkpoints(workdir, w)
        except ValueError as e:  # e.g. a failed attempt: no layout to draw
            print(f"{w}: {e}")
            continue
        for aid, _ in found:
            png = f"{workdir}/_pictures/{aid}.png"
            if not os.path.isfile(png):
                subprocess.run([sys.executable, "-m", "sipath", "render", workdir, aid], check=True,
                               capture_output=True)
            b64 = base64.b64encode(open(png, "rb").read()).decode()
            cells.append(f'<figure style="display:inline-block;margin:4px"><img width="{width}" '
                         f'src="data:image/png;base64,{b64}"><figcaption>{aid}</figcaption></figure>')
    display(HTML("".join(cells)))


def show_md(path):
    display(Markdown(open(path).read()))
''')

# ----------------------------------------------------------------------------- recipes
recipe(1, "RTL to a signed-off layout", "1-rtl-to-a-signed-off-layout", """
`sipath/examples/gcd8.yaml`: RTL, constraints and a utilization; the flow runs
synthesis, floorplan, power grid, placement, CTS, routing, fill, extraction and
signoff (KLayout DRC and LVS, antennas, density, nine-corner STA).
""")
code("""
!python3 -m sipath run sipath/examples/gcd8.yaml runs/gcd8 --resume
!python3 -m sipath report runs/gcd8
""")
code("""
attempts("runs/gcd8", metrics=["insts", "wns", "drc", "wirelength_um"])
""")
code("""
so = json.load(open(outputs("runs/gcd8", "signoff.0")["signoff"]))
print("signoff pass:", so["pass"], "| DRC:", so["drc"]["count"], "| LVS match:", so["lvs"]["match"])
print({c: round(v["wns"], 3) for c, v in so["timing"]["corners"].items()}, "(setup WNS per corner, ns)")
pictures("runs/gcd8", "floorplan", "place", "cts", "route_digital")
""")

recipe(2, "A first cut at PPA, then the clock", "2-a-first-cut-at-ppa-then-the-clock", """
`--explore` runs to global routing at the typical corner, with no signoff, and
suggests a clock period.
""")
code("""
!python3 -m sipath run sipath/examples/gcd8.yaml runs/gcd8_explore --explore --resume
show_md(glob.glob("runs/gcd8_explore/*/0/ppa.md")[0])
""")

recipe(3, "Synthesis: recipes and the fallback", "3-synthesis-recipes-front-ends-the-fallback", """
Each synth attempt maps with a set of ABC recipes and keeps the smallest netlist
that meets timing; two attempts with different recipe sets and fallbacks,
picked by `synth_select`. Synthesis only: the flow stops after it.
""")
code("""
path = derive("gcd8", "synth", flow=[
    {"step": "synth", "variants": [{"abc_sweep": "balanced", "synth_fallback": "least_area"}, {"abc_sweep": "delay"}],
     "score": "synth_select", "parallel": 2}])
!python3 -m sipath run {path} runs/gcd8_synth --resume
for a in ("synth.0", "synth.1"):
    print(a, outputs("runs/gcd8_synth", a)["abc_sweep"]["why"])
attempts("runs/gcd8_synth", metrics=["synth_cells", "synth_area_um2", "synth_wns_ns", "abc_recipe"])
""")

recipe(4, "Placement variants chosen by a score", "4-placement-variants-chosen-by-a-score", """
Three densities placed at once; `routability_then_wns` ranks by global-route
overflow, then setup slack. The flow stops after placement.
""")
code("""
path = derive("gcd8", "place", flow=[
    {"step": "synth"}, {"step": "floorplan"}, {"step": "physical"},
    {"step": "place", "variants": [{"density": 0.5}, {"density": 0.6}, {"density": 0.7, "routability_driven": False}],
     "score": "routability_then_wns", "parallel": 3}])
!python3 -m sipath run {path} runs/gcd8_place --resume
display(attempts("runs/gcd8_place", ["place"], ["grt_overflow", "wns", "hpwl_um"]))
pictures("runs/gcd8_place", "place.0", "place.1", "place.2")
""")

recipe(5, "Hard macros: comparing placers", "5-hard-macros-comparing-placers", """
`sram8`: eight Sky130 1 KB SRAMs and glue logic. Random placements under the
rules (halo, channels, pin clearance) and, when PyTorch is available (it is on
Colab), the CD-LNS placer; `macro_quick` scores each by a quick place and route.
A random seed can find no legal placement: that attempt fails and the others
still compete.
""")
code("""
variants = [{"method": "random", "seed": 1}, {"method": "random", "seed": 2}]
try:
    import torch  # the cdlns placer runs in a Python with PyTorch: this one
    os.environ["SIPATH_PLACER_PYTHON"] = sys.executable
    variants.append({"method": "cdlns", "placer_seconds": 60})
except ImportError:
    print("no PyTorch here: cdlns skipped (python3 -m sipath setup-placer installs it)")
path = derive("sram8", "macros", flow=[{"step": "synth"}, {"step": "floorplan"},
                                       {"step": "macros", "variants": variants, "score": "macro_quick",
                                        "parallel": len(variants)}])
!python3 -m sipath run {path} runs/sram8_macros --resume
display(attempts("runs/sram8_macros", ["macros"], ["hpwl_um", "grt_overflow"]))
pictures("runs/sram8_macros", *[f"macros.{i}" for i in range(len(variants))])
""")

recipe(6, "Analog nets: guard, shields, coupling limits", "6-analog-nets-guard-shields-coupling-limits", """
`dp8` routes its analog nets first with a wide rule, shields `ana_in`, and its
extraction reports each analog net's coupling to digital nets against limits.
""")
code("""
!python3 -m sipath run sipath/examples/dp8.yaml runs/dp8 --resume
print(outputs("runs/dp8", "route_analog.0")["analog_shield"])
show_md(glob.glob("runs/dp8/extract/0/analog_coupling.md")[0])
pictures("runs/dp8", "route_digital")
""")

recipe(7, "A full die with a pad ring", "7-a-full-die-with-a-pad-ring", """
`mcu8` is a whole die: 39 pads placed by rules, an SRAM, an ADC and a DAC. The
first cell runs the floorplan, the macro placement and the power grid (a few
minutes); the second, optional, runs it to signoff (much longer on Colab).
""")
code("""
path = derive("mcu8", "ring", flow=[{"step": "floorplan"},
                                    {"step": "macros", "variants": [{"method": "random", "seed": 1, "samples": 40}]},
                                    {"step": "physical"}])
!python3 -m sipath run {path} runs/mcu8_ring --resume
pictures("runs/mcu8_ring", "physical", width=600)
""")
code("""
# optional, long: the whole die to signoff
# !python3 -m sipath run mcu8 runs/mcu8 --resume
""")

recipe(8, "An OpenFrame chip, prechecked, with its seal ring", "8-an-openframe-chip-prechecked-with-its-seal-ring", """
A template writes a project for ChipFoundry's OpenFrame; the harness makes the
design the frame's wrapper. ChipFoundry's precheck runs in its own image, so it
needs Docker (Codespaces): build the image once (several minutes), then run.
""")
code("""
!python3 -m sipath new runs/myproj --template openframe --set inputs=8 --set outputs=6 --force
!ls runs/myproj
""")
code("""
# Docker only (Codespaces): the Magic image, then the precheck image on it
if DOCKER:
    !docker build -q --platform linux/amd64 -t sipath/orfs-magic:8.3.685 docker/magic
    !docker build -q --platform linux/amd64 -t sipath/precheck:1.3.7 docker/precheck
else:
    print("no Docker here: the precheck is skipped")
""")
code("""
# optional, long: openframe_gcd8 to signoff, the precheck (with Docker) and the frame with its seal ring
checks = ["drc", "density", "antenna", "setup", "hold", "drv", "annotation", "unconstrained", "harness"]
flow = [{"step": s} for s in ("synth", "floorplan", "physical", "place", "cts", "route_analog", "route_digital",
                              "route_repair", "fill", "extract")]
flow += [{"step": "signoff", "variants": [{"checks": checks, "require": [c for c in checks if c != "drv"]}]}]
flow += [{"step": "precheck"}] if DOCKER else []
flow += [{"step": "integrate", "variants": [{"seal_ring": True}]}]
path = derive("openframe_gcd8", "openframe", flow=flow)
# !python3 -m sipath run {path} runs/openframe_gcd8 --resume
""")

recipe(9, "Hardened macros in a parent design", "9-hardened-macros-in-a-parent-design", """
`harden` turns a signed-off run into macro views (abstract LEF, Liberty per
corner, GDS, netlists); given recipe 1's run and `--resume`, nothing re-runs.
`gcd8_pair` then places two of them in a parent and signs it off (DRC sees
their layout, LVS their netlist).
""")
code("""
!python3 -m sipath harden sipath/examples/gcd8.yaml sipath/examples/hardened/gcd8 runs/gcd8 --resume
!ls sipath/examples/hardened/gcd8
!python3 -m sipath run sipath/examples/gcd8_pair.yaml runs/gcd8_pair --resume
pictures("runs/gcd8_pair", "route_digital", width=500)
""")

recipe(10, "Signoff checks one by one, and your own step", "10-signoff-checks-one-by-one-and-your-own-step", """
Each signoff check is also a step, and a step can be your own function, named
by import path. With `--resume` on recipe 1's run only the new steps run (recipe
11): the finished attempts are kept.
""")
code('''
os.makedirs("tutorial/mypkg", exist_ok=True)
open("tutorial/mypkg/__init__.py", "w").close()
with open("tutorial/mypkg/checks.py", "w") as f:
    f.write("""
from sipath.params import Param


def inst_count(ctx):
    \\"\\"\\"A step: counts instances (OpenROAD session in ctx.session) and checks a floor.\\"\\"\\"
    n = int(ctx.session.value("llength [[ord::get_db_block] getInsts]"))
    ctx.metrics["user_insts"] = n
    if n < ctx.params["at_least"]:
        raise RuntimeError(f"only {n} instances")


inst_count.params = [Param("at_least", "int", 1, "fewest instances accepted", min=0)]
""")
os.environ["PYTHONPATH"] = os.path.abspath("tutorial") + os.pathsep + os.environ.get("PYTHONPATH", "")
''')
code("""
base = [s for s in yaml.safe_load(open(f"{EX}/gcd8.yaml"))["flow"] if s["step"] != "signoff"]
path = derive("gcd8", "checks", flow=base + [
    {"step": "signoff_quick", "fn": "signoff", "variants": [{"checks": ["drc", "setup", "hold"],
                                                              "require": ["drc", "setup", "hold"]}]},
    {"step": "lvs"}, {"step": "timing", "variants": [{"require": ["setup", "hold"]}]},
    {"step": "mypkg.checks:inst_count", "variants": [{"at_least": 100}]}])
!python3 -m sipath run {path} runs/gcd8 --resume
attempts("runs/gcd8", metrics=["drc_signoff", "lvs_match", "sta_wns_ss_max", "user_insts"]).tail(5)
""")

recipe(11, "Resume, and a step under another name", "11-resume-and-a-step-under-another-name", """
Recipe 10 already resumed a run and ran `signoff` again as `signoff_quick`
(`fn: signoff`). The attempt table shows which attempts were recorded before
(`resumed`) and which ran now.
""")
code("""
g = json.load(open("runs/gcd8/graph.json"))
pd.DataFrame([{"attempt": a["id"], "resumed": a.get("resumed"), "seconds": round(a.get("seconds") or 0, 1)}
              for a in g["attempts"]])
""")

recipe(12, "An ECO from Python", "12-an-eco-from-python", """
An ECO continues from the signed-off head of a run (recipe 6's dp8; recipe 1's
gcd8 already continued into recipe 9's views): resize one gate.
The change is checked for equivalence, only touched nets are re-routed, and the
ECO is rejected if setup or hold slack drops by more than `max_slack_loss`
(0.05 ns by default); if it is, the cell tries again with a looser bound.
""")
code("""
import re
from sipath import designfile
from sipath.scheduler import StepFailed

flow = designfile.run("sipath/examples/dp8.yaml", "runs/dp8", resume=True)  # nothing re-runs
head = flow.steps["signoff"].selected
with open(head.outputs["def_"]) as f:  # a small AND gate of the routed design
    inst = re.search(r"- (\\S+) sky130_fd_sc_hd__and2_1 ", f.read()).group(1)
ops = [{"op": "resize", "inst": inst, "master": "sky130_fd_sc_hd__and2_2"}]
try:
    a = flow.eco("eco1", head, ops).selected
except StepFailed as e:
    print("rejected:", str(e).splitlines()[-1].strip())
    a = flow.eco("eco2", head, ops, max_slack_loss=0.5).selected
print(f"{inst} -> and2_2: {a.id} {a.status}; WNS {a.metrics['wns']:.3f} ns, DRC {a.metrics.get('drc')}, "
      f"equivalence {a.outputs.get('equivalence')}")
""")

recipe(13, "Driving a run from an agent (MCP)", "13-driving-a-run-from-an-agent-mcp", """
`python3 -m sipath mcp` serves SiPath's tools over the Model Context Protocol;
an agent (Claude, for one) calls them. Here a few lines of Python play the
agent: open a run, run steps, sweep placement in the background, read the
attempts, render the winner.
""")
code('''
import time


class Mcp:
    """A minimal MCP client: JSON-RPC over the server's stdin and stdout."""

    def __init__(self):
        self.p = subprocess.Popen([sys.executable, "-m", "sipath", "mcp"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "notebook"}})

    def rpc(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\\n")
        self.p.stdin.flush()
        while True:  # skip notifications (progress) until the reply
            msg = json.loads(self.p.stdout.readline())
            if msg.get("id") == self.n:
                return msg

    def call(self, tool, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})["result"]
        if r["isError"]:
            raise RuntimeError(r["content"][0]["text"])
        return r.get("structuredContent")


agent = Mcp()
R = agent.call("open_run", design="gcd8", workdir="runs/agent_gcd8")["run_id"]
parent = None
for step in ("synth", "floorplan", "physical"):
    parent = agent.call("run_step", run_id=R, step=step, **({"parent": parent} if parent else {}))["selected"]
    print(step, "->", parent)
job = agent.call("run_step", run_id=R, step="place", parent=parent, variants=[{"density": 0.5}, {"density": 0.7}],
                 score="routability_then_wns", parallel=2, background=True)
while (st := agent.call("job_status", job_id=job["job_id"]))["status"] == "running":
    time.sleep(5)
for a in agent.call("list_attempts", run_id=R, step="place")["attempts"]:
    print(a["id"], a["status"], a["params"], {k: a["metrics"].get(k) for k in ("grt_overflow", "wns", "hpwl_um")})
agent.call("render_checkpoint", run_id=R, attempt=st["result"]["selected"])
agent.p.stdin.close()
pictures("runs/agent_gcd8", st["result"]["selected"])
''')

recipe(14, "Regression limits", "14-regression-limits-and-repeated-runs", """
Freeze a good run's results as limits, then check runs against them. The
committed limits come from a 12-thread workstation, so wall time is ignored
here (`SIPATH_LIMITS_IGNORE`), as the CI does.
""")
code("""
!python3 -m sipath limits runs/gcd8 gcd8 --suggest
!SIPATH_LIMITS_IGNORE=max_runtime_s python3 -m sipath limits runs/gcd8 gcd8
""")

recipe(15, "Design-space exploration: sweeps at several steps, in parallel",
       "15-design-space-exploration-sweeps-at-several-steps-in-parallel", """
`gcd8_explore` sweeps synthesis (2 recipe sets), placement (3 densities) and
routing (2 repair settings); each step's score picks the attempt the next step
continues from. First the quick look, then the full flow.
""")
code("""
!python3 -m sipath run sipath/examples/gcd8_explore.yaml runs/gcd8_x --explore --resume
!python3 -m sipath run sipath/examples/gcd8_explore.yaml runs/gcd8_full --resume
attempts("runs/gcd8_full", ["synth", "place", "route_digital"], ["synth_wns_ns", "wns", "hpwl_um", "drc"])
""")
code("""
pictures("runs/gcd8_full", "place.0", "place.1", "place.2")
""")
recipe(16, "Heatmaps and adaptive rounds: hotspots into the next placement",
       "16-heatmaps-and-adaptive-rounds-hotspots-into-the-next-placement", """
A crowded gcd8 (65% core utilization, placement density 0.8). Placement
collects heatmaps after its checkpoint is saved; `adapt` turns the density
hotspots into partial blockages for the next attempts, and the step selects
again by `hotspots_then_wns`. The flow stops after placement.
""")
code("""
path = derive("gcd8", "adapt", core_utilization=0.65, flow=[
    {"step": "synth"}, {"step": "floorplan"}, {"step": "physical"},
    {"step": "place", "variants": [{"density": 0.8}], "adapt": {"rounds": 2}, "parallel": 2}])
!python3 -m sipath run {path} runs/gcd8_adapt --resume
g = json.load(open("runs/gcd8_adapt/graph.json"))
print("rounds:", g["steps"]["place"]["rounds"])
pd.DataFrame([{"attempt": a["id"], "status": a["status"], "from": a.get("adapted_from"),
               "blockages": len(a["params"].get("blockages") or []),
               "hot share": a["metrics"].get("heatmap_placement_hot_share"), "wns": a["metrics"].get("wns"),
               "reason": (a.get("adapt_reason") or "")[:90]} for a in g["attempts"] if a["step"] == "place"])
""")
code("""
sel = g["steps"]["place"]["selected"]
show = lambda a: display(HTML(open(outputs("runs/gcd8_adapt", a)["heatmaps"]["placement"]["csv"][:-4] + ".svg").read()))
!python3 -m sipath report runs/gcd8_adapt
print("placement density, first attempt then the selected one:")
show("place.0"); show(sel)
pictures("runs/gcd8_adapt", "place.0", sel)
""")

md("""
## Next

- [Design files]({docs}/design-files.md): every key, the examples.
- [The flow]({docs}/flow.md): what each step does.
- [Agents]({docs}/agents.md): the MCP tools.
- Any run's `report.html` (in the Colab file browser, or downloaded) shows its steps, attempts and pictures.
""".replace("{docs}", DOCS))


def notebook() -> dict:
    def cell(i, kind, src):
        lines = src.splitlines(keepends=True)
        c = {"cell_type": kind, "id": f"cell-{i:02d}", "metadata": {}, "source": lines}
        if kind == "code":
            c.update(execution_count=None, outputs=[])
        return c
    return {"cells": [cell(i, k, s) for i, (k, s) in enumerate(CELLS)],
            "metadata": {"colab": {"provenance": [], "toc_visible": True},
                         "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                         "language_info": {"name": "python"}},
            "nbformat": 4, "nbformat_minor": 5}


def text() -> str:
    return json.dumps(notebook(), indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    with open(OUT, "w") as f:
        f.write(text())
    print(OUT)
