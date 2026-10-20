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
    if m.get("synth_area_um2") is not None:  # synthesis: met timing first, then smaller
        return synth_wns_then_area(m)
    if m.get("chip_check_pass") is not None:  # chip integration: an assembled chip that passes its checks
        return (1, 0) if m["chip_check_pass"] else None
    if m.get("precheck_pass") is not None:  # a tapeout precheck: passing, then the fewest failed checks
        return (1 if m["precheck_pass"] else 0, -(m.get("precheck_failed") or 0))
    return None


def synth_area(m):
    """Smallest synthesized area."""
    return None if m.get("synth_area_um2") is None else -m["synth_area_um2"]


def synth_wns_then_area(m):
    """Synthesis: met timing (pre-placement estimate) first, then the smallest
    area; among variants missing timing, the best slack."""
    if m.get("synth_area_um2") is None:
        return None
    wns = m.get("synth_wns_ns")
    if wns is None:
        return (1, -m["synth_area_um2"])
    return (1, -m["synth_area_um2"]) if wns >= 0 else (0, wns)


def _front(cands):
    """(slack, area) Pareto front of attempts: none faster and smaller."""
    return [c for c in cands if not any(
        o.metrics["synth_area_um2"] <= c.metrics["synth_area_um2"] and o.metrics["synth_wns_ns"] >= c.metrics["synth_wns_ns"]
        and (o.metrics["synth_area_um2"] < c.metrics["synth_area_um2"] or o.metrics["synth_wns_ns"] > c.metrics["synth_wns_ns"])
        for o in cands if o is not c)]


def _knee(cands, clip_ns=None):
    """The front point closest to the ideal (best slack, least area) with both
    axes normalized over the front; None under three front points."""
    best = max(c.metrics["synth_wns_ns"] for c in cands)
    if clip_ns:
        cands = [c for c in cands if c.metrics["synth_wns_ns"] >= best - clip_ns]
    front = _front(cands)
    if len(front) < 3:
        return None
    w = [c.metrics["synth_wns_ns"] for c in front]
    a = [c.metrics["synth_area_um2"] for c in front]
    if max(w) - min(w) <= 1e-9 or max(a) - min(a) <= 1e-9:
        return None

    def dist(c):
        wn = (max(w) - c.metrics["synth_wns_ns"]) / (max(w) - min(w))
        an = (c.metrics["synth_area_um2"] - min(a)) / (max(a) - min(a))
        return (wn * wn + an * an) ** 0.5
    return min(front, key=lambda c: (dist(c), -c.metrics["synth_wns_ns"], _prio(c)))


def _prio(c):
    from .recipes import priority
    return priority((c.params or {}).get("abc") or "delay_map_resyn")


def synth_select(attempts, fallback: str = "knee"):
    """Synthesis selector (shalan/synth_flow's rule): the least area among the
    attempts meeting timing (synth_wns_ns >= 0). If none meets, ``fallback``:
    ``knee`` (the knee of the slack/area front, else the best slack),
    ``best_slack`` or ``least_area``; ties by recipe rank. Returns (chosen
    attempt, rationale)."""
    cands = [a for a in attempts if a.metrics.get("synth_area_um2") is not None]
    if not cands:
        return None, "no synthesized netlist"
    timed = [a for a in cands if a.metrics.get("synth_wns_ns") is not None]
    if not timed:
        best = min(cands, key=lambda a: (a.metrics["synth_area_um2"], _prio(a)))
        return best, "no timing estimate: least area"
    meeting = [a for a in timed if a.metrics["synth_wns_ns"] >= 0]
    if meeting:
        best = min(meeting, key=lambda a: (a.metrics["synth_area_um2"], -a.metrics["synth_wns_ns"], _prio(a)))
        return best, f"least area among {len(meeting)}/{len(timed)} meeting timing"
    fastest = max(timed, key=lambda a: (a.metrics["synth_wns_ns"], -a.metrics["synth_area_um2"], -_prio(a)))
    if fallback == "best_slack":
        return fastest, "none meets timing: best slack (synth_fallback)"
    if fallback == "least_area":
        small = min(timed, key=lambda a: (a.metrics["synth_area_um2"], -a.metrics["synth_wns_ns"], _prio(a)))
        return small, (f"none meets timing: least area (synth_fallback; fastest "
                       f"{fastest.metrics['synth_wns_ns']:+.3f} ns at {fastest.metrics['synth_area_um2']:.0f} um2)")
    knee = _knee(timed, timed[0].metrics.get("synth_period_ns"))
    if knee is not None:
        return knee, (f"none meets timing: knee of the slack/area front (fastest "
                      f"{fastest.metrics['synth_wns_ns']:+.3f} ns at {fastest.metrics['synth_area_um2']:.0f} um2)")
    return fastest, "none meets timing: best slack"


synth_select.selector = True  # chooses among all attempts (Flow._select), not one attempt's number

SCORES = {f.__name__: f for f in (setup_wns, hpwl, drc_then_wns, drc_then_slack, wns_then_hpwl, macro_hpwl,
                                           macro_quick, routability_then_wns, synth_area, synth_wns_then_area, synth_select, auto)}


def get(name):
    try:
        return SCORES[name]
    except KeyError:
        raise KeyError(f"unknown score {name!r}; known: {sorted(SCORES)}") from None
