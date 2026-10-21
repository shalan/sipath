"""Analog nets and supplies: NDRs, the row obstructions on the pin-access layers, analog supplies routed
like signals (their IO-ring pins parked meanwhile) to a temporary launch pin."""
from __future__ import annotations

from .. import tcl
from ..tcl import q, tcl_parse

ROW_BLOCKAGES = r"""
set block [ord::get_db_block]; set tech [ord::get_db_tech]
# merge vertically adjacent row segments with the same x extent into one box
set boxes {}
foreach row [$block getRows] {
  set b [$row getBBox]
  lappend boxes [list [$b xMin] [$b xMax] [$b yMin] [$b yMax]]
}
set boxes [lsort -index 2 -integer [lsort -index 0 -integer $boxes]]
set merged {}
foreach bx $boxes {
  lassign $bx x0 x1 y0 y1
  set hit 0
  for {set k 0} {$k < [llength $merged]} {incr k} {
    lassign [lindex $merged $k] mx0 mx1 my0 my1
    if {$mx0 == $x0 && $mx1 == $x1 && $my1 == $y0} { lset merged $k [list $mx0 $mx1 $my0 $y1]; set hit 1; break }
  }
  if {!$hit} { lappend merged $bx }
}
set ::flow_row_obs {}
foreach lname {@LAYERS@} {
  set l [$tech findLayer $lname]
  foreach m $merged { lassign $m x0 x1 y0 y1; lappend ::flow_row_obs [odb::dbObstruction_create $block $l $x0 $y0 $x1 $y1] }
}
llength $::flow_row_obs
"""

SUPPLY_PARK = r"""
# route an analog supply like a signal: IO-ring bus pins (connected by
# abutment, not by routing) move to a scratch net, the net becomes a regular
# signal net with a wide NDR; ::flow_park keeps the moved pins
set net [::flow_net @NET@]
set park [odb::dbNet_create [ord::get_db_block] "@NET@__ring"]
$park setSigType [[::flow_net @NET@] getSigType]; $park setSpecial  ;# routers ignore it
set ::flow_park(@NET@) {}
foreach it [$net getITerms] {
  set inst [$it getInst]; set t [[$inst getMaster] getType]
  if {[string match PAD* $t] || [string match ENDCAP* $t]} {
    $it disconnect; $it connect $park; lappend ::flow_park(@NET@) $it
  }
}
# the supply's own top-level terminal (on the pad's bond pin) is parked too
set ::flow_park_bt(@NET@) {}
foreach bt [$net getBTerms] { $bt connect $park; lappend ::flow_park_bt(@NET@) $bt }
set ::flow_supply_sig(@NET@) [$net getSigType]
$net clearSpecial
$net setSigType SIGNAL
# the detailed router routes only signal nets and refuses a signal net on a
# POWER pin: the remaining (macro) pins are typed SIGNAL for this route only
set ::flow_mterm_sig(@NET@) {}
foreach it [$net getITerms] {
  set mt [$it getMTerm]
  lappend ::flow_mterm_sig(@NET@) $mt [$mt getSigType]
  $mt setSigType SIGNAL
}
assign_ndr -ndr ASUPPLY -net @NET@
llength [$net getITerms]
"""

SUPPLY_UNPARK = r"""
set net [::flow_net @NET@]
foreach {mt st} $::flow_mterm_sig(@NET@) { $mt setSigType $st }
$net setSigType $::flow_supply_sig(@NET@)
foreach it $::flow_park(@NET@) { $it disconnect; $it connect $net }
foreach bt $::flow_park_bt(@NET@) { $bt connect $net }
odb::dbNet_destroy [::flow_net "@NET@__ring"]
llength [$net getITerms]
"""


def analog_ndr(s, design):
    s.run(tcl.analog_ndr(design))


def create_ndr(s, name: str, width: str, spacing: str):
    """A non-default rule: ``width`` and ``spacing`` as ``layer value ...`` lists."""
    s.run(f"create_ndr -name {name} -width {{{width}}} -spacing {{{spacing}}}")


def assign_ndr(s, ndr: str, net: str):
    s.run(f"assign_ndr -ndr {ndr} -net {q(net)}")


def park_supply(s, net: str) -> str:
    """The supply's IO-ring pins and terminal onto a scratch net, the net a signal with the ASUPPLY rule;
    how many pins it keeps (SUPPLY_PARK)."""
    return s.value(SUPPLY_PARK.replace("@NET@", q(net)).replace("@KEEP@", "{}"))


def unpark_supply(s, net: str):
    s.run(SUPPLY_UNPARK.replace("@NET@", q(net)))


def block_rows(s, layers) -> int:
    """Obstructions over the rows on ``layers`` (ROW_BLOCKAGES); how many."""
    return int(s.value(ROW_BLOCKAGES.replace("@LAYERS@", " ".join(layers))))


def unblock_rows(s):
    s.run("foreach o $::flow_row_obs { odb::dbObstruction_destroy $o }; set ::flow_row_obs {}")


def pin_dump(s, dump_tcl: str, inst: str, pin: str):
    """An instance's pin shapes and obstructions (``dump_tcl`` defines ``::flow_pc_dump``), parsed."""
    s.run(dump_tcl)
    return tcl_parse(s.value(f"::flow_pc_dump {q(inst)} {q('^' + pin + '$')}"))


def create_launch_pin(s, net: str, layer: str, box):
    """A temporary top-level pin ``<net>__launch`` on ``layer`` (box in DBU): the router's endpoint."""
    s.run(f"set bt [odb::dbBTerm_create [::flow_net {q(net)}] {q(net + '__launch')}]\n"
          "$bt setIoType INOUT\nset bp [odb::dbBPin_create $bt]\n"
          f"odb::dbBox_create $bp [[ord::get_db_tech] findLayer {layer}] {box[0]} {box[1]} {box[2]} {box[3]}\n"
          "$bp setPlacementStatus FIRM")


def destroy_launch_pin(s, net: str):
    s.run(f"odb::dbBTerm_destroy [[ord::get_db_block] findBTerm {q(net + '__launch')}]")
