"""Design description handed to the flow. Two modes:

* ``mode="macro"``: soft macro; ``floorplan_def`` gives die, rows and pins.
* ``mode="die"``: MCU die; ``die``, ``pads`` (edge + order) and ``macros``.

Every file is checked before an OpenROAD process is started; a missing input
is a failed run (``MissingInput``), never a skipped step.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class MissingInput(FileNotFoundError):
    pass


@dataclass
class Strap:
    layer: str
    width: float
    pitch: float
    offset: float = 0.0


@dataclass
class PdnSpec:
    """Power grid. Fields left None take the PDK's defaults (``pdn`` section)."""
    rail_layer: str | None = None
    rail_width: float | None = None
    straps: list[Strap] | None = None
    connects: list[tuple[str, str]] | None = None
    rings: bool = False  # off unless asked
    ring_connect_to_pads: bool = False  # join the core ring to pad supply pins of the same net
    ring_pad_layers: tuple = ()  # pad pin layers the ring may connect to
    ring_layers: tuple[str, str] | None = None
    ring_width: float | None = None
    ring_spacing: float | None = None
    ring_core_offset: float | None = None
    macro_connects: list[tuple[str, str]] = field(default_factory=list)
    straps_from_pdk: bool = False  # set by Design.resolve: straps are the PDK's defaults


@dataclass
class Macro:
    inst: str
    lef: str
    lib: str | dict | None = None  # one Liberty for all corners, or {corner: path}
    location: tuple[float, float] | None = None  # microns, lower-left; None = autoplace
    orient: str = "R0"
    halo: float = 10.0
    # supply pin regex -> net. Nets other than VDD/VSS (e.g. VDDA) get logical
    # connections only; the PDN builds the VDD/VSS grid.
    supplies: dict = field(default_factory=lambda: {"^VDD$": "VDD", "^VSS$": "VSS"})
    # macro PDN grid; None = connect with PdnSpec.macro_connects only
    grid_stripes: list | None = None  # [Strap]
    grid_connects: list | None = None  # [(layer, layer)]
    grid_starts_with: str | None = None  # POWER or GROUND; None keeps OpenROAD default
    grid_halo: float = 5.0
    orients: list | None = None  # orientations the autoplacer may try (None: just ``orient``)
    gds: str | None = None  # layout for signoff (DRC/LVS); LEF-only macros cannot be signed off
    cdl: str | None = None  # transistor/subcircuit netlist for LVS
    # "blackbox": LVS checks only the chip's connections to the macro's pins
    # (the macro is its LEF abstract on the LVS side; no cdl needed). Without
    # it, a macro with no cdl makes LVS fail.
    lvs: str | None = None
    # True: physical signoff uses the LEF abstract (pins, blockages) instead of
    # the GDS: DRC checks the chip around the macro, not its insides, and LVS
    # treats it as a black box. The written GDS still carries the macro's GDS.
    abstract: bool = False
    # no lib: write a black-box Liberty model from the LEF (sipath.blackbox); None: the design's auto_blackbox
    auto_blackbox: bool | None = None
    analog: bool | None = None  # None: analog if it connects to an analog net or analog supply
    sram: str | None = None  # one of the PDK's SRAM macros (its sram table): lef and lib from there


@dataclass
class Pad:
    inst: str
    edge: str  # north | south | east | west
    order: int
    master: str = ""


@dataclass
class Group:
    """Cells grouped by hierarchy prefix. ``fence`` makes a dbRegion (members
    must be inside, others outside); ``cluster`` packs members together."""
    name: str
    prefix: str
    kind: str  # "fence" | "cluster"
    box: tuple[float, float, float, float] | None = None  # fence box, microns
    utilization: float = 0.7  # cluster packing density


@dataclass
class Datapath:
    """Structured placement. ``kind="column"``: cells abutted bottom-to-top,
    one per row. ``kind="array"``: rows of cells abutted left-to-right,
    stacked bottom-to-top. ``kind="slices"``: generic bit-slice structure,
    ``cells[bit][stage]`` (``None`` for a stage a bit does not have); one row
    per bit, each stage a column as wide as its widest cell, so stage k of
    every bit starts at the same x; ``stage_gap`` sites between columns;
    ``rows_per_slice`` > 1 spaces the slices out (the unused rows stay free)."""
    name: str
    kind: str
    cells: list  # column: [inst]; array/slices: [[inst, ...], ...]
    origin: tuple[float, float]  # microns, lower-left of the structure
    stage_gap: int = 0
    rows_per_slice: int = 1


