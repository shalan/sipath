<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/sipath-logo-dark.svg">
    <img src="docs/logo/sipath-logo.svg" alt="SiPath" width="420">
  </picture>
</p>

# SiPath — the path to silicon, steered by AI

SiPath is a Python physical-design flow that drives live OpenROAD sessions,
from RTL (Yosys synthesis) or a gate-level netlist to a **routed DEF, SPEF
and a signed-off GDS**. It is built to be steered by an AI agent:
- its MCP server lets an agent open runs, launch sweeps of variants, read
  metrics and reports, run ECOs and query checkpoints;
- the attempt graph keeps every path tried and selects the best by score;
- signoff (DRC, LVS, STA) is the judge of what counts.

A person can drive the same steps from the CLI or Python. PDKs and designs are
data: `sipath/pdks/sky130hd.yaml` and `sipath/pdks/ihp-sg13g2.yaml` describe the
PDKs, and a design file (YAML/JSON) describes a design and its steps.

```bash
python3 -m sipath run sipath/examples/dp8.yaml   # mode 1: soft macro, floorplan from a DEF (sky130A, open_pdks)
python3 -m sipath run mcu8                     # mode 2: MCU die, pad ring + hard macros (example name)
python3 -m sipath run ihp_add8                 # a soft macro on the second PDK (IHP SG13G2)
python3 -m sipath pdks                         # PDK descriptions available
python3 -m sipath run mcu8 runs/mcu8 --resume  # continue an interrupted run (finished work is kept)
python3 -m sipath run dp8 --config my.yaml -v  # run configuration file, INFO logging
python3 -m unittest sipath.tests.test_sipath
python3 -m sipath validate      # inputs + scheduler rules (stand-in sessions, no design)
python3 -m sipath draw <odb> <svg> [--ring]   # schematic SVG of a checkpoint
python3 -m sipath render <odb> <png>          # OpenROAD's own layout render
python3 -m sipath mcp           # MCP server on stdio
```

The MCP server exposes `open_run` (an example name or any design file;
`resume` picks up a workdir), `run_step`, `run_eco`, `job_status`,
`cancel_job`, `list_jobs`, `list_attempts`, `get_outputs`,
`query_checkpoint`, `draw_checkpoint` and `render_checkpoint` (plus
`list_designs`, `list_scores`).
- `run_step` / `run_eco` are jobs. In the foreground they send
  `notifications/progress` as attempts start and end (when the request has a
  `progressToken`). With `background: true` they return a job id at once.
- `cancel_job`, or `notifications/cancelled` for the request, kills the
  job's OpenROAD processes. Its attempts end `cancelled` with no checkpoint;
  the step can be run again (in a fresh attempt directory).
- Arguments are checked before anything runs (step, score, parallel,
  parameter names per step, ECO ops): a bad one is a tool error.
- Resources: each run's `graph.json` and every attempt's DEF, SPEF (per
  corner), reports, debug checkpoint and pictures, as
  `flow://<run>/<attempt>/<name>`.
- `query_checkpoint` runs Tcl on a fresh process with `write_*`, `exec`,
  `file`, `source`, `socket` removed and `open` limited to the session's own
  result files: a guard against accidental writes, not a security sandbox.

**Runs are resumable and configured.**
- `graph.json` records every attempt: params, parent, status, checkpoint,
  metrics, process, timing (phases and slowest Tcl commands), the
  OpenROAD transcript tail of a failed attempt, the run configuration and
  the image digest. `--resume` (or `resume=True`) rebuilds the graph: a step
  asked again with the same parent, variants and score returns its recorded
  result; attempts of an interrupted sweep that finished are kept and only
  the rest run. A selected attempt's directory is never deleted, and no
  attempt directory is reused.
- `sipath.config` (file, `FLOW_*` environment, a design file's `config:`
  section): threads, command timeout, parallel cap, container CPU/memory,
  image (`name@sha256:...` pins it; `require_digest` refuses anything else),
  debug checkpoints for failed attempts, log level. The library logs to the
  `flow` logger and is silent unless the caller configures logging.

## Where OpenROAD comes from

`find_openroad()` uses, in order: `$SIPATH_OPENROAD`, `openroad` on PATH, then
the ORFS docker image (`openroad/orfs:latest`, or `$SIPATH_OPENROAD_IMAGE`) if
it is already pulled. With docker, the host directories are mounted at the
same path, so file names in Tcl are the same inside and outside.

**Sky130 is first class** (`pdk: sky130A` or `sky130B`, `sipath/pdks/sky130A.yaml`):
every view comes from an open_pdks tree (`$PDK_ROOT`, default `~/.ciel`, as
installed by ciel/volare), mounted into the container:
- hd standard cells (fd + ef LEFs), the nom tech LEF, `tracks.info`,
  `sky130_ef_io` pads, the OpenRAM `sky130_sram_macros`;
