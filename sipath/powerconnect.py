"""Connect supply-pin tabs to the power grid with special wiring.

pdngen cannot reach supply tabs that sit under an instance's own upper-metal
obstructions: e.g. the core-side tabs of power pads under their own upper-metal
obstructions, and supply tabs on the side of some analog macros. For each such pin this draws a
straight strap on the tab's layer, outward from the instance, to the nearest
grid shape of the same net on a higher layer, and a via stack at the overlap.

A strap is accepted only if it overlaps no shape of another net on its layer
(PDN shapes and other pins, with a spacing margin) and no obstruction of any
instance on that layer, and it stays inside the die. A same-net shape closer
than the margin without touching it (a notch) is bridged with a fill shape.
Every shape is snapped inward to the manufacturing grid (pin edges in some
vendor LEFs are not on it) and via arrays are centred on their landing. The
result is verified afterwards with ``check_power_grid`` by the caller.
"""
from __future__ import annotations

from .tools.openroad.tcl import q, tcl_parse

DUMP = r"""
proc ::flow_pc_dump {inst_name pin_pattern} {
  set block [ord::get_db_block]
  set inst [::flow_inst $inst_name]
  set m [$inst getMaster]; set bb [$inst getBBox]
  set pins {}
  foreach it [$inst getITerms] {
    set mt [$it getMTerm]
    if {![regexp -- $pin_pattern [$mt getName]]} continue
    set net [$it getNet]; if {$net eq "NULL"} continue
    set shapes {}
    foreach mp [$mt getMPins] { foreach g [$mp getGeometry] {
      set l [$g getTechLayer]; if {$l eq "NULL"} continue
      lappend shapes [list [$l getName] [$g xMin] [$g yMin] [$g xMax] [$g yMax]]
    } }
    lappend pins [list [$mt getName] [$net getName] $shapes]
  }
  set obs {}
  foreach o [$m getObstructions] {
    set l [$o getTechLayer]; if {$l eq "NULL"} continue
    lappend obs [list [$l getName] [$o xMin] [$o yMin] [$o xMax] [$o yMax]]
  }
  return [list [$inst getOrient] [$bb xMin] [$bb yMin] [$bb xMax] [$bb yMax] [$m getWidth] [$m getHeight] $pins $obs]
}
proc ::flow_pc_special {layers} {
  # every special shape on the given layers: {net layer x0 y0 x1 y1}
  set out {}
  foreach net [[ord::get_db_block] getNets] {
    if {![$net isSpecial]} continue
    foreach sw [$net getSWires] { foreach b [$sw getWires] {
      if {[$b isVia]} {
        set v [$b getTechVia]; if {$v eq "NULL"} { set v [$b getBlockVia] }
        foreach l [list [$v getBottomLayer] [$v getTopLayer]] {
          if {[lsearch -exact $layers [$l getName]] >= 0} {
            lappend out [list [$net getName] [$l getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] } }
      } elseif {[lsearch -exact $layers [[$b getTechLayer] getName]] >= 0} {
        lappend out [list [$net getName] [[$b getTechLayer] getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
      }
    } }
  }
  return $out
}
proc ::flow_pc_vias {} {
  # every special via: {net bottom-layer x y} (centre)
  set out {}
  foreach net [[ord::get_db_block] getNets] {
    if {![$net isSpecial]} continue
    foreach sw [$net getSWires] { foreach b [$sw getWires] {
      if {![$b isVia]} continue
      set v [$b getTechVia]; if {$v eq "NULL"} { set v [$b getBlockVia] }
      lappend out [list [$net getName] [[$v getBottomLayer] getName] \
        [expr {([$b xMin] + [$b xMax]) / 2}] [expr {([$b yMin] + [$b yMax]) / 2}]]
    } }
  }
  return $out
}
"""


def _xf(o, x, y, X, Y, W, H):
    return {"R0": (X + x, Y + y), "R90": (X + H - y, Y + x), "R180": (X + W - x, Y + H - y),
            "R270": (X + y, Y + W - x), "MX": (X + x, Y + H - y), "MY": (X + W - x, Y + y),
            "MXR90": (X + y, Y + x), "MYR90": (X + H - y, Y + W - x)}[o]


