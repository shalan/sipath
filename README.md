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
from RTL (Yosys synthesis) or a gate-level netlist to a **routed DEF, SPEF
and a signed-off GDS**. It is built to be steered by an AI agent:
- its MCP server lets an agent open runs, launch sweeps of variants, read
  metrics and reports, run ECOs and query checkpoints;
- the attempt graph keeps every path tried and selects the best by score;
- signoff (DRC, LVS, STA) is the judge of what counts.

A person can drive the same steps from the CLI or Python. PDKs and designs are
data: a PDK description (`sipath/pdks/*.yaml`; Sky130 from an open_pdks tree is
the most complete, IHP SG13G2 the second PDK) and a design file (YAML or JSON)
that describes a design and its steps.

```bash
# runs
python3 -m sipath run sipath/examples/gcd8.yaml          # RTL to signoff (soft macro, sky130A)
python3 -m sipath run mcu8                               # a die: pad ring, hard macros, analog (example name)
python3 -m sipath run ihp_add8                           # a soft macro on IHP SG13G2
python3 -m sipath run sipath/examples/gcd8.yaml runs/x --explore   # first-cut PPA in a minute, suggested clock
python3 -m sipath run mcu8 runs/mcu8 --resume            # continue an interrupted run (finished work is kept)
python3 -m sipath run dp8 --config my.yaml -v            # a run configuration file, INFO logging
python3 -m sipath report runs/gcd8 --render              # the run's report page (report.md / report.html)

# setup and tools
python3 -m sipath install-pdk      # the locked open_pdks release through ciel (downloads; --dry-run prints it)
python3 -m sipath toolchain --check   # configured images and installed PDK trees against the lock
python3 -m sipath setup-placer     # the Python with CPU PyTorch for the cdlns macro placer (~/.cache/sipath)
python3 -m sipath pdks             # PDK descriptions available
python3 -m sipath templates        # design templates; `new <dir> --template openframe` writes a project
python3 -m sipath harden gcd8 sipath/examples/hardened/gcd8   # a design's hardened-macro views
python3 -m sipath draw <odb> <svg> [--ring]   # schematic SVG of a checkpoint
python3 -m sipath render <odb> <png>          # OpenROAD's own layout render
python3 -m sipath mcp              # MCP server on stdio

# tests and benchmarks
python3 -m unittest sipath.tests.test_unit      # unit tests: no OpenROAD, seconds
python3 -m sipath.tests.run_live -j 4          # live tests (real runs), classes in parallel, with times
python3 -m sipath limits runs/gcd8 gcd8        # a run against its regression limits
python3 -m sipath validate                     # example inputs and scheduler rules
python3 bench/orfs_compare.py gcd aes          # against OpenROAD-flow-scripts
python3 bench/synth_bench.py                   # synthesis benchmark: 16 designs
```

## Contents

