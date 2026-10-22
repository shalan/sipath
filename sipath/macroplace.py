"""Hard-macro placement: candidates, rules and a quick placement-based score.

Methods (param ``method``):

* ``random`` (default): for each autoplaced macro, ``samples`` legal positions on
  the row/site grid drawn from ``random.Random(seed)`` (a share of them pushed
  against a core edge), each tried in every allowed orientation (``orients``
  param, else ``Macro.orients``, else ``Macro.orient``). The candidate with the
  lowest objective is kept: macro-net HPWL + ``analog_weight`` x analog-net
  HPWL, over placed terminals only and pin-accurate, so orientation and the
  side the pins are on count. The same seed gives the same placement.
* ``mpl``: OpenROAD's ``rtl_macro_placer``. Keepouts are passed as placement
  blockages (removed afterwards) and halos widened to the channel and guard
  rules; its result is checked against the same rules as ``random`` and the
  attempt fails if it breaks one.

Rules, on the macro boxes (``Design`` fields, overridable per attempt):

* halo: a macro grown by its halo stays in the core, off keepouts and off
  other grown macros;
* channel (``macro_channel``): two macros facing each other are at least this
  far apart, and a macro is either against the core edge (within halo + one
  row) or this far from it, so no sliver channels are left;
* pin side (``macro_pin_clearance``): in front of a macro edge that carries
  signal pins there is at least this much space to the next macro or the core
  edge, also against the core edge;
* analog guard (``analog_guard``): a macro on an analog net or analog supply
  keeps this much extra space to every other macro, and a placement blockage
  of that width around it keeps standard cells away.

Evaluation (param ``evaluate``, default on): once the macros are placed and
the rows cut, a non-timing global placement of the standard cells, a detailed
placement (legal positions, so no cell sits on a macro) and a global route with ``-allow_congestion`` measure ``quick_hpwl_um`` and the
congestion (``quick_overflow``, ``quick_congested_gcells``). The cells are then
unplaced and the guides cleared, so the checkpoint holds only the macros.
``macro_cost_um`` = quick HPWL + analog_weight x analog HPWL; the
``macro_quick`` score ranks by overflow, then that cost.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

from . import congestion
from .tools.openroad.ops import db as db_ops
from .tools.openroad.ops import macros as ops
from .tools.openroad.ops import route as route_ops

ROT90 = ("R90", "R270", "MXR90", "MYR90")
ALL_ORIENTS = ("R0", "MX", "MY", "R180", "R90", "R270", "MXR90", "MYR90")
OPPOSITE = {"left": "right", "right": "left", "bottom": "top", "top": "bottom"}


@dataclass
class Rules:
    channel: float = 0.0  # DBU
    pin_clearance: float = 0.0
    analog_guard: float = 0.0
    hug: float = 0.0  # distance up to which a macro counts as against the core edge (beyond its halo)


@dataclass
class MacroBox:
    inst: str
    box: tuple  # x0 y0 x1 y1 (DBU), the macro itself
    halo: int
    pin_sides: frozenset = frozenset()  # placed sides with signal pins
    analog: bool = False


# ---------------------------------------------------------------- geometry
def oriented_size(w, h, orient):
    return (h, w) if orient in ROT90 else (w, h)


def orient_side(side: str, orient: str, w: int, h: int) -> str:
    """Placed side of a master side ``side`` for orientation ``orient``."""
    from .powerconnect import _rect
    local = {"left": (0, 0, 0, h), "right": (w, 0, w, h), "bottom": (0, 0, w, 0), "top": (0, h, w, h)}[side]
    W, H = oriented_size(w, h, orient)
    r = _rect(orient, 0, 0, w, h, *local)
    if r[0] == r[2]:
        return "left" if r[0] == 0 else "right"
    return "bottom" if r[1] == 0 else "top"


def pin_sides(pins, w, h, frac: float = 0.1) -> frozenset:
    """Master sides carrying at least ``frac`` of the signal pins (each pin
    counted once, on the side nearest its shapes' centre)."""
    count = {s: 0 for s in OPPOSITE}
    total = 0
    for _name, sig, _io, shapes in pins:
        if sig in ("POWER", "GROUND") or not shapes:
            continue
        xs = [(int(a) + int(c)) / 2 for a, _, c, _ in shapes]
        ys = [(int(b) + int(d)) / 2 for _, b, _, d in shapes]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        side = min((("left", cx), ("right", w - cx), ("bottom", cy), ("top", h - cy)), key=lambda t: t[1])[0]
        count[side] += 1
        total += 1
    return frozenset(s for s, n in count.items() if total and n >= max(1, frac * total))


def _grow(b, d):
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def _overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def violation(c: MacroBox, others, core, keepouts, rules: Rules) -> str | None:
    """First rule ``c`` breaks against the core, keepouts and placed macros."""
    x0, y0, x1, y1 = c.box
    h = c.halo
    gaps = {"left": x0 - core[0], "bottom": y0 - core[1], "right": core[2] - x1, "top": core[3] - y1}
    for side, g in gaps.items():
        if g < h:
            return f"halo leaves the core on the {side}"
        if side in c.pin_sides and g < rules.pin_clearance:
            return f"pins on the {side} edge are {g} from the core edge (< {rules.pin_clearance:.0f})"
        if rules.channel and h + rules.hug < g < rules.channel:
            return f"sliver channel of {g} to the core edge on the {side} (< {rules.channel:.0f})"
    grown = _grow(c.box, h)
    for k in keepouts:
        if _overlap(grown, k):
            return f"overlaps keepout {k}"
    for o in others:
        need = c.halo + o.halo + (rules.analog_guard if (c.analog or o.analog) else 0)
        gx = max(o.box[0] - x1, x0 - o.box[2])
        gy = max(o.box[1] - y1, y0 - o.box[3])
        if gy < 0 or gx < 0:  # projections overlap on one axis: they face each other
            horizontal = gy < 0
            g = gx if horizontal else gy
            if horizontal:
                side_c = "right" if o.box[0] >= x1 else "left"
            else:
                side_c = "top" if o.box[1] >= y1 else "bottom"
            req = max(need, rules.channel)
            if side_c in c.pin_sides or OPPOSITE[side_c] in o.pin_sides:
                req = max(req, rules.pin_clearance)
            if g < req:
                return f"{g} from {o.inst} on its {OPPOSITE[side_c]} (< {req:.0f})"
        elif gx < need and gy < need:
            return f"halo overlaps {o.inst}"
    return None


# ---------------------------------------------------------------- OpenROAD side
OBJ_TCL = ops.OBJ_TCL  # the Tcl lives with its operations (tools/openroad/ops/macros.py)


def _rules(ctx, dbu, row_h) -> Rules:
    d, p = ctx.design, ctx.params
    def pick(name, design_value):  # a parameter left unset (None) falls back to the design's value
        v = p.get(name)
        return design_value if v is None else v
    return Rules(channel=pick("channel", d.macro_channel) * dbu,
                 pin_clearance=pick("pin_clearance", d.macro_pin_clearance) * dbu,
                 analog_guard=pick("analog_guard", d.analog_guard) * dbu, hug=row_h)


def _analog_insts(ctx) -> set[str]:
    d, s = ctx.design, ctx.session
    nets = set(d.analog_nets) | set(d.analog_supplies)
    out = set()
    for m in d.macros:
        if m.analog is not None:
            if m.analog:
                out.add(m.inst)
            continue
        if nets & set(ops.macro_nets(s, m.inst)):
            out.add(m.inst)
    return out


def _master_sides(ctx, inst):
    from .chip import _master_pins
    master = ops.master_name(ctx.session, inst)
    w, h, pins = _master_pins(ctx.session, master)
    return w, h, pin_sides(pins, w, h)


def _box_of(s, inst):
    return ops.inst_box(s, inst)


def place(ctx) -> dict:  # noqa: C901
    from .chip import MACRO_HPWL, _core
    d, s, p = ctx.design, ctx.session, ctx.params
    ops.load_helpers(s, MACRO_HPWL)
    method = p.get("method", "random")
    if method not in ("random", "mpl", "cdlns"):
        raise ValueError(f"unknown macro placement method {method!r}")
    seed = p.get("seed", 0)
    rng = random.Random(seed)
    dbu = db_ops.dbu(s)
    core = tuple(_core(s))
    rx, ry, site_w, row_h = ops.first_row(s)
    rules = _rules(ctx, dbu, row_h)
    keepouts = [tuple(int(v * dbu) for v in box) for box in d.keepouts]
    analog = _analog_insts(ctx)
    aw = float(p.get("analog_weight", 4.0))
    analog_nets = list(d.analog_nets)
    sides = {m.inst: _master_sides(ctx, m.inst) for m in d.macros}

    def mbox(m, orient, x, y):
        w, h, ps = sides[m.inst]
        W, H = oriented_size(w, h, orient)
        return MacroBox(m.inst, (x, y, x + W, y + H), int(m.halo * dbu),
                        frozenset(orient_side(sd, orient, w, h) for sd in ps), m.inst in analog)

    placed: list[MacroBox] = []
    out = {}
    for m in d.macros:
        if m.location is not None:
            x, y = int(round(m.location[0] * dbu)), int(round(m.location[1] * dbu))
            ops.place_frozen(s, m.inst, m.location, m.orient)
            placed.append(mbox(m, m.orient, x, y))
            out[m.inst] = {"mode": "frozen", "loc_um": list(m.location), "orient": m.orient}
    autos = [m for m in d.macros if m.location is None]
    if method == "mpl" and autos:
        out.update(_mpl(ctx, autos, placed, keepouts, rules, analog, dbu, mbox, core))
    elif method == "cdlns" and autos:
        out.update(_cdlns(ctx, autos, placed, keepouts, rules, analog, dbu, mbox, core,
                          (rx, ry, site_w, row_h)))
    else:
        for m in autos:
            orients = p.get("orients") or m.orients or [m.orient]
            bad = [o for o in orients if o not in ALL_ORIENTS]
            if bad:
                raise ValueError(f"unknown orientation(s) {bad} for {m.inst}")
            best, tried, rejected = None, 0, {}
            want = p.get("samples", 40)
            for _ in range(want * 50):
                if tried >= want:
                    break
                w, h, _ps = sides[m.inst]
                W, H = oriented_size(w, h, orients[0])
                hl = int(m.halo * dbu)
                x = rng.randint(core[0] + hl, max(core[0] + hl, core[2] - max(w, h) - hl))
                y = rng.randint(core[1] + hl, max(core[1] + hl, core[3] - max(w, h) - hl))
                r = rng.random()  # a share of candidates hug a core edge (channel rule)
                if r < 0.15:
                    x = core[0] + hl
                elif r < 0.30:
                    y = core[1] + hl
                x = rx + -(-(x - rx) // site_w) * site_w  # up to the site grid
                y = ry + -(-(y - ry) // row_h) * row_h  # up to the row grid
                any_legal = False
                for o in orients:
                    c = mbox(m, o, x, y)
                    why = violation(c, placed, core, keepouts, rules)
                    if why:
                        key = why.split(" (")[0].split(" from ")[0][:60]
                        rejected[key] = rejected.get(key, 0) + 1
                        continue
                    any_legal = True
                    hp, ahp = ops.try_position(s, m.inst, o, x, y, analog_nets)
                    obj = hp + aw * ahp
                    if best is None or obj < best[0]:
                        best = (obj, o, x, y, c, hp, ahp)
                tried += any_legal
            if best is None:
                raise RuntimeError(f"no legal position for macro {m.inst}; rejections: {rejected}")
            _, o, x, y, c, hp, ahp = best
            ops.set_position(s, m.inst, o, x, y)
            placed.append(c)
            out[m.inst] = {"mode": "auto", "method": "random", "seed": seed, "loc_um": [x / dbu, y / dbu],
                           "orient": o, "candidates": tried, "objective_um": round(best[0], 3)}
    # analog guard: no standard cells within the guard band around analog macros
    guards = []
    min_row = row_min_width(s, ctx.pdk)
    if rules.analog_guard:
        for c in placed:
            if c.analog:
                g = _grow(c.box, c.halo + int(rules.analog_guard))
                g = (max(g[0], core[0]), max(g[1], core[1]), min(g[2], core[2]), min(g[3], core[3]))
                # no rows (taps, endcaps) under it; no row pieces too short for a well tap
                ops.guard_blockage(s, g, int(min_row * dbu))
                guards.append([v / dbu for v in g])
    if guards:
        ctx.outputs["analog_guard_blockages"] = guards
    halo = max((m.halo for m in d.macros), default=0)
    if d.macros:
        # a row piece must hold a well tap between its endcaps, or its n-well
        # floats (mcu8: 40 untapped wells beside the SRAM, found by LVS)
        ops.cut_rows(s, halo, min_row)
    ctx.metrics["macro_hpwl_um"] = ops.macro_hpwl(s, [m.inst for m in d.macros]) if d.macros else 0.0
    ctx.metrics["macro_hpwl_unplaced_terms"] = ops.macro_hpwl_skipped(s) if d.macros else 0
    ctx.metrics["analog_hpwl_um"] = ops.nets_hpwl(s, analog_nets) if analog_nets else 0.0
    ctx.metrics["analog_macros"] = sorted(analog)
    # every macro, frozen or not, checked against the rules (frozen ones are reported, not refused)
    final = []
    for c in placed:
        others = [o for o in placed if o is not c]
        final.append((c.inst, violation(c, others, core, keepouts, rules)))
    ctx.outputs["macro_rule_check"] = {i: (w or "ok") for i, w in final}
    if p.get("evaluate", True) and d.macros:
        _evaluate(ctx, aw)
    return out


def _mpl(ctx, autos, placed, keepouts, rules, analog, dbu, mbox, core) -> dict:
    s, d, p = ctx.session, ctx.design, ctx.params
    ops.mpl_keepouts(s, keepouts)
    for m in d.macros:
        h = m.halo * dbu + (rules.analog_guard / 2 if m.inst in analog else 0)
        h = max(h, rules.channel / 2, rules.pin_clearance / 2) / dbu
        ops.macro_halo(s, m.inst, h)
    # rtl_macro_placer also places the standard cells; they are unplaced again
    # afterwards so every method hands the same kind of checkpoint on
    ops.mpl_mark_free(s)
    rep = ctx.path("mpl")
    os.makedirs(rep, exist_ok=True)
    util = p.get("target_util", 0.25)
    try:
        ops.rtl_macro_placer(s, rep, util)
    finally:
        ops.mpl_end(s)
    out, problems = {}, []
    for m in autos:
        o, st = ops.orient_and_status(s, m.inst)
        x0, y0, _, _ = _box_of(s, m.inst)
        if st == "NONE":
            raise RuntimeError(f"rtl_macro_placer left {m.inst} unplaced")
        ops.set_firm(s, m.inst)
        c = mbox(m, o, x0, y0)
        why = violation(c, placed, core, keepouts, rules)
        if why:
            problems.append(f"{m.inst}: {why}")
        placed.append(c)
        out[m.inst] = {"mode": "auto", "method": "mpl", "loc_um": [x0 / dbu, y0 / dbu], "orient": o}
    if problems:
        raise RuntimeError("rtl_macro_placer result breaks the macro rules:\n  " + "\n  ".join(problems))
    return out


CDLNS_EXPORT_TCL = ops.CDLNS_EXPORT_TCL  # the Tcl lives with its operations (tools/openroad/ops/macros.py)


def placer_python(ctx) -> str:
    """The Python with PyTorch the cdlns method runs in (config placer_python,
    else ~/.cache/sipath/placer-venv); an error saying how to make one."""
    cfg = getattr(ctx, "config", None)
    py = (getattr(cfg, "placer_python", "") or "").strip() or \
        os.path.expanduser("~/.cache/sipath/placer-venv/bin/python")
    if not os.path.isfile(py):
        raise RuntimeError(f"macros method cdlns needs a Python with PyTorch ({py} not found): run "
                           "`python3 -m sipath setup-placer`, or set placer_python")
    return py


def legal_start(boxes: list, cw: float, ch: float, gap: float) -> int:
    """Moves the movable boxes (dicts with centre x, y, w, h, fixed; canvas
    µm), largest first, to the nearest spot inside the canvas that keeps
    ``gap`` from every box already placed (the fixed ones first): the spot
    itself, else a candidate from the canvas edges and the positions abutting
    a placed box at ``gap``. When that fragments the canvas, the boxes start
    again from a shelf packing in their row and column order. Returns the
    number moved; ValueError when a box has no legal spot."""
    want = {id(b): (b["x"], b["y"]) for b in boxes}
    try:
        return _greedy_legal(boxes, cw, ch, gap)
    except ValueError:
        movable = [b for b in boxes if not b["fixed"]]
        for b, (x, y) in zip(movable, _shelves(movable, cw, gap + 2e-3)):
            b["x"], b["y"] = x, y
        _greedy_legal(boxes, cw, ch, gap)
        return sum((b["x"], b["y"]) != want[id(b)] for b in movable)


def _shelves(boxes: list, cw: float, gap: float) -> list:
    """Centres packing ``boxes`` bottom-up into rows ``gap`` apart, keeping
    their order by y between rows and by x within a row."""
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i]["y"], boxes[i]["x"]))
    rows, row, width = [], [], 0.0
    for i in order:
        w = boxes[i]["w"] + (gap if row else 0.0)
        if row and width + w > cw:
            rows.append(row)
            row, width, w = [], 0.0, boxes[i]["w"]
        row.append(i)
        width += w
    rows.append(row)
    out, y = [None] * len(boxes), 0.0
    for row in rows:
        h, x = max(boxes[i]["h"] for i in row), 0.0
        for i in sorted(row, key=lambda i: boxes[i]["x"]):
            out[i] = (x + boxes[i]["w"] / 2, y + boxes[i]["h"] / 2)
            x += boxes[i]["w"] + gap
        y += h + gap
    return out


