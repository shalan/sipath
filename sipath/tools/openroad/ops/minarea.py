"""OpenROAD operations of the post-route minimum-area repair (sipath.minarea): the technology's area rules, the
blocks and port shapes a patch must respect, writing the patches."""
from __future__ import annotations

from ..tcl import tcl_parse

TECH_DB = r"""
set o {}
foreach l [[ord::get_db_tech] getLayers] {
  if {[$l getType] ne "ROUTING"} continue
  lappend o [list [$l getName] [$l getWidth] [$l getArea] [$l getSpacing] [$l getDirection] [$l getPitch]]
}
set v {}
foreach via [[ord::get_db_tech] getVias] {
  set bs {}
  foreach b [$via getBoxes] {
    set L [$b getTechLayer]
    if {$L ne "NULL" && [$L getType] eq "ROUTING"} { lappend bs [list [$L getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] }
  }
  lappend v [list [$via getName] $bs]
}
list $o $v
"""


def tech_db(s) -> list:
    """[routing layers (name width area spacing direction pitch), vias] from the loaded technology (DBU)."""
    return tcl_parse(s.value(TECH_DB))


BLOCKS_AND_PORTS_TCL = """set o {}
foreach i [[ord::get_db_block] getInsts] {
  set m [$i getMaster]
  if {[$m isBlock] || [string match PAD* [$m getType]]} {
    set b [$i getBBox]; lappend o [list [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
  }
}
set p {}
foreach bt [[ord::get_db_block] getBTerms] {
  set n [[$bt getNet] getName]
  foreach bp [$bt getBPins] { foreach bx [$bp getBoxes] {
    set l [$bx getTechLayer]; if {$l eq "NULL"} continue
    lappend p [list $n [$l getName] [$bx xMin] [$bx yMin] [$bx xMax] [$bx yMax]]
  } }
}
list $o $p"""


def blocks_and_ports(s) -> list:
    """[hard macro and pad boxes, top-level port shapes (net layer box)] in DBU."""
    return tcl_parse(s.value(BLOCKS_AND_PORTS_TCL))


def write_patches(s, nets: dict, via_layers: dict):
    """Each patched net's whole routing, encoded here (ops.wires.encoder_tcl), in one call: ``nets`` maps a net
    name to its paths (the patch rectangles appended), ``via_layers`` a via to its (bottom, top) layers."""
    from .wires import encoder_tcl
    s.run("\n".join(encoder_tcl(n, info, via_layers) for n, info in nets.items()))
