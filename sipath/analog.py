"""Analog nets: route first with a non-default rule, then freeze.

Freezing converts the detailed-router wiring of each analog net (read back
from a DEF written by the same process) into special wiring (dbSWire /
dbSBox), deletes the regular wire, marks the net special and dont_touch.
Global and detailed routing then treat those shapes as fixed obstacles and
never rip them up. A signature of the special shapes is taken before and
after the digital route so a changed analog net fails the attempt.
"""
from __future__ import annotations

import re

from .tcl import q, qlist


def def_net_routing(def_text: str, net: str, vias: dict | None = None) -> list[tuple]:
    """Parse the regular routing of ``net`` from a DEF NETS section.

    Returns a list of ops: ("seg", layer, (x1,y1,e1), (x2,y2,e2)),
    ("via", layer, name, (x,y)), ("rect", layer, (x,y), (dx1,dy1,dx2,dy2)).
    ``e`` is None when the DEF gives no extension (default half width).
    ``vias`` maps via name -> (bottom, top) layer so a path that continues
    after a via switches layer; without it the names are still collected.
    """
    m = re.search(r"\nNETS \d+ ;(.*?)\nEND NETS", def_text, re.S)
    if not m:
        raise ValueError("DEF has no NETS section")
    esc = net.replace("/", "\\/")
    stmt = None
    for s in m.group(1).split(";"):
        toks = s.split()
        if len(toks) >= 2 and toks[0] == "-" and toks[1] in (net, esc):
            stmt = toks
            break
    if stmt is None:
        raise ValueError(f"net {net} not in DEF")
    if "ROUTED" not in stmt:
        raise ValueError(f"net {net} is not routed")
    toks = stmt[stmt.index("ROUTED") + 1:]
    ops, layer, last, i = [], None, None, 0
    keywords = {"+"}

    def point(i):
        assert toks[i] == "("
        j = toks.index(")", i)
        vals = toks[i + 1:j]
        return vals, j + 1

    expect_layer = True
    while i < len(toks):
        t = toks[i]
        if t in keywords:
            break
        if expect_layer:
            layer, last, expect_layer = t, None, False
            i += 1
            continue
        if t == "NEW":
            expect_layer = True
            i += 1
        elif t in ("TAPER",):
            i += 1
        elif t in ("TAPERRULE", "STYLE", "MASK"):
            i += 2
        elif t == "(":
            vals, i = point(i)
            x = int(vals[0]) if vals[0] != "*" else last[0]
            y = int(vals[1]) if vals[1] != "*" else last[1]
            e = int(vals[2]) if len(vals) > 2 else None
            cur = (x, y, e)
            if last is not None:
                ops.append(("seg", layer, last, cur))
            last = cur
        elif t == "RECT":
            vals, i = point(i + 1)
            ops.append(("rect", layer, (last[0], last[1]), tuple(int(v) for v in vals)))
        elif t == "VIRTUAL":
            vals, i = point(i + 1)
            last = (int(vals[0]) if vals[0] != "*" else last[0], int(vals[1]) if vals[1] != "*" else last[1], None)
        else:  # via name (optionally followed by an orientation)
            ops.append(("via", layer, t, (last[0], last[1])))
            i += 1
            if i < len(toks) and toks[i] in ("N", "S", "E", "W", "FN", "FS", "FE", "FW"):
                i += 1
            if vias and t in vias:
                bot, top = vias[t]
                layer = top if layer == bot else bot
    return ops


def query_via_layers_tcl(names) -> str:
    return f"""
set out {{}}
set tech [ord::get_db_tech]; set block [ord::get_db_block]
foreach v {qlist(names)} {{
  set via [$block findVia $v]
  if {{$via eq "NULL"}} {{ set via [$tech findVia $v] }}
  if {{$via eq "NULL"}} {{ error "unknown via $v" }}
  lappend out $v [[$via getBottomLayer] getName] [[$via getTopLayer] getName]
}}
set out
"""


def layer_widths_tcl(net: str) -> str:
    return f"""
set out {{}}
set net [::flow_net {q(net)}]
set rule [$net getNonDefaultRule]
foreach l [[ord::get_db_tech] getLayers] {{
  if {{[$l getType] ne "ROUTING"}} continue
  set w [$l getWidth]
  if {{$rule ne "NULL"}} {{ set lr [$rule getLayerRule $l]; if {{$lr ne "NULL"}} {{ set w [$lr getWidth] }} }}
  lappend out [$l getName] $w
}}
set out
"""


_CHK = 'if {$b eq "NULL"} { error "dbSBox_create refused a shape" }'


def _even_box(box):
    """OpenDB refuses a special box whose width and length are both odd (DBU)."""
    x0, y0, x1, y1 = box
    if (x1 - x0) % 2 and (y1 - y0) % 2:
        x1 -= 1
    return x0, y0, x1, y1


