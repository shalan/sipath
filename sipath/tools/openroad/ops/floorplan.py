"""Floorplan and power grid: die, rows, pins, fences and clusters, taps and endcaps, the PDN."""
from __future__ import annotations

from .. import tcl
from ..tcl import qe, tcl_words


def used_port_names(s) -> list[str]:
    """Top-level ports whose net reaches an instance pin."""
    return tcl_words(s.value(
        "set o {}; foreach t [[ord::get_db_block] getBTerms] { set n [$t getNet]; "
        "if {$n ne \"NULL\" && [llength [$n getITerms]]} { lappend o [$t getName] } }; set o"))


def floorplan_from_def(s, pdk, def_path: str):
    s.run(tcl.floorplan_from_def(pdk, def_path))


def harness_power_pins(s, script: str):
    """The frame's user-supply pins (the harness writes the Tcl)."""
    s.run(script)


def harness_clip_pins(s, script: str) -> int:
    """Clip template pins overhanging the boundary (the harness writes the Tcl); how many."""
    return int(s.value(script))


def floorplan_die(s, pdk, design):
    s.run(tcl.floorplan_die(pdk, design))


def place_pins(s, pdk, random: bool = False):
    s.run(tcl.place_pins(pdk, random=True) if random else tcl.place_pins(pdk))


def core_box(s) -> list[str]:
    return tcl_words(s.value(
        "set b [[ord::get_db_block] getCoreArea]; list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))


def channel_rows_and_origins(s, channel_row: str) -> list[str]:
    """[pin-channel rows, core xMin, core yMin, region rows' xMin, yMin, DBU per micron]."""
    return tcl_words(s.value(f"""set __r {{}}; set __c 0
foreach __row [[ord::get_db_block] getRows] {{
  if {{[string match {channel_row}_* [$__row getName]]}} {{ incr __c; continue }}
  set __b [$__row getBBox]; lappend __r [$__b xMin] [$__b yMin]
}}
set __a [[ord::get_db_block] getCoreArea]
list $__c [$__a xMin] [$__a yMin] [::tcl::mathfunc::min {{*}}[lmap {{x y}} $__r {{ set x }}]] \
  [::tcl::mathfunc::min {{*}}[lmap {{x y}} $__r {{ set y }}]] [[ord::get_db_block] getDbUnitsPerMicron]"""))


def rows_without_tap(s, tap: str) -> list[str]:
    """Rows with no instance of master ``tap`` on them."""
    return tcl_words(s.value(f"""
set __taps {{}}
foreach __i [[ord::get_db_block] getInsts] {{
  if {{[[$__i getMaster] getName] ne {qe(tap)}}} continue
  set __b [$__i getBBox]
  dict lappend __taps [$__b yMin] [list [$__b xMin] [$__b xMax]]
}}
set __out {{}}
foreach __r [[ord::get_db_block] getRows] {{
  set __rb [$__r getBBox]; set __ok 0
  if {{[dict exists $__taps [$__rb yMin]]}} {{
    foreach __t [dict get $__taps [$__rb yMin]] {{
      if {{[lindex $__t 0] >= [$__rb xMin] && [lindex $__t 1] <= [$__rb xMax]}} {{ set __ok 1; break }}
    }}
  }}
  if {{!$__ok}} {{ lappend __out [$__r getName] }}
}}
set __out"""))


def macros_in_box(s, halo_um: float, box) -> str:
    """Hard macros whose box grown by ``halo_um`` overlaps ``box`` (DBU), as a Tcl list."""
    return s.value(f"""
set hit {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{![[$inst getMaster] isBlock]}} continue
  set b [$inst getBBox]; set h [expr {{int({halo_um} * [[ord::get_db_block] getDbUnitsPerMicron])}}]
  if {{[$b xMin] - $h < {box[2]} && [$b xMax] + $h > {box[0]} && [$b yMin] - $h < {box[3]} && [$b yMax] + $h > {box[1]}}} {{
    lappend hit [$inst getName]
  }}
}}
set hit""")


def create_fence(s, group, box) -> str:
    """A fence region for ``group`` (instances in it)."""
    return s.value(tcl.fence(group, box))


def create_cluster(s, group) -> str:
    return s.value(tcl.cluster_group(group))


def endcaps_and_taps(s, pdk):
    s.run(tcl.endcaps_and_taps(pdk))


def pdn(s, design, pdk, straps):
    s.run(tcl.pdn(design, pdk, straps))


def master_count(s, master: str) -> int:
    return int(s.value(
        f"set n 0; foreach i [[ord::get_db_block] getInsts] {{ if {{[[$i getMaster] getName] eq {qe(master)}}} {{ incr n }} }}; set n"))


def endcap_count(s) -> int:
    return int(s.value(
        "set n 0; foreach i [[ord::get_db_block] getInsts] { if {[string match PHY_EDGE* [$i getName]]} { incr n } }; set n"))
