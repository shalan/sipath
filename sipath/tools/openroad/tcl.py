"""Tcl generators. Pure functions: inputs in, Tcl text out."""
from __future__ import annotations

import re

from ...design import Design, Group
from ...pdk import Pdk


from .tclquote import q, qe, qlist, tcl_parse, tcl_words  # noqa: F401 (re-exported)


def require_files(files) -> str:
    return "\n".join(f"if {{![file exists {q(f)}]}} {{ error {q('missing input file: ' + f)} }}" for f in files)


HELPERS = r"""
proc ::flow_metrics {} {
  set block [ord::get_db_block]
  set m [dict create]
  dict set m insts [llength [$block getInsts]]
  foreach {k cmd} {wns {sta::worst_slack -max} tns {sta::total_negative_slack -max} whs {sta::worst_slack -min}} {
    if {[catch {set v [eval $cmd]}]} { set v nan }
    dict set m $k $v
  }
  if {[info exists ::flow_corners] && [llength $::flow_corners] > 1} {
    foreach c $::flow_corners {
      foreach {k mm} {wns -max whs -min} {
        if {[catch {set v [sta::worst_slack $mm -corner $c]}]} { set v nan }
        dict set m ${k}_$c $v
      }
    }
  }
  set hp 0
  foreach net [$block getNets] {
    set st [$net getSigType]
    if {$st eq "POWER" || $st eq "GROUND"} continue
    set xs {}; set ys {}
    foreach it [$net getITerms] { set a [$it getAvgXY]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } }
    foreach bt [$net getBTerms] { set a [$bt getFirstPinLocation]; if {[lindex $a 0]} { lappend xs [lindex $a 1]; lappend ys [lindex $a 2] } }
    if {[llength $xs] > 1} {
      set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
      set hp [expr {$hp + [lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]}]
    }
  }
  dict set m hpwl_um [expr {double($hp) / [$block getDbUnitsPerMicron]}]
  set area 0
  foreach inst [$block getInsts] {
    set mst [$inst getMaster]
    if {[$mst getType] eq "CORE" || [string match "CORE_*" [$mst getType]]} {
      if {[$mst getType] in {CORE_SPACER CORE_WELLTAP CORE_ANTENNACELL}} continue
      set area [expr {$area + double([$mst getWidth]) * [$mst getHeight]}]
    }
  }
  set core [$block getCoreArea]
  set carea [expr {double([$core dx]) * [$core dy]}]
  dict set m std_util [expr {$carea > 0 ? $area / $carea : 0}]
  return $m
}
proc ::flow_flag {key} {
  set p [odb::dbBoolProperty_find [ord::get_db_block] $key]
  expr {$p ne "NULL" && [$p getValue]}
}
proc ::flow_set_flag {key} {
  set b [ord::get_db_block]
  set p [odb::dbBoolProperty_find $b $key]
  if {$p eq "NULL"} { odb::dbBoolProperty_create $b $key 1 } else { $p setValue 1 }
}
proc ::flow_timing_prep {} {
  # parasitics for checkpoint metrics: placement estimate unless SPEF was read
  # (or a global-only route left its own estimate)
  if {[::flow_flag flow_post_cts]} { set_propagated_clock [all_clocks] }
  if {[info exists ::flow_grt_parasitics]} { return global_routing }
  if {![info exists ::flow_spef_loaded]} {
    if {[[ord::get_db_block] getRows] ne {}} { estimate_parasitics -placement }
    return placement
  }
  return rcx
}
proc ::flow_supply_pin_data {inst_name} {
  # supply pins of a macro: {pin net {layer x0 y0 x1 y1 ...master coords}} and
  # the special shapes of each such net {net {layer x0 y0 x1 y1} ...}
  set inst [::flow_inst $inst_name]
  set pins {}; set nets {}
  foreach it [$inst getITerms] {
    set mt [$it getMTerm]
    if {[$mt getSigType] ni {POWER GROUND}} continue
    set net [$it getNet]
    set nname [expr {$net eq "NULL" ? "-" : [$net getName]}]
    set shapes {}
    foreach mp [$mt getMPins] { foreach g [$mp getGeometry] {
      set l [$g getTechLayer]; if {$l eq "NULL"} continue
      lappend shapes [list [$l getName] [$g xMin] [$g yMin] [$g xMax] [$g yMax]]
    } }
    lappend pins [list [$mt getName] $nname $shapes]
    if {$net ne "NULL" && ![dict exists $nets $nname]} {
      set boxes {}
      foreach sw [$net getSWires] { foreach b [$sw getWires] {
        if {[$b isVia]} {
          # via: its bbox on its bottom and top metal (getViaBoxes is not usable from Tcl)
          set v [$b getTechVia]; if {$v eq "NULL"} { set v [$b getBlockVia] }
          foreach l [list [$v getBottomLayer] [$v getTopLayer]] {
            lappend boxes [list [$l getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] }
        } else { lappend boxes [list [[$b getTechLayer] getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]] }
      } }
      dict set nets $nname $boxes
    }
  }
  set m [$inst getMaster]; set bb [$inst getBBox]
  return [list [$inst getOrient] [$bb xMin] [$bb yMin] [$bb xMax] [$bb yMax] [$m getWidth] [$m getHeight] $pins $nets]
}
proc ::flow_find_inst {name} {
  # NULL when there is none. The database keeps a bus register's or an escaped
  # Verilog name (result[0]_reg, \bank[0].u_ram) with its brackets escaped;
  # STA reports print them plain
  set inst [[ord::get_db_block] findInst $name]
  if {$inst eq "NULL"} { set inst [[ord::get_db_block] findInst [string map {[ \\[ ] \\]} $name]] }
  return $inst
}
proc ::flow_inst {name} {
  set inst [::flow_find_inst $name]
  if {$inst eq "NULL"} { error "no instance $name" }
  return $inst
}
proc ::flow_net {name} {
  set net [[ord::get_db_block] findNet $name]
  if {$net eq "NULL"} { set net [[ord::get_db_block] findNet [string map {[ \\[ ] \\]} $name]] }
  if {$net eq "NULL"} { error "no net $name" }
  return $net
}
proc ::flow_swire_signature {names} {
  set sig {}
  foreach n $names {
    set net [::flow_net $n]
    set boxes {}
    foreach sw [$net getSWires] { foreach b [$sw getWires] {
      if {[$b isVia]} {
        set v [$b getTechVia]; if {$v eq "NULL"} { set v [$b getBlockVia] }
        lappend boxes [list via [$v getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
      } else {
        lappend boxes [list [[$b getTechLayer] getName] [$b xMin] [$b yMin] [$b xMax] [$b yMax]]
      }
    } }
    lappend sig [list $n [$net isSpecial] [$net isDoNotTouch] [expr {[$net getWire] ne "NULL"}] [lsort $boxes]]
  }
  return $sig
}
"""