def swire_tcl(net: str, ops: list[tuple], widths: dict[str, int], vias: dict[str, tuple[str, str]]) -> str:
    """Tcl that rebuilds ``net`` as special wiring from parsed ops."""
    lines = [
        f"set net [::flow_net {q(net)}]",
        "set tech [ord::get_db_tech]; set block [ord::get_db_block]",
        "set sw [odb::dbSWire_create $net ROUTED]",
    ]
    layer = None
    nshapes = 0
    for op in ops:
        kind = op[0]
        if kind == "seg":
            _, layer, (x1, y1, e1), (x2, y2, e2) = op
            w = widths[layer]
            h = w // 2
            e1 = h if e1 is None else e1
            e2 = h if e2 is None else e2
            if y1 == y2:
                (xa, ea), (xb, eb) = sorted([(x1, e1), (x2, e2)])
                box = (xa - ea, y1 - h, xb + eb, y1 + h)
            elif x1 == x2:
                (ya, ea), (yb, eb) = sorted([(y1, e1), (y2, e2)])
                box = (x1 - h, ya - ea, x1 + h, yb + eb)
            else:
                raise ValueError(f"non-manhattan segment on {net}")
            b = _even_box(box)
            lines.append(f"set b [odb::dbSBox_create $sw [$tech findLayer {layer}] {b[0]} {b[1]} {b[2]} {b[3]} NONE]; "
                         + _CHK)
            nshapes += 1
        elif kind == "rect":
            _, layer, (x, y), (a, b, c, d) = op
            r = _even_box((x + a, y + b, x + c, y + d))
            lines.append(f"set b [odb::dbSBox_create $sw [$tech findLayer {layer}] {r[0]} {r[1]} {r[2]} {r[3]} NONE]; "
                         + _CHK)
            nshapes += 1
        elif kind == "via":
            _, layer, name, (x, y) = op
            lines.append(f"set v [$block findVia {name}]; if {{$v eq \"NULL\"}} {{ set v [$tech findVia {name}] }}")
            lines.append(f"set b [odb::dbSBox_create $sw $v {x} {y} NONE]; " + _CHK)
            nshapes += 1
    if nshapes == 0:
        raise ValueError(f"net {net}: no routing shapes parsed")
    lines += [
        "odb::dbWire_destroy [$net getWire]",
        "$net clearGuides",
        "$net setSpecial",
        "$net setWireOrdered 0",
        "$net setDoNotTouch 1",
        f"set_dont_touch [get_nets {q(net)}]",
        "llength [$sw getWires]",
    ]
    return "\n".join(lines)


def freeze_nets(session, nets: list[str], def_path: str) -> dict[str, int]:
    """Convert routed analog nets to frozen special wiring. Returns shape counts."""
    for n in nets:
        if session.value(f"expr {{[[::flow_net {q(n)}] getWire] ne \"NULL\"}}") != "1":
            raise RuntimeError(f"analog net {n} was not routed")
    session.run(f"write_def {q(def_path)}")
    with open(def_path) as f:
        text = f.read()
    counts = {}
    for n in nets:
        via_names = sorted({op[2] for op in def_net_routing(text, n) if op[0] == "via"})
        vias = {}
        if via_names:
            flat = session.value(query_via_layers_tcl(via_names)).split()
            vias = {flat[i]: (flat[i + 1], flat[i + 2]) for i in range(0, len(flat), 3)}
        ops = def_net_routing(text, n, vias)
        flat = session.value(layer_widths_tcl(n)).split()
        widths = {flat[i]: int(flat[i + 1]) for i in range(0, len(flat), 2)}
        counts[n] = int(session.value(swire_tcl(n, ops, widths, vias)))
    return counts


def signature(session, nets: list[str]) -> str:
    return session.value(f"::flow_swire_signature {qlist(nets)}")


def thaw_nets(session, nets: list[str], def_path: str, check_def: str) -> dict[str, int]:
    """Undo ``freeze_nets`` right before extraction: rebuild each analog net's
    regular wiring exactly as routed (from the DEF written at freeze time),
    remove its special wiring and clear the special flag, so OpenRCX extracts
    it. dont_touch stays. The rebuilt geometry is compared with the saved one;
    a mismatch fails. Returns segment counts per net."""
    from . import wires
    with open(def_path) as f:
        saved = wires.parse_def_paths(f.read())
    missing = [n for n in nets if n not in saved]
    if missing:
        raise RuntimeError(f"no saved routing for analog nets {missing} in {def_path}")
    names = sorted({it[1] for n in nets for p in saved[n]["paths"] for it in p["items"] if it[0] == "via"})
    flat = session.value(query_via_layers_tcl(names)).split() if names else []
    vias = {flat[k]: (flat[k + 1], flat[k + 2]) for k in range(0, len(flat), 3)}
    for n in nets:
        session.run(f"set net [::flow_net {q(n)}]\n"
                    "foreach sw [$net getSWires] { odb::dbSWire_destroy $sw }\n"
                    "$net clearSpecial")
        session.run(wires.encoder_tcl(n, saved[n], vias))
        session.run(f"set_dont_touch [get_nets {q(n)}]")
    session.run(f"write_def {q(check_def)}")
    with open(check_def) as f:
        now = wires.parse_def_paths(f.read())
    out = {}
    for n in nets:
        if n not in now or wires.geometry(now[n], vias) != wires.geometry(saved[n], vias):
            raise RuntimeError(f"analog net {n}: restored wiring differs from the frozen routing")
        out[n] = sum(len(p["items"]) for p in now[n]["paths"])
    return out


