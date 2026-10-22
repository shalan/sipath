"""Regular-wire normalisation for routing in a fresh process.

The detailed router of this OpenROAD build mis-reads its own wires when a
path continues after a via (``M1 (x y) VIA (x2 *) (* y2)`` is read as one
non-orthogonal segment, error DRT-1010). Re-routing from a checkpoint (ECO)
needs to read every existing wire, so before that each routed net is
re-encoded with ``dbWireEncoder``: same points, vias, patches and layer
rules, but a new path starts at every via landing point. Geometry is
unchanged.
"""
from __future__ import annotations

from .tools.openroad.tcl import q


def parse_def_paths(def_text: str) -> dict[str, dict]:
    """Return {net: {"ndr": name|None, "paths": [path]}} for routed nets.

    A path is {"layer", "taper", "items"} with items ("pt", x, y, ext|None),
    ("via", name), ("rect", dx1, dy1, dx2, dy2), ("virtual", x, y).
    """
    body = def_text[def_text.index("\nNETS "):def_text.index("\nEND NETS")]
    out = {}
    for stmt in body.split(";")[1:]:
        toks = stmt.split()
        if len(toks) < 2 or toks[0] != "-" or "ROUTED" not in toks:
            continue
        name = toks[1]
        ndr = toks[toks.index("NONDEFAULTRULE") + 1] if "NONDEFAULTRULE" in toks else None
        t = toks[toks.index("ROUTED") + 1:]
        paths, cur, last, i = [], None, None, 0
        while i < len(t):
            tok = t[i]
            if tok == "+":
                break
            if cur is None or tok == "NEW":
                if tok == "NEW":
                    i += 1
                cur = {"layer": t[i], "taper": False, "items": []}
                paths.append(cur)
                last = None
                i += 1
                continue
            if tok == "TAPER":
                cur["taper"] = True
                i += 1
            elif tok in ("TAPERRULE", "STYLE", "MASK"):
                i += 2
            elif tok == "(":
                j = t.index(")", i)
                v = t[i + 1:j]
                x = last[0] if v[0] == "*" else int(v[0])
                y = last[1] if v[1] == "*" else int(v[1])
                ext = int(v[2]) if len(v) > 2 else None
                cur["items"].append(("pt", x, y, ext))
                last = (x, y)
                i = j + 1
            elif tok == "RECT":
                j = t.index(")", i)
                cur["items"].append(("rect", *map(int, t[i + 2:j])))
                i = j + 1
            elif tok == "VIRTUAL":
                j = t.index(")", i)
                v = t[i + 2:j]
                x = last[0] if v[0] == "*" else int(v[0])
                y = last[1] if v[1] == "*" else int(v[1])
                cur["items"].append(("virtual", x, y))
                last = (x, y)
                i = j + 1
            else:
                cur["items"].append(("via", tok))
                i += 1
                if i < len(t) and t[i] in ("N", "S", "E", "W", "FN", "FS", "FE", "FW"):
                    i += 1
        out[name] = {"ndr": ndr, "paths": paths}
    return out


def needs_split(paths) -> bool:
    """A path needs re-encoding if it continues after a via or turns a corner
    (more than two points in a row on one layer)."""
    for p in paths:
        items = p["items"]
        run = 0
        for k, it in enumerate(items):
            if it[0] == "via" and any(x[0] in ("pt", "rect", "virtual") for x in items[k + 1:]):
                return True
            run = run + 1 if it[0] == "pt" else 0
            if run > 2:
                return True
    return False


from .tools.openroad.ops.wires import encoder_tcl  # noqa: E402,F401  (the encoding lives with its operations)


def normalize(session, def_path: str) -> int:
    """Re-encode every routed net whose paths continue after a via."""
    from .analog import query_via_layers_tcl
    session.run(f"write_def {q(def_path)}")
    with open(def_path) as f:
        nets = parse_def_paths(f.read())
    todo = {n: i for n, i in nets.items() if needs_split(i["paths"])}
    if not todo:
        return 0
    vias = sorted({it[1] for i in todo.values() for p in i["paths"] for it in p["items"] if it[0] == "via"})
    flat = session.value(query_via_layers_tcl(vias)).split()
    via_layers = {flat[k]: (flat[k + 1], flat[k + 2]) for k in range(0, len(flat), 3)}
    script = "\n".join(encoder_tcl(n, i, via_layers) for n, i in todo.items())
    session.run(script)
    return len(todo)


def geometry(info: dict, via_layers: dict[str, tuple[str, str]] | None = None) -> tuple:
    """Canonical geometry of a parsed net: merged collinear segments per layer
    (and patches), plus vias. Independent of how paths are split.
    ``via_layers`` (name -> (bottom, top)) gives the layer a path continues
    on after a via."""
    segs: dict[tuple, list[tuple[int, int]]] = {}
    vias, rects = [], []
    for p in info["paths"]:
        layer, last = p["layer"], None
        for it in p["items"]:
            if it[0] == "pt":
                x, y = it[1], it[2]
                if last is not None and (x, y) != last:
                    if y == last[1]:
                        segs.setdefault((layer, "h", y), []).append(tuple(sorted((last[0], x))))
                    else:
                        segs.setdefault((layer, "v", x), []).append(tuple(sorted((last[1], y))))
                last = (x, y)
            elif it[0] == "via":
                vias.append((it[1], last))
                if via_layers and it[1] in via_layers:
                    bot, top = via_layers[it[1]]
                    layer = top if layer == bot else bot
            elif it[0] == "rect":
                rects.append((layer, last, it[1:]))
            elif it[0] == "virtual":
                last = (it[1], it[2])
    merged = []
    for key, ivs in segs.items():
        ivs.sort()
        cur = list(ivs[0])
        for a, b in ivs[1:]:
            if a <= cur[1]:
                cur[1] = max(cur[1], b)
            else:
                merged.append((key, tuple(cur)))
                cur = [a, b]
        merged.append((key, tuple(cur)))
    return tuple(sorted(merged)), tuple(sorted(vias)), tuple(sorted(rects))


def restore(session, before_def: str, check_def: str) -> int:
    """Re-encode every routed net whose wiring differs from ``before_def``
    (OpenRCX re-orders wires and drops their patches), then check the
    geometry matches the saved one. Returns the number of nets restored."""
    from .analog import query_via_layers_tcl
    with open(before_def) as f:
        saved = parse_def_paths(f.read())
    session.run(f"write_def {q(check_def)}")
    with open(check_def) as f:
        now = parse_def_paths(f.read())
    names = sorted({it[1] for i in saved.values() for p in i["paths"] for it in p["items"] if it[0] == "via"})
    flat = session.value(query_via_layers_tcl(names)).split() if names else []
    vias = {flat[k]: (flat[k + 1], flat[k + 2]) for k in range(0, len(flat), 3)}
    todo = [n for n, i in saved.items() if n not in now or geometry(now[n], vias) != geometry(i, vias)]
    if not todo:
        return 0
    session.run("\n".join(encoder_tcl(n, saved[n], vias) for n in todo))
    session.run(f"write_def {q(check_def)}")
    with open(check_def) as f:
        again = parse_def_paths(f.read())
    bad = [n for n in todo if n not in again or geometry(again[n], vias) != geometry(saved[n], vias)]
    if bad:
        raise RuntimeError(f"routing of {bad[:5]} could not be restored after extraction")
    return len(todo)
