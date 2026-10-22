"""OpenROAD wire encoding: a net's routing (the paths sipath.wires parses from DEF) as dbWireEncoder calls."""
from __future__ import annotations

from ..tcl import q


def encoder_tcl(net: str, info: dict, via_layers: dict[str, tuple[str, str]]) -> str:
    lines = [f"set net [::flow_net {q(net)}]",
             "set tech [ord::get_db_tech]; set block [ord::get_db_block]",
             "set ndr [$net getNonDefaultRule]",
             "if {[$net getWire] ne \"NULL\"} { odb::dbWire_destroy [$net getWire] }",
             "set w [odb::dbWire_create $net]",
             "set enc [odb::dbWireEncoder]",
             "$enc begin $w"]

    def new_path(layer, taper):
        lines.append(f"set L [$tech findLayer {layer}]")
        if info["ndr"] and not taper:
            lines.append("set lr [expr {$ndr eq \"NULL\" ? \"NULL\" : [$ndr getLayerRule $L]}]")
            lines.append("if {$lr ne \"NULL\"} { $enc newPath $L ROUTED $lr } else { $enc newPath $L ROUTED }")
        else:
            lines.append("$enc newPath $L ROUTED")

    for p in info["paths"]:
        layer, last, npts = p["layer"], None, 0
        new_path(layer, p["taper"])
        items = p["items"]
        for k, it in enumerate(items):
            kind = it[0]
            if kind == "pt":
                _, x, y, ext = it
                if npts >= 2:
                    # corner: end the previous segment here, start a new path
                    new_path(layer, p["taper"])
                    lines.append(f"$enc addPoint {last[0]} {last[1]}")
                    npts = 1
                lines.append(f"$enc addPoint {x} {y}" + (f" {ext}" if ext is not None else ""))
                last = (x, y)
                npts += 1
            elif kind == "rect":
                lines.append(f"$enc addRect {it[1]} {it[2]} {it[3]} {it[4]}")
            elif kind == "virtual":
                # a virtual jump ends the path; continue with a fresh one
                last = (it[1], it[2])
                new_path(layer, p["taper"])
                lines.append(f"$enc addPoint {last[0]} {last[1]}")
                npts = 1
            elif kind == "via":
                name = it[1]
                lines.append(f"set v [$block findVia {name}]; "
                             f"if {{$v ne \"NULL\"}} {{ $enc addVia $v }} else {{ $enc addTechVia [$tech findVia {name}] }}")
                bot, top = via_layers[name]
                layer = top if layer == bot else bot
                if any(x[0] in ("pt", "rect", "virtual") for x in items[k + 1:]):
                    new_path(layer, p["taper"])
                    lines.append(f"$enc addPoint {last[0]} {last[1]}")
                    npts = 1
    lines += ["$enc end", "$net setWireOrdered 0", "expr 1"]
    return "\n".join(lines)
