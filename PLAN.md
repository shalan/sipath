# Plan: completing the SiPath flow library

(The library was called `flow` until 2026-10-15; renamed SiPath, package `sipath`.)

Starting point (2026-10-13): netlist → routed DEF + SPEF works for two Sky130 designs (dp8 soft macro, mcu8 die
with a ChipFoundry SRAM/ADC/DAC and a pad ring), on a live OpenROAD, with an attempt graph, sweeps, ECOs and an
MCP server; 36 tests pass. A review found correctness bugs, missing steps between routing and a usable SPEF, and
heavy Sky130/example coupling. This plan closes those gaps.

Rule for every item: it is done when a test proves it on the real OpenROAD (or a unit test where no binary is
involved), and the README says exactly what it does and does not do.

Sizes: S ≈ under half a day, M ≈ 1–2 days, L ≈ 3–5 days.

---

## Phase 0. Make current claims true (bugs) — DONE 2026-10-13 (47 tests pass, live runs included)

Results on the regenerated runs: every analog net has a `*D_NET` (dp8 2/2, mcu8 4/4, e.g. `ana.vref_hi`
0.104 pF); route variants ranked on RCX (`parasitics == "rcx"`); the macro score no longer sees the 161 unplaced
std-cell terminals, and the ADC/DAC moved from the bottom-left to beside their analog pads; ECO name collisions
and timing regressions are refused/rejected; the MCP query guard also blocks OpenSTA's command-abbreviation
handler, which had silently turned a removed `exec` into `execute_dft_plan`.

| # | Item | Size | Done when |
|---|------|------|-----------|
| 0.1 | Analog nets in the SPEF. Frozen special nets are skipped by RCX today (0 of 8 analog nets have a `*D_NET`). Keep their routing as regular wires protected by dont_touch + a `set_nets_to_route` exclusion list, or extract them separately and merge. | M | live test: every analog net has a `*D_NET` with non-zero C in both designs; digital route still leaves them unchanged |
| 0.2 | Macro score bias. During `macros` all std cells sit at (0,0). Score only macro↔pad and macro↔macro nets, or run a quick non-timing gpl inside the attempt before scoring. | S | unit test of the scorer; live test: score is unchanged when unplaced cells are moved |
| 0.3 | ECO safety: reject a name that already exists; compare the ECO against its parent and fail on a score regression (configurable tolerance). | S | FakeSession tests: name collision refused, parent ODB intact; regression rejected |
| 0.4 | Score post-route attempts on extracted parasitics: RCX inside each `route_digital` attempt; scores that include hold (`whs`); configurable fail-on-negative-slack after extraction. | M | live test: route variants ranked with `parasitics == "rcx"`; a forced-violation design fails |
| 0.5 | Unconstrained designs: a score that does not need slack (`drc_then_hpwl`) as the default when no clocks exist. | S | unit test with NaN/INF metrics |
| 0.6 | Tcl safety: one escaping routine for braces/brackets/backslashes; no interpolation inside `"..."`; a real Tcl-list parser (quotes, escapes). | S | unit tests with `a}b`, `clk[0]`, `"x y"` |
| 0.7 | Error paths: rc ≠ 0 (break/continue/return) is a failure; close errors are recorded; setup failures mark the attempt failed in `graph.json`; analog route requires a violation count; CTS analog check in both modes. | S | unit tests per path |
| 0.8 | Hard-coded numbers: DBU from the block; macro snapping from the row origin and rechecked against the core. Native runner without `$SIPATH_PLATFORMS` gives a clear error. | S | unit tests; validate on native-runner stub |
| 0.9 | MCP honesty: `query_checkpoint` either runs in a guarded interpreter (no `write_db`, `exec`, `file`) or drops `readOnlyHint` and says it can modify. | S | protocol test |

## Phase 1. Timing closure and checks before the SPEF — DONE 2026-10-13

Results:
- dp8 at 10 ns and mcu8 meet timing at ss/tt/ff after extraction.
- Post-route repair (now its own `route_repair` step) fixes real hold
  violations, e.g. mcu8 at ff: −0.066 → +0.052 ns.
- 0 antenna violations after the global- and detailed-route repair loops.
- One SPEF per corner; timing, skew, power and congestion reports per attempt.
- Tie fanout is 1.

Deviations from the plan:
- 1.1 uses incremental global-route mode around the repair; a second full
  global route in the same process inherits stale state.
- 1.2 runs in a fresh process (`route_repair`): patch routes after many
  incremental global routes hung.
- 1.3 disables CTS dummy loads when several corners are loaded (CTS-0118).
- dp8's clock went from 6 to 10 ns: its 9 ns ripple-multiplier path cannot
  meet 6 ns at ss.

