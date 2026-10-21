"""Compatibility alias: the module lives in sipath.tools.openroad.session (the OpenROAD adapter).
Library code imports it from there; this name stays for plugins and older imports."""
import sys

from .tools.openroad import session as _module

sys.modules[__name__] = _module
