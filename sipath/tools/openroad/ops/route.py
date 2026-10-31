"""Routing: layers, pin access, global and detailed routing, repair on global routes,
antennas, extraction, fillers."""
from __future__ import annotations

from .. import tcl
from ..tcl import q, qlist, tcl_words


def set_routing_layers(s, design, pdk, margin: float = 0.0):
    """The design's routing layer range and global-route capacity adjustments (``margin``: extra reduction)."""
    s.run(tcl.routing_layers(design, pdk, margin=margin) if margin else tcl.routing_layers(design, pdk))


def set_signal_layers(s, low: str, high: str):
    s.run(f"set_routing_layers -signal {low}-{high} -clock {low}-{high}")


def pin_access(s):
    s.run("pin_access")


def run_global_route(s, script: str):
    """A global-route script (a Tcl error, e.g. GRT-0116 congestion, propagates)."""
    s.run(script)


def global_route_allow_congestion(s, report: str):
    """Global route that leaves congestion instead of failing, then clears the guides (only the placement is kept)."""
    s.run(f"global_route -allow_congestion -congestion_iterations 30 -congestion_report_file {q(report)}\n"
          "foreach net [[ord::get_db_block] getNets] { $net clearGuides }")  # config.command_timeout


def global_route_repair_script(pdk):
    """The commands shared by execution and the pre-repair replay bundle."""
    return ("global_route -start_incremental\nestimate_parasitics -global_routing\n"
            f"repair_design{tcl.design_margins(pdk)}\n"
            "repair_timing -setup -hold -repair_tns 100" + tcl.margins(pdk, "hold_route"))


def repair_on_global_routes(s, pdk):
    """Repair with global-route parasitics in incremental mode (the router later re-routes only what changed)."""
    s.run(global_route_repair_script(pdk))


def global_route_digital(report: str) -> str:
    """The script that routes the digital nets globally (run through run_global_route)."""
    return tcl.global_route_digital(report)


def end_incremental_global_route(report: str) -> str:
    """The script that ends the incremental global route (run through run_global_route)."""
    return f"global_route -end_incremental -congestion_report_file {q(report)}"


def global_route_parasitics(s):
    """Estimate parasitics from the global routes and mark them as what timing uses."""
    s.run("estimate_parasitics -global_routing\nset ::flow_grt_parasitics 1")


def repair_antennas(s, diode: str, iterations: int):
    return s.run(f"repair_antennas {q(diode)} -iterations {iterations}")


def detailed_route(s, drc_report: str, iters: int):
    return s.run(tcl.detailed_route_only(drc_report, iters))


def detailed_route_all_digital(s, drc_report: str, iters: int):
    """Route every digital net (a patch route may have narrowed the router's net list)."""
    return s.run(tcl.DIGITAL_NETS + "::flow_route_nets $::flow_digital\n"
                 + tcl.detailed_route_only(drc_report, iters))  # config.command_timeout


def route_nets(s, nets, drc_report: str, iters: int, congestion_report: str | None = None):
    return s.run(tcl.route_subset(nets, drc_report, iters, congestion_report))


def check_antennas(s, report: str):
    return s.run(f"check_antennas -report_file {q(report)}")


def write_def(s, path: str):
    s.run(f"write_def {q(path)}")


def rcx(s, pdk, spef: str):
    """OpenRCX for every corner's rules (tcl.rcx)."""
    s.run(tcl.rcx(pdk, spef))


def insert_fillers(s, pdk, fillers):
    s.run(tcl.filler(pdk, fillers))


def master_counts(s) -> dict[str, int]:
    """{master: instances of it} in the block."""
    w = tcl_words(s.value("set __c [dict create]\nforeach __i [[ord::get_db_block] getInsts] "
                          "{ dict incr __c [[$__i getMaster] getName] }\nset __c"))
    return {w[i]: int(w[i + 1]) for i in range(0, len(w), 2)}


def instances_of(s, masters) -> list[tuple[str, str]]:
    """(instance, master) of the block's instances of ``masters``, by row (y) then x."""
    w = tcl_words(s.value(f"""set __o {{}}
foreach __i [[ord::get_db_block] getInsts] {{
  set __m [[$__i getMaster] getName]
  if {{$__m ni {qlist(masters)}}} continue
  lassign [$__i getLocation] __x __y
  lappend __o [list $__y $__x [$__i getName] $__m]
}}
join [lmap __r [lsort -integer -index 0 [lsort -integer -index 1 $__o]] {{lrange $__r 2 3}}]"""))
    return [(w[i], w[i + 1]) for i in range(0, len(w), 2)]


def replace_with_fill(s, names, cells) -> int:
    """Each instance of ``names`` replaced by ``cells`` (fill cells) tiling its sites: same row, orientation
    and placement status, widest first. In the database directly: filler_placement keeps the site map of its
    first call in a process and fills no gap made after it. Returns the cells created."""
    return int(s.value(f"""set __blk [ord::get_db_block]
set __ms {{}}
foreach __c {qlist(cells)} {{
  set __m [[ord::get_db] findMaster $__c]
  if {{$__m ne "NULL"}} {{ lappend __ms [list [$__m getWidth] $__m] }}
}}
set __ms [lsort -integer -decreasing -index 0 $__ms]
if {{![info exists ::flow_fill_swap]}} {{ set ::flow_fill_swap 0 }}
set __made 0
foreach __n {qlist(names)} {{
  set __i [$__blk findInst $__n]
  set __b [$__i getBBox]
  set __x [$__b xMin]; set __y [$__b yMin]; set __end [$__b xMax]
  set __o [$__i getOrient]; set __st [$__i getPlacementStatus]
  odb::dbInst_destroy $__i
  while {{$__x < $__end}} {{
    set __fit {{}}
    foreach __mw $__ms {{ if {{$__x + [lindex $__mw 0] <= $__end}} {{ set __fit $__mw; break }} }}
    if {{$__fit eq {{}}}} {{ error "no fill cell fits the [expr {{$__end - $__x}}] DBU left of $__n" }}
    while {{[$__blk findInst "FILLER_SWAP_[incr ::flow_fill_swap]"] ne "NULL"}} {{}}
    set __f [odb::dbInst_create $__blk [lindex $__fit 1] "FILLER_SWAP_$::flow_fill_swap"]
    $__f setOrient $__o
    $__f setLocation $__x $__y
    $__f setPlacementStatus $__st
    set __x [expr {{$__x + [lindex $__fit 0]}}]
    incr __made
  }}
}}
set __made"""))