def corner_libs(pdk: Pdk, d: Design, corner: str) -> list[str]:
    cs = [c for c in pdk.signoff_corners() if c.name == corner]
    libs = [c.liberty for c in cs] + list(d.extra_libs)
    process = cs[0].process if cs else corner
    # the pads' own timing models (PDK io.liberty), for the pad masters the design uses
    for master in sorted({p.master for p in d.pads} & set(pdk.io_libs)):
        by = pdk.io_libs[master]
        key = corner if corner in by else process if process in by else None
        if key is None:
            raise ValueError(f"pad {master}: no Liberty for corner {corner} (process {process})")
        if by[key] not in libs:
            libs.append(by[key])
    for m in d.macros:
        if isinstance(m.lib, dict):  # keyed by corner name or by process (ss/tt/ff)
            key = corner if corner in m.lib else process
            if key not in m.lib:
                raise ValueError(f"macro {m.inst}: no Liberty for corner {corner} (process {process})")
            libs.append(m.lib[key])
        elif m.lib:
            libs.append(m.lib)
    return libs


def _read_libs(pdk: Pdk, d: Design, corners=None) -> list[str]:
    corners = corners or pdk.analysis_corners()
    if len(corners) == 1:
        return [f"read_liberty {q(f)}" for f in corner_libs(pdk, d, corners[0].name)]
    out = []
    main = pdk.typical().name
    for c in corners:
        for k, f in enumerate(corner_libs(pdk, d, c.name)):
            out.append(f"read_liberty -corner {c.name} {q(f)}")
            if c.name == main and k == 0:
                # CTS resolves dummy-load cells from one library; with several
                # corners loaded it must be told which (else CTS-0118)
                out.append("set ::flow_cts_library [get_name [lindex [get_libs *] end]]")
    return out


