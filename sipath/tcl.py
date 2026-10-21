"""Compatibility alias: the module lives in sipath.tools.openroad.tcl (the OpenROAD adapter).
Library code imports it from there; this name stays for plugins and older imports."""
import sys

from .tools.openroad import tcl as _module

sys.modules[__name__] = _module