def is_frozen(session, nets: list[str]) -> bool:
    flags = {session.value(f"[::flow_net {q(n)}] isSpecial") for n in nets}
    if len(flags) != 1:
        raise RuntimeError(f"analog nets partly frozen: {flags}")
    return flags == {"1"}


_CUNIT = {"FF": 1.0, "PF": 1e3, "NF": 1e6, "UF": 1e9}


def spef_coupling(text: str) -> tuple[dict, dict]:
    """Coupling capacitance between nets in a SPEF: ({(a, b): fF} with a < b,
    {net: total fF}). A coupling cap is written once, in either net's *CAP
    section; its far node is a net's internal node (*N:k), an instance pin
    (*I in some net's *CONN) or a port."""
    m = re.search(r"^\*C_UNIT\s+([\d.eE+-]+)\s+(\w+)", text, re.M)
    scale = float(m.group(1)) * _CUNIT[m.group(2).upper()] if m else 1e3
    names = {}
    nm = text.find("*NAME_MAP")
    if nm >= 0:
        end = text.find("\n*PORTS", nm)
        end = text.find("\n*D_NET", nm) if end < 0 else end
        for k, v in re.findall(r"^\*(\d+) (\S+)$", text[nm:end], re.M):
            names["*" + k] = v.replace("\\", "")
    name = lambda t: names.get(t, t.replace("\\", ""))  # noqa: E731
    owner, total, caps = {}, {}, []
    for sec in re.split(r"^\*D_NET ", text, flags=re.M)[1:]:
        head, _, rest = sec.partition("\n")
        tok = head.split()
        net = name(tok[0])
        total[net] = float(tok[1]) * scale
        conn = re.search(r"^\*CONN\n(.*?)^\*(?:CAP|RES|END)", rest, re.M | re.S)
        for ln in (conn.group(1).splitlines() if conn else []):
            t = ln.split()
            if len(t) >= 2 and t[0] in ("*I", "*P"):
                owner[t[1]] = net
        cap = re.search(r"^\*CAP\n(.*?)^\*(?:RES|END)", rest, re.M | re.S)
        for ln in (cap.group(1).splitlines() if cap else []):
            t = ln.split()
            if len(t) == 4:
                caps.append((net, t[2], float(t[3]) * scale))
    pairs = {}
    for net, far, c in caps:
        other = owner.get(far)
        if other is None:
            base = far.split(":")[0] if ":" in far else far
            other = name(base) if base in names or ":" in far else name(far)
        if other == net or c <= 0:
            continue
        key = (net, other) if net < other else (other, net)
        pairs[key] = pairs.get(key, 0.0) + c
    return pairs, total


def coupling_report(spefs: list[str], analog_nets: list[str], top: int = 10) -> dict:
    """Per analog net, from every corner's SPEF (the worst value of each pair):
    total capacitance, coupling to digital nets (sum and the ``top`` largest
    aggressors) and to the other analog nets."""
    worst, totals = {}, {}
    for path in spefs:
        with open(path) as f:
            pairs, total = spef_coupling(f.read())
        for k, v in pairs.items():
            worst[k] = max(worst.get(k, 0.0), v)
        for k, v in total.items():
            totals[k] = max(totals.get(k, 0.0), v)
    ana = set(analog_nets)
    out = {}
    for n in analog_nets:
        dig, other = {}, {}
        for (a, b), v in worst.items():
            if n in (a, b):
                o = b if a == n else a
                (other if o in ana else dig)[o] = v
        aggr = sorted(dig.items(), key=lambda kv: -kv[1])
        out[n] = {"total_ff": round(totals.get(n, 0.0), 4),
                  "digital_ff": round(sum(dig.values(), 0.0), 4),
                  "aggressors": [[o, round(v, 4)] for o, v in aggr[:top]],
                  "aggressor_count": len(aggr),
                  "analog_ff": {o: round(v, 4) for o, v in sorted(other.items())}}
    return out