def _greedy_legal(boxes: list, cw: float, ch: float, gap: float) -> int:
    g = gap + 1e-3  # strictly clear of the placer's own (dx < size + gap) test
    placed, moved = [b for b in boxes if b["fixed"]], 0

    def clear(x, y, w, h):
        return all(abs(x - p["x"]) >= (w + p["w"]) / 2 + gap or abs(y - p["y"]) >= (h + p["h"]) / 2 + gap
                   for p in placed)

    todo = sorted((b for b in boxes if not b["fixed"]), key=lambda b: -b["w"] * b["h"])
    spots = {}
    for b in todo:
        w, h = b["w"], b["h"]
        lo_x, hi_x, lo_y, hi_y = w / 2, cw - w / 2, h / 2, ch - h / 2
        if hi_x < lo_x or hi_y < lo_y:
            raise ValueError(f"{b['name']} ({w:g} x {h:g} µm) does not fit the canvas ({cw:g} x {ch:g} µm)")
        x0, y0 = min(max(b["x"], lo_x), hi_x), min(max(b["y"], lo_y), hi_y)
        if not clear(x0, y0, w, h):
            xs = {x0, lo_x, hi_x} | {p["x"] + s * ((w + p["w"]) / 2 + g) for p in placed for s in (-1, 1)}
            ys = {y0, lo_y, hi_y} | {p["y"] + s * ((h + p["h"]) / 2 + g) for p in placed for s in (-1, 1)}
            xs = [x for x in xs if lo_x <= x <= hi_x]
            ys = [y for y in ys if lo_y <= y <= hi_y]
            spot = next(((x, y) for _, x, y in sorted(((x - x0) ** 2 + (y - y0) ** 2, x, y) for x in xs for y in ys)
                         if clear(x, y, w, h)), None)
            if spot is None:
                raise ValueError(f"no legal start for {b['name']}: the macros do not fit the canvas "
                                 f"({cw:g} x {ch:g} µm) {gap:g} µm apart")
            x0, y0 = spot
            moved += 1
        spots[id(b)] = (x0, y0)
        placed.append({**b, "x": x0, "y": y0})
    for b in todo:  # the boxes change only when every one has a spot
        b["x"], b["y"] = spots[id(b)]
    return moved


