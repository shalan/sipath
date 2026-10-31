"""Placement and clock-tree operations."""
from __future__ import annotations

from .. import tcl
from ..tcl import q, qlist, tcl_words


def check_placement(s):
    return s.run("check_placement -verbose", check=False)


def detailed_placement(s):
    s.run("detailed_placement")


def inst_count(s) -> int:
    return int(s.value("llength [[ord::get_db_block] getInsts]"))


def repair_tie_fanout(s, master_port: str):
    s.run(f"repair_tie_fanout -separation 5 {q(master_port)}")


def remove_buffers(s):
    s.run("remove_buffers")


def global_place_skip_io(s, density: float, seed) -> str:
    """A first global placement ignoring the IO pins (they are placed after it, by the cells); its transcript."""
    return s.run(tcl.global_place(density, seed, timing_driven=False) + " -skip_io").output


def global_place(s, density: float, seed, timing_driven: bool, keep_resize: bool, routability_driven: bool) -> str:
    """Global placement; its transcript (the target density gpl used is in it)."""
    return s.run(tcl.global_place(density, seed, timing_driven=timing_driven, keep_resize=keep_resize,
                                  routability_driven=routability_driven)).output


def place_pins(s, pdk):
    s.run(tcl.place_pins(pdk))


def channel_blockages_tcl(channel_row: str) -> str:
    """Pin channels out of global placement: their rows blocked, the core area the region's rows (what gpl
    sees without channels; restore_pin_channels puts the core back). Returns the blocked rows."""
    return f"""set ::flow_pinch_blk {{}}; set __r {{}}
foreach r [[ord::get_db_block] getRows] {{
  set b [$r getBBox]
  if {{![string match {channel_row}_* [$r getName]]}} {{
    lappend __r [$b xMin] [$b yMin] [$b xMax] [$b yMax]
    continue
  }}
  lappend ::flow_pinch_blk [odb::dbBlockage_create [ord::get_db_block] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
}}
set ::flow_core [[ord::get_db_block] getCoreArea]
set ::flow_core [list [$::flow_core xMin] [$::flow_core yMin] [$::flow_core xMax] [$::flow_core yMax]]
if {{[llength $::flow_pinch_blk]}} {{
  lassign [lrange $__r 0 3] x0 y0 x1 y1
  foreach {{a b c d}} $__r {{ set x0 [expr {{min($x0, $a)}}]; set y0 [expr {{min($y0, $b)}}]
                            set x1 [expr {{max($x1, $c)}}]; set y1 [expr {{max($y1, $d)}}] }}
  [ord::get_db_block] setCoreArea [odb::new_Rect $x0 $y0 $x1 $y1]
}}
llength $::flow_pinch_blk"""


def block_pin_channels(s, channel_row: str) -> int:
    return int(s.value(channel_blockages_tcl(channel_row)))


CHANNEL_RESTORE = ("foreach b $::flow_pinch_blk { odb::dbBlockage_destroy $b }\nunset ::flow_pinch_blk\n"
                   "[ord::get_db_block] setCoreArea [odb::new_Rect {*}$::flow_core]")


def restore_pin_channels(s):
    s.run(CHANNEL_RESTORE)


# per signal pin with cells on its net: {pin x y {buffer chain from the pin} last_x last_y}; the chain:
# single-load nets whose load is a buffer (master *buf*), at least two; last: the last buffer's centre
PIN_CHAINS = r"""set __o {}
foreach __t [[ord::get_db_block] getBTerms] {
  set __n [$__t getNet]
  if {$__n eq "NULL" || [$__n isSpecial] || ![llength [$__n getITerms]] || ![llength [$__t getBPins]]} continue
  set __b [[lindex [$__t getBPins] 0] getBBox]
  set __chain {}
  while 1 {
    set __loads {}
    foreach __i [$__n getITerms] { if {![$__i isOutputSignal]} { lappend __loads $__i } }
    if {[llength $__loads] != 1} break
    set __inst [[lindex $__loads 0] getInst]
    if {![string match *buf* [[$__inst getMaster] getName]] || [lsearch -exact $__chain [$__inst getName]] >= 0} break
    set __out "NULL"
    foreach __i [$__inst getITerms] { if {[$__i isOutputSignal]} { set __out [$__i getNet] } }
    if {$__out eq "NULL"} break
    lappend __chain [$__inst getName]
    set __n $__out
  }
  if {[llength $__chain] < 2} continue
  set __lb [[::flow_inst [lindex $__chain end]] getBBox]
  lappend __o [list [$__t getName] [expr {([$__b xMin] + [$__b xMax]) / 2}] [expr {([$__b yMin] + [$__b yMax]) / 2}] \
    $__chain [expr {([$__lb xMin] + [$__lb xMax]) / 2}] [expr {([$__lb yMin] + [$__lb yMax]) / 2}]]
}
set __o"""

ROW_BOXES = ("set __o {}; foreach r [[ord::get_db_block] getRows] { set b [$r getBBox]; "
             "lappend __o [list [$r getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] }; set __o")


def row_boxes(s) -> list:
    """[(row name, x0, y0, x1, y1)] in DBU."""
    return [(w[0], *map(int, w[1:])) for w in map(tcl_words, tcl_words(s.value(ROW_BOXES)))]


def pin_chains(s) -> list:
    """The buffer chains on signal pins' nets (PIN_CHAINS), as word lists."""
    return list(map(tcl_words, tcl_words(s.value(PIN_CHAINS))))


