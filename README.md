<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/sipath-logo-dark.svg">
    <img src="docs/logo/sipath-logo.svg" alt="SiPath" width="420">
  </picture>
</p>

# SiPath — the path to silicon, steered by AI

[![unit](https://github.com/shalan/sipath/actions/workflows/unit.yml/badge.svg)](https://github.com/shalan/sipath/actions/workflows/unit.yml)
[![live](https://github.com/shalan/sipath/actions/workflows/live.yml/badge.svg)](https://github.com/shalan/sipath/actions/workflows/live.yml)
[![Open the tutorial in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/shalan/sipath/blob/main/notebooks/sipath_tutorial.ipynb)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/shalan/sipath?quickstart=1)

SiPath is **flow infrastructure for physical design**, with a **reference
flow** built on it. A flow is data: a design file in YAML (or JSON) lists the
steps, the parameter variants of each, and the scores that choose between the
attempts. The infrastructure runs it:

- an **attempt graph**: every step runs as one or more attempts (parallel
  variants, retries, adaptive rounds), every attempt is kept, a named score
  selects the one the next step continues from; runs resume and cancel;
- a **step library** and **tool adapters** (OpenROAD in live sessions, Yosys,
  KLayout, Magic, Netgen), extended by plugins: your own steps, scores,
  signoff checks, harnesses and templates;
- **signoff** as the judge: a failed required check fails the run;
- an **MCP server**, so an AI agent drives the same runs: it opens runs,
  launches sweeps, reads metrics, heatmaps and reports, and runs ECOs.

The **reference flow** is the step chain a design file gets when it names no
`flow:`. It takes RTL or a gate-level netlist to a **routed DEF, SPEF and a
signed-off GDS**. PDKs are data too: Sky130 from open_pdks is the most
complete description, and IHP SG13G2 is the second. A person drives the same
steps from the CLI or Python.

## Contents

[Quick start](#quick-start) · [Features](#features) ·
[The reference flow](#the-reference-flow) · [EDA tools](#eda-tools) ·
[Results](#results) · [Known limits](#known-limits) ·
[Documentation](#documentation) · [Repository](#repository) ·
[License](#license)

## Quick start

To try it without installing anything, open the
[tutorial notebook](notebooks/sipath_tutorial.ipynb) in Colab or Codespaces
(the badges above): it runs the example flows (the full mcu8 and OpenFrame runs,
an hour each, are left commented out).

SiPath needs Python 3.10+ and either Docker or, on Linux x86_64,
`python3 -m sipath install-tools`, which unpacks the locked image's OpenROAD,
Yosys and KLayout; runs then use those without Docker (`runtime: auto`;
`SIPATH_RUNTIME=docker` keeps the image; an `openroad` already on `PATH`, or
`$SIPATH_OPENROAD`, comes first). The tool images and the open_pdks release
are locked in `sipath/toolchain.json`.

```bash
python3 -m pip install -e . ciel               # the sipath package and console script; ciel installs PDKs
python3 -m sipath install-pdk                  # the locked open_pdks release (sky130) through ciel
docker pull openroad/orfs@sha256:751a77afcade9882b51427e6d9d079b8e270e7a8f4aa66df2d0659457d1c29fd
                                               # or, on Linux x86_64 without Docker: python3 -m sipath install-tools
python3 -m sipath doctor                       # ready? what a run would use, and the fix for anything missing
```

One setup path per runtime (macOS with Docker, Linux native, Colab), with the
Magic and precheck images, is in [PDKs and runtimes](docs/pdks-and-runtimes.md#setup).

Run a design (more in [example flows](docs/examples.md)):

```bash
python3 -m sipath run sipath/examples/gcd8.yaml               # RTL to signoff (soft macro, sky130A)
python3 -m sipath run sipath/examples/gcd8.yaml runs/x --explore   # first-cut PPA in a minute
python3 -m sipath run mcu8                                     # a die: pad ring, hard macros, analog
python3 -m sipath run mcu8 runs/mcu8 --resume                  # continue an interrupted run (--fresh: start over)
python3 -m sipath report runs/gcd8 --render                    # the run's report page
```

`run` exits 0 when no required check failed, 1 when the run or a required
check failed, 3 when a check is inconclusive or a budget or cancel stopped it;
a workdir that already holds a run needs `--resume` or `--fresh`.

Other commands:

| command | what it does |
|---|---|
| `sipath doctor` | is this machine ready, what a run would use, and the fix for anything missing |
| `sipath run <design> --plan` | what a run would do before it runs: attempts, how many at once, threads, budgets |
| `sipath pdks`, `sipath templates` | PDK descriptions; design templates (`sipath new <dir> --template openframe`) |
| `sipath harden <design> <dir>` | a design's hardened-macro views (LEF, Liberty, GDS, SPEF) |
| `sipath install-tools [dir]` | the locked ORFS image's tools without Docker (Linux x86_64, glibc 2.35+: Colab, Ubuntu 22.04+) |
| `sipath setup-placer` | the Python with CPU PyTorch for the CD-LNS macro placer |
| `sipath render <run> [step]`, `sipath draw` | OpenROAD's layout picture, or a schematic SVG, of a step's checkpoint (`all`: every step); `sipath run ... --snapshots` draws one after every step |
| `sipath lvs <gds> <top> <spice> --pdk <name>` | full-device LVS of a layout with the PDK's own deck |
| `sipath limits <run> <design>` | a run against its regression limits |
| `sipath validate`, `sipath docs`, `sipath schema` | check the shipped examples (your own file: `sipath run <file> --plan`); regenerate the reference; JSON Schema |
| `docker build ... docker/magic`, then `docker build ... docker/precheck` | the Magic image, then the precheck image built on it (ChipFoundry's cf-precheck, Netgen, CVC): the commands are in [PDKs and runtimes](docs/pdks-and-runtimes.md#setup) |
| `sipath mcp` | the MCP server, on stdio |
| `python3 -m unittest sipath.tests.test_unit` | unit tests (no tools, seconds) |
| `python3 -m sipath.tests.run_live -j 4` | live tests: real runs, in parallel |
| `python3 bench/orfs_compare.py gcd aes`, `python3 bench/repeat_compare.py aes` | the benchmark against OpenROAD-flow-scripts: ORFS's run and one SiPath run; SiPath's repeated runs (the tables' SiPath rows) |

## Features

| area | what SiPath does |
|---|---|
| **Inputs** | Design files checked against a schema before anything runs; typed step parameters (variant > design > PDK > default); PDKs as data; images locked by digest and the open_pdks release by commit; plugins, and steps named by import path ([design files](docs/design-files.md)) |
| **Synthesis** | Verilog or SystemVerilog (slang), linted by Yosys and Verilator; ABC with 20 measured recipes at the slow corner, the netlist picked by least area meeting timing; Booth and prefix-adder variants; bounded RTL-to-netlist equivalence ([synthesis](docs/synthesis.md)) |
| **Floorplan and chip** | Soft macro (from a DEF, a die box or a utilization) or full die with a pad ring generated from rules; the OpenFrame harness with the wrapper checked pin by pin, assembled into the frame's chip (optional seal ring); Caravel's wrapper checks only (experimental: no chip assembly, its power stripes not verified) |
| **Hard macros** | Fixed, or placed by a random search with orientation search, OpenROAD's mpl, or the CD-LNS placer from the Macro Placement Challenge 2026; halo, channel, pin-clearance and analog-guard rules; scored by a quick place and route |
| **Power** | Power grid (rings, straps scaled to the design), straps to supply pins the grid cannot reach, 3.3 V analog supplies routed with a wide rule; grid and IR checks |
| **Placement and CTS** | Timing- and routability-driven placement; fences, clusters and structured datapaths (column, array, bit-slice) kept intact through sizing |
| **Analog nets** | Routed first with a non-default rule and frozen; guarded against later digital wiring; optional grounded shields and adjacent-layer keep-away; coupling to digital nets reported and bounded |
| **Routing and repair** | Digital routing with repair and antenna loops; post-route timing repair on extracted parasitics that re-routes only the changed nets (a full re-route, in a fresh attempt, when a patch fails); metal fill ([flow](docs/flow.md)) |
| **Signoff** | KLayout DRC and density, standard-cell LVS, antennas, nine-corner STA, harness conformance; opt-in Magic DRC, XOR and full-device LVS; every check also a step of its own; every run states its execution and verification status apart, with what was not checked ([signoff](docs/signoff.md)) |
| **Tapeout precheck** | ChipFoundry's own precheck (cf-precheck, pinned) on a signed-off OpenFrame design: the project exported in its layout, run in a pinned image with Magic, Netgen and CVC, its results per check ([signoff](docs/signoff.md#tapeout-precheck)) |
| **Exploration** | Variants of a step in parallel, chosen by named scores; resume and cancel; an explore mode that gives PPA and a suggested clock in about a minute; adaptive rounds that turn a placement's hotspots into the next attempts ([flow](docs/flow.md#adaptive-rounds)); ECOs checked for equivalence |
| **Metrics** | Every attempt's metrics in `graph.json`, with power and peak memory per step; `metrics.json` and `metrics.csv` in METRICS2.1 names, as ORFS and LibreLane write them ([flow](docs/flow.md#metrics)) |
| **Heatmaps** | Placement density, RUDY, routing congestion, pin density, power and IR drop after place, CTS, routing and extraction (OpenROAD's own maps, taken after the checkpoint is saved); peak, p95, hot share and the hotspots with the instances or nets inside; in the run report and over MCP ([flow](docs/flow.md#heatmaps)) |
| **Agents** | MCP server: runs, steps, adaptive rounds, ECOs, background jobs with progress, checkpoint queries, heatmaps and pictures ([agents](docs/agents.md)) |
| **Quality** | Regression limits per design; unit and live tests; CI on GitHub Actions (unit tests on every push, live tests nightly) |

## The reference flow

A design file without `flow:` runs this chain. A `flow:` list replaces it:
any order, your own steps, several variants per step and a score to choose.
Every step runs as one or more attempts, in parallel, and the next step
continues from the attempt its score selects. An OpenROAD step's attempt is
its own OpenROAD process; tool steps (synthesis in Yosys, `integrate` in
Magic, `precheck` in its image) run their tools instead, with no OpenROAD
session. Every
attempt is kept in `graph.json`.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/flow-dark.svg">
  <img src="docs/diagrams/flow-light.svg" width="100%" alt="SiPath's flow: a design file, the Python API or an AI agent over MCP drives an attempt graph. The reference chain runs synth, floorplan, macros, physical, place, cts, then route_analog, route_digital, route_repair, fill, extract and signoff. Inside every step, variants run as attempts in parallel, a score selects one and the next step continues from it; others are kept as branches or rejected. Placement can run adaptive rounds, a failed repair patch is retried with a full re-route, and ECOs patch the signed-off result. A run gives its verdict, the layout and reports, hardened views and, for harness designs, precheck and chip integration.">
</picture>

`fill` and `signoff` run when the PDK has fill rules and signoff decks; a
netlist design starts at `floorplan`. Each box is a step with typed
parameters ([reference](docs/reference.md)); [how the flow
works](docs/flow.md) describes them, with the loops: adaptive rounds,
bounded search (`keep`, decided at a later step), retries and ECOs. The
diagram is drawn by `docs/diagrams/flow_diagram.py`.

## EDA tools

Versions are the ones locked in `sipath/toolchain.json` (images pinned by
digest); `python3 -m sipath toolchain --check` compares what is installed.

| tool | version | what SiPath uses it for | from |
|---|---|---|---|
| [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD) (OpenSTA, OpenRCX, PDNSim, RePlAce, TritonCTS, FastRoute, TritonRoute, Resizer, ICeWall) | 26Q3-1867-g84e3ff1eb2 | floorplan to extraction in live sessions; timing at synthesis and nine-corner signoff STA; antenna checks; heatmaps; layout pictures | ORFS image, or `install-tools` |
| [Yosys](https://github.com/YosysHQ/yosys) with ABC and slang | 0.68 | synthesis and the ABC recipe sweep, SystemVerilog through slang, lint, bounded equivalence | ORFS image |
| [Verilator](https://github.com/verilator/verilator) | not locked | RTL lint (`--lint-only -Wall`), when it is found | host, optional |
| [KLayout](https://github.com/KLayout/klayout) | 0.30.7 | GDS stream-out, DRC, density, standard-cell and full-device LVS, XOR, seal ring, chip check | ORFS image |
| [Magic](https://github.com/RTimothyEdwards/magic) | 8.3.685 | chip assembly into a frame (`integrate`); optional Magic DRC and XOR | Magic image (`docker/magic`) |
| [cf-precheck](https://pypi.org/project/cf-precheck/), with [Netgen](https://github.com/RTimothyEdwards/netgen) and [CVC](https://github.com/d-m-bailey/cvc) | 1.3.7 (Netgen 1.5.325, CVC 1.1.7) | ChipFoundry's tapeout precheck on harness designs: LVS (Netgen), OEB and ERC (CVC), XOR, DRC | precheck image (`docker/precheck`) |
| [open_pdks](https://github.com/RTimothyEdwards/open_pdks) via [ciel](https://github.com/fossi-foundation/ciel) | 8afc8346 | the Sky130 PDK: sky130A/B, hd and hs cells, IO, SRAM macros | `python3 -m sipath install-pdk` |
| CD-LNS macro placer (PyTorch) | vendored | the `cdlns` macro placement method | `python3 -m sipath setup-placer` |

## Results

Highlights; every table, its method and its date are in
[benchmarks](docs/benchmarks.md).

Against OpenROAD-flow-scripts on its own reference designs (same RTL, SDC and
utilization; both layouts measured by the same code; WNS at the typical
corner):

<!-- table:orfs_readme -->
| design | flow | WNS (ns) | TNS (ns) | wirelength (µm) | cell area (µm²) | DRC | wall (s) | runs | signoff | SiPath code |
|---|---|---|---|---|---|---|---|---|---|---|
| gcd (1.1 ns) | ORFS | −1.465 | −65.09 | 11,786 | 5,319 | 0 | 321 | 1 | — | — |
| gcd (1.1 ns) | SiPath | −1.083 | −46.62 | 8,061 | 4,975 | 0 | 369 | 1 | failed 1/1: setup | unrecorded |
| aes | ORFS | −0.196 | −0.67 | 588,211 | 124,637 | 0 | 2,724 | 1 | — | — |
| aes | SiPath | +0.233 | 0.00 | 583,437 | 136,273 | 0 | 4,207 | 1 of 2 (DRT-0206) | failed 1/1: drv, setup | unrecorded |
<!-- /table -->

SiPath's wall time includes signoff (DRC, LVS, nine-corner STA), which ORFS's
make does not run. The benchmark runs signoff without failing the run
(`fail_on_violation: false`), so both SiPath runs completed although their
signoff failed: gcd's 1.1 ns clock is out of reach, and aes misses setup at
the slow corners and has 34 slew violations (DRC 0 and LVS match in both).
One of aes's two runs failed in routing (`DRT-0206`); it ran before post-route
repair retried that failure with a full re-route, so it says little about the
flow as it is now. These SiPath runs predate recorded commits (`unrecorded` in
the last column). The table is written from the records in `bench/results/`
([benchmarks](docs/benchmarks.md)).

| benchmark | result |
|---|---|
| SiPath's designs through ORFS (gcd8, dp8, dp8_hs, ihp_add8) | timing met in both flows; SiPath 2–18% less cell area |
| Synthesis, shalan/synth_flow's 16 designs | 12 meet timing with the defaults, 13 with the front-end sweep (synth_flow before repair: 6) |
| Analog coupling, mcu8 `vref_lo` | 31.2 fF from a digital net → 0.09 fF with the guard and shields; signoff clean |
| Macro placement, sram8 (eight SRAMs) | random search beats CD-LNS on the quick placement's HPWL and global-route overflow (574k vs 628k µm, 7 vs 39; HPWL, not routed wirelength) |
| ChipFoundry precheck, OpenFrame example | 12 of 12 checks pass (LVS with Netgen, OEB with CVC, XOR, KLayout DRC); Magic DRC optional |
| Explore mode, gcd8 | 20 s from RTL to a PPA first cut and a suggested clock |

## Known limits

- RTL-to-netlist equivalence is bounded (10 cycles from reset), not a full
  proof, and times out on large designs.
- Full-device LVS with the PDK's deck runs but matches neither dp8 nor the
  ChipFoundry ADC yet; the standard-cell LVS is the gating check.
- No second voltage domain: 3.3 V analog supplies reach macro pins from
  supply pads, but there is no 3.3 V cell region, level shifting or
  voltage-aware check, and 3.3 V supplies from wrapper pins are not routed.
- PDKs: Sky130 has signoff decks; IHP SG13G2 routes to DEF and SPEF without
  them; no gf180mcu yet. No gate-level simulation.
- Seven sky130 HD cells are kept out of synthesis and sizing: the KLayout LVS
  deck cannot pair them.
- A PDK whose slow and fast corner files are not found (the ORFS platform tree
  alone, without the open_pdks release) falls back to the typical corner, and
  signoff then judges timing there only; `sipath validate` and each run's
  `corner_source` say which corners were used.
- The amd64 image's KLayout does not finish the OpenFrame wrapper's LVS
  (under Rosetta, and on CI's native runners); the arm64 runtime matches it in
  843 s, and ChipFoundry's precheck (Netgen LVS) passes it.
- The arm64 image's OpenROAD crashes in post-route repair on the OpenFrame
  wrapper and on aes.
- On mcu8, `vref_hi` keeps 9.7 fF of coupling from digital wires on the layers
  below it.
- dp8's Magic-vs-KLayout XOR shows two sub-0.1 µm² differences on wide analog
  wires.

## Documentation

| document | contents |
|---|---|
| [Tutorial notebook](notebooks/sipath_tutorial.ipynb) | the example flows, runnable in Colab or Codespaces |
| [Example flows](docs/examples.md) | recipes, one feature each: RTL to signoff, explore mode, synthesis and placement sweeps, macro placers, analog shields, a full die, an OpenFrame chip with precheck and seal ring, hardened macros, custom steps, resume, ECOs, an agent session, regression limits, a design-space exploration in parallel |
| [How the flow works](docs/flow.md) | each step: floorplan and pads, hard macros, power, placement and CTS, analog nets, routing and repair, sweeps and ECOs, frames and hardened macros |
| [Synthesis](docs/synthesis.md) | the synth step, its recipes and why its defaults are what they are |
| [PDKs and runtimes](docs/pdks-and-runtimes.md) | Sky130 (open_pdks, release pinning, corners, excluded cells), IHP, where OpenROAD comes from, arm64 and native tools, resumable runs |
| [Signoff](docs/signoff.md) | every check, what it measures, and what it found |
| [Design files and examples](docs/design-files.md) | the design file, field by field, and the examples (mcu8 in detail) |
| [Agents (MCP)](docs/agents.md) | the MCP server's tools, jobs and resources |
| [Benchmarks](docs/benchmarks.md) | against ORFS both ways, synthesis, macro placement, analog coupling, amd64 against arm64, explore mode |
| [Reference](docs/reference.md) | every design-file key and step parameter (generated by `sipath docs`) |
| [Tests, limits and CI](docs/testing.md) | unit and live tests, regression limits, the GitHub Actions workflows |
| [Modules](docs/modules.md) | what each source file does |
| [Workarounds](docs/workarounds.md) | behaviour of this OpenROAD build that the flow works around |

## Repository

| path | contents |
|---|---|
| `sipath/` | the package: flow, steps (`sipath/steps/`), signoff, PDK descriptions (`pdks/`), harnesses, examples, tests |
| `sipath/toolchain.json` | the locked images, tool versions and open_pdks release |
| `bench/` | benchmarks against ORFS, synthesis and architectures, and repeated runs |
| `docker/` | recipes for the images SiPath builds itself (Magic; the precheck image) |
| `docs/` | the documentation above |
| `PLAN.md` | the plan and backlog |

## License

SiPath is licensed under the [Apache License 2.0](LICENSE). Third-party files keep their own terms:
- `sipath/pdks/sky130/sky130_stdcell.lvs` derives from OpenROAD-flow-scripts (BSD 3-Clause);
- `sipath/klayout/stream.py` follows ORFS `def2stream.py` (BSD 3-Clause);
- the ChipFoundry IP views under `sipath/examples/inputs/chipfoundry/` keep their vendors'
  licenses, as listed in `SOURCES.txt`;
- the CD-LNS macro placer and the challenge's `Benchmark` under `sipath/third_party/`
  (Apache-2.0, unchanged; provenance in its `README.md`).