def cdlns_spec(text: str, dbu: int, core, edge: int, autos: dict, keepouts, seed: int, seconds: float,
               gap_um: float, name: str) -> tuple[dict, list]:
    """The CD-LNS placer's input from a floorplan dump (CDLNS_EXPORT_TCL):
    canvas = the core less ``edge``; hard macros (movable: ``autos``, a name ->
    True map, the rest fixed) with their pin offsets; standard cells grouped into
    fixed soft macros, one per non-empty tile of a G x G grid; keepouts as fixed
    blocks; IO pins as ports; nets as [owner, slot] lists. Returns (spec, hard
    macro names in order)."""
    ox, oy = core[0] + edge, core[1] + edge
    cw, ch = (core[2] - core[0] - 2 * edge) / dbu, (core[3] - core[1] - 2 * edge) / dbu
    um = lambda v, o: (float(v) - o) / dbu  # noqa: E731
    macros, cells, ports, nets, net = {}, {}, {}, [], None
    for ln in text.splitlines():
        t = [w.replace("\\[", "[").replace("\\]", "]") for w in ln.split()]  # OpenDB escapes [ ] in names
        if not t:
            continue
        if t[0] == "M":
            macros[t[1]] = tuple(int(float(v)) for v in t[2:6])
        elif t[0] == "C":
            cells[t[1]] = (float(t[2]), float(t[3]), float(t[4]))
        elif t[0] == "P":
            ports[t[1]] = (um(t[2], ox), um(t[3], oy))
        elif t[0] == "N":
            net = []
            nets.append(net)
        elif t[0] in ("I", "B") and net is not None:
            net.append(t[1:])
    hard = sorted(macros, key=lambda n: (not autos.get(n, False), n))
    pins = {n: {} for n in hard}
    out_m = []
    for n in hard:
        x0, y0, x1, y1 = macros[n]
        out_m.append({"name": n, "x": um((x0 + x1) / 2, ox), "y": um((y0 + y1) / 2, oy), "w": (x1 - x0) / dbu,
                      "h": (y1 - y0) / dbu, "fixed": not autos.get(n, False)})
    for k, b in enumerate(keepouts):
        out_m.append({"name": f"__keepout{k}", "x": um((b[0] + b[2]) / 2, ox), "y": um((b[1] + b[3]) / 2, oy),
                      "w": (b[2] - b[0]) / dbu, "h": (b[3] - b[1]) / dbu, "fixed": True})
        hard.append(f"__keepout{k}")
        pins[hard[-1]] = {}
    nh = len(out_m)
    legal_start(out_m, cw, ch, gap_um)  # the placer improves a legal placement; it cannot repair one
    g = max(4, min(48, int(round((len(cells) / 30) ** 0.5)) or 4))
    tw, th = (core[2] - core[0]) / g, (core[3] - core[1]) / g
    tiles = {}
    tile_of = {}
    for c, (x, y, a) in cells.items():
        key = (min(g - 1, max(0, int((x - core[0]) / tw))), min(g - 1, max(0, int((y - core[1]) / th))))
        s = tiles.setdefault(key, [0.0, 0.0, 0.0])
        s[0] += x * a
        s[1] += y * a
        s[2] += a
        tile_of[c] = key
    soft_ix = {}
    for key, (sx, sy, a) in sorted(tiles.items()):
        side = min((a ** 0.5) / dbu, tw / dbu, th / dbu)
        soft_ix[key] = len(out_m)
        out_m.append({"name": f"__cluster_{key[0]}_{key[1]}", "x": um(sx / a, ox), "y": um(sy / a, oy),
                      "w": max(side, 0.01), "h": max(side, 0.01), "fixed": False})
    n = len(out_m)
    port_ix = {p: n + k for k, p in enumerate(ports)}
    idx = {name: k for k, name in enumerate(hard)}
    offsets = [[] for _ in range(nh)]
    out_nets = []
    for entries in nets:
        e, seen_soft = [], set()
        for t in entries:
            nm = t[0]
            if nm in idx and len(t) >= 3:
                k = idx[nm]
                x0, y0, x1, y1 = macros[nm]
                key = t[3] if len(t) > 3 else f"{t[1]},{t[2]}"
                if key not in pins[nm]:
                    pins[nm][key] = len(offsets[k])
                    offsets[k].append([(float(t[1]) - (x0 + x1) / 2) / dbu, (float(t[2]) - (y0 + y1) / 2) / dbu])
                e.append([k, pins[nm][key]])
            elif nm in tile_of:
                s = soft_ix[tile_of[nm]]
                if s not in seen_soft:
                    seen_soft.add(s)
                    e.append([s, 0])
            elif nm in port_ix:
                e.append([port_ix[nm], 0])
        if len(e) >= 2:
            out_nets.append(e)
    spec = {"name": name, "canvas": [cw, ch], "num_hard": nh, "macros": out_m,
            "ports": [list(ports[p]) for p in ports], "pin_offsets": offsets, "nets": out_nets,
            "grid": [g, g], "seed": seed, "seconds": seconds, "gap": gap_um}
    return spec, hard[:nh]


