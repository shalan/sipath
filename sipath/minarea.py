"""Post-route minimum-area repair.

A safety net after detailed routing: an island of metal (wires + via landings
+ patches of one net on one layer) below the layer's minimum area fails DRC.
The router adds minimum-area patches itself; they used to be lost when
OpenRCX re-ordered the wires (see steps.run_rcx), which is how such islands
(a 0.23 um jog between two stacked vias, ~0.19 um2 against 0.24 um2) reached
the signoff DRC. With the routing restored after extraction this normally
finds nothing.

``repair`` rebuilds each routed net's metal per layer from the DEF (segments
with their end extensions, via landings from the tech LEF, existing patches),
merges touching shapes, and for every island under the layer's minimum area
adds a patch rectangle (``NEW <layer> ( x y ) RECT ( ... )``, as the router
writes its own patches) that lengthens the island along the layer's routing
direction, on the side(s) whose spacing to other nets' metal allows it. Nets
with a non-default rule and islands that cannot be patched are reported, not
touched. The signoff DRC checks the result.
"""
from __future__ import annotations

import math
import re

from . import wires
from .tools.openroad.tcl import q


# ------------------------------------------------------------------ tech LEF
def tech_rules(tech_lef: str) -> dict:
    """Routing-layer width/spacing/min-area/direction and fixed via
    geometry (per layer rects) from a tech LEF, in microns."""
    with open(tech_lef) as fh:
        text = fh.read()
    layers, vias = {}, {}
    for m in re.finditer(r"^LAYER\s+(\S+)\s*\n(.*?)^END\s+\1", text, re.S | re.M):
        name, body = m.group(1), m.group(2)
        body = re.sub(r"#.*", "", body)
        if not re.search(r"TYPE\s+ROUTING", body):
            continue
        d = {}
        for key, pat in (("width", r"^\s*WIDTH\s+([\d.]+)\s*;"), ("area", r"^\s*AREA\s+([\d.]+)\s*;"),
                         ("spacing", r"^\s*SPACING\s+([\d.]+)\s*;"), ("pitch", r"^\s*PITCH\s+([\d.]+)")):
            mm = re.search(pat, body, re.M)
            if mm:
                d[key] = float(mm.group(1))
        if "spacing" not in d:  # SPACINGTABLE PARALLELRUNLENGTH ... WIDTH 0 <s0> ...: the base spacing
            mm = re.search(r"SPACINGTABLE.*?WIDTH\s+0(?:\.0+)?\s+([\d.]+)", body, re.S)
            if mm:
                d["spacing"] = float(mm.group(1))
        mm = re.search(r"DIRECTION\s+(HORIZONTAL|VERTICAL)", body)
        d["dir"] = mm.group(1)[0] if mm else "H"
        layers[name] = d
    for m in re.finditer(r"^VIA\s+(\S+)[^\n]*\n(.*?)^END\s+\1", text, re.S | re.M):
        name, body = m.group(1), m.group(2)
        rects, cur = {}, None
        for line in re.sub(r"#.*", "", body).splitlines():
            t = line.replace(";", " ").split()
            if not t:
                continue
            if t[0] == "LAYER":
                cur = t[1]
            elif t[0] == "RECT" and cur in layers:
                x0, y0, x1, y1 = map(float, t[1:5])
                r = rects.setdefault(cur, [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])
                r[0], r[1], r[2], r[3] = min(r[0], x0, x1), min(r[1], y0, y1), max(r[2], x0, x1), max(r[3], y0, y1)
        if rects:
            vias[name] = {k: tuple(v) for k, v in rects.items()}
    return {"layers": layers, "vias": vias}


TECH_DB = r"""
set o {}
foreach l [[ord::get_db_tech] getLayers] {
  if {[$l getType] ne "ROUTING"} continue
  lappend o [list [$l getName] [$l getWidth] [$l getArea] [$l getSpacing] [$l getDirection] [$l getPitch]]
}
set v {}
foreach via [[ord::get_db_tech] getVias] {
  set bs {}
  foreach b [$via getBoxes] {
    set L [$b getTechLayer]
    if {$L ne "NULL" && [$L getType] eq "ROUTING"} { lappend bs [list [$L getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] }
  }
  lappend v [list [$via getName] $bs]
}
list $o $v
"""


def tech_rules_db(session, dbu: int) -> dict:
    """The same rules from the loaded technology (works when the tech LEF
    lives only where OpenROAD runs, e.g. inside the container)."""
    from .tools.openroad.tcl import tcl_parse
    lays, vias_ = tcl_parse(session.value(TECH_DB))
    layers = {}
    for name, w, area, sp, direction, pitch in lays:
        d = {"width": int(w) / dbu, "dir": "V" if direction.upper().startswith("V") else "H", "pitch": int(pitch) / dbu}
        if float(area) > 0:
            d["area"] = float(area) / (dbu * dbu)  # OpenDB keeps the area in DBU^2
        if int(sp) > 0:
            d["spacing"] = int(sp) / dbu
        layers[name] = d
    vias = {}
    for name, boxes in vias_:
        rects = {}
        for ly, x0, y0, x1, y1 in boxes:
            r = [int(x0) / dbu, int(y0) / dbu, int(x1) / dbu, int(y1) / dbu]
            cur = rects.setdefault(ly, r)
            rects[ly] = [min(cur[0], r[0]), min(cur[1], r[1]), max(cur[2], r[2]), max(cur[3], r[3])]
        if rects:
            vias[name] = {k: tuple(v) for k, v in rects.items()}
    return {"layers": layers, "vias": vias}


