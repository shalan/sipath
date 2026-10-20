<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/sipath-logo-dark.svg">
    <img src="docs/logo/sipath-logo.svg" alt="SiPath" width="420">
  </picture>
</p>

# SiPath — the path to silicon, steered by AI

[![unit](https://github.com/shalan/sipath/actions/workflows/unit.yml/badge.svg)](https://github.com/shalan/sipath/actions/workflows/unit.yml)
[![live](https://github.com/shalan/sipath/actions/workflows/live.yml/badge.svg)](https://github.com/shalan/sipath/actions/workflows/live.yml)

SiPath is a Python physical-design flow that drives live OpenROAD sessions,
from RTL or a gate-level netlist to a **routed DEF, SPEF and a signed-off
GDS**. It is built to be steered by an AI agent:

- an **MCP server** lets an agent open runs, launch sweeps, read metrics and
  reports, run ECOs and query checkpoints;
- an **attempt graph** keeps every path tried and selects the best by score;
- **signoff** (DRC, LVS, STA) is the judge of what counts: a failed required
  check fails the run.

A person drives the same steps from the CLI or Python. PDKs and designs are
data: a PDK description (Sky130 from open_pdks is the most complete; IHP
SG13G2 is the second PDK) and a design file in YAML or JSON.

## Contents

[Quick start](#quick-start) · [Features](#features) ·
[Results](#results) · [Known limits](#known-limits) ·
[Documentation](#documentation) · [Repository](#repository) ·
[License](#license)

## Quick start

SiPath needs Python 3.10+ and Docker (or a native OpenROAD). The tool images
and the open_pdks release are locked in `sipath/toolchain.json`.

```bash
python3 -m pip install -e .                    # the sipath package and console script
python3 -m sipath install-pdk                  # the locked open_pdks release (sky130) through ciel
docker pull openroad/orfs@sha256:751a77afcade9882b51427e6d9d079b8e270e7a8f4aa66df2d0659457d1c29fd
python3 -m sipath toolchain --check            # images and PDK tree against the lock
```

Run a design:

```bash
python3 -m sipath run sipath/examples/gcd8.yaml               # RTL to signoff (soft macro, sky130A)
python3 -m sipath run sipath/examples/gcd8.yaml runs/x --explore   # first-cut PPA in a minute
python3 -m sipath run mcu8                                     # a die: pad ring, hard macros, analog
python3 -m sipath run mcu8 runs/mcu8 --resume                  # continue an interrupted run
python3 -m sipath report runs/gcd8 --render                    # the run's report page
```

Other commands:

| command | what it does |
|---|---|
| `sipath pdks`, `sipath templates` | PDK descriptions; design templates (`sipath new <dir> --template openframe`) |
| `sipath harden <design> <dir>` | a design's hardened-macro views (LEF, Liberty, GDS, SPEF) |
| `sipath setup-placer` | the Python with CPU PyTorch for the CD-LNS macro placer |
| `sipath draw`, `sipath render` | a schematic SVG, or OpenROAD's layout picture, of a checkpoint |
| `sipath lvs <gds> <top> <spice>` | full-device LVS of a layout with the PDK's own deck |
| `sipath limits <run> <design>` | a run against its regression limits |
| `sipath validate`, `sipath docs`, `sipath schema` | check the examples; regenerate the reference; JSON Schema |
| `docker build --platform linux/amd64 -t sipath/precheck:1.3.7 docker/precheck` | the precheck image (ChipFoundry's cf-precheck, Netgen, CVC) |
| `sipath mcp` | the MCP server, on stdio |
| `python3 -m unittest sipath.tests.test_unit` | unit tests (no tools, seconds) |
| `python3 -m sipath.tests.run_live -j 4` | live tests: real runs, in parallel |
| `python3 bench/orfs_compare.py gcd aes` | the benchmark against OpenROAD-flow-scripts |

## Features

| area | what SiPath does |
|---|---|
| **Inputs** | Design files checked against a schema before anything runs; typed step parameters (variant > design > PDK > default); PDKs as data; images locked by digest and the open_pdks release by commit; plugins, and steps named by import path ([design files](docs/design-files.md)) |
| **Synthesis** | Verilog or SystemVerilog (slang), linted by Yosys and Verilator; ABC with 20 measured recipes at the slow corner, the netlist picked by least area meeting timing; Booth and prefix-adder variants; bounded RTL-to-netlist equivalence ([synthesis](docs/synthesis.md)) |
| **Floorplan and chip** | Soft macro (from a DEF, a die box or a utilization) or full die with a pad ring generated from rules; OpenFrame and Caravel harnesses with the wrapper checked pin by pin, assembled into the frame's chip (optional seal ring) |
| **Hard macros** | Fixed, or placed by a random search with orientation search, OpenROAD's mpl, or the CD-LNS placer from the Macro Placement Challenge 2026; halo, channel, pin-clearance and analog-guard rules; scored by a quick place and route |
| **Power** | Power grid (rings, straps scaled to the design), straps to supply pins the grid cannot reach, 3.3 V analog supplies routed with a wide rule; grid and IR checks |
| **Placement and CTS** | Timing- and routability-driven placement; fences, clusters and structured datapaths (column, array, bit-slice) kept intact through sizing |
| **Analog nets** | Routed first with a non-default rule and frozen; guarded against later digital wiring; optional grounded shields and adjacent-layer keep-away; coupling to digital nets reported and bounded |
| **Routing and repair** | Digital routing with repair and antenna loops; post-route timing repair on extracted parasitics that re-routes only the changed nets (a full re-route, in a fresh attempt, when a patch fails); metal fill ([flow](docs/flow.md)) |
| **Signoff** | KLayout DRC and density, standard-cell LVS, antennas, nine-corner STA, harness conformance; opt-in Magic DRC, XOR and full-device LVS; every check also a step of its own ([signoff](docs/signoff.md)) |
| **Tapeout precheck** | ChipFoundry's own precheck (cf-precheck, pinned) on a signed-off OpenFrame design: the project exported in its layout, run in a pinned image with Magic, Netgen and CVC, its results per check ([signoff](docs/signoff.md#tapeout-precheck)) |
| **Exploration** | Variants of a step in parallel, chosen by named scores; resume and cancel; an explore mode that gives PPA and a suggested clock in about a minute; ECOs checked for equivalence |
| **Agents** | MCP server: runs, steps, ECOs, background jobs with progress, checkpoint queries and pictures ([agents](docs/agents.md)) |
| **Quality** | Regression limits per design; 168 unit tests and 46 live tests; CI on GitHub Actions (unit tests on every push, live tests nightly) |

## Results

Highlights; every table, its method and its date are in
[benchmarks](docs/benchmarks.md).

Against OpenROAD-flow-scripts on its own reference designs (same RTL, SDC and
utilization; both layouts measured by the same code; WNS at the typical
corner):

| design | flow | WNS (ns) | TNS (ns) | wirelength (µm) | cell area (µm²) | DRC | wall (s) |
|---|---|---|---|---|---|---|---|
| gcd (1.1 ns) | ORFS | −1.465 | −65.1 | 11,786 | 5,319 | 0 | 321 |
| gcd (1.1 ns) | SiPath | **−1.083** | **−46.6** | **8,061** | **4,975** | 0 | **252** |
| aes | ORFS | −0.196 | −0.67 | **588,211** | **124,637** | 0 | **2,724** |
| aes | SiPath | **−0.010** | **−0.02** | 650,995 | 148,639 | 0 | 4,602 |

SiPath's wall time includes signoff (DRC, LVS, nine-corner STA), which ORFS's
make does not run.

| benchmark | result |
|---|---|
| SiPath's designs through ORFS (gcd8, dp8, dp8_hs, ihp_add8) | timing met in both flows; SiPath 2–18% less cell area |
| Synthesis, shalan/synth_flow's 16 designs | 12 meet timing with the defaults, 13 with the front-end sweep (synth_flow before repair: 6) |
| Analog coupling, mcu8 `vref_lo` | 31.2 fF from a digital net → 0.09 fF with the guard and shields; signoff clean |
| Macro placement, sram8 (eight SRAMs) | random search beats CD-LNS on routed wirelength and overflow (574k vs 628k µm, 7 vs 39) |
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
- On Apple silicon, the amd64 image's KLayout does not finish the OpenFrame
  wrapper's LVS (the arm64 runtime matches it in 843 s; CI runs it natively).
- The arm64 image's OpenROAD crashes in post-route repair on the OpenFrame
  wrapper and on aes.
- In the OpenFrame example, repair leaves one buffer on the long `resetb_l`
  port net with a 2.19 ns input transition (limit 0.75 ns).
- On mcu8, `vref_hi` keeps 9.7 fF of coupling from digital wires on the layers
  below it.
- dp8's Magic-vs-KLayout XOR shows two sub-0.1 µm² differences on wide analog
  wires.

## Documentation

| document | contents |
|---|---|
| [How the flow works](docs/flow.md) | each step: floorplan and pads, hard macros, power, placement and CTS, analog nets, routing and repair, sweeps and ECOs, frames and hardened macros |
| [Synthesis](docs/synthesis.md) | the synth step, its recipes and why its defaults are what they are |
| [PDKs and runtimes](docs/pdks-and-runtimes.md) | Sky130 (open_pdks, release pinning, corners, excluded cells), IHP, where OpenROAD comes from, arm64 and native tools, resumable runs |
| [Signoff](docs/signoff.md) | every check, what it measures, and what it found |
| [Design files and examples](docs/design-files.md) | the design file, field by field, and the eleven examples (mcu8 in detail) |
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