def _define_corners(pdk: Pdk, corners=None) -> list[str]:
    names = [c.name for c in (corners or pdk.analysis_corners())]
    out = [f"set ::flow_corners {qlist(names)}"]
    if len(names) > 1:
        out.insert(0, f"define_corners {' '.join(names)}")
    return out


def _all_libs(pdk: Pdk, d: Design, corners=None) -> list[str]:
    return sorted({f for c in (corners or pdk.analysis_corners()) for f in corner_libs(pdk, d, c.name)})


def load_root(pdk: Pdk, d: Design, netlist: str | None = None) -> str:
    """The design from its netlist (``netlist``: e.g. the synth step's output)."""
    netlist = netlist or d.netlist
    lefs = [pdk.tech_lef, *pdk.cell_lefs, *d.extra_lefs, *[m.lef for m in d.macros]]
    files = lefs + _all_libs(pdk, d) + [netlist, d.sdc] + ([d.floorplan_def] if d.floorplan_def else [])
    out = [require_files(files), *_define_corners(pdk)]
    out += [f"read_lef {q(f)}" for f in lefs]
    out += _read_libs(pdk, d)
    out += [f"read_verilog {q(netlist)}", f"link_design {d.top}"]
    out.append(_timing_setup(pdk, d))
    return "\n".join(out)


def load_checkpoint(pdk: Pdk, d: Design, odb: str) -> str:
    out = [require_files([odb, *_all_libs(pdk, d), d.sdc]), *_define_corners(pdk), f"read_db {q(odb)}"]
    out += _read_libs(pdk, d)
    out.append(_timing_setup(pdk, d))
    out.append("if {[::flow_flag flow_post_cts]} { set_propagated_clock [all_clocks] }")
    return "\n".join(out)


def _clamp_wire_rc(script: str, pdk: Pdk, d: Design | None) -> str:
    """A ``set_wire_rc -layer`` above the design's top routing layer (e.g. the
    clock on the top metal while an embedded macro stops below it) uses the
    design's top layer instead."""
    if d is None or not d.max_route_layer or d.max_route_layer not in pdk.layers:
        return script
    top = pdk.layer_index(d.max_route_layer)
    out = []
    for ln in script.splitlines():
        w = ln.split()
        if w[:1] == ["set_wire_rc"] and "-layer" in w:
            i = w.index("-layer") + 1
            if i < len(w) and w[i] in pdk.layers and pdk.layer_index(w[i]) > top:
                w[i] = d.max_route_layer
                ln = " ".join(w)
        out.append(ln)
    return "\n".join(out)


def rc_setup(pdk: Pdk, d: Design | None = None, corners=None) -> str:
    """Estimation RC: each corner's own script (scaled from its tech LEF when
    the PDK gives one), the PDK's otherwise."""
    corners = corners or pdk.analysis_corners()
    if len(corners) == 1:
        return _clamp_wire_rc(corners[0].rc_script or pdk.rc_script, pdk, d)
    out = []
    for c in corners:
        lines = [ln for ln in _clamp_wire_rc(c.rc_script or pdk.rc_script, pdk, d).splitlines() if ln.strip()]
        out += [f"{ln} -corner {c.name}" for ln in lines]
    return "\n".join(out)


