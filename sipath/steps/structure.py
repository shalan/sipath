"""Fences, clusters and structured datapaths: enforcement, compaction and the re-abut / structure check
every sizing step runs."""
from __future__ import annotations

from .. import datapath
from ..tools.openroad.ops import place as place_ops, structure as structure_ops


def _snap_box(session, box_um):
    """Snap a box (microns) to rows vertically and to the site grid horizontally."""
    rows = datapath.query_rows(session)
    dbu = datapath.dbu_per_micron(session)
    x0, y0, x1, y1 = (int(round(v * dbu)) for v in box_um)
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
    return structure_ops.fence_violations(session, names)

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
        res = structure_ops.enforce_fences_round(session, rnd, names)
        stats["adopted"] += int(res[0])
        stats["evicted"] += int(res[1])
        if res == ["0", "0"]:
            break
        place_ops.detailed_placement(session)
    return stats

def compact_cluster(session, g, avoid=()):
    """Pull a cluster's cells into a packed block at their centroid."""
    flat = structure_ops.cluster_cells(session, g.prefix)
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
    return structure_ops.cluster_spread(session, g.prefix)

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
