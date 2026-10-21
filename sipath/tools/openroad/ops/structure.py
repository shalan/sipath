"""Fences, clusters: region checks, keeping fences exclusive, cluster cells and spread."""
from __future__ import annotations

from ..tcl import q, qlist


def fence_violations(s, names) -> list[str]:
    """Members outside their fence and non-members inside one, one line each."""
    tcl_ = f"""
set bad {{}}
set block [ord::get_db_block]
foreach rn {qlist(names)} {{
  set r [$block findRegion $rn]
  set bs [$r getBoundaries]
  foreach inst [$r getRegionInsts] {{
    set ib [$inst getBBox]; set inside 0
    foreach b $bs {{ if {{[$ib xMin] >= [$b xMin] && [$ib xMax] <= [$b xMax] && [$ib yMin] >= [$b yMin] && [$ib yMax] <= [$b yMax]}} {{ set inside 1 }} }}
    if {{!$inside}} {{ lappend bad "[$inst getName] outside $rn" }}
  }}
  foreach inst [$block getInsts] {{
    if {{[$inst getRegion] ne "NULL"}} continue
    if {{[[$inst getMaster] getType] ne "CORE"}} continue
    set ib [$inst getBBox]
    foreach b $bs {{ if {{[$ib xMin] < [$b xMax] && [$ib xMax] > [$b xMin] && [$ib yMin] < [$b yMax] && [$ib yMax] > [$b yMin]}} {{ lappend bad "[$inst getName] intrudes $rn" }} }}
  }}
}}
join $bad \\n
"""
    out = s.value(tcl_)
    return [line for line in out.splitlines() if line.strip()]


def enforce_fences_round(s, rnd: int, names) -> list[str]:
    """One round of keeping fences exclusive: [adopted, moved]."""
    return s.value(f"""
set round {rnd}
set adopted 0; set moved 0
set block [ord::get_db_block]
set core [$block getCoreArea]
foreach rn {qlist(names)} {{
  set r [$block findRegion $rn]
  set grp [lindex [$r getGroups] 0]
  foreach b [$r getBoundaries] {{
    foreach inst [$block getInsts] {{
      if {{[$inst getRegion] ne "NULL" || [[$inst getMaster] getType] ne "CORE" || [$inst isFixed]}} continue
      set ib [$inst getBBox]
      if {{!([$ib xMin] < [$b xMax] && [$ib xMax] > [$b xMin] && [$ib yMin] < [$b yMax] && [$ib yMax] > [$b yMin])}} continue
      set serves 0
      foreach it [$inst getITerms] {{
        if {{![$it isOutputSignal]}} continue
        set net [$it getNet]; if {{$net eq "NULL"}} continue
        foreach ld [$net getITerms] {{ if {{[[$ld getInst] getRegion] eq $r}} {{ set serves 1 }} }}
      }}
      if {{$serves}} {{
        $r addInst $inst; if {{$grp ne ""}} {{ $grp addInst $inst }}; incr adopted; continue
      }}
      set w [$ib getDX]; set h [$ib getDY]
      set dl [expr {{[$ib xMax] - [$b xMin]}}]; set dr [expr {{[$b xMax] - [$ib xMin]}}]
      set left [expr {{[$b xMin] - $w >= [$core xMin]}}]; set right [expr {{[$b xMax] + $w <= [$core xMax]}}]
      set nx [$ib xMin]; set ny [$ib yMin]
      if {{$round == 0}} {{
        if {{($dl <= $dr && $left) || !$right}} {{ set nx [expr {{[$b xMin] - $w}}] }} else {{ set nx [$b xMax] }}
      }} elseif {{$round == 1}} {{
        if {{($dl > $dr && $left) || !$right}} {{ set nx [expr {{[$b xMin] - $w}}] }} else {{ set nx [$b xMax] }}
      }} elseif {{[$b yMin] - $h >= [$core yMin]}} {{
        set ny [expr {{[$b yMin] - $h}}]
      }} else {{ set ny [$b yMax] }}
      $inst setLocation $nx $ny
      incr moved
    }}
  }}
}}
list $adopted $moved""").split()


def cluster_cells(s, prefix: str) -> list[str]:
    """[name, x, y, width, height, ...] of the instances whose name starts with ``prefix``."""
    return s.value(f"""
set out {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[string first {q(prefix)} [$inst getName]] == 0}} {{
    set l [$inst getLocation]; set m [$inst getMaster]
    lappend out [$inst getName] [lindex $l 0] [lindex $l 1] [$m getWidth] [$m getHeight]
  }}
}}
set out""").split()


def cluster_spread(s, prefix: str) -> float:
    """Bounding-box half perimeter (microns) of the instances whose name starts with ``prefix``."""
    return float(s.value(f"""
set xs {{}}; set ys {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[string first {q(prefix)} [$inst getName]] == 0}} {{ set b [$inst getBBox]; lappend xs [$b xMin] [$b xMax]; lappend ys [$b yMin] [$b yMax] }}
}}
set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
expr {{double([lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]) / [[ord::get_db_block] getDbUnitsPerMicron]}}"""))
