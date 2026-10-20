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
  rules. Estimation RC is the calibrated nominal table scaled per layer and
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

## Runtimes

`find_openroad()` uses, in order: `$SIPATH_OPENROAD`, `openroad` on PATH, then
the ORFS docker image (the digest locked in `sipath/toolchain.json`, or
`$SIPATH_OPENROAD_IMAGE`) if it is already pulled. Host directories are
mounted at the same path, so file names in Tcl are the same inside and
outside. On Linux the containers run as the calling user, so the files they
write stay the user's (`SIPATH_DOCKER_AS_ROOT=1` keeps the image's user).

The default is the ORFS image (`openroad/orfs`, amd64: under Rosetta on Apple
silicon). Two ways to avoid the emulation:
- an arm64 image with the tools on PATH, e.g. LibreLane's multi-arch image:
  `SIPATH_OPENROAD_IMAGE=ghcr.io/librelane/librelane:3.0.16
  SIPATH_OPENROAD_BINARY=openroad SIPATH_PLATFORM=linux/arm64` (and
  `SIPATH_MAGIC_IMAGE` the same image for the Magic checks). Its tool versions
  differ (OpenROAD 2026-02-17, Yosys 0.62), so results differ slightly (gcd8:
  158 against 165 cells). Measurements under
  [Benchmarks](benchmarks.md#rosetta-amd64-against-native-arm64).
- `native_tools` (`SIPATH_NATIVE_TOOLS=yosys,klayout`): those tools run on the
  host while OpenROAD stays in the image (gcd8 synthesis 10.0 s to 4.4 s with
  Homebrew's Yosys).

Magic (opt-in checks) runs from `config.magic_image`
(`docker build -t sipath/orfs-magic:8.3.685 docker/magic`). Each run's
provenance records the image digest, platform, binary, tool versions and the
PDK release.

**Runs are resumable.** `graph.json` records every attempt: params, parent,
status, checkpoint, metrics, process, timing (phases and slowest Tcl
commands), the OpenROAD transcript tail of a failed attempt, the run
configuration and provenance. `--resume` rebuilds the graph: a step asked
again with the same parent, variants and score returns its recorded result;
attempts of an interrupted sweep that finished are kept and only the rest
run. A selected attempt's directory is never deleted, and no attempt
directory is reused. The library logs to the `sipath` logger and is silent
unless the caller configures logging.

## Run configuration

From a file (`--config`, `$SIPATH_CONFIG`), `SIPATH_*` variables, or a design
file's `config:` section (all keys in the [reference](reference.md)):

- **threads:** every core by default, as ORFS (two thirds of them on macOS,
  where Docker's VM shares the host); per step with the `threads` parameter of
  every OpenROAD step, and shared among a step's attempts running at once;
- **timeouts** (per command and per signoff tool), the **parallel cap**,
  container CPU and memory limits, **debug checkpoints** of failed attempts,
  the log level;
- **images and tools:** the OpenROAD image (pinned by digest; an unpinned
  image is refused unless `SIPATH_REQUIRE_DIGEST=0`), the Magic image, native
  tools on the host, the OpenROAD binary, the platform, the macro placer's
  Python;
- **PDK release:** a PDK tree of another release is refused unless
  `SIPATH_REQUIRE_PDK=0`.
