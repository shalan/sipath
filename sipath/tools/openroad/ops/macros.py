"""OpenROAD operations of hard macro placement (sipath.macroplace): the helpers and questions of the random
search, rtl_macro_placer (mpl), the CD-LNS export and quick evaluation, and placing a macro."""
from __future__ import annotations

from ..tcl import q, qlist, tcl_parse, tcl_words

OBJ_TCL = r"""
proc ::flow_nets_hpwl {names} {
  # HPWL of the named nets over placed terminals only (DBU -> um)
  set block [ord::get_db_block]; set hp 0
  foreach n $names {
    set net [$block findNet $n]; if {$net eq "NULL"} continue
    set xs {}; set ys {}
    foreach t [$net getITerms] {
      if {![[$t getInst] isPlaced]} continue
      set a [$t getAvgXY]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } else {
        set b [[$t getInst] getBBox]; lappend xs [expr {([$b xMin]+[$b xMax])/2}]; lappend ys [expr {([$b yMin]+[$b yMax])/2}] } }
    foreach t [$net getBTerms] { set a [$t getFirstPinLocation]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } }
    if {[llength $xs] > 1} {
      set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
      set hp [expr {$hp + [lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]}]
    }
  }
  expr {double($hp) / [$block getDbUnitsPerMicron]}
}
proc ::flow_macro_try {inst orient x y analog} {
  set i [::flow_inst $inst]
  $i setOrient $orient; $i setLocation $x $y; $i setPlacementStatus PLACED
  list [::flow_macro_hpwl [list $inst]] [::flow_nets_hpwl $analog]
}
proc ::flow_macro_nets {inst} {
  set out {}
  foreach it [[::flow_inst $inst] getITerms] {
    set n [$it getNet]; if {$n ne "NULL"} { lappend out [$n getName] }
  }
  return $out
}
proc ::flow_cut_rows_box {x0 y0 x1 y1 min_width} {
  # split every row that overlaps the box into the parts left and right of it
  # (cut_rows only cuts around instances); parts narrower than min_width (DBU) are dropped
  set block [ord::get_db_block]; set n 0
  foreach r [$block getRows] {
    set rb [$r getBBox]
    if {!([$rb xMin] < $x1 && [$rb xMax] > $x0 && [$rb yMin] < $y1 && [$rb yMax] > $y0)} continue
    set site [$r getSite]; set sw [$r getSpacing]; set o [$r getOrigin]
    set ox [lindex $o 0]; set oy [lindex $o 1]; set ns [$r getSiteCount]
    set name [$r getName]; set orient [$r getOrient]; set dir [$r getDirection]
    set left [expr {($x0 - $ox) / $sw}]
    if {$left > $ns} { set left $ns }
    set rstart [expr {($x1 - $ox + $sw - 1) / $sw}]
    odb::dbRow_destroy $r
    set min_sites [expr {max(1, ($min_width + $sw - 1) / $sw)}]
    if {$left >= $min_sites} { odb::dbRow_create $block ${name}_gl $site $ox $oy $orient $dir $left $sw }
    if {$ns - $rstart >= $min_sites} {
      odb::dbRow_create $block ${name}_gr $site [expr {$ox + $rstart * $sw}] $oy $orient $dir [expr {$ns - $rstart}] $sw
    }
    incr n
  }
  return $n
}
proc ::flow_quick_eval {density report} {
  # quick standard-cell placement + congestion-tolerant global route; cells are
  # unplaced and guides cleared afterwards whatever happens (errors re-raised)
  set block [ord::get_db_block]
  set free {}
  foreach inst [$block getInsts] { if {![$inst isPlaced]} { lappend free $inst } }
  set rc [catch {
    global_placement -density $density -skip_io
    detailed_placement
    set hp [dict get [::flow_metrics] hpwl_um]
    global_route -allow_congestion -congestion_iterations 10 -congestion_report_file $report
  } err opts]
  foreach inst $free { $inst setPlacementStatus NONE }
  foreach net [$block getNets] { $net clearGuides }
  if {$rc == 1} { return -options $opts $err }
  list [llength $free] $hp
}
"""