def _rect(o, X, Y, W, H, x0, y0, x1, y1):
    (ax, ay), (bx, by) = _xf(o, x0, y0, X, Y, W, H), _xf(o, x1, y1, X, Y, W, H)
    return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))


def _overlap(a, b, margin=0):
    return a[0] < b[2] + margin and a[2] > b[0] - margin and a[1] < b[3] + margin and a[3] > b[1] - margin


def connect(session, inst: str, pin_pattern: str, layers: list[str], die, *, max_len: int = 60000,
            margin: int = 400, min_width: int = 1000, via_pitch: int = 1600, via_inset: int = 800) -> list[dict]:
    """Strap every matching supply pin of ``inst`` to its net's grid.

    ``layers``: the PDK's routing layers bottom to top; ``via_pitch`` /
    ``via_inset`` (DBU) shape the via arrays. Returns one
    record per pin: {"pin", "net", "ok", "layer", "to", "length"}."""
    session.run(DUMP)
    grid = max(1, int(session.value("[ord::get_db_tech] getManufacturingGrid") or 1))
    o, bx0, by0, bx1, by1, W, H, pins, obs = tcl_parse(session.value(f"::flow_pc_dump {q(inst)} {q(pin_pattern)}"))
    bx0, by0, bx1, by1, W, H = map(int, (bx0, by0, bx1, by1, W, H))
    obs = [(ly, _rect(o, bx0, by0, W, H, *map(int, r))) for ly, *r in obs]
    special = [(n, ly, tuple(map(int, r))) for n, ly, *r in tcl_parse(session.value(
        "::flow_pc_special {" + " ".join(layers) + "}"))]
    vias = _via_table(session, layers)
    placed = [(n, ly, int(x), int(y)) for n, ly, x, y in tcl_parse(session.value("::flow_pc_vias"))]
    records = []
    for pin, net, shapes in pins:
        best, reasons = None, []
        for ly, *r in shapes:
            if ly not in layers or ly == layers[-1]:
                continue
            p = _rect(o, bx0, by0, W, H, *map(int, r))
            up = layers[layers.index(ly) + 1:]
            for d in ("W", "E", "S", "N"):
                why = []
                cand = _strap(p, d, ly, up, net, special, obs, (bx0, by0, bx1, by1), die, max_len, margin, min_width,
                              why)
                if cand and (best is None or cand["length"] < best["length"]):
                    best = cand
                elif why:
                    reasons.append(f"{ly} {d}: {why[0]}")
        if best is None:
            records.append({"pin": pin, "net": net, "ok": False, "why": sorted(set(reasons))[:12]})
            continue
        best = _on_grid(best, grid)
        best["fills"] = _notch_fills(best["box"], best["layer"], net, special, margin, grid)
        best["skipped_vias"] = _draw(session, net, best, layers, vias, via_pitch, via_inset, grid, placed)
        special.append((net, best["layer"], best["box"]))  # later straps keep clear of this one
        special += [(net, best["layer"], f) for f in best["fills"]]
        records.append({"pin": pin, "net": net, "ok": True, "layer": best["layer"], "to": best["to_layer"],
                        "length": best["length"], "notch_fills": len(best["fills"]),
                        "vias_already_there": best["skipped_vias"]})
    return records


