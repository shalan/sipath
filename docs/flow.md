# How the flow works

Part of the [SiPath documentation](../README.md#documentation).

(`synth`) → `floorplan` → (`macros`) → `physical` → `place` → `cts` →
`route_analog` → `route_digital` → `route_repair` → (`fill`) → `extract` →
`signoff` → (`views`, `integrate`). `ppa` ends an exploration run; `drc`,
`density`, `lvs`, `lvs_full`, `antenna`, `timing`, `magic_drc`, `xor` and
`harness` run single signoff checks.

## What each step needs

The order above is the reference flow, not a rule. Each step declares the
capabilities it needs from its parent chain and those it adds, and a flow may
list steps in any order that satisfies them. A step whose chain lacks one is
refused before any tool starts, naming what is missing and the step that gives
it: a design-file flow is checked as a whole before its first step runs (also
by `sipath validate`), `Flow.step` checks each step, and MCP's `run_step`
checks before its job starts.

| step | needs | adds |
|---|---|---|
| `synth` | (RTL in the design) | netlist |
| `floorplan` | netlist (the design's, or synthesis') | floorplan |
| `macros` | floorplan | macros_placed |
| `physical` | floorplan | power_grid (grid, taps, endcaps) |
| `place` | floorplan, power_grid | placed |
| `cts` | placed | clock_tree |
| `route_analog` | placed | analog_routed |
| `route_digital` | placed | routed |
| `route_repair` | routed | |
| `fill` | routed | filled |
| `extract` | routed | parasitics |
| `signoff` | routed; parasitics when timing is among its checks | signoff, gds, powered_netlist |
| check steps (`drc`, `lvs`, `timing`, ...) | as signoff for their checks | powered_netlist; gds for the checks that stream it |
| `views` | signoff, gds, parasitics | views |
| `integrate` | gds | chip_gds |
| `precheck` | gds, powered_netlist | |
| `ppa` | placed | |
| an ECO | routed | (keeps its parent's) |

A plugin declares its own (`@registry.step("name", requires={"placed"},
provides={"..."})`, or `fn.requires` / `fn.provides` on a step imported by
path); `requires` may be a function of the step's parameters, as signoff's
is. A step that declares neither runs unchecked, with a warning, and the steps
after it are not checked either. Every attempt records its chain's
capabilities in `graph.json`.

## Floorplan and pads
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

## Hard macros (`macroplace.py`)
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
  The placer schedules its phases on a clock. With `placer_budget: work`
  (the default) SiPath gives it a work clock that advances by the reference
  cost of its work (`third_party/work_clock.py`), so the same input and seed
  give the same placement on any machine. On sram8 the result matches the
  wall clock's within 1% in cost and iteration counts, at 20 s and 120 s.
  `placer_budget: time` keeps the wall clock, whose results vary with the
  machine's speed and load.
- Rules for every method: halo, `macro_channel` (no sliver channel between
  macros or to the core edge), `macro_pin_clearance` (space in front of pin
  edges), `analog_guard` (extra space around analog macros and a standard-cell
  blockage band; rows are cut under it). Results compared under
  [Benchmarks](benchmarks.md#macro-placement).

## Power (`physical`)
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

## Placement and CTS
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

## Analog nets (`route_analog`)
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
  regression limits bound them. Results under [Benchmarks](benchmarks.md#analog-coupling).

## Routing and repair
- `route_digital`: global route; repair on global-route parasitics in
  incremental mode; `repair_antennas`; detailed route; `check_antennas` and up
  to three antenna repair rounds; RCX to rank the variants (`drc_then_slack`).
- `route_repair`: RCX; `repair_timing` (resize only, so structural equivalence
  is checked; hold buffers separately); re-route of the changed nets
  (`eco.patch_route`, a full re-route past a quarter of the nets); RCX again,
  at most two rounds; fillers. `drv_sizing: true` (off by default; on in the
  OpenFrame example and template) also sizes up, on extracted parasitics, the
  drivers of nets whose standard cells see slew or capacitance violations, then
  legalizes, patch-routes and extracts: placement estimates miss some local
  nets (a gcd packed at a strip's edge got wires twice their estimate). An
  incremental patch can lose neighbouring nets:
  the detailed router rips up nets near the patch that have no global-route
  guides and cannot route them back, and this OpenROAD build cannot run the
  global router twice in one process. The attempt then ends with `RetryWith`
  and the scheduler runs one fresh attempt with a full re-route
  (`max_patch_nets: 0`), recorded as its `retry_of`. The same retry follows a
  router failure inside the patch (aes: `DRT-0206 checkConnectivity`). A
  global-router failure inside `repair_timing` itself retries without
  post-route timing repair (`rounds: 0`), recorded in the retry's
  `retry_reason` and outputs ([workarounds](workarounds.md)).
- `fill`: OpenROAD `density_fill` with the PDK's fill rules, up to the
  design's top routing layer (an embedded macro leaves the layers above to its
  parent), streamed to the PDK's fill datatypes.
- Each attempt writes timing (per corner), clock skew, power and congestion
  reports.

## Metrics
- Every attempt records its metrics in `graph.json` (timing per corner,
  wirelength, DRC, design-rule violators, heatmap summaries, ...). OpenROAD
  steps also record `power_*_w`, the internal, switching, leakage and total
  power of the typical corner from the step's power report, and
  `power_total_w_<corner>`. They record `mem_peak_mb` too: the process's
  peak resident memory (VmHWM; Linux, so the Docker runs and native Linux).
- After every step `metrics.json` and `metrics.csv` (metric, value) sit
  beside `graph.json`. They hold the selected chain's metrics in METRICS2.1
  names, as ORFS and LibreLane write them: `<stage>__<category>__<metric>`
  per ORFS stage (synth, floorplan, detailedplace, cts, globalroute,
  detailedroute, finish), then each metric's last value without the stage
  (LibreLane's final metrics; synthesis's estimates keep their prefix).
  Examples: `cts__timing__setup__ws`, `timing__setup__ws__corner:ss_max`,
  `route__wirelength`, `route__drc_errors`, `power__total`,
  `design__max_slew_violation__count`, `design__lvs_error__count`,
  `detailedroute__mem__peak`, `runtime__total` (seconds),
  `heatmap__rudy__hot_share`. The names follow those conventions; they have
  not been checked against either tool's dashboards.

## Heatmaps
- `place`, `cts`, `route_digital` and `extract` collect OpenROAD's heatmaps
  (`gui::dump_heatmap`, headless) after the attempt's checkpoint is saved, in
  the same process (`Ctx.after_save`). RUDY and the routing map start the
  global router, so taking them earlier could change the checkpoint; taken
  after the save they change neither it nor the metrics read before it (gcd8:
  all 242 metric values identical with and without). A failed collection is
  recorded in `after_save_errors` and never fails the attempt.
- Maps (step parameter `heatmaps`, `[]` for none): `placement` (density),
  `rudy`, `routing` (congestion; route_digital and extract), `pin` (density),
  `power`, `irdrop` (extract, after its IR analysis: the core power net on the
  rail layer). Grids: bins sized to give at most 100 per side (10 µm at
  least); RUDY and routing follow the global router's gcells, with the
  routing steps' layer range and capacities set. A map OpenROAD cannot give
  is recorded as unavailable, with its reason.
- Each map is `heatmaps/<map>.csv` in the attempt directory, summarised in
  `outputs["heatmaps"][map]`: unit, peak, p95, the share of hot bins (at least
  90% for placement, 100% for RUDY and routing, else 80% of the map's peak)
  and up to three hotspots (hot bins merged into 4-connected boxes) with the
  instances (without fill, taps and endcaps) or nets inside them. Metrics
  `heatmap_<map>_peak`, `_p95` and `_hot_share`.
- The run report draws the selected attempts' maps (`heatmaps/<map>.svg`)
  with a table of their summaries; MCP's `query_heatmap(run, attempt, map,
  box?)` returns a map's summary and its bins, all or those in a box.
- RUDY and the routing map have one bin per gcell: 316,020 on the OpenFrame
  die. A map finer than 100 × 100 is stored, and drawn, coarsened to at most
  100 per side, each bin the largest value it covers. Its summary and hotspots
  still come from the full map (`bins` is the full map's count, `stored_bins`
  the file's). An OpenFrame run's heatmaps went from about 200 MB to 4.8 MB,
  with the same summaries.
- Collection time per step: under 0.1 s on gcd8, 1.6 to 2.4 s on aes, and
  1.8 to 4.7 s on the OpenFrame die, against steps of 7 to 52 s there. On
  `extract`, RUDY reads higher than on `route_digital` when `fill` ran in
  between: metal fill takes routing capacity.

## Adaptive rounds
- A flow item's `adapt: {rounds: N, ...}` turns what a step's selected
  attempt measured into new attempts of the same step. Each round asks the
  step's adapter (`registry.ADAPTERS`) for proposals, runs those not tried
  yet from the same parent (`Flow.extend`), and selects again over all the
  step's attempts. With `adapt` and no `score`, the step ranks by
  `hotspots_then_wns`: global-route overflow, then the summed share of hot
  bins in the placement, RUDY and routing heatmaps, then setup slack.
- The rounds stop after N, when nothing is hot, when the adapter proposes
  nothing new, or when no proposal scores better than the selection. Each
  new attempt records `adapted_from` and `adapt_reason`; the step records its
  `rounds` and why they stopped, so a resume replays them and runs nothing
  again. MCP's `adapt_step` runs more rounds on a step an agent ran.
- `place` (the built-in adapter, options `maps` [placement, rudy],
  `max_density` 70, `density_step` 0.05, `min_density` 0.3, `max_blockages`
  6) proposes two attempts:
  - **Local:** the same parameters plus partial placement blockages
    (`place` `blockages`: `create_blockage -max_density`) over the hotspot
    boxes not covered yet. They guide global placement only. The step removes
    exactly the blockages it created before legalization (fixed taps inside
    one fail `check_placement`), so the checkpoint carries none.
  - **Global:** a lower `density`, only while it stays above the cells'
    utilization, which global placement cannot spread below.
- Measured on gcd8 at 65% core utilization and density 0.8 (two rounds):
  - **Round 1:** blockages over three density hotspots took the hot share of
    the placement map from 20% to 8%, with setup slack 4.614 → 4.572 ns.
  - **Round 2:** no proposal scored better, so the rounds stopped.
  - **Signoff cost:** the spread placement left one net 0.03 ns over the slew
    limit after routing. With `route_repair` `drv_sizing` it signs off, at
    3.81 ns of slow-corner slack against 4.21 ns without the rounds.
  - **No win here:** this design had no routing congestion to relieve (RUDY
    had no hot bins).
- Measured on aes (35% core utilization, the benchmark's settings), which is
  congested: 8.8% of the RUDY bins over 100%, peak 136%. Blockages over the
  six hotspots lowered the RUDY peak to 124.5% but spread the congestion
  (8.8% → 9.0% hot RUDY bins, 2.7% → 3.3% hot placement bins), at −0.08 ns
  of setup slack; no lower density was proposed (0.50 is below the cells'
  53%). The round lost and the rounds stopped; the run then reproduced the
  baseline exactly (all 244 metric values). So far, on both designs,
  blockages move congestion rather than remove it.
- **Judged by the final result** ([strategies](benchmarks.md#strategies-judged-by-the-final-result)):
  on crowded gcd8, two rounds ended with 0.2 ns less slack at the typical
  corner, 0.4 ns at the worst one and 11 slew violations, against the defaults.
  Adaptive rounds therefore stay opt-in (`adapt:`), and are meant to run with
  a bounded search to extraction (`keep: 2`, `decide_at: extract`), where the
  baseline competes on the routed result and wins when the rounds did not
  help.

## Sweeps and ECOs
- `variants=[...]`, `parallel=N`, `score="<name>"` (default `auto`: setup slack
  when constrained, else wirelength); rejected attempts stay in `graph.json`.
  `fail_on_violation` makes a timing violation fail the attempt.
- **Forks.** The next step continues from the selected attempt; to carry on
  from another one, fork: `flow.fork(attempt, workdir)` (MCP `fork_run`)
  starts a new run in its own directory from any finished attempt, a rejected
  one included. The new graph holds that attempt and its ancestors, selected
  there and pointing at the original run's checkpoints (read, never written;
  each records `forked_from`), and continues with any step; the original run
  is not touched, and the fork resumes like any run. Measured on dp8: the
  placement its score rejected, forked and routed, while the original
  `graph.json` stayed byte-identical.
- **Bounded search.** A step's score judges what that step can measure; a
  placement that looks best may route worse. `keep: k` with `decide_at:
  <later step>` carries the step's best k attempts, plus the baseline (the
  variant with no parameters, else the first) when it is not among them,
  through the following steps as branches, each step named
  `<step>@<attempt>` (`route_digital@place.2`) in the same graph; the kept
  attempts get status `kept`. At `decide_at` the branches' attempts are
  compared by that step's score (or `decide_score`), the decision is recorded
  in `graph.json` (`decisions`, with the branches that failed before it), the
  winner becomes the run's head and the run continues from it. A branch that
  fails ends there; the others go on. A cancel or a run budget is not a lost
  branch: the run stops, no comparison is recorded, and `--resume` finishes
  the search (a resumed comparison reuses its recorded decision). With a
  selector score (a rule over the attempts, such as `synth_select`), the
  branches are the rule's choice, then its choice among the rest, and so on;
  the baseline is found by its parameters. Branches run one after
  another, each within the budgets; `--plan` shows the steps on branches.
  Measured with `keep: 2` to `extract`: on gcd8 the selected placement
  (density 0.75) also won after extraction (setup slack 4.364 against 4.249
  ns for the baseline); on dp8 the two kept placements (target density 0.55
  and 0.45, both passed to global placement) came out the same, to the HPWL
  and the slack, for a reason not established yet, and the tie went to the
  selected branch.
- ECOs `resize` (`replace_cell`) and `buffer` (`insert_buffer`): refused unless
  the parent is the selected head of its chain; structural equivalence before
  routing; rejected if setup or hold slack drops by more than
  `max_slack_loss` (0.05 ns); only the touched nets are re-routed (a full route
  past `max_patch_nets`). Equivalence reads cell functions from the session's
  Liberty, so drive-strength classes and buffers are derived, not named.

## Frames and hardened macros
- Harnesses (`harness: openframe`, `caravel`, or a plugin's): the harness sets
  the die and every template pin, creates the frame's supply pins (user
  supplies strapped to the core ring), the power grid and routing limits.
  Template pins overhanging the boundary are clipped for routing and restored
  at signoff; the `harness` check compares the routed wrapper with the
  template (die, every pin by name, layer and rectangle); loopback nets stay
  plain wires. `examples/openframe_gcd8.yaml` puts a 4-bit gcd on the
  OpenFrame's east-edge pads.
- Pin channels: with a `core_region`, every used template pin farther than
  `pin_reach` (300 µm) from it gets `pin_channel_rows` (2) rows at its height,
  from the pin across the region, so its first buffer sits next to the pin (a
  frame pin can be millimetres from the rows, and no buffer inside the region
  fixes the slew of the wire to it). Global placement sees the region as the
  core with the channels blocked, so the logic places as without them, and
  the straps keep their place. After repair and legalization, each such pin's
  buffer chain is spaced along the rows; then every signal pin's chain nets
  are repaired with a wire limit (place `pin_max_wire`, 400 µm) and repair
  runs again on the legalized wires. In the OpenFrame example `resetb_l`
  (south edge, 2.35 mm from the east strip) gets one, and no standard cell
  exceeds the slew limit (before: 2.19 ns). `pin_channels: false` disables
  them; embedded macros have no port buffers.
- `integrate` (after signoff): Magic puts the signed-off wrapper GDS,
  unchanged, into the frame's layout (as the frame's own build does) and
  writes the chip GDS; KLayout checks one top cell, the frame's die, the
  wrapper placed once at its origin and identical to the signed-off GDS.
  `seal_ring: true` adds the harness's seal ring (OpenFrame:
  `advSeal_6um_gen`), which ChipFoundry otherwise adds at tape-out. KLayout
  places it centred on the die (OpenFrame: at −6, −6 µm, as ChipFoundry's
  compositor implies); its opening (3,396.8 × 4,996.8 µm) is smaller than the
  die, so it sits on the die's outer band. The chip check requires it once,
  centred, touching none of the frame's shapes on any layer, and reports its
  opening.
- `views`: abstract LEF, a timing Liberty per corner (`write_timing_model`),
  GDS, LVS subcircuit, SPEF per corner, netlists with and without supply pins,
  and `macro.yaml`. `sipath harden <design> <dir>` runs a design through
  signoff and `views`; a parent lists `{inst: u0, views: <dir>, location: [x,
  y]}` (`examples/gcd8_pair.yaml`). Library cells the macro's GDS carries are
  kept as the library's, so LVS pairs them.
- Design templates: `sipath new <dir> --template openframe --set inputs=8
  --set outputs=6` writes a project (design file, wrapper with every frame
  port and the loopback ties, a starter core, SDC) that runs as generated.

## Exploration mode

`--explore` (or `run(..., explore=True)`) gives a first cut at PPA to set the
SDC before a full run: the design's own flow without the equivalence check,
detailed routing, post-route repair, fill, extraction, signoff, views or
integration, optimized at the typical corner only. Timing and power come from
global-routing parasitics (placement estimates when the flow stops before
routing). `ppa.json` / `ppa.md` and the run report give cell area,
utilization, WNS/TNS/WHS, total power, wirelength, and per clock the
achievable period (period − WNS), fmax and a suggested `create_clock` period
(achievable + 5%, the `ppa` step's `margin`).
