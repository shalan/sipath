# Workarounds for this OpenROAD build

Part of the [SiPath documentation](../README.md#documentation).

Found by running it:
- `place_pins -random` is a no-op: automatic pins are placed provisionally at
  floorplan without it, and again after global placement.
- `write_timing_model` takes `-scene`, not `-corner`.
- After `pin_access`, a `repair_antennas` following a second detailed route
  segfaults (`grt::GlobalRouter::findNets`): `pin_access` is off by default.
- A second `global_route` in one process fails or inherits stale state
  (GRT-0229, an "infinite usage" gcell): repair between global routes uses
  `-start_incremental`/`-end_incremental`, and post-route repair is its own
  step (a fresh process). Patch routes time out after 30 min.
- `clock_tree_synthesis -clk_nets` splits its value on spaces: names are
  passed space-joined, not list-quoted.
- With several corner libraries loaded, CTS cannot resolve its dummy-load
  cells (CTS-0118): multi-corner runs pass `-dont_use_dummy_load`. Liberty
  cells without a LEF master are set dont_use.
- The detailed router can end a segment with extension 0 at a same-layer
  corner (a diagonal pinch under the minimum width); signoff squares such
  corners (`corners_squared`).
- The global router places pins outside the die off its grid (harness pins
  are clipped for routing); the detailed router takes one pin per signal port
  (duplicate template pins keep one copy).
- `dbSBox_create` silently returns NULL for a box whose width and length are
  both odd (DBU): boxes are made even, and every create is checked.
- DPL's blocked-layer check flags a macro an analog wire lands on;
  `check_placement` tolerates exactly that.
- `make_fake_io_site` segfaults when a site already exists: sites are created
  once per session.
- Post-route setup repair with buffering segfaults inside `repair_timing`:
  it is sizing only; hold repair (buffers) runs separately.
- `add_pdn_ring -connect_to_pads` silently connects nothing to the sky130
  power-pad tabs, so the power connector does it.
- The detailed router refuses POWER nets (DRT-0305) and SIGNAL nets on POWER
  pins (DRT-0307): analog supplies are routed with their pins retyped SIGNAL
  for that route only.
- DRT re-reads its own L-shaped wires from a checkpoint as non-orthogonal
  (DRT-1010): before an ECO re-route, wires are re-encoded one path per
  segment.
- Timing-driven placement diverges inside a fence after its first timing
  iteration: with fences, `-keep_resize_below_overflow 0`.
- `dbWireDecoder` segfaults from Tcl, so analog routing is read back from a
  DEF written by the same process.
- KLayout 0.30.7 crashes on `connect_implicit` for the gpiov2 pad: LVS joins
  pin pieces through private per-pin layers; its SPICE reader upcases names,
  so black boxes are blanked under both spellings.
- The sky130 pads need `-rotation_vertical R180 -rotation_horizontal R90`; a
  check fails the attempt if a pad's core-side pin does not face the core.
- The router keeps only the minimum spacing from special wiring, whatever the
  analog net's NDR: hence the analog guard.
- The router writes `( * * 0 )` (a zero-extension point at the via's
  location) right after a via; it draws no metal and must not be squared as a
  corner (that drew a zero-length path cf-precheck rejects).
- In an incremental patch route, the detailed router rips up neighbouring nets
  that have no global-route guides and cannot route them back (four nets lost
  on gcd), and a second global route in the process fails: post-route repair
  ends that attempt with `RetryWith` and a fresh attempt routes fully.
- On a routed design `repair_timing` re-estimates the nets it changes with the
  incremental global router, whatever the parasitics source
  (`::est::set_parasitics_src placement` or `detailed_routing`, or the
  database guides cleared); when a resized cell's pin leaves its net's
  guides, that router fails (aes with the typical-corner netlist: GRT-0304,
  then GRT-0229 with usage 65534). `route_repair` ends that attempt with
  `RetryWith` and a fresh attempt runs without post-route timing repair
  (`rounds: 0`); its outputs say why (`skipped_because`, the attempt's
  `retry_reason`) and signoff judges the timing.
