"""Fences, clusters and structured datapaths: enforcement, compaction and the re-abut / structure check
every sizing step runs."""
from __future__ import annotations

from .. import datapath
from ..tcl import q, qlist


def _snap_box(session, box_um):
    """Snap a box (microns) to rows vertically and to the site grid horizontally."""
    rows = datapath.query_rows(session)
    dbu = datapath.dbu_per_micron(session)
    x0, y0, x1, y1 = (int(v * dbu) for v in box_um)
    r = rows[0]
    sx0 = r.x + ((x0 - r.x + r.site_w - 1) // r.site_w) * r.site_w
    sx1 = r.x + ((x1 - r.x) // r.site_w) * r.site_w
    ys = [row.y for row in rows]
    top = [row.y + row.height for row in rows]
    sy0 = min((y for y in ys if y >= y0), default=ys[-1])
    sy1 = max((t for t in top if t <= y1), default=top[0])
    if sx1 <= sx0 or sy1 <= sy0:
        raise ValueError(f"box {box_um} has no whole rows/sites")
    return sx0, sy0, sx1, sy1

def fence_violations(session, names) -> list[str]:
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
    out = session.value(tcl_)
    return [line for line in out.splitlines() if line.strip()]

def enforce_fences(session, names, rounds: int = 4) -> dict:
    """Keep fence regions (dbRegion EXCLUSIVE) exclusive.

    OpenROAD keeps members inside a fence but does not keep later-inserted
    cells (repair buffers, clock buffers) out. A non-member found inside a
    fence joins it when it drives a member (it serves the fence); otherwise
    it is moved just outside and the design is re-legalized."""
    stats = {"adopted": 0, "evicted": 0}
    if not names:
        return stats
    for rnd in range(rounds):
        res = session.value(f"""
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
        stats["adopted"] += int(res[0])
        stats["evicted"] += int(res[1])
        if res == ["0", "0"]:
            break
        session.run("detailed_placement")
    return stats

def compact_cluster(session, g, avoid=()):
    """Pull a cluster's cells into a packed block at their centroid."""
    flat = session.value(f"""
set out {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[string first {q(g.prefix)} [$inst getName]] == 0}} {{
    set l [$inst getLocation]; set m [$inst getMaster]
    lappend out [$inst getName] [lindex $l 0] [lindex $l 1] [$m getWidth] [$m getHeight]
  }}
}}
set out""").split()
    cells, sizes, cx, cy = [], {}, 0, 0
    for i in range(0, len(flat), 5):
        n, x, y, w, h = flat[i], *map(int, flat[i + 1:i + 5])
        cells.append(n)
        sizes[n] = (w, h)
        cx += x + w / 2
        cy += y + h / 2
    if not cells:
        raise ValueError(f"cluster {g.name}: no cells")
    rows = datapath.query_rows(session)
    center = [cx / len(cells), cy / len(cells)]
    pl = datapath.pack_block(cells, center, rows, sizes, g.utilization, avoid=list(avoid))
    datapath.apply(session, pl, status="PLACED")
    xs = [p.x for p in pl] + [p.x + p.w for p in pl]
    ys = [p.y for p in pl] + [p.y + p.h for p in pl]
    return {"cells": len(cells), "bbox_um": [v / datapath.dbu_per_micron(session) for v in
                                             (min(xs), min(ys), max(xs), max(ys))]}

def cluster_spread(session, g) -> float:
    """Bounding-box half perimeter (microns) of a cluster's cells."""
    v = session.value(f"""
set xs {{}}; set ys {{}}
foreach inst [[ord::get_db_block] getInsts] {{
  if {{[string first {q(g.prefix)} [$inst getName]] == 0}} {{ set b [$inst getBBox]; lappend xs [$b xMin] [$b xMax]; lappend ys [$b yMin] [$b yMax] }}
}}
set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
expr {{double([lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]) / [[ord::get_db_block] getDbUnitsPerMicron]}}""")
    return float(v)

def _dp_tap(ctx):
    """(well-tap master, tap distance um) for datapath tap slots, or None."""
    p = ctx.pdk
    return (p.tap_cell, p.tap_distance) if p.tap_cell and p.tap_distance else None

def reabut_all(ctx) -> dict:
    out = {}
    for dp in ctx.design.datapaths:
        out[dp.name] = datapath.reabut(ctx.session, dp, _dp_tap(ctx))
    return out

def datapath_check(ctx, when: str = "") -> int:
    """Every datapath structure still sits exactly where its packing with the
    current masters puts it (rows abutted, stage columns aligned). Called after
    each resize + legalize; a broken structure fails the attempt."""
    bad = {}
    for dp in ctx.design.datapaths:
        problems = datapath.check_structure(ctx.session, dp, _dp_tap(ctx))
        if problems:
            bad[dp.name] = problems[:10]
    if bad:
        raise RuntimeError(f"datapath structure broken{' ' + when if when else ''}: {bad}")
    ctx.metrics["datapaths_intact"] = len(ctx.design.datapaths)
    return len(ctx.design.datapaths)
