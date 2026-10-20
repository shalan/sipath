# Modules

Part of the [SiPath documentation](../README.md#documentation).

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
| `precheck.py` | ChipFoundry's tapeout precheck (cf-precheck) on a signed-off harness design: project export, golden root, results |
| `signoff.py`, `lvs_full.py`, `klayout/` | signoff checks; full-device LVS; GDS stream-out, chip check, seal ring and XOR scripts |
| `harness.py`, `harnesses/`, `templates.py`, `templates/` | frames (OpenFrame, Caravel) and their pinned sources; design templates |
| `registry.py`, `scores.py`, `demo.py` | extension points; named scores; stand-in scores for scheduler tests |
| `mcp_server.py`, `report.py`, `viz.py` | MCP server; run reports; schematic drawings |
| `limits.py`, `toolchain.py`, `toolchain.json` | regression limits; locked images and PDK release |
| `examples/`, `tests/`, `bench/` | example designs; unit and live tests with `limits.yaml`; benchmarks against ORFS, synthesis and architectures |
