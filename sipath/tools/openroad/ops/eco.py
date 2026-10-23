"""OpenROAD operations of the ECO and post-route repair libraries (sipath.eco): netlist snapshots, cell
replacement and buffer insertion, ripping up wires, re-routing a subset, the questions their checks ask."""
from __future__ import annotations

from .. import tcl
from ..tcl import q, qlist, tcl_parse, tcl_words

LOCATIONS_TCL = """
set out {}
foreach inst [[ord::get_db_block] getInsts] {
  set t [[$inst getMaster] getType]
  if {$t eq "CORE_SPACER"} continue
  set l [$inst getLocation]
  lappend out [$inst getName] [lindex $l 0] [lindex $l 1] [$inst getOrient]
}
set out
"""

MASTERS_TCL = """
set out {}
foreach inst [[ord::get_db_block] getInsts] {
  if {[[$inst getMaster] getType] eq "CORE_SPACER"} continue
  lappend out [$inst getName] [[$inst getMaster] getName]
}
set out
"""

NET_NAMES_TCL = "set o {}; foreach n [[ord::get_db_block] getNets] { lappend o [$n getName] }; join $o \\n"


def locations(s) -> dict[str, tuple]:
    """Every instance but spacers: name -> (x, y, orientation)."""
    f = tcl_words(s.value(LOCATIONS_TCL))  # a Tcl list: a bus register's name is braced
    return {f[i]: (f[i + 1], f[i + 2], f[i + 3]) for i in range(0, len(f), 4)}


def masters(s) -> dict[str, str]:
    f = tcl_words(s.value(MASTERS_TCL))
    return dict(zip(f[0::2], f[1::2]))


def inst_nets(s, insts) -> set[str]:
    """The signal nets on the pins of ``insts`` (supplies and special nets left out)."""
    if not insts:
        return set()
    out = s.value(f"""
set out {{}}
foreach n {qlist(sorted(insts))} {{
  set i [::flow_find_inst $n]
  if {{$i eq "NULL"}} continue
  foreach it [$i getITerms] {{
    set net [$it getNet]
    if {{$net eq "NULL" || [$net isSpecial] || [$net getSigType] in {{POWER GROUND}}}} continue
    lappend out [$net getName]
  }}
}}
join $out \\n""")
    return {line for line in out.splitlines() if line}


def resize(s, inst: str, master: str) -> str:
    """replace_cell; returns the instance's master afterwards (replace_cell does nothing when the pins differ)."""
    s.run(f"::flow_inst {q(inst)}\nreplace_cell {q(inst)} {q(master)}")
    return s.value(f"[[::flow_inst {q(inst)}] getMaster] getName")


def net_names(s) -> set[str]:
    return set(s.value(NET_NAMES_TCL).splitlines())


def insert_buffer(s, net: str, master: str, loads=None, name: str | None = None):
    """A buffer of ``master`` on ``net`` (before ``loads``, instance/pin names, when given; named ``name``)."""
    args = [f"-buffer_cell {master}", f"-net {q(net)}"]
    if loads:
        args.append(f"-load_pins [get_pins {qlist(loads)}]")
    if name:
        args.append(f"-buffer_name {q(name)}")
    s.run(f"::flow_net {q(net)}\ninsert_buffer {' '.join(args)}")


def destroy_wires(s, nets=None) -> int:
    """Rip up the signal wiring (of ``nets``; every signal net with None); returns how many wires."""
    sel = "1" if nets is None else f"[lsearch -exact $keep [$net getName]] >= 0"
    keep = f"set keep {qlist(sorted(nets))}" if nets is not None else "set keep {}"
    return int(s.value(f"""
{keep}
set n 0
foreach net [[ord::get_db_block] getNets] {{
  if {{[$net isSpecial] || [$net getSigType] in {{POWER GROUND ANALOG}}}} continue
  if {{!({sel})}} continue
  set w [$net getWire]
  if {{$w ne "NULL"}} {{ odb::dbWire_destroy $w; incr n }}
  $net clearGuides
}}
set n"""))


def via_layers(s, query_tcl: str) -> list[str]:
    """The flat (via, bottom, top) list a via-layer query (analog.query_via_layers_tcl) answers."""
    return s.value(query_tcl).split()


def write_verilog(s, path: str):
    s.run(f"write_verilog {q(path)}")


def remove_fillers(s):
    s.run("remove_fillers")


def fill(s, pdk):
    """The PDK's filler cells into the gaps (tcl.filler)."""
    s.run(tcl.filler(pdk))


def route_all_digital(s, drc: str, iters: int):
    """Global and detailed route of every digital net (tcl.route_digital); the result carries the transcript."""
    return s.run(tcl.route_digital(drc, iters))  # config.command_timeout


def route_nets(s, nets, drc: str, iters: int):
    """Route only ``nets`` (tcl.route_subset); the result carries the transcript."""
    return s.run(tcl.route_subset(sorted(nets), drc, iters))  # config.command_timeout: a hang fails the attempt


def unwired_digital(s) -> list:
    """Digital nets without wiring: [name, instance pins, ports] each."""
    return tcl_parse(s.value(tcl.DIGITAL_NETS + """
set out {}
foreach name $::flow_digital {
  set n [::flow_net $name]
  if {[$n getWire] eq "NULL"} { lappend out [list $name [llength [$n getITerms]] [llength [$n getBTerms]]] }
}
set out"""))


def digital_net_count(s) -> int:
    return int(s.value(tcl.DIGITAL_NETS + "llength $::flow_digital"))


def drv_driver_list(s, pins) -> list[str]:
    """For violating pins: [the drivers' [instance master] list, every library cell name] (Tcl words)."""
    return tcl_words(s.value(f"""set __o {{}}; set __seen {{}}
foreach __p {qlist(sorted(set(pins)))} {{
  set __pin [get_pins -quiet $__p]
  if {{![llength $__pin]}} continue
  foreach __d [get_pins -quiet -of_objects [get_nets -of_objects $__pin] -filter "direction==output"] {{
    set __i [::flow_find_inst [get_full_name [get_cells -of_objects $__d]]]
    if {{$__i eq "NULL" || ![string match CORE* [[$__i getMaster] getType]]}} continue
    if {{[lsearch -exact $__seen [$__i getName]] >= 0}} continue
    lappend __seen [$__i getName]
    lappend __o [list [$__i getName] [[$__i getMaster] getName]]
  }}
}}
list $__o [lmap __c [get_lib_cells */*] {{ get_name $__c }}]"""))


def report_slew_cap_violators(s, report: str):
    s.run(f"report_check_types -max_slew -max_capacitance -violators > {q(report)}")


def replace_with_lib_cell(s, inst: str, cell: str):
    s.run(f"replace_cell {q(inst)} [lindex [get_lib_cells */{cell}] 0]")


def repair_timing_routed(s, pdk):
    """Setup repair by sizing only (post-route setup buffering crashes this build), then hold repair."""
    s.run("repair_timing -setup -repair_tns 100 -skip_pin_swap -skip_gate_cloning -skip_buffer_removal "
          "-skip_buffering" + tcl.margins(pdk))
    s.run("repair_timing -hold -skip_pin_swap -skip_gate_cloning -skip_buffer_removal"
          + tcl.margins(pdk, "hold_route"))
