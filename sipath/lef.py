"""LEF reading without a tool: the macros of a LEF, their pins, pins that
touch in an abstract (one net), and how much of a macro a layer covers. Used
by synthesis, black boxes, signoff and filler placement."""
from __future__ import annotations

import functools
import re


class LefError(ValueError):
    pass


def lef_macros(path: str) -> dict[str, list[str]]:
    """{macro name: pin names in LEF order} for every MACRO in a LEF."""
    with open(path, errors="replace") as f:
        text = f.read()
    out = {}
    for m in re.finditer(r"^\s*MACRO\s+(\S+)(.*?)^\s*END\s+\1\s*$", text, re.M | re.S):
        # unique, in order (a PDK LEF may declare a pin twice)
        out[m.group(1)] = list(dict.fromkeys(re.findall(r"^\s*PIN\s+(\S+)", m.group(2), re.M)))
    return out


def lef_pin_shorts(path: str, master: str) -> dict[str, str]:
    """Pins of ``master`` whose LEF rectangles overlap or touch on one layer
    (one net in the abstract): {pin: first pin of its group, in LEF order}
    for every pin but the first of each group."""
    with open(path, errors="replace") as f:
        text = f.read()
    m = re.search(rf"^\s*MACRO\s+{re.escape(master)}\s*$(.*?)^\s*END\s+{re.escape(master)}\s*$", text, re.M | re.S)
    if not m:
        return {}
    rects, order = [], []
    for pm in re.finditer(r"^\s*PIN\s+(\S+)(.*?)^\s*END\s+\1\s*$", m.group(1), re.M | re.S):
        name = pm.group(1)
        order.append(name)
        layer = None
        for line in pm.group(2).splitlines():
            t = line.replace(";", " ").split()
            if t[:1] == ["LAYER"]:
                layer = t[1]
            elif t[:1] == ["RECT"] and layer:
                x0, y0, x1, y1 = map(float, t[1:5])
                rects.append((layer, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), name))
    parent = {n: n for n in order}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    rects.sort(key=lambda r: (r[0], r[1]))
    active = []
    for r in rects:
        active = [a for a in active if a[0] == r[0] and a[3] >= r[1]]
        for a in active:
            if a[5] != r[5] and a[2] <= r[4] and r[2] <= a[4]:
                x, y = find(a[5]), find(r[5])
                if x != y:
                    if order.index(x) < order.index(y):
                        parent[y] = x
                    else:
                        parent[x] = y
        active.append(r)
    return {n: find(n) for n in order if find(n) != n}


def lef_macro(path: str) -> tuple[str, list[str]]:
    """(macro name, pin names in LEF order) of a single-macro LEF."""
    macros = lef_macros(path)
    if not macros:
        raise LefError(f"no MACRO in {path}")
    name = next(iter(macros))
    return name, macros[name]


def _union_area(rects) -> float:
    """Area of a union of axis-aligned rectangles (x0, y0, x1, y1), overlaps counted once."""
    xs = sorted({x for r in rects for x in (r[0], r[2])})
    total = 0.0
    for a, b in zip(xs, xs[1:]):
        lo = hi = None
        for y0, y1 in sorted((r[1], r[3]) for r in rects if r[0] <= a and r[2] >= b):
            if hi is None or y0 > hi:
                if hi is not None:
                    total += (b - a) * (hi - lo)
                lo, hi = y0, y1
            else:
                hi = max(hi, y1)
        if hi is not None:
            total += (b - a) * (hi - lo)
    return total


@functools.lru_cache(maxsize=8)
def layer_cover(paths: tuple[str, ...], layer: str) -> dict[str, tuple[float, float]]:
    """{macro: (SIZE area in um2, the share of it ``layer`` covers)} for every MACRO of the LEFs: pins and
    obstructions together, overlaps once, shapes moved by the macro's ORIGIN and clipped to its box. A macro
    that draws ``layer`` with a POLYGON is left out: its share would be a guess."""
    out = {}
    for path in paths:
        with open(path, errors="replace") as f:
            text = f.read()
        for m in re.finditer(r"^\s*MACRO\s+(\S+)(.*?)^\s*END\s+\1\s*$", text, re.M | re.S):
            body = m.group(2)
            size = re.search(r"^\s*SIZE\s+([-\d.]+)\s+BY\s+([-\d.]+)", body, re.M)
            if not size or float(size.group(1)) <= 0 or float(size.group(2)) <= 0:
                continue
            w, h = float(size.group(1)), float(size.group(2))
            origin = re.search(r"^\s*ORIGIN\s+([-\d.]+)\s+([-\d.]+)", body, re.M)
            ox, oy = (float(origin.group(1)), float(origin.group(2))) if origin else (0.0, 0.0)
            rects, current, polygon = [], None, False
            for line in body.splitlines():
                t = line.replace(";", " ").split()
                if not t:
                    continue
                if t[0] in ("PORT", "OBS"):
                    current = None
                elif t[0] == "LAYER":
                    current = t[1]
                elif t[0] == "POLYGON" and current == layer:
                    polygon = True
                elif t[0] == "RECT" and current == layer:
                    v = t[3:7] if t[1] == "MASK" else t[1:5]
                    x0, y0, x1, y1 = (float(a) for a in v)
                    x0, x1 = sorted((x0 + ox, x1 + ox))
                    y0, y1 = sorted((y0 + oy, y1 + oy))
                    x0, y0, x1, y1 = max(x0, 0.0), max(y0, 0.0), min(x1, w), min(y1, h)
                    if x1 > x0 and y1 > y0:
                        rects.append((x0, y0, x1, y1))
            if not polygon:
                out[m.group(1)] = (w * h, _union_area(rects) / (w * h))
    return out
