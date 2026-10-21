"""Reports and summaries: report files, power, the PPA summary query."""
from __future__ import annotations

from ..tcl import q, tcl_parse

PPA_TCL = r"""
set __o {}
set __b [ord::get_db_block]; set __dbu [$__b getDbUnitsPerMicron]
set __std 0.0; set __mac 0.0; set __n 0
foreach __i [$__b getInsts] {
  set __m [$__i getMaster]
  set __a [expr {double([$__m getWidth]) * [$__m getHeight] / $__dbu / $__dbu}]
  if {[$__m isBlock]} { set __mac [expr {$__mac + $__a}]; continue }
  if {[$__m getType] ne "CORE" || [regexp {fill|tap|decap} [$__m getName]]} continue
  set __std [expr {$__std + $__a}]; incr __n
}
set __c [$__b getCoreArea]
set __core [expr {double([$__c dx]) * [$__c dy] / $__dbu / $__dbu}]
lappend __o std_area $__std macro_area $__mac cells $__n core_area $__core
lappend __o wns [sta::worst_slack -max] tns [sta::total_negative_slack -max] whs [sta::worst_slack -min]
set __cl {}
foreach __k [all_clocks] { lappend __cl [list [get_name $__k] [get_property $__k period]] }
lappend __o clocks $__cl
set __o
"""


def to_file(s, path: str, cmd: str):
    """Run an STA report command with its output redirected to ``path``; the result (rc) says if it failed."""
    return s.run(f"sta::redirect_file_begin {q(path)}\n"
                 f"set ::__rc [catch {{{cmd}}} ::__msg]\n"
                 "sta::redirect_file_end\n"
                 "if {$::__rc} { error $::__msg }", check=False)


def power(s) -> str:
    """``report_power`` output."""
    return s.run("report_power").output


def ppa_values(s):
    """Areas, cells, core, slacks and clocks (PPA_TCL), parsed."""
    return tcl_parse(s.value(PPA_TCL))