def move_centres(s, moves):
    """Place each instance with its centre at (x, y) DBU: [(name, (x, y)), ...]."""
    s.run("\n".join(f"set __i [::flow_inst {q(b)}]; set __m [$__i getMaster]\n"
                    f"$__i setLocation [expr {{{x} - [$__m getWidth] / 2}}] [expr {{{y} - [$__m getHeight] / 2}}]\n"
                    "$__i setPlacementStatus PLACED" for b, (x, y) in moves))


# the nets from each signal port (clock ports skipped) through its buffer chain: forward from an input
# (while a net's one load is a buffer), backward from an output (while its driver is a buffer whose
# input net has that one load), the net past the chain included; as STA nets
PORT_NETS = r"""set __o {}
foreach __t [[ord::get_db_block] getBTerms] {
  set __n [$__t getNet]
  if {$__n eq "NULL" || [$__n isSpecial] || ![llength [$__n getITerms]]} continue
  if {[$__t getName] in $::flow_skip_ports} continue
  set __seen {}
  while {[lsearch -exact $__seen $__n] < 0} {
    lappend __seen $__n
    set __next "NULL"
    if {[$__t getIoType] eq "OUTPUT"} {
      foreach __i [$__n getITerms] {
        if {![$__i isOutputSignal] || ![string match *buf* [[[$__i getInst] getMaster] getName]]} continue
        foreach __j [[$__i getInst] getITerms] { if {[$__j isInputSignal]} { set __next [$__j getNet] } }
      }
    } else {
      set __loads {}
      foreach __i [$__n getITerms] { if {![$__i isOutputSignal]} { lappend __loads $__i } }
      if {[llength $__loads] == 1 && [string match *buf* [[[[lindex $__loads 0] getInst] getMaster] getName]]} {
        foreach __j [[[lindex $__loads 0] getInst] getITerms] {
          if {[$__j isOutputSignal]} { set __next [$__j getNet] }
        }
      }
    }
    if {$__next eq "NULL"} break
    set __loads 0
    foreach __i [$__next getITerms] { if {![$__i isOutputSignal]} { incr __loads } }
    set __n $__next
    if {$__loads != 1} { lappend __seen $__n; break }
  }
  foreach __n $__seen { if {[lsearch -exact $__o $__n] < 0} { lappend __o $__n } }
}
set ::flow_port_nets [lmap __n $__o { sta::db_net_to_sta $__n }]
llength $::flow_port_nets"""


def collect_port_nets(s, skip_ports) -> int:
    """The signal ports' buffer-chain nets into ``::flow_port_nets`` (PORT_NETS); how many."""
    return int(s.value(f"set ::flow_skip_ports {qlist(skip_ports)}\n" + PORT_NETS))


def repair_port_nets(s, wire_um: float, slew: float, cap: float):
    """``rsz::repair_net_cmd`` with a wire length limit on every net of ``::flow_port_nets``, then legalization."""
    s.run(f"estimate_parasitics -placement\nforeach __n $::flow_port_nets {{ rsz::repair_net_cmd $__n "
          f"[sta::distance_ui_sta {wire_um:g}] {slew} {cap} }}\ndetailed_placement")


def repair_design_legalized(s, util: str, pdk):
    """repair_design on placement estimates, then legalization (``util``: the -max_utilization option text)."""
    s.run(f"estimate_parasitics -placement\nrepair_design{util}{tcl.design_margins(pdk)}\ndetailed_placement")


def begin_blockages(s):
    """Remember the block's blockages, so record_blockages can tell the ones created after."""
    s.run("set ::flow_place_blockages {}\nset ::flow_blockages_before [[ord::get_db_block] getBlockages]")


def create_blockage(s, box, max_density):
    s.run(f"create_blockage -region {{{box[0]} {box[1]} {box[2]} {box[3]}}} -max_density {max_density}")


def record_blockages(s):
    """The blockages created since begin_blockages, into ``::flow_place_blockages``."""
    s.run("foreach __b [[ord::get_db_block] getBlockages] {\n"
          "  if {$__b ni $::flow_blockages_before} { lappend ::flow_place_blockages $__b }\n}")


def remove_recorded_blockages(s):
    s.run("if {[info exists ::flow_place_blockages]} {\n"
          "  foreach __b $::flow_place_blockages { odb::dbBlockage_destroy $__b }\n"
          "  set ::flow_place_blockages {}\n}")


def repair_after_place(s, max_util, pdk):
    s.run(tcl.repair_after_place(max_util, pdk))


def improve_placement(s):
    """ORFS's placement cleanup: shorter wires, less congestion for the router."""
    s.run('improve_placement -max_displacement "5 1"\noptimize_mirroring')


def net_terminals(s, net: str) -> list[str]:
    """``inst/pin`` and port names on ``net``, sorted."""
    return tcl_words(s.value(f"set o {{}}; set net [::flow_net {q(net)}]; "
                             "foreach t [$net getITerms] { lappend o \"[[$t getInst] getName]/[[$t getMTerm] getName]\" }; "
                             "foreach t [$net getBTerms] { lappend o [$t getName] }; lsort $o"))


def configure_cts_slew(s, max_slew):
    s.run(f"configure_cts_characterization -max_slew {float(max_slew)}")


def clock_tree(s, pdk, nets, sink_clustering_size=None, sink_clustering_levels=None, distance_between_buffers=None):
    s.run(tcl.cts(pdk, nets, sink_clustering_size, sink_clustering_levels, distance_between_buffers))
