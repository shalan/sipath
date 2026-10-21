"""The Netgen adapter. Netgen runs today inside the precheck image, as
ChipFoundry's cf-precheck LVS (``sipath.precheck``); SiPath's own LVS is
KLayout's. A Netgen LVS engine beside it is planned; until then this module
holds how the locked precheck image's Netgen is probed."""
from __future__ import annotations

VERSION = ("echo quit | netgen -batch", r"Netgen (\S+)")
