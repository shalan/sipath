# Tests, regression limits and CI

Part of the [SiPath documentation](../README.md#documentation).

## Tests

- **Unit tests** (`python3 -m unittest sipath.tests.test_unit`; 168 tests,
  seconds, no tools): parsers and generators, the scheduler, scores, schemas
  and parameters, geometry (shields, macro rules), undefined names, the step
  parameters of every example, and that the library code names no PDK. Tests
  that need an open_pdks tree skip, with the reason, when there is none.
- **Live tests** (`python3 -m sipath.tests.run_live -j 4`; 46 tests): real
  runs of the examples through OpenROAD, Yosys, KLayout and Magic, one process
  per test class, in parallel, with each class's time.
  `python3 -m unittest sipath.tests.test_sipath` runs everything in one process.
- **Benchmarks** (`bench/`): against OpenROAD-flow-scripts both ways, the
  synthesis bench, amd64 against arm64, and configurations × repeats
  (`bench/repeat_compare.py`); results in [benchmarks](benchmarks.md).

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
