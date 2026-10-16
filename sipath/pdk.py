"""PDK descriptions, loaded from data files (``sipath/pdks/<name>.yaml``).

A PDK file names the views (tech/cell LEF, Liberty, RCX rules, per-corner
files), the routing stack, the special cells (tap, endcap, fill, tie, diode,
CTS buffers), the standard-cell supply pins, PDN and analog defaults, RC and
track setup, and the IO library (masters, LEFs, pad roles, corner and filler
cells). Nothing in the flow code names a PDK layer or cell.

Paths use ``${var}`` variables: ``platforms`` (ORFS ``flow/platforms`` from
the runner, or $SIPATH_PLATFORMS), ``here`` (the file's directory), and any the
file declares under ``vars`` (with an environment variable and an optional
default). A corner whose variables are not set is left out.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

PDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdks")


@dataclass
class Corner:
    """One analysis corner: standard-cell Liberty and OpenRCX rules.
    ``process`` keys per-corner macro Liberty (``Macro.lib[process]``); ``rc``
    names its interconnect corner; ``rc_script`` (None: the PDK's) holds that
    corner's set_layer_rc / set_wire_rc for estimated parasitics."""
    name: str
    liberty: str
    rcx_rules: str
    process: str = ""
    rc: str = "nom"
    rc_script: str | None = None

    def __post_init__(self):
        self.process = self.process or self.name


@dataclass
class Pdk:
    name: str
    tech_lef: str
    cell_lef: str | list  # one LEF or several (e.g. two cell libraries)
    liberty: str
    rcx_rules: str
    site: str
    tap_cell: str
    endcap_cell: str
    fill_cells: list[str]
    cts_buffers: list[str]
    cts_root_buffer: str
    rc_script: str  # set_layer_rc / set_wire_rc
    tracks_script: str
    layers: list[str]
    io_lefs: dict[str, str] = field(default_factory=dict)
    sram: dict[str, dict[str, str]] = field(default_factory=dict)
    dont_use: list[str] = field(default_factory=list)  # patterns sizing/CTS must not choose
    exclude: list[str] = field(default_factory=list)  # cells with known layout problems: never used anywhere
    exclude_sources: list[str] = field(default_factory=list)  # where ``exclude`` came from
    signoff: dict = field(default_factory=dict)  # KLayout tech/map, DRC/LVS decks, cell/IO GDS and CDL, ...
    cts_apply_ndr: str = ""  # clock_tree_synthesis -apply_ndr strategy ("" = the tool's default)
    diode_cell: str = ""
    corners: list[Corner] = field(default_factory=list)  # empty: one corner from liberty/rcx_rules
    corner_source: str = "single typical corner"
    io_roles: dict = field(default_factory=dict)  # pad master pattern -> role (padplan)
    io_libs: dict = field(default_factory=dict)  # pad master -> {process (tt/ss/ff): Liberty}: real pad timing
    layer_adjustments: dict = field(default_factory=dict)  # routing layer -> GRT capacity reduction (0..1)
    local_layers: list = field(default_factory=list)  # below the routing stack (cell-local layers)
    min_layer: str = ""
    max_layer_core: str = ""
    max_layer_macro: str = ""
    tap_distance: float = 14.0
    tie_hi: tuple = ()  # (master, pin)
    tie_lo: tuple = ()
    power_pins: list = field(default_factory=list)  # [{"pin": regex, "net": name, "kind": power|ground|""}]
    voltages: dict = field(default_factory=lambda: {"core": 1.8, "analog": 3.3})
    pdn_defaults: dict = field(default_factory=dict)
    analog_defaults: dict = field(default_factory=dict)
    io_corner: str = ""
    io_fill: list = field(default_factory=list)
    source: str = ""  # the description file
    typical_corner: str = ""  # nominal corner (CTS library, main SPEF); default tt / the first
    optimize_corners: list = field(default_factory=list)  # corners the flow repairs against (default all)
    constraints: dict = field(default_factory=dict)  # design rules: max_transition/fanout/capacitance, ...
    clock_min_layer: str = ""  # lowest layer for clock nets
    repair_margins: dict = field(default_factory=dict)  # ns: setup, hold_place, hold_route; %: slew, cap
    step_defaults: dict = field(default_factory=dict)  # {step: {param: value}} (sipath.params)
    pin_layers: tuple = ()  # (horizontal, vertical) layers for automatic pin placement
    synth_buffer: tuple = ()  # (cell, input pin, output pin) for synthesis

    @property
    def cell_lefs(self) -> list[str]:
        return list(self.cell_lef) if isinstance(self.cell_lef, (list, tuple)) else [self.cell_lef]

    def typical(self) -> Corner:
        cs = self.analysis_corners()
        for c in cs:
            if c.name == (self.typical_corner or "tt"):
                return c
        return cs[0]

    def layer_index(self, layer: str) -> int:
        try:
            return self.layers.index(layer)
        except ValueError:
            raise ValueError(f"{layer} is not a routing layer of {self.name} ({self.layers})") from None

    def layers_between(self, lo: str, hi: str) -> list[str]:
        return self.layers[self.layer_index(lo):self.layer_index(hi) + 1]

    def above(self, layer: str) -> str:
        i = self.layer_index(layer)
        if i + 1 >= len(self.layers):
            raise ValueError(f"no routing layer above {layer}")
        return self.layers[i + 1]

    def signoff_corners(self) -> list[Corner]:
        """Every corner: extraction writes a SPEF for each, signoff STA checks each."""
        return self.corners or [Corner("tt", self.liberty, self.rcx_rules)]

    def analysis_corners(self) -> list[Corner]:
        """The corners in-flow sessions load and repair against: the PDK's
        ``optimize_corners`` (e.g. typical, worst setup, worst hold), else all.
        Each corner costs a parasitics estimate and a timing view at every
        repair (aes placement: 148 s with nine corners, 8 s with one)."""
        allc = self.signoff_corners()
        if not self.optimize_corners:
            return allc
        by = {c.name: c for c in allc}
        return [by[n] for n in self.optimize_corners]

    def excluded(self, cell: str, extra=()) -> bool:
        """``cell`` is on the PDK's exclude list or matches one of ``extra`` (patterns)."""
        import fnmatch
        return cell in set(self.exclude) or any(fnmatch.fnmatchcase(cell, p) for p in extra)

    def tie_cells(self) -> list[tuple[str, str]]:
        return [t for t in (self.tie_hi, self.tie_lo) if t]

    def files(self) -> list[str]:
        out = [self.tech_lef, *self.cell_lefs, self.liberty, self.rcx_rules]
        for c in self.corners:
            out += [c.liberty, c.rcx_rules]
        so = self.signoff or {}
        for k in ("klayout_tech", "klayout_map", "drc_deck", "lvs_deck", "density_deck", "fill_rules", "magic_rc"):
            if so.get(k):
                out.append(so[k])
        for k in ("cell_gds", "cell_cdl", "io_gds", "io_cdl"):
            out += list(so.get(k) or [])
        out = list(dict.fromkeys(out))
        out += list(self.io_lefs.values())
        for by in self.io_libs.values():
            out += list(by.values())
        for v in self.sram.values():
            out += list(v.values())
        return out


VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingVariable(KeyError):
    pass


def _expand(value, env: dict):
    """Expand ``${var}`` in strings (recursively in lists and dicts)."""
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in env:
                raise MissingVariable(m.group(1))
            return str(env[m.group(1)])
        out = VAR.sub(sub, value)
        if VAR.search(out):  # a variable whose value names another one
            out = _expand(out, env)
        return out
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    return value


def _host_path(p: str) -> str:
    """Resolve symlinks of paths that exist on this host (ciel/volare trees are
    symlink farms and a container mount needs the targets); paths that only
    exist where OpenROAD runs are kept as written."""
    return os.path.realpath(p) if isinstance(p, str) and os.path.exists(p) else p


def _hostify(v):
    """Resolve host paths inside a nested structure (see _host_path)."""
    if isinstance(v, str):
        return _host_path(v)
    if isinstance(v, list):
        return [_hostify(x) for x in v]
    if isinstance(v, dict):
        return {k: _hostify(x) for k, x in v.items()}
    return v


def read_file(path: str) -> dict:
    with open(path) as f:
        text = f.read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(f"{path}: PyYAML is needed for YAML files (or give a .json file)") from e
    return yaml.safe_load(text)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _with_base(data: dict, path: str, depth: int = 0) -> dict:
    """``extends: <name or file>``: start from that description, then this one."""
    base = data.get("extends")
    if not base:
        return data
    if depth > 4:
        raise ValueError(f"{path}: 'extends' nests too deep")
    bpath = resolve_spec(os.path.join(os.path.dirname(path), base) if base.endswith((".yaml", ".json")) else base)
    merged = _merge(_with_base(read_file(bpath), bpath, depth + 1), {k: v for k, v in data.items() if k != "extends"})
    return merged


def tracks_from_info(path: str) -> str:
    """make_tracks commands from an OpenLane ``tracks.info`` (layer X|Y offset pitch)."""
    tr = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3 and parts[1] in ("X", "Y"):
                tr.setdefault(parts[0], {})[parts[1]] = (parts[2], None)
            elif len(parts) == 4 and parts[1] in ("X", "Y"):
                tr.setdefault(parts[0], {})[parts[1]] = (parts[2], parts[3])
    out = []
    for ly, d in tr.items():
        if "X" not in d or "Y" not in d:
            raise ValueError(f"{path}: {ly} needs both X and Y tracks")
        (xo, xp), (yo, yp) = d["X"], d["Y"]
        out.append(f"make_tracks {ly} -x_offset {xo} -x_pitch {xp} -y_offset {yo} -y_pitch {yp}")
    return "\n".join(out) + "\n"


def tech_lef_rc(path: str) -> dict:
    """Per routing layer (R ohm/sq, C per area, edge C, default width) and per
    cut layer (resistance) from a tech LEF."""
    out, cur, typ = {}, None, None
    with open(path) as f:
        for raw in f:
            line = raw.split("#")[0].strip()
            if not line:
                continue
            w = line.rstrip(" ;").split()
            if w[0] == "LAYER" and len(w) == 2 and not line.endswith(";"):  # a definition, not a VIA's layer
                cur, typ = w[1], None
                out[cur] = {}
            elif w[0] == "END" and cur and len(w) == 2 and w[1] == cur:
                cur = None
            elif cur is None:
                continue
            elif w[0] == "TYPE":
                typ = w[1]
                out[cur]["type"] = typ
            elif w[:2] == ["RESISTANCE", "RPERSQ"]:
                out[cur]["rpersq"] = float(w[2])
            elif w[:2] == ["CAPACITANCE", "CPERSQDIST"]:
                out[cur]["cpersq"] = float(w[2])
            elif w[0] == "EDGECAPACITANCE":
                out[cur]["edge"] = float(w[1])
            elif w[0] == "WIDTH" and len(w) == 2 and "width" not in out[cur]:
                out[cur]["width"] = float(w[1])
            elif w[0] == "RESISTANCE" and len(w) == 2 and typ == "CUT":
                out[cur]["rcut"] = float(w[1])
    return out


RC_LINE = re.compile(r"^set_layer_rc\s+(-layer|-via)\s+(\S+)(.*)$")


def scale_rc(script: str, nom: dict, corner: dict, vias: dict | None = None) -> str:
    """The nominal set_layer_rc script with each layer's resistance and
    capacitance scaled by corner / nominal (from the two tech LEFs); via
    resistances by the cut layer's ratio (``vias``: via name -> cut layer)."""
    out = []
    for line in script.splitlines():
        m = RC_LINE.match(line.strip())
        if not m:
            out.append(line)
            continue
        kind, name, rest = m.groups()
        key = name if kind == "-layer" else (vias or {}).get(name, name)
        a, b = nom.get(key, {}), corner.get(key, {})

        def ratio(fn):
            try:
                x, y = fn(a), fn(b)
            except (KeyError, TypeError):
                return 1.0
            return y / x if x else 1.0
        rr = ratio(lambda d: d["rpersq"]) if kind == "-layer" else ratio(lambda d: d["rcut"])
        cr = ratio(lambda d: d["cpersq"] * d["width"] + 2 * d["edge"])

        def sub(flag, r):
            nonlocal rest
            rest = re.sub(rf"({flag}\s+)(\S+)", lambda mm: f"{mm.group(1)}{float(mm.group(2)) * r:.6g}", rest)
        sub("-resistance", rr)
        if kind == "-layer":
            sub("-capacitance", cr)
        out.append(f"set_layer_rc {kind} {name}{rest}")
    return "\n".join(out)


def resolve_spec(spec: str) -> str:
    if os.path.isfile(spec):
        return os.path.abspath(spec)
    for ext in (".yaml", ".yml", ".json"):
        p = os.path.join(PDK_DIR, spec + ext)
        if os.path.isfile(p):
            return p
    known = sorted(os.path.splitext(f)[0] for f in os.listdir(PDK_DIR))
    raise FileNotFoundError(f"no PDK description {spec!r} (a file, or one of {known})")


def load(spec: str, runner=None, **variables) -> Pdk:
    """Load a PDK description by name (``sipath/pdks``) or path. ``runner``
    supplies ``platforms``; keyword arguments set or override variables."""
    path = resolve_spec(spec)
    data = _with_base(read_file(path), path)
    env = {"here": os.path.dirname(path)}
    plat = runner.platform_root() if runner is not None else os.environ.get("SIPATH_PLATFORMS")
    if plat:
        env["platforms"] = plat
    for name, decl in (data.get("vars") or {}).items():
        val = os.environ.get(decl.get("env", "")) if decl.get("env") else None
        if not val and decl.get("default"):
            val = os.path.expanduser(decl["default"])
        if val and (not decl.get("must_exist", True) or os.path.isdir(val)):
            env[name] = val
    env.update({k: v for k, v in variables.items() if v is not None})
    return from_dict(data, env, source=path)


PDK_KEYS = {"name", "description", "extends", "vars", "tech_lef", "cell_lef", "liberty", "rcx_rules", "rc",
            "rc_tech_lefs", "rc_via_cut_layers", "site", "tracks", "tracks_file", "layers", "local_layers",
            "corners", "typical_corner", "optimize_corners", "cells", "route", "power", "pdn", "analog", "io", "sram", "constraints",
            "repair_margins", "step_defaults", "signoff"}


def from_dict(data: dict, env: dict, source: str = "") -> Pdk:  # noqa: C901
    unknown = sorted(set(data) - PDK_KEYS)
    if unknown:
        raise ValueError(f"PDK {data.get('name')}: unknown keys {unknown}; known: {sorted(PDK_KEYS)}")
    def need(key, value=None):
        v = data.get(key) if value is None else value
        try:
            out = _expand(v, env)
            if isinstance(out, list):
                return [_host_path(x) for x in out]
            return _host_path(out) if isinstance(out, str) and "\n" not in out else out
        except MissingVariable as e:
            raise ValueError(f"PDK {data.get('name')}: {key} needs variable ${{{e.args[0]}}}; with a native "
                             "openroad set $SIPATH_PLATFORMS to an OpenROAD-flow-scripts flow/platforms "
                             "directory (the docker runner uses the image's)") from None

    cells, route, power = data.get("cells", {}), data.get("route", {}), data.get("power", {})
    rc_script = need("rc")
    rc_tech = {}  # rc corner -> tech LEF (per-corner RC scaling against "nom")
    for k, v in (data.get("rc_tech_lefs") or {}).items():
        f = need(f"rc_tech_lefs.{k}", v)
        if not os.path.isfile(f):
            raise ValueError(f"PDK {data.get('name')}: rc tech LEF {f} not found on this host")
        rc_tech[k] = tech_lef_rc(f)
    vias = dict(data.get("rc_via_cut_layers") or {})
    corners, skipped = [], []
    for c in data.get("corners") or []:
        try:
            rc = c.get("rc", "nom")
            script = None
            if rc_tech and rc != "nom":
                if rc not in rc_tech or "nom" not in rc_tech:
                    raise ValueError(f"corner {c['name']}: no tech LEF for rc corner {rc!r} (and 'nom')")
                script = scale_rc(rc_script, rc_tech["nom"], rc_tech[rc], vias)
            corners.append(Corner(c["name"], _host_path(_expand(c["liberty"], env)),
                                  _host_path(_expand(c["rcx_rules"], env)), c.get("process", ""), rc, script))
        except MissingVariable as e:
            skipped.append(f"{c['name']} (needs ${{{e.args[0]}}})")
    io = data.get("io") or {}
    io_lefs = {}
    if io.get("masters"):
        io_env = dict(env, lef_dir=need("io.lef_dir", io.get("lef_dir", "")))
        for m in io["masters"]:
            io_lefs[m] = _expand(io["lef_pattern"], dict(io_env, master=m))
    io_libs = {}
    if io.get("liberty"):
        io_env = dict(env, lef_dir=io.get("lef_dir", ""))
        io_libs = {m: {proc: _host_path(_expand(f, io_env)) for proc, f in by.items()}
                   for m, by in io["liberty"].items()}
    liberty = need("liberty")
    rcx = need("rcx_rules")
    if len(corners) <= 1:
        corner_source = "single typical corner" + (f"; left out: {', '.join(skipped)}" if skipped else "")
        corners = []
    else:
        corner_source = "/".join(c.name for c in corners) + (f" (left out: {', '.join(skipped)})" if skipped else "")
    exclude, sources = set(cells.get("exclude") or []), []
    for k, f in enumerate(cells.get("exclude_files") or []):
        f = need(f"cells.exclude_files[{k}]", f)
        if not os.path.isfile(f):
            raise ValueError(f"PDK {data.get('name')}: exclude list {f} not found")
        with open(f) as fh:
            exclude |= {ln.split("#")[0].strip() for ln in fh if ln.split("#")[0].strip()}
        sources.append(f)
    pdk = Pdk(
        name=data["name"], tech_lef=need("tech_lef"), cell_lef=need("cell_lef"), liberty=liberty, rcx_rules=rcx,
        site=data["site"], tap_cell=cells.get("tap", ""), endcap_cell=cells.get("endcap", ""),
        fill_cells=list(cells.get("fill", [])),
        cts_buffers=list(cells.get("cts_buffers", [])), cts_root_buffer=cells.get("cts_root_buffer", ""),
        rc_script=rc_script,
        tracks_script=tracks_from_info(need("tracks_file")) if data.get("tracks_file") else need("tracks"),
        layers=list(data["layers"]),
        io_lefs=io_lefs, sram={k: need("sram", v) for k, v in (data.get("sram") or {}).items()},
        dont_use=list(cells.get("dont_use", [])), diode_cell=cells.get("diode", ""), corners=corners,
        corner_source=corner_source, io_roles=dict(io.get("roles", {})), io_libs=io_libs,
        layer_adjustments=dict(route.get("layer_adjustments") or {}),
        local_layers=list(data.get("local_layers") or []),
        min_layer=route.get("min_layer", data["layers"][0]),
        max_layer_core=route.get("max_layer_core", data["layers"][-1]),
        max_layer_macro=route.get("max_layer_macro", route.get("max_layer_core", data["layers"][-1])),
        tap_distance=float(cells.get("tap_distance") or 0.0),
        tie_hi=tuple(cells.get("tie_hi") or ()), tie_lo=tuple(cells.get("tie_lo") or ()),
        power_pins=list(power.get("pins") or []), voltages=dict(power.get("voltages") or {}),
        pdn_defaults=dict(data.get("pdn") or {}), analog_defaults=dict(data.get("analog") or {}),
        io_corner=io.get("corner", ""), io_fill=list(io.get("fill") or []), source=source,
        typical_corner=data.get("typical_corner", ""),
        optimize_corners=list(data.get("optimize_corners") or []), constraints=dict(data.get("constraints") or {}),
        clock_min_layer=route.get("clock_min_layer", ""),
        repair_margins={k: float(v) for k, v in (data.get("repair_margins") or {}).items()},
        exclude=sorted(exclude), exclude_sources=sources,
        signoff=_hostify(need("signoff", data.get("signoff") or {})),
        cts_apply_ndr=str(cells.get("cts_apply_ndr") or ""),
        step_defaults=dict(data.get("step_defaults") or {}),
        pin_layers=tuple(route.get("pin_layers") or ()),
        synth_buffer=tuple(cells.get("synth_buffer") or ()),
    )
    from . import params
    params.check_defaults(f"PDK {pdk.name}", pdk.step_defaults)
    special = [*pdk.fill_cells, pdk.tap_cell, pdk.endcap_cell, *pdk.cts_buffers, pdk.cts_root_buffer,
               pdk.diode_cell, *(c[0] for c in pdk.tie_cells())]
    bad = sorted({c for c in special if c and c in exclude})
    if bad:
        raise ValueError(f"PDK {pdk.name}: special cells on the exclude list: {bad}")
    if pdk.clock_min_layer:
        pdk.layer_index(pdk.clock_min_layer)
    if pdk.typical_corner and pdk.corners and pdk.typical_corner not in [c.name for c in pdk.corners]:
        raise ValueError(f"PDK {pdk.name}: typical_corner {pdk.typical_corner} is not a corner")
    unknown = [n for n in pdk.optimize_corners if n not in [c.name for c in pdk.signoff_corners()]]
    if unknown:
        raise ValueError(f"PDK {pdk.name}: optimize_corners {unknown} are not corners")
    if pdk.optimize_corners and pdk.typical_corner and pdk.typical_corner not in pdk.optimize_corners:
        raise ValueError(f"PDK {pdk.name}: optimize_corners must include the typical corner {pdk.typical_corner}")
    for ly in [pdk.min_layer, pdk.max_layer_core, pdk.max_layer_macro]:
        pdk.layer_index(ly)
    return pdk