def _cdlns(ctx, autos, placed, keepouts, rules, analog, dbu, mbox, core, grid) -> dict:
    """Macro placement by Team Macro Polo's CD-LNS placer (sipath/third_party):
    a quick mixed-size global placement gives initial macro and cell positions,
    the cells are grouped into fixed soft macros, the placer moves the macros
    (in the placer Python, ``placer_seconds``), and the result is snapped to the
    site and row grid and checked against the macro rules. The cells are left
    unplaced again, like every method."""
    import json
    import subprocess
    s, d, p = ctx.session, ctx.design, ctx.params
    rx, ry, site_w, row_h = grid
    py = placer_python(ctx)
    for m in autos:  # a fixed orientation: the placer flips nothing
        ops.set_orient(s, m.inst, m.orient)
    ops.cdlns_mark_free(s)
    dump = ctx.path("cdlns_floorplan.txt")
    try:
        ops.quick_global_place(s, p.get('quick_density', 0.6))
        ops.cdlns_export(s, dump)
    finally:
        ops.cdlns_end(s)
    halo = max([m.halo for m in d.macros] + [0.0])
    edge = int(max(halo * dbu, rules.pin_clearance))  # the canvas keeps halos and pin clearance off the core edge
    # spacing the rule check asks of two facing macros (violation), plus room for
    # snapping both to the site and row grid
    gap_um = max(2 * halo + ((rules.analog_guard / dbu) if analog else 0.0), rules.channel / dbu,
                 rules.pin_clearance / dbu) + 2 * max(site_w, row_h) / dbu
    with open(dump) as f:
        spec, hard = cdlns_spec(f.read(), dbu, core, edge, {m.inst: True for m in autos}, keepouts,
                                int(p.get("seed", 0)) or 42, float(p.get("placer_seconds", 120)), gap_um, d.name)
    spec["budget"] = p.get("placer_budget", "work")
    spec_path, out_path = ctx.path("cdlns_spec.json"), ctx.path("cdlns_out.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f)
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "third_party", "run_cdlns.py")
    from .tools import process
    limit = float(p.get("placer_seconds", 120)) * 6 + 600
    r = process.run([py, runner, spec_path, out_path], ctx.path("cdlns.log"), timeout=limit,
                    tracker=getattr(ctx, "processes", None))
    if r.timed_out:
        raise RuntimeError(f"cdlns placer ran past {limit:.0f} s and was stopped (log {ctx.path('cdlns.log')})")
    if r.rc != 0 or not os.path.isfile(out_path):
        tail = open(ctx.path("cdlns.log"), errors="replace").read().strip().splitlines()[-8:]
        raise RuntimeError(f"cdlns placer failed (exit {r.rc}, log {ctx.path('cdlns.log')}):\n" +
                           "\n".join(tail))
    with open(out_path) as f:
        pos = json.load(f)["positions"]
    ctx.outputs["cdlns"] = {"spec": spec_path, "macros": len(hard), "soft_macros": len(spec["macros"]) - spec["num_hard"],
                            "nets": len(spec["nets"]), "gap_um": round(gap_um, 3), "seconds": spec["seconds"],
                            "budget": spec["budget"]}
    by = {m.inst: m for m in autos}
    missing = sorted(set(by) - set(hard[:len(pos)]))
    if missing:
        raise RuntimeError(f"cdlns placer returned no position for {', '.join(missing)}")
    out, problems = {}, []
    for (cx, cy), name in zip(pos, hard):
        m = by.get(name)
        if m is None:
            continue
        x0, y0, x1, y1 = _box_of(s, name)
        w, h = x1 - x0, y1 - y0
        x = core[0] + edge + int(round(cx * dbu)) - w // 2
        y = core[1] + edge + int(round(cy * dbu)) - h // 2
        x = rx + int(round((x - rx) / site_w)) * site_w  # to the site and row grid
        y = ry + int(round((y - ry) / row_h)) * row_h
        x, y, why, moved = _nearest_legal(m, x, y, w, h, placed, core, keepouts, rules, mbox, (rx, ry, site_w, row_h))
        c = mbox(m, m.orient, x, y)
        if why:
            problems.append(f"{name}: {why}")
        ops.set_position(s, name, m.orient, x, y)
        placed.append(c)
        out[name] = {"mode": "auto", "method": "cdlns", "loc_um": [x / dbu, y / dbu], "orient": m.orient,
                     "repair_um": round(moved / dbu, 3)}
    if problems:
        raise RuntimeError("cdlns result breaks the macro rules:\n  " + "\n  ".join(problems))
    return out


