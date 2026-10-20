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
   that meets timing, else the knee of the slack/area front, else the best
   slack (`synth_fallback: knee`; or `best_slack`, `least_area`). `abc:
   <recipe>` maps with one recipe. No ABC delay target by default
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