# --------------------------------------------------------------------- shapes
def _seg_rect(a, b, w, ext_a, ext_b):
    (x0, y0), (x1, y1) = a, b
    h = w // 2
    if y0 == y1:  # horizontal
        lo, hi = (x0, ext_a), (x1, ext_b)
        if x1 < x0:
            lo, hi = hi, lo
        return (lo[0] - lo[1], y0 - h, hi[0] + hi[1], y0 + h)
    lo, hi = (y0, ext_a), (y1, ext_b)
    if y1 < y0:
        lo, hi = hi, lo
    return (x0 - h, lo[0] - lo[1], x0 + h, hi[0] + hi[1])


def net_shapes(info: dict, rules: dict, dbu: int, via_layers: dict) -> dict[str, list[tuple]] | None:
    """Metal rects (DBU) per layer of one parsed net; None if a via is unknown."""
    lay, vias = rules["layers"], rules["vias"]
    out: dict[str, list] = {}
    for p in info["paths"]:
        layer = p["layer"]
        last, last_ext = None, None
        for it in p["items"]:
            kind = it[0]
            if kind == "pt":
                _, x, y, ext = it
                if layer not in lay:
                    last, last_ext = (x, y), ext
                    continue
                w = int(round(lay[layer]["width"] * dbu))
                e = ext if ext is not None else w // 2
                if last is not None and (x, y) != last:
                    out.setdefault(layer, []).append(_seg_rect(last, (x, y), w, last_ext if last_ext is not None
                                                               else w // 2, e))
                last, last_ext = (x, y), e
            elif kind == "via":
                name = it[1]
                if name not in vias or last is None:
                    return None
                for ly, (x0, y0, x1, y1) in vias[name].items():
                    out.setdefault(ly, []).append((last[0] + int(round(x0 * dbu)), last[1] + int(round(y0 * dbu)),
                                                   last[0] + int(round(x1 * dbu)), last[1] + int(round(y1 * dbu))))
                bot, top = via_layers.get(name, (None, None))
                layer = top if layer == bot else bot if layer == top else layer
                last_ext = None
            elif kind == "rect" and last is not None:
                _, dx0, dy0, dx1, dy1 = it
                out.setdefault(layer, []).append((last[0] + dx0, last[1] + dy0, last[0] + dx1, last[1] + dy1))
            elif kind == "virtual":
                last, last_ext = (it[1], it[2]), None
    return out


def _touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def islands(rects: list[tuple]) -> list[list[tuple]]:
    n = len(rects)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            if _touch(rects[i], rects[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rects[i])
    return list(groups.values())


def union_area(rects: list[tuple]) -> int:
    xs = sorted({v for r in rects for v in (r[0], r[2])})
    ys = sorted({v for r in rects for v in (r[1], r[3])})
    area = 0
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            cx, cy = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
            if any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects):
                area += (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
    return area


def _bbox(rects):
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def _clear(box, others, space):
    return not any(box[0] < o[2] + space and o[0] < box[2] + space and box[1] < o[3] + space and o[1] < box[3] + space
                   for o in others)


def plan_patch(island, layer_rule, dbu, others, grid=5):
    """Patch rect (DBU) bringing ``island`` to the minimum area, or None."""
    amin = layer_rule["area"] * dbu * dbu
    a = union_area(island)
    if a >= amin:
        return None, a
    x0, y0, x1, y1 = _bbox(island)
    horiz = layer_rule["dir"] == "H"
    thick = (y1 - y0) if horiz else (x1 - x0)
    length = (x1 - x0) if horiz else (y1 - y0)
    need = math.ceil((amin * 1.02) / thick)  # the patch alone covers the minimum area (2 % margin)
    need = int(math.ceil(need / grid) * grid)
    grow = max(0, need - length)
    space = int(round(layer_rule.get("spacing", 0) * dbu))
    for left, right in ((grow - grow // 2, grow // 2), (grow, 0), (0, grow)):
        left, right = int(math.ceil(left / grid) * grid), int(math.ceil(right / grid) * grid)
        box = ((x0 - left, y0, x1 + right, y1) if horiz else (x0, y0 - left, x1, y1 + right))
        # only the added metal needs room; the island itself is the router's
        added = ([(x0 - left, y0, x0, y1)] if left else []) + ([(x1, y0, x1 + right, y1)] if right else []) \
            if horiz else ([(x0, y0 - left, x1, y0)] if left else []) + ([(x0, y1, x1, y1 + right)] if right else [])
        if all(_clear(r, others, space) for r in added):
            return box, a
    return "blocked", a


# ----------------------------------------------------------------------- run
def repair(ctx, def_path: str | None = None, layers: list[str] | None = None) -> dict:
    """Patch every routed island under its layer's minimum area (see module doc)."""
    from .analog import query_via_layers_tcl
    import os
    s, pdk = ctx.session, ctx.pdk
    dbu = int(s.value("[ord::get_db_block] getDbUnitsPerMicron"))
    rules = tech_rules(pdk.tech_lef) if os.path.isfile(pdk.tech_lef) else tech_rules_db(s, dbu)
    def_path = def_path or ctx.path("minarea_check.def")
    s.run(f"write_def {q(def_path)}")
    with open(def_path) as f:
        text = f.read()
    nets = wires.parse_def_paths(text)
    names = sorted({it[1] for i in nets.values() for p in i["paths"] for it in p["items"] if it[0] == "via"})
    flat = s.value(query_via_layers_tcl(names)).split() if names else []
    via_layers = {flat[k]: (flat[k + 1], flat[k + 2]) for k in range(0, len(flat), 3)}
    # the lowest routing layer holds standard-cell pin access: left to the router
    check = [ly for ly in (layers or pdk.layers[1:]) if rules["layers"].get(ly, {}).get("area")]
    # metal not in the routing: hard macro / pad outlines (islands there merge with
    # their pins: not judged) and top-level pin shapes (other nets' are obstacles)
    flatb = s.value("""set o {}
foreach i [[ord::get_db_block] getInsts] {
  set m [$i getMaster]
  if {[$m isBlock] || [string match PAD* [$m getType]]} {
    set b [$i getBBox]; lappend o [list [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
  }
}
set p {}
foreach bt [[ord::get_db_block] getBTerms] {
  set n [[$bt getNet] getName]
  foreach bp [$bt getBPins] { foreach bx [$bp getBoxes] {
    set l [$bx getTechLayer]; if {$l eq "NULL"} continue
    lappend p [list $n [$l getName] [$bx xMin] [$bx yMin] [$bx xMax] [$bx yMax]]
  } }
}
list $o $p""")
    from .tools.openroad.tcl import tcl_parse
    blocks_, pins_ = tcl_parse(flatb)
    blocks = [tuple(map(int, b)) for b in blocks_]
    pins = [(n, ly, tuple(map(int, r))) for n, ly, *r in pins_]
    shapes, skipped = {}, []
    for n, info in nets.items():
        sh = net_shapes(info, rules, dbu, via_layers)
        if sh is None:
            skipped.append(n)
            continue
        shapes[n] = sh
    by_layer = {ly: [(n, r) for n, sh in shapes.items() for r in sh.get(ly, [])] for ly in check}
    patches, blocked, found = {}, [], 0
    for n, info in nets.items():
        if n not in shapes:
            continue
        for ly in check:
            for isl in islands(shapes[n].get(ly, [])):
                if union_area(isl) >= rules["layers"][ly]["area"] * dbu * dbu:
                    continue
                found += 1
                if info["ndr"]:
                    blocked.append(f"{n} on {ly}: non-default rule net, not patched")
                    continue
                bb = _bbox(isl)
                if any(_touch(bb, b) for b in blocks) or any(m == n and pl == ly and _touch(bb, r)
                                                           for m, pl, r in pins):
                    blocked.append(f"{n} on {ly} at {bb}: touches a macro/pad or its pin, not judged")
                    continue
                win = (bb[0] - 4000, bb[1] - 4000, bb[2] + 4000, bb[3] + 4000)
                others = [r for m, r in by_layer[ly] if m != n and _touch(r, win)]
                others += [r for m, pl, r in pins if m != n and pl == ly and _touch(r, win)]
                box, area = plan_patch(isl, rules["layers"][ly], dbu, others)
                if box == "blocked":
                    blocked.append(f"{n} on {ly} at {bb} ({area / dbu / dbu:.3f} um2): no room for a patch")
                elif box:
                    patches.setdefault(n, []).append((ly, box))
    planned = sum(len(v) for v in patches.values())
    limit = max(10, len(nets) // 50)
    if planned > limit:  # the router leaves a handful at most: something is wrong, change nothing
        return {"islands": found, "patched": 0, "nets": 0, "skipped_nets": len(skipped),
                "blocked": blocked[:20], "refused": f"{planned} patches planned (limit {limit}): not applied"}
    for n, pl in patches.items():
        info = nets[n]
        for ly, (x0, y0, x1, y1) in pl:
            cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
            info["paths"].append({"layer": ly, "taper": False,
                                  "items": [("pt", cx, cy, None), ("rect", x0 - cx, y0 - cy, x1 - cx, y1 - cy)]})
    if patches:
        s.run("\n".join(wires.encoder_tcl(n, nets[n], via_layers) for n in patches))
    return {"islands": found, "patched": sum(len(v) for v in patches.values()), "nets": len(patches),
            "blocked": blocked, "skipped_nets": len(skipped)}
