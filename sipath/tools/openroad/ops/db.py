"""Queries on the open database: units, die, rows, nets, instances, slack."""
from __future__ import annotations

from ..tcl import q, qlist, tcl_parse, tcl_words


def dbu(s) -> int:
    """Database units per micron."""
    return int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))


def die_box(s) -> tuple[int, int, int, int]:
    """The die area (DBU)."""
    return tuple(map(int, tcl_words(s.value("set b [[ord::get_db_block] getDieArea]; "
                                            "list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))))


def row_count(s) -> int:
    return int(s.value("llength [[ord::get_db_block] getRows]"))


def layer_is_vertical(s, layer: str) -> bool:
    return s.value(f"[[ord::get_db_tech] findLayer {layer}] getDirection") == "VERTICAL"


def net_has_bterms(s, net: str) -> bool:
    return s.value(f"llength [[::flow_net {q(net)}] getBTerms]") != "0"


def clock_port_net(s, port: str) -> str:
    """The net on clock port ``port`` (a Tcl error when there is no such port)."""
    return s.value(f"set b [[ord::get_db_block] findBTerm {q(port)}]; "
                   f"if {{$b eq \"NULL\"}} {{ error {q('no clock port ' + port)} }}; [$b getNet] getName")


def require_net(s, net: str) -> None:
    """A Tcl error unless the database has net ``net`` (``::flow_net``)."""
    s.run(f"::flow_net {q(net)}")


def supply_pin_data(s, inst: str):
    """``::flow_supply_pin_data``: orientation, box, pin shapes and the PDN shapes of their nets, parsed."""
    return tcl_parse(s.value(f"::flow_supply_pin_data {q(inst)}"))


def macro_endpoints(s, nets) -> set[str]:
    """Hard-macro instances on any of ``nets``."""
    return set(tcl_words(s.value(f"""
set o {{}}
foreach n {qlist(nets)} {{
  foreach t [[::flow_net $n] getITerms] {{ if {{[[[$t getInst] getMaster] isBlock]}} {{ lappend o [[$t getInst] getName] }} }}
}}
lsort -unique $o""")))


def tie_max_fanout(s, ties) -> int:
    """The largest fanout of an output of a tie cell (masters ``ties``)."""
    return int(s.value(f"""
set worst 0
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[[$inst getMaster] getName] ni {qlist(ties)}}} continue
  foreach it [$inst getITerms] {{
    if {{![$it isOutputSignal]}} continue
    set n [$it getNet]; if {{$n eq "NULL"}} continue
    set f [expr {{[llength [$n getITerms]] - 1 + [llength [$n getBTerms]]}}]
    if {{$f > $worst}} {{ set worst $f }}
  }}
}}
set worst"""))


def inst_master_types(s, insts) -> dict[str, str]:
    """{instance: master type} (UNKNOWN for a name the database lacks)."""
    flat = tcl_words(s.value(
        f"set o {{}}; foreach n {qlist(insts)} {{ set i [::flow_find_inst $n]; "
        "lappend o $n [expr {$i eq \"NULL\" ? \"UNKNOWN\" : [[$i getMaster] getType]}] }; set o"))
    return dict(zip(flat[0::2], flat[1::2]))


def unrouted_net_count(s) -> int:
    """Signal nets with two or more terminals and no wire."""
    return int(s.value("""
set n 0
foreach net [[ord::get_db_block] getNets] {
  if {[$net isSpecial] || [$net getSigType] in {POWER GROUND}} continue
  if {[llength [$net getITerms]] + [llength [$net getBTerms]] < 2} continue
  if {[$net getWire] eq "NULL"} { incr n }
}
set n"""))


def worst_slacks(s) -> list[str]:
    """[setup worst slack, hold worst slack] as STA prints them."""
    return tcl_words(s.value("list [sta::worst_slack -max] [sta::worst_slack -min]"))


def flow_metrics(s) -> str:
    """The flow helpers' metric list (``::flow_metrics``)."""
    return s.value("::flow_metrics")
