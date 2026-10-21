"""Heatmaps as data: OpenROAD's ``gui::dump_heatmap`` CSVs read, summarised and drawn,
without a tool (``tools/openroad/heatmap.py`` dumps them).

A map is a list of bins ``(x0, y0, x1, y1, value)`` in microns, with the unit its
CSV header gives (``%`` for placement density, RUDY and routing congestion,
``pins``, ``W``, ``mV``...). ``summary`` gives the peak, the 95th percentile, the
share of hot bins and the hot regions (4-connected hot bins, merged into boxes);
``query`` the bins inside a box; ``svg`` a picture for the run report.
"""
from __future__ import annotations

import csv
import math

MAPS = ("placement", "rudy", "routing", "pin", "power", "irdrop")
OPENROAD_NAMES = {"placement": "Placement", "rudy": "RUDY", "routing": "Routing", "pin": "Pin",
                  "power": "Power", "irdrop": "IRDrop"}
# a bin is hot at or above an absolute value for the % maps (density near full, demand above capacity),
# else at or above HOT_RELATIVE of the map's peak
HOT_ABSOLUTE = {"placement": 90.0, "rudy": 100.0, "routing": 100.0}
HOT_RELATIVE = 0.8


def read(path: str) -> tuple[str, list[tuple[float, float, float, float, float]]]:
    """(unit, bins) of a ``gui::dump_heatmap`` CSV: header ``x0,y0,x1,y1,value (<unit>)``."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows or len(rows[0]) < 5:
        raise ValueError(f"{path}: not a heatmap CSV")
    head = rows[0][4]
    unit = head[head.find("(") + 1:head.rfind(")")] if "(" in head else ""
    return unit, [tuple(float(x) for x in r[:5]) for r in rows[1:] if len(r) >= 5]


def hot_threshold(name: str, bins) -> float | None:
    if name in HOT_ABSOLUTE:
        return HOT_ABSOLUTE[name]
    peak = max((b[4] for b in bins), default=0.0)
    return HOT_RELATIVE * peak if peak > 0 else None


def _percentile(values: list[float], q: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def regions(bins, threshold: float) -> list[dict]:
    """Hot bins (value >= threshold) merged into 4-connected regions: box, peak, mean, bins; by peak, then size."""
    xs = sorted({b[0] for b in bins})
    ys = sorted({b[1] for b in bins})
    ix, iy = {x: i for i, x in enumerate(xs)}, {y: i for i, y in enumerate(ys)}
    hot = {(ix[b[0]], iy[b[1]]): b for b in bins if b[4] >= threshold}
    seen, out = set(), []
    for start in hot:
        if start in seen:
            continue
        stack, cells = [start], []
        seen.add(start)
        while stack:
            c = stack.pop()
            cells.append(hot[c])
            for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + d[0], c[1] + d[1])
                if n in hot and n not in seen:
                    seen.add(n)
                    stack.append(n)
        vals = [b[4] for b in cells]
        out.append({"box": [min(b[0] for b in cells), min(b[1] for b in cells),
                            max(b[2] for b in cells), max(b[3] for b in cells)],
                    "peak": max(vals), "mean": sum(vals) / len(vals), "bins": len(cells)})
    out.sort(key=lambda r: (-r["peak"], -r["bins"]))
    return out


def summary(name: str, unit: str, bins, top: int = 3) -> dict:
    """peak, p95, hot (threshold and share of bins), the ``top`` hot regions."""
    vals = [b[4] for b in bins]
    th = hot_threshold(name, bins)
    hot = sum(1 for v in vals if th is not None and v >= th)
    return {"unit": unit, "bins": len(bins), "peak": max(vals, default=0.0), "p95": _percentile(vals, 0.95),
            "hot_threshold": th, "hot_share": hot / len(vals) if vals else 0.0,
            "hotspots": regions(bins, th)[:top] if th is not None else []}


def coarsen(bins, max_side: int = 100):
    """``bins`` regridded to at most ``max_side`` per side: each coarse bin covers k x k fine bins (k the
    smallest that fits) and takes the largest of their values, so hotspots stay visible. Unchanged when the
    map already fits."""
    xs = sorted({b[0] for b in bins})
    ys = sorted({b[1] for b in bins})
    k = max(1, -(-len(xs) // max_side), -(-len(ys) // max_side))
    if k == 1:
        return list(bins)
    ix, iy = {x: i // k for i, x in enumerate(xs)}, {y: i // k for i, y in enumerate(ys)}
    cells = {}
    for b in bins:
        key = (ix[b[0]], iy[b[1]])
        c = cells.get(key)
        cells[key] = list(b) if c is None else [min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3]),
                                               max(c[4], b[4])]
    return [tuple(c) for _, c in sorted(cells.items(), key=lambda kv: (kv[0][0], kv[0][1]))]


def write(path: str, unit: str, bins) -> None:
    """A map back to ``gui::dump_heatmap``'s CSV form."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["x0", "y0", "x1", "y1", f"value ({unit})"])
        w.writerows([f"{v:g}" for v in b[:4]] + [f"{b[4]:.6e}"] for b in bins)


def overlap(a, b) -> float:
    """Overlap area of two boxes (x0, y0, x1, y1); 0 when they only touch or are apart."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def inside(box, objects: dict, limit: int = 10) -> dict:
    """Objects ({name: box}) overlapping ``box``: their count and the ``limit`` largest overlaps."""
    hits = sorted(((overlap(box, b), n) for n, b in objects.items() if overlap(box, b) > 0), reverse=True)
    return {"count": len(hits), "top": [n for _, n in hits[:limit]]}


def query(bins, box=None) -> list[dict]:
    """The bins overlapping ``box`` (all without one), as dicts."""
    return [{"box": list(b[:4]), "value": b[4]} for b in bins if box is None or overlap(box, b[:4]) > 0]


def svg(name: str, unit: str, bins, width: int = 260) -> str:
    """The map as an inline SVG (cold blue to hot red, scaled to its own peak), north up."""
    if not bins:
        return ""
    x0, y0 = min(b[0] for b in bins), min(b[1] for b in bins)
    x1, y1 = max(b[2] for b in bins), max(b[3] for b in bins)
    s = width / max(x1 - x0, y1 - y0, 1e-9)
    peak = max(b[4] for b in bins) or 1.0
    rects = []
    for b in bins:
        t = max(0.0, min(1.0, b[4] / peak))
        r, g, bl = int(40 + 215 * t), int(60 + 120 * (1 - abs(2 * t - 1))), int(200 * (1 - t))
        rects.append(f'<rect x="{(b[0] - x0) * s:.1f}" y="{(y1 - b[3]) * s:.1f}" width="{(b[2] - b[0]) * s:.2f}" '
                     f'height="{(b[3] - b[1]) * s:.2f}" fill="rgb({r},{g},{bl})"/>')
    w, h = (x1 - x0) * s, (y1 - y0) * s
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
            f'viewBox="0 0 {w:.1f} {h:.1f}"><title>{name} (peak {peak:.3g} {unit})</title>'
            + "".join(rects) + "</svg>")
