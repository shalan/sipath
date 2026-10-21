"""Harnesses: a fixed frame (pad ring, seal ring) around a user area that the
design fills as the frame's *wrapper*.

A harness is a class registered in sipath.registry; the built-in ones are
data, one description per frame in ``sipath/harnesses/*.yaml``:

* the wrapper template DEF (die, every boundary pin with its layer and
  position) and the wrapper module (its ports must match the template's pins);
* the user supplies (e.g. ``{VDD: vccd1, VSS: vssd1}``): the design's core
  supplies are renamed to them, their template pins are created on the
  power nets and strapped to the core ring;
* the power grid (ring, straps) and routing limits the frame expects;
* the frame layout and the wrapper's place in it (integration, see
  ``integrate``).

A design selects one with ``harness: openframe`` (and ``harness_options``).
``apply`` turns the design into a soft macro on the template (floorplan DEF
with rows, supplies, PDN); after the run, the ``harness`` signoff check
compares the routed wrapper with the template (die, every pin's name, layer
and rectangle; nothing missing or extra).
"""
from __future__ import annotations

import os
import re

from .design import PdnSpec, Strap

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harnesses")


# --------------------------------------------------------------- DEF template
def parse_def_pins(path: str) -> tuple[tuple[int, int, int, int], int, list[dict]]:
    """(die box, DBU, [{name, net, use, layer, rect: (x0, y0, x1, y1) absolute}]).
    A pin with several ports gives one entry per port."""
    with open(path) as f:
        text = f.read()
    dbu = int(re.search(r"UNITS DISTANCE MICRONS (\d+)", text).group(1))
    die = tuple(int(v) for v in re.search(r"DIEAREA\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)",
                                          text).groups())
    body = text[text.index("\nPINS"):text.index("END PINS")]
    pins = []
    for stmt in body.split(";")[1:]:
        m = re.match(r"\s*-\s+(\S+)\s+\+\s+NET\s+(\S+)", stmt)
        if not m:
            continue
        name, net = m.groups()
        use = (re.search(r"\+\s+USE\s+(\w+)", stmt) or [None, "SIGNAL"])[1]
        ports = re.split(r"\+\s+PORT\b", stmt)[1:] or [stmt]
        for port in ports:  # a port: one or more LAYER rectangles, then its placement
            place = re.search(r"\+\s+(?:PLACED|FIXED)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*(\w+)", port)
            if not place:
                continue
            px, py, orient = int(place.group(1)), int(place.group(2)), place.group(3)
            if orient not in ("N", "FN"):
                raise ValueError(f"{path}: pin {name} orientation {orient} not supported")
            for lm in re.finditer(r"\+\s+LAYER\s+(\S+)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)",
                                  port):
                layer = lm.group(1)
                x0, y0, x1, y1 = map(int, lm.groups()[1:])
                pins.append({"name": name, "net": net, "use": use, "layer": layer,
                             "rect": (px + x0, py + y0, px + x1, py + y1)})
    return die, dbu, pins


CHANNEL_ROW = "PINCH"  # name prefix of the pin channels' rows (global placement keeps the logic out of them)


def floorplan_def(template: str, out: str, site: str, site_w: float, row_h: float, margin: float,
                  region: tuple | None = None, channels: list | None = None) -> str:
    """The template with standard-cell rows (orientations N/FS alternating,
    snapped to the site grid): in the core (die inset by ``margin`` um), or
    only in ``region`` (x0, y0, x1, y1 um) for a wrapper that is mostly empty;
    plus the ``channels`` (from ``pin_channels``: rows on the region's row
    grid, as (row index, x0, x1) DBU), named ``PINCH_<n>``."""
    die, dbu, _ = parse_def_pins(template)
    with open(template) as f:
        text = f.read()
    sw, rh = round(site_w * dbu), round(row_h * dbu)
    x0, y0, x1, y1 = row_area(die, dbu, site_w, row_h, margin, region, template)
    nsites, nrows = (x1 - x0) // sw, (y1 - y0) // rh
    segs = [("ROW", r, x0, x0 + nsites * sw) for r in range(nrows)]
    segs += [(CHANNEL_ROW, *c) for c in sorted(channels or [])]
    rows = "".join(f"ROW {n}_{i} {site} {a} {y0 + r * rh} {'N' if r % 2 == 0 else 'FS'} DO {(b - a) // sw} BY 1 "
                   f"STEP {sw} 0 ;\n" for i, (n, r, a, b) in enumerate(segs))
    text = re.sub(r"(DIEAREA[^;]*;)", lambda m: m.group(1) + "\n\n" + rows, text, count=1)
    with open(out, "w") as f:
        f.write(text)
    return out


