# Modules

Part of the [SiPath documentation](../README.md#documentation).

## Layers

The code is in layers. A unit test (`Layers` in `sipath/tests/test_unit.py`)
reads every module's imports, including imports inside functions, and fails
on any import that crosses these rules.

| layer | modules | may import |
|---|---|---|
| core | `scheduler`, `params`, `scores`, `registry`, `config`, `report`, `recipes`, `toolchain`, `fingerprint`, `verdict` | core, model |
| model | `design`, `pdk`, `netlist`, `lef`, `heatmap`, `sdc` | core, model |
| tools | `tools/` | core, model, tools |
| steps | `steps/` and the libraries they use (everything not listed elsewhere) | core, model, tools, steps |
| flows | `designfile`, `mcp_server`, `__main__`, the package's `__init__` | everything |

- **Core: no tool knowledge.** The scheduler runs attempts, keeps the graph,
  scores and resumes. A checkpoint is an opaque path to it. The checkpoint
  store loads a parent, reads the metrics and saves
  (`registry.checkpoint_store()`, by default `tools/openroad/checkpoint.py`:
  OpenROAD databases). `Flow.eco` runs `registry.eco_runner()`, by default
  `eco.run_eco`. The registry owns the step table (`registry.STEPS`, filled by
  `steps/`). It names these built-ins by module path (`registry.BUILTIN`) and
  imports them on first use, as it does for plugins. `toolchain` reads the
  lock; it runs nothing.
- **Model:** the design and PDK descriptions, Verilog netlists, LEF macros
  and heatmap CSVs, read without a tool.
- **Tools:** how a tool runs and what it is told.
- **Steps:** stage logic: which operations run, in what order, with which
  parameters, and what their results decide. The step modules (`steps/`)
  send no Tcl themselves: every OpenROAD command they need is a named
  operation in `tools/openroad/ops/`, and a second test
  (`test_steps_talk_to_openroad_only_through_its_operations`) fails on any
  `.run(...)` or `.value(...)` on a session inside `steps/`, and inside the
  libraries `eco`, `macroplace`, `congestion` and `minarea` (their operations:
  `ops/eco.py`, `ops/macros.py`, `ops/minarea.py`). The other libraries
  (`chip`, `padplan`, `powerconnect`, `analog`, `shield`, `datapath`, the
  signoff side) still write their Tcl inline.
- **Flows:** design files, the default chain, the CLI and the MCP server.
- **Compatibility aliases.** `sipath.session`, `sipath.tcl`,
  `sipath.tclquote` and `sipath.nativetools` are the moved modules themselves
  (`sys.modules` aliases), kept for plugins and older imports. Library code
  imports the `sipath.tools` names, which the test enforces. Synthesis does
  not import signoff.

## Core

| module | role |
|---|---|
| `scheduler.py` | `Flow.step`, `Flow.eco`, the attempt graph (`graph.json`), resume, cancel, retries (`RetryWith`, recorded with `retry_of` and `retry_reason`) |
| `verdict.py` | what a run established, apart from whether it finished: execution status, a verification status (passed, failed, inconclusive, omitted, completed), every check along the selected chain, what was omitted and what limits the rest |
| `fingerprint.py` | what a run's results depend on (input file contents, the resolved design, PDK, tools, result-changing config), as hashes; each attempt's fingerprint, which a resume compares |
| `params.py`, `config.py` | step parameter and design-file declarations; run configuration |
| `scores.py`, `recipes.py` | named scores; ABC recipes and objective sets (`synth_recipes/`) |
| `registry.py` | extension points (steps, scores, checks, harnesses, templates, extensions), the step table and the core's built-in defaults |
| `report.py` | run reports (`report.md`, `report.html`) from `graph.json` |
| `toolchain.py`, `toolchain.json` | the locked images and open_pdks release; a PDK tree's release |

## Model

| module | role |
|---|---|
| `design.py`, `pdk.py`, `pdks/` | design spec (unset fields from the PDK); PDK descriptions and their loader (`read_file`: YAML or JSON) |
| `netlist.py`, `lef.py`, `sdc.py` | Verilog netlist model; LEF macros, pins and touching pins; an SDC's clock ports |
| `heatmap.py` | heatmap CSVs (`gui::dump_heatmap`): read, summarised (peak, p95, hot share, hot regions), queried by box, drawn as SVG |

## Tools (`tools/`)

| module | role |
|---|---|
| `openroad/session.py` | `OpenRoadSession`: one process per attempt; Tcl in over stdin, result/errorInfo back through files; a Tcl error raises `TclError`. Docker and native runners. `FakeSession` for graph tests |
| `openroad/tcl.py`, `openroad/tclquote.py` | Tcl generators and helpers; quoting (no substitution possible) and Tcl-list parsing |
| `openroad/checkpoint.py` | the checkpoint store: process setup (threads, helpers), loading a parent, the metrics every attempt records, `write_db` |
| `openroad/ops/` | the operations the steps call, one module per area: `db` (database queries, metrics), `floorplan`, `place` (placement, repair, pin channels, blockages, CTS), `structure` (fences, clusters), `route`, `analog` (NDRs, supply parking, row blockages), `power` (grid checks, IR drop), `finish` (fill, views, parasitics), `report` (reports to files, PPA values), and for the libraries `eco` (ECO and post-route repair), `macros` (macro placement), `minarea` |
| `openroad/heatmap.py` | heatmaps dumped after an attempt's checkpoint is saved (`Ctx.after_save`), with the instances or nets in each hotspot |
| `process.py` | every tool process under one manager: its own process group (a kill takes the tree), a named container (`docker kill` on a cancel or a timeout), registered with its attempt so `Flow.cancel` reaches it, one log format with exit, duration and peak memory |
| `run.py` | `run_tool` (a tool next to OpenROAD through `process.py`, its output in a log) and the runner lookup; `ToolFailed` |
| `yosys.py`, `klayout.py`, `magic.py`, `netgen.py` | per-tool commands and version probes (Netgen: probe only; it runs in the precheck image) |
| `doctor.py` | `sipath doctor`: the runtime a run would use, images, native tools, the PDK release and views, optional tools, each with its fix |
| `toolchain.py`, `native.py` | probing the locked images' tools (`toolchain --check`); the locked ORFS image's tools without Docker (`install-tools`) |

## Steps and their libraries

| module | role |
|---|---|
| `steps/adapt.py` | adaptive rounds: the `place` adapter (hotspots into partial blockages and a lower density), registered in `registry.ADAPTERS` |
| `steps/` | one module per stage: `floorplan` (floorplan, macros, physical), `place` (place, cts), `analog_route`, `route` (route_digital, route_repair), `finish` (fill, extract, signoff and the check steps, views, integrate, ppa), helpers in `common` and `structure` |
| `synth.py`, `blackbox.py` | the synth step (Yosys, Verilator, a timing check); black-box Liberty models |
| `chip.py`, `padplan.py`, `powerconnect.py` | pad ring (ICeWall); pad plan rules; straps from supply tabs to the grid |
| `macroplace.py`, `third_party/` | hard macro placement (random, mpl, cdlns) and its rules; the vendored CD-LNS placer |
| `analog.py`, `shield.py` | analog freeze, restore and coupling report; shields, guard and keep-away |
| `datapath.py`, `congestion.py`, `minarea.py` | structured placement and re-abut; congestion reports; post-route minimum-area safety net |
| `eco.py`, `equiv.py`, `liberty.py`, `wires.py` | ECOs and post-route repair; structural equivalence on Liberty functions; Liberty models; wire re-encoding and restore after RCX |
| `signoff.py`, `lvs_full.py`, `precheck.py`, `klayout/` | signoff checks; full-device LVS; ChipFoundry's cf-precheck; GDS stream-out, chip check, seal ring and XOR scripts |
| `harness.py`, `harnesses/`, `templates.py`, `templates/` | frames (OpenFrame, Caravel) and their pinned sources; design templates |
| `schema.py`, `reference.py`, `limits.py`, `viz.py`, `demo.py` | design-file schema; the generated reference; regression limits; pictures; stand-in steps for scheduler tests |

## Flows

| module | role |
|---|---|
| `designfile.py` | design files (YAML/JSON), the default chain, `run`, `harden`, explore mode |
| `__main__.py` | the CLI |
| `mcp_server.py` | the MCP server |

`examples/`, `tests/` and `bench/` hold the example designs, the unit and
live tests (with `limits.yaml`), and the benchmarks against ORFS, synthesis
and architectures.
