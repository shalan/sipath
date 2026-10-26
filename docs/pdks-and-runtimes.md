# PDKs and runtimes

Part of the [SiPath documentation](../README.md#documentation).

## PDKs

A PDK description (`sipath/pdks/*.yaml`, or a path to one) names the views,
routing stack, special cells (tap/endcap optional, tie hi/lo, diode, CTS
buffers), supply pins, PDN and analog defaults, RC, tracks, corners, the IO
library, the SRAM macros and the signoff decks. The library code names no
PDK layer or cell (a unit test greps for it); the same code runs IHP SG13G2.

**Sky130 is first class** (`pdk: sky130A` or `sky130B`): every view comes from
an open_pdks tree (`$PDK_ROOT`, default `~/.ciel`, as installed by ciel),
mounted into the container.
- **Release pinned.** `sipath/toolchain.json` locks open_pdks `8afc8346…`.
  Every run reads the tree's `SOURCES` file (else ciel's `versions/<commit>`
  path) and records it in its provenance (`pdk_release`). A tree of another
  release, or one that says none, is refused unless `SIPATH_REQUIRE_PDK=0`,
  and that run records the mismatch. `python3 -m sipath install-pdk` installs
  the locked release with the libraries SiPath uses (ciel's default set lacks
  the hs cells; the lock lists them); `toolchain --check` compares the
  installed trees and names missing libraries.
- **Views.** hd standard cells (fd + ef LEFs), the nom tech LEF, `tracks.info`,
  `sky130_ef_io` pads with their gpiov2 Liberty, the OpenRAM
  `sky130_sram_macros` (a design names one with `sram:`).
- **Corners.** Nine signoff corners, tt/ss/ff (tt_025C_1v80, ss_100C_1v60,
  ff_n40C_1v95) × nom/min/max interconnect, each with its own Liberty and RCX
  rules. The RCX rules follow the PDK's own `libs.tech/openlane/config.tcl`
  (as OpenLane and LibreLane read it): the Calibre-derived set on sky130A, the
  `spef_extractor` set on sky130B, which the release ships no Calibre rules
  for (variable `${rcx_source}`). Estimation RC is the calibrated nominal table scaled per layer and
  via by each corner's tech LEF. Extraction runs once per RCX rule file (3
  SPEFs), each read into the 3 corners that share it. Placement, CTS and
  routing repair on `optimize_corners` (tt_nom, ss_max, ff_min): on aes, nine
  corners made each parasitics estimate ~18x slower (143 s against 8 s);
  signoff still checks all nine.
- **Design rules.** Max transition 0.75 ns, max fanout 10, max capacitance
  0.2 pF, applied before the design's SDC. Boundary defaults (inv_2 driving
  cell, 33.4 fF output load) apply to embedded macros only, since a die's
  ports are bond pads. Standard-cell slew/cap/fanout violators are counted
  (`drv_*`); IO-ring, pad, macro and port violators are listed apart.
- **Reference flow settings.** Tap distance 13 µm; clock nets on met3 and up;
  CTS clkbuf_2/4/8 with a clkbuf_16 root; decap and fill cells; met4/met5
  straps (153.6/153.18 µm) and core ring; repair margins (setup 0.025 ns, hold
  0.1 ns after placement and CTS, 0.05 ns after global route).
- **Excluded cells.** Each Sky130 description reads the PDK's
  `drc_exclude.cells` (53 hd, 51 hs cells: all `lpflow`, some drives,
  `xor3`/`xnor3`, `mux4_4`, `fa_4`, ...). Sizing, buffering and CTS cannot
  choose them, a netlist that uses one is refused at validation (waive with
  `allow_excluded`), and an ECO to one is refused. Designs add patterns with
  `dont_use`.
- **High-speed library.** `pdk: sky130A_hs` (extends sky130A) runs on
  `sky130_fd_sc_hs` (unit site 0.48 × 3.33 µm, its own tracks, special cells
  and boundary defaults). Example: `examples/dp8_hs.yaml`.

