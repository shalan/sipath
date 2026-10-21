"""LEF reading without a tool: the macros of a LEF, their pins, and pins that
touch in an abstract (one net). Used by synthesis, black boxes and signoff."""
from __future__ import annotations

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