def connect_bterms(session, nets: list[str], layers: list[str], die, *, max_len: int = 10 ** 7,
                   margin: int = 400, min_width: int = 1000, via_pitch: int = 1600, via_inset: int = 800) -> list[dict]:
    """Strap every top-level pin (BTerm) of ``nets`` to its net's grid, like
    connect() does for instance pins: a straight strap on the pin's layer to
    the nearest same-net shape above (e.g. a harness's supply pins at the die
    edge to the core ring). Pin shapes are clipped to the die first (template
    pins may straddle its edge)."""
    session.run(DUMP)
    grid = max(1, int(session.value("[ord::get_db_tech] getManufacturingGrid") or 1))
    pins = tcl_parse(session.value(
        "set o {}; foreach n " + "{" + " ".join(nets) + "}" + " { set net [[ord::get_db_block] findNet $n]; "
        "if {$net eq \"NULL\"} continue; foreach t [$net getBTerms] { foreach p [$t getBPins] { "
        "foreach b [$p getBoxes] { lappend o [list $n [$t getName] [[$b getTechLayer] getName] "
        "[$b xMin] [$b yMin] [$b xMax] [$b yMax]] } } } }; set o"))
    special = [(n, ly, tuple(map(int, r))) for n, ly, *r in tcl_parse(session.value(
        "::flow_pc_special {" + " ".join(layers) + "}"))]
    vias = _via_table(session, layers)
    placed = [(n, ly, int(x), int(y)) for n, ly, x, y in tcl_parse(session.value("::flow_pc_vias"))]
    records = []
    for net, term, ly, *r in pins:
        x0, y0, x1, y1 = map(int, r)
        p = (max(x0, die[0]), max(y0, die[1]), min(x1, die[2]), min(y1, die[3]))
        if ly not in layers or ly == layers[-1] or p[2] <= p[0] or p[3] <= p[1]:
            continue
        up = layers[layers.index(ly) + 1:]
        best, reasons = None, []
        for d in ("W", "E", "S", "N"):
            why = []
            cand = _strap(p, d, ly, up, net, special, [], p, die, max_len, margin, min_width, why)
            if cand and (best is None or cand["length"] < best["length"]):
                best = cand
            elif why:
                reasons.append(f"{d}: {why[0]}")
        if best is None:
            records.append({"pin": term, "net": net, "ok": False, "why": sorted(set(reasons))[:8]})
            continue
        best = _on_grid(best, grid)
        best["fills"] = _notch_fills(best["box"], best["layer"], net, special, margin, grid)
        best["skipped_vias"] = _draw(session, net, best, layers, vias, via_pitch, via_inset, grid, placed)
        special.append((net, best["layer"], best["box"]))
        records.append({"pin": term, "net": net, "ok": True, "layer": best["layer"], "to": best["to_layer"],
                        "length": best["length"]})
    return records


def _strap(p, d, ly, up, net, special, obs, ibox, die, max_len, margin, min_width, why=None):  # noqa: C901
    """Strap from pin shape ``p`` in direction ``d`` on layer ``ly`` to the
    nearest same-net shape on a layer in ``up``; None if blocked."""
    horizontal = d in ("W", "E")
    # candidate targets: same net, higher layer, whose span overlaps the pin's
    # span across the strap direction by at least min_width (the strap is that overlap)
    best = None
    for n, tl, t in special:
        if n != net or tl not in up:
            continue
        lo = max(p[1], t[1]) if horizontal else max(p[0], t[0])
        hi = min(p[3], t[3]) if horizontal else min(p[2], t[2])
        if hi - lo < min_width:
            continue
        if horizontal:
            if d == "W" and t[2] <= p[0]:
                dist = p[0] - t[2]
            elif d == "E" and t[0] >= p[2]:
                dist = t[0] - p[2]
            else:
                continue
        else:
            if d == "S" and t[3] <= p[1]:
                dist = p[1] - t[3]
            elif d == "N" and t[1] >= p[3]:
                dist = t[1] - p[3]
            else:
                continue
        if dist <= max_len and (best is None or dist < best[0]):
            best = (dist, tl, t, lo, hi)
    why = why if why is not None else []
    if best is None:
        why.append("no same-net target on a higher layer within reach")
        return None
    dist, tl, t, lo, hi = best
    x0, y0, x1, y1 = (p[0], lo, p[2], hi) if horizontal else (lo, p[1], hi, p[3])
    # the strap runs through the pin shape (real overlap, not edge contact) into the
    # target by at most its own width (a target parallel to the strap may be long)
    span = max(hi - lo, 1000)
    if horizontal:
        box = (max(t[0], t[2] - span), y0, x1, y1) if d == "W" else (x0, y0, min(t[2], t[0] + span), y1)
    else:
        box = (x0, max(t[1], t[3] - span), x1, y1) if d == "S" else (x0, y0, x1, min(t[3], t[1] + span))
    if not (die[0] <= box[0] and box[2] <= die[2] and die[1] <= box[1] and box[3] <= die[3]):
        why.append("outside the die")
        return None
    # the strap must not touch another net's shape on its layer, nor any obstruction on it
    for n, sl, sbx in special:
        if sl == ly and n != net and _overlap(box, sbx, margin):
            why.append(f"crosses {n} on {ly} at {sbx}")
            return None
    for ol, ob in obs:
        if ol == ly and _overlap(box, ob):
            why.append(f"crosses the instance obstruction on {ly} at {ob}")
            return None
    via_box = (max(box[0], t[0]), max(box[1], t[1]), min(box[2], t[2]), min(box[3], t[3]))
    if via_box[2] - via_box[0] <= 0 or via_box[3] - via_box[1] <= 0:
        why.append("no via overlap")
        return None
    # the landing patches between the strap and the target (and the vias in
    # them) must not touch another net on those layers either: keep the
    # largest part of the overlap that is clear of them
    between = up[:up.index(tl)]
    blocks = [sbx for n, sl, sbx in special if n != net and sl in between and _overlap(via_box, sbx, margin)]
    blocks += [ob for ol, ob in obs if ol in between and _overlap(via_box, ob)]
    if blocks:
        free = _clear_part(via_box, blocks, margin, min_width)
        if free is None:
            why.append(f"via landing on {between} crosses another net at {blocks[:3]}")
            return None
        via_box = free
    return {"box": box, "layer": ly, "to_layer": tl, "via_box": via_box, "length": dist}


