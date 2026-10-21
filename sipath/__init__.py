"""SiPath: physical-design flow infrastructure (attempt graph, step library, tool
adapters, plugins, MCP) with a reference RTL-to-GDS flow, driven by design files.

Public API:
    Flow, StepRun, Attempt, Ctx, StepFailed, EcoRefused   (sipath.scheduler)
    OpenRoadSession, FakeSession, find_openroad           (sipath.session)
    Design, Macro, Pad, Group, Datapath, PdnSpec, Strap   (sipath.design)
    load_pdk                                              (sipath.pdk: PDK description files)
    designfile.load / designfile.run                      (design description files)
    steps (synth, floorplan, macros, physical, place, cts, route_analog,
           route_digital, route_repair, extract, signoff), eco.run_eco
"""
__version__ = "0.1.0"  # pyproject.toml

from .design import Datapath, Design, Group, Macro, MissingInput, Pad, PdnSpec, Strap
from .pdk import load as load_pdk
from .scheduler import Attempt, Ctx, EcoRefused, Flow, StepFailed, StepRun
from .tools.openroad.session import FakeSession, OpenRoadSession, TclError, find_openroad

__all__ = [
    "Attempt", "Ctx", "Datapath", "Design", "EcoRefused", "FakeSession", "Flow", "Group", "Macro",
    "MissingInput", "OpenRoadSession", "Pad", "PdnSpec", "StepFailed", "StepRun", "Strap", "TclError",
    "find_openroad", "load_pdk",
]