def constraints_setup(pdk: Pdk, d: Design, library_slew: bool = False) -> list[str]:
    """The PDK's design rules and default boundary conditions, applied before
    the design SDC so that the SDC can override any of them. The boundary
    defaults (driving cell, output load) model on-chip neighbours of an
    embedded block, so they apply in macro mode only: a die's ports are bond
    pads, driven and loaded from the board."""
    c = dict(pdk.constraints or {})
    if library_slew:
        c.pop("max_transition", None)
    if d.mode != "macro":
        c.pop("driving_cell", None)
        c.pop("output_load", None)
    out = []
    for key, cmd in (("max_transition", "set_max_transition"), ("max_fanout", "set_max_fanout"),
                     ("max_capacitance", "set_max_capacitance")):
        if c.get(key) is not None:
            out.append(f"{cmd} {c[key]} [current_design]")
    clocks = set(d.clock_ports)
    if c.get("driving_cell"):
        cell, pin = c["driving_cell"]
        out.append(f"set ::flow_in {{}}\nforeach p [all_inputs] {{ if {{[get_name $p] ni {qlist(sorted(clocks))}}} "
                   f"{{ lappend ::flow_in $p }} }}\n"
                   f"if {{[llength $::flow_in]}} {{ set_driving_cell -lib_cell {cell} -pin {pin} $::flow_in }}")
    if c.get("output_load") is not None:
        out.append(f"if {{[llength [all_outputs]]}} {{ set_load {c['output_load']} [all_outputs] }}")
    return out


def _timing_setup(pdk: Pdk, d: Design, corners=None, *, sdc=None, library_slew=False) -> str:
    out = [*constraints_setup(pdk, d, library_slew), f"read_sdc {q(sdc or d.sdc)}", rc_setup(pdk, d, corners)]
    if pdk.dont_use:
        out.append(f"set_dont_use {qlist(pdk.dont_use)}")
    # cells with known layout problems and the design's own exclusions: never chosen
    # by sizing, buffering or CTS (cells not in the loaded Liberty are skipped)
    excl = list(pdk.exclude) + list(getattr(d, "dont_use", []) or [])
    if excl:
        out.append(f"set ::flow_excluded 0\nforeach c {qlist(excl)} {{ foreach lc [get_lib_cells -quiet */$c] "
                   "{ set_dont_use $lc; incr ::flow_excluded } }")
    # Liberty cells with no LEF master (e.g. extra cells in corner libraries)
    # must never be chosen by sizing, buffering or CTS
    out.append("set ::flow_no_master 0\n"
               "foreach c [get_lib_cells */*] { if {[[ord::get_db] findMaster [get_name $c]] eq \"NULL\"} "
               "{ set_dont_use $c; incr ::flow_no_master } }")
    for n in d.analog_nets:
        out.append(f"set_dont_touch [get_nets {q(n)}]")
    # port-to-port feedthroughs (no cell pin, e.g. a harness's loopback ties)
    # stay plain wires: buffering them would place the buffers in the rows,
    # far from the boundary pins they join
    out.append("set ::flow_feedthroughs 0\n"
               "foreach __n [[ord::get_db_block] getNets] { if {![$__n isSpecial] && [llength [$__n getITerms]] == 0 "
               "&& [llength [$__n getBTerms]] >= 2} { $__n setDoNotTouch 1; incr ::flow_feedthroughs } }")
    return "\n".join(out)


def floorplan_from_def(pdk: Pdk, def_path: str) -> str:
    return f"read_def -floorplan_initialize {q(def_path)}\n{pdk.tracks_script}"


def floorplan_die(pdk: Pdk, d: Design) -> str:
    """Die and core from the die box and core margin, or (no die) from the
    target utilization and aspect ratio with the margin around the core."""
    if not d.die:
        return (f"initialize_floorplan -utilization {d.core_utilization * 100:g} -aspect_ratio {d.aspect_ratio:g} "
                f"-core_space {d.core_margin:g} -site {pdk.site}\n{pdk.tracks_script}")
    x0, y0, x1, y1 = d.die
    m = d.core_margin
    return (f"initialize_floorplan -die_area {{{x0} {y0} {x1} {y1}}} "
            f"-core_area {{{x0 + m} {y0 + m} {x1 - m} {y1 - m}}} -site {pdk.site}\n{pdk.tracks_script}")


