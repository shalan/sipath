"""Structured datapath placement.

``column``: cells stacked one per row, left edges aligned (a ripple adder
bit-slice column). ``array``: each physical row abutted left to right, rows
stacked (an array multiplier). ``slices``: generic bit-slice structure, one
row per bit, stages in aligned columns (``pack_slices``). ``pack``: a group of cells shelf-packed into
a near-square block.

Packing is computed in Python from rows and master widths queried from the
live database, then applied as FIRM placements. ``reabut`` repeats the
packing with the *current* masters, so a cell that grew during sizing pushes
its row neighbours instead of overlapping them; fixed physical cells (taps,
endcaps) caught under the new extent are moved just past it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .tcl import place_instance, q, qlist


@dataclass
class Row:
    name: str
    x: int
    y: int
    orient: str
    site_w: int
    nsites: int
    height: int

    @property
    def x_end(self):
        return self.x + self.site_w * self.nsites


@dataclass
class Placement:
    inst: str
    x: int
    y: int
    orient: str
    w: int
    h: int
    tap: bool = False  # a tap slot: no cell of the structure, a well tap goes here

    def box(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)


ROWS_TCL = """
set out {}
foreach row [[ord::get_db_block] getRows] {
  set bb [$row getBBox]
  lappend out [list [$row getName] [lindex [$row getOrigin] 0] [lindex [$row getOrigin] 1] [$row getOrient] [$row getSpacing] [$row getSiteCount] [$bb dy]]
}
set out
"""


def query_rows(session) -> list[Row]:
    rows = []
    for item in _tcl_list(session.value(ROWS_TCL)):
        n, x, y, o, sw, ns, h = item
        rows.append(Row(n, int(x), int(y), o, int(sw), int(ns), int(h)))
    rows.sort(key=lambda r: (r.y, r.x))
    return rows


def query_sizes(session, insts: list[str]) -> dict[str, tuple[int, int]]:
    tcl = f"""