CDLNS_EXPORT_TCL = r"""
proc ::flow_cdlns_export {path} {
  set f [open $path w]
  set blk [ord::get_db_block]
  foreach i [$blk getInsts] {
    set m [$i getMaster]
    set bb [$i getBBox]
    if {[$m isBlock]} {
      puts $f "M [$i getName] [$bb xMin] [$bb yMin] [$bb xMax] [$bb yMax]"
    } elseif {[$i isPlaced]} {
      puts $f "C [$i getName] [expr {([$bb xMin] + [$bb xMax]) / 2}] [expr {([$bb yMin] + [$bb yMax]) / 2}] \
               [expr {double([$bb getDX]) * [$bb getDY]}]"
    }
  }
  foreach b [$blk getBTerms] {
    set bb [$b getBBox]
    if {[$bb dx] || [$bb dy] || [$bb xMin]} {
      puts $f "P [$b getName] [expr {([$bb xMin] + [$bb xMax]) / 2}] [expr {([$bb yMin] + [$bb yMax]) / 2}]"
    }
  }
  foreach n [$blk getNets] {
    if {[$n isSpecial] || [$n getSigType] in {POWER GROUND}} continue
    set its [$n getITerms]
    set bts [$n getBTerms]
    if {[llength $its] + [llength $bts] < 2} continue
    puts $f "N"
    foreach it $its {
      set i [$it getInst]
      if {[[$i getMaster] isBlock]} {
        set bb [$it getBBox]
        puts $f "I [$i getName] [expr {([$bb xMin] + [$bb xMax]) / 2}] [expr {([$bb yMin] + [$bb yMax]) / 2}] [[$it getMTerm] getName]"
      } else {
        puts $f "I [$i getName]"
      }
    }
    foreach bt $bts { puts $f "B [$bt getName]" }
  }
  close $f
}
"""


def load_helpers(s, macro_hpwl_tcl: str):
    """The macro-net HPWL helpers (chip.MACRO_HPWL) and the placement objective (OBJ_TCL)."""
    s.run(macro_hpwl_tcl)
    s.run(OBJ_TCL)


def macro_nets(s, inst: str) -> list[str]:
    return tcl_words(s.value(f"::flow_macro_nets {q(inst)}"))


def master_name(s, inst: str) -> str:
    return s.value(f"[[::flow_inst {q(inst)}] getMaster] getName")


def inst_box(s, inst: str) -> tuple:
    return tuple(map(int, tcl_words(s.value(
        f"set b [[::flow_inst {q(inst)}] getBBox]; list [$b xMin] [$b yMin] [$b xMax] [$b yMax]"))))


def first_row(s) -> tuple:
    """(origin x, origin y, site width, row height) of the first row, in DBU."""
    return tuple(map(int, tcl_words(s.value(
        "set r [lindex [[ord::get_db_block] getRows] 0]; set o [$r getOrigin]; "
        "list [lindex $o 0] [lindex $o 1] [$r getSpacing] [[$r getBBox] dy]"))))


def place_frozen(s, inst: str, location, orient: str):
    s.run(f"place_macro -macro_name [[::flow_inst {q(inst)}] getName] -location {{{location[0]} {location[1]}}} "
          f"-orientation {orient} -exact\n[::flow_inst {q(inst)}] setPlacementStatus LOCKED")


def try_position(s, inst: str, orient: str, x: int, y: int, analog_nets) -> tuple[float, float]:
    """(macro-net HPWL, analog-net HPWL) with the macro there (OBJ_TCL's ::flow_macro_try)."""
    hp, ahp = map(float, tcl_words(s.value(f"::flow_macro_try {q(inst)} {orient} {x} {y} {qlist(analog_nets)}")))
    return hp, ahp


def set_position(s, inst: str, orient: str, x: int, y: int):
    s.run(f"set i [::flow_inst {q(inst)}]; $i setOrient {orient}; $i setLocation {x} {y}; "
          "$i setPlacementStatus FIRM")