def _clear_part(box, blocks, margin, min_len):
    """Largest part of ``box``, cut along its longer side, that stays
    ``margin`` away from every block; None if shorter than ``min_len``."""
    horizontal = box[2] - box[0] >= box[3] - box[1]
    lo, hi = (box[0], box[2]) if horizontal else (box[1], box[3])
    cuts = sorted((b[0] - margin, b[2] + margin) if horizontal else (b[1] - margin, b[3] + margin) for b in blocks)
    best, pos = None, lo
    for a, b in cuts + [(hi, hi)]:
        if min(a, hi) - pos >= min_len and (best is None or min(a, hi) - pos > best[1] - best[0]):
            best = (pos, min(a, hi))
        pos = max(pos, b)
    if best is None:
        return None
    return (best[0], box[1], best[1], box[3]) if horizontal else (box[0], best[0], box[2], best[1])


def _via_table(session, layers):
    """Tech via per adjacent layer pair (bottom -> name)."""
    out = {}
    flat = tcl_parse(session.value("""
set o {}
foreach v [[ord::get_db_tech] getVias] {
  lappend o [list [$v getName] [[$v getBottomLayer] getName] [[$v getTopLayer] getName]]
}
set o"""))
    for name, bot, top in flat:
        if bot in layers and top in layers and layers.index(top) == layers.index(bot) + 1:
            out.setdefault(bot, name)
    return out