def place_pins(pdk: Pdk, random: bool = False) -> str:
    """Pins on the die edges (PDK pin layers): provisional at floorplan
    (``random``: cells not placed yet; this OpenROAD build dropped
    ``-random``, so the pins are spread by the placer's default), then again
    by the placed cells. Steps before placement (the macros step's
    congestion estimate) need pins with geometry."""
    if len(pdk.pin_layers) != 2:
        raise ValueError(f"PDK {pdk.name} has no route.pin_layers (horizontal, vertical) for automatic pins")
    h, v = pdk.pin_layers
    return f"place_pins -hor_layers {h} -ver_layers {v}"


def endcaps_and_taps(pdk: Pdk) -> str:
    """Endcaps left and right of every row and well taps, for the cells the PDK
    has (a library without them gets neither)."""
    out = []
    if pdk.endcap_cell:
        out.append(f"place_endcaps -left_edge {pdk.endcap_cell} -right_edge {pdk.endcap_cell}")
    if pdk.tap_cell:
        out.append(f"place_tapcells -master {pdk.tap_cell} -distance {pdk.tap_distance}")
    return "\n".join(out) or "# no endcap or tap cells in this PDK"


def global_connect(d: Design, pdk: Pdk) -> str:
    out = []
    for gp in pdk.power_pins:
        kind = {"power": " -power", "ground": " -ground"}.get(gp.get("kind", ""), "")
        out.append(f"add_global_connection -net {d.net(gp['net'])} -inst_pattern {{.*}} -pin_pattern {q(gp['pin'])}{kind}")
    for m in d.macros:
        for pat, net in m.supplies.items():
            net = d.net(net)
            kind = "-ground" if d.is_ground(net) else "-power"
            # a regular expression: the instance name matched literally (bank[0].u_ram)
            out.append(f"add_global_connection -net {net} -inst_pattern {q('^' + re.escape(m.inst) + '$')} "
                       f"-pin_pattern {q(pat)} {kind}")
    out.append("global_connect")
    return "\n".join(out)


def pdn(d: Design, pdk: Pdk, straps: list | None = None) -> str:
    """The power grid. ``straps``: the core straps to use instead of the
    design's (e.g. PDK defaults scaled to a small core)."""
    p = d.pdn
    out = [global_connect(d, pdk),
           f"set_voltage_domain -name CORE -power {d.net('VDD')} -ground {d.net('VSS')}",
           "define_pdn_grid -name grid -voltage_domains CORE",
           f"add_pdn_stripe -grid grid -layer {p.rail_layer} -width {p.rail_width} -followpins"]
    if p.rings:
        h, v = p.ring_layers
        pads = ""
        if p.ring_connect_to_pads:
            pads = " -connect_to_pads"
            if p.ring_pad_layers:
                pads += f" -connect_to_pad_layers {{{' '.join(p.ring_pad_layers)}}}"
        out.append(f"add_pdn_ring -grid grid -layers {{{h} {v}}} -widths {p.ring_width} -spacings {p.ring_spacing} "
                   f"-core_offsets {p.ring_core_offset}{pads}")
    ext = " -extend_to_core_ring" if p.rings else ""
    for s in (p.straps if straps is None else straps):
        out.append(f"add_pdn_stripe -grid grid -layer {s.layer} -width {s.width} -pitch {s.pitch} -offset {s.offset}{ext}")
    for a, b in p.connects:
        out.append(f"add_pdn_connect -grid grid -layers {{{a} {b}}}")
    for m in d.macros:
        g = q("mg_" + m.inst)
        if m.grid_stripes is not None:  # explicit macro grid (straps over the macro)
            out.append(f"define_pdn_grid -name {g} -voltage_domains CORE -macro -instances {q(m.inst)} "
                       f"-grid_over_pg_pins -halo {m.grid_halo}")
            for st in m.grid_stripes:
                out.append(f"add_pdn_stripe -grid {g} -layer {st.layer} -width {st.width} -pitch {st.pitch} "
                           f"-offset {st.offset}")
            for a, b in (m.grid_connects or []):
                out.append(f"add_pdn_connect -grid {g} -layers {{{a} {b}}}")
            continue
        connects = m.grid_connects if m.grid_connects is not None else p.macro_connects
        if not connects:
            continue  # no macro grid: supply pins stay logical (reported by physical())
        out.append(f"define_pdn_grid -name {g} -voltage_domains CORE -macro "
                   f"-instances {q(m.inst)} -halo {{{m.halo} {m.halo} {m.halo} {m.halo}}} -grid_over_boundary")
        for a, b in connects:
            out.append(f"add_pdn_connect -grid {g} -layers {{{a} {b}}}")
    out.append("pdngen")
    return "\n".join(out)