def row_area(die: tuple, dbu: int, site_w: float, row_h: float, margin: float, region: tuple | None = None,
             template: str = "") -> tuple[int, int, int, int]:
    """The DBU box ``floorplan_def`` fills with rows: x0 on the site grid, y0
    the first row's bottom (x1, y1: the core or region bound, not snapped)."""
    sw, rh = round(site_w * dbu), round(row_h * dbu)
    m = round(margin * dbu)
    box = (die[0] + m, die[1] + m, die[2] - m, die[3] - m)
    if region:
        r = tuple(round(v * dbu) for v in region)
        box = (max(box[0], r[0]), max(box[1], r[1]), min(box[2], r[2]), min(box[3], r[3]))
        if box[2] - box[0] < 10 * sw or box[3] - box[1] < 2 * rh:
            raise ValueError(f"core region {region} is empty or outside the core of {template}")
    return (-(-box[0] // sw) * sw, box[1], box[2], box[3])


def pin_channels(pins: list[tuple[int, int]], die: tuple, dbu: int, site_w: float, row_h: float, margin: float,
                 region: tuple, reach: float, nrows: int = 2, min_len: float = 0.0) -> list[tuple[int, int, int]]:
    """Rows next to the pins far from the core region, so that repair can
    buffer their nets next to them (a frame pin may lie millimetres from the
    region; no buffer in the region fixes the slew of the wire to it).
    ``pins``: (x, y) DBU of the used signal pins; one farther than ``reach``
    um (Manhattan) from the region's rows gets a channel: ``nrows`` rows at
    its height, on the region's row grid and inside the core, from the pin
    across the region's x range (above or below the region), or up to the
    region (beside it), at least ``min_len`` um long (a vertical power strap
    must cross it). Returns (row index, x0, x1) DBU, row 0 being the region's
    first row; overlapping channels are merged."""
    sw, rh = round(site_w * dbu), round(row_h * dbu)
    rx0, ry0, rx1_, ry1_ = row_area(die, dbu, site_w, row_h, margin, region)
    rn, sites = (ry1_ - ry0) // rh, (rx1_ - rx0) // sw
    rx1, ry1 = rx0 + sites * sw, ry0 + rn * rh
    m = round(margin * dbu)
    cx0, cy0, cx1, cy1 = die[0] + m, die[1] + m, die[2] - m, die[3] - m
    kmin, kmax = -((ry0 - cy0) // rh), (cy1 - ry0) // rh - 1  # row indices inside the core
    pad, gap, mlen = round(10 * dbu), round(5 * dbu), round(min_len * dbu)
    spans: dict[int, list[list[int]]] = {}
    for px, py in pins:
        px, py = min(max(px, cx0), cx1), min(max(py, cy0), cy1)
        if max(rx0 - px, 0, px - rx1) + max(ry0 - py, 0, py - ry1) <= reach * dbu:
            continue
        k0 = min(max((py - ry0) // rh - (nrows - 1) // 2, kmin), kmax - nrows + 1)
        beside = k0 + nrows > 0 and k0 < rn  # band rows at the region's heights: stop short of it
        if beside and px < rx0:
            a, b = px - pad, rx0 - gap
            a = min(a, b - mlen)
        elif beside:
            a, b = rx1 + gap, px + pad
            b = max(b, a + mlen)
        else:
            a, b = min(rx0, px - pad), max(rx1, px + pad)
            if b - a < mlen:
                a, b = (a + b - mlen) // 2, (a + b + mlen) // 2
        a, b = -(-max(a, cx0) // sw) * sw, min(b, cx1) // sw * sw
        for k in range(k0, k0 + nrows):
            pieces = [(a, b)]
            if 0 <= k < rn:  # never over the region's own rows
                pieces = [(a, min(b, rx0 - gap)), (max(a, -(-(rx1 + gap) // sw) * sw), b)]
            spans.setdefault(k, []).extend([list(p) for p in pieces if p[1] - p[0] >= 20 * sw])
    out = []
    for k, segs in sorted(spans.items()):
        segs.sort()
        merged = [segs[0]]
        for s in segs[1:]:
            if s[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], s[1])
            else:
                merged.append(s)
        out += [(k, a, b) for a, b in merged]
    return out


# ------------------------------------------------------------------- harnesses
class Harness:
    """Base class: a frame with a fixed user area. Subclasses set ``spec``."""
    name = ""
    spec: dict = {}
    root = HERE

    def __init__(self, options: dict | None = None):
        self.options = dict(options or {})

    def path(self, key: str) -> str:
        p = self.spec[key]
        return p if os.path.isabs(p) else os.path.join(self.root, p)

    # ---- before the run
    def apply(self, d, pdk, workdir: str) -> dict:
        """Make ``d`` this harness's wrapper. Returns what was set (for provenance)."""
        s = self.spec
        if d.top != s["wrapper"]:
            raise ValueError(f"harness {self.name}: the design's top must be {s['wrapper']}, not {d.top}")
        if s.get("pdks") and pdk.name not in s["pdks"]:
            raise ValueError(f"harness {self.name} is for {s['pdks']}, not {pdk.name}")
        os.makedirs(os.path.join(workdir, "harness"), exist_ok=True)
        fp, _ = self.floorplan(pdk, os.path.join(workdir, "harness", f"{s['wrapper']}.fp.def"))
        d.mode, d.floorplan_def, d.die, d.core_utilization = "macro", fp, None, None
        d.is_core = bool(s.get("is_core", True))
        if s.get("max_route_layer"):
            d.max_route_layer = s["max_route_layer"]
        d.supply_names = {**s.get("supplies", {}), **d.supply_names}
        if s.get("pdn"):
            p = dict(s["pdn"])
            if p.get("straps") is not None:
                p["straps"] = [Strap(**x) for x in p["straps"]]
            for k in ("connects", "macro_connects"):
                if p.get(k) is not None:
                    p[k] = [tuple(c) for c in p[k]]
            if p.get("ring_layers") is not None:
                p["ring_layers"] = tuple(p["ring_layers"])
            d.pdn = PdnSpec(**p)
        return {"harness": self.name, "floorplan_def": fp, "supplies": d.supply_names}

    def floorplan(self, pdk, out: str, used_pins: list[str] = ()) -> tuple[str, list]:
        """The wrapper's floorplan DEF: rows in the core (``core_region``
        option: only there) and, with a core region, channels to the
        ``used_pins`` (signal pins with cells on their nets) farther than
        ``pin_reach`` um (default 300) from it: ``pin_channel_rows`` rows
        (default 2) each, long enough for a vertical strap to power them
        (``pin_channels: false``: none). Returns (path, channels)."""
        s, o = self.spec, self.options
        region = tuple(o["core_region"]) if o.get("core_region") else None
        channels = []
        if region and used_pins and o.get("pin_channels", True):
            die, dbu, pins = parse_def_pins(self.path("template_def"))
            used = set(used_pins)
            pts = [((p["rect"][0] + p["rect"][2]) // 2, (p["rect"][1] + p["rect"][3]) // 2)
                   for p in pins if p["name"] in used]
            straps = (s.get("pdn") or {}).get("straps") or []
            min_len = max((st["offset"] + st["pitch"] + 2 * st["width"] for st in straps), default=0.0)
            channels = pin_channels(pts, die, dbu, s["site_width"], s["row_height"], s["core_margin"], region,
                                    float(o.get("pin_reach", 300.0)), int(o.get("pin_channel_rows", 2)), min_len)
        return floorplan_def(self.path("template_def"), out, pdk.site, s["site_width"], s["row_height"],
                             s["core_margin"], region, channels), channels

    # ---- during the run
    def supply_pin_names(self) -> list[str]:
        """Every supply pin of the wrapper (``power_pins``: the frame's supplies,
        used or not; by default the user supplies only)."""
        return list(self.spec.get("power_pins") or sorted(set(self.spec.get("supplies", {}).values())))

    def power_pins(self) -> list[dict]:
        """Template shapes of the supply pins (each shape once)."""
        wanted, out = set(self.supply_pin_names()), []
        for p in parse_def_pins(self.path("template_def"))[2]:
            if p["name"] in wanted and p not in out:
                out.append(p)
        return out

    def create_power_pins_tcl(self) -> str:
        """Tcl that creates the supply pins on special power/ground nets, one
        pin per supply with all its template shapes (the netlist has no power
        ports). Supplies the design does not use get their pins and nothing
        else, as the frame expects every wrapper pin."""
        out = ["set __b [ord::get_db_block]", "set __tech [ord::get_db_tech]"]
        ground = {v for k, v in self.spec.get("supplies", {}).items() if k.upper().startswith(("VSS", "GND"))}
        pins = self.power_pins()
        for net in sorted({p["net"] for p in pins}):
            sig = "GROUND" if net in ground or net.lower().startswith(("vss", "gnd")) else "POWER"
            out += [f"set __n [$__b findNet {net}]",
                    f"if {{$__n eq \"NULL\"}} {{ set __n [odb::dbNet_create $__b {net}] }}",
                    "$__n setSpecial", f"$__n setSigType {sig}",
                    f"set __t [$__b findBTerm {net}]",
                    f"if {{$__t eq \"NULL\"}} {{ set __t [odb::dbBTerm_create $__n {net}] }}",
                    "$__t setIoType INOUT", f"$__t setSigType {sig}", "$__t setSpecial"]
            for p in (q for q in pins if q["net"] == net):
                x0, y0, x1, y1 = p["rect"]
                out += ["set __p [odb::dbBPin_create $__t]", "$__p setPlacementStatus FIRM",
                        f"odb::dbBox_create $__p [$__tech findLayer {p['layer']}] {x0} {y0} {x1} {y1}"]
        return "\n".join(out)

    @staticmethod
    def clip_pins_tcl() -> str:
        """Tcl that clips every signal pin shape to the die. Frame templates
        let pins overhang the wrapper boundary (onto the frame's wires); the
        global router places such pins off its grid, and a net of pins only
        (a loopback tie) is then left with no pin at all. ``restore_pins_tcl``
        puts the template shapes back before the outputs are written. A pin
        listed twice with the same shapes (the OpenFrame template has four)
        keeps one copy: the detailed router takes one pin per signal port."""
        return r"""set __blk [ord::get_db_block]; set __die [$__blk getDieArea]; set __n 0
foreach __b [$__blk getBTerms] {
  if {[[$__b getNet] isSpecial]} continue
  set __seen {}
  foreach __p [$__b getBPins] {
    set __bs {}; set __out 0; set __sig {}
    foreach __x [$__p getBoxes] {
      lappend __bs [list [$__x getTechLayer] [$__x xMin] [$__x yMin] [$__x xMax] [$__x yMax]]
      lappend __sig [list [[$__x getTechLayer] getName] [$__x xMin] [$__x yMin] [$__x xMax] [$__x yMax]]
      if {[$__x xMin] < [$__die xMin] || [$__x yMin] < [$__die yMin] || [$__x xMax] > [$__die xMax]
          || [$__x yMax] > [$__die yMax]} { set __out 1 }
    }
    if {[lsearch -exact $__seen $__sig] >= 0} { odb::dbBPin_destroy $__p; continue }
    lappend __seen $__sig
    if {!$__out} continue
    set __st [$__p getPlacementStatus]; odb::dbBPin_destroy $__p
    set __np [odb::dbBPin_create $__b]; $__np setPlacementStatus $__st
    foreach __x $__bs { lassign $__x __l __x0 __y0 __x1 __y1
      odb::dbBox_create $__np $__l [expr {max($__x0, [$__die xMin])}] [expr {max($__y0, [$__die yMin])}] \
        [expr {min($__x1, [$__die xMax])}] [expr {min($__y1, [$__die yMax])}] }
    incr __n
  }
}
set __n"""

    def restore_pins_tcl(self) -> str:
        """Tcl that gives every signal pin the template's shapes again (one
        BPin per template rectangle); the routed connection lies inside the
        clipped shape, which the template shape contains."""
        by_name: dict[str, list] = {}
        supplies = set(self.supply_pin_names())
        for p in parse_def_pins(self.path("template_def"))[2]:
            if p["name"] not in supplies and p not in by_name.get(p["name"], []):  # duplicates once
                by_name.setdefault(p["name"], []).append(p)
        out = ["set __blk [ord::get_db_block]; set __tech [ord::get_db_tech]; set __n 0"]
        for name, ps in sorted(by_name.items()):
            out.append(f"set __b [$__blk findBTerm {{{name}}}]")
            out.append("if {$__b ne \"NULL\"} { set __st FIXED; foreach __p [$__b getBPins] "
                       "{ set __st [$__p getPlacementStatus]; odb::dbBPin_destroy $__p }")
            for p in ps:
                x0, y0, x1, y1 = p["rect"]
                out.append(f"  set __np [odb::dbBPin_create $__b]; $__np setPlacementStatus $__st; "
                           f"odb::dbBox_create $__np [$__tech findLayer {p['layer']}] {x0} {y0} {x1} {y1}")
            out.append("  incr __n }")
        out.append("set __n")
        return "\n".join(out)

    # ---- after the run
    def conformance(self, def_path: str) -> dict:
        """The routed wrapper against the template: same die, every template pin
        (by name) present with the same layer and rectangle, no pin the template lacks."""
        tdie, tdbu, tpins = parse_def_pins(self.path("template_def"))
        die, dbu, pins = parse_def_pins(def_path)
        scale = dbu / tdbu
        # by pin name: in the wrapper several pins may share a net (assigns, loopback ties)
        want = {(p["name"], p["layer"], tuple(round(v * scale) for v in p["rect"])) for p in tpins}
        have = {(p["name"], p["layer"], p["rect"]) for p in pins}
        missing = sorted(want - have)
        extra = sorted(h for h in have if h not in want)
        die_ok = tuple(round(v * scale) for v in tdie) == die
        return {"pass": die_ok and not missing and not extra, "die_matches": die_ok,
                "template_pins": len(want), "missing": [f"{n} {ly} {r}" for n, ly, r in missing[:20]],
                "missing_count": len(missing), "extra": [f"{n} {ly} {r}" for n, ly, r in extra[:20]],
                "extra_count": len(extra),
                "reason": (f"{len(missing)} template pin(s) missing or moved, {len(extra)} extra"
                           + ("" if die_ok else ", die differs"))}


    # ---- integration (H2)
    def integrate_script(self, workdir: str, wrapper_gds: str, out_gds: str) -> str:
        """Magic script putting the hardened wrapper (its GDS, copied as is)
        into the frame layout and writing the chip GDS, as the frame's own
        build does (Caravel ``make openframe``). ``workdir`` holds a copy of the
        frame views (``prepare_frame``)."""
        f = self.spec["frame"]
        m = f["magic"]
        paths = ["frame/mag", "frame/mag/hexdigits", "frame/mag/primitives", "."]
        lines = ["crashbackups stop", "drc off", "gds readonly true", "gds rescale false"]
        lines += [f"addpath {{{os.path.join(workdir, p)}}}" for p in paths]
        lines += [f"gds read {{{wrapper_gds}}}",  # the wrapper's cells, read-only: written back unchanged
                  f"load {m['top']} -dereference", "select top cell", "expand",
                  "puts \"SIPATH_BBOX [box values]\"",
                  "cif *hier write disable", "cif *array write disable",
                  f"gds write {{{out_gds}}}", "puts SIPATH_DONE", "quit -noprompt"]
        return "\n".join(lines) + "\n"

    @staticmethod
    def seal_cell(seal_gds: str) -> str:
        """The seal ring's cell name: its GDS file's name (advSeal_6um_gen.gds)."""
        base = os.path.basename(seal_gds)
        for ext in (".gz", ".gds", ".GDS"):
            base = base[:-len(ext)] if base.endswith(ext) else base
        return base

    def seal_ring(self, workdir: str) -> str | None:
        """The frame's seal ring GDS, decompressed into ``workdir``; None if it has none."""
        import gzip
        import shutil
        src = (self.spec.get("frame") or {}).get("seal_ring")
        if not src:
            return None
        src = src if os.path.isabs(src) else os.path.join(self.root, src)
        dst = os.path.join(workdir, os.path.basename(src)[:-3] if src.endswith(".gz") else os.path.basename(src))
        if src.endswith(".gz"):
            with gzip.open(src) as fi, open(dst, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        else:
            shutil.copy2(src, dst)
        return dst

    def prepare_frame(self, workdir: str) -> list[str]:
        """Copy the frame's views into ``workdir`` (GDS decompressed: the
        views name their GDS files uncompressed, relative to themselves)."""
        import gzip
        import shutil
        f = self.spec["frame"]
        copied = []
        for key in [f["magic"]["file"], *f.get("views", [])]:
            src = key if os.path.isabs(key) else os.path.join(self.root, key)
            shutil.copy2(src, os.path.join(workdir, os.path.basename(src)))
            copied.append(os.path.basename(src))
        cells = f.get("cells")
        if cells:
            src = cells if os.path.isabs(cells) else os.path.join(self.root, cells)
            dst = os.path.join(workdir, "frame")
            shutil.copytree(src, dst, dirs_exist_ok=True)
            for dp, _, fs in os.walk(dst):
                for g in fs:
                    if g.endswith(".gds.gz"):
                        with gzip.open(os.path.join(dp, g)) as fi, open(os.path.join(dp, g[:-3]), "wb") as fo:
                            shutil.copyfileobj(fi, fo)
            copied.append("frame/")
        return copied


class TemplateHarness(Harness):
    """A harness described by data (sipath/harnesses/<name>.yaml)."""


def load_builtin() -> list[str]:
    """Register the harness descriptions shipped in sipath/harnesses (once)."""
    from . import registry
    from .pdk import read_file
    names = []
    for f in sorted(os.listdir(HERE)):
        if not f.endswith((".yaml", ".yml")):
            continue
        spec = read_file(os.path.join(HERE, f))
        name = spec["name"]
        if name in registry.HARNESSES:
            continue
        cls = type(f"{name.title().replace('_', '')}Harness", (TemplateHarness,), {"spec": spec, "root": HERE})
        registry.register_harness(name, cls)
        names.append(name)
    return names


def get(name: str, options: dict | None = None) -> Harness:
    from . import registry
    load_builtin()
    if name not in registry.HARNESSES:
        raise ValueError(f"unknown harness {name!r}; known: {sorted(registry.HARNESSES)}")
    return registry.HARNESSES[name](options)