def _inward(box, g):
    """``box`` shrunk to the grid ``g`` (DBU)."""
    x0, y0, x1, y1 = box
    return (-(-x0 // g) * g, -(-y0 // g) * g, x1 // g * g, y1 // g * g)


def _outward(box, g):
    x0, y0, x1, y1 = box
    return (x0 // g * g, y0 // g * g, -(-x1 // g) * g, -(-y1 // g) * g)


def _on_grid(s, g):
    return dict(s, box=_inward(s["box"], g), via_box=_inward(s["via_box"], g))


def _notch_fills(box, ly, net, special, margin, g):
    """Same-net shapes on ``ly`` that come within ``margin`` of ``box``
    without touching it leave a notch (a spacing violation even on one net):
    fill the gap over the span where the two face each other."""
    fills = []
    for n, sl, t in special:
        if sl != ly or n != net or _overlap(box, t) or not _overlap(box, t, margin):
            continue
        ox0, ox1 = max(box[0], t[0]), min(box[2], t[2])
        oy0, oy1 = max(box[1], t[1]), min(box[3], t[3])
        if ox1 > ox0:  # facing vertically: fill the y gap
            gap = (ox0, min(box[3], t[3]), ox1, max(box[1], t[1]))
        elif oy1 > oy0:  # facing horizontally: fill the x gap
            gap = (min(box[2], t[2]), oy0, max(box[0], t[0]), oy1)
        else:
            continue  # corner to corner
        fills.append(_outward(gap, g))
    return fills


def _even(box, g=1):
    """OpenDB refuses a special box whose width and length are both odd (in
    DBU); trim one grid step off the max side of any odd dimension."""
    x0, y0, x1, y1 = box
    if (x1 - x0) % 2:
        x1 -= g
    if (y1 - y0) % 2:
        y1 -= g
    return x0, y0, x1, y1


def _centred(lo, hi, pitch, inset, g):
    """Via centres along [lo, hi]: as many as fit at ``pitch`` with ``inset``
    to the ends, centred, on the grid."""
    n = max(1, (hi - lo - 2 * inset) // pitch + 1) if hi - lo >= 2 * inset else 1
    start = (lo + hi) / 2 - (n - 1) * pitch / 2
    return [int(round((start + k * pitch) / g)) * g for k in range(n)]


def _draw(session, net, s, layers, vias, pitch=1600, inset=800, grid=1, placed=None):
    """Draw the strap, fills, landings and via arrays. A via position within
    one pitch of a via of the same net and level already there (e.g. the
    router's, on a wire this strap lands on) is left out: two such cuts would
    overlap into one illegal cut. ``placed`` gets the new vias; returns how
    many positions were left out."""
    placed = placed if placed is not None else []
    x0, y0, x1, y1 = _even(s["box"], grid)
    lo, hi = layers.index(s["layer"]), layers.index(s["to_layer"])
    vx0, vy0, vx1, vy1 = _even(s["via_box"], grid)
    chk = 'if {$b eq "NULL"} { error "dbSBox_create refused a shape" }'
    lines = [f"set net [::flow_net {q(net)}]", "set tech [ord::get_db_tech]",
             "set sw [odb::dbSWire_create $net ROUTED]",
             f"set b [odb::dbSBox_create $sw [$tech findLayer {s['layer']}] {x0} {y0} {x1} {y1} STRIPE]; {chk}"]
    for f in s.get("fills", []):
        fx0, fy0, fx1, fy1 = _even(f, grid)
        lines.append(f"set b [odb::dbSBox_create $sw [$tech findLayer {s['layer']}] {fx0} {fy0} {fx1} {fy1} "
                     f"STRIPE]; {chk}")
    # landing patches on intermediate layers, then a via array per level
    for k in range(lo + 1, hi):
        lines.append(f"set b [odb::dbSBox_create $sw [$tech findLayer {layers[k]}] {vx0} {vy0} {vx1} {vy1} STRIPE]; {chk}")
    xs = _centred(vx0, vx1, pitch, inset, grid)
    ys = _centred(vy0, vy1, pitch, inset, grid)
    skipped = 0
    for k in range(lo, hi):
        name = vias[layers[k]]
        # only vias inside this strap's landing connect the same two shapes
        there = [(ex, ey) for n, ly, ex, ey in placed
                 if n == net and ly == layers[k] and vx0 <= ex <= vx1 and vy0 <= ey <= vy1]
        for x in xs:
            for y in ys:
                if any(abs(x - ex) < pitch and abs(y - ey) < pitch for ex, ey in there):
                    skipped += 1
                    continue
                lines.append(f"set b [odb::dbSBox_create $sw [$tech findVia {name}] {x} {y} STRIPE]; {chk}")
                placed.append((net, layers[k], x, y))
    session.run("\n".join(lines))
    return skipped


def shorts(session, layers: list[str], limit: int = 20) -> list[dict]:
    """Special shapes of different nets that overlap or touch on one layer (a
    short the router never sees: DRC passes, LVS fails). Vias count on their
    bottom and top layers. Returns up to ``limit`` pairs."""
    session.run(DUMP)
    shapes = tcl_parse(session.value("::flow_pc_special {" + " ".join(layers) + "}"))
    return touching_pairs([(n, ly, *map(int, r)) for n, ly, *r in shapes], limit)


def touching_pairs(shapes, limit: int = 20) -> list[dict]:
    """Pairs of (net, layer, x0, y0, x1, y1) of different nets on one layer
    that overlap or touch (sweep along x per layer)."""
    by_layer = {}
    for n, ly, *r in shapes:
        by_layer.setdefault(ly, []).append((*r, n))
    found = []
    for ly, boxes in sorted(by_layer.items()):
        boxes.sort()
        active = []
        for b in boxes:
            active = [a for a in active if a[2] >= b[0]]
            for a in active:
                if a[4] != b[4] and a[1] <= b[3] and b[1] <= a[3]:
                    found.append({"layer": ly, "nets": sorted((a[4], b[4])), "a": a[:4], "b": b[:4]})
                    if len(found) >= limit:
                        return found
            active.append(b)
    return found
