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
  from those records only, and a unit test fails when a table is stale.

## Regression limits

`sipath/tests/limits.yaml` holds per-design limits: setup and hold slack, DRC,
LVS, signoff pass, wirelength, instances, wall time and analog coupling.
`python3 -m sipath limits <workdir> <design>` checks a run against them (exit
1 on a violation); `--suggest` writes an entry from a good run. The live tests
check their runs against the file. `SIPATH_LIMITS_IGNORE=max_runtime_s`
(comma-separated keys) skips limits that belong to another machine, such as
wall time in CI.

## CI (GitHub Actions)

- **`unit`** runs on every push and pull request, on Python 3.10, 3.12 and
  3.14: the unit tests, a check that the generated reference is current, and
  a check of the wheel's package data.
- **`live`** runs nightly and on demand: the locked ORFS image and the locked
  open_pdks release (fetched by `sipath install-pdk`), in three parallel jobs
  (mcu8; the OpenFrame harness; everything else), with the regression limits
  except wall time, keeping the logs of any run that does not pass (failed,
  timed out or cancelled). The precheck image is built locally, so CI runs
  OpenFrame without cf-precheck.
- On Linux the containers run as the calling user (`--user`), so the files
  they write stay the user's; `SIPATH_DOCKER_AS_ROOT=1` keeps the image's user.
