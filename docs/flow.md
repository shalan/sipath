# How the flow works

Part of the [SiPath documentation](../README.md#documentation).

(`synth`) → `floorplan` → (`macros`) → `physical` → `place` → `cts` →
`route_analog` → `route_digital` → `route_repair` → (`fill`) → `extract` →
`signoff` → (`views`, `integrate`). `ppa` ends an exploration run; `drc`,
`density`, `lvs`, `lvs_full`, `antenna`, `timing`, `magic_drc`, `xor` and
`harness` run single signoff checks.

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
  at most two rounds; fillers. An incremental patch can lose neighbouring nets:
  the detailed router rips up nets near the patch that have no global-route
  guides and cannot route them back, and this OpenROAD build cannot run the
  global router twice in one process. The attempt then ends with `RetryWith`
  and the scheduler runs one fresh attempt with a full re-route
  (`max_patch_nets: 0`), recorded as its `retry_of`. The same retry follows a
  router failure inside the patch (aes: `DRT-0206 checkConnectivity`).
- `fill`: OpenROAD `density_fill` with the PDK's fill rules, up to the
  design's top routing layer (an embedded macro leaves the layers above to its
  parent), streamed to the PDK's fill datatypes.
- Each attempt writes timing (per corner), clock skew, power and congestion
  reports.

## Sweeps and ECOs
- `variants=[...]`, `parallel=N`, `score="<name>"` (default `auto`: setup slack
  when constrained, else wirelength); rejected attempts stay in `graph.json`.
  `fail_on_violation` makes a timing violation fail the attempt.
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