def _nearest_legal(m, x, y, w, h, placed, core, keepouts, rules, mbox, grid, reach: int = 40):
    """``(x, y)`` if the macro is legal there, else the nearest legal spot among
    its core-edge snaps (a macro hugs an edge or leaves a full channel) and
    site/row offsets up to ``reach`` steps. Returns (x, y, rule broken or None,
    distance moved)."""
    rx, ry, site_w, row_h = grid
    hl = mbox(m, m.orient, x, y).halo
    cands = [(x, y)]
    snaps_x = [core[0] + hl, core[2] - hl - w]
    snaps_y = [core[1] + hl, core[3] - hl - h]
    snaps_x = [rx + -(-(v - rx) // site_w) * site_w if v == snaps_x[0] else rx + ((v - rx) // site_w) * site_w
               for v in snaps_x]
    snaps_y = [ry + -(-(v - ry) // row_h) * row_h if v == snaps_y[0] else ry + ((v - ry) // row_h) * row_h
               for v in snaps_y]
    cands += [(sx, y) for sx in snaps_x] + [(x, sy) for sy in snaps_y] + [(sx, sy) for sx in snaps_x for sy in snaps_y]
    step = max(site_w, row_h)
    for k in range(1, reach + 1):
        for dx, dy in ((k, 0), (-k, 0), (0, k), (0, -k), (k, k), (-k, -k), (k, -k), (-k, k)):
            cands.append((x + rx * 0 + dx * step - (dx * step) % site_w, y + dy * step - (dy * step) % row_h))
    first = None
    for cx, cy in sorted(set(cands), key=lambda c: abs(c[0] - x) + abs(c[1] - y)):
        why = violation(mbox(m, m.orient, cx, cy), placed, core, keepouts, rules)
        if why is None:
            return cx, cy, None, abs(cx - x) + abs(cy - y)
        if first is None:
            first = why
    return x, y, first, 0


def _evaluate(ctx, analog_weight):
    s, d = ctx.session, ctx.design
    route_ops.set_routing_layers(s, d, ctx.pdk)
    rpt = ctx.path("quick_congestion.rpt")
    n, hp = ops.quick_eval(s, ctx.params.get('quick_density', 0.6), rpt)
    cong = congestion.parse(rpt)
    ctx.metrics["quick_cells"] = int(n)
    ctx.metrics["quick_hpwl_um"] = float(hp)
    ctx.metrics["quick_overflow"] = cong["overflow"]
    ctx.metrics["quick_congested_gcells"] = cong["gcells"]
    ctx.metrics["macro_cost_um"] = float(hp) + analog_weight * ctx.metrics["analog_hpwl_um"]
    if cong["gcells"]:
        ctx.outputs.setdefault("reports", {})["quick_congestion"] = rpt


def row_min_width(session, pdk) -> float:
    """Shortest row piece (um) that is sure to get a well tap: taps go every
    2 x tap_distance along a row, plus a tap and the two endcaps. 0 without
    tap cells."""
    if not pdk.tap_cell:
        return 0.0
    def width(m):
        return ops.master_width_um(session, m)
    w = width(pdk.tap_cell) + (2 * width(pdk.endcap_cell) if pdk.endcap_cell else 0.0)
    return round(2 * pdk.tap_distance + w, 3)