@dataclass
class Design:
    name: str
    top: str
    netlist: str | None  # gate-level Verilog; None with rtl (the synth step writes it)
    sdc: str
    mode: str  # "macro" | "die"
    floorplan_def: str | None = None
    die: tuple[float, float, float, float] | None = None
    core_margin: float = 0.0
    pads: list[Pad] = field(default_factory=list)
    pad_masters: dict = field(default_factory=dict)  # role -> master (corner, fill list, ...)
    macros: list[Macro] = field(default_factory=list)
    analog_nets: list[str] = field(default_factory=list)
    # fields left None/"" below take the PDK's defaults (Design.resolve)
    analog_ndr: dict | None = None  # {"width": {layer: um}, "spacing": {layer: um}}
    # analog wires may not use these layers over standard-cell rows (they would
    # block cell pin access); outside rows (macro halos, pads, die edge) they may
    analog_row_blocked_layers: tuple | None = None
    extra_lefs: list[str] = field(default_factory=list)
    extra_libs: list[str] = field(default_factory=list)
    is_core: bool = False  # DESIGN_IS_CORE
    min_route_layer: str = ""
    max_route_layer: str = ""  # default: the PDK's core or embedded-macro top layer
    pdn: PdnSpec = field(default_factory=PdnSpec)
    groups: list[Group] = field(default_factory=list)
    datapaths: list[Datapath] = field(default_factory=list)
    clock_ports: list[str] = field(default_factory=lambda: ["clk"])
    clock_nets: list[str] = field(default_factory=list)  # explicit CTS nets (else nets of clock_ports)
    keepouts: list = field(default_factory=list)  # boxes (um) the macro autoplacer must avoid
    macro_channel: float = 0.0  # um; facing macros (and macro to core edge, unless against it) at least this apart
    macro_pin_clearance: float = 0.0  # um of free space in front of a macro edge that carries signal pins
    analog_guard: float = 0.0  # um of extra space around analog macros (other macros and standard cells)
    layer_adjustments: dict = field(default_factory=dict)  # layer -> GRT capacity reduction (overrides PDK)
    pad_supplies: dict = field(default_factory=dict)  # IO-cell pin regex -> net (e.g. "^VCCD$": "VDD")
    pad_kinds: dict = field(default_factory=dict)  # signal pad inst -> input|output|analog (pad rules)
    pad_rules: object = None  # padplan.PadRules; None = defaults
    analog_supplies: list = field(default_factory=list)  # supply nets routed like analog nets (e.g. VDDA)
    analog_supply_layers: tuple | None = None  # (lowest, highest): wide upper metal only
    analog_supply_ndr: dict | None = None
    analog_shield: list = field(default_factory=list)  # analog nets with grounded shield wires beside them
    analog_shield_net: str = ""  # the shields' net; default: the core ground
    analog_keepaway: bool = False  # obstructions on the layers above/below shielded nets (no digital wires there)
    pdk: str = ""  # PDK description name or file (used by design files and the CLI)
    dont_use: list = field(default_factory=list)  # extra cell patterns this design never uses
    allow_excluded: list = field(default_factory=list)  # PDK-excluded cells this design uses on purpose (waivers)
    step_defaults: dict = field(default_factory=dict)  # {step: {param: value}} (sipath.params), over the PDK's
    auto_blackbox: bool = False  # macros without lib get a black-box Liberty model from their LEF (opt-in)
    # PDK core supply net -> its name in this design (e.g. a harness's {VDD: vccd1, VSS: vssd1});
    # applies to the standard-cell supplies, the PDN, macro and pad supply maps and signoff
    supply_names: dict = field(default_factory=dict)
    harness: str | None = None  # a frame (sipath.harness): the design is its wrapper
    harness_options: dict = field(default_factory=dict)  # e.g. {core_region: [x0, y0, x1, y1]} (um): rows only there
    plugins: list = field(default_factory=list)  # modules imported before the design file is checked (sipath.registry)
    extensions: dict = field(default_factory=dict)  # plugin namespace -> its settings (checked by the plugin's schema)
    # RTL input (instead of netlist): the synth step writes the netlist
    rtl: list = field(default_factory=list)  # Verilog / SystemVerilog files
    rtl_defines: dict = field(default_factory=dict)  # name -> value ("" for a bare define)
    rtl_includes: list = field(default_factory=list)  # include directories
    # macro mode without floorplan_def: die (with core_margin) or core_utilization; pins placed by the flow
    core_utilization: float | None = None  # target core utilization (0..1) after synthesis
    aspect_ratio: float = 1.0  # core height / width with core_utilization

    def resolve(self, pdk) -> "Design":
        """Fill every field left unset from the PDK's defaults (idempotent)."""
        import copy
        pd, an = pdk.pdn_defaults, pdk.analog_defaults
        if not self.min_route_layer:
            self.min_route_layer = pdk.min_layer
        if not self.max_route_layer:
            self.max_route_layer = pdk.max_layer_core if self.is_core else pdk.max_layer_macro
        p = self.pdn
        if p.rail_layer is None:
            p.rail_layer = pd.get("rail_layer", pdk.layers[0])
        if p.rail_width is None:
            p.rail_width = pd["rail_width"]
        macro = not self.is_core
        if p.straps is None:
            key = "straps_macro" if macro and "straps_macro" in pd else "straps"
            p.straps = [Strap(**s) for s in pd.get(key, [])]
            p.straps_from_pdk = True  # may be scaled down to a small core (steps.fit_straps)
        if p.connects is None:
            key = "connects_macro" if macro and "connects_macro" in pd else "connects"
            p.connects = [tuple(c) for c in pd.get(key, [])]
        for f, default in (("ring_width", 2.0), ("ring_spacing", 1.6), ("ring_core_offset", 2.0)):
            if getattr(p, f) is None:
                setattr(p, f, pd.get(f, default))
        if p.ring_layers is None:
            p.ring_layers = tuple(pd.get("ring_layers", pdk.layers[-2:]))
        if self.analog_ndr is None:
            self.analog_ndr = copy.deepcopy(an["ndr"])
        if self.analog_row_blocked_layers is None:
            self.analog_row_blocked_layers = tuple(an.get("row_blocked_layers", ()))
        if self.analog_supply_layers is None:
            self.analog_supply_layers = tuple(an.get("supply_layers", pdk.layers[-3:]))
        if self.analog_supply_ndr is None:
            self.analog_supply_ndr = copy.deepcopy(an["supply_ndr"])
        if not self.pad_masters.get("corner") and pdk.io_corner and self.mode == "die":
            self.pad_masters = dict(self.pad_masters, corner=pdk.io_corner)
        if not self.pad_masters.get("fill") and pdk.io_fill and self.mode == "die":
            self.pad_masters = dict(self.pad_masters, fill=list(pdk.io_fill))
        if self.mode == "die" and pdk.io_lefs:  # the IO library views the pad ring uses
            used = {p.master for p in self.pads} | {self.pad_masters.get("corner")} | set(self.pad_masters.get("fill", []))
            for m in sorted(x for x in used if x):
                lef = pdk.io_lefs.get(m)
                if lef and lef not in self.extra_lefs:
                    self.extra_lefs.append(lef)
        return self

    def input_files(self) -> list[str]:
        files = [self.sdc, *self.extra_lefs, *self.extra_libs, *self.rtl]
        if self.netlist or not self.rtl:
            files.append(self.netlist)
        if self.mode == "macro" and self.floorplan_def:
            files.append(self.floorplan_def)
        for m in self.macros:
            files.append(m.lef)
            if isinstance(m.lib, dict):
                files += list(m.lib.values())
            elif m.lib:
                files.append(m.lib)
            files += [f for f in (m.gds, m.cdl) if f]
        return files

    def net(self, name: str) -> str:
        """A supply net's name in this design (supply_names applied)."""
        return self.supply_names.get(name, name)

    def is_ground(self, net: str) -> bool:
        """``net`` (a name in this design) is a ground: the design's VSS, or a net whose logical name (the
        supply_names key that maps to it, else its own) is a ground's (VSS*, GND*, VGND*)."""
        logical = [k for k, v in self.supply_names.items() if v == net] or [net]
        return net == self.net("VSS") or any(n.upper().startswith(("VSS", "GND", "VGND")) for n in logical)

    def excluded_in(self, netlist: str, pdk) -> list[str]:
        """Cells of ``netlist`` the PDK or the design excludes (waivers left out)."""
        if not (pdk.exclude or self.dont_use):
            return []
        from .netlist import read_verilog
        try:
            used = {i.cell for i in read_verilog(netlist, self.top).instances}
        except Exception:  # not parseable here: OpenROAD's read reports it
            return []
        return sorted(c for c in used if pdk.excluded(c, self.dont_use) and c not in self.allow_excluded)

    def validate(self, pdk=None, check_pdk_files: bool = True, runner_prefixes: tuple[str, ...] = ()):
        """Check the description and that every host-side input exists.

        Paths under ``runner_prefixes`` live where OpenROAD runs (e.g. inside
        the container); they are checked by the session before any read."""
        problems = []
        for m in self.macros:
            if m.lvs not in (None, "blackbox"):
                problems.append(f"macro {m.inst}: lvs must be 'blackbox' or absent, not {m.lvs!r}")
        if self.mode not in ("macro", "die"):
            problems.append(f"unknown mode {self.mode!r}")
        if self.mode == "macro" and not (self.floorplan_def or self.die or self.core_utilization or self.harness):
            problems.append("macro mode needs floorplan_def, die or core_utilization")
        if not self.netlist and not self.rtl:
            problems.append("needs netlist (gate level) or rtl (synthesized by the synth step)")
        if self.core_utilization is not None and not 0 < self.core_utilization < 1:
            problems.append(f"core_utilization must be in (0, 1), not {self.core_utilization}")
        for inc in self.rtl_includes:
            if not os.path.isdir(inc):
                problems.append(f"rtl include directory {inc} not found")
        if self.mode == "die" and not self.die:
            problems.append("die mode needs die box")
        stray = [n for n in self.analog_shield if n not in self.analog_nets]
        if stray:
            problems.append(f"analog_shield names nets that are not analog_nets: {stray}")
        if self.analog_keepaway and not self.analog_shield:
            problems.append("analog_keepaway applies to the analog_shield nets; none given")
        if pdk is not None:
            self.resolve(pdk)
            top = pdk.layer_index(pdk.max_layer_macro)
            if not self.is_core and self.max_route_layer in pdk.layers and pdk.layer_index(self.max_route_layer) > top:
                problems.append(f"embedded macro (DESIGN_IS_CORE false) cannot route on {self.max_route_layer} "
                                f"(top layer {pdk.max_layer_macro})")
            for s in self.pdn.straps:
                if not self.is_core and s.layer in pdk.layers and pdk.layer_index(s.layer) > top:
                    problems.append(f"embedded macro cannot use {s.layer} straps (top layer {pdk.max_layer_macro})")
            for ly in (self.min_route_layer, self.max_route_layer, *(s.layer for s in self.pdn.straps)):
                if ly not in pdk.layers:
                    problems.append(f"{ly} is not a routing layer of {pdk.name}")
        def local(f):
            return not any(str(f).startswith(p) for p in runner_prefixes)

        if pdk is not None and self.netlist and os.path.isfile(self.netlist):
            bad = self.excluded_in(self.netlist, pdk)
            if bad:
                problems.append(f"netlist instantiates cells excluded by the PDK ({', '.join(pdk.exclude_sources) or 'list'}) "
                                f"or the design: {bad}; replace them or waive with allow_excluded")
        missing = [f for f in self.input_files() if f is None or (local(f) and not os.path.isfile(f))]
        if pdk is not None and check_pdk_files:
            missing += [f for f in pdk.files() if local(f) and not os.path.isfile(f)]
        if missing:
            problems.append("missing input files:\n  " + "\n  ".join(str(m) for m in missing))
        if problems:
            raise MissingInput("; ".join(problems))
