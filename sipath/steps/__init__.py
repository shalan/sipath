"""Step functions for ``Flow.step``. Each takes a ``Ctx`` and drives the
attempt's OpenROAD session. Nothing here fabricates a number: metrics are
read from the session or from files that this process wrote.

One module per stage: ``floorplan`` (floorplan, macros, physical), ``place``
(place, cts), ``analog_route``, ``route`` (route_digital, route_repair),
``finish`` (fill, extract, signoff, views, integrate, ppa), with ``common``
and ``structure`` helpers. Every name stays importable from ``sipath.steps``."""
from __future__ import annotations

from .. import params, registry
from ..synth import synth  # tool step: Yosys, no OpenROAD process
from .common import (  # noqa: F401
    _drt_numbers,
    _orient_xy,
    macro_supply_pins,
    grid_nets,
    _via_array,
    connect_supplies,
    _no_supply_shorts,
    power_checks,
    clock_nets,
    check_placement,
    tie_max_fanout,
    reports,
    design_rule_counts,
    _global_route,
    routability_check,
    PPA_TCL,
    ppa_summary,
    antenna_loop,
    _check_unrouted,
    check_antennas,
    _timing_gate,
    min_area_repair,
    run_rcx,
    reconnect_supplies,
)
from .structure import (  # noqa: F401
    _snap_box,
    fence_violations,
    enforce_fences,
    compact_cluster,
    cluster_spread,
    _dp_tap,
    reabut_all,
    datapath_check,
)
from .floorplan import (  # noqa: F401
    floorplan,
    macros,
    fit_straps,
    rows_without_tap,
    physical,
)
from .place import (  # noqa: F401
    place,
    _analog_terms,
    cts,
)
from .analog_route import (  # noqa: F401
    ROW_BLOCKAGES,
    SUPPLY_PARK,
    _launch_pin,
    SUPPLY_UNPARK,
    shield_nets,
    route_analog,
    analog_coupling,
)
from .route import (  # noqa: F401
    route_digital,
    route_repair,
)
from .adapt import adapt_place
from .finish import (  # noqa: F401
    check_step,
    precheck,
    fill,
    views,
    integrate,
    ppa,
    extract,
    signoff,
)

STEPS = registry.STEPS  # the registry's table: plugins add to the same dict
STEPS.update({
    "synth": synth, "floorplan": floorplan, "macros": macros, "physical": physical, "place": place, "cts": cts,
    "route_analog": route_analog, "route_digital": route_digital, "route_repair": route_repair, "fill": fill, "extract": extract, "views": views, "integrate": integrate, "ppa": ppa,
    "precheck": precheck,
    "signoff": signoff,
})

STEPS.update({name: check_step(name) for name in params.CHECK_STEPS})  # `- step: drc`, `- step: lvs`, ...


def _signoff_needs(checks):
    """Routing always; extracted parasitics when timing is among the checks (they read every corner's SPEF)."""
    timing = {"setup", "hold", "drv", "annotation", "unconstrained"}
    return lambda p: {"routed"} | ({"parasitics"} if timing & set(p.get("checks") or checks) else set())


# what each built-in step needs from its parent chain and what it adds (sipath.scheduler.CAPABILITIES)
CAPS = {
    "synth": ((), ("netlist",)),
    "floorplan": (("netlist",), ("floorplan",)),
    "macros": (("floorplan",), ("macros_placed",)),
    "physical": (("floorplan",), ("power_grid",)),
    "place": (("floorplan", "power_grid"), ("placed",)),
    "cts": (("placed",), ("clock_tree",)),
    "route_analog": (("placed",), ("analog_routed",)),
    "route_digital": (("placed",), ("routed",)),
    "route_repair": (("routed",), ()),
    "fill": (("routed",), ("filled",)),
    "extract": (("routed",), ("parasitics",)),
    "signoff": (_signoff_needs(params.SIGNOFF_CHECKS), ("signoff", "gds", "powered_netlist")),
    "views": (("signoff", "gds", "parasitics"), ("views",)),
    "integrate": (("gds",), ("chip_gds",)),
    "precheck": (("gds", "powered_netlist"), ()),
    "ppa": (("placed",), ()),
}
for _name, _checks in params.CHECK_STEPS.items():  # a check step streams the GDS when one of its checks reads it
    _gds = bool({"drc", "density", "lvs", "lvs_full", "magic_drc", "xor"} & set(_checks))
    CAPS[_name] = (_signoff_needs(_checks), ("powered_netlist",) + (("gds",) if _gds else ()))
for _name, (_req, _prov) in CAPS.items():
    STEPS[_name].requires = _req if callable(_req) else frozenset(_req)
    STEPS[_name].provides = frozenset(_prov)
registry.ADAPTERS.setdefault("place", adapt_place)  # adaptive rounds: hotspots -> the next placements

# parameters each step reads from its variant dict; anything else is a mistake
# parameters are declared (type, default, doc) in sipath.params
STEP_PARAMS = {step: set(decl) for step, decl in params.DECLARED.items() if step in STEPS}


def check_params(step: str, variants) -> None:
    """Raise ValueError for a variant that is not a dict, names a parameter
    the step does not declare, or gives a value of the wrong type or range."""
    if step not in STEPS or step not in params.DECLARED:  # live tables: plugin steps included
        raise ValueError(f"unknown step {step!r}; known: {sorted(STEPS)}")
    if variants is None:
        return
    if not isinstance(variants, list) or not variants:
        raise ValueError(f"{step}: variants must be a non-empty list of parameter dicts")
    for k, v in enumerate(variants):
        if not isinstance(v, dict):
            raise ValueError(f"{step}: variant {k} is not a dict: {v!r}")
        try:
            params.check(step, v)
        except ValueError as e:
            raise ValueError(f"variant {k}: {e}") from e