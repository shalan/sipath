# Benchmarks

Part of the [SiPath documentation](../README.md#documentation).

Each table says when it was measured. The ORFS comparisons (both ways) and the
aes step table are written from the benchmark records in `bench/results/`
(`python3 bench/tables.py`), as is the synthesis fallback table in
[synthesis](synthesis.md); the synthesis bench, macro placement, analog
coupling, arm64 and exploration tables are still transcribed from their runs.

## Against ORFS

`python3 bench/orfs_compare.py gcd aes` runs ORFS's own sky130hd reference
designs through ORFS (`make` in the ORFS image) and through SiPath (sky130A,
the standard flow with fill) from the same RTL, SDC and core utilization. Both
final layouts are measured by the same code: setup WNS/TNS at the typical
corner (open_pdks tt Liberty, the design's SDC, each flow's own SPEF), routed
wirelength from the final DEF, logic cells and their area (no fill, tap, decap
or diode), the detailed router's DRC count and the PDK's KLayout DRC deck.
Wall time is the whole run, signoff included for SiPath, on an Apple-silicon
Mac under Rosetta (amd64 image), 12 threads: indicative only. ORFS's results
are its 2026-10-16 runs (same image), measured again.

**Completion is not signoff.** The benchmark appends `signoff` with
`fail_on_violation: false`, so a SiPath run finishes, and is measured, even
when signoff fails. The last column is the verdict of the run's
`signoff.json` (every required check: DRC, density, LVS, antennas, setup and
hold on all nine corners, slew/cap/fanout, annotation). ORFS has no signoff
step to compare. The tables on this page are written from the benchmark
records in `bench/results/` (`python3 bench/tables.py`; a test fails when a
table and its records disagree): each record holds the run's metrics, its
verdict, its step times, the SiPath commit (null for runs from before
commits were recorded), the image digest and open_pdks release, the machine,
the inputs' hashes, the overrides and the run directory it came from. The
SiPath rows are the current default configuration's runs from
`bench/repeat_compare.py`, measured by the same code as ORFS's: how many
completed, and the median of those that did.

<!-- table:orfs -->
| design | flow | measured | WNS (ns) | TNS (ns) | wirelength (µm) | cells | cell area (µm²) | DRC (router / KLayout) | wall (s) | runs | signoff |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gcd (1.1 ns) | ORFS | 10-15 | −1.465 | −65.09 | 11,786 | 473 | 5,319 | 0 / 0 | 321 | 1 | — |
| gcd (1.1 ns) | SiPath | 10-21 | −1.083 | −46.62 | 8,061 | 473 | 4,975 | 0 / 0 | 369 | 1 | failed 1/1: setup |
| aes | ORFS | 10-15 | −0.196 | −0.67 | 588,211 | 15,396 | 124,637 | 0 / 0 | 2,724 | 1 | — |
| aes | SiPath | 10-21 | +0.233 | 0.00 | 583,437 | 19,123 | 136,273 | 0 / 0 | 4,207 | 1 of 2 (DRT-0206) | failed 1/1: drv, setup |
<!-- /table -->

- **gcd** (a 1.1 ns clock, beyond what sky130 HD closes): SiPath ends 0.38 ns
  ahead of ORFS in WNS and 28% in TNS, with 32% less wire and 6% less cell
  area. Its wall time (369 s, one run, signoff included) is longer than ORFS's
  321 s; the former `knee` configuration's two runs, with the same netlist,
  took 251 and 253 s, so the difference is the machine's load. Signoff fails on setup (worst −3.079 ns
  over the nine corners; the clock is out of reach), with DRC 0, LVS match and
  antennas 0. Its post-route repair
  lost four nets in the incremental patch and was retried with a full re-route
  in a fresh process (see [How the flow works](flow.md#routing-and-repair)).
  With the 2026-10-16 defaults SiPath was 1.0 ns behind; the difference is the
  synthesis front end (slow-corner mapping, the recipe sweep).
- **aes**: timing met at tt (WNS +0.233 ns against ORFS's −0.196), with 1%
  less wire and 9% more cell area. Synthesis maps at the slow corner, where
  aes's clock cannot be met, so the selection takes the fastest netlist
  (`delay_map_resyn`, `synth_fallback: best_slack`, the default since
  2026-10-21; with the former `knee` default: −0.010 ns, 650,995 µm of wire,
  148,639 µm², see [synthesis](synthesis.md#the-fallback-when-nothing-meets-timing)).
  Wall time measured with other runs sharing the machine (the same `knee`
  netlist took 9,674 s once and 5,614 s the next time).
- **One aes run of two failed.** The first `best_slack` run stopped in
  post-route repair: the detailed router's connectivity check failed
  (`DRT-0206`) after an incremental patch. The second, from the same inputs,
  completed and is the row above.
- **aes signoff fails** on setup at the slow corners (−2.383 ns at ss_max;
  +0.222 ns at tt and +0.921 ns at ff; ORFS does not check ss) and on 34 slew,
  1 capacitance and 2 fanout violations. DRC 0, LVS match, antennas 0, hold
  met on all nine corners.
- **One input, one result.** A first aes run the same day closed at −0.328 ns
  (6,679 s) and failed LVS on `sky130_fd_sc_hd__o211ai_4`, a cell sizing chose
  that the KLayout deck cannot pair; excluding that cell (now one of seven)
  changes what sizing may pick, which explains the difference. Repeated gcd
  runs are identical at every step, down to the final WNS, so the flow itself
  is deterministic there; aes's repeats gave the same netlist every time, the
  same metrics for `knee` and `least_area`, and one routing failure for
  `best_slack` (above).
- Both flows route to met5 with the design's own routing adjustments (aes: 30%
  on every layer). ORFS's equivalence check (kepler-formal) is disabled
  (`LEC_CHECK=0`): its binary dies under Rosetta.

aes by step (seconds), from the same runs as the table: ORFS's stage times
from its own logs, SiPath's every attempt of each step (its 683 s of
synthesis include 600 s of an equivalence check that timed out, and its
post-route repair includes 393 s of an incremental patch that failed,
`DRT-0206`, before the full re-route it retried with):

<!-- table:aes_steps -->
| step | ORFS | SiPath |
|---|---|---|
| synthesis | 88 | 683 |
| floorplan, power, analog routing, fill | 9 | 17 |
| place | 83 | 115 |
| CTS | 26 | 72 |
| routing (ORFS: global, detailed, fill cells) | 2,292 | 967 |
| post-route repair | – | 1,419 |
| extraction | – | 26 |
| signoff (SiPath: DRC, LVS, nine-corner STA) | 51 | 908 |
| **tools, summed** | 2,549 | 4,207 |
| **whole run (wall)** | 2,724 | 4,207 |
<!-- /table -->


## SiPath's own designs through ORFS

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

<!-- table:sipath_designs -->
| design | flow | WNS (ns) | wirelength (µm) | cells | cell area (µm²) | DRT / KLayout DRC | wall (s) | signoff |
|---|---|---|---|---|---|---|---|---|
| gcd8 (RTL) | ORFS | 7.165 | 4,220 | 211 | 1,973 | 0 / 0 | 168 (tools 66) | — |
| gcd8 (RTL) | SiPath | 7.154 | 3,460 | 202 | 1,667 | 0 / 0 | 64 | passed |
| dp8 | ORFS | 5.984 | 5,576 | 151 | 2,185 | 0 / 0 | 147 (tools 44) | — |
| dp8 | SiPath | 5.812 | 7,230 | 111 | 1,790 | 0 / 0 | 67 | passed |
| dp8_hs | ORFS | 7.658 | 5,816 | 151 | 2,610 | 0 / 0 | 150 (tools 47) | — |
| dp8_hs | SiPath | 7.514 | 7,328 | 111 | 2,442 | 0 / 0 | 81 | passed |
| ihp_add8 | ORFS | 8.412 | 2,252 | 117 | 2,097 | 0 / – | 138 (tools 28) | — |
| ihp_add8 | SiPath | 8.487 | 2,743 | 89 | 2,054 | 0 / – | 19 | completed |
<!-- /table -->

Timing is met with similar slack everywhere (TNS 0). SiPath uses 2–18% less
cell area: ORFS buffers every port (49 port buffers on dp8, which ends with 40
more cells than SiPath's) and SiPath does not for an embedded macro.
On gcd8 (plain placement) SiPath's wire is 18% shorter; on the datapath designs
it is 22–30% longer, the cost of the structured placement the design file asks
for (slices and arrays at a fixed origin, a cluster) and of the analog nets on
a wider non-default rule. ORFS's wall time is mostly per-stage process start-up
under Rosetta (its own stage times, from its logs, add up to 28–66 s); SiPath's includes
signoff (KLayout DRC, LVS, nine-corner STA: 19–24 s). mcu8 and ihp_ring (pad
rings, hard macros, analog supplies) are not compared: they would need ORFS
footprint scripts written for them.

## Synthesis bench

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

## Macro placement

Measured 2026-10-18. `examples/sram8.yaml` (eight sky130 1 KB SRAMs plus glue) and mcu8 (two
placed macros), each attempt scored by `macro_quick` (lower is better):

| design | method | macro-net HPWL (µm) | total quick HPWL (µm) | overflow | macro cost (µm) |
|---|---|---|---|---|---|
| sram8 | random, seed 1 | 150,547 | 574,065 | 7 | — |
| sram8 | cdlns | 146,339 | 628,305 | 39 | — |
| sram8 | mpl | fails (MPL-0003: no valid tiling) | | | — |
| mcu8 | random, best of 3 seeds | — | — | 0 | 53,061 |
| mcu8 | cdlns | — | — | 0 | 58,329 |
| mcu8 | mpl | — | — | — | 68,785 |

The `cdlns` rows were measured with the wall clock. Since 2026-10-22 the placer runs
on a work clock by default (`placer_budget: work`), so its result no longer varies
between runs ([hard macros](flow.md#hard-macros-macroplacepy)).

The random search wins on SiPath's score so far. cdlns gives the shortest
macro-net wiring on sram8, but it packs the SRAMs about 36 µm apart, and the
glue logic between them then routes with more overflow. The challenge's
proxy cost rewards that packing; SiPath judges by a quick route.

## Analog coupling

Measured 2026-10-18 and 19. Coupling from digital nets onto analog nets (worst corner), from
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

## Rosetta (amd64) against native arm64

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

## Strategies, judged by the final result

`python3 bench/strategy_compare.py gcd8_crowded dp8 aes --repeats 2` runs each
design to signoff five ways and measures the end, not the step a strategy
changes: `defaults` (the reference flow), `sweep` (the default placement and
two densities in parallel, chosen at placement by `routability_then_wns`),
`adaptive` (two adaptive placement rounds), `search` (the sweep with `keep: 2`,
decided after extraction) and `adaptive_search` (adaptive rounds, then the
best two and the baseline carried to extraction). Measured as the other
benchmarks are (typical-corner slack, cells, cell area and routed wirelength
from the final DEF), plus the worst signoff corner's slack, slew violations,
DRC, wall time and the core-seconds the scheduler allocated. Runs went one at
a time; gcd8_crowded is gcd8 at 65% core utilization and placement density
0.8, the setting the adaptive rounds were first measured on.

<!-- table:strategies -->
| design | strategy | runs | verification | WNS tt (ns) | WNS worst corner (ns) | cell area (µm²) | wirelength (µm) | DRC | slew viol. | wall (s) | core-s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gcd8_crowded | defaults | 2 of 2 | passed 2/2 | 6.981 | 4.213 | 1,672 | 3,438 | 0 | 0 | 70 | 833 |
| gcd8_crowded | sweep | 2 of 2 | passed 2/2 | 6.981 | 4.213 | 1,672 | 3,438 | 0 | 0 | 71 | 846 |
| gcd8_crowded | adaptive | 2 of 2 | failed 2/2: drv | 6.780 | 3.812 | 1,657 | 3,696 | 0 | 11 | 93 | 1,109 |
| gcd8_crowded | search | 2 of 2 | passed 2/2 | 6.981 | 4.213 | 1,672 | 3,438 | 0 | 0 | 110 | 1,309 |
| gcd8_crowded | adaptive_search | 2 of 2 | passed 2/2 | 6.981 | 4.213 | 1,672 | 3,438 | 0 | 0 | 170 | 2,027 |
| dp8 | defaults | 2 of 2 | passed 2/2 | 5.812 | 0.831 | 1,790 | 7,230 | 0 | 0 | 66 | 789 |
| dp8 | sweep | 2 of 2 | passed 2/2 | 5.812 | 0.831 | 1,790 | 7,230 | 0 | 0 | 66 | 783 |
| dp8 | adaptive | 2 of 2 | passed 2/2 | 5.812 | 0.831 | 1,790 | 7,230 | 0 | 0 | 70 | 839 |
| dp8 | search | 2 of 2 | passed 2/2 | 5.812 | 0.831 | 1,790 | 7,230 | 0 | 0 | 107 | 1,276 |
| dp8 | adaptive_search | 2 of 2 | passed 2/2 | 5.812 | 0.831 | 1,790 | 7,230 | 0 | 0 | 111 | 1,320 |
<!-- /table -->

- **Every repeat gave the same result,** to the last digit: the variation is
  in wall time only (two runs that the machine's sleep stretched were run
  again).
- **No strategy beat the defaults on these two designs.** On gcd8_crowded the
  sweep's placement score picked the default placement; on dp8 every strategy
  ended with the same layout.
- **Adaptive rounds alone made gcd8_crowded worse at the end:** 0.2 ns less
  slack at the typical corner, 0.4 ns at the worst one, 11 slew violations
  against none, for a placement that looked better by its heatmaps; both
  runs fail verification on those slew violations (the table counts each
  row's verdicts over all its runs). Carried
  to extraction with the baseline kept (`adaptive_search`), the baseline won
  and the final result was the defaults', at 2.4 times the core-seconds.
- **The bounded search costs** 1.5-1.6 times the defaults' core-seconds with
  one extra branch, and returns the defaults' result when the baseline is
  best: it is insurance, not a gain, on these designs.

## Exploration mode

Measured 2026-10-19. `--explore` on gcd8 (ORFS image, Rosetta): 20 s from RTL
to a PPA first cut at global routing; achievable period 3.39 ns (295 MHz)
against the SDC's 10 ns, suggested `create_clock -period 3.56`, 1,637 µm² of
cells, 0.87 mW.
