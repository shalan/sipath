"""Compatibility alias: the module lives in sipath.tools.native."""
import sys

from .tools import native as _module

sys.modules[__name__] = _module