`sky130hd` (the ORFS platform copy inside the image) remains a fallback when
no open_pdks tree is installed. The PDKs read from ORFS platform files
(sky130hd, IHP SG13G2) are pinned with the ORFS image's digest; for a native
binary set `$SIPATH_PLATFORMS` to an ORFS `flow/platforms` directory.

**IHP SG13G2** (`pdk: ihp-sg13g2`): standard cells and sg13g2 IO pads from the
ORFS platform tree; routes to a DEF and three-corner SPEF and closes a pad ring
(`examples/ihp_ring.py`), without signoff decks.

## Setup

One supported path per runtime. Each ends with `python3 -m sipath doctor`,
which checks the machine (runtime, images, native tools, the PDK release and
every view its description names, optional tools), says what a run would use,
gives the fix for each problem and exits 1 when a run could not start
(`--deep` also runs every locked image and compares its tools' versions with
the lock).

**macOS (Docker).** The locked images are amd64 and run under Rosetta on
Apple silicon (see below for the arm64 options).

```bash
python3 -m pip install -e . ciel
python3 -m sipath install-pdk
docker pull openroad/orfs@sha256:751a77afcade9882b51427e6d9d079b8e270e7a8f4aa66df2d0659457d1c29fd
# optional, in this order: Magic (magic_drc, xor, integrate), then the precheck image built on it
docker build --platform linux/amd64 -t sipath/orfs-magic:8.3.685 docker/magic
docker build --platform linux/amd64 -t sipath/precheck:1.3.7 docker/precheck
python3 -m sipath doctor
```

A locally built image has another digest than the lock's: name it as
[recipe 8](examples.md#8-an-openframe-chip-prechecked-with-its-seal-ring)
shows (`SIPATH_MAGIC_IMAGE`, `SIPATH_PRECHECK_IMAGE`).

**Linux x86_64 (native).** The locked image's own OpenROAD, Yosys and
KLayout, without Docker; runs find them by themselves (`runtime: auto`).
Magic, Netgen and the precheck still need Docker and the images above.

```bash
python3 -m pip install -e . ciel
python3 -m sipath install-pdk
python3 -m sipath install-tools
python3 -m sipath doctor
```

**Colab.** The [tutorial notebook](../notebooks/sipath_tutorial.ipynb) does
the Linux native path in its first cells.

## Runtimes

**Which runtime.** `runtime` (`SIPATH_RUNTIME`) chooses: `auto` (the
default), `native` or `docker`. `auto` takes the first of `$SIPATH_OPENROAD`,
`openroad` on PATH, the locked tools unpacked by `install-tools` (below), and
the locked ORFS image if it is pulled. `native` stops before the image, and
`docker` takes only the image. So on Linux, once `install-tools` has run, runs
use the native tools with nothing else to set; elsewhere, or without it, they
use Docker. Both run the same builds: the tree is the locked image's own
files. A tree of another image (an older lock) is not used, and a warning
says so. Host directories are
mounted at the same path, so file names in Tcl are the same inside and
outside. On Linux the containers run as the calling user, so the files they
write stay the user's (`SIPATH_DOCKER_AS_ROOT=1` keeps the image's user).

Without Docker, on Linux x86_64 with glibc 2.35 or newer (Ubuntu 22.04+, Colab),
`python3 -m sipath install-tools [dir]` downloads the locked ORFS image's
layers from the registry, checks each against its digest and unpacks them
(1.6 GB to download; without ORFS's test designs and headers, 4 GB unpacked) into `dir` (default
`~/.cache/sipath/orfs-root`; another directory goes in `tools_root`,
`SIPATH_TOOLS_ROOT`). Its `sipath-bin/` holds `openroad`, `sta`, `yosys`,
`yosys-abc` and `klayout` wrappers that run the image's binaries with the
image's libraries; the steps call them and read the image's platform tree, with
nothing on PATH. Magic, Netgen and the precheck are other images, so their
checks need Docker.

