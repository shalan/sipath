# Example flows

Part of the [SiPath documentation](../README.md#documentation).

Recipes, one feature each: a design-file snippet, the command, and what to look
at afterwards. A snippet with `flow:` replaces the `flow:` of a design file (the
rest of the file stays as it is); every snippet here is checked against the
design-file schema and the declared step parameters by the unit tests, so the
names are the ones SiPath accepts. Full designs for most recipes are in
`sipath/examples/` ([design files and examples](design-files.md)).

- [1. RTL to a signed-off layout](#1-rtl-to-a-signed-off-layout)
- [2. A first cut at PPA, then the clock](#2-a-first-cut-at-ppa-then-the-clock)
- [3. Synthesis: recipes, front ends, the fallback](#3-synthesis-recipes-front-ends-the-fallback)
- [4. Placement variants chosen by a score](#4-placement-variants-chosen-by-a-score)
- [5. Hard macros: comparing placers](#5-hard-macros-comparing-placers)
- [6. Analog nets: guard, shields, coupling limits](#6-analog-nets-guard-shields-coupling-limits)
- [7. A full die with a pad ring](#7-a-full-die-with-a-pad-ring)
- [8. An OpenFrame chip, prechecked, with its seal ring](#8-an-openframe-chip-prechecked-with-its-seal-ring)
- [9. Hardened macros in a parent design](#9-hardened-macros-in-a-parent-design)
- [10. Signoff checks one by one, and your own step](#10-signoff-checks-one-by-one-and-your-own-step)
- [11. Resume, and a step under another name](#11-resume-and-a-step-under-another-name)
- [12. An ECO from Python](#12-an-eco-from-python)
- [13. Driving a run from an agent (MCP)](#13-driving-a-run-from-an-agent-mcp)
- [14. Regression limits and repeated runs](#14-regression-limits-and-repeated-runs)
- [15. Design-space exploration: sweeps at several steps, in parallel](#15-design-space-exploration-sweeps-at-several-steps-in-parallel)

## 1. RTL to a signed-off layout

The smallest design file: RTL, constraints and a target utilization. Without a
`flow:` list SiPath runs its default chain, synthesis to signoff.

```yaml
name: gcd8
top: gcd8
mode: macro
pdk: sky130A
rtl: [rtl/gcd8.v]
sdc: rtl/gcd8.sdc
core_utilization: 0.4
```

```bash
python3 -m sipath run my_gcd8.yaml runs/gcd8
python3 -m sipath render runs/gcd8 all          # a picture per step: runs/gcd8/_pictures/place.0.png, ...
python3 -m sipath report runs/gcd8
```

`sipath render runs/gcd8 place` draws one step (its selected attempt);
`place.1` names an attempt, a rejected one too. Look at
`runs/gcd8/report.html` (every step, its attempts and metrics, the signoff
checks, the pictures in flow order) and `signoff/0/signoff.json` (DRC, LVS,
antennas, nine-corner timing). `sipath/examples/gcd8.yaml` is this design.

## 2. A first cut at PPA, then the clock

Before a full run, `--explore` runs the design's flow to global routing in
about a minute and suggests a clock.

```bash
python3 -m sipath run sipath/examples/gcd8.yaml runs/gcd8_explore --explore
```

`ppa.md` gives cell area, utilization, WNS/TNS/WHS, power, wirelength, and per
clock the achievable period and a suggested `create_clock`. Put that period in
the SDC, then run the full flow (recipe 1).

## 3. Synthesis: recipes, front ends, the fallback

The synth step maps with several ABC recipes in one attempt and keeps the
smallest netlist that meets timing. Choose the recipe set, the corner and what
to do when nothing meets timing:

```yaml
flow:
- step: synth
  variants: [{abc_sweep: balanced, synth_fallback: least_area}]
- step: floorplan
- step: physical
- step: place
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: signoff
```

Or make every recipe × front end a separate attempt (Booth multipliers,
prefix adders), picked by `synth_select`:

```yaml
flow:
- step: synth
  sweep: delay
  synth_opts_sweep: [[], [booth], [adder=kogge-stone], [adder=han-carlson]]
```

The selected attempt's `abc_sweep.why` says which recipe won and why; the
[synthesis bench](benchmarks.md#synthesis-bench) has measured numbers.

## 4. Placement variants chosen by a score

Any step takes `variants` (one attempt each, run `parallel` at a time) and a
named `score` that picks the one the next step continues from; the others stay
in `graph.json`.

```yaml
flow:
- step: synth
- step: floorplan
- step: physical
- step: place
  variants: [{density: 0.5}, {density: 0.6}, {density: 0.7, routability_driven: false}]
  score: routability_then_wns
  parallel: 3
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: signoff
```

`routability_then_wns` ranks by global-route overflow first, so a congested
variant loses on score rather than failing later. Other scores: `auto` (setup
slack when constrained, else wirelength), `setup_wns`, `wns_then_hpwl`,
`drc_then_slack`, `macro_quick`, `synth_select`; plugins register more, and
MCP's `list_scores` lists them all.

## 5. Hard macros: comparing placers

Macros without a `location` are placed by the `macros` step. Three methods,
each checked against the same rules (halo, channel, pin clearance, analog
guard) and scored by a quick place and route:

```yaml
macros:
- {inst: "bank[0].u_ram", sram: sky130_sram_1kbyte_1rw1r_32x256_8, halo: 15,
   supplies: {"^vccd1$": VDD, "^vssd1$": VSS}}
keepouts: [[0, 0, 100, 100]]
macro_channel: 30
flow:
- step: synth
- step: floorplan
- step: macros
  variants: [{method: random, seed: 1}, {method: random, seed: 2}, {method: mpl},
             {method: cdlns, placer_seconds: 120}]
  score: macro_quick
  parallel: 4
```

`cdlns` needs the placer's Python once: `python3 -m sipath setup-placer`.
`sipath/examples/sram8.yaml` is eight SRAMs placed this way; the
[macro placement benchmark](benchmarks.md#macro-placement) compares the
methods.

## 6. Analog nets: guard, shields, coupling limits

Analog nets are routed first with a wide rule and frozen; later digital wiring
keeps that spacing. Shield the sensitive ones and bound their coupling:

```yaml
analog_nets: [ana_in, vref]
analog_shield: [ana_in]          # grounded wires on both sides
analog_keepaway: true            # no digital wiring on the layers above and below
flow:
- step: synth
- step: floorplan
- step: physical
- step: place
- step: cts
- step: route_analog
  variants: [{shield_tie_um: 30}]
- step: route_digital
- step: route_repair
- step: extract
  variants: [{max_analog_coupling_ff: 1.5, max_analog_aggressor_ff: 0.5}]
- step: signoff
```

`route_analog`'s outputs give each shielded net's coverage; `extract` writes
`analog_coupling.md` (each analog net's coupling to digital nets and its worst
aggressors) and fails above the limits. `sipath/examples/dp8.yaml` and
`mcu8.yaml` shield their analog nets ([analog coupling](benchmarks.md#analog-coupling)).

## 7. A full die with a pad ring

`mode: die` generates the pad ring from rules (supply pairs on every edge,
analog pads next to their supplies, every IO control pin tied) and routes 3.3 V
analog supplies from their pads:

```yaml
name: chip
top: chip
mode: die
pdk: sky130A
netlist: rtl/chip_flat.v
sdc: rtl/chip.sdc
die: [0, 0, 1500, 1500]
core_margin: 241
pads:
- {inst: pad_clk, edge: west, order: 0, master: sky130_ef_io__gpiov2_pad_wrapped}
pad_kinds: {pad_clk: input}
pad_supplies: {"^VCCD$": VDD}
analog_supplies: [VDDA, VSSA]
```

`sipath/examples/mcu8.yaml` is a complete die: 39 pads, an SRAM, an ADC and a
DAC, analog nets shielded; `python3 -m sipath run mcu8`.

## 8. An OpenFrame chip, prechecked, with its seal ring

A harness makes the design the frame's wrapper (template die and pins, frame
supplies, power grid). After signoff, ChipFoundry's own precheck runs on the
exported project, and `integrate` puts the wrapper into the frame's layout
with the seal ring:

```bash
python3 -m sipath new myproj --template openframe --set inputs=8 --set outputs=6
docker build --platform linux/amd64 -t sipath/precheck:1.3.7 docker/precheck   # once
```

```yaml
flow:
- step: synth
- step: floorplan
- step: physical
- step: place
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: signoff
- step: precheck
- step: integrate
  variants: [{seal_ring: true}]
```

`precheck`'s outputs list every cf-precheck check (LVS with Netgen, OEB with
CVC, XOR, KLayout DRC, ...); `integrate`'s `chipcheck` confirms the die, the
wrapper at its origin and unchanged, and the seal ring centred and touching
nothing. `sipath/examples/openframe_gcd8.yaml` is a gcd on the east-edge pads.

## 9. Hardened macros in a parent design

Harden a signed-off design into views (abstract LEF, Liberty per corner, GDS,
SPEF, netlists), then place it in a parent:

```bash
python3 -m sipath harden gcd8 sipath/examples/hardened/gcd8
```

```yaml
macros:
- {inst: u0, views: sipath/examples/hardened/gcd8, location: [20, 20]}
- {inst: u1, views: sipath/examples/hardened/gcd8, location: [160, 20]}
```

The parent signs off with the macros' insides checked (DRC sees their layout,
LVS their netlist); `sipath/examples/gcd8_pair.yaml` does this with two gcd8.

## 10. Signoff checks one by one, and your own step

Each signoff check is also a step (its own process, resumable, its own
variants), and a step function can be named by import path:

```yaml
flow:
- step: synth
- step: floorplan
- step: physical
- step: place
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: drc
- step: lvs
- step: timing
  variants: [{require: [setup, hold]}]
- step: mypkg.checks:inst_count
  variants: [{at_least: 100}]
```

```python
# mypkg/checks.py
from sipath.params import Param


def inst_count(ctx):
    """A step: counts instances (OpenROAD session in ctx.session) and checks a floor."""
    n = int(ctx.session.value("llength [[ord::get_db_block] getInsts]"))
    ctx.metrics["user_insts"] = n
    if n < ctx.params["at_least"]:
        raise RuntimeError(f"only {n} instances")


inst_count.params = [Param("at_least", "int", 1, "fewest instances accepted", min=0)]
```

A whole package of steps, scores, checks and harnesses registers through
`plugins: [mypkg.sipath_ext]` (`sipath/examples/plugin_example.py`).

## 11. Resume, and a step under another name

An interrupted run continues where it stopped; finished attempts are kept:

```bash
python3 -m sipath run mcu8 runs/mcu8 --resume
```

A step can run again under another name with `fn:`, for example a quick
signoff of a few checks before the full one:

```yaml
flow:
- step: synth
- step: floorplan
- step: physical
- step: place
- step: cts
- step: route_analog
- step: route_digital
- step: route_repair
- step: extract
- step: signoff_quick
  fn: signoff
  variants: [{checks: [drc, setup, hold], require: [drc, setup, hold]}]
- step: signoff
```

## 12. An ECO from Python

ECOs (resize, buffer) continue from the head of a run, are checked for
structural equivalence before routing, re-route only the touched nets and are
rejected if setup or hold slack drops by more than `max_slack_loss`:

```python
from sipath import designfile

flow = designfile.run("sipath/examples/dp8.yaml", "runs/dp8")
head = flow.steps["signoff"].selected
eco = flow.eco("eco1", head, [
    {"op": "resize", "inst": "u_acc.x_0", "master": "sky130_fd_sc_hd__xor2_2"},
    {"op": "buffer", "net": "u_acc.n_3", "master": "sky130_fd_sc_hd__buf_2"},
])
print(eco.selected.metrics["wns"], eco.selected.outputs.get("equivalence"))
```

## 13. Driving a run from an agent (MCP)

`python3 -m sipath mcp` serves the tools on stdio; register it with an MCP
client as a command. A session, as tool calls:

```text
open_run     {"design": "gcd8", "workdir": "runs/agent_gcd8"}                 -> run_id
run_step     {"run_id": R, "step": "synth"}
run_step     {"run_id": R, "step": "place", "variants": [{"density": 0.5}, {"density": 0.7}],
              "score": "routability_then_wns", "parallel": 2, "background": true}   -> job_id
job_status   {"job_id": J}                                                      (progress, then the result)
list_attempts {"run_id": R, "step": "place"}
get_outputs  {"run_id": R, "attempt": "place.1"}                               (metrics, reports, files)
render_checkpoint {"run_id": R, "attempt": "place.1"}                          (a layout picture)
run_eco      {"run_id": R, "parent": "signoff.0", "ops": [{"op": "resize", "inst": "u1", "master": "sky130_fd_sc_hd__nand2_4"}]}
```

`describe_step` gives any step's parameters as a JSON Schema; arguments are
checked before anything runs ([agents](agents.md)).

## 14. Regression limits and repeated runs

Freeze a good run's results as limits, then check later runs against them:

```bash
python3 -m sipath limits runs/gcd8 gcd8 --suggest      # an entry for sipath/tests/limits.yaml
python3 -m sipath limits runs/gcd8 gcd8                # exit 1 on a violation
```

Compare configurations over repeated runs (median, min and max of each
metric, and where identical inputs diverge step by step):

```bash
python3 bench/repeat_compare.py aes --repeats 2 \
    --config knee: --config least_area:synth.synth_fallback=least_area
```

The live tests check their runs against `limits.yaml`, nightly in CI
([tests, limits and CI](testing.md)).

## 15. Design-space exploration: sweeps at several steps, in parallel

`sipath/examples/gcd8_explore.yaml` sweeps three steps of gcd8 at a 4 ns clock.
Each step runs its variants side by side, its `score` selects one, and the next
step continues from that one only, so the run follows the best path found so
far rather than every combination (2 + 3 + 2 attempts, not 2 × 3 × 2):

```yaml
config: {parallel_cap: 4}    # most attempts alive at once, across the run
flow:
- step: synth                # two recipe sets, each swept inside its attempt
  variants: [{abc_sweep: delay}, {abc_sweep: area}]
  score: synth_select
  parallel: 2
- step: floorplan
- step: physical
- step: place                # three densities, three at a time
  variants: [{density: 0.45}, {density: 0.6}, {density: 0.75}]
  score: routability_then_wns
  parallel: 3
- step: cts
- step: route_analog
- step: route_digital        # with and without repair on global-route parasitics
  variants: [{repair_after_grt: true}, {repair_after_grt: false}]
  score: drc_then_slack
  parallel: 2
- step: route_repair
- step: fill
- step: extract
- step: signoff
```

First the quick sweep, then the full one:

```bash
python3 -m sipath run sipath/examples/gcd8_explore.yaml runs/gcd8_x --explore   # global routing only
python3 -m sipath run sipath/examples/gcd8_explore.yaml runs/gcd8_full          # to signoff
python3 -m sipath report runs/gcd8_full                                         # every attempt, why it lost
```

Measured (Apple-silicon Mac, amd64 image, 12 threads, while another long run
shared the machine):

| step | variant | full flow: result | selected |
|---|---|---|---|
| synth | `abc_sweep: delay` | `delay_map_resyn`, +0.519 ns at ss_max, 1,768 µm² | ✓ |
| synth | `abc_sweep: area` | `yosys_default`, −0.825 ns, 1,710 µm² | |
| place | `density: 0.45` | WNS +0.250 ns, HPWL 3,437 µm, no overflow | |
| place | `density: 0.6` | WNS +0.225 ns, HPWL 3,341 µm | |
| place | `density: 0.75` | WNS +0.300 ns, HPWL 3,209 µm | ✓ |
| route_digital | `repair_after_grt: true` | WNS +0.126 ns, DRC 0, 4,289 µm | ✓ |
| route_digital | `repair_after_grt: false` | WNS +0.056 ns, DRC 0, 4,124 µm | |

| run | attempts | wall time | result |
|---|---|---|---|
| `--explore` | 7 | 29 s | WNS +0.907 ns at tt (global-route parasitics); achievable 3.09 ns, suggested `create_clock -period 3.25` |
| full, in parallel | 7 + the single steps | 91 s | signoff passes: DRC 0, LVS match, WNS +1.975 ns at tt, +0.126 ns at ss_max |
| full, one attempt at a time (`SIPATH_PARALLEL_CAP=1`) | the same | 104 s | identical: the same selections and numbers at every step |

What this shows:
- **Parallelism changes the wall time, not the result.** Both full runs select
  the same attempts with the same metrics: the flow is deterministic, and
  attempts running at once share the run's OpenROAD threads (each gets
  `threads` / the attempts alive at once; a step's `threads` parameter
  overrides it). On a design this small the gain is modest (12%); it grows with
  the attempts' length, e.g. mcu8's macro sweep.
- **Explore mode is a first look, judged at the typical corner.** There it
  picks the area recipe set (it meets 4 ns at tt); the full flow, judging at
  the slow corner, picks the delay set. In explore mode a routing score that
  needs the detailed router's DRC count (`drc_then_slack`) becomes
  `setup_wns`.
- **Every attempt stays.** `graph.json` holds each attempt's parameters,
  metrics, score and status (`selected` or `rejected`); `report.html` shows
  them; over MCP, `list_attempts` and `get_outputs` read them. To try another
  path, re-run the step with that variant alone (or a different `score`): a run
  continues only from a selected attempt.
- **An interrupted sweep resumes.** `--resume` keeps the attempts that
  finished (a sweep of three that stopped after two runs only the third) and
  re-runs nothing that is recorded.