- 9 signoff corners, tt/ss/ff (tt_025C_1v80, ss_100C_1v60, ff_n40C_1v95) ×
  nom/min/max interconnect. Each corner reads its own Liberty and RCX rules.
  Its estimation RC is the calibrated nominal table scaled per layer and per
  via by that corner's tech LEF (sheet and cut resistance, area and edge
  capacitance). Extraction runs once per RCX rule file (3 SPEFs), each read
  into the 3 corners that share it;
- the PDK's design rules (max transition 0.75 ns, max fanout 10, max
  capacitance 0.2 pF) applied before the design SDC. Its boundary defaults
  (inv_2 driving cell, 33.4 fF output load) apply to embedded macros only,
  since a die's ports are bond pads. Standard-cell slew/cap/fanout violators
  are counted (`drv_*`, `drv_clean`); IO-ring, pad, macro and port violators
  are listed apart (`drv_excluded`, by master type);
- the reference flow settings:
  - tap distance 13 µm;
  - clock nets on met3 and up;
  - CTS clkbuf_2/4/8 with a clkbuf_16 root;
  - decap + fill cells;
  - met4/met5 reference straps (153.6/153.18 µm) and core ring;
  - repair margins (setup 0.025 ns, hold 0.1 ns after placement/CTS and
    0.05 ns after global route).

**High-speed library**: `pdk: sky130A_hs` (`sipath/pdks/sky130A_hs.yaml`, extends
sky130A) runs on `sky130_fd_sc_hs` (unit site 0.48 × 3.33 µm, its own tracks,
tap/endcap/fill/CTS/diode cells and boundary defaults from the PDK's hs
config). Example: `sipath/examples/dp8_hs.yaml`.

**Cells with known layout problems** are never used: each Sky130 description
reads the PDK's `drc_exclude.cells` (53 hd, 51 hs cells: all `lpflow`, some
small/large drives, `xor3`/`xnor3`, `mux4_4`, `fa_4`, ...). Sizing, buffering
and CTS cannot choose them (`set_dont_use`), a netlist that instantiates one
is refused at validation (waive with `allow_excluded`), an ECO to one is
refused, and the PDK's special cells may not be on the list. Designs add their
own patterns with `dont_use`.

