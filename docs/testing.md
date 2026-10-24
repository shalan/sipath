# Tests, regression limits and CI

Part of the [SiPath documentation](../README.md#documentation).

## Tests

- **Unit tests** (`python3 -m unittest sipath.tests.test_unit`; seconds,
  no tools): parsers and generators, the scheduler, scores, schemas
  and parameters, geometry (shields, macro rules), undefined names, the step
  parameters of every example, and that the library code names no PDK. Tests
  that need an open_pdks tree skip, with the reason, when there is none.
- **Live tests** (`python3 -m sipath.tests.run_live -j 4`): real
  runs of the examples through OpenROAD, Yosys, KLayout and Magic, one process
  per test class, in parallel, with each class's time.
  `python3 -m unittest sipath.tests.test_sipath` runs everything in one process.
- **Executable docs.** The documentation's code is tested, not only read:
  every YAML block validates as a design file or a flow (step names,
  parameters, prerequisites), every Python block compiles, every documented
  `sipath run` makes a plan and every other documented command exists (unit
  tests, `ExecutableDocs`); the MCP session of
  [recipe 13](examples.md#13-driving-a-run-from-an-agent-mcp) runs as printed
  on gcd8, through signoff and its ECO (live, `LiveDocs`); and the tutorial
  notebook's first part runs on the native tools, without Docker, in the
  nightly CI (`notebooks/smoke.py`, which runs the notebook's own cells).
- **Benchmarks** (`bench/`): against OpenROAD-flow-scripts both ways, the
  synthesis bench, amd64 against arm64, and configurations × repeats
  (`bench/repeat_compare.py`); results in [benchmarks](benchmarks.md).
  `orfs_compare.py` and `repeat_compare.py` write one record per result into
  `bench/results/` (`bench/records.py`: metrics named for what they measure,
  the run's verdict, step times, SiPath commit, image digest and open_pdks
  release, machine, input hashes, overrides, the run directory, and why a
  failed run failed); `bench/tables.py` writes the documentation's tables
  from those records only, and a unit test fails when a table is stale. A
  record takes its input hashes from the run's own graph (as hashed when it
  ran), its SiPath identity from the run's provenance (commit, code digest,
  the digest of an uncommitted diff, which the driver archives under
  `bench/results/sources/<key>` before it runs, the key a digest of the base
  commit, the diff and every untracked file: written once, never
  overwritten), and keeps the exporter's identity
  apart; each run has an outcome (completed, execution or measurement
  failed), a run that fails before it has a graph still gets a record, and
  `strategy_compare.py` exits 1 when a run did not complete. Tables count a
  run's results only when it completed and was measured (older records: exit
  code 0 and a measured result); measurement failures show in the runs
  column.

## Regression limits

`sipath/tests/limits.yaml` holds per-design limits: setup and hold slack, DRC,
LVS, signoff pass, wirelength, instances, wall time and analog coupling.
`python3 -m sipath limits <workdir> <design>` checks a run against them (exit
1 on a violation); `--suggest` prints an entry from a good run, to paste into
the file. The live tests
check their runs against the file. `SIPATH_LIMITS_IGNORE=max_runtime_s`
(comma-separated keys) skips limits that belong to another machine, such as
wall time in CI.

## CI (GitHub Actions)

- **`unit`** runs on pushes to `main` and on pull requests, on Python 3.10,
  3.12 and 3.14: the unit tests, a check that the generated reference is
  current, and a check of the wheel's contents (what it must carry and what it
  must not). Tests that need an open_pdks tree skip there, with the reason.
- **`live`** runs nightly and on demand (a dispatch may name classes; each runs
  in its own group's job): the locked ORFS image and the locked open_pdks
  release (fetched by `sipath install-pdk`), in four parallel design jobs, with the
  regression limits except wall time, keeping the logs of any run that does
  not pass (failed, timed out or cancelled):
  - **chip**: mcu8 (`LiveMcuDie`);
  - **openframe**: builds the Magic image and the precheck image on the
    locked ORFS image, then `LiveOpenFrame` (signoff, cf-precheck, chip
    integration with the seal ring) and `LiveMagic`;
  - **route-search-openframe**: builds both images and runs `LiveRouteSearchOpenFrame`,
    requiring cf-precheck before chip integration;
  - **blocks**: `sipath validate`, the unit tests with the PDK (those that skip
    in `unit`), then every other live class.

  A class whose every test skipped fails (`run_live`), so a missing tool,
  PDK or image cannot pass as tested; a live run with no regression limits
  fails too. What may skip, with the reason in the log: a test of a tool the
  job does not have (cdlns without PyTorch, `LiveMagic` without the Magic
  image); `LiveOpenFrame` leaves its precheck step out when the precheck image
  is missing, which the openframe job builds.
- **`tutorial`** (with `live`) runs the tutorial notebook's first part on the
  native path, no Docker (`install-tools`, `install-pdk`, gcd8 to signoff,
  explore mode) through `notebooks/smoke.py`: a cell fails when it raises or
  when any of its `!` commands exits non-zero.
- The unit workflow also installs Icarus Verilog and runs
  `sipath.tests.test_route_search`: both example plans, GCD8 functional tests,
  and the OpenFrame wrapper's GCD and pad-control tests. The live classes
  `LiveRouteSearchMacro` and `LiveRouteSearchOpenFrame` exercise the shipped
  YAMLs, stop/resume inside placement, check the extracted decision and final
  artifacts, then verify a completed resume reuses its results. They have no
  measured PPA regression limits yet.
- On Linux the containers run as the calling user (`--user`), so the files
  they write stay the user's; `SIPATH_DOCKER_AS_ROOT=1` keeps the image's user.