def fence(g: Group, box_dbu: tuple[int, int, int, int]) -> str:
    x0, y0, x1, y1 = box_dbu
    return f"""
set block [ord::get_db_block]
set r [odb::dbRegion_create $block {q(g.name)}]
if {{$r eq "NULL"}} {{ error {q("cannot create region " + g.name)} }}
odb::dbBox_create $r {x0} {y0} {x1} {y1}
$r setRegionType EXCLUSIVE
set grp [odb::dbGroup_create $r {q(g.name + '_grp')}]
set n 0
foreach inst [$block getInsts] {{
  if {{[string first {q(g.prefix)} [$inst getName]] == 0}} {{ $r addInst $inst; $grp addInst $inst; incr n }}
}}
if {{$n == 0}} {{ error {q(f"fence {g.name}: no instance matches prefix {g.prefix}")} }}
set n
"""


def cluster_group(g: Group) -> str:
    return f"""
set block [ord::get_db_block]
set grp [odb::dbGroup_create $block {q(g.name)}]
$grp setType PHYSICAL_CLUSTER
set n 0
foreach inst [$block getInsts] {{
  if {{[string first {q(g.prefix)} [$inst getName]] == 0}} {{ $grp addInst $inst; incr n }}
}}
if {{$n == 0}} {{ error {q(f"cluster {g.name}: no instance matches prefix {g.prefix}")} }}
set n
"""


def global_place(density: float, seed: int | None = None, timing_driven: bool = True,
                 keep_resize: bool = True, routability_driven: bool = False) -> str:
    flags = ["-timing_driven"] if timing_driven else []
    if routability_driven:
        flags.append("-routability_driven")
    if timing_driven and not keep_resize:
        flags.append("-keep_resize_below_overflow 0")
    flags += [f"-density {density}"]
    if seed is not None:
        flags.append(f"-random_seed {seed}")
    return f"global_placement {' '.join(flags)}"


def margins(pdk: Pdk | None, hold: str | None = None) -> str:
    """repair_timing slack margins from the PDK (setup, and hold for the
    given stage: ``hold_place`` or ``hold_route``)."""
    m = getattr(pdk, "repair_margins", None) or {}
    out = f" -setup_margin {m['setup']}" if m.get("setup") else ""
    if hold and m.get(hold):
        out += f" -hold_margin {m[hold]}"
    return out


def design_margins(pdk: Pdk | None) -> str:
    """repair_design slew/capacitance margins (percent) from the PDK: repair
    against estimated parasitics leaves room for the extracted ones."""
    m = getattr(pdk, "repair_margins", None) or {}
    return "".join(f" -{k}_margin {m[k]}" for k in ("slew", "cap") if m.get(k))


def repair_after_place(max_util: float | None = None, pdk: Pdk | None = None) -> str:
    util = f" -max_utilization {max_util}" if max_util else ""
    return ("estimate_parasitics -placement\n"
            f"repair_design{util}{design_margins(pdk)}\n"
            "repair_timing -setup -repair_tns 100" + margins(pdk) + util)


def detailed_place() -> str:
    return "detailed_placement\ncheck_placement -verbose"


