# Synthesis

Part of the [SiPath documentation](../README.md#documentation).

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
   that meets timing, else (no recipe meets timing) the fastest netlist
   (`synth_fallback: best_slack`, the default; or `knee` of the slack/area
   front, `least_area`). `abc:
   <recipe>` maps with one recipe. No ABC delay target by default
   (`abc_target: none|period|<ps>`).
6. **Checks and proof.** An unmapped cell (e.g. a latch) or an excluded cell
   fails the step. The selected netlist is then checked against the RTL
   (Yosys miter, cell functions from the Liberty file): equal outputs for
   every input sequence for `equiv_depth` (10) cycles from reset, within
   `equiv_timeout` (600 s). It is bounded, not a full proof (induction needs
   a register correspondence the synthesized names do not give). A successful
   synth step therefore does not always mean equivalence was shown; the
   outcome is in the attempt's `equiv` metric and output:

   | `equiv` | meaning | the step |
   |---|---|---|
   | `equivalent for N cycles from reset` | no difference within N cycles | succeeds |
   | `mismatch` | a counterexample (in the error, and `equiv.log`) | fails |
   | `inconclusive (timeout)` | the solver ran out of time: nothing shown either way (aes: 600 s) | succeeds (`equiv_on_timeout: fail`: fails) |
   | `skipped` | the design has macros, which are black boxes to the check | succeeds |
   | (none) | `equiv: off` | succeeds |

   A run that ends without a result (no verdict and no timeout in the log)
   fails the step. Raise `equiv_timeout` or lower `equiv_depth` to turn an
   inconclusive result into a verdict. `equiv_on_timeout` decides what a
   timeout does: `warn` (the default: the step passes, the run's
   [verdict](signoff.md#the-runs-verdict) is inconclusive), `fail` (the step
   fails) or `pass` (accepted: the verdict counts it as passed and says so in
   a note).

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

Why these defaults (numbers from the [synthesis bench](benchmarks.md#synthesis-bench)):
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

## The fallback when nothing meets timing

Measured 2026-10-21, each design to signoff with each fallback (`least_area`
was worst on timing everywhere, and is not shown). The default changed from
`knee` to `best_slack` on these results.

| design | slack at ss_max: knee | best_slack | cell area | wirelength | slew violations |
|---|---|---|---|---|---|
| aes (ORFS) | −3.460 ns | **−2.383 ns** | −8% | −10% | 127 → 34 |
| gcd (ORFS, 1.1 ns) | −3.079 ns | −3.079 ns | same netlist | same | same |
| alu32 (10 ns) | −1.990 ns | **−1.209 ns** | +15% | +27% | 25 → 42 |
| apb_timer (10 ns) | −0.970 ns | **−0.234 ns** | +3% | +7% | 0 → 14 |

`best_slack` gives the better slow-corner slack on three designs and ties on
the fourth; its cost in area, wire and slew violations depends on the design
(`route_repair`'s `drv_sizing` sizes up the drivers of slew violators after
routing). None of the four meets timing at the slow corner with either.
`bench/repeat_compare.py` reruns the comparison.

