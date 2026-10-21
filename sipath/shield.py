"""Grounded shield wires beside analog nets, and an adjacent-layer keep-away.

OpenROAD's router does not shield signal nets, so SiPath draws the shields
itself, after the analog nets are routed and frozen (sipath.analog) and before
anything digital is routed:

1. Room: a shielded net gets its own NDR (``ANALOG_SH``) whose spacing on every
   layer leaves one minimum-width shield and its spacing on each side.
2. Shields: every straight segment of the net gets a minimum-width wire of the
   shield net (ground) on each side, at the minimum spacing from it, clipped
   around every other shape on that layer with the spacing rule (other nets'
   wiring and vias, the net's own vias and corners, instance pins and
   obstructions, IO pins, other shields that would leave a notch).
3. Ground: a piece touching a shield-net shape on its own layer is grounded by
   it; otherwise it gets a via where it crosses a shield-net shape on the layer
   below or above (the via's pad flush with the shield's inner edge, so the
   spacing to the analog wire never shrinks; its far pad inside the ground
   shape). A piece that reaches no ground is dropped, never left floating.
4. Keep-away (optional): routing obstructions on the layers above and below
   each shielded segment, over the segment and its shields, never on the
   pin-access layers and never over an instance or IO pin.

Shields are drawn only on layers above every standard-cell master's own
geometry (the cells' shapes are not dumped). Coverage (shielded length over
the length of both sides) is reported per net.
"""
from __future__ import annotations

import math

from .tools.openroad.tcl import tcl_parse

DUMP = r"""
proc ::flow_sh_layers {} {
  # routing layers: name direction min-width min-spacing min-area (getArea is in DBU^2)
  set out {}
  foreach l [[ord::get_db_tech] getLayers] {
    if {[$l getType] ne "ROUTING"} continue
    lappend out [list [$l getName] [$l getDirection] [$l getWidth] [$l getSpacing] [expr {round([$l getArea])}]]
  }
  return $out
}
proc ::flow_sh_spacing {layer widths length} {
  set l [[ord::get_db_tech] findLayer $layer]; set out {}
  foreach w $widths { lappend out [$l getSpacing $w $length] }
  return $out
}
proc ::flow_sh_via {name} {
  # a tech via's shapes per layer, relative to its origin: {layer x0 y0 x1 y1}
  set v [[ord::get_db_tech] findVia $name]; set out {}
  foreach b [$v getBoxes] {
    set l [$b getTechLayer]
    lappend out [list [$l getName] [$l getType] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
  }
  return [list [[$v getBottomLayer] getName] [[$v getTopLayer] getName] $out]
}
proc ::flow_sh_special {} {
  # every special shape on a routing layer, vias per layer: {net layer kind x0 y0 x1 y1}
  set out {}
  foreach net [[ord::get_db_block] getNets] {
    if {![$net isSpecial]} continue
    set n [$net getName]
    foreach sw [$net getSWires] { foreach b [$sw getWires] {
      if {[$b isVia]} {
        set v [$b getTechVia]; if {$v eq "NULL"} { set v [$b getBlockVia] }
        set xy [$b getViaXY]; set x [lindex $xy 0]; set y [lindex $xy 1]
        foreach vb [$v getBoxes] {
          set l [$vb getTechLayer]
          if {[$l getType] ne "ROUTING"} continue
          lappend out [list $n [$l getName] via [expr {$x + [$vb xMin]}] [expr {$y + [$vb yMin]}] \
            [expr {$x + [$vb xMax]}] [expr {$y + [$vb yMax]}]]
        }
      } else {
        lappend out [list $n [[$b getTechLayer] getName] wire [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
      }
    } }
  }
  return $out
}
proc ::flow_sh_instances {} {
  # every instance whose master is not a core cell: {orient x y W H {{layer x0 y0 x1 y1} ...}}
  # with its pins and obstructions in master coordinates
  set out {}
  foreach i [[ord::get_db_block] getInsts] {
    set m [$i getMaster]
    if {[$m isCore]} continue
    set boxes {}
    foreach mt [$m getMTerms] { foreach mp [$mt getMPins] { foreach g [$mp getGeometry] { lappend boxes $g } } }
    foreach o [$m getObstructions] { lappend boxes $o }
    set shapes {}
    foreach g $boxes {
      set l [$g getTechLayer]; if {$l eq "NULL" || [$l getType] ne "ROUTING"} continue
      lappend shapes [list [$l getName] [$g xMin] [$g yMin] [$g xMax] [$g yMax]]
    }
    set bb [$i getBBox]
    lappend out [list [$i getOrient] [$bb xMin] [$bb yMin] [$m getWidth] [$m getHeight] $shapes]
  }
  return $out
}
proc ::flow_sh_bterms {} {
  set out {}
  foreach bt [[ord::get_db_block] getBTerms] { foreach bp [$bt getBPins] { foreach b [$bp getBoxes] {
    set l [$b getTechLayer]; if {$l eq "NULL"} continue
    lappend out [list [$l getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
  } } }
  return $out
}
proc ::flow_sh_core_top {} {
  # the highest routing layer a core (standard) cell master placed in the design draws on, and that master
  set top -1; set who ""; set names {}; set seen [dict create]
  foreach l [[ord::get_db_tech] getLayers] { if {[$l getType] eq "ROUTING"} { lappend names [$l getName] } }
  foreach i [[ord::get_db_block] getInsts] {
    set m [$i getMaster]
    if {![$m isCore] || [dict exists $seen [$m getName]]} continue
    dict set seen [$m getName] 1
    set gs {}
    foreach mt [$m getMTerms] { foreach mp [$mt getMPins] { foreach g [$mp getGeometry] { lappend gs $g } } }
    foreach o [$m getObstructions] { lappend gs $o }
    foreach g $gs {
      set l [$g getTechLayer]; if {$l eq "NULL"} continue
      set k [lsearch -exact $names [$l getName]]; if {$k > $top} { set top $k; set who [$m getName] }
    }
  }
  return [list [expr {$top >= 0 ? [lindex $names $top] : ""}] $who]
}
"""