set out {{}}
foreach n {qlist(insts)} {{ set m [[::flow_inst $n] getMaster]; lappend out $n [$m getWidth] [$m getHeight] }}
set out
"""
    flat = _tcl_flat(session.value(tcl))
    return {flat[i]: (int(flat[i + 1]), int(flat[i + 2])) for i in range(0, len(flat), 3)}


def dbu_per_micron(session) -> int:
    return int(session.value("[ord::get_db_block] getDbUnitsPerMicron"))


def _levels(rows: list[Row]) -> list[int]:
    return sorted({r.y for r in rows})


def _level_at(rows: list[Row], y: int) -> int:
    levels = _levels(rows)
    for i, ly in enumerate(levels):
        if ly >= y:
            return i
    raise ValueError(f"no row at or above y={y}")


def _segment(rows: list[Row], level: int, x: int, w: int) -> Row:
    """Row segment at a row level that holds [x, x+w] (rows are cut by macros)."""
    levels = _levels(rows)
    if level >= len(levels):
        raise ValueError("structure runs out of rows")
    y = levels[level]
    for r in rows:
        if r.y == y and r.x <= x and x + w <= r.x_end:
            return r
    raise ValueError(f"no row segment at y={y} holds x={x}..{x + w}")


def _snap_x(row: Row, x: int, mode: str = "nearest") -> int:
    """x on the row's site grid: nearest site, or ``floor`` / ``ceil`` when the
    result must stay left / right of x (e.g. beside an obstacle)."""
    import math
    f = (x - row.x) / row.site_w
    k = {"nearest": round, "floor": math.floor, "ceil": math.ceil}[mode](f)
    return row.x + max(0, k) * row.site_w


def _with_slots(widths: list[int], slot) -> tuple[list[int], list[int], int]:
    """Offsets of a row of items (``widths``) with tap slots: one at the left
    end and another before any item that would end more than ``pitch`` past
    the last slot, so no point of the row is farther than ``pitch`` from a tap
    on its left. ``slot``: (slot width, pitch) or None. Returns (item offsets,
    slot offsets, total width)."""
    offs, soffs, x = [], [], 0
    if not slot:
        for w in widths:
            offs.append(x)
            x += w
        return offs, soffs, x
    sw, pitch = slot
    run = None
    for w in widths:
        if run is None or run + w > pitch:
            soffs.append(x)
            x += sw
            run = 0
        offs.append(x)
        x += w
        run += w
    return offs, soffs, x


def _slots(row: Row, x0: int, soffs: list[int], slot) -> list[Placement]:
    return [Placement("", x0 + s, row.y, row.orient, slot[0], row.height, tap=True) for s in soffs]


def pack_column(cells, origin, rows, sizes) -> list[Placement]:
    l0 = _level_at(rows, origin[1])
    out = []
    for k, c in enumerate(cells):
        w, h = sizes[c]
        row = _segment(rows, l0 + k, origin[0], w)
        out.append(Placement(c, _snap_x(row, origin[0]), row.y, row.orient, w, h))
    return out


def pack_array(cell_rows, origin, rows, sizes, slot=None) -> list[Placement]:
    """Each row abutted left to right (with tap slots, ``slot``), rows stacked."""
    l0 = _level_at(rows, origin[1])
    out = []
    for k, cells in enumerate(cell_rows):
        offs, soffs, total = _with_slots([sizes[c][0] for c in cells], slot)
        row = _segment(rows, l0 + k, origin[0], total)
        x = _snap_x(row, origin[0])
        for c, off in zip(cells, offs):
            w, h = sizes[c]
            out.append(Placement(c, x + off, row.y, row.orient, w, h))  # abut
        out += _slots(row, x, soffs, slot)
    return out


def pack_slices(slices, origin, rows, sizes, stage_gap: int = 0, rows_per_slice: int = 1,
                slot=None) -> list[Placement]:
    """Bit-slice structure: ``slices[bit][stage]`` (None = no cell). One row
    level per bit (every ``rows_per_slice`` levels); stage k is a column as wide
    as its widest cell, ``stage_gap`` sites between columns, so a stage starts
    at the same x in every bit. Tap slots (``slot``) are whole columns, so the
    alignment holds."""
    if rows_per_slice < 1:
        raise ValueError("rows_per_slice must be >= 1")
    nst = max((len(s) for s in slices), default=0)
    widths = [max((sizes[s[k]][0] for s in slices if k < len(s) and s[k] is not None), default=0)
              for k in range(nst)]
    l0 = _level_at(rows, origin[1])
    site_w = rows[0].site_w
    cols = [w + (stage_gap * site_w if k < nst - 1 else 0) for k, w in enumerate(widths)]
    offs, soffs, total = _with_slots(cols, slot)
    out = []
    for b, cells in enumerate(slices):
        row = _segment(rows, l0 + b * rows_per_slice, origin[0], total)
        x = _snap_x(row, origin[0])
        for k in range(nst):
            c = cells[k] if k < len(cells) else None
            if c is not None:
                w, h = sizes[c]
                out.append(Placement(c, x + offs[k], row.y, row.orient, w, h))
        out += _slots(row, x, soffs, slot)
    return out


def _fit_span(rows, l0, nlevels, x_left, width, avoid=()) -> int:
    """Nearest x to ``x_left`` where [x, x+width] fits a row segment on every
    level l0..l0+nlevels-1 and the block misses every ``avoid`` box."""
    levels = _levels(rows)
    y0 = levels[l0]
    y1 = levels[min(l0 + nlevels, len(levels)) - 1] + rows[0].height
    cands = []
    for r in rows:
        if r.y == levels[l0] and r.x_end - r.x >= width:
            wants = [(x_left, "nearest")] + [(b[0] - width, "floor") for b in avoid] + [(b[2], "ceil") for b in avoid]
            for want, mode in wants:
                lim = r.x_end - width
                x = _snap_x(r, min(max(want, r.x), lim), mode)
                if x > lim:  # rounding up past the row end
                    x = _snap_x(r, lim, "floor")
                cands.append((abs(x - x_left), x))
    tried = []
    for _, x in sorted(set(cands)):
        if any(x < b[2] and x + width > b[0] and y0 < b[3] and y1 > b[1] for b in avoid):
            tried.append(f"x={x}: overlaps an avoided box")
            continue
        try:
            for k in range(nlevels):
                _segment(rows, l0 + k, x, width)
            return x
        except ValueError as e:
            tried.append(f"x={x}: {e}")
            continue
    raise ValueError(f"no row segments hold a {width}-wide, {nlevels}-row block near x={x_left} "
                     f"(rows {l0}..{l0 + nlevels - 1} of {len(levels)}, y {y0}..{y1}); tried: {tried[:6]}")


def pack_block(cells, center, rows, sizes, utilization=0.7, avoid=()) -> list[Placement]:
    """Shelf-pack ``cells`` into a near-square block centred on ``center``."""
    area = sum(sizes[c][0] * sizes[c][1] for c in cells) / max(utilization, 0.05)
    height = rows[0].height
    nrows = max(1, round((area ** 0.5) / height))
    width = int(area / (nrows * height)) + max(w for w, _ in sizes.values())
    levels = _levels(rows)
    l0 = max(0, min(len(levels) - nrows - 1, _level_at(rows, int(center[1] - nrows * height / 2))))
    x_left = _fit_span(rows, l0, nrows + 1, int(center[0] - width / 2), width, avoid)
    out, k, x = [], 0, None
    for c in sorted(cells, key=lambda c: -sizes[c][0]):
        w, h = sizes[c]
        row = _segment(rows, l0 + k, x_left, width)
        x0 = _snap_x(row, x_left)
        if x is None:
            x = x0
        if x + w - x0 > width:
            k += 1
            if k >= nrows:
                nrows += 1
            row = _segment(rows, l0 + k, x_left, width)
            x = _snap_x(row, x_left)
        out.append(Placement(c, x, row.y, row.orient, w, h))
        gap = int((1 / utilization - 1) * w)
        x = _snap_x(row, x + w + gap)
    return out


def check_internal(placements: list[Placement]):
    """No two placements of a structure may overlap."""
    by_row: dict[int, list[Placement]] = {}
    for p in placements:
        by_row.setdefault(p.y, []).append(p)
    for row in by_row.values():
        row.sort(key=lambda p: p.x)
        for a, b in zip(row, row[1:]):
            if a.x + a.w > b.x:
                raise ValueError(f"overlap {a.inst} / {b.inst}")


def apply(session, placements: list[Placement], status="FIRM"):
    check_internal(placements)
    session.run("\n".join(place_instance(p.inst, p.x, p.y, p.orient, status) for p in placements if not p.tap))


def placements_for(dp, session, tap=None) -> list[Placement]:
    """The structure's placements; ``tap``: (well-tap master, tap distance in
    um) reserves tap slots in rows of several cells (array, slices): abutted
    rows leave no room for the tap grid, and a cell farther than the PDK's tap
    distance from a tap breaks the latch-up rules."""
    rows = query_rows(session)
    dbu = dbu_per_micron(session)
    origin = (int(dp.origin[0] * dbu), int(dp.origin[1] * dbu))
    cells = dp.cells if dp.kind == "column" else [c for r in dp.cells for c in r if c is not None]
    sizes = query_sizes(session, cells)
    slot = None
    if tap and tap[0] and tap[1]:
        slot = (int(session.value(f"[[ord::get_db] findMaster {q(tap[0])}] getWidth")), int(tap[1] * dbu))
    if dp.kind == "column":
        return pack_column(dp.cells, origin, rows, sizes)
    if dp.kind == "array":
        return pack_array(dp.cells, origin, rows, sizes, slot)
    if dp.kind == "slices":
        return pack_slices(dp.cells, origin, rows, sizes, getattr(dp, "stage_gap", 0), getattr(dp, "rows_per_slice", 1),
                           slot)
    raise ValueError(f"unknown datapath kind {dp.kind}")


def check_structure(session, dp, tap=None) -> list[str]:
    """Differences between where the structure's cells are and where packing
    with their current masters puts them, and tap slots without a well tap
    (empty list = intact)."""
    want = placements_for(dp, session, tap)
    cells = [p for p in want if not p.tap]
    flat = _tcl_flat(session.value(
        f"set out {{}}; foreach n {qlist([p.inst for p in cells])} {{ set i [::flow_inst $n]; set l [$i getLocation]; "
        f"lappend out $n [lindex $l 0] [lindex $l 1] [$i getOrient] }}; set out"))
    have = {flat[i]: (int(flat[i + 1]), int(flat[i + 2]), flat[i + 3]) for i in range(0, len(flat), 4)}
    out = []
    for p in cells:
        if have[p.inst] != (p.x, p.y, p.orient):
            out.append(f"{p.inst} at {have[p.inst]}, packing wants {(p.x, p.y, p.orient)}")
    taps = {(int(a), int(b), int(c), int(d)) for _n, a, b, c, d, ty in _tcl_list(session.value(FIXED_PHYS_TCL))
            if ty == "CORE_WELLTAP"}
    for p in want:
        if p.tap and p.box() not in taps:
            out.append(f"tap slot at {(p.x, p.y)} holds no well tap")
    try:
        check_internal(want)
    except ValueError as e:
        out.append(str(e))
    return out


def place_datapath(session, dp, tap=None) -> list[Placement]:
    """Place the structure's cells (FIRM); its tap slots are filled by
    ``reabut`` once the tap grid exists."""
    p = placements_for(dp, session, tap)
    apply(session, p)
    return p


FIXED_PHYS_TCL = """
set out {}
foreach inst [[ord::get_db_block] getInsts] {
  set t [[$inst getMaster] getType]
  if {$t in {CORE_WELLTAP ENDCAP ENDCAP_PRE ENDCAP_POST CORE_ENDCAP} || [string match ENDCAP* $t]} {
    set bb [$inst getBBox]
    lappend out [list [$inst getName] [$bb xMin] [$bb yMin] [$bb xMax] [$bb yMax] $t]
  }
}
set out
"""


def fill_tap_slots(session, dp, placements: list[Placement], master: str) -> dict:
    """A well tap in every tap slot: one already there, else a tap the
    structure covers in that row (moved in), else a new one."""
    slots = [p for p in placements if p.tap]
    if not slots:
        return {"slot_taps": 0, "taps_created": 0}
    phys = [(n, int(a), int(b), int(c), int(d)) for n, a, b, c, d, ty in _tcl_list(session.value(FIXED_PHYS_TCL))
            if ty == "CORE_WELLTAP"]
    at = {(x0, y0, x1, y1): n for n, x0, y0, x1, y1 in phys}
    used = {at[p.box()] for p in slots if p.box() in at}
    extent = {}
    for p in placements:
        lo, hi = extent.get(p.y, (p.x, p.x + p.w))
        extent[p.y] = (min(lo, p.x), max(hi, p.x + p.w))
    script, created = [], 0
    for k, p in enumerate(slots):
        if p.box() in at:
            continue
        lo, hi = extent[p.y]
        spare = [n for n, x0, y0, x1, _ in phys if y0 == p.y and x0 < hi and x1 > lo and n not in used]
        if spare:
            name = spare[0]
        else:
            name = f"DPTAP_{dp.name}_{p.x}_{p.y}"
            script.append(f"odb::dbInst_create [ord::get_db_block] [[ord::get_db] findMaster {q(master)}] {q(name)}")
            created += 1
        used.add(name)
        script.append(place_instance(name, p.x, p.y, p.orient, "FIRM"))
    if script:
        session.run("\n".join(script))
    return {"slot_taps": len(slots), "taps_created": created}


def reabut(session, dp, tap=None) -> dict:
    """Re-pack ``dp`` with current masters; fill its tap slots; move other
    taps out of the new extent.

    Returns {"moved": n cells whose x changed, "taps_moved": n, "slot_taps",
    "taps_created"}.
    """
    new = placements_for(dp, session, tap)
    cur = {}
    flat = _tcl_flat(session.value(
        f"set out {{}}; foreach n {qlist([p.inst for p in new if not p.tap])} {{ set i [::flow_inst $n]; "
        f"lappend out $n [lindex [$i getLocation] 0] }}; set out"))
    for i in range(0, len(flat), 2):
        cur[flat[i]] = int(flat[i + 1])
    moved = sum(1 for p in new if not p.tap and cur[p.inst] != p.x)
    apply(session, new)
    slots = fill_tap_slots(session, dp, new, tap[0]) if tap else {}
    slot_boxes = {p.box() for p in new if p.tap}
    rows = query_rows(session)
    row_by_y = {r.y: r for r in rows}
    phys = [(n, int(a), int(b), int(c), int(d)) for n, a, b, c, d, _t in _tcl_list(session.value(FIXED_PHYS_TCL))]
    taps_moved = 0
    script = []
    names = {p.inst for p in new}
    occupied = {}  # y -> list of (x0,x1)
    for p in new:
        occupied.setdefault(p.y, []).append((p.x, p.x + p.w))
    for n, x0, y0, x1, y1 in phys:
        if n in names or (x0, y0, x1, y1) in slot_boxes:  # a slot's own tap stays
            continue
        for a0, a1 in occupied.get(y0, []):
            if x0 < a1 and x1 > a0:
                row = row_by_y.get(y0)
                nx = a1
                width = x1 - x0
                # first free site past every structure cell in this row
                while any(nx < b1 and nx + width > b0 for b0, b1 in occupied[y0]):
                    nx = max(b1 for b0, b1 in occupied[y0] if nx < b1 and nx + width > b0)
                others = [(m, bx0, bx1) for m, bx0, by0, bx1, _ in phys if by0 == y0 and m != n]
                if any(nx < bx1 and nx + width > bx0 for m, bx0, bx1 in others):
                    raise RuntimeError(f"re-abut of {dp.name}: no room to move {n}")
                if row is not None and nx + width > row.x_end:
                    raise RuntimeError(f"re-abut of {dp.name}: {n} would leave row")
                # keep the tap's own status (OpenDB has no FIXED; DEF FIXED is LOCKED)
                script.append(f"set i [::flow_inst {q(n)}]; set st [$i getPlacementStatus]; "
                              f"$i setPlacementStatus PLACED; $i setLocation {nx} {y0}; $i setPlacementStatus $st")
                taps_moved += 1
                break
    if script:
        session.run("\n".join(script))
    return {"moved": moved, "taps_moved": taps_moved, **slots}


# -- Tcl list helpers (results of `set out` are Tcl lists) ----------------------
def _tcl_flat(s: str) -> list[str]:
    from .tclquote import tcl_words
    return tcl_words(s)


def _tcl_list(s: str) -> list[list[str]]:
    from .tclquote import tcl_parse
    return [x if isinstance(x, list) else [x] for x in tcl_parse(s)]
