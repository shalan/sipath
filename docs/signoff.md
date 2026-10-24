# Signoff

Part of the [SiPath documentation](../README.md#documentation).

The `signoff` step (`sipath/signoff.py`) checks the final database; every
check can also run as a step of its own.

- **GDS.** The routed DEF streamed with every cell's layout, supply nets
  labelled. A cell no GDS defines (a LEF-only macro) makes DRC/LVS impossible
  and fails them. Each macro's GDS keeps its own cells: names that clash with
  a library or another macro are renamed, not merged; library GDS load
  first-definition-wins.
- **DRC.** open_pdks `sky130A_mr.drc`, its rule groups in parallel processes
  (`drc_split`; aes 209 s to 156 s); every marker counts, also by owner (`top`
  or the macro or library whose cell it is in). Metal density with the PDK's
  deck: gating for top-level layouts, reported for embedded macros.
- **LVS.** `sipath/pdks/sky130/sky130_stdcell.lvs` (derived from ORFS's deck)
  against a reference netlist from `write_cdl` after the supply connections
  are re-applied: only the subcircuits the design uses, tie cells as the poly
  shorts their layout is, supplies as ports. Black boxes (macros with
  `lvs: blackbox` or `abstract: true`, and the PDK's `signoff.lvs_blackbox`
  masters: Sky130's IO cells) are compared as their pins only and listed in
  `lvs.blackboxed`. A macro with neither a `cdl` nor `lvs: blackbox` fails LVS.
- **Antennas** (`check_antennas`).
- **Timing**, in a fresh OpenROAD process from only the final netlist,
  Liberty, SDC and per-corner SPEF: setup and hold on all nine corners,
  slew/cap/fanout on standard cells, parasitic annotation (a loaded net
  without parasitics fails), unconstrained endpoints.
  Each expected corner must have finite setup and hold results for those
  checks to pass; a missing corner or missing slack is not a timing pass.
- **Opt-in:** `magic_drc` (Magic `drc(full)`), `xor` (Magic's stream-out of the
  routed DEF against KLayout's; fill not compared), `lvs_full`: full-device
  LVS with the PDK's own deck (open_pdks `sky130.lvs`: high-voltage MOS, MIM
  caps, resistors, diodes), also as `sipath lvs <gds> <top> <schematic...> --pdk <name>`
  for a macro. Schematics are prepared for the deck (device aliases, the
  device prefix on CDL models, cell SPICE for black boxes, parallel MOS merged
  as the deck merges the layout's). Not yet matching: the ChipFoundry ADC's
  vendor schematic lacks its `decoder3to8`; on dp8 the deck and the PDK's cell
  netlists disagree (parallel devices merged across cells, tap diodes and tie
  resistors, well pins).

`signoff.json` holds every result; a failed required check fails the attempt.
Measured:
- gcd8, dp8 and dp8_hs: DRC 0, density clean, LVS match, antennas 0, timing
  met on nine corners.
- mcu8 (ChipFoundry macros signed off as abstracts, their GDS merged into the
  written GDS): DRC 0, LVS match (macros and IO cells as black boxes),
  antennas 0, setup +8.83 ns and hold +0.099 ns over nine corners, design
  rules clean, with all four analog nets shielded. On the full layouts the
  deck finds 95 markers inside the vendor cells (ADC: 86 `capm.2b_a`, 6
  `hvtp.2`, 2 `nsdm.1`; DAC: 1 `nwell.6`), for ChipFoundry to address.

Found by signoff and fixed (DRC alone sees none of the LVS ones):
- A VDD–VSS short: a strap's landing patch crossed a VSS stripe. Landings now
  avoid other nets on every layer they cross, and every step that adds straps
  fails if special shapes of two nets touch (`special_shorts`).
- Floating ESD clamps: the plain `*_hvc` power pads leave their clamp
  terminals to be routed; mcu8 uses the `*_clamped` pads, as Caravel does.
- 158 untapped row pieces beside the SRAM with floating n-wells: such pieces
  are cut and a row without a tap fails `physical`.
- Strap vias next to the router's own (merged, illegal cuts), off-grid pin
  edges copied from vendor LEFs and a notch to a fill shape: mcu8's top-level
  DRC went from 12 markers to 0.
- OpenRCX re-orders each net's wire and drops the router's minimum-area
  patches; every extraction restores the routing from a DEF written just
  before (geometry checked). A post-route minimum-area repair stays as a
  safety net and normally finds nothing.
- Cells added after the power grid (CTS buffers, fillers, ties, diodes) had
  unconnected supply pins; global connections are re-applied after every step
  that adds cells.

## The run's verdict

A finished run is not a verified one, so every run states both
(`sipath/verdict.py`). It reads the selected chain from the first step to the
last, with every check any attempt in it ran: synthesis' equivalence check,
`signoff` and the check steps (each check's own result, a later attempt
overriding an earlier one), and cf-precheck's checks. It writes the result to
`graph.json` (`verification`), at the top of the run report, into
`metrics.json` (`flow__verification__status`, `flow__execution__status` and
counts), into MCP's `run_step` result, and as the CLI's last line.

- **Execution:** `completed`, `failed` (the latest step's attempts failed or
  were interrupted), `cancelled`, `budget` (a run budget stopped it; `--resume`
  continues). A step whose attempts all ran and were
  rejected (an ECO that lost slack) is a note, not a failure, and the verdict
  covers the chain up to the latest step that selected an attempt.
- **Verification:** `passed` (every check that ran passed and the standard
  ones all ran: equivalence for an RTL design, and every check signoff
  requires by default), `failed` (a required check failed, also when
  `fail_on_violation: false` let the step pass), `inconclusive` (nothing
  failed, something could not be decided: an equivalence timeout),
  `omitted` (nothing failed or undecided, but standard checks did not run: no
  signoff, a subset of checks, equivalence off or skipped) or `completed` (no
  check ran at all).
- **The result is the run's head** (`head` in `graph.json`): the latest
  selection on the main chain, a bounded search's winner, an accepted ECO.
  The verdict, the report, `metrics.json`, the CLI and the benchmark export all
  read the result through that head's ancestry, never through another
  branch's selection; so do `sipath render`/`draw` without an attempt (an
  attempt or a branch's step named explicitly shows that one) and the
  regression limits.
- **A check holds for the design it saw.** A check followed on the chain by a
  step that changes the design (an ECO, a re-route, fill) is `stale`, not
  passed, with what changed it; the run's verification is then `omitted` until
  the check runs again. The RTL-to-netlist equivalence of synthesis is kept:
  later netlist changes check their own (ECOs and post-route repair).
- **What limits the checks that ran**, listed as notes: black boxes LVS
  checked at their pins only, abstract macros whose insides DRC did not see,
  unconstrained endpoints and pad ports, density judged in the parent for an
  embedded macro, a timeout accepted as a pass (`equiv_on_timeout: pass`), a
  failure that did not fail its step, a check that failed but was not
  required.

The aes benchmark run, for example, reads `execution completed; verification
FAILED; failed: setup, drv; inconclusive: equivalence`, with the note that
its signoff ran with `fail_on_violation: false`.

## Tapeout precheck

The `precheck` step runs ChipFoundry's own tapeout precheck,
[cf-precheck](https://github.com/chipfoundry/cf-precheck), on a signed-off
harness design (OpenFrame today). SiPath does not re-implement it: it runs the
tool itself, pinned, and reads its results.

- **Pinned:** cf-precheck 1.3.7 in the precheck image (`docker/precheck`:
  KLayout, Magic, Netgen 1.5.325, CVC 1.1.7, GNU awk), locked by digest in
  `sipath/toolchain.json` and verified by `toolchain --check`; the golden files
  it compares against (the frame's top netlist, the empty wrapper GDS, the
  Makefile) are fetched at the frame's pinned commit
  (`sipath/harnesses/sources/openframe/golden`, checksums in `SHA256SUMS`).
  cf-precheck makes no network calls; it is updated deliberately, never before
  a run.
- **Project:** the step exports the run in cf-precheck's layout:
  `gds/<wrapper>.gds` (the signed-off GDS), `verilog/gl/<wrapper>.v` (the netlist
  with supply pins that signoff writes) and `lvs/<wrapper>/lvs_config.json`
  (cf-precheck's own configuration for the flat wrapper).
- **Results:** `outputs["precheck"]` lists every check as pass, fail or skip
  with its details, from cf-precheck's `.cf/project.json`; a missing result,
  or the tool's verdict disagreeing with its checks, is a failure. A failed
  check fails the step unless `fail_on_violation: false`; `skip` and
  `magic_drc` pass through to the tool.

On the OpenFrame example (gcd on the east-edge pads, signed off without
KLayout LVS under Rosetta) all 12 checks pass: top cell, XOR against the
golden wrapper, KLayout FEOL, BEOL, off-grid, density, pin labels and zero
area, spikes, illegal cell names, LVS (Magic extraction and Netgen) and OEB
(CVC: 0 errors; 29 warnings for the unused GPIOs, which default to analog
mode). Magic DRC is optional and was not run.

Found by running it: SiPath's signoff squared a zero-extension point the
router writes right after a via (`( * * 0 )`, no metal) as if it were a corner,
which drew a zero-length met1 path; cf-precheck's zero-area and spike checks
flagged it and SiPath's own DRC did not. Repeated points are now left alone.