def cts(pdk: Pdk, clk_nets: list[str], sink_clustering_size=None, sink_clustering_levels=None, distance_between_buffers=None) -> str:
    # with several corner libraries loaded, this CTS build cannot resolve its
    # dummy-load cells (CTS-0118, even with -library), so dummy loads are off
    lib = ("[expr {[info exists ::flow_cts_library] ? "
           "\"-library $::flow_cts_library -dont_use_dummy_load\" : {}}]")
    ndr = f" -apply_ndr {pdk.cts_apply_ndr}" if getattr(pdk, "cts_apply_ndr", "") else ""
    clustering = "".join(f" -{k} {int(v)}" for k, v in
                         (("sink_clustering_size", sink_clustering_size),
                          ("sink_clustering_levels", sink_clustering_levels)) if v is not None)
    if distance_between_buffers is not None:
        clustering += f" -distance_between_buffers {float(distance_between_buffers)}"
    # -clk_nets is split on spaces: names go space-joined, not list-quoted ({gpio_in[0]})
    return (f"clock_tree_synthesis -root_buf {pdk.cts_root_buffer} -buf_list {qlist(pdk.cts_buffers)} "
            f"-clk_nets [join {qlist(clk_nets)} {{ }}] -sink_clustering_enable{clustering}{ndr} {{*}}{lib}\n"
            "set_propagated_clock [all_clocks]\n"
            "::flow_set_flag flow_post_cts\n"
            "estimate_parasitics -placement\n"
            "repair_timing -setup -hold -repair_tns 100" + margins(pdk, "hold_place"))


def filler(pdk: Pdk, cells: list[str] | None = None) -> str:
    """Fill the empty sites: ``cells`` (default the PDK's list, decaps first)."""
    return f"filler_placement {qlist(cells or pdk.fill_cells)}"


def routing_layers(d: Design, pdk: Pdk | None = None, margin: float = 0.0) -> str:
    """Routing layer range and global-route capacity adjustments (PDK values,
    overridden per layer by the design; ``margin`` raises every layer to at
    least that reduction)."""
    rng = f"{d.min_route_layer}-{d.max_route_layer}"
    clk = rng
    cmin = getattr(pdk, "clock_min_layer", "") if pdk else ""
    if cmin and cmin in pdk.layers and d.max_route_layer in pdk.layers:
        lo = max(pdk.layer_index(cmin), pdk.layer_index(d.min_route_layer))
        if lo <= pdk.layer_index(d.max_route_layer):  # clock nets from the PDK's clock layer up
            clk = f"{pdk.layers[lo]}-{d.max_route_layer}"
    out = [f"set_routing_layers -signal {rng} -clock {clk}"]
    adj = dict(getattr(pdk, "layer_adjustments", {}) or {})
    adj.update(d.layer_adjustments)
    if margin:
        layers = list(pdk.layers) if pdk else []
        if d.min_route_layer in layers and d.max_route_layer in layers:
            for ly in layers[layers.index(d.min_route_layer):layers.index(d.max_route_layer) + 1]:
                adj[ly] = max(adj.get(ly, 0.0), margin)
    for ly, v in adj.items():
        out.append(f"set_global_routing_layer_adjustment {ly} {v}")
    return "\n".join(out)


def analog_ndr(d: Design) -> str:
    w = " ".join(f"{k} {v}" for k, v in d.analog_ndr["width"].items())
    s = " ".join(f"{k} {v}" for k, v in d.analog_ndr["spacing"].items())
    out = [f"create_ndr -name ANALOG -width {{{w}}} -spacing {{{s}}}"]
    for n in d.analog_nets:
        out.append(f"assign_ndr -ndr ANALOG -net {q(n)}")
        out.append(f"[::flow_net {q(n)}] setSigType ANALOG")
    return "\n".join(out)


def route_subset(nets: list[str], drc_report: str, iters: int = 20, congestion_report: str | None = None) -> str:
    cong = f" -congestion_report_file {q(congestion_report)}" if congestion_report else ""
    return (ROUTE_NETS + f"::flow_route_nets {qlist(nets)}\n"
            f"global_route{cong}\n"
            f"detailed_route -droute_end_iter {iters} -output_drc {q(drc_report)}")