**Signoff** (`signoff` step, `sipath/signoff.py`) on the final database:
- physical, with KLayout from the image and the PDK's decks:
  - the routed DEF streamed to GDS with every cell's layout, supply nets
    labelled. A cell that no GDS defines (a LEF-only macro) makes DRC/LVS
    impossible and fails them. Each macro's GDS keeps its own cells:
    names that clash with a library or another macro are renamed, not merged
    (the ADC and DAC both carry a different `follower_amp`). Library GDS
    load first-definition-wins (the IO libraries repeat cells);
  - DRC: open_pdks `sky130A_mr.drc`, every marker counts. Markers are also
    counted by owner: `top` (the design's own geometry) or the macro or
    library whose cell they are reported in;
  - LVS: `sipath/pdks/sky130/sky130_stdcell.lvs` (derived from ORFS's
    sky130hd deck), against a reference netlist from OpenROAD's
    `write_cdl` after the supply connections are re-applied. The netlist
    takes only the PDK subcircuits the design uses (the PDK's
    `macro_sparecell` is broken), writes tie cells as the poly shorts their
    layout is, lists the supplies as ports, and gives the hs decaps'
    three-node transistors their fourth node. Black boxes are compared as
    their pins only (LEF abstract vs pin-only subcircuit) and listed in
    `lvs.blackboxed`. They are macros with `lvs: blackbox`, and the PDK's
    `signoff.lvs_blackbox` masters (Sky130: the IO cells, whose ESD/HV
    devices are outside the deck). A macro with neither a `cdl` nor
    `lvs: blackbox` fails LVS;
  - antennas (`check_antennas`).
- timing, in a fresh OpenROAD process from only the final netlist, Liberty,
  SDC and per-corner SPEF:
  - setup and hold on all 9 corners;
  - slew/cap/fanout on standard cells;
  - parasitic annotation: a loaded net without parasitics fails; dangling
    tie outputs and IO-ring abutment nets are listed;
  - unconstrained endpoints (analog and pad ports listed apart).

`signoff.json` holds every result; a failed required check fails the attempt.
Measured:
- dp8 and dp8_hs pass: DRC 0, LVS match, antenna 0, timing met on 9 corners.
- mcu8: the ChipFoundry macros are signed off as abstracts (`abstract: true`),
  and their GDS is merged into the written GDS (`inputs/chipfoundry/*/views`).
  - Signoff passes: DRC 0, LVS match (0 errors; the macros and IO cells are
    black boxes), antenna 0, setup +8.70 ns, hold +0.066 ns on 9 corners,
    design rules clean.
  - On the full layouts, KLayout's deck finds 95 markers inside the vendor
    cells: in the ADC, 86 `capm.2b_a`, 6 `hvtp.2` and 2 `nsdm.1`; in the
    DAC, 1 `nwell.6`. These are for ChipFoundry to address.

Found and fixed while adding signoff:
- The supply-tab connectors (`sipath/powerconnect.py`) had three faults:
  - they copied off-grid pin edges from the DAC and `ef_io` LEFs (1 nm off
    the 5 nm grid); shapes are now snapped inward to the manufacturing grid;
  - they put vias next to ones the analog router had already placed (cuts
    merged into illegal shapes); positions already covered are now left out
    and the arrays are centred;
  - they left a 0.1 µm notch to a same-net fill shape; such gaps are now
    filled.

  mcu8's top-level DRC went from 12 markers to 0.
- Found by LVS on mcu8 (DRC cannot see any of these):
  - A real VDD–VSS short: a connector's met4 landing patch crossed a VSS
    stripe. Landings now avoid other nets on every layer between the strap
    and its target. Each step that adds connectors fails if special shapes
    of two nets touch (`special_shorts`).
  - Floating ESD clamps: the plain `*_hvc` power pads leave DRN_HVC and
    SRC_BDY_HVC as tabs to be routed. mcu8 now uses the `*_clamped` pads,
    as Caravel does, whose clamps are wired inside.
  - 158 untapped row pieces beside the SRAM, whose n-wells floated. Row
    pieces shorter than 2 × tap distance + tap + endcaps are cut, and the
    `physical` step fails on any row without a tap (`rows_without_tap`).
- LVS deck and reference, for black boxes:
  - KLayout 0.30.7 crashes on `connect_implicit` for the gpiov2 pad, and
    when the extracted netlist is edited. Pin pieces are joined through
    private per-pin layers instead.
  - The SPICE reader upcases names, so black boxes are blanked under both
    spellings.
  - PDK LEF quirks: a pin declared twice, and overlapping pins (merged in
    the reference, reported as `abstract_shorts`).
- OpenRCX re-orders each net's wire and drops its RECT patches, the router's
  minimum-area patches among them. Every extraction now restores the routing
  from a DEF written just before (`steps.run_rcx`, geometry checked);
  before, routed DEFs lacked those patches (signoff DRC: m3.6).
- Cells added after the power grid was built (CTS buffers, fillers, tie
  cells, diodes) had unconnected supply pins in the database; the global
  connections are re-applied after each step that adds cells.
- A post-route minimum-area repair (`sipath/minarea.py`) stays as a safety
  net; it normally finds nothing. It never patches nets with a non-default
  rule or islands touching macros, pads or pins, and changes nothing at all
  when it would patch an implausible number of islands (more than 10 or 2 %
  of the nets): it reports instead.

`sky130hd` (the ORFS platform copy inside the image) remains as a fallback
when no open_pdks tree is installed; for a native binary set
`$SIPATH_PLATFORMS` to an ORFS `flow/platforms` directory.

On this machine there is no native `openroad`; runs use the image's
OpenROAD `26Q3-1867-g84e3ff1eb2` (linux/amd64 under emulation).

## Layout

| module | role |
|---|---|
| `session.py` | `OpenRoadSession`: one process per attempt; Tcl in over stdin, result/errorInfo back through files (OpenROAD's stdout re-emits bytes on lines > 4 KiB); a Tcl error, or a `break`/`continue` outside a loop, raises `TclError` (`return` is a normal completion). Values are quoted with `tclquote.q` (no substitution possible). `FakeSession`: canned answers for graph tests only. |
| `scheduler.py` | `Flow.step(name, fn, parent, variants, score, parallel)`, `Flow.eco(...)`, attempt graph (`graph.json`), resume, cancel. Each attempt: own session, loads parent ODB, runs, writes its own ODB. Best by named score is `selected`, others `rejected` (kept). Failures write no ODB. |
| `config.py` | run configuration (threads, timeouts, parallel cap, container limits, image digest, debug checkpoints, logging) |
| `steps.py` | `floorplan`, `macros`, `physical`, `place`, `cts`, `route_analog`, `route_digital`, `route_repair`, `extract` |
| `padplan.py`, `powerconnect.py` | pad plan rules; straps from supply tabs to the grid |
| `chip.py` | pad ring (ICeWall) |
| `macroplace.py` | hard macros: frozen, seeded random search with orientation search, or `rtl_macro_placer`; channel / pin-side / analog-guard rules; quick placement + global route score |
| `congestion.py` | global-route congestion reports (regions, overflow, worst box, nets) |
| `datapath.py` | column / array / bit-slice / block packing, re-abut after sizing, structure check |
| `analog.py` | NDR route first, then freeze as special wiring |
| `eco.py`, `equiv.py`, `liberty.py`, `wires.py` | resize/buffer ECO, structural equivalence on Liberty functions, wire re-encoding and restore after RCX |
| `signoff.py`, `klayout/stream.py`, `minarea.py` | signoff (GDS, DRC, LVS, antennas, independent STA); post-route minimum-area safety net |
| `pdk.py`, `pdks/*.yaml` | PDK description files and their loader (`load_pdk`) |
| `design.py`, `designfile.py` | design spec (unset fields from the PDK), design files and `run` |
| `tcl.py`, `netlist.py`, `scores.py` | generators, Verilog model, named scores |
| `examples/` | `dp8.yaml`, `mcu8.yaml`, `ihp_add8.yaml` (+ thin `.py` wrappers), `ihp_ring.py`; generators write the inputs under `examples/inputs/` |

## What each requirement maps to

- **Timing-driven placement**: `global_placement -timing_driven`,
  `estimate_parasitics -placement`, `repair_design`, `repair_timing`,
  `detailed_placement` (+ `check_placement`). Optional
  `routability_driven`. After legalization a global route with
  `-allow_congestion` measures congestion (`grt_overflow`,
  `grt_congested_gcells`, report kept); `congestion_margin` reserves a share
  of every layer for head-room. The `routability_then_wns` score ranks by
  overflow first, so a congested variant is rejected by score, not by a
  crash. A congested digital route fails with the report summarised
  (regions, overflow, worst box, nets). Layer adjustments come from the PDK
  and the design.
- **PDN**: followpin rails on met1 plus straps (`add_pdn_stripe`); rings only
  if `PdnSpec(rings=True)`. The live tests check the DEF has
  `SHAPE FOLLOWPIN` and `SHAPE STRIPE`, and no `SHAPE RING`.
- **Endcaps/taps**: `place_endcaps -left_edge/-right_edge`, `place_tapcells`.
  The step fails unless the endcap count is 2× the row (segment) count.
- **Analog nets**: NDR (`create_ndr`/`assign_ndr`), routed alone first
  (`set_nets_to_route`). The router's wiring is then rebuilt as `dbSWire`
  special wiring, the net is made `setSpecial` and `set_dont_touch`, and its
  stored guides are cleared. The digital route gets an explicit list of
  non-special nets. A signature of the analog shapes is compared before and
  after, and a change fails the attempt. Before extraction the exact regular
  wiring is restored from the DEF saved at freeze time (geometry compared),
  because OpenRCX skips special nets; `extract` fails if an analog net has no
  `*D_NET` in the SPEF. ECOs re-freeze first and restore again before their
  own extraction. CTS gets explicit `-clk_nets`; analog nets are rejected as
  clock nets, and their terminals must be unchanged after CTS (both modes).
- **Hard macros** (`macroplace.py`): `Macro(location=...)` is frozen
  (LOCKED). Otherwise `method="random"`: legal positions on the row/site grid
  from `random.Random(seed)` (some pushed against a core edge), each in every
  allowed orientation (`orients`), objective = macro-net HPWL + 4 × analog-net
  HPWL over placed terminals (pin-accurate, so orientation and pin side
  count); or `method="mpl"`: `rtl_macro_placer` with keepouts as temporary
  blockages, its result checked against the same rules. Rules:
  halo, `macro_channel` (no sliver channel between macros or to the core
  edge), `macro_pin_clearance` (space in front of pin edges),
  `analog_guard` (extra space around analog macros and a standard-cell
  blockage band; rows are cut under it). Every attempt is then scored the same
  way: quick gpl + detailed placement + global route of the standard cells
  (then unplaced again), `macro_quick` = least overflow, then quick HPWL +
  4 × analog HPWL. mcu8 sweeps 3 seeds, an orientation search and mpl.
- **Datapath**: `column` (one cell per row), `array` (abutted rows) and
  `slices` (generic bit-slice: `cells[bit][stage]`, stage columns aligned
  across bits, optional gaps and rows per slice), FIRM. Placed before taps,
  so taps step around them. After every sizing step (placement repair, CTS,
  post-global-route repair, ECOs, post-route repair) `reabut` re-packs with
  the current masters and a structure check fails the attempt unless every
  cell sits exactly where the packing puts it (`datapaths_intact`).
- **Groups**: `fence` is a `dbRegion` (EXCLUSIVE) plus a `dbGroup`. Cells
  that later steps insert into a fence join it if they drive a member, and
  are evicted otherwise. `cluster` is a `PHYSICAL_CLUSTER` group,
  shelf-packed at its centroid after gpl.
- **Sweeps**: `variants=[...]`, `parallel=N`, `score="<name>"` (default
  `auto`: setup slack when constrained, else wirelength); rejected attempts
  stay in `graph.json`. Each attempt records its process name and start/end
  times. Route attempts run OpenRCX before scoring, so `drc_then_slack`
  (DRC count, then the worse of setup and hold) ranks them on extracted
  parasitics; `timing_met` is recorded and `fail_on_violation` makes a
  violation fail the attempt.
- **ECOs**: `resize` (`replace_cell`) and `buffer` (`insert_buffer`).
  - Parent rules: an ECO is refused unless its parent is the selected head
    of the chain, so a sibling or stale parent is refused. A name already
    used by a step or attempt is refused (its checkpoints would be lost).
  - An ECO whose setup or hold slack (after extraction) drops by more than
    `max_slack_loss` (default 0.05 ns) against the parent is rejected; its
    checkpoint is kept for inspection.
  - Structural equivalence runs before routing. A failure raises, so no ODB
    is written.
  - Touched nets are the op nets plus the nets of every moved instance. If
    there are more than `max_patch_nets`, all digital wiring is ripped up for
    a full route. Otherwise only the patch is re-routed. Nets the router
    re-routes as collateral are reported.
- **Embedded macro**: `Design(is_core=False)` → routing up to the PDK's
  `max_layer_macro` (Sky130 met4, IHP Metal4), and validation refuses straps
  above it.
- **PDK and design as data**: a PDK file names views, routing stack, special
  cells (tap/endcap optional, tie hi/lo, diode, CTS buffers), supply pins,
  PDN/analog defaults, RC, tracks, corners and the IO library; the library
  code names no PDK layer or cell (a unit test greps for it). A design file
  holds the `Design` fields and steps; unset fields take the PDK's defaults.
  The same code runs IHP SG13G2 to a routed DEF + 3-corner SPEF.
- **Equivalence from Liberty**: cell functions, pin directions and buffers
  come from the Liberty loaded in the session (OpenSTA); cells are
  interchangeable when their truth tables match, so drive-strength classes,
  delay cells as buffers and physical-only cells are derived, not named.

## Step pipeline

`floorplan` → (`macros`) → `physical` → `place` → `cts` → `route_analog` →
`route_digital` → `route_repair` → `extract`.

**Timing closure (Phase 1).**
- Three corners when an open_pdks `sky130A` tree is available (`$SIPATH_SKY130A`
  or `~/.ciel/sky130A`):
  - ss: ss_100C_1v60 Liberty, max OpenRCX rules;
  - tt: tt_025C_1v80, nominal rules;
  - ff: ff_n40C_1v95, min rules.
- Macros may give a Liberty file per corner (the CF SRAM does). Metrics carry
  `wns_<corner>`/`whs_<corner>`; scores use the worst corner. `extract` writes
  one SPEF per corner.
- `place` first runs `repair_tie_fanout`: one tie cell per load.
- `route_digital`:
  1. global route;
  2. repair on global-route parasitics, in incremental global-route mode;
  3. `repair_antennas`;
  4. detailed route;
  5. `check_antennas`, then a post-detailed-route antenna repair loop (up to
     3 rounds);
  6. RCX, to rank the variants.
- `route_repair`:
  1. RCX;
  2. `repair_timing` (resize/buffer only, so structural equivalence is
     checked);
  3. re-route the changed nets (`eco.patch_route`);
  4. RCX again, at most 2 rounds;
  5. fillers.
- Each attempt writes timing (setup/hold, per corner), clock skew, power
  (per corner) and congestion reports, listed in `outputs["reports"]`.

**Power and IO (Phase 2).**
- Pad plan rules (`sipath.padplan`) are checked before any pad is placed:
  - supply pads abut their return pad;
  - a VDDIO/VSSIO pair on every edge with signal pads;
  - at most 8 outputs per IO pair;
  - core supply (VCCD/VSSD) on at least 2 edges;
  - analog pads on an edge with VDDA/VSSA and never next to an output.
- Every gpiov2 control pin is tied: core-domain pins from tie cells, `*_H`
  pins from the pad's own TIE_HI_ESD/TIE_LO_ESD. The floorplan fails on any
  floating pad input.
- The pad ring's supply buses are the core/IO nets (`Design.pad_supplies`).
  Top-level VDD/VSS/VDDA/... terminals sit on the supply pads' bond pins.
- **Power connector** (`sipath.powerconnect`). pdngen cannot reach these tabs
  (they sit under the cells' own upper-metal obstructions):
  - the power pads' core-side met3 tabs, strapped to the core ring;
  - the analog macros' 1.8 V tabs, strapped to the grid.

  Each strap avoids other nets and obstructions and gets a via stack.
- **3.3 V analog supplies** (VDDA/VSSA):
  - routed from a launch point at the pad tab to the ADC/DAC tabs, on
    met3–met5 with a 2 µm rule;
  - the IO-ring pins and the supply terminal are parked on a scratch net
    meanwhile;
  - then frozen as special nets and strapped to the pad.

  Every macro supply pin is then connected (SRAM 6/6, ADC 4/4, DAC 4/4).
- `extract` runs `check_power_grid` (against the pad terminals when they
  exist) and `analyze_power_grid` for VDD, VSS, VDDA and VSSA.
- The pad ring is generic:
  - corner site and row depths from the corner cell;
  - corner rotation from the edges its buses exit;
  - pad rotations by trial (core-side pins must face the core);
  - ring buses from the IO fillers.

  `examples/ihp_ring.py` closes the ring on IHP SG13G2 with nothing
  configured: rotations R0/R270/R0, against sky130's R180/R90/R180.

## Workarounds for this OpenROAD build (found by running it)

- With several corner libraries loaded, CTS cannot resolve its dummy-load
  cells (CTS-0118, even with `-library`): multi-corner runs pass
  `-dont_use_dummy_load`.
- Liberty cells with no LEF master (extra cells in corner libraries) are set
  dont_use at every load.
- A second full `global_route` in one process inherits stale state (an
  "infinite usage" gcell): repair between global routes uses
  `-start_incremental`/`-end_incremental`.
- A patch route after many incremental global routes in one process hung at
  0% CPU, so post-route repair is its own step (fresh process). Patch routes
  also time out after 30 min.
- `dbSBox_create` silently returns NULL for a box whose width and length are
  both odd (DBU). Boxes are made even, and every create is checked.
- DPL's blocked-layer check flags a macro an analog wire lands on.
  `check_placement` tolerates exactly that (hard macros on analog nets) and
  nothing else.
- `make_fake_io_site` segfaults when a site name already exists: sites are
  created once per session.
- Post-route setup repair with buffering segfaults inside `repair_timing`
  (extracted multi-corner parasitics). Post-route setup repair is sizing only;
  hold repair (buffers) runs separately.
- `add_pdn_ring -connect_to_pads` silently connects nothing to the sky130
  power-pad tabs, so the power connector does it.
- The detailed router refuses POWER nets (DRT-0305) and SIGNAL nets on POWER
  pins (DRT-0307). Analog supplies are routed with their pins retyped SIGNAL
  for that route only, and restored.
- Analog nets may not use met1/met2 over standard-cell rows (temporary routing
  blockages). Otherwise a wire over a cell's met1 pins blocks its pin access.

- DRT re-reads its own L-shaped wires from a checkpoint as non-orthogonal
  (DRT-1010). Before an ECO re-route, wires are re-encoded with
  `dbWireEncoder`: same geometry, one path per segment.
- Timing-driven gpl diverges inside a fence region after its first timing
  iteration. With fences, `-keep_resize_below_overflow 0` is passed
  (reweighting stays; the explicit repair runs after gpl).
- `dbWireDecoder` segfaults from Tcl, so analog routing is read back from a
  DEF written by the same process.
- The sky130 pads need `-rotation_vertical R180 -rotation_horizontal R90`. A
  check fails the attempt if any pad's core-side pin does not face the core.

## Specifying a design (the flow's inputs)

A design is one YAML (or JSON) file. Relative paths are relative to that
file. Three ways to run it:
- CLI: `python3 -m sipath run my.yaml [workdir] [--resume] [--config cfg.yaml]`.
- Python: `sipath.designfile.run("my.yaml", workdir)`, or build a
  `sipath.design.Design` yourself and pass `flow_steps=[...]`.
- MCP: `open_run` with the file's path.

`python3 -m sipath validate` checks every example file. `Design.validate()`
reports every missing input at once: a missing file fails the run before
OpenROAD starts.

Every design-file key and every step parameter is declared with its type,
default and doc (`sipath/schema.py`, `sipath/params.py`). From those
declarations:
- A design file is checked before anything runs. Unknown keys, wrong types,
  values out of range and missing required keys are errors that name the
  key path. PDK descriptions reject unknown keys too.
- `docs/reference.md` is generated by `python3 -m sipath docs`. A test fails
  when it is stale.
- `python3 -m sipath schema` writes a JSON Schema of the design file, for
  editors.
- The MCP tool `describe_step` returns each step's parameter schema.

Parameter defaults can be set per design or per PDK with
`step_defaults: {place: {density: 0.5}}`. The order is: the attempt's
variant, then the design file, then the PDK, then the declared default.

```yaml
name: chip                  # run/report name
top: chip                   # top module in the netlist
mode: die                   # die (pad ring) | macro (soft macro from a floorplan DEF)
pdk: sky130A                # a description in sipath/pdks (python3 -m sipath pdks) or a path to one
netlist: rtl/chip_flat.v    # flattened gate-level Verilog, or instead:
# rtl: [rtl/chip.v]         #   RTL, linted and synthesized by the synth step (Yosys)
sdc: rtl/chip.sdc
# mode: macro  ->  floorplan_def: fp.def   (die, rows, pins), or die / core_utilization with pins placed
#                  by the flow; is_core: false limits routing to the embedded top layer
die: [0, 0, 1500, 1500]     # mode: die; microns
core_margin: 241
pads:                       # ring order per edge; master from the PDK's IO set
- {inst: pad_clk, edge: west, order: 0, master: sky130_ef_io__gpiov2_pad_wrapped}
pad_kinds: {pad_clk: input}         # signal pads: input | output | analog
pad_supplies: {"^VCCD$": VDD}       # IO-cell supply pin regex -> net
macros:
- inst: u_sram
  lef: ip/sram.lef
  lib: {ss: ip/sram_ss.lib, tt: ip/sram_tt.lib, ff: ip/sram_ff.lib}   # or one file, or null
  location: [262.2, 843.2]          # null = autoplaced (macros step)
  orient: R90
  halo: 15
  supplies: {"^vpwr[amp]$": VDD, "^vgnd$": VSS}
  grid_stripes: [{layer: met3, width: 2, pitch: 30, offset: 5}]
  grid_connects: [[met3, met4]]
  auto_blackbox: true               # no lib: a black-box Liberty model is written from the LEF (opt-in)
  gds: ip/sram.gds                  # signoff DRC (and LVS when cdl is given)
  cdl: ip/sram.cdl                  # transistor netlist for LVS
  lvs: blackbox                     # no cdl: LVS checks only the connections to its pins
  abstract: true                    # signoff on the LEF abstract (DRC around it, LVS black box);
                                    # the written GDS still carries its layout
analog_nets: [ana_in]               # routed first with an NDR, then frozen
analog_supplies: [VDDA, VSSA]
pdn: {straps: [{layer: met4, width: 1.6, pitch: 54.28, offset: 27.14}], rings: false}
datapaths: [{name: add, kind: slices, cells: [[u.ra_0, u.fa_0], [u.ra_1, u.fa_1]], origin: [30, 30]}]
groups: [{name: io, prefix: u_out., kind: fence, box: [10, 10, 60, 40]}]
keepouts: [[0, 0, 100, 100]]        # boxes the macro autoplacer avoids
macro_channel: 20                   # also macro_pin_clearance, analog_guard
dont_use: ["*_16"]                  # extra cell patterns; allow_excluded waives PDK exclusions
extra_lefs: [ip/blackboxes.lef]
extra_libs: [ip/blackboxes.lib]
config: {threads: 8, parallel_cap: 4}   # run configuration (also FLOW_* env, --config)
inputs: {generate: "mypkg.gen:write"}   # optional hook that writes generated inputs first
flow:                               # steps in order; each continues from the previous selection
- step: floorplan
- step: macros
  variants: [{method: random, seed: 1}, {method: random, seed: 2}]
  score: macro_quick
  parallel: 2
- step: place
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: signoff                     # checks: [drc, lvs, antenna, setup, hold, drv, annotation, unconstrained]
```

An RTL design needs only `rtl`, `sdc`, and `core_utilization` or `die`
(see `examples/gcd8.yaml`):
- **`synth`, a tool step (Yosys, no OpenROAD process)**:
  - Lint first: implicit nets, hierarchy errors, undriven or multiply driven
    nets and combinational loops are errors. Warnings are counted;
    `lint_strict` makes them fatal.
  - Synthesis on the PDK's typical Liberty, flattened: flops by dfflibmap,
    logic by ABC with the SDC clock as the delay target (`abc: area` for
    area). Constants go on tie cells and port assigns get buffers. The PDK's
    dont_use and exclusion lists bind both mappers.
  - The netlist is checked: an unmapped cell (e.g. a latch) or an excluded
    cell fails the step.
  - Its netlist is the attempt's checkpoint. Variants and scores
    (`synth_area`) work as for any step.
- **Macros without a Liberty file**: with `auto_blackbox: true` (on the design,
  or per macro), the flow writes a black-box model from the macro's LEF into
  `<workdir>/models/` before the run:
  - pin directions, with indexed pins grouped into buses;
  - a default pin capacitance, and the area from the LEF;
  - no timing arcs; power and ground pins left out (LEF `USE`, or the macro's
    `supplies`).

  Yosys uses it to resolve the instance and OpenROAD links against it. Paths
  through the macro are not timed. The run's provenance lists each model and
  any pin whose direction had to be assumed (`inout`). It is off by default:
  without it, synthesis fails and the message names the macros and the ways
  out.
- **Floorplan without a DEF**: pins are placed at random, then by the cells
  after a first placement. PDK-default power straps too wide for a small
  core are scaled to it (recorded as `pdn_straps_scaled`).

Field by field:
- **Steps.** Step parameters are checked by name (`sipath.steps.STEP_PARAMS`).
  A step may run under another name with `fn:`, e.g.
  `{step: signoff_fast, fn: signoff}`.
- **Omitted fields.** They come from the PDK description (layers, PDN
  defaults, corners, constraints, exclusions, signoff decks). Without a
  `flow:` list, the default step list is used.
- **Corners.** The PDK lists the corners. A macro's `lib` maps a corner name,
  or its process prefix (`ss`, `tt`, `ff`), to a Liberty file.
- **Abstract macros.** With `abstract: true`, DRC and LVS see the macro as its
  LEF abstract (pins and obstructions):
  - DRC checks the design around the macro, not the vendor's insides. It
    cannot check spacing to shapes the abstract does not show.
  - LVS treats the macro as a black box.
  - The written GDS still merges the macro's GDS.
  - `signoff.json` lists these macros under `drc.abstract_macros`.
- **Black boxes in LVS.** Two sources:
  - a macro with `lvs: blackbox` or `abstract: true`;
  - PDK masters matching `signoff.lvs_blackbox` (for Sky130, the IO cells).

  In both cases LVS compares a LEF abstract against a pin-only subcircuit.
  `signoff.json` lists them under `lvs.blackboxed`.

## Inputs

Each example is a design file (`examples/*.yaml`) whose
`inputs.generate` hook writes its generated inputs under `examples/inputs/`
(`examples/designs.py`, `examples/ihp_add8.py`; also
`python3 -m sipath gen-inputs`).

**dp8** (soft macro, Sky130 HD): flat netlist with an 8-bit ripple adder
(placed as bit slices: a and b registers + full adder per bit), a 4×4 array
multiplier, an XOR accumulator and 2 analog feed-throughs. Also its
floorplan DEF (die, rows, pins) and SDC.

**ihp_add8** (soft macro, IHP SG13G2): an 8-bit registered ripple adder from
xor2/and2/a21o (the library has no full adder), placed as bit slices of six
stages, a cluster, 2 analog feed-throughs; Metal3/Metal2 pins on the ORFS
track grid. No tap or endcap cells in that library. Routed DEF + SPEF for
ss/tt/ff (one RCX rule file in the ORFS tree, so the three SPEFs are the
same parasitics; the Liberty differs per corner).

**mcu8** (die): 1.5 × 1.5 mm, 39 `sky130_ef_io` pads, and ChipFoundry hard
macros (provenance and commits in `inputs/chipfoundry/SOURCES.txt`):

| instance | IP | views used | placement |
|---|---|---|---|
| `u_sram` | `CF_SRAM_1024x32` (Apache-2.0) | LEF, ss/tt/ff Liberty, GDS; LVS black box (no netlist published) | frozen, R90 |
| `u_adc` | `EF_ADCS1008NC`, 10-bit SAR front end (repo has no license file; the model header says Apache-2.0) | LEF, behavioral model, GDS; LVS black box (analog; schematic SPICE kept for a macro-level LVS) | autoplaced (sweep) |
| `u_dac` | `sky130_ef_ip__rdac3v_8bit` (Apache-2.0) | LEF, behavioral model, GDS; LVS black box (no netlist at the pinned commit) | autoplaced (sweep) |

The ADC front end has no controller, so the top contains a gate-level SAR
controller: a one-hot pointer plus trial and result registers. A unit test
simulates it against the vendor's comparator model.

Other generated mcu8 inputs:
- The scaling datapath (multiplier and ripple adder), a 10-bit SRAM
  address counter and the output registers.
- The SRAM test and power-switch pins, tied off as ChipFoundry's pin
  description specifies.
- `EF_ADCS1008NC.annotated.lef`: the vendor LEF has no pin DIRECTION/USE,
  so a copy is written with them, taken from the vendor model. Geometry is
  untouched.
- `mcu8_blackboxes.lib`: pin directions, buses and loads for the ADC, DAC
  and pad (none ship Liberty).

**Power, as measured** (extract step, mcu8):
- Every macro supply pin is connected:
  - SRAM: met3/met4 macro grid;
  - ADC/DAC 1.8 V tabs: power-connector straps;
  - ADC/DAC 3.3 V tabs: the routed VDDA/VSSA nets.
- `check_power_grid` passes for VDD, VSS, VDDA and VSSA against terminals on
  the supply pads.
- Worst IR drop is about 0.4 mV on VDD. VDDA/VSSA show 0 mV only because the
  analog macros have no power model.

Also out of scope:
- The pad tie-off values follow the gpiov2 mode table. Verify them against
  the IO datasheet before a tapeout.

## License

SiPath is licensed under the [Apache License 2.0](LICENSE). Third-party files keep their own terms:
- `sipath/pdks/sky130/sky130_stdcell.lvs` derives from OpenROAD-flow-scripts (BSD 3-Clause);
- `sipath/klayout/stream.py` follows ORFS `def2stream.py` (BSD 3-Clause);
- the ChipFoundry IP views under `sipath/examples/inputs/chipfoundry/` keep their vendors'
  licenses, as listed in `SOURCES.txt`.