WIDTH_SAMPLES_UM = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 1e6)


def _overlap(a, b, m=0):
    return a[0] < b[2] + m and a[2] > b[0] - m and a[1] < b[3] + m and a[3] > b[1] - m


def _inside(a, b):
    return b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]


def _up(v, g):
    return -(-v // g) * g


def _down(v, g):
    return v // g * g


class Index:
    """Boxes per layer in square bins, for overlap queries."""

    def __init__(self, bin_size: int = 20000):
        self.bin, self.bins = bin_size, {}

    def add(self, layer, box, item):
        b = self.bin
        for i in range(box[0] // b, box[2] // b + 1):
            for j in range(box[1] // b, box[3] // b + 1):
                self.bins.setdefault((layer, i, j), []).append((box, item))

    def query(self, layer, box, m=0):
        b, seen, out = self.bin, set(), []
        for i in range((box[0] - m) // b, (box[2] + m) // b + 1):
            for j in range((box[1] - m) // b, (box[3] + m) // b + 1):
                for r, item in self.bins.get((layer, i, j), ()):
                    if id(item) not in seen and _overlap(box, r, m):
                        seen.add(id(item))
                        out.append((r, item))
        return out


class Rules:
    """Per layer: direction, min width, min area, spacing by width (DBU)."""

    def __init__(self, layers: dict, spacing: dict, grid: int):
        self.layers, self.spacing, self.grid = layers, spacing, grid  # spacing: layer -> [(width, s)]

    def space(self, layer: str, width: int) -> int:
        for w, s in self.spacing[layer]:
            if width <= w:
                return s
        return self.spacing[layer][-1][1]

    def width(self, layer: str) -> int:
        return self.layers[layer]["width"]

    def min_len(self, layer: str, w: int) -> int:
        a = self.layers[layer]["area"]
        return _up(max(2 * w, math.ceil(a / w) if a else 0), self.grid)


def candidates(box, layer, rules: Rules, w_sh: int) -> list[tuple]:
    """The two shield rectangles beside a straight wire ``box``: (rect,
    horizontal, side) with side +1 above/right, -1 below/left; none for a box
    shorter than twice its width (a via landing or patch)."""
    dx, dy = box[2] - box[0], box[3] - box[1]
    horizontal = dx >= dy
    w, length = (dy, dx) if horizontal else (dx, dy)
    if length < 2 * w:
        return []
    g = rules.grid
    s = _up(rules.space(layer, max(w, w_sh)), g)
    if horizontal:
        return [((box[0], box[3] + s, box[2], box[3] + s + w_sh), True, 1),
                ((box[0], box[1] - s - w_sh, box[2], box[1] - s), True, -1)]
    return [((box[2] + s, box[1], box[2] + s + w_sh, box[3]), False, 1),
            ((box[0] - s - w_sh, box[1], box[0] - s, box[3]), False, -1)]


def clip(rect, horizontal: bool, blocks: list[tuple], rules: Rules, layer: str, min_len: int) -> list[tuple]:
    """Parts of ``rect`` (along its length) at least ``min_len`` long that stay
    the spacing rule away from every block (box) on its layer."""
    g = rules.grid
    w_sh = (rect[3] - rect[1]) if horizontal else (rect[2] - rect[0])
    lo, hi = (rect[0], rect[2]) if horizontal else (rect[1], rect[3])
    cuts = []
    for b in blocks:
        m = rules.space(layer, max(w_sh, min(b[2] - b[0], b[3] - b[1])))
        if not _overlap(rect, b, m):
            continue
        cuts.append((b[0] - m, b[2] + m) if horizontal else (b[1] - m, b[3] + m))
    out, pos = [], lo
    for a, b in sorted(cuts) + [(hi, hi)]:
        end = min(a, hi)
        p0, p1 = _up(pos, g), _down(end, g)
        if p1 - p0 >= min_len:
            out.append((p0, rect[1], p1, rect[3]) if horizontal else (rect[0], p0, rect[2], p1))
        pos = max(pos, b)
    return out


def ground_via(piece, horizontal: bool, side: int, gbox, via: dict, layer: str, other: str, g: int):
    """A via joining ``piece`` (on ``layer``) to the ground shape ``gbox`` on
    ``other``: its pad on ``layer`` flush with the piece's inner edge (the side
    facing the analog wire) and within the piece along its length, its pad on
    ``other`` inside ``gbox``. Returns (x, y, pad on layer) or None."""
    pl, po = via["pads"][layer], via["pads"][other]
    ov = (max(piece[0], gbox[0]), max(piece[1], gbox[1]), min(piece[2], gbox[2]), min(piece[3], gbox[3]))
    if horizontal:
        if ov[2] <= ov[0]:
            return None
        x = _down((ov[0] + ov[2]) // 2, g)
        y = piece[1] - pl[1] if side > 0 else piece[3] - pl[3]  # pad's inner edge on the piece's inner edge
    else:
        if ov[3] <= ov[1]:
            return None
        y = _down((ov[1] + ov[3]) // 2, g)
        x = piece[0] - pl[0] if side > 0 else piece[2] - pl[2]
    pad = (x + pl[0], y + pl[1], x + pl[2], y + pl[3])
    lo, hi = (piece[0], piece[2]) if horizontal else (piece[1], piece[3])
    plo, phi = (pad[0], pad[2]) if horizontal else (pad[1], pad[3])
    if plo < lo or phi > hi:
        return None
    if not _inside((x + po[0], y + po[1], x + po[2], y + po[3]), gbox):
        return None
    return x, y, pad


def _sliver(a, b, w: int) -> bool:
    """``a`` and ``b`` overlap, but by less than ``w`` across one side: merged,
    they leave a neck narrower than the minimum width."""
    return _overlap(a, b) and min(min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])) < w


def _neck(box, ly, ground, w: int) -> bool:
    """``box`` would touch a shield-net shape on ``ly`` by a sliver that no
    other shield-net shape it properly overlaps covers (a real neck once
    merged)."""
    shapes = [r for r, _ in ground.query(ly, box) if _overlap(box, r)]
    full = [r for r in shapes if not _sliver(box, r, w)]
    for r in shapes:
        if r in full:
            continue
        ov = (max(box[0], r[0]), max(box[1], r[1]), min(box[2], r[2]), min(box[3], r[3]))
        if not any(_inside(ov, f) for f in full):
            return True
    return False


def _clear(box, ly, blocks, ground, rules) -> bool:
    """``box`` on ``ly`` keeps the spacing rule from every block and from every
    shield-net shape it does not touch (a notch), and overlaps the ones it
    touches by at least the minimum width (no neck)."""
    far = 4 * rules.space(ly, 10 ** 7)
    for r, _ in blocks.query(ly, box, far):
        if _overlap(box, r, rules.space(ly, max(min(box[2] - box[0], box[3] - box[1]), min(r[2] - r[0], r[3] - r[1])))):
            return False
    if _neck(box, ly, ground, rules.width(ly)):
        return False
    for r, _ in ground.query(ly, box, far):
        if not _overlap(box, r) and _overlap(box, r, rules.space(ly, min(box[2] - box[0], box[3] - box[1]))):
            return False
    return True


def _via_ok(via: dict, x: int, y: int, ground, blocks, rules: Rules) -> bool:
    """A new via at (x, y): on each of its layers the pad keeps the spacing
    rule from other nets, comes nowhere near another via of the shield net
    (overlapping or close cuts), and touches or keeps clear of the shield
    net's wires (no notch)."""
    for ly, r in via["pads"].items():
        if ly not in rules.layers:
            continue
        pad = (x + r[0], y + r[1], x + r[2], y + r[3])
        if not _clear(pad, ly, blocks, Index(), rules):
            return False
        m = rules.space(ly, max(pad[2] - pad[0], pad[3] - pad[1]))
        for s, item in ground.query(ly, pad, m):
            if item[1] == "via" or not _overlap(pad, s):
                return False
        if _neck(pad, ly, ground, rules.width(ly)):
            return False
    return True


def tie_strap(piece, horizontal: bool, side: int, ly: str, layers: list[str], strap_layers: set, ground, blocks,
              rules: Rules, vias: dict, reach: int):
    """The shortest strap grounding ``piece`` when nothing crosses it: on a
    layer next to the piece's, from a via on the piece outward (away from the
    analog wire) to a shield-net shape on the strap's layer (ending inside
    it), on the piece's layer or on the layer beyond the strap's (through a
    second via inside it). Returns (strap layer, strap box, [(via, x, y)]) or
    None."""
    g = rules.grid
    k = layers.index(ly)
    best = None
    for a in (layers[k - 1] if k > 0 else None, layers[k + 1] if k + 1 < len(layers) else None):
        if a is None or a not in strap_layers:
            continue
        via = vias.get(min(ly, a, key=layers.index))
        if not via:
            continue
        pl, pa = via["pads"][ly], via["pads"][a]
        v_of = {v["name"]: v for v in vias.values()}
        ws = _up(max(rules.width(a), (pa[2] - pa[0]) if horizontal else (pa[3] - pa[1])), g)
        # the strap runs across the piece: along y for a horizontal piece
        lo, hi = (piece[0], piece[2]) if horizontal else (piece[1], piece[3])
        out_edge = (piece[3] if side > 0 else piece[1]) if horizontal else (piece[2] if side > 0 else piece[0])
        zone = ((lo, out_edge, hi, out_edge + reach) if side > 0 else (lo, out_edge - reach, hi, out_edge)) \
            if horizontal else ((out_edge, lo, out_edge + reach, hi) if side > 0 else (out_edge - reach, lo, out_edge, hi))
        ka = layers.index(a)
        beyond = layers[2 * ka - k] if 0 <= 2 * ka - k < len(layers) else None
        for tl in (a, ly, beyond):
            if tl is None:
                continue
            fvia = via if tl in (a, ly) else vias.get(min(a, tl, key=layers.index))
            if fvia is None:
                continue
            for t, item in ground.query(tl, zone):
                if item[1] != "wire":
                    continue  # never land on another via's pad
                tlo, thi = (t[0], t[2]) if horizontal else (t[1], t[3])
                plo, phi = max(lo, tlo), min(hi, thi)
                if phi - plo < ws:
                    continue
                pos = _down((plo + phi) // 2, g)
                # the via on the piece, its pad flush with the inner edge
                if horizontal:
                    near = (pos, piece[1] - pl[1] if side > 0 else piece[3] - pl[3])
                else:
                    near = (piece[0] - pl[0] if side > 0 else piece[2] - pl[2], pos)
                pad_l = (near[0] + pl[0], near[1] + pl[1], near[0] + pl[2], near[1] + pl[3])
                pad_a = (near[0] + pa[0], near[1] + pa[1], near[0] + pa[2], near[1] + pa[3])
                made = [(via["name"], *near)]
                if tl == a:  # end inside the target on the strap's layer
                    if horizontal:
                        y1 = t[1] + min(t[3] - t[1], ws) if side > 0 else t[3] - min(t[3] - t[1], ws)
                        box = (pos - ws // 2, min(pad_a[1], y1), pos - ws // 2 + ws, max(pad_a[3], y1))
                    else:
                        x1 = t[0] + min(t[2] - t[0], ws) if side > 0 else t[2] - min(t[2] - t[0], ws)
                        box = (min(pad_a[0], x1), pos - ws // 2, max(pad_a[2], x1), pos - ws // 2 + ws)
                    if not (horizontal and t[0] <= box[0] and box[2] <= t[2]) and \
                            not (not horizontal and t[1] <= box[1] and box[3] <= t[3]):
                        continue  # a partial overlap would leave a notch
                else:  # a second via inside the target (on the piece's layer or the one beyond the strap's)
                    far = (pos, _down((t[1] + t[3]) // 2, g)) if horizontal else (_down((t[0] + t[2]) // 2, g), pos)
                    ft, fa_rel = fvia["pads"][tl], fvia["pads"][a]
                    if not _inside((far[0] + ft[0], far[1] + ft[1], far[0] + ft[2], far[1] + ft[3]), t):
                        continue
                    fa = (far[0] + fa_rel[0], far[1] + fa_rel[1], far[0] + fa_rel[2], far[1] + fa_rel[3])
                    box = (min(pad_a[0], fa[0]), min(pad_a[1], fa[1]), max(pad_a[2], fa[2]), max(pad_a[3], fa[3]))
                    box = (box[0], box[1], max(box[2], box[0] + ws), max(box[3], box[1] + ws))
                    made.append((fvia["name"], *far))
                length = (box[3] - box[1]) if horizontal else (box[2] - box[0])
                if best is not None and length >= best[0]:
                    continue
                if not _clear(pad_l, ly, blocks, Index(), rules) or \
                        not all(_via_ok(v_of[name], vx, vy, ground, blocks, rules) for name, vx, vy in made):
                    continue
                # the strap may touch the piece and its target only
                if not _clear(box, a, blocks, ground, rules):
                    continue
                best = (length, a, box, made)
    return None if best is None else best[1:]


def _add_via(ground, vias: dict, name: str, x: int, y: int) -> None:
    for v in vias.values():
        if v["name"] == name:
            for ly, r in v["pads"].items():
                ground.add(ly, (x + r[0], y + r[1], x + r[2], y + r[3]), [None, "via"])


def plan(nets: list[str], shield_net: str, special: list, inst_shapes: list, bterms: list, rules: Rules,
         layers: list[str], allowed: set, vias: dict, strap_layers: set | None = None, reach: int = 0) -> dict:
    """Shield pieces and grounding vias for ``nets``. ``special``: (net,
    layer, kind, box); ``inst_shapes``/``bterms``: (layer, box); ``vias``:
    bottom layer -> {name, pads: {layer: rel box}}; a piece nothing crosses
    gets a tie strap (``tie_strap``) on a ``strap_layers`` layer within
    ``reach`` DBU. Returns {net: {pieces: [(layer, box)], ties: [(layer,
    box)], vias: [(name, x, y)], covered, sides, dropped}}."""
    g = rules.grid
    blocks, ground = Index(), Index()
    for n, ly, kind, box in special:
        (ground if n == shield_net else blocks).add(ly, box, [box, kind])
    for ly, box in inst_shapes + bterms:
        blocks.add(ly, box, [box])
    out = {}
    for net in nets:
        rec = {"pieces": [], "ties": [], "vias": [], "covered": 0, "sides": 0, "dropped": 0,
               "skipped_layers": set()}
        for n, ly, kind, box in special:
            if n != net or kind != "wire":
                continue
            if ly not in allowed:
                rec["skipped_layers"].add(ly)
                continue
            w_sh = _up(rules.width(ly), g)
            for rect, horizontal, side in candidates(box, ly, rules, w_sh):
                rec["sides"] += (rect[2] - rect[0]) if horizontal else (rect[3] - rect[1])
                near = [r for r, _ in blocks.query(ly, rect, 4 * rules.space(ly, 10 ** 7))]
                # shield-net shapes that would come within spacing without touching leave a notch
                # (or that they would touch by a sliver nothing else covers: a neck)
                touching = [r for r, _ in ground.query(ly, rect) if _overlap(rect, r)]
                full = [r for r in touching if not _sliver(rect, r, w_sh)]
                near += [r for r, _ in ground.query(ly, rect, 4 * rules.space(ly, 10 ** 7)) if not _overlap(rect, r)]
                near += [r for r in touching if r not in full and not any(
                    _inside((max(rect[0], r[0]), max(rect[1], r[1]), min(rect[2], r[2]), min(rect[3], r[3])), f)
                    for f in full)]
                for piece in clip(rect, horizontal, near, rules, ly, rules.min_len(ly, w_sh)):
                    done = any(_overlap(piece, r) for r, _ in ground.query(ly, piece))
                    made = []
                    k = layers.index(ly)
                    for other in [layers[k - 1] if k > 0 else None, layers[k + 1] if k + 1 < len(layers) else None]:
                        if done or other is None:
                            continue
                        via = vias.get(min(ly, other, key=layers.index))
                        if not via:
                            continue
                        for gbox, item in ground.query(other, piece):
                            if item[1] != "wire":
                                continue  # never land on another via's pad
                            v = ground_via(piece, horizontal, side, gbox, via, ly, other, g)
                            if v is None:
                                continue
                            x, y, pad = v
                            # the pads may stick out beyond the shield and the target: keep them clear
                            if not _via_ok(via, x, y, ground, blocks, rules):
                                continue
                            made.append((via["name"], x, y))
                            done = True
                            break
                    if not done and reach:
                        tie = tie_strap(piece, horizontal, side, ly, layers, strap_layers or set(), ground, blocks,
                                        rules, vias, reach)
                        if tie:
                            tl, tbox, tvias = tie
                            rec["ties"].append((tl, tbox))
                            ground.add(tl, tbox, [tbox, "wire"])
                            made += tvias
                            done = True
                    if not done:
                        rec["dropped"] += 1
                        continue
                    rec["pieces"].append((ly, piece))
                    rec["vias"] += made
                    for vn, vx, vy in made:
                        _add_via(ground, vias, vn, vx, vy)
                    rec["covered"] += (piece[2] - piece[0]) if horizontal else (piece[3] - piece[1])
                    ground.add(ly, piece, [piece, "wire"])  # later shields touch it or keep clear of it
        rec["skipped_layers"] = sorted(rec["skipped_layers"])
        out[net] = rec
    return out


def keepaway(nets, special: list, inst_shapes: list, bterms: list, rules: Rules, layers: list[str],
             barred: set) -> list[tuple]:
    """Obstruction boxes on the layers just above and below each straight
    segment of ``nets``, as wide as the segment and its shields, not on
    ``barred`` layers, cut around instance and IO pins on that layer."""
    pins = Index()
    for ly, box in inst_shapes + bterms:
        pins.add(ly, box, [box])
    out = []
    for n, ly, kind, box in special:
        if n not in nets or kind != "wire":
            continue
        dx, dy = box[2] - box[0], box[3] - box[1]
        horizontal = dx >= dy
        if (dx if horizontal else dy) < 2 * (dy if horizontal else dx):
            continue
        w_sh = _up(rules.width(ly), rules.grid)
        e = _up(rules.space(ly, max(dy if horizontal else dx, w_sh)), rules.grid) + w_sh
        rect = (box[0], box[1] - e, box[2], box[3] + e) if horizontal else (box[0] - e, box[1], box[2] + e, box[3])
        k = layers.index(ly)
        for other in (layers[k - 1] if k > 0 else None, layers[k + 1] if k + 1 < len(layers) else None):
            if other is None or other in barred:
                continue
            near = [r for r, _ in pins.query(other, rect, 4 * rules.space(other, 10 ** 7))]
            out += [(other, p) for p in clip(rect, horizontal, near, rules, other, rules.width(other))]
    return out


def guard(nets: dict, special: list, inst_shapes: list, bterms: list, rules: Rules) -> list[tuple]:
    """Routing obstructions that keep later (digital) wiring the NDR spacing
    from frozen analog wiring: the router keeps only the minimum spacing from
    special wiring, so every wire and via shape of a net in ``nets`` (net ->
    {layer: NDR spacing, DBU}) is grown on its layer by the NDR spacing less
    the minimum spacing, cut around instance and IO pins on that layer."""
    pins = Index()
    for ly, box in inst_shapes + bterms:
        pins.add(ly, box, [box])
    out = []
    for n, ly, kind, box in special:
        sp = nets.get(n, {}).get(ly)
        if not sp:
            continue
        e = _up(sp - rules.space(ly, 0), rules.grid)
        if e <= 0:
            continue
        rect = (box[0] - e, box[1] - e, box[2] + e, box[3] + e)
        horizontal = rect[2] - rect[0] >= rect[3] - rect[1]
        near = [r for r, _ in pins.query(ly, rect, 4 * rules.space(ly, 10 ** 7))]
        out += [(ly, p) for p in clip(rect, horizontal, near, rules, ly, rules.width(ly))]
    return out


def tech(session, layers: list[str]) -> tuple[Rules, dict]:
    """Layer rules and the grounding via per adjacent layer pair from the tech."""
    session.run(DUMP)
    grid = max(1, int(session.value("[ord::get_db_tech] getManufacturingGrid") or 1))
    dbu = int(session.value("[ord::get_db_tech] getDbUnitsPerMicron"))
    info = {}
    for name, d, w, s, a in tcl_parse(session.value("::flow_sh_layers")):
        info[name] = {"dir": d, "width": int(w), "space": int(s), "area": int(a)}
    spacing = {}
    for ly in layers:
        ws = [int(round(x * dbu)) for x in WIDTH_SAMPLES_UM[:-1]]
        sp = [int(v) for v in tcl_parse(session.value(f"::flow_sh_spacing {ly} {{{' '.join(map(str, ws))}}} 0"))]
        table = [(w, max(s, info[ly]["space"])) for w, s in zip(ws, sp)]
        table.append((10 ** 12, table[-1][1]))
        spacing[ly] = [(w, max(s for _, s in table[:k + 1])) for k, (w, _) in enumerate(table)]
    rules = Rules({ly: info[ly] for ly in layers}, spacing, grid)
    from .powerconnect import _via_table
    vias = {}
    for bot, name in _via_table(session, layers).items():
        b, t, shapes = tcl_parse(session.value(f"::flow_sh_via {name}"))
        pads = {}
        for ly, kind, *r in shapes:
            if kind == "ROUTING":
                r = tuple(map(int, r))
                p = pads.get(ly)
                pads[ly] = r if p is None else (min(p[0], r[0]), min(p[1], r[1]), max(p[2], r[2]), max(p[3], r[3]))
        if b in pads and t in pads:
            vias[bot] = {"name": name, "pads": pads}
    return rules, vias


def dump(session) -> tuple[list, list, list, tuple]:
    """Special shapes, non-core instance shapes, IO pin shapes and the highest
    routing layer a placed standard cell draws on with that cell ("", "" if none)."""
    special = [(n, ly, k, tuple(map(int, r))) for n, ly, k, *r in tcl_parse(session.value("::flow_sh_special"))]
    from .powerconnect import _rect
    insts = []
    for o, x, y, w, h, shapes in tcl_parse(session.value("::flow_sh_instances")):
        x, y, w, h = map(int, (x, y, w, h))
        insts += [(ly, _rect(o, x, y, w, h, *map(int, r))) for ly, *r in shapes]
    bterms = [(ly, tuple(map(int, r))) for ly, *r in tcl_parse(session.value("::flow_sh_bterms"))]
    top = tcl_parse(session.value("::flow_sh_core_top"))
    return special, insts, bterms, tuple(top) if len(top) == 2 else ("", "")


def ndr_spacing(rules: Rules, ndr: dict, dbu: int) -> dict:
    """NDR spacing (um) per layer that leaves room for a shield on each side of
    a wire of the NDR's width: spacing to the shield, the shield, spacing from
    it to a wide neighbour."""
    out = {}
    for ly, w_um in ndr["width"].items():
        if ly not in rules.layers:
            continue
        w, w_sh = int(round(w_um * dbu)), rules.width(ly)
        room = rules.space(ly, max(w, w_sh)) + w_sh + rules.space(ly, 3 * dbu)
        out[ly] = round(max(room / dbu, ndr["spacing"].get(ly, 0.0)), 3)
    return out


def draw(session, shield_net: str, result: dict, grid: int) -> int:
    """Draw every planned piece and via as special wiring of ``shield_net``."""
    from .powerconnect import _even
    chk = 'if {$b eq "NULL"} { error "dbSBox_create refused a shield shape" }'
    lines = [f"set net [::flow_net {{{shield_net}}}]", "set tech [ord::get_db_tech]",
             "set sw [odb::dbSWire_create $net ROUTED]"]
    n = 0
    for rec in result.values():
        for ly, box in rec["pieces"]:
            x0, y0, x1, y1 = _even(box, grid)
            lines.append(f"set b [odb::dbSBox_create $sw [$tech findLayer {ly}] {x0} {y0} {x1} {y1} NONE]; {chk}")
            n += 1
        for ly, box in rec["ties"]:
            x0, y0, x1, y1 = _even(box, grid)
            lines.append(f"set b [odb::dbSBox_create $sw [$tech findLayer {ly}] {x0} {y0} {x1} {y1} NONE]; {chk}")
            n += 1
        for name, x, y in rec["vias"]:
            lines.append(f"set b [odb::dbSBox_create $sw [$tech findVia {name}] {x} {y} NONE]; {chk}")
            n += 1
    if n:
        session.run("\n".join(lines))
    return n


def obstruct(session, boxes: list[tuple]) -> int:
    lines = ["set block [ord::get_db_block]; set tech [ord::get_db_tech]"]
    for ly, (x0, y0, x1, y1) in boxes:
        lines.append(f"odb::dbObstruction_create $block [$tech findLayer {ly}] {x0} {y0} {x1} {y1}")
    if boxes:
        session.run("\n".join(lines))
    return len(boxes)


def summary(result: dict, dbu: int) -> dict:
    return {n: {"pieces": len(r["pieces"]), "ties": len(r["ties"]), "vias": len(r["vias"]), "dropped": r["dropped"],
                "shielded_um": round(r["covered"] / dbu, 3), "sides_um": round(r["sides"] / dbu, 3),
                "coverage": round(r["covered"] / r["sides"], 4) if r["sides"] else 0.0,
                "unshielded_layers": r["skipped_layers"]} for n, r in result.items()}
