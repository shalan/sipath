"""Power grid: connectivity and IR checks, global supply connections."""
from __future__ import annotations

from .. import tcl


def check_grid(s, net: str, has_terminals: bool):
    return s.run(f"check_power_grid -net {net}" + ("" if has_terminals else " -dont_require_terminals"), check=False)


def ir_drop(s, net: str, voltage: float, has_terminals: bool):
    src = "" if has_terminals else " -source_type STRAPS"
    return s.run(f"set_pdnsim_net_voltage -net {net} -voltage {voltage}\nanalyze_power_grid -net {net}{src}",
                 check=False)


def global_connect(s, design, pdk):
    s.run(tcl.global_connect(design, pdk))