def guard_blockage(s, box, min_row_dbu: int):
    """A placement blockage over ``box`` and no row pieces under it (too short for a well tap)."""
    s.run(f"odb::dbBlockage_create [ord::get_db_block] {box[0]} {box[1]} {box[2]} {box[3]}")
    s.run(f"::flow_cut_rows_box {box[0]} {box[1]} {box[2]} {box[3]} {int(min_row_dbu)}")


def cut_rows(s, halo: float, min_row: float):
    s.run(f"cut_rows -halo_width_x {halo} -halo_width_y {halo} -row_min_width {min_row:g}")


def macro_hpwl(s, insts) -> float:
    names = " ".join(q(i) for i in insts)
    return float(s.value(f"::flow_macro_hpwl {{{names}}}"))


def macro_hpwl_skipped(s) -> int:
    return int(s.value("set ::flow_macro_hpwl_skipped"))


def nets_hpwl(s, nets) -> float:
    return float(s.value(f"::flow_nets_hpwl {qlist(nets)}"))


def mpl_keepouts(s, keepouts):
    """Blockages for rtl_macro_placer over the keepouts (removed by mpl_end)."""
    s.run("set ::flow_mpl_blk {}")
    for k in keepouts:
        s.run(f"lappend ::flow_mpl_blk [odb::dbBlockage_create [ord::get_db_block] {k[0]} {k[1]} {k[2]} {k[3]}]")


def macro_halo(s, inst: str, h: float):
    s.run(f"set_macro_halo -macro_name [[::flow_inst {q(inst)}] getName] -halo {{{h:.3f} {h:.3f}}}")


def mpl_mark_free(s):
    """The standard cells not placed yet (rtl_macro_placer places them; mpl_end unplaces them again)."""
    s.run("set ::flow_mpl_free {}\nforeach i [[ord::get_db_block] getInsts] { "
          "if {![$i isPlaced] && ![[$i getMaster] isBlock]} { lappend ::flow_mpl_free $i } }")


def rtl_macro_placer(s, report_dir: str, util):
    s.run(f"rtl_macro_placer -report_directory {q(report_dir)} -target_util {util}", timeout=1800)


def mpl_end(s):
    s.run("foreach b $::flow_mpl_blk { odb::dbBlockage_destroy $b }\nunset ::flow_mpl_blk\n"
          "foreach i $::flow_mpl_free { $i setPlacementStatus NONE }\nunset ::flow_mpl_free")


def orient_and_status(s, inst: str) -> list[str]:
    return tcl_words(s.value(f"set i [::flow_inst {q(inst)}]; list [$i getOrient] [$i getPlacementStatus]"))


def set_firm(s, inst: str):
    s.run(f"[::flow_inst {q(inst)}] setPlacementStatus FIRM")


def set_orient(s, inst: str, orient: str):
    s.run(f"[::flow_inst {q(inst)}] setOrient {orient}")


def cdlns_mark_free(s):
    s.run("set ::flow_cdlns_free {}\nforeach i [[ord::get_db_block] getInsts] { "
          "if {![$i isPlaced] && ![[$i getMaster] isBlock]} { lappend ::flow_cdlns_free $i } }")


def quick_global_place(s, density):
    s.run(f"global_placement -skip_io -density {density}", timeout=1800)


def cdlns_export(s, dump: str):
    """The floorplan as the CD-LNS placer's input (CDLNS_EXPORT_TCL, ::flow_cdlns_export)."""
    s.run(CDLNS_EXPORT_TCL)
    s.run(f"::flow_cdlns_export {q(dump)}")


def cdlns_end(s):
    s.run("foreach i $::flow_cdlns_free { $i setPlacementStatus NONE }\nunset ::flow_cdlns_free")


def quick_eval(s, density, report: str) -> list:
    """(cells, HPWL) of a quick placement with a congestion report (OBJ_TCL's ::flow_quick_eval)."""
    return tcl_parse(s.value(f"::flow_quick_eval {density} {q(report)}", timeout=1800))


def master_width_um(s, master: str) -> float:
    return float(s.value(f"expr {{double([[[ord::get_db] findMaster {master}] getWidth]) / "
                         f"[[ord::get_db_block] getDbUnitsPerMicron]}}"))