| # | Item | Size | Done when |
|---|------|------|-----------|
| 1.1 | Repair after global route: `estimate_parasitics -global_routing`, `repair_design`, `repair_timing -setup -hold`, incremental GRT. Fillers move after detailed route. | M | live: mcu8 hold ≥ 0 after RCX across seeds; dp8 unchanged or better |
| 1.2 | Repair after detailed route: RCX → `repair_timing` → incremental route (reuses the ECO machinery) → RCX again, bounded iterations. | M | live: a tightened clock that fails at extract is fixed by this step or the attempt fails with the remaining slack |
| 1.3 | Multi-corner: PDK corners (ss/tt/ff Liberty incl. the SRAM's 12 corners), RCX min/nom/max rules, `define_corners`; scores use the worst corner; SPEF per corner. | L | live: 3 SPEFs written; reported WNS = min over corners |
| 1.4 | Antenna: `check_antennas`, `repair_antennas` + diode cell from the PDK, re-route; metric and fail threshold. | M | live: zero antenna violations reported on both designs, check result in metrics |
| 1.5 | Reports per attempt: `report_checks` (setup/hold, top paths), clock skew/latency, `report_power`, congestion; files listed in outputs and MCP `get_outputs`. | S | files exist and parse |
| 1.6 | Netlist hygiene: `repair_tie_fanout` with the PDK tie cell, optional `buffer_ports`, `remove_buffers` on input netlists. | S | live: no tie net with fanout > 1 |

## Phase 2. Power and IO (die mode) — DONE 2026-10-13

Results on mcu8:
- The pad list passes the pad rules: 39 pads, a VDDIO/VSSIO pair on every
  edge, core supply on 2 edges, analog pads grouped with VDDA/VSSA.
- No floating pad inputs.
- Supply terminals sit on the supply pads, and the pad tabs are strapped to
  the core ring.
- All 14 hard-macro supply pins are connected: the 1.8 V tabs by straps, the
  3.3 V VDDA/VSSA routed from their pads.
- `check_power_grid` passes for VDD, VSS, VDDA and VSSA; worst IR drop about
  0.4 mV.
- The pad ring is generic: geometry and rotations come from the library, and
  it closes on IHP SG13G2 with different rotations (R0/R270/R0 vs sky130's
  R180/R90/R180).

Deviations from the plan:
- 2.2 uses its own strap connector: `add_pdn_ring -connect_to_pads` silently
  connected nothing to the sky130 pad tabs.
- 2.3 builds no second pdngen domain. VDDA/VSSA are routed as wide special
  nets (2 µm, met3–met5) from a launch point at the pad, then strapped to the
  pad.
- The VDDA/VSSA IR drop is 0 mV only because the analog macros have no power
  model.
- 2.5 tie-off values follow the gpiov2 mode table (to be checked against the
  IO datasheet).

| # | Item | Size | Done when |
|---|------|------|-----------|
| 2.1 | Grid checks: `check_power_grid` (opens/unconnected instances) and `analyze_power_grid` IR drop with pad-located sources; metrics + fail threshold. | M | live: zero unconnected std cells/macros on VDD/VSS; IR report |
| 2.2 | Pad-to-core power: connect VCCD/VSSD pads to the core grid (core ring or straps to pad pins; evaluate pdngen pad connection vs `add_sroute_connect`). | L | live: core VDD/VSS nets connect to the VCCD/VSSD pads (connectivity check) |
| 2.3 | Analog macro supplies: explicit macro straps landing on each supply tab (location from LEF), second domain for 3.3 V (VDDA/VSSA) fed from its pads. | L | `macro_supply_pins.unconnected == []` for ADC/DAC |
| 2.4 | Pad plan rules: VDDIO/VSSIO pairs per edge, core supply pair on ≥ 2 edges, analog pads grouped with analog supplies and separated from switching outputs, max outputs per IO supply pair; checked before placement. | M | unit tests on pad lists; mcu8 pad list updated and passing |
| 2.5 | Pad control pins: per-master tie-off table (DM, ENABLE_H, …) applied by the netlist builder or checked as an input rule. | S | no floating pad input pins |
| 2.6 | Generic pad ring: corner site and row depths from the corner master; rotations chosen by trying candidates and keeping the one that passes the core-facing and ring checks; validated on a second IO library (IHP SG13G2 in ORFS). | M | live: ring closed on Sky130 and SG13G2 with no per-library numbers in code |

## Phase 3. Placement quality — DONE 2026-10-13

Results:
- 3.1 mcu8 sweeps five macro attempts in one step: seeded random search for
  seeds 1–3, the same with an orientation search (R0/MX/MY/R180), and
  `rtl_macro_placer`. Every attempt is scored the same way: quick gpl +
  detailed placement + global route of the standard cells, then
  `macro_quick` (overflow first, then quick HPWL + 4 × analog-net HPWL).
  Every autoplaced macro passes the channel (20 µm), pin-side (20 µm) and
  analog-guard (10 µm) rules. In the regenerated run seed 3 wins (cost
  53,340 µm); the orientation search has the shortest analog-net HPWL
  (2,730 µm, ADC rotated R180) but a longer total; mpl is last (68,981 µm).
  Re-running a seed gives the same locations and orientations.
- 3.2 Every place attempt runs a congestion-tolerant global route
  (`grt_overflow`). In the dp8 test a variant that reserves 95 % of every
  layer has overflow 110 and is rejected by `routability_then_wns`, although
  its slack is the best. A congested digital route fails with the report
  summarised. `routability_driven` gpl is a place option. Layer adjustments
  live in the PDK and the design.
- 3.3 Analog macros (connected to analog nets/supplies, detected) keep a
  guard to other macros and get a standard-cell blockage band (rows cut
  under it). Analog-net HPWL is a metric and part of the macro cost; the
  routed analog wirelength is reported by `route_analog`.
- 3.4 `slices` datapaths (bit × stage, stage columns aligned). dp8's adder
  is now 8 bit slices of [a register, b register, full adder]. A structure
  check runs after every resizing step. Live ECO: a grown stage-0 register
  moves stages 1–2 of all 8 bits (16 cells); routed DRC 0, structure intact.

Deviations from the plan:
- `cut_rows` ignores placement blockages, so rows under the guard band are
  split in OpenDB by the flow.
- `rtl_macro_placer` also places the standard cells; they are unplaced again
  so every method hands on the same kind of checkpoint.
- mpl has no seed in this build; its sweep variant is deterministic.


| # | Item | Size | Done when |
|---|------|------|-----------|
| 3.1 | Macro placement: orientation search, pin-side awareness, channel width rules, optional `rtl_macro_placer` candidate in the same sweep, score after a quick gpl (wirelength + routability proxy). | L | live: sweep compares random vs mpl vs oriented; best kept by score; result reproducible by seed |
| 3.2 | Routability: `-routability_driven` placement option, GRT congestion report, `-allow_congestion` diagnostics instead of a bare failure, layer adjustments in the PDK config. | M | live: congestion metric in graph; congested variant rejected by score not by crash |
| 3.3 | Analog-aware placement: keepouts/guard spacing around analog macros and analog net length in the macro score. | M | analog net length metric reported and used |
| 3.4 | Datapath structures beyond column/array (generic bit-slice spec), and re-abut checks after every resizing step including post-route repair. | M | unit + live tests |

## Phase 4. Generality: PDK and design as data — DONE 2026-10-13

Results:
- 4.1 `sipath/pdks/sky130hd.yaml` and `sipath/pdks/ihp-sg13g2.yaml`: views,
  routing stack, special cells (tap/endcap optional, tie hi/lo, diode, CTS
  buffers), standard-cell supply pins, PDN and analog defaults, RC, tracks,
  corners (a corner whose variables are unset is left out) and the IO
  library (masters → LEF, pad roles, corner and fillers). Design fields left
  unset take the PDK's defaults (`Design.resolve`). A unit test greps the
  library modules (`flow/*.py`) for `sky130`, `sg13g2`, `met1-5`, `li1` and
  `Metal1-5`: none.
- 4.2 Design files (YAML/JSON) with the steps to run:
  `python3 -m sipath run <file|example>`. dp8, mcu8 and ihp_add8 are YAML;
  `dp8.py`/`mcu8.py` only load them. Pad LEFs, corner and fillers come from
  the PDK's IO library. The MCP `open_run` takes a design file too.
- 4.3 Equivalence uses Liberty functions read from OpenSTA in the session:
  output pins from directions, function classes from truth tables, buffers
  from function, physical-only cells from having no outputs. Unit tests cover
  Sky130 and IHP cell sets (delay cell recognised as a buffer, a21o vs o21ai
  and dfrbp vs dfrbpq refused).
- 4.4 ihp_add8 on IHP SG13G2: routed DEF + ss/tt/ff SPEF, DRC 0, antenna 0,
  timing met in every corner, VDD/VSS grid connected, analog nets in the
  SPEF, routing up to Metal4 (embedded). A live resize ECO passes the
  Liberty-based check, and a function change is refused with no ODB.

Deviations from the plan:
- The ORFS IHP tree has one RCX rule file, so the three IHP SPEFs carry the
  same parasitics (the Liberty differs per corner).
- The example netlist generators (`examples/*.py`) still name their PDK's
  cells: they are example data, not library code.


| # | Item | Size | Done when |
|---|------|------|-----------|
| 4.1 | PDK description file (YAML/JSON): layers, site, DBU, tap rule, endcap/fill/tie/diode/CTS cells, power-pin patterns, corners, RCX rules, pad library facts. Remove Sky130 strings from code paths. | L | grep: no `sky130`/`met[1-5]` literals outside `pdks/` |
| 4.2 | Design description file: netlist, SDC, mode, floorplan DEF or die + pad list, macros (views, location/auto, supplies, grid), groups, datapaths, analog nets. `python3 -m sipath run design.yaml`. | M | dp8 and mcu8 expressed as YAML; examples become thin |
| 4.3 | Equivalence from Liberty functions (buffer/inverter/drive-strength classes derived, not regex). | M | unit tests on a second library |
| 4.4 | Second PDK end to end (IHP SG13G2 or GF180 from ORFS) with a small design. | L | live: routed DEF + SPEF on the second PDK |

## Phase 5. Robustness, resume, observability — DONE 2026-10-13

Results:
- 5.1 `Flow(..., resume=True)` / `--resume`: the graph is rebuilt from
  `graph.json`. Live test: a dp8 run is killed (SIGKILL) after the first of
  three place attempts finished. On resume, floorplan, physical and place.0
  are not run again (same start times), place.1 and place.2 run, and the
  selection equals an uninterrupted run's. A selected attempt's directory is
  never deleted; no attempt directory is ever reused (retries get `<k>_rN`).
- 5.2 Failed attempts keep the last 40 transcript lines in the graph; with
  `debug_failed` their database is written under `debug/`, never as a
  checkpoint and never usable as a parent.
- 5.3 `sipath.config`: threads, command timeout, parallel cap, container CPU
  and memory, image (digest pinning, `require_digest`), debug checkpoints,
  log level; from a file, `FLOW_*` variables, a design file's `config:`
  section or arguments. The graph records the configuration and the image
  id/digest that ran.
- 5.4 `flow` logger (silent by default, `-v` on the CLI). Per attempt:
  start / load / step / metrics / write_db seconds and the 8 slowest Tcl
  commands. Metrics on a synthetic 100k-instance placed design: 1.5 s
  (limit 10 s); the existing Tcl metrics already met it, so they were kept
  and a test guards them.

Found and fixed on the way:
- A Tcl wrapper failure (its result file could not be written) used to hang
  the session until the 1-hour timeout; it now fails at once.
- Re-creating a just-deleted attempt directory left Docker Desktop's mount
  showing a stale view; directories are no longer reused.


| # | Item | Size | Done when |
|---|------|------|-----------|
| 5.1 | Resume: rebuild a `Flow` from `graph.json` (attempts, selections, checkpoints); rerun skips completed steps; never deletes a selected attempt's dir. | M | test: kill mid-sweep, resume, same final selection |
| 5.2 | Failure forensics: optional debug ODB for failed attempts (kept apart from checkpoints), transcript tail in the graph. | S | test |
| 5.3 | Configuration: threads, command timeout, parallel cap, container CPU/memory limits, image digest pinning. | S | tests read config |
| 5.4 | Logging module, per-attempt timing breakdown, scalable metric collection (avoid all-instance Tcl loops on large designs). | M | 100k-instance synthetic design: metrics < 10 s |

## Phase 6. MCP and agent use — DONE 2026-10-13

Results:
- 6.1 `open_run` takes any design file (and `resume`). Live test on
  `ihp_add8.yaml` through the stdio server.
- 6.2 Tool calls run on worker threads. `run_step`/`run_eco` are jobs:
  progress notifications in the foreground, `background: true` + `job_status`,
  `cancel_job` / `notifications/cancelled`. Live test: `route_digital` is
  cancelled mid-run, its attempt ends `cancelled` with no checkpoint, its
  container is gone (`docker ps`), and the step then runs again to a routed
  DEF + SPEF. One job per run at a time.
- 6.3 Resources (`resources/list`, `resources/read`): graph, DEF, SPEF per
  corner, reports, debug checkpoints, pictures. Bad step, score, parallel,
  per-step parameter names (also checked for design files) and ECO ops are
  tool errors.


| # | Item | Size | Done when |
|---|------|------|-----------|
| 6.1 | Open any design file (Phase 4.2), not only dp8/mcu8. | S | protocol + live test |
| 6.2 | Long calls: progress notifications, cancellation (kills the attempt's processes), background runs with polling. | M | test: cancel a route mid-run, processes gone, attempt marked cancelled |
| 6.3 | MCP resources for DEF/SPEF/reports/pictures; tool errors for bad scores/params instead of internal errors. | S | protocol tests |

## Immediate list (2026-10-14) — superseded by the Backlog below

| # | Item | Size | Done when |
|---|------|------|-----------|
| I1 | Git: `git init`, `.gitignore` (runs/), first local commit (owner approval needed; nothing pushed). | S | `git log` shows the current tree |
| I2 | Live tests without open_pdks: skip with the reason, or fall back to the ORFS Sky130 copy (owner decision). | S | suite on a host without `~/.ciel` skips/passes, never errors |
| I3 | Pin the ORFS image by digest in the default config; graph records it (already) and a mismatch fails. | S | config test + live run with the pinned image |
| I4 | Benchmark against ORFS: synthesize two sky130 reference designs with ORFS's Yosys, run ORFS and this flow from the same netlist, compare WNS/TNS, wirelength, DRC, runtime. | M | table in README with both flows' numbers |
| I5 | Mixed 3.33 µm libraries: hs, ms, ls freely mixed (lp only in its own rows/regions, ss/ff corners only: no tt Liberty, different layout family). Family PDK file with several Liberty files per corner, a primary library for tap/endcap/fill/tie/diode/CTS cells, per-design allowed libraries for sizing (cross-library swaps checked by the Liberty equivalence), one track grid. | M | live: a netlist mixing hs/ms/ls routes DRC 0 with 9-corner SPEF; ECO swapping ms->hs passes equivalence |
| I6 | Minimum signoff: GDS (KLayout, in the image) + KLayout DRC with the open_pdks deck; Magic/Netgen need installing for Magic DRC and LVS. Required before claiming that mixed-library abutment is clean. | M-L | DRC report count 0 on dp8 / mixed design; owner lifts "no GDS" |
| I7 | Real pad timing: `sky130_ef_io` gpiov2 Liberty instead of the black-box pad Liberty in mcu8. | S-M | mcu8 timing paths through pad delays |
| I8 | Hardened-macro views: abstract LEF, timing Liberty, GDS of a finished soft macro (needed for OpenFrame). | M | dp8 views load into a parent design |
| I9 | OpenFrame harness (owner's first harness choice), after I6 and I8. | L | a design in the OpenFrame wrapper, routed, DRC clean |
| I10 | Packaging: `pyproject.toml`, generated inputs outside the package. | S | `pip install -e .` + tests |

Library facts behind I5 (checked in the open_pdks tree):
- hs, ms, ls and lp share the `unit` site (0.48 x 3.33 µm) and the VPWR/VGND/VPB/VNB pins. hd (2.72 µm) and hvl (4.07 µm) cannot share rows with them.
- Tech LEFs: hs = ms = lp; ls differs by one edge capacitance value.
- Tracks: ms = ls; hs differs on the li1 Y and met5 offsets.
- lp: no tt Liberty, no OpenLane config, no tracks.info.
- Well/implant abutment between libraries cannot be judged from LEF: it needs DRC on GDS (I6).

Cell-level check (2026-10-14: LEF, Liberty, the PDK's exclude lists, and the GDS edge layers read with KLayout):
- hs, ms and ls are the same 390 footprints: same names, sizes, power rails and n-well. ~380 of them also have
  identical signal-pin shapes (a cell can change library in place, even after routing); ~10 differ in pins only.
- Same 139 logic functions in all three (ls adds `decaphe`); about 365 usable cells each after the PDK's
  `drc_exclude.cells` (~50 per library, all lpflow cells and some small drives: never used).
- Cell-edge layers (n-well, nsdm, psdm, hvtp, lvtn, ...) are identical between ms and ls on all 391 cells and
  between hs and the others on 386 of 390. A cross-library neighbour presents the same edge as a same-library
  one, so hs/ms/ls rows mix without spacers as far as these layers show (DRC confirmation: I6).
- lp is a different layout family: half its cells are narrower, no pin shapes match, its well/implant edges
  switch at 1.655 µm instead of 1.66 µm and carry an hvtp band (74 edge signatures, none shared with
  hs/ms/ls). Next to an hs/ms/ls cell this leaves a 5 nm nsdm/psdm offset at the shared edge: treat lp as
  not row-mixable with the others (own rows/regions, or a spacer) until DRC says otherwise. It also has no tt
  Liberty and 3 double-height level shifters.

## HS library, exclusions, signoff — DONE 2026-10-14

Owner's request: enable the HS library (mixing deferred), exclude cells with
layout problems, add physical and timing signoff (lifts the "no GDS" rule).

Results:
- `sky130A_hs` PDK description and `dp8_hs` example: routed, signed off
  (setup +5.71 ns at ss_max vs +0.85 ns on hd; hold +0.091 ns).
- Exclusions from the PDK's own `drc_exclude.cells`, enforced in sizing/CTS
  (`set_dont_use`), netlist validation, ECOs and special-cell checks.
- `signoff` step: GDS (KLayout), DRC (sky130A_mr deck), LVS (deck derived
  from ORFS, reference CDL from `write_cdl`), antennas, independent 9-corner
  STA with design-rule, annotation and constraint checks. dp8 and dp8_hs: DRC
  0, LVS match, antenna 0, timing met. mcu8: timing/antenna only (LEF-only
  macros).

Found and fixed:
- OpenRCX dropped every RECT patch, the router's min-area patches included,
  in all routed DEFs until now. Routing is restored after each extraction.
- Cells inserted after the PDN had floating supply pins in the database.
- KLayout LVS on the PDK CDL needed: used-subcircuits only (broken spare
  cell), tie cells as shorts, supplies as ports, substrate from the cell
  area layer (the hd tap cell has no boundary layer), 3-node MOS repair (hs).

Open:
- mcu8's 8 `u_core.ro_*/D` endpoints are unconstrained in the independent
  STA (fed by SRAM DO through the macro's Liberty); to investigate.
- One partially annotated pin on dp8 (`u_out.rp_0/D`, an OpenRCX artifact on
  li1 pin access; LVS shows it connected).
- Physical signoff of mcu8 needs the ChipFoundry macros' GDS/CDL (a download).

## Sky130 first class: PDK fidelity — DONE 2026-10-13

Owner's scope: PDK fidelity (not the other cell libraries, not GDS/DRC/LVS
yet); OpenFrame is the harness to do first when a harness is in scope.

Results:
- `sipath/pdks/sky130A.yaml` / `sky130B.yaml` (extends A): every view from
  open_pdks (`$PDK_ROOT`, default `~/.ciel`): fd + ef cell LEFs, nom tech LEF,
  tracks.info, Liberty, RCX rules, `sky130_ef_io`, OpenRAM macros. dp8 and
  mcu8 run on it.
- 9 corners (tt/ss/ff × nom/min/max RC). Estimation RC per corner is scaled
  from the min/max tech LEFs (met2 R ×1.16 at max; via2 ×2.35). Extraction
  runs once per RC rule, giving 3 SPEFs shared by 9 corners.
- The PDK's design rules apply before the SDC; boundary defaults only for
  macros. Standard-cell DRV counted (dp8, mcu8: 0), ring/pad/macro/port
  violators listed apart (mcu8: 276).
- Reference settings: tap distance 13, clock routing from met3, CTS
  clkbuf_2/4/8 + clkbuf_16, decap + fill, met4/met5 reference PDN and core
  ring, repair margins. mcu8 hold went from +0.001 ns to +0.070 ns at ff_min.
- Results: dp8 setup +0.869 ns (ss_max), hold +0.192 ns (ff_min). mcu8 setup
  +8.81 ns (ss_max), hold +0.070 ns (ff_min). DRC 0, antenna 0, power grids
  connected, 39 pads and a closed ring on the open_pdks IO LEF.

Found on the way:
- Applying the PDK driving cell to a die's ports put an inv_2 on the bond
  pads (25 ns transitions, WNS −9.5 ns); boundary defaults are macro-only.

Not in scope yet: hs/ms/ls/hdll/hvl libraries, pad Liberty timing (the
pads use a black-box Liberty), GDS + Magic/KLayout DRC + Netgen LVS,
OpenFrame harness.

## Phase 7. Packaging, docs, CI — folded into the Backlog below

| # | Item | Size | Done when |
|---|------|------|-----------|
| 7.1 | `pyproject.toml`, generated inputs outside the package, versioned releases. | S | `pip install -e .` and tests pass |
| 7.2 | CI: unit tests always; live tests on the ORFS image (pinned digest). | M | green pipeline |
| 7.3 | README/docstrings match code; limitations section kept current; git history. | S | review checklist |

---

## Order and milestones

1. **M1: honest results** = Phase 0 (≈ 1 week). Nothing reported that wasn't produced, analog parasitics present,
   macro score fixed, ECOs safe.
2. **M2: usable SPEF** = Phase 1 (≈ 2 weeks). Timing closed after routing, multi-corner, antenna-clean, reports.
3. **M3: real die** = Phase 2 + 3.1 (≈ 3 weeks). Powered macros and pads, pad rules, better macro placement.
4. **M4: a library, not two examples** = Phases 4–7 (≈ 3–4 weeks). PDK/design as data, second PDK, resume, CI.

Phases 1 and 2 can run in parallel after M1; Phase 4.1 should start early in M3 so new code is written against the
PDK description rather than retrofitted.

## Open decisions for the owner

- Primary target: Sky130 only, or Sky130 + a second PDK (affects when Phase 4 starts)?
- 3.3 V domain for the analog macros: build it (Phase 2.3, large) or document the macros as unpowered for now?
- Native OpenROAD vs docker as the supported runtime (affects CI and performance).

## mcu8 macro views and chip signoff — DONE 2026-10-14

Owner's requests: get the missing macro views; then "use abstract layout for CF IPs".

Result (`runs/mcu8`, `python3 -m sipath run mcu8`): signoff passes.
- DRC 0. The three ChipFoundry macros are signed off as LEF abstracts (`abstract: true`)
  and their GDS is merged into the written GDS.
- LVS match, 0 errors. Black boxes: the 3 macros and the 12 IO cell types; everything
  else is transistor level.
- Antenna 0. Timing on 9 corners: setup +8.70 ns, hold +0.066 ns; slew/cap/fanout
  clean on standard cells.
- Runtimes (amd64 image under Rosetta): DRC 6 min, LVS 44 min, flow to extract 5 min.

Views: GDS of the three macros (ADC/DAC at the pinned commits, SRAM from the local clone)
and the ADC schematic SPICE, in `inputs/chipfoundry/*/views` (SOURCES.txt, SHA256SUMS).
No netlist is published for the SRAM or for the DAC at its commit; the ADC repository
has no license file. On the full layouts, KLayout's deck finds 95 markers inside the
vendor cells (ADC 94: capm.2b_a 86, hvtp.2 6, nsdm.1 2; DAC 1: nwell.6), to report to
ChipFoundry.

Real defects found by LVS, all fixed (DRC passes on all of them):
- VDD–VSS short: a supply connector's met4 landing patch crossed a VSS stripe. Landings
  now avoid other nets on every layer in between, and a special-net short check fails
  the step.
- Floating ESD clamps on all 12 power pads: the plain `*_hvc` pads need DRN_HVC and
  SRC_BDY_HVC routed. The pads are now the `*_clamped` variants (as in Caravel).
- 158 untapped row pieces beside the SRAM, with floating n-wells. Rows are cut below
  2 × tap distance + tap + endcaps, and a tap-coverage check fails the step.
- Placement then gave 0.78 ns slews against 0.75 ns (estimated vs extracted
  parasitics). repair_design now keeps 10 % slew/cap margin (PDK `repair_margins`).

Also fixed on the way:
- connector off-grid shapes, merged vias and notches (top-level DRC 12 -> 0);
- per-macro GDS cell namespaces, and missing cells detected by ghost status;
- the LVS deck for black boxes:
  - KLayout 0.30.7 crashes on `connect_implicit` and on editing the extracted
    netlist, so pin pieces join through private layers;
  - case-insensitive blanking;
  - PDK LEF quirks (a duplicate pin, overlapping pins).

Open:
- 8 unconstrained endpoints `u_core.ro_*/D` (SRAM DO -> registers), informational.
- The ADC's transistor-level LVS (macro level) needs a deck with hv devices, MIM caps and
  xhigh-po resistors.
- LVS takes 44 min under emulation; a native KLayout would cut that a lot.

## Typed configuration, then synthesis and lint — DONE 2026-10-15

Owner's request: items 6 and 5 of the LibreLane/ORFS comparison.

Typed configuration:
- Every step parameter is declared in `sipath/params.py` with type, default, unit,
  allowed values or range, and doc. Variants are checked by type and range.
  Parameters resolve in this order: variant, design `step_defaults`, PDK
  `step_defaults`, declared default. A step sees every declared parameter.
- Every design-file key is declared in `sipath/schema.py`. Unknown keys, wrong types and
  missing required keys are listed with their key paths. PDK descriptions reject unknown
  keys.
- Generated outputs:
  - `docs/reference.md`, from `python3 -m sipath docs` (a test fails if it is stale);
  - a JSON Schema, from `python3 -m sipath schema`;
  - the MCP tool `describe_step`.
- Found on the way: `extract` read `fail_on_violation`, but the parameter could not be
  set (it was undeclared).

Synthesis and lint:
- A `synth` tool step: the scheduler runs it without OpenROAD, and its netlist is the
  next step's starting design. It uses Yosys lint and synthesis with the PDK's
  dont_use and exclusions, and checks for unmapped or excluded cells. Its metrics are
  area, cells, flops and lint warnings. There is a `synth_area` score.
- Macro mode without a floorplan DEF: `die` or `core_utilization`; random pins, then
  pins placed after a first placement. PDK-default straps are scaled to small cores
  (as LibreLane's PDN auto-adjust does).
- Example `gcd8` (my RTL): from RTL, signoff passes (DRC 0, LVS match, antenna 0,
  setup +3.6 ns, hold +0.15 ns).
- Found on the way: `set_nets_to_route` resolves names as STA patterns. Nets with
  escaped names (Yosys bus bits such as `state\[0\]`) dropped silently out of global
  routing, and OpenROAD then crashed in `repair_design`. The flow now hands the
  router database nets.

Not done (next): RTL-vs-netlist formal equivalence (`eqy` is in the image); Verilator
lint (not in the image); SystemVerilog beyond Yosys's `read_verilog -sv`.

## Harnesses, fill, hardened views, second DRC engine, ORFS benchmark — 2026-10-15

- **P2.3 plugin registry** (done, pushed): steps, scores, signoff checks, harnesses
  and design-file extensions registered from a design file's `plugins:` or the
  `sipath.plugins` entry points.
- **H1 harnesses** (OpenFrame, Caravel; data in `sipath/harnesses/*.yaml`, frame
  files fetched from pinned ChipFoundry commits, see `sources/SOURCES.txt`):
  the design becomes the frame's wrapper (template die and pins, every frame
  supply pin, user supplies strapped to the ring, PDN, routing limits); the
  `harness` signoff check compares the routed wrapper with the template. Found
  by running it: pin-to-pin loopback nets were buffered into far rows (now plain
  wires), template pins overhang the die (clipped for routing, restored at
  signoff), the template lists four pins twice, CTS could not take bracketed
  clock names, and the router leaves zero-extension corners (squared at
  signoff). `examples/openframe_gcd8.yaml`: a 4-bit gcd on the east-edge pads.
- **P0.2 fill and density**: `fill` step (OpenROAD `density_fill`, sky130 rules
  in `sipath/pdks/sky130/fill.json`, up to the design's top routing layer), fill
  streamed to the PDK's fill datatypes, `density` signoff check (the PDK's
  deck; gating for top-level layouts, informational for embedded macros).
  gcd8: 1,413 fill shapes, DRC 0, density 0, LVS match.
- **P2.1 hardened views**: `views` step and `python3 -m sipath harden`; a macro
  entry `views: <dir>`. `examples/gcd8_pair.yaml` (two hardened gcd8 + glue)
  signs off: DRC 0, density 0, LVS match, timing met. Found: the GDS writer
  renamed the macro's copies of library cells, so LVS flattened the macro;
  copies identical to the library cell (per-layer XOR) are now the library's.
- **P1.5 second DRC engine**: opt-in `magic_drc` and `xor` checks (Magic from
  `docker/magic`, `config.magic_image`). gcd8: Magic DRC 0 (a planted 0.05 um
  met1 shape is caught as met1.1), XOR empty on 27 layers. dp8: Magic found 560
  latch-up violations (LU.2/LU.3) the KLayout deck does not check: the datapath
  packer moved the well taps out of abutted rows. Datapath structures now
  reserve tap slots (at most the PDK tap distance apart; whole columns in
  bit-slice structures) filled with well taps. dp8 XOR: two sub-0.1 um2
  differences at M2M3_PR vias next to wire ends (Magic builds vias from its
  contact rules) — open.
- **P1.4 ORFS benchmark**: `bench/orfs_compare.py` runs ORFS's sky130hd reference
  designs through both flows from the same RTL/SDC/utilization and measures
  both results with the same code. Found while doing it: a second global route
  in one OpenROAD process fails (post-route repair now re-routes once per
  process); ORFS's equivalence check binary dies under Rosetta (LEC_CHECK=0).

## Backlog by priority (2026-10-15)

Replaces the immediate list and Phase 7. Size: S ≈ hours to a day, M ≈ days, L ≈ a
week or more. "Owner" marks items that need a decision or approval first.

### P0: blockers (a tapeout, or trusting results, depends on them)

| # | Item | Size | Done when |
|---|------|------|-----------|
| ~~P0.1~~ | **DONE 2026-10-15.** Version control: renamed SiPath (`sipath`), git repo, pushed to github.com/shalan/sipath (public, Apache-2.0), commits as shalan. | S | done |
| P0.2 | Metal fill and density: a `finish` step (OpenROAD `density_fill` or KLayout fill, PDK fill rules), density checks in signoff DRC, fill in the written GDS. (Done 2026-10-15 for gcd8 and gcd8_pair; dp8/mcu8 runs pending, see above.) | M | dp8, gcd8 and mcu8 signoff with fill: DRC 0 including density |
| P0.3 | RTL vs netlist equivalence: `eqy` (in the image) on the synth step's netlist against the RTL, with the PDK Liberty cell functions. (Done 2026-10-15, bounded: Yosys miter + SAT from reset, 10 cycles; gcd8 equivalent, a wrong gate refused. A full proof needs register correspondence: open.) | M | gcd8 proven; a deliberately wrong netlist is refused |
| P0.4 | Full-device LVS: the PDK-maintained KLayout LVS deck (or Magic extraction + Netgen) with hv devices, MIM caps and poly resistors, beside the standard-cell deck. (Partly done 2026-10-16: open_pdks' `sky130.lvs` integrated as the opt-in signoff check `lvs_full` and `sipath lvs`; schematic preparation (aliases, device prefix, cell SPICE for black boxes, parallel MOS merge). The ADC LVS runs (10 s) but does not match: the vendor schematic has no `decoder3to8`. dp8 does not match under this deck (its standard-cell LVS still does): the deck merges parallel devices across cells in flat mode, extracts no tap diode or tie resistor, and fails the cells' well/substrate pins hierarchically. Open: reconcile the deck with the PDK cell netlists (or Magic + Netgen), get the ADC's decoder netlist from ChipFoundry.) | M-L | dp8 still matches; a macro-level LVS of the ADC against its schematic runs |

### P1: signoff speed, confidence at scale, reproducibility

| # | Item | Size | Done when |
|---|------|------|-----------|
| P1.1 | Native toolchain: KLayout/OpenROAD/Yosys on the host (Homebrew/Nix) through the runner, or a multi-arch image. Today the amd64 image under Rosetta makes mcu8 LVS take 44 min. **Owner** (supported runtime) (2026-10-15: runner takes an arm64 image (LibreLane 3.0.16) and native tools; gcd8 1.9x faster, signoff 2.8x; Homebrew's KLayout cask is disabled (Gatekeeper). Live suite on arm64 pending.) | M | mcu8 signoff time measured natively, both runtimes tested |
| P1.2 | Parallel signoff: DRC, LVS and STA in separate processes at once. | S | signoff wall time ≈ the longest check |
| P1.3 | Regression with metric limits: a limits file per design (WNS, DRC, wirelength, area, runtime) and one checker. Split `test_sipath.py` (now ~2,100 lines, 120 tests, ~55 min) into unit and live modules; slow tests marked and run in parallel. (Done 2026-10-16: `sipath/tests/limits.yaml` for gcd8, dp8, dp8_hs, ihp_add8, mcu8 and `sipath limits` (`--suggest` from a good run), checked by the live tests; tests split into `test_unit` (125 tests, 4 s), `test_live` and shared `common`, `test_sipath` still runs all; `sipath.tests.run_live -j N` runs the live classes in parallel processes with their times.) | M | `unit` < 1 min; live suite parallel; limits checked |
| P1.4 | Benchmark against ORFS (old I4): 2–3 sky130 reference designs from the same RTL through both flows; compare WNS/TNS, wirelength, DRC and runtime. | M | table in README |
| P1.5 | Second DRC engine: Magic DRC plus a Magic-vs-KLayout GDS XOR (as in ChipFoundry/Efabless precheck). (Checks done 2026-10-15; gcd8 clean in both; dp8 latch-up fixed, two XOR slivers open.) | M | dp8 clean in both; XOR empty |
| P1.6 | Reproducibility: image digest pinned by default (old I3), `pyproject.toml` with generated inputs outside the package (old I10), tool versions locked. (Done 2026-10-16: images locked by digest with their tool versions in `sipath/toolchain.json`, `sipath toolchain --check`; unpinned images refused unless `SIPATH_REQUIRE_DIGEST=0`; `pyproject.toml` with a `sipath` console script; example inputs stay committed and generators leave current files alone, so an installed package is never written. Verified in a throwaway venv (setuptools 84): `pip install -e .` and a wheel install used from outside the repo find the PDKs, decks, example inputs, templates and harness sources.) | S | `pip install -e .`; a run with an unpinned image is refused by default |
| P1.7 | CI: unit tests on every change, live tests nightly on the pinned image (needs P0.1). | M | green pipeline |

### P2: capability

| # | Item | Size | Done when |
|---|------|------|-----------|
| P2.1 | Macro hardening views (old I8): abstract LEF, timing Liberty, GDS, powered netlist and SPEF from a finished soft macro. (Done 2026-10-15: gcd8 views, gcd8_pair signs off.) | M | dp8/gcd8 views load into a parent design and sign off there |
| P2.2 | OpenFrame harness (old I9, owner's choice): wrapper, pads, seal ring, project integration (after P0.2 and P2.1). (H1 wrapper flow done 2026-10-15; H2 integration next: needs the frame's extra Caravel cells, download pending the owner's OK.) | L | a design in the OpenFrame wrapper, routed, signed off |
| P2.3 | Pluggable, finer steps: split `steps.py` (1,100 lines); checks as their own steps; user steps from an import path in the design file. | M | a custom step added without editing the package |
| P2.4 | Run report: one HTML/Markdown page per run (metrics per step, failures with report links, pictures). (Done 2026-10-15.) | S-M | report written by every run |
| P2.5 | Real pad timing (old I7): gpiov2 Liberty instead of the black-box pad Liberty in mcu8. (Done 2026-10-16: PDK `io.liberty` per process, read per corner for the pads a design uses; mcu8's black-box pad model removed; PAD->IN 1.35 ns tt, OUT->PAD 5.24 ns ss on the routed mcu8; live test added.) | S-M | mcu8 timing paths through pad delays |
| P2.6 | Mixed hs/ms/ls libraries (old I5). | M | a netlist mixing them signs off; ms->hs ECO passes equivalence |
| P2.7 | Front end: Verilator lint (needs the tool), SystemVerilog through slang, synthesis variants (abc scripts, retiming) as attempts. (Done 2026-10-15: Yosys lint + synthesis step, black-box Liberty models for macros without a lib. Done 2026-10-17: slang front end (`frontend`), Verilator lint (`verilator`, stubs for library cells), ABC goals delay/area/speed and `retime` as attempts with a pre-placement OpenSTA estimate and the `synth_wns_then_area` score; gcd8_sv (SV) synthesizes and is proven equivalent; live tests for both.) | M | an SV design synthesizes; a sweep over abc goals picks by score |
| P2.9 | Synthesis front end from shalan/synth_flow: judge at the slow corner with wire load, SDC boundary model for ABC, no delay target by default, register names kept, its 18 measured recipes and objective sets, its selection rule (least area meeting timing, else the knee), front-end variants (Booth, prefix adders), its 16-design bench. (Done 2026-10-17: all of it, plus an in-step recipe sweep with equivalence on the winner, slow-corner mapping by default, `bench/synth_bench.py`: 12/16 designs meet timing with the defaults, 13/16 with the front-end sweep, against 6/16 for synth_flow's pre-repair results. Open: hierarchical bottom-up synthesis, several libraries at once (with P2.6), gate-level simulation.) | M | bench table in README; defaults justified by measurements |
| P2.10 | Macro placer from the Partcl/HRT Macro Placement Challenge 2026, CPU only. (Done 2026-10-18: Team Macro Polo's CD-LNS, 8th and the best-ranked CPU entry, vendored unchanged in `sipath/third_party/` and run as `macros` method `cdlns` in a separate PyTorch Python (`setup-placer`); export with clustered movable soft macros, legal start, rule-aware spacing, snap and check; Verilog reader takes part-selects, vectors and constants on instance pins; escaped names (`bank[0].u_ram`) found in OpenDB; `examples/sram8.yaml` and a live test. Measured: random search still wins on `macro_quick` (sram8 574k vs 628k µm, overflow 7 vs 39; mcu8 53k vs 58k). Open: a routability term in the placer objective or channel-width sweep, the challenge's GPU leaders as an optional method.) | M | sram8 and mcu8 numbers in README |
| P2.11 | Analog nets: coupling report and shielding. (Done 2026-10-18: `extract` reports per analog net total C and coupling to digital and analog nets from every corner's SPEF, with step and regression limits; `analog_shield` draws grounded min-width shields on both sides after the frozen analog route (NDR room, rule-based clipping, ground by touch, crossing via or tie strap, floating pieces dropped, coverage reported, short check), `analog_keepaway` blocks the adjacent layers. Guard obstructions keep the NDR spacing against later digital wiring (the router keeps only the minimum from special wiring; mcu8's `vref_lo` had a digital net 0.25 µm away for 340 µm). dp8: 93 % coverage, 2.8 → 0 fF; mcu8: vref_lo 31.2 → 0.09 fF, signoff clean. The special-wire freeze stays: extraction already restores the regular wiring first. Open: vref_hi's 9.7 fF from other-layer runs along the ADC pins (a wider adjacent-layer keep-away), coaxial (above/below) shields, differential pairs and matching.) | M | dp8 shielded and signed off; mcu8 vref coupling measured with and without |
| P2.8 | Tests without open_pdks (old I2): skip with a reason or fall back to the ORFS sky130 copy. **Owner** | S | suite on a host without `~/.ciel` skips, never errors |

### P3: polish and follow-ups

| # | Item | Size |
|---|------|------|
| P3.1 | The 8 unconstrained endpoints `u_core.ro_*/D` in mcu8 (SRAM DO paths). | S |
| P3.2 | Report the 95 DRC markers inside the ChipFoundry ADC/DAC layouts (draft for the owner). **Owner** | S |
| P3.3 | gf180mcu PDK description. | M |
| P3.4 | Power estimation from switching activity (VCD/SAIF) next to the IR-drop check. | M |
| P3.5 | Typed-config follow-ups: defaults in step code removed (the declarations are the source); PDK description schema and docs like the design file's. | S |

Suggested order: P0.1 → P0.2 → P1.2 → P0.3 → P1.1 → P1.3/P1.7 → P0.4 → P2.1 → P2.2. P1.2 and
P0.3 are small and lift every later iteration; P1.1 needs the runtime decision.