[Features](#features) · [Synthesis](#synthesis) · [PDKs](#pdks) ·
[Runtimes](#runtimes) · [How the flow works](#how-the-flow-works) ·
[Signoff](#signoff) · [Agents (MCP)](#agents-mcp) · [Benchmarks](#benchmarks) ·
[Design files](#design-files) · [Examples](#examples) · [Modules](#modules) ·
[Workarounds](#workarounds-for-this-openroad-build) · [License](#license)

## Features

**Inputs and configuration**
- Design files in YAML or JSON, checked against a schema before anything runs
  (every unknown key and wrong type listed at once). The JSON Schema and
  `docs/reference.md` are generated from the same declarations.
- Typed step parameters, resolved variant > design `step_defaults` > PDK
  `step_defaults` > declared default; `describe_step` lists them.
- PDKs as data: sky130A/B (open_pdks), sky130A_hs, sky130hd (ORFS platform
  files), IHP SG13G2.
- Run configuration from a file, `SIPATH_*` variables or a design file's
  `config:`: threads (every core by default, two thirds on macOS; per step;
  shared among a step's attempts running at once), timeouts, parallel cap,
  container limits, images, native tools, OpenROAD binary, platform.
- Reproducible toolchain: images locked by digest with their tool versions,
  and the open_pdks release locked by commit (`sipath/toolchain.json`). An
  unpinned image or a PDK tree of another release is refused by default, and
  each run records what it used. Packaged with `pyproject.toml` (`sipath`
  console script).
- Plugins (`plugins:` or the `sipath.plugins` entry points) add steps, scores,
  signoff checks, harnesses, design templates and design-file extensions. A
  flow can also name a step function by import path
  (`- step: mypkg.checks:inst_count`), with typed parameters from its `params`
  attribute.

**Front end** (`synth`; see [Synthesis](#synthesis))
- Verilog or SystemVerilog (slang), linted by Yosys and Verilator.
- ABC mapping with 20 measured recipes, at the slow corner, without a delay
  target, given the SDC's boundary model; several recipes in one attempt, the
  netlist picked by least area meeting timing (else the knee of the
  slack/area front), judged by OpenSTA with the Liberty wire-load model.
- Front-end variants (Booth multipliers, parallel-prefix adders); register
  names kept through flattening.
- Bounded RTL-to-netlist equivalence of the selected netlist; black-box
  Liberty models for macros without one (opt-in).

**Physical design**
- Soft macro (floorplan from a DEF, a die box or a target utilization; pins
  placed automatically) or full die (pad ring generated from rules, every IO
  control pin tied).
- Hard macros: fixed by the design, or placed by a seeded random search with
  orientation search, OpenROAD's `rtl_macro_placer`, or the CD-LNS placer from
  the Macro Placement Challenge 2026; every result checked against halo,
  channel, pin-clearance and analog-guard rules and scored by a quick place
  and route. Per-macro supply connections and grids; signoff on the abstract
  or the full layout; hardened views reused with `views: <dir>`.
- Power grid (rings, straps scaled to the design) and a power connector for
  pins the grid generator cannot reach (pad tabs, analog macro tabs, harness
  supply pins); 3.3 V analog supplies routed with a wide non-default rule.
- Placement driven by timing and routability (as ORFS), then
  `improve_placement` and `optimize_mirroring`; fences and clusters;
  structured datapaths (column, array, bit-slice) with well-tap slots.
- Analog nets routed first with a non-default rule and frozen, kept at that
  spacing from later digital wiring, optionally shielded (grounded wires on
  both sides) with an adjacent-layer keep-away; their coupling to digital
  nets is reported and bounded.
- Digital routing with repair on global-route parasitics, antenna repair
  loops and incremental patch routes; post-route timing repair on extracted
  parasitics that re-routes only the nets it changed, checked for equivalence.
- Metal fill with the PDK's rules; in-flow repair on three corners, OpenRCX
  extraction and signoff timing at all nine.

**Exploration mode** (`--explore`)
- A first cut at PPA to set the SDC before a full run: the design's flow
  without the equivalence check, detailed routing, post-route repair, fill,
  extraction or signoff, optimized at the typical corner, with timing and
  power from global-routing parasitics.
- `ppa.json` / `ppa.md` give cell area, utilization, WNS/TNS/WHS, power,
  wirelength and, per clock, the achievable period, fmax and a suggested
  `create_clock` period (achievable + 5%).

**Signoff** (see [Signoff](#signoff))
- KLayout DRC (rule groups in parallel), metal density, standard-cell LVS,
  antennas, independent nine-corner STA, harness conformance; opt-in Magic
  DRC, a Magic-vs-KLayout XOR and full-device LVS with the PDK's own deck.
- One summary (`signoff.json`); a required check that fails fails the run.
  Every check is also a step of its own (`drc`, `lvs`, `timing`, ...).

**Frames and chips**
- Harnesses (OpenFrame, Caravel): the design is the frame's wrapper (template
  die and pins, every frame supply pin, power grid, routing limits), checked
  against the template pin by pin; `integrate` places the signed-off wrapper
  in the frame's layout and checks the chip GDS.
- Hardened macro views (`views`, `sipath harden`) and design templates
  (`sipath templates`, `sipath new --template openframe`).

**Execution**
- Attempt graph: variants per step run in parallel, named scores choose,
  resume, cancel, provenance; a failed attempt writes no checkpoint.
- ECOs (resize, buffer) checked for structural equivalence and timing loss.
- MCP server for agents; a run report after every step.
- Regression limits per design and one checker (`sipath limits`); unit and
  live tests; CI on GitHub Actions (unit tests on every push, live tests
  nightly).

**Known limits**
- RTL equivalence is bounded, not a full proof.
- Full-device LVS runs but matches neither dp8 nor the ChipFoundry ADC yet;
  the standard-cell LVS is the gating check.
- No second voltage domain: 3.3 V analog supplies are routed to macro pins,
  but there is no 3.3 V (hvl) cell region, level shifting or voltage-aware
  check.
- PDKs: Sky130 has signoff decks; IHP SG13G2 routes to DEF and SPEF without
  them; no gf180mcu yet.
- No gate-level simulation.
- Seven sky130 HD cells are kept out of synthesis and sizing: the KLayout LVS
  deck cannot pair them (`sipath/pdks/sky130/lvs_exclude_sky130_fd_sc_hd.cells`).
- The arm64 image's OpenROAD crashes in the repair after global routing on
  the OpenFrame wrapper and on aes.
- dp8's XOR shows two sub-0.1 µm² differences on wide analog wires (Magic
  reads their wire-end extensions differently).
- On mcu8, `vref_hi` keeps 9.7 fF of coupling from digital wires on the
  layers below it.

## Synthesis

The `synth` step turns RTL into a flat gate-level netlist with Yosys (a tool
step: no OpenROAD process). Its defaults come from measurements, its own and
[shalan/synth_flow](https://github.com/shalan/synth_flow)'s (16 designs, OpenSTA
at the slow corner with the Liberty wire-load model).

1. **Read.** Yosys's Verilog reader, or slang for SystemVerilog
   (`frontend: auto|yosys|slang`; `auto` takes slang for `.sv` sources when the
   image's Yosys has `read_slang`, built in or as the plugin). Lint, synthesis
   and the equivalence check read the RTL the same way
   (`examples/gcd8_sv.yaml`: package, enum, packed struct, function).
2. **Lint.** Yosys: implicit nets, hierarchy errors, undriven or multiply
   driven nets and combinational loops are errors. Verilator
   `--lint-only -Wall` when found (`verilator: auto|on|off`; host, native tool
   or image); library cells and macros the RTL instantiates get stub modules.
   Warnings are counted, `lint_strict` makes them fatal.
3. **Generic synthesis.** `synth -flatten -noabc` with the front-end options
   (`synth_opts`: `booth`, `adder=kogge-stone|han-carlson|sklansky`, `noshare`,
   `nofsm`, `noalumacc`, `opt_dff_sat`, `opt_full`), optional retiming
   (`retime`), registers named after the wire they drive (`acc[3]_reg`,
   `keep_names`).
4. **Mapping at the slow corner** (`map_corner`, default the timing corner):
   flops by `dfflibmap`, logic by ABC with a recipe and the boundary model, the
   SDC's driving cell and output load (else the PDK's defaults for a macro),
   written as ABC's two-line constraint file in fF. ABC's reader knows only
   those two statements; an SDC handed to it as is loses the load
   (`set_load -pin_load` reads as 0), so SiPath resolves the SDC itself.
5. **Recipe sweep and selection.** `abc_sweep` maps with several recipes in
   the same attempt (default `auto`: synth_flow's five delay recipes plus
   `area_map`; `delay`, `area`, `balanced`, a list, or `off`). Each netlist is
   timed by OpenSTA at the slow corner with the wire-load model
   (`synth_wns_ns`, `synth_tns_ns`), and `synth_select` keeps the least area
   that meets timing, else the knee of the slack/area front, else the best
   slack. `abc: <recipe>` maps with one recipe. No ABC delay target by default
   (`abc_target: none|period|<ps>`).
6. **Checks and proof.** An unmapped cell (e.g. a latch) or an excluded cell
   fails the step; the selected netlist is proven against the RTL (Yosys
   miter, cell functions from the Liberty file): equal outputs for every input
   sequence for `equiv_depth` (10) cycles from reset. A mismatch fails the step
   with the counterexample. It is bounded, not a full proof (induction needs
   a register correspondence the synthesized names do not give).

Recipes live in `sipath/synth_recipes/`: 18 from synth_flow (with its notes)
plus SiPath's `area_map` and `speed_sizing`; a new one is a `.abc` file there.
Its objective sets (top five per objective on its bench) are the `abc_sweep`
names. A sweep can also be separate attempts, front ends included:

```yaml
flow:
- step: synth
  sweep: delay                                  # or area, balanced, or a list of recipes
  synth_opts_sweep: [[], [booth], [adder=kogge-stone], [adder=han-carlson]]
```

Each recipe x front end becomes an attempt, picked by `synth_select`.

Why these defaults (numbers from the [synthesis bench](#synthesis-bench)):
- **No delay target.** With `abc_target: period`, 4 of 16 designs meet timing
  against 10 without (both mapped at the typical corner); synth_flow found
  the target worse on 190 of 256 runs. ABC relaxes against its own wireless
  model, slack OpenSTA does not see.
- **Mapped at the slow corner.** 12 of 16 meet timing against 10 mapping at
  the typical corner; slack better on 14 designs.
- **Boundary model.** On aes it gains 1.7–3 ns of slack for 1–2% area.
- **Judged at the slow corner with wire load.** Judged at the typical corner
  without wires, the estimate shared ABC's blind spot and favoured the delay
  target.
- **A sweep, not one recipe.** No recipe wins everywhere: `area_map` is the
  smallest on gcd8 at 10 ns and the slowest on aes at 3.6 ns.

Macros without a Liberty file: with `auto_blackbox: true` (on the design, or
per macro), a black-box model is written from the macro's LEF into
`<workdir>/models/` (pin directions with buses, a default pin capacitance,
the LEF's area, no timing arcs, supply pins left out). Yosys resolves the
instance with it and OpenROAD links against it; paths through the macro are
not timed, and the run's provenance lists each model and any pin whose
direction had to be assumed. It is off by default: without it, synthesis
fails and the message names the macros and the ways out.

## PDKs

A PDK description (`sipath/pdks/*.yaml`, or a path to one) names the views,
routing stack, special cells (tap/endcap optional, tie hi/lo, diode, CTS
buffers), supply pins, PDN and analog defaults, RC, tracks, corners, the IO
library, the SRAM macros and the signoff decks. The library code names no
PDK layer or cell (a unit test greps for it); the same code runs IHP SG13G2.

**Sky130 is first class** (`pdk: sky130A` or `sky130B`): every view comes from
an open_pdks tree (`$PDK_ROOT`, default `~/.ciel`, as installed by ciel),
mounted into the container.
- **Release pinned.** `sipath/toolchain.json` locks open_pdks `8afc8346…`.
  Every run reads the tree's `SOURCES` file (else ciel's `versions/<commit>`
  path) and records it in its provenance (`pdk_release`). A tree of another
  release, or one that says none, is refused unless `SIPATH_REQUIRE_PDK=0`,
  and that run records the mismatch. `python3 -m sipath install-pdk` installs
  the locked release; `toolchain --check` compares the installed trees.
- **Views.** hd standard cells (fd + ef LEFs), the nom tech LEF, `tracks.info`,
  `sky130_ef_io` pads with their gpiov2 Liberty, the OpenRAM
  `sky130_sram_macros` (a design names one with `sram:`).
- **Corners.** Nine signoff corners, tt/ss/ff (tt_025C_1v80, ss_100C_1v60,
  ff_n40C_1v95) × nom/min/max interconnect, each with its own Liberty and RCX
  rules. Estimation RC is the calibrated nominal table scaled per layer and
  via by each corner's tech LEF. Extraction runs once per RCX rule file (3
  SPEFs), each read into the 3 corners that share it. Placement, CTS and
  routing repair on `optimize_corners` (tt_nom, ss_max, ff_min): on aes, nine
  corners made each parasitics estimate ~18x slower (143 s against 8 s);
  signoff still checks all nine.
- **Design rules.** Max transition 0.75 ns, max fanout 10, max capacitance
  0.2 pF, applied before the design's SDC. Boundary defaults (inv_2 driving
  cell, 33.4 fF output load) apply to embedded macros only, since a die's
  ports are bond pads. Standard-cell slew/cap/fanout violators are counted
  (`drv_*`); IO-ring, pad, macro and port violators are listed apart.
- **Reference flow settings.** Tap distance 13 µm; clock nets on met3 and up;
  CTS clkbuf_2/4/8 with a clkbuf_16 root; decap and fill cells; met4/met5
  straps (153.6/153.18 µm) and core ring; repair margins (setup 0.025 ns, hold
  0.1 ns after placement and CTS, 0.05 ns after global route).
- **Excluded cells.** Each Sky130 description reads the PDK's
  `drc_exclude.cells` (53 hd, 51 hs cells: all `lpflow`, some drives,
  `xor3`/`xnor3`, `mux4_4`, `fa_4`, ...). Sizing, buffering and CTS cannot
  choose them, a netlist that uses one is refused at validation (waive with
  `allow_excluded`), and an ECO to one is refused. Designs add patterns with
  `dont_use`.
- **High-speed library.** `pdk: sky130A_hs` (extends sky130A) runs on
  `sky130_fd_sc_hs` (unit site 0.48 × 3.33 µm, its own tracks, special cells
  and boundary defaults). Example: `examples/dp8_hs.yaml`.

`sky130hd` (the ORFS platform copy inside the image) remains a fallback when
no open_pdks tree is installed. The PDKs read from ORFS platform files
(sky130hd, IHP SG13G2) are pinned with the ORFS image's digest; for a native
binary set `$SIPATH_PLATFORMS` to an ORFS `flow/platforms` directory.

**IHP SG13G2** (`pdk: ihp-sg13g2`): standard cells and sg13g2 IO pads from the
ORFS platform tree; routes to a DEF and three-corner SPEF and closes a pad ring
(`examples/ihp_ring.py`), without signoff decks.

## Runtimes

`find_openroad()` uses, in order: `$SIPATH_OPENROAD`, `openroad` on PATH, then
the ORFS docker image (the digest locked in `sipath/toolchain.json`, or
`$SIPATH_OPENROAD_IMAGE`) if it is already pulled. Host directories are
mounted at the same path, so file names in Tcl are the same inside and
outside. On Linux the containers run as the calling user, so the files they
write stay the user's (`SIPATH_DOCKER_AS_ROOT=1` keeps the image's user).

The default is the ORFS image (`openroad/orfs`, amd64: under Rosetta on Apple
silicon). Two ways to avoid the emulation:
- an arm64 image with the tools on PATH, e.g. LibreLane's multi-arch image:
  `SIPATH_OPENROAD_IMAGE=ghcr.io/librelane/librelane:3.0.16
  SIPATH_OPENROAD_BINARY=openroad SIPATH_PLATFORM=linux/arm64` (and
  `SIPATH_MAGIC_IMAGE` the same image for the Magic checks). Its tool versions
  differ (OpenROAD 2026-02-17, Yosys 0.62), so results differ slightly (gcd8:
  158 against 165 cells). Measurements under
  [Benchmarks](#rosetta-amd64-against-native-arm64).
- `native_tools` (`SIPATH_NATIVE_TOOLS=yosys,klayout`): those tools run on the
  host while OpenROAD stays in the image (gcd8 synthesis 10.0 s to 4.4 s with
  Homebrew's Yosys).

Magic (opt-in checks) runs from `config.magic_image`
(`docker build -t sipath/orfs-magic:8.3.685 docker/magic`). Each run's
provenance records the image digest, platform, binary, tool versions and the
PDK release.

**Runs are resumable.** `graph.json` records every attempt: params, parent,
status, checkpoint, metrics, process, timing (phases and slowest Tcl
commands), the OpenROAD transcript tail of a failed attempt, the run
configuration and provenance. `--resume` rebuilds the graph: a step asked
again with the same parent, variants and score returns its recorded result;
attempts of an interrupted sweep that finished are kept and only the rest
run. A selected attempt's directory is never deleted, and no attempt
directory is reused. The library logs to the `sipath` logger and is silent
unless the caller configures logging.

## How the flow works

(`synth`) → `floorplan` → (`macros`) → `physical` → `place` → `cts` →
`route_analog` → `route_digital` → `route_repair` → (`fill`) → `extract` →
`signoff` → (`views`, `integrate`). `ppa` ends an exploration run; `drc`,
`density`, `lvs`, `lvs_full`, `antenna`, `timing`, `magic_drc`, `xor` and
`harness` run single signoff checks.

**Floorplan and pads**
- A soft macro takes its die, rows and pins from a DEF, or a die box or a
  utilization with pins placed at random, then by the cells after a first
  placement. PDK straps too wide for a small core are scaled to it.
- `Design(is_core=False)` (an embedded macro) routes up to the PDK's
  `max_layer_macro` (Sky130 met4, IHP Metal4); straps above it are refused.
- Pad plan rules (`sipath.padplan`) are checked before any pad is placed:
  supply pads abut their return pad; a VDDIO/VSSIO pair on every edge with
  signal pads; at most 8 outputs per IO pair; core supply on at least 2
  edges; analog pads on an edge with VDDA/VSSA and never next to an output.
- Every gpiov2 control pin is tied: core-domain pins from tie cells, `*_H`
  pins from the pad's own TIE_HI_ESD/TIE_LO_ESD; any floating pad input fails
  the floorplan. Pad timing comes from the PDK's pad Liberty per process: on
  mcu8 an input pad's PAD->IN is 1.35 ns at tt and an output pad's OUT->PAD
  5.24 ns at ss.
- The pad ring is generic: corner site and row depths from the corner cell,
  corner rotation from the edges its buses exit, pad rotations by trial
  (core-side pins must face the core), ring buses from the IO fillers.
  `examples/ihp_ring.py` closes the ring on IHP SG13G2 with nothing
  configured (rotations R0/R270/R0, against sky130's R180/R90/R180).

**Hard macros** (`macroplace.py`)
- A macro with a `location` is frozen (LOCKED). Otherwise the `macros` step
  places it with one of three methods, and every attempt is scored the same
  way: a quick global and detailed placement and global route of the
  standard cells (then unplaced again); `macro_quick` = least overflow, then
  quick HPWL + 4 × analog HPWL.
- `random` (default): `samples` legal positions per macro on the row/site grid
  from `random.Random(seed)` (some against a core edge), each in every allowed
  orientation; the objective is macro-net HPWL + 4 × analog-net HPWL over
  placed terminals (pin-accurate, so orientation and pin side count).
- `mpl`: `rtl_macro_placer` with keepouts as temporary blockages.
- `cdlns`: Team Macro Polo's CD-LNS placer from the Partcl/HRT Macro
  Placement Challenge 2026 (coordinate descent + large-neighbourhood search +
  annealing; the best-ranked CPU-only entry), run for `placer_seconds` in a
  separate Python with PyTorch (`sipath setup-placer`, config
  `placer_python`). SiPath exports the floorplan after a quick mixed-size
  global placement (macros with pin offsets, standard cells clustered into
  movable soft macros, IO pins, keepouts as fixed blocks), hands the placer a
  legal start with rule-aware spacing, and snaps the result to the grid.
- Rules for every method: halo, `macro_channel` (no sliver channel between
  macros or to the core edge), `macro_pin_clearance` (space in front of pin
  edges), `analog_guard` (extra space around analog macros and a standard-cell
  blockage band; rows are cut under it). Results compared under
  [Benchmarks](#macro-placement).

**Power** (`physical`)
- Followpin rails plus straps; rings when asked (`PdnSpec(rings=True)`).
  Endcaps and taps (`place_endcaps`, `place_tapcells`); the step fails unless
  the endcap count is twice the row-segment count, and on any row without a
  tap (row pieces too short for a tap are cut).
- The power connector (`sipath.powerconnect`) straps tabs pdngen cannot reach
  (under the cells' own upper-metal obstructions): the power pads' core-side
  tabs to the core ring, the analog macros' 1.8 V tabs to the grid. Each strap
  avoids other nets and obstructions and gets a via stack; a short between
  special nets fails the step.
- 3.3 V analog supplies (VDDA/VSSA) are routed from a launch point at the pad
  tab to the macro tabs on met3–met5 with a 2 µm rule, frozen as special nets
  and strapped to the pad.
- `extract` runs `check_power_grid` (against the pad terminals when they
  exist) and `analyze_power_grid` for every supply.

**Placement and CTS**
- `global_placement -timing_driven -routability_driven`,
  `estimate_parasitics -placement`, `repair_design`, `repair_timing`,
  `detailed_placement`, `improve_placement`, `optimize_mirroring`, then
  `check_placement`. `repair_tie_fanout` first: one tie cell per load.
- After legalization, a global route with `-allow_congestion` measures
  congestion (`grt_overflow`, report kept); `congestion_margin` reserves
  head-room on every layer, and the `routability_then_wns` score rejects a
  congested variant by score, not by a crash.
- Datapaths: `column` (one cell per row), `array` (abutted rows) and `slices`
  (`cells[bit][stage]`, stage columns aligned across bits), FIRM, placed
  before taps with tap slots reserved. After every sizing step `reabut`
  re-packs with the current masters and a structure check fails the attempt
  unless every cell sits where the packing puts it.
- Groups: `fence` is a `dbRegion` (EXCLUSIVE) plus a `dbGroup` (inserted cells
  join it if they drive a member, else are evicted); `cluster` is a
  `PHYSICAL_CLUSTER` group shelf-packed at its centroid after placement.
- CTS gets explicit `-clk_nets`; analog nets are refused as clock nets and
  their terminals must be unchanged after CTS.

**Analog nets** (`route_analog`)
- Routed alone first with their NDR (`create_ndr`/`assign_ndr`,
  `set_nets_to_route`), with temporary blockages keeping them off the
  pin-access layers over standard-cell rows. The wiring is rebuilt as
  special wiring, made `setSpecial` and `set_dont_touch`, and a signature of
  the shapes is compared after the digital route: a change fails the attempt.
  Before extraction the exact regular wiring is restored (OpenRCX skips
  special nets); `extract` fails if an analog net has no `*D_NET`.
- **Guard.** The router keeps only the minimum spacing from special wiring,
  so the NDR spacing alone would protect an analog wire only from what was
  routed before it. Obstructions around the frozen wiring (`guard`, default
  on; cut around pins) keep digital wires at the NDR spacing.
- **Shields** (`analog_shield: [nets]`, `shield.py`). OpenROAD's router does
  not shield signal nets, so SiPath draws the shields. A shielded net's NDR
  leaves room for one on each side; every straight segment then gets a
  minimum-width wire of `analog_shield_net` (default the core ground) on both
  sides at the minimum spacing, clipped by the spacing rule around every other
  shape on that layer. A piece is grounded by touching a ground shape, by a
  via where it crosses one on the next layer (pad flush with the inner edge,
  never on or beside another via), or by a tie strap outward on the next
  layer (`shield_tie_um`, default 30 µm, never on the pin-access layers). A
  piece that reaches no ground is dropped, never left floating; no sliver
  overlaps; coverage is reported per net.
- **Keep-away** (`analog_keepaway: true`): obstructions on the layers just
  above and below each shielded segment and its shields (not on pin-access
  layers, cut around pins).
- **Coupling report.** `extract` writes `analog_coupling.json`/`.md` from
  every corner's SPEF: per analog net its total capacitance, its coupling to
  digital nets (sum and largest aggressors, worst corner) and to the other
  analog nets. Metrics `analog_coupling_ff` / `analog_aggressor_ff`; step and
  regression limits bound them. Results under [Benchmarks](#analog-coupling).

**Routing and repair**
- `route_digital`: global route; repair on global-route parasitics in
  incremental mode; `repair_antennas`; detailed route; `check_antennas` and up
  to three antenna repair rounds; RCX to rank the variants (`drc_then_slack`).
- `route_repair`: RCX; `repair_timing` (resize only, so structural equivalence
  is checked; hold buffers separately); re-route of the changed nets
  (`eco.patch_route`, a full re-route past a quarter of the nets); RCX again,
  at most two rounds; fillers.
- `fill`: OpenROAD `density_fill` with the PDK's fill rules, up to the
  design's top routing layer (an embedded macro leaves the layers above to its
  parent), streamed to the PDK's fill datatypes.
- Each attempt writes timing (per corner), clock skew, power and congestion
  reports.

**Sweeps and ECOs**
- `variants=[...]`, `parallel=N`, `score="<name>"` (default `auto`: setup slack
  when constrained, else wirelength); rejected attempts stay in `graph.json`.
  `fail_on_violation` makes a timing violation fail the attempt.
- ECOs `resize` (`replace_cell`) and `buffer` (`insert_buffer`): refused unless
  the parent is the selected head of its chain; structural equivalence before
  routing; rejected if setup or hold slack drops by more than
  `max_slack_loss` (0.05 ns); only the touched nets are re-routed (a full route
  past `max_patch_nets`). Equivalence reads cell functions from the session's
  Liberty, so drive-strength classes and buffers are derived, not named.

**Frames and hardened macros**
- Harnesses (`harness: openframe`, `caravel`, or a plugin's): the harness sets
  the die and every template pin, creates the frame's supply pins (user
  supplies strapped to the core ring), the power grid and routing limits.
  Template pins overhanging the boundary are clipped for routing and restored
  at signoff; the `harness` check compares the routed wrapper with the
  template (die, every pin by name, layer and rectangle); loopback nets stay
  plain wires. `examples/openframe_gcd8.yaml` puts a 4-bit gcd on the
  OpenFrame's east-edge pads.
- `integrate` (after signoff): Magic puts the signed-off wrapper GDS,
  unchanged, into the frame's layout (as the frame's own build does) and
  writes the chip GDS; KLayout checks one top cell, the frame's die, the
  wrapper placed once at its origin and identical to the signed-off GDS. The
  seal ring is left to tape-out, as in the frame's build.
- `views`: abstract LEF, a timing Liberty per corner (`write_timing_model`),
  GDS, LVS subcircuit, SPEF per corner, netlists with and without supply pins,
  and `macro.yaml`. `sipath harden <design> <dir>` runs a design through
  signoff and `views`; a parent lists `{inst: u0, views: <dir>, location: [x,
  y]}` (`examples/gcd8_pair.yaml`). Library cells the macro's GDS carries are
  kept as the library's, so LVS pairs them.
- Design templates: `sipath new <dir> --template openframe --set inputs=8
  --set outputs=6` writes a project (design file, wrapper with every frame
  port and the loopback ties, a starter core, SDC) that runs as generated.

## Signoff

The `signoff` step (`sipath/signoff.py`) checks the final database; every
check can also run as a step of its own.

- **GDS.** The routed DEF streamed with every cell's layout, supply nets
  labelled. A cell no GDS defines (a LEF-only macro) makes DRC/LVS impossible
  and fails them. Each macro's GDS keeps its own cells: names that clash with
  a library or another macro are renamed, not merged; library GDS load
  first-definition-wins.
- **DRC.** open_pdks `sky130A_mr.drc`, its rule groups in parallel processes
  (`drc_split`; aes 209 s to 156 s); every marker counts, also by owner (`top`
  or the macro or library whose cell it is in). Metal density with the PDK's
  deck: gating for top-level layouts, reported for embedded macros.
- **LVS.** `sipath/pdks/sky130/sky130_stdcell.lvs` (derived from ORFS's deck)
  against a reference netlist from `write_cdl` after the supply connections
  are re-applied: only the subcircuits the design uses, tie cells as the poly
  shorts their layout is, supplies as ports. Black boxes (macros with
  `lvs: blackbox` or `abstract: true`, and the PDK's `signoff.lvs_blackbox`
  masters: Sky130's IO cells) are compared as their pins only and listed in
  `lvs.blackboxed`. A macro with neither a `cdl` nor `lvs: blackbox` fails LVS.
- **Antennas** (`check_antennas`).
- **Timing**, in a fresh OpenROAD process from only the final netlist,
  Liberty, SDC and per-corner SPEF: setup and hold on all nine corners,
  slew/cap/fanout on standard cells, parasitic annotation (a loaded net
  without parasitics fails), unconstrained endpoints.
- **Opt-in:** `magic_drc` (Magic `drc(full)`), `xor` (Magic's stream-out of the
  routed DEF against KLayout's; fill not compared), `lvs_full`: full-device
  LVS with the PDK's own deck (open_pdks `sky130.lvs`: high-voltage MOS, MIM
  caps, resistors, diodes), also as `sipath lvs <gds> <top> <schematic...>`
  for a macro. Schematics are prepared for the deck (device aliases, the
  device prefix on CDL models, cell SPICE for black boxes, parallel MOS merged
  as the deck merges the layout's). Not yet matching: the ChipFoundry ADC's
  vendor schematic lacks its `decoder3to8`; on dp8 the deck and the PDK's cell
  netlists disagree (parallel devices merged across cells, tap diodes and tie
  resistors, well pins).

`signoff.json` holds every result; a failed required check fails the attempt.
Measured:
- gcd8, dp8 and dp8_hs: DRC 0, density clean, LVS match, antennas 0, timing
  met on nine corners.
- mcu8 (ChipFoundry macros signed off as abstracts, their GDS merged into the
  written GDS): DRC 0, LVS match (macros and IO cells as black boxes),
  antennas 0, setup +8.83 ns and hold +0.099 ns over nine corners, design
  rules clean, with all four analog nets shielded. On the full layouts the
  deck finds 95 markers inside the vendor cells (ADC: 86 `capm.2b_a`, 6
  `hvtp.2`, 2 `nsdm.1`; DAC: 1 `nwell.6`), for ChipFoundry to address.

Found by signoff and fixed (DRC alone sees none of the LVS ones):
- A VDD–VSS short: a strap's landing patch crossed a VSS stripe. Landings now
  avoid other nets on every layer they cross, and every step that adds straps
  fails if special shapes of two nets touch (`special_shorts`).
- Floating ESD clamps: the plain `*_hvc` power pads leave their clamp
  terminals to be routed; mcu8 uses the `*_clamped` pads, as Caravel does.
- 158 untapped row pieces beside the SRAM with floating n-wells: such pieces
  are cut and a row without a tap fails `physical`.
- Strap vias next to the router's own (merged, illegal cuts), off-grid pin
  edges copied from vendor LEFs and a notch to a fill shape: mcu8's top-level
  DRC went from 12 markers to 0.
- OpenRCX re-orders each net's wire and drops the router's minimum-area
  patches; every extraction restores the routing from a DEF written just
  before (geometry checked). A post-route minimum-area repair stays as a
  safety net and normally finds nothing.
- Cells added after the power grid (CTS buffers, fillers, ties, diodes) had
  unconnected supply pins; global connections are re-applied after every step
  that adds cells.

## Agents (MCP)

`python3 -m sipath mcp` serves `open_run` (an example name or any design file;
`resume` picks up a workdir), `run_step`, `run_eco`, `job_status`,
`cancel_job`, `list_jobs`, `list_attempts`, `get_outputs`, `describe_step`,
`query_checkpoint`, `draw_checkpoint` and `render_checkpoint` (plus
`list_designs`, `list_scores`).
- `run_step` / `run_eco` are jobs: in the foreground they send
  `notifications/progress` as attempts start and end (with a
  `progressToken`); with `background: true` they return a job id at once.
- `cancel_job`, or `notifications/cancelled`, kills the job's OpenROAD
  processes; its attempts end `cancelled` with no checkpoint.
- Arguments are checked before anything runs (step, score, parallel,
  parameter names per step, ECO ops): a bad one is a tool error.
- Resources: each run's `graph.json` and every attempt's DEF, SPEF (per
  corner), reports, debug checkpoint and pictures, as
  `flow://<run>/<attempt>/<name>`.
- `query_checkpoint` runs Tcl in a fresh process with `write_*`, `exec`,
  `file`, `source`, `socket` removed and `open` limited to the session's own
  result files: a guard against accidental writes, not a security sandbox.

## Benchmarks

Each table says when it was measured.

### Against ORFS

Measured 2026-10-19. `python3 bench/orfs_compare.py gcd aes` runs ORFS's own
sky130hd reference designs through ORFS (`make` in the ORFS image) and through
SiPath (sky130A, the standard flow with fill) from the same RTL, SDC and core
utilization, then measures both final layouts with the same code: setup
WNS/TNS at the typical corner (open_pdks tt Liberty, the design's SDC, each
flow's own SPEF), routed wirelength from the final DEF, logic cells and their
area (no fill, tap, decap or diode), the detailed router's DRC count and the
PDK's KLayout DRC deck on each GDS. Wall time is the whole run, on an
Apple-silicon Mac under Rosetta (amd64 image), 12 threads: indicative only.
ORFS's results are its 2026-10-16 runs (same image), measured again.

| design | flow | WNS (ns) | TNS (ns) | wirelength (µm) | cells | cell area (µm²) | DRT DRC | KLayout DRC | wall (s) |
|---|---|---|---|---|---|---|---|---|---|
| gcd (1.1 ns) | ORFS | −1.465 | −65.1 | 11,786 | 473 | 5,319 | 0 | 0 | 321 |
| gcd (1.1 ns) | SiPath | −1.318 | −57.7 | 8,050 | 485 | 4,917 | 0 | 0 | 167 |
| aes | ORFS | −0.196 | −0.67 | 588,211 | 15,396 | 124,637 | 0 | 0 | 2,724 |
| aes | SiPath | −0.328 | −0.87 | 649,099 | 17,924 | 148,641 | 0 | 0 | 6,679 |
| aes | SiPath, 2026-10-16 (synthesis defaults before 10-17) | −0.237 | −0.88 | 568,195 | 16,389 | 143,121 | 0 | 0 | 4,258 |

aes by step (seconds):

| step | ORFS | SiPath | SiPath 2026-10-16 |
|---|---|---|---|
| synthesis | — | 689 (600 of it the RTL equivalence check, timed out: inconclusive) | 666 |
| place | 83 | 114 | 108 |
| CTS | 26 | 44 | 47 |
| routing (global, repair, detailed, antennas) | 2,290 (detailed route) | 3,698 | 1,226 (detailed route) |
| post-route repair | — | 1,440 | 1,507 |
| extraction | — | 25 | 31 |
| signoff (LVS, DRC, nine-corner STA in parallel) | — | 655 | 655 |
| **whole run** | **2,724** | **6,679** | **4,258** |

- **gcd** (a 1.1 ns clock, beyond what sky130 HD closes): SiPath now ends
  0.15 ns ahead of ORFS in WNS and 11% in TNS, with 32% less wire, 8% less
  cell area and half the wall time, signoff included. With its 2026-10-16
  defaults it was 1.0 ns behind; the difference is the synthesis front end
  (slow-corner mapping, the recipe sweep).
- **aes** got worse than on 2026-10-16: WNS at tt −0.328 ns (ORFS −0.196),
  10% more wire and 19% more cell area than ORFS. Synthesis now maps at the
  slow corner, where aes's clock cannot be met (synthesis WNS −4.9 ns), so the
  selection falls back to the best-slack recipe (`delay_syn2`): a bigger
  netlist that routes slower (3,698 s against 1,226 s) and is repaired harder
  (34k instances after repair). The synthesis bench's gains do not carry over
  to aes end to end; how to pick a netlist when nothing meets timing at
  synthesis is open.
- **aes signoff:** DRC 0, antennas 0, hold met on all nine corners, setup
  −0.33 ns at tt, −4.27 ns at ss_max and +0.85 ns at ff (ORFS does not check
  ss), 126 slew violations. LVS failed on `sky130_fd_sc_hd__o211ai_4`, a cell
  post-route resizing chose that the KLayout deck cannot pair; it is now
  excluded with the other six, and aes is being run again.
- Both flows route to met5 with the design's own routing adjustments (aes:
  30% on every layer). Placement is routability-driven by default; before it
  was, global routing on aes failed with these adjustments. ORFS's
  equivalence check (kepler-formal) is disabled (`LEC_CHECK=0`): its binary
  dies under Rosetta.

### SiPath's own designs through ORFS

Measured 2026-10-19 (ORFS: its 2026-10-16 runs, measured again). `python3 bench/orfs_compare.py sipath/examples/gcd8.yaml sipath/examples/dp8.yaml ...`
goes the other way: ORFS gets a generated `config.mk` with the design's netlist
(`SYNTH_NETLIST_FILES`) or RTL, SDC, floorplan DEF (or utilization and margin)
and top routing layer, on its platform for the PDK. It also gets what SiPath
reads from the design file, in ORFS's own terms: the analog nets
do-not-touch (`POST_FLOORPLAN_TCL`; without it ORFS's repair fails on dp8's
input-to-output analog net, RSZ-0074) and, where the design fixes its straps or
on IHP (the platform's core ring does not fit a macro's margin, PDN-0351),
SiPath's power grid (`PDN_TCL`). SiPath runs the design's own flow with
signoff added (sky130; IHP has no signoff decks here). Same measurement as
above; 12 threads, Rosetta.

| design | flow | WNS (ns) | wirelength (µm) | cells | cell area (µm²) | DRT / KLayout DRC | wall (s) |
|---|---|---|---|---|---|---|---|
| gcd8 (RTL) | ORFS | 7.165 | 4,220 | 211 | 1,973 | 0 / 0 | 168 (tools 54) |
| gcd8 (RTL) | SiPath | 7.154 | 3,460 | 202 | 1,667 | 0 / 0 | 64 |
| dp8 | ORFS | 5.984 | 5,576 | 151 | 2,185 | 0 / 0 | 147 (tools 33) |
| dp8 | SiPath | 5.812 | 7,230 | 111 | 1,791 | 0 / 0 | 67 |
| dp8_hs | ORFS | 7.658 | 5,816 | 151 | 2,610 | 0 / 0 | 150 (tools 34) |
| dp8_hs | SiPath | 7.514 | 7,328 | 111 | 2,442 | 0 / 0 | 81 |
| ihp_add8 | ORFS | 8.412 | 2,252 | 117 | 2,097 | 0 / – | 138 (tools 18) |
| ihp_add8 | SiPath | 8.487 | 2,743 | 89 | 2,054 | 0 / – | 19 |

Timing is met with similar slack everywhere (TNS 0). SiPath uses 2–18% less
cell area: ORFS buffers every port (49 port buffers on dp8, which ends with 40
more cells than SiPath's) and SiPath does not for an embedded macro.
On gcd8 (plain placement) SiPath's wire is 18% shorter; on the datapath designs
it is 22–30% longer, the cost of the structured placement the design file asks
for (slices and arrays at a fixed origin, a cluster) and of the analog nets on
a wider non-default rule. ORFS's wall time is mostly per-stage process start-up
under Rosetta (its own stage times add up to 18–54 s); SiPath's includes
signoff (KLayout DRC, LVS, nine-corner STA: 19–24 s). mcu8 and ihp_ring (pad
rings, hard macros, analog supplies) are not compared: they would need ORFS
footprint scripts written for them.

### Synthesis bench

Measured 2026-10-17, and again on 2026-10-19 with identical results. `python3 bench/synth_bench.py` runs the synth step alone on shalan/synth_flow's
16 designs (`bench/synth`: 12 in-house designs and 4 external IPs at pinned
commits, each with its SDC) on sky130A, and lists the selected netlist next to
synth_flow's best pre-repair result per design (its `baseline-noD` run: the
same slow-corner Liberty, OpenSTA with wire load, no delay target, no
post-synthesis repairs). Slack at ss_max with the Liberty wire-load model;
the front-end sweep adds Kogge-Stone, Han-Carlson, Sklansky, Booth and
Booth + Kogge-Stone as attempts.

| design | period (ns) | SiPath WNS (ns) | area (µm²) | recipe | + front-end sweep: WNS | area | front end | synth_flow WNS | area |
|---|---|---|---|---|---|---|---|---|---|
| alu32 | 10 | −1.352 | 10,850 | delay_map_resyn | +0.841 | 11,936 | adder=kogge-stone | −0.922 | 10,870 |
| mul16_pipe | 9 | +0.474 | 17,922 | delay_map_resyn | +1.018 | 14,305 | booth | −0.411 | 12,463 |
| mul32_mac | 18 | +0.285 | 73,156 | delay_map | +0.054 | 62,125 | adder=han-carlson | −2.584 | 39,668 |
| fir8 | 12 | +1.347 | 15,664 | delay_map_resyn | +0.008 | 15,441 | adder=sklansky | +0.323 | 13,292 |
| aes_round | 8 | +0.751 | 59,641 | delay_syn2 | +0.751 | 59,641 | plain | +1.422 | 53,538 |
| sha256_core | 13 | +1.051 | 71,622 | delay_map_resyn | +1.051 | 71,622 | plain | −0.074 | 65,980 |
| crc32_8 | 4 | +0.360 | 2,791 | delay_map | +0.360 | 2,791 | plain | −0.030 | 2,153 |
| rr_arbiter16 | 6 | +0.116 | 3,791 | delay_map | +0.174 | 3,626 | adder=kogge-stone | −0.430 | 3,065 |
| uart | 4 | +0.308 | 4,079 | delay_map | +0.102 | 3,945 | adder=sklansky | +0.054 | 3,765 |
| spi_master | 4 | +0.326 | 4,770 | delay_map | +0.325 | 4,735 | adder=kogge-stone | +0.203 | 4,868 |
| apb_timer | 5 | −1.190 | 11,321 | delay_syn2 | −0.205 | 11,805 | adder=han-carlson | −0.065 | 11,347 |
| fifo_sync | 4 | +0.025 | 26,671 | delay_map | +0.020 | 26,603 | adder=kogge-stone | −0.602 | 27,948 |
| zx16_core_ahb | 10 | +0.933 | 22,401 | delay_map | +0.079 | 22,126 | adder=sklansky | −0.173 | 20,520 |
| zxip | 11 | −0.969 | 156,319 | delay_syn2 | −0.206 | 160,365 | adder=kogge-stone | −1.238 | 161,253 |
| ms_psram_ahb | 8 | −0.109 | 25,154 | delay_map_resyn | −0.109 | 25,154 | plain | +0.117 | 25,149 |
| uart_apb_sys | 10 | +3.698 | 17,748 | delay_choice_deep_v3 | +3.698 | 17,748 | plain | +3.971 | 16,917 |

| configuration | designs meeting timing | wall time, 16 designs |
|---|---|---|
| defaults (`abc_sweep: auto`, mapped at ss_max) | 12 | 156 s |
| defaults + front-end sweep | 13 | 702 s |
| mapped at the typical corner (`map_corner: typical`) | 10 | 158 s |
| typical corner + `abc_target: period` | 4 | 155 s |
| synth_flow, best pre-repair result per design | 6 | — |

The selection keeps the least area meeting timing, so a sweep can return less
slack at less area (fir8). SiPath often meets timing with more area than
synth_flow's runs (mul32_mac: 73k µm², meeting timing, against 40k missing by
2.6 ns); synth_flow maps with a stripped 120-cell Liberty and the full flow
there closes the remaining designs with OpenSTA-guided sizing after synthesis,
which SiPath leaves to OpenROAD after placement.

### Macro placement

`examples/sram8.yaml` (eight sky130 1 KB SRAMs plus glue) and mcu8 (two
placed macros), each attempt scored by `macro_quick` (lower is better):

| design | method | macro-net HPWL (µm) | total quick HPWL (µm) | overflow | macro cost (µm) |
|---|---|---|---|---|---|
| sram8 | random, seed 1 | 150,547 | 574,065 | 7 | — |
| sram8 | cdlns | 146,339 | 628,305 | 39 | — |
| sram8 | mpl | fails (MPL-0003: no valid tiling) | | | — |
| mcu8 | random, best of 3 seeds | — | — | 0 | 53,061 |
| mcu8 | cdlns | — | — | 0 | 58,329 |
| mcu8 | mpl | — | — | — | 68,785 |

The random search wins on SiPath's score so far. cdlns gives the shortest
macro-net wiring on sram8, but it packs the SRAMs about 36 µm apart, and the
glue logic between them then routes with more overflow. The challenge's
proxy cost rewards that packing; SiPath judges by a quick route.

### Analog coupling

Coupling from digital nets onto analog nets (worst corner), from
`analog_coupling.md`:

| design | net | unguarded, unshielded (fF) | guard + shields (fF) | shield coverage | total C (fF) |
|---|---|---|---|---|---|
| dp8 | ana_in[0] | 3.68 (2.78 from `clk`) | 0.00 | 93 % | 21 → 38 |
| mcu8 | vref_lo | 31.2 (from `core.pad_ch_0_i`) | 0.09 | 63 % | 130 → 196 |
| mcu8 | ana_in | 0.29 | 0.01 | 70 % | 65 → 115 |
| mcu8 | dac_out | 1.15 | 0.58 | 91 % | 47 → 96 |
| mcu8 | vref_hi | 9.8 | 9.7 | 47 % | 121 → 147 |

Both designs sign off clean with the shields (DRC 0, LVS match, antennas 0,
timing met). Before the guard, mcu8's router laid `core.pad_ch_0_i` 0.25 µm
from `vref_lo` for 340 µm, inside the analog NDR's spacing. `vref_hi`'s
remainder comes from met1/met2 digital runs about 2 µm beside its met3 run
along the ADC's pin edge: coupling between layers, which a same-layer shield
does not stop and the keep-away (as wide as the shields) does not reach.

### Rosetta (amd64) against native arm64

Measured 2026-10-16. `bench/arch_compare.py gcd8 aes` runs the
same flows on the ORFS image (amd64, Rosetta) and LibreLane's (arm64), one
after the other on an idle 18-core Apple M5 Max, 12 threads (wall seconds):

| run | amd64 (Rosetta) | arm64 (native) | arm64 gain |
|---|---|---|---|
| gcd8 explore | 17.5 | 11.0 | 1.59x |
| gcd8 full, to signoff | 63.5 | 34.0 | 1.87x |
| aes explore | 197.7 | 137.1 | 1.44x |
| aes full | 3,723 | crashed in post-route repair (2,061 s in) | — |

- Per step (gcd8 and the explore runs), arm64 is 1.3–2.7x faster on
  OpenROAD work and 2.7–4.8x on signoff tools (KLayout GDS 4.8x, density, LVS
  and DRC ~3x). In the full aes run its CTS is slower (71 s against 46 s).
- One detailed-routing iteration on aes is 1.5x faster natively (225 s
  against 341 s); the whole aes route takes the same time (1,169 s against
  1,160 s) because the older router converges more slowly (662 violations left
  after its 5th iteration against 288).
- aes synthesis is ~650 s on both: the RTL equivalence check runs to its
  timeout (inconclusive).
- LibreLane's OpenROAD segfaults in post-route `repair_timing` on aes
  (`grt::GlobalRouter::addDirtyNet` from a cell swap), the same family as its
  crash on the OpenFrame example: a bug of that build. The amd64 image stays
  the default.
- Threads: with all 18 cores, Docker's VM leaves the host none; LibreLane's
  arm64 OpenROAD then takes 50 s for an aes global placement it does in 1.9 s
  at 12 threads (amd64: 3.2 s against 1.2 s), and detailed routing gains under
  5% past 12. On macOS the default is two thirds of the cores (`threads`;
  a step can take its own, e.g. `step_defaults: {place: {threads: 8}}`);
  12 threads also made the amd64 runs 3–34% faster than 18.

### Exploration mode

Measured 2026-10-19. `--explore` on gcd8 (ORFS image, Rosetta): 20 s from RTL
to a PPA first cut at global routing; achievable period 3.39 ns (295 MHz)
against the SDC's 10 ns, suggested `create_clock -period 3.56`, 1,637 µm² of
cells, 0.87 mW.

## Design files

A design is one YAML (or JSON) file; relative paths are relative to it. Three
ways to run it:
- CLI: `python3 -m sipath run my.yaml [workdir] [--resume] [--explore] [--config cfg.yaml]`.
- Python: `sipath.designfile.run("my.yaml", workdir)`, or build a
  `sipath.design.Design` and pass `flow_steps=[...]`.
- MCP: `open_run` with the file's path.

Every design-file key and step parameter is declared with its type, default
and doc (`sipath/schema.py`, `sipath/params.py`). A design file is checked
before anything runs: unknown keys, wrong types, values out of range and
missing required keys are errors naming the key path, and a missing input
file fails before OpenROAD starts. `docs/reference.md` (generated by
`python3 -m sipath docs`; a test fails when it is stale) lists every key and
parameter; `python3 -m sipath schema` writes a JSON Schema for editors.
Parameter defaults can be set per design or PDK with `step_defaults: {place:
{density: 0.5}}`.

```yaml
name: chip                  # run/report name
top: chip                   # top module in the netlist
mode: die                   # die (pad ring) | macro (soft macro)
pdk: sky130A                # a description in sipath/pdks (python3 -m sipath pdks) or a path to one
netlist: rtl/chip_flat.v    # flattened gate-level Verilog, or instead:
# rtl: [rtl/chip.v]         #   RTL, linted and synthesized by the synth step
sdc: rtl/chip.sdc
# mode: macro  ->  floorplan_def: fp.def (die, rows, pins), or die / core_utilization with pins placed
#                  by the flow; is_core: false limits routing to the embedded top layer
die: [0, 0, 1500, 1500]     # microns
core_margin: 241
pads:                       # ring order per edge; master from the PDK's IO set
- {inst: pad_clk, edge: west, order: 0, master: sky130_ef_io__gpiov2_pad_wrapped}
pad_kinds: {pad_clk: input}         # signal pads: input | output | analog
pad_supplies: {"^VCCD$": VDD}       # IO-cell supply pin regex -> net
macros:
- inst: u_sram
  lef: ip/sram.lef                  # or sram: <PDK SRAM name>, or views: <hardened views dir>
  lib: {ss: ip/sram_ss.lib, tt: ip/sram_tt.lib, ff: ip/sram_ff.lib}   # or one file, or null
  location: [262.2, 843.2]          # null = placed by the macros step
  orient: R90
  halo: 15
  supplies: {"^vpwr[amp]$": VDD, "^vgnd$": VSS}
  grid_stripes: [{layer: met3, width: 2, pitch: 30, offset: 5}]
  grid_connects: [[met3, met4]]
  auto_blackbox: true               # no lib: a black-box Liberty model from the LEF (opt-in)
  gds: ip/sram.gds                  # signoff DRC (and LVS when cdl is given)
  cdl: ip/sram.cdl                  # transistor netlist for LVS
  lvs: blackbox                     # no cdl: LVS checks only the connections to its pins
  abstract: true                    # signoff on the LEF abstract; the written GDS still carries the layout
analog_nets: [ana_in]               # routed first with an NDR, then frozen and guarded
analog_shield: [ana_in]             # grounded shield wires on both sides (analog_keepaway: adjacent layers too)
analog_supplies: [VDDA, VSSA]
pdn: {straps: [{layer: met4, width: 1.6, pitch: 54.28, offset: 27.14}], rings: false}
datapaths: [{name: add, kind: slices, cells: [[u.ra_0, u.fa_0], [u.ra_1, u.fa_1]], origin: [30, 30]}]
groups: [{name: io, prefix: u_out., kind: fence, box: [10, 10, 60, 40]}]
keepouts: [[0, 0, 100, 100]]        # boxes the macro placer avoids
macro_channel: 20                   # also macro_pin_clearance, analog_guard
dont_use: ["*_16"]                  # extra cell patterns; allow_excluded waives PDK exclusions
extra_lefs: [ip/blackboxes.lef]
extra_libs: [ip/blackboxes.lib]
plugins: [mypkg.sipath_ext]         # modules that register steps, scores, checks, harnesses
config: {threads: 8, parallel_cap: 4}   # run configuration (also SIPATH_* env, --config)
inputs: {generate: "mypkg.gen:write"}   # optional hook that writes generated inputs first
flow:                               # steps in order; each continues from the previous selection
- step: floorplan
- step: macros
  variants: [{method: random, seed: 1}, {method: random, seed: 2}, {method: cdlns}]
  score: macro_quick
  parallel: 3
- step: place
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: mypkg.checks:inst_count     # a step function by import path
- step: signoff                     # or single checks: - step: drc, - step: lvs, - step: timing, ...
```

An RTL design needs only `rtl`, `sdc`, and `core_utilization` or `die` (see
`examples/gcd8.yaml`). Field notes:
- **Steps.** A step may run under another name with `fn:` (`{step:
  signoff_fast, fn: signoff}`); `fn:` and `step:` also take `module:function`.
  Without a `flow:` list the default chain is used.
- **Omitted fields** come from the PDK description (layers, PDN defaults,
  corners, constraints, exclusions, signoff decks).
- **Corners.** A macro's `lib` maps a corner name, or its process prefix (`ss`,
  `tt`, `ff`), to a Liberty file.
- **Abstract macros** (`abstract: true`): DRC checks the design around the
  macro (not spacing to shapes the abstract does not show), LVS treats it as
  a black box, the written GDS still merges its layout, and `signoff.json`
  lists it under `drc.abstract_macros`.

## Examples

Each example is a design file in `sipath/examples/`; an `inputs.generate` hook
writes generated inputs under `examples/inputs/` (`examples/designs.py`,
`examples/ihp_add8.py`; also `python3 -m sipath gen-inputs`).

| example | what it shows |
|---|---|
| `gcd8.yaml` | RTL to signoff: lint, synthesis, floorplan from a utilization |
| `gcd8_sv.yaml` | SystemVerilog through slang (package, enum, packed struct, function) |
| `gcd8_pair.yaml` | two hardened gcd8 macros plus glue, signed off with their insides checked |
| `dp8.yaml` | soft macro from a floorplan DEF: bit-slice adder, array multiplier, a cluster, two analog feed-throughs (`ana_in[0]` shielded) |
| `dp8_hs.yaml` | dp8 on the sky130 high-speed library |
| `mcu8.yaml` | a 1.5 × 1.5 mm die: 39 `sky130_ef_io` pads, ChipFoundry SRAM/ADC/DAC macros, analog nets shielded, 3.3 V supplies |
| `sram8.yaml` | eight sky130 1 KB SRAMs (`sram:`), random search against the CD-LNS placer |
| `openframe_gcd8.yaml` | a 4-bit gcd in the OpenFrame harness, integrated into the frame |
| `ihp_add8.yaml` | soft macro on IHP SG13G2 (no tap or endcap cells; one RCX rule file) |
| `ihp_ring.py` | a pad ring closed on IHP SG13G2 with nothing configured |
| `plugin_example.py` | a plugin with one of each extension point (step, score, signoff check, design-file extension) |

**mcu8** in detail. ChipFoundry hard macros (provenance and commits in
`inputs/chipfoundry/SOURCES.txt`):

| instance | IP | views used | placement |
|---|---|---|---|
| `u_sram` | `CF_SRAM_1024x32` (Apache-2.0) | LEF, ss/tt/ff Liberty, GDS; LVS black box (no netlist published) | frozen, R90 |
| `u_adc` | `EF_ADCS1008NC`, 10-bit SAR front end (repo has no license file; the model header says Apache-2.0) | LEF, behavioral model, GDS; LVS black box (schematic SPICE kept for a macro-level LVS) | placed by the macros step |
| `u_dac` | `sky130_ef_ip__rdac3v_8bit` (Apache-2.0) | LEF, behavioral model, GDS; LVS black box (no netlist at the pinned commit) | placed by the macros step |

- The ADC front end has no controller, so the top contains a gate-level SAR
  controller (a one-hot pointer plus trial and result registers); a unit test
  simulates it against the vendor's comparator model.
- Generated inputs: a scaling datapath (multiplier and ripple adder), a 10-bit
  SRAM address counter, output registers; the SRAM test and power-switch pins
  tied as ChipFoundry specifies; `EF_ADCS1008NC.annotated.lef` (the vendor LEF
  has no pin DIRECTION/USE; a copy gets them from the vendor model, geometry
  untouched); `mcu8_blackboxes.lib` (pin directions, buses and loads for the
  ADC, DAC and pad, none of which ship Liberty).
- Power as measured: every macro supply pin is connected (SRAM by its met3/met4
  grid, the ADC/DAC 1.8 V tabs by straps, their 3.3 V tabs by the routed
  VDDA/VSSA); `check_power_grid` passes for VDD, VSS, VDDA and VSSA against
  terminals on the supply pads; worst IR drop about 0.4 mV on VDD (VDDA/VSSA
  show 0 mV only because the analog macros have no power model).
- The pad tie-off values follow the gpiov2 mode table; verify them against
  the IO datasheet before a tapeout.

## Modules

| module | role |
|---|---|
| `session.py` | `OpenRoadSession`: one process per attempt; Tcl in over stdin, result/errorInfo back through files; a Tcl error raises `TclError`. Docker and native runners. `FakeSession` for graph tests. |
| `tclquote.py`, `tcl.py` | quoting (no substitution possible) and Tcl-list parsing; Tcl generators and helpers |
| `scheduler.py` | `Flow.step`, `Flow.eco`, the attempt graph (`graph.json`), resume, cancel; each attempt loads its parent's ODB and writes its own |
| `config.py`, `params.py`, `schema.py`, `reference.py` | run configuration; step parameter and design-file declarations; the generated reference |
| `design.py`, `designfile.py`, `pdk.py`, `pdks/` | design spec (unset fields from the PDK), design files and `run`; PDK descriptions and their loader |
| `steps/` | one module per stage: `floorplan` (floorplan, macros, physical), `place` (place, cts), `analog_route`, `route` (route_digital, route_repair), `finish` (fill, extract, signoff and the check steps, views, integrate, ppa), helpers in `common` and `structure` |
| `synth.py`, `recipes.py`, `synth_recipes/`, `blackbox.py` | the synth step; ABC recipes and objective sets; black-box Liberty models |
| `chip.py`, `padplan.py`, `powerconnect.py` | pad ring (ICeWall); pad plan rules; straps from supply tabs to the grid |
| `macroplace.py`, `third_party/` | hard macro placement (random, mpl, cdlns) and its rules; the vendored CD-LNS placer |
| `analog.py`, `shield.py` | analog freeze, restore and coupling report; shields, guard and keep-away |
| `datapath.py`, `congestion.py`, `minarea.py` | structured placement and re-abut; congestion reports; post-route minimum-area safety net |
| `eco.py`, `equiv.py`, `liberty.py`, `netlist.py`, `wires.py` | ECOs; structural equivalence on Liberty functions; Liberty and Verilog models; wire re-encoding and restore after RCX |
| `signoff.py`, `lvs_full.py`, `klayout/` | signoff checks; full-device LVS; GDS stream-out, chip check and XOR scripts |
| `harness.py`, `harnesses/`, `templates.py`, `templates/` | frames (OpenFrame, Caravel) and their pinned sources; design templates |
| `registry.py`, `scores.py`, `demo.py` | extension points; named scores; stand-in scores for scheduler tests |
| `mcp_server.py`, `report.py`, `viz.py` | MCP server; run reports; schematic drawings |
| `limits.py`, `toolchain.py`, `toolchain.json` | regression limits; locked images and PDK release |
| `examples/`, `tests/`, `bench/` | example designs; unit and live tests with `limits.yaml`; benchmarks against ORFS, synthesis and architectures |

## Workarounds for this OpenROAD build

Found by running it:
- `place_pins -random` is a no-op: automatic pins are placed provisionally at
  floorplan without it, and again after global placement.
- `write_timing_model` takes `-scene`, not `-corner`.
- After `pin_access`, a `repair_antennas` following a second detailed route
  segfaults (`grt::GlobalRouter::findNets`): `pin_access` is off by default.
- A second `global_route` in one process fails or inherits stale state
  (GRT-0229, an "infinite usage" gcell): repair between global routes uses
  `-start_incremental`/`-end_incremental`, and post-route repair is its own
  step (a fresh process). Patch routes time out after 30 min.
- `clock_tree_synthesis -clk_nets` splits its value on spaces: names are
  passed space-joined, not list-quoted.
- With several corner libraries loaded, CTS cannot resolve its dummy-load
  cells (CTS-0118): multi-corner runs pass `-dont_use_dummy_load`. Liberty
  cells without a LEF master are set dont_use.
- The detailed router can end a segment with extension 0 at a same-layer
  corner (a diagonal pinch under the minimum width); signoff squares such
  corners (`corners_squared`).
- The global router places pins outside the die off its grid (harness pins
  are clipped for routing); the detailed router takes one pin per signal port
  (duplicate template pins keep one copy).
- `dbSBox_create` silently returns NULL for a box whose width and length are
  both odd (DBU): boxes are made even, and every create is checked.
- DPL's blocked-layer check flags a macro an analog wire lands on;
  `check_placement` tolerates exactly that.
- `make_fake_io_site` segfaults when a site already exists: sites are created
  once per session.
- Post-route setup repair with buffering segfaults inside `repair_timing`:
  it is sizing only; hold repair (buffers) runs separately.
- `add_pdn_ring -connect_to_pads` silently connects nothing to the sky130
  power-pad tabs, so the power connector does it.
- The detailed router refuses POWER nets (DRT-0305) and SIGNAL nets on POWER
  pins (DRT-0307): analog supplies are routed with their pins retyped SIGNAL
  for that route only.
- DRT re-reads its own L-shaped wires from a checkpoint as non-orthogonal
  (DRT-1010): before an ECO re-route, wires are re-encoded one path per
  segment.
- Timing-driven placement diverges inside a fence after its first timing
  iteration: with fences, `-keep_resize_below_overflow 0`.
- `dbWireDecoder` segfaults from Tcl, so analog routing is read back from a
  DEF written by the same process.
- KLayout 0.30.7 crashes on `connect_implicit` for the gpiov2 pad: LVS joins
  pin pieces through private per-pin layers; its SPICE reader upcases names,
  so black boxes are blanked under both spellings.
- The sky130 pads need `-rotation_vertical R180 -rotation_horizontal R90`; a
  check fails the attempt if a pad's core-side pin does not face the core.
- The router keeps only the minimum spacing from special wiring, whatever the
  analog net's NDR: hence the analog guard.

## License

SiPath is licensed under the [Apache License 2.0](LICENSE). Third-party files keep their own terms:
- `sipath/pdks/sky130/sky130_stdcell.lvs` derives from OpenROAD-flow-scripts (BSD 3-Clause);
- `sipath/klayout/stream.py` follows ORFS `def2stream.py` (BSD 3-Clause);
- the ChipFoundry IP views under `sipath/examples/inputs/chipfoundry/` keep their vendors'
  licenses, as listed in `SOURCES.txt`;
- the CD-LNS macro placer and the challenge's `Benchmark` under `sipath/third_party/`
  (Apache-2.0, unchanged; provenance in its `README.md`).
