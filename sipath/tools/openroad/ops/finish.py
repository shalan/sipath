"""Finishing: metal fill, the hardened-macro views, parasitics for the PPA summary."""
from __future__ import annotations

from ..tcl import q, tcl_parse, tcl_words


def density_fill(s, rules: str, area: str = ""):
    """``density_fill`` with a rules file (``area``: the -area option text, or empty)."""
    s.run(f"density_fill -rules {q(rules)}{area}")


def fill_counts(s) -> list:
    """[layer, count, ...] of the fill shapes, parsed."""
    return tcl_parse(s.value("set c [dict create]; foreach f [[ord::get_db_block] getFills] "
                             "{ dict incr c [[$f getTechLayer] getName] }; set c"))


def write_abstract_lef(s, lef: str, bloat_occupied_layers: bool):
    s.run(f"write_abstract_lef{' -bloat_occupied_layers' if bloat_occupied_layers else ''} {q(lef)}")


def write_netlists(s, netlist: str, power_netlist: str):
    s.run(f"write_verilog {q(netlist)}\nwrite_verilog -include_pwr_gnd {q(power_netlist)}")


def read_spef(s, spef: str, corner: str | None = None):
    s.run(f"read_spef{f' -corner {corner}' if corner else ''} {q(spef)}")


def write_timing_model(s, lib: str, library_name: str, cell: str, scene: str | None = None):
    s.run(f"write_timing_model{f' -scene {scene}' if scene else ''} -library_name {q(library_name)} "
          f"-cell_name {q(cell)} {q(lib)}")


def signal_ports(s) -> set[str]:
    """Top-level ports not on a special (supply) net."""
    return set(tcl_words(s.value("set o {}; foreach b [[ord::get_db_block] getBTerms] "
                                 "{ if {![[$b getNet] isSpecial]} { lappend o [$b getName] } }; set o")))


def load_extracted_parasitics(s, spef: str, corner: str | None = None):
    """An extracted SPEF as the parasitics timing uses (``::flow_spef_loaded``)."""
    s.run(f"read_spef {'-corner ' + corner + ' ' if corner else ''}{q(spef)}\nset ::flow_spef_loaded 1")


def timing_prep(s):
    """The flow helpers' default parasitics (placement estimates when nothing better is loaded)."""
    s.run("::flow_timing_prep")