On macOS, runs use the ORFS image (`openroad/orfs`, amd64: under Rosetta on Apple
silicon). Two ways to avoid the emulation:
- an arm64 image with the tools on PATH, e.g. LibreLane's multi-arch image
  3.0.16, by the digest `sipath/toolchain.json` records for it
  (`images.librelane_arm64`; a tag is refused while `require_digest` is on):

  ```bash
  export SIPATH_OPENROAD_IMAGE=ghcr.io/librelane/librelane@sha256:02d50f259d483979e126ef188a50f5b61e1d285b9d538312f280b087439b0302
  export SIPATH_OPENROAD_BINARY=openroad SIPATH_PLATFORM=linux/arm64
  export SIPATH_MAGIC_IMAGE="$SIPATH_OPENROAD_IMAGE"   # the same image for the Magic checks
  ```

  Its tool versions differ (OpenROAD 2026-02-17, Yosys 0.62), so results
  differ slightly (gcd8: 158 against 165 cells). Measurements under
  [Benchmarks](benchmarks.md#rosetta-amd64-against-native-arm64).
- `native_tools` (`SIPATH_NATIVE_TOOLS=yosys,klayout`): those tools run on the
  host while OpenROAD stays in the image (gcd8 synthesis 10.0 s to 4.4 s with
  Homebrew's Yosys).

Magic (opt-in checks) runs from `config.magic_image`
(`docker build --platform linux/amd64 -t sipath/orfs-magic:8.3.685 docker/magic`, on the locked ORFS
image). Each run's
provenance records the image digest, platform, binary, tool versions and the
PDK release.

**Runs are resumable.** `graph.json` records every attempt: params, parent,
status, checkpoint, metrics, process, timing (phases and slowest Tcl
commands), the OpenROAD transcript tail of a failed attempt, the run
configuration and provenance. `--resume` rebuilds the graph: a step asked
again with the same parent, variants and score, on the same inputs, returns its
recorded result; attempts of an interrupted sweep that finished are kept and
only the rest run.

**A resume never reuses an obsolete result.** Every run records what its
results depend on (`fingerprint.py`, `inputs` in `graph.json`): the content of
every input file (RTL and its include directories, netlist, SDC, floorplan
DEF, macro LEF, Liberty, GDS and CDL, extra views), the resolved design
description (design-file keys and the PDK defaults they take), the PDK
description and release, the tools (image digests, binaries, native tool
versions, plugin code) and the configuration keys that change results
(images, binaries, runtime, the placer's Python). Threads, timeouts and
container limits are left out: they change how long a run takes, not what it
produces. Every attempt's fingerprint chains these with its step, parameters,
the step's declared defaults and its parent's fingerprint. On `--resume`, a
step whose fingerprint differs runs again, and so does every step after it;
its recorded attempts stay in `graph.json` as `stale`, with the reason (`x.sdc
changed`, `its parent changed`, `its variants or score changed`), and the run
report and the CLI list them. Every step reads the SDC, the Liberty and the
LEF views, so any input change re-runs the flow from its first step; a change
to a step's variants re-runs from that step. A graph written before
fingerprints is resumed as before, with a warning that its inputs were not
checked. SiPath's own code is part of the fingerprint (its version and a
digest of its sources): a resume after the code changed runs again and says
so. The parameters fingerprinted are the ones execution resolves (the
variant, the design's and the PDK's `step_defaults`, the declared defaults),
as are those that prerequisites and admission judge, and each attempt
records the threads it ran with. A selected attempt's directory is never deleted, and no attempt
directory is reused. The library logs to the `sipath` logger and is silent
unless the caller configures logging.

## Run configuration

From a file (`--config`, `$SIPATH_CONFIG`), `SIPATH_*` variables, or a design
file's `config:` section (all keys in the [reference](reference.md)). Later
ones win: the defaults, then the file, then `SIPATH_*`, then the design file's
`config:`, so a design file's `parallel_cap: 4` holds against
`SIPATH_PARALLEL_CAP=1`; change the design file to change it:

- **threads:** the run's cores: every core by default, as ORFS (two thirds
  of them on macOS, where Docker's VM shares the host); shared among a step's
  attempts running at once, and inside a signoff among its concurrent checks
  (DRC and its rule groups, LVS, full LVS, STA), so they add up to the budget
  instead of each taking every core; a step's `threads` parameter asks for a
  number per attempt, and fewer attempts run at once when those would not fit;
- **budgets:** `budget_s` (the run's wall time: running attempts stop with
  status `budget` and `--resume` continues), `step_budget_s` (a time box per
  step: attempts not finished by then stop and the step selects among those
  that finished), `max_attempts`, and `memory_gb` (fewer attempts at once when
  the step's peak memory, recorded by an earlier attempt, says they would not
  fit). `graph.json` records each step's allocation (attempts at once, threads
  each, what limited it), and the verdict's execution status says `budget`.
  `sipath run <design> [workdir] --plan` shows the allocation before anything
  runs: each step's attempts, how many at once, threads each, a signoff's
  concurrent checks and their share, the times and memory peaks a previous run
  in `workdir` recorded, and what the budgets will cut;
- **timeouts** (per command and per signoff tool), the **parallel cap**,
  container CPU and memory limits, **debug checkpoints** of failed attempts,
  the log level;
- **images and tools:** the OpenROAD image (pinned by digest; an unpinned
  image is refused unless `SIPATH_REQUIRE_DIGEST=0`), the Magic image, native
  tools on the host, the OpenROAD binary, the platform, the macro placer's
  Python;
- **PDK release:** a PDK tree of another release is refused unless
  `SIPATH_REQUIRE_PDK=0`.


### Strict reproducibility and thread experiments

Set `strict_reproducibility: true` in run configuration, or
`SIPATH_STRICT_REPRODUCIBILITY=1`, to serialize attempts and keep their
OpenROAD thread allocation stable even on partial resume and retry. Explicit
per-step thread settings still apply. `threads`, container CPU/memory limits,
and recorded runtime resource settings then participate in cache identity;
changing them invalidates reuse. Switching into or out of strict mode also
invalidates reuse. Legacy graphs lacking fingerprints are refused in strict
mode; use a fresh directory. Forked graphs cannot be resumed in strict mode:
`Flow.fork` copies a selected ancestry without the original step fingerprints,
so strict reuse cannot validate those steps. Start a fresh fully fingerprinted
run when strict resume is required. Ordinary-mode fork resume is unchanged.
`--plan` reflects serial admission.

This is conservative cache behavior, not a promise of bit-identical output.
Tool internals, machine differences and nondeterminism can still change a
result. Ordinary mode retains resource-flexible resume for compatibility;
thread count is not assumed empirically invariant. Timeouts and run budgets
remain stop controls, not proof that two successful tool runs are identical.

Run fresh repeated physical experiments with:

```bash
python3 bench/thread_repro.py sipath/examples/gcd8.yaml --threads 1 2 4 --repeats 2 --out runs/thread-repro
```

The command refuses an existing output directory, runs sequentially with
strict configuration, and records every exit status, observed thread counts,
input/tool provenance, configuration, selected-stage metrics and artifact
hashes in `comparison.json`. Failed runs remain in the record and cause a
nonzero exit. Comparisons include both the first run and the first repeat
with the same thread count, so within-count variation is visible separately.
Checkpoint and DEF hashes compare exact bytes, not semantic equivalence;
missing artifacts are unknown rather than equal. Reports describe observed
variation only, not a global determinism guarantee.
