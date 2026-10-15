"""Named scores. Higher is better. A score returns None when the metrics it
needs are missing, and such an attempt can never be selected."""
from __future__ import annotations


def setup_wns(m):
    """Setup worst negative slack (ns)."""
    return m.get("wns")


def hpwl(m):
    """Shortest half-perimeter wirelength."""
    v = m.get("hpwl_um")
    return None if v is None else -v


def drc_then_wns(m):
    """Fewest DRC violations, then setup slack."""
    if m.get("drc") is None or m.get("wns") is None:
        return None
    return (-m["drc"], m["wns"])


def wns_then_hpwl(m):
    """Setup slack (to 1 ps), then wirelength."""
    if m.get("wns") is None or m.get("hpwl_um") is None:
        return None
    return (round(m["wns"], 3), -m["hpwl_um"])


def macro_hpwl(m):
    """Shortest wirelength of macro nets (placed terminals only)."""
    v = m.get("macro_hpwl_um")
    return None if v is None else -v


def macro_quick(m):
    """Least congestion after a quick placement + global route, then the lowest
    macro cost (quick-placement HPWL + weighted analog-net HPWL)."""
    if m.get("quick_overflow") is None or m.get("macro_cost_um") is None:
        return None
    return (-m["quick_overflow"], -m["macro_cost_um"])


def routability_then_wns(m):
    """Least global-route overflow (congestion check after placement), then
    setup slack (to 1 ps), then wirelength."""
    if m.get("grt_overflow") is None or m.get("wns") is None or m.get("hpwl_um") is None:
        return None
    return (-m["grt_overflow"], round(m["wns"], 3), -m["hpwl_um"])


def drc_then_slack(m):
    """Fewest DRC violations, then the worse of setup and hold slack."""
    if m.get("drc") is None or m.get("wns") is None:
        return None
    worst = m["wns"] if m.get("whs") is None else min(m["wns"], m["whs"])
    return (-m["drc"], worst)


def auto(m):
    """Setup slack when the design is constrained, else wirelength. Works for
    designs without clocks (where slack is undefined)."""
    if m.get("wns") is not None:
        return (1, m["wns"], -(m.get("hpwl_um") or 0.0))
    if m.get("hpwl_um") is not None:
        return (0, -m["hpwl_um"])
    if m.get("synth_area_um2") is not None:  # synthesis: smaller is better
        return (0, -m["synth_area_um2"])
    return None


def synth_area(m):
    """Smallest synthesized area."""
    return None if m.get("synth_area_um2") is None else -m["synth_area_um2"]


SCORES = {f.__name__: f for f in (setup_wns, hpwl, drc_then_wns, drc_then_slack, wns_then_hpwl, macro_hpwl,
                                           macro_quick, routability_then_wns, synth_area, auto)}


def get(name):
    try:
        return SCORES[name]
    except KeyError:
        raise KeyError(f"unknown score {name!r}; known: {sorted(SCORES)}") from None