# The router's net list is given as database nets (grt::add_net_to_route):
# set_nets_to_route resolves names as STA patterns, which silently drops nets
# whose names carry escapes (Yosys bus bits like state\[0\]); a net repair
# later buffers then has no route and OpenROAD crashes.
ROUTE_NETS = """
proc ::flow_route_nets {names} {
  set block [ord::get_db_block]
  foreach n $names {
    set net [$block findNet $n]
    if {$net eq "NULL"} { error "flow_route_nets: no net named $n" }
    grt::add_net_to_route $net
  }
}
"""
DIGITAL_NETS = ROUTE_NETS + """
set ::flow_digital {}
foreach net [[ord::get_db_block] getNets] {
  if {[$net isSpecial] || [$net getSigType] in {POWER GROUND ANALOG}} continue
  if {[llength [$net getITerms]] + [llength [$net getBTerms]] < 2} continue
  lappend ::flow_digital [$net getName]
}
"""


def global_route_digital(congestion_report: str) -> str:
    """Global route of every non-special signal net, with a congestion report."""
    return (DIGITAL_NETS + "::flow_route_nets $::flow_digital\n"
            f"global_route -congestion_iterations 50 -congestion_report_file {q(congestion_report)}")


def detailed_route_only(drc_report: str, iters: int = 64) -> str:
    return f"detailed_route -droute_end_iter {iters} -output_drc {q(drc_report)}"


def route_digital(drc_report: str, iters: int = 64, guide: str | None = None) -> str:
    """Route every non-special signal net. Frozen (special) analog nets are
    not in the list, so the router only sees their shapes as obstacles."""
    g = f" -guide_file {q(guide)}" if guide else ""
    return (DIGITAL_NETS + "::flow_route_nets $::flow_digital\n"
            f"global_route -congestion_iterations 50{g}\n"
            f"detailed_route -droute_end_iter {iters} -output_drc {q(drc_report)}")


def spef_paths(pdk: Pdk, base: str) -> dict[str, str]:
    """SPEF file per corner. One extraction per distinct RCX rule file, shared
    by the corners that use it: the typical corner's goes to ``base``, the
    others to ``base`` with ``_<rc corner>`` before ``.spef``."""
    corners = pdk.signoff_corners()  # every corner's SPEF, for signoff STA
    root = base[:-5] if base.endswith(".spef") else base
    typ = pdk.typical()
    by_rule = {typ.rcx_rules: base}
    for c in corners:
        if c.rcx_rules not in by_rule:
            tag = c.rc if c.rc and c.rc != typ.rc else c.name
            by_rule[c.rcx_rules] = f"{root}_{tag}.spef"
    if len(set(by_rule.values())) != len(by_rule):
        raise ValueError("two RCX rule files would write the same SPEF; give the corners distinct rc names")
    return {c.name: by_rule[c.rcx_rules] for c in corners}


def rcx(pdk: Pdk, spef: str) -> str:
    """OpenRCX: one extraction per RCX rule file (of all corners), written
    once and read into every corner of the session that uses those rules."""
    corners = pdk.analysis_corners()
    every = pdk.signoff_corners()
    paths = spef_paths(pdk, spef)
    out = []
    for path in dict.fromkeys(paths.values()):
        users = [c for c in corners if paths[c.name] == path]
        rules = next(c for c in every if paths[c.name] == path).rcx_rules
        out += ["define_process_corner -ext_model_index 0 X",
                f"extract_parasitics -ext_model_file {q(rules)}",
                f"write_spef {q(path)}"]
        for c in users:
            out.append(f"read_spef {'-corner ' + c.name + ' ' if len(corners) > 1 else ''}{q(path)}")
    out.append("set ::flow_spef_loaded 1")
    return "\n".join(out)


def extract(pdk: Pdk, spef: str, def_out: str) -> str:
    return rcx(pdk, spef) + f"\nwrite_def {q(def_out)}"


def place_instance(name: str, x: int, y: int, orient: str, status: str = "FIRM") -> str:
    # FIRM/LOCKED instances cannot be moved; unfix, move, refix
    return (f"set i [::flow_inst {q(name)}]; $i setPlacementStatus PLACED; $i setOrient {orient}; "
            f"$i setLocation {x} {y}; $i setPlacementStatus {status}")
