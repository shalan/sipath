"""Regression limits (P1.3): a run's key results against a per-design limits file.

    python3 -m sipath limits <workdir> <design> [--file limits.yaml]   check (exit 1 on a violation)
    python3 -m sipath limits <workdir> <design> --suggest [--until step]   a limits entry from this run

A limits file maps a design name to its limits; the default is
``sipath/tests/limits.yaml`` (the live tests check their runs against it):

    gcd8:
      until: signoff          # the step the run is measured up to (default: signoff, else the last step)
      min_wns_ns: 2.9         # worst setup slack (signoff: worst of every corner)
      min_whs_ns: 0.0         # worst hold slack
      max_drc: 0              # signoff DRC count (else the router's)
      max_wirelength_um: 3800
      max_insts: 600
      max_runtime_s: 180      # wall time from the first attempt to the end of `until`
      signoff_pass: true
      lvs_match: true

Designs with analog nets can also bound ``max_analog_coupling_ff`` (the worst
analog net's coupling to digital nets) and ``max_analog_aggressor_ff``.
``min_*`` / ``max_*`` bound a number; any other key must equal the run's value.
A limit whose quantity the run does not have is a violation: a missing result
is a failed run. ``SIPATH_LIMITS_IGNORE=max_runtime_s`` (comma-separated keys)
skips limits that belong to another machine, e.g. wall time in CI.
"""
from __future__ import annotations

import json
import math
import os

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests", "limits.yaml")


def run_metrics(workdir: str, until: str | None = None) -> dict:
    """The run's results up to step ``until`` (default: signoff when it ran, else
    the last step): timing, DRC, LVS, wirelength, instances, wall time."""
    with open(os.path.join(workdir, "graph.json")) as f:
        g = json.load(f)
    order = list((g.get("steps") or {}).keys())
    by_id = {a["id"]: a for a in g["attempts"]}
    if until is None:
        until = "signoff" if "signoff" in order else (order[-1] if order else None)
    if until not in order:
        return {"error": f"step {until} did not run"}
    steps = order[:order.index(until) + 1]
    sel = [by_id[g["steps"][s]["selected"]] for s in steps if g["steps"][s].get("selected") in by_id]
    if len(sel) < len(steps):
        return {"error": f"a step up to {until} has no selected attempt"}
    m = {}
    for a in sel:  # later steps override earlier ones
        m.update(a.get("metrics") or {})
    names = set(steps)
    atts = [a for a in g["attempts"] if a["step"] in names and a.get("started_at")]
    end = max((a.get("finished_at") or 0) for a in sel)
    out = {"wns_ns": m.get("wns"), "whs_ns": m.get("whs"), "drc": m.get("drc"), "wirelength_um": m.get("wirelength_um"),
           "insts": m.get("insts"), "analog_coupling_ff": m.get("analog_coupling_ff"),
           "analog_aggressor_ff": m.get("analog_aggressor_ff"), "runtime_s": round(end - min(a["started_at"] for a in atts), 1) if atts else None}
    so = (sel[-1].get("outputs") or {}).get("signoff") if until == "signoff" else None
    if so and os.path.isfile(so):
        with open(so) as f:
            s = json.load(f)
        corners = (s.get("timing") or {}).get("corners") or {}
        wns = [c["wns"] for c in corners.values() if c.get("wns") is not None]
        whs = [c["whs"] for c in corners.values() if c.get("whs") is not None]
        out.update(wns_ns=min(wns) if wns else out["wns_ns"], whs_ns=min(whs) if whs else out["whs_ns"],
                   drc=(s.get("drc") or {}).get("count", out["drc"]), lvs_match=(s.get("lvs") or {}).get("match"),
                   signoff_pass=bool(s.get("pass")))
    return out


def check(workdir: str, entry: dict) -> list[str]:
    """Violations of ``entry`` (one design's limits) by the run in ``workdir``."""
    got = run_metrics(workdir, entry.get("until"))
    if "error" in got:
        return [got["error"]]
    bad = []
    ignore = {k.strip() for k in os.environ.get("SIPATH_LIMITS_IGNORE", "").split(",") if k.strip()}
    for key, want in entry.items():
        if key == "until" or key in ignore:  # e.g. max_runtime_s on another machine (CI)
            continue
        kind, _, name = key.partition("_") if key.startswith(("min_", "max_")) else ("eq", "", key)
        v = got.get(name)
        if v is None:
            bad.append(f"{name}: no value in the run (limit {key} {want})")
        elif kind == "min" and v < want:
            bad.append(f"{name} {v} below the minimum {want}")
        elif kind == "max" and v > want:
            bad.append(f"{name} {v} above the maximum {want}")
        elif kind == "eq" and v != want:
            bad.append(f"{name} {v}, expected {want}")
    return bad


def suggest(workdir: str, until: str | None = None) -> dict:
    """A limits entry from a good run: setup slack may lose a quarter (at least
    0.25 ns) but stays met when met, hold stays met, DRC stays at the run's
    count, wirelength and instances may grow 10%, analog coupling 20% + 0.5 fF,
    wall time may double (+30 s: machine load)."""
    m = run_metrics(workdir, until)
    if "error" in m:
        raise ValueError(m["error"])
    e = {} if until is None else {"until": until}
    if m["wns_ns"] is not None:
        w = m["wns_ns"]
        e["min_wns_ns"] = max(0.0, round(min(w * 0.75, w - 0.25), 2)) if w >= 0 else round(w - 0.25, 2)
    if m["whs_ns"] is not None:
        e["min_whs_ns"] = 0.0 if m["whs_ns"] >= 0 else round(m["whs_ns"] - 0.05, 2)
    if m["drc"] is not None:
        e["max_drc"] = int(m["drc"])
    for k in ("wirelength_um", "insts"):
        if m.get(k):
            e[f"max_{k}"] = int(math.ceil(round(m[k] * 1.10, 6)))
    for k in ("analog_coupling_ff", "analog_aggressor_ff"):  # +20 % and 0.5 fF: routing varies a little
        if m.get(k) is not None:
            e[f"max_{k}"] = round(m[k] * 1.2 + 0.5, 2)
    if m["runtime_s"]:
        e["max_runtime_s"] = int(math.ceil(round(m["runtime_s"] * 2 + 30, 6)))
    for k in ("signoff_pass", "lvs_match"):
        if m.get(k) is not None:
            e[k] = m[k]
    return e


def load(path: str | None = None) -> dict:
    from .pdk import read_file
    return read_file(path or DEFAULT) or {}


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("-")]
    if len(args) < 2:
        print(__doc__)
        return 2
    workdir, design = args[0], args[1]
    path = argv[argv.index("--file") + 1] if "--file" in argv else None
    if "--suggest" in argv:
        e = suggest(workdir, argv[argv.index("--until") + 1] if "--until" in argv else None)
        print(f"{design}:\n" + "\n".join(f"  {k}: {json.dumps(v)}" for k, v in e.items()))
        return 0
    limits = load(path)
    if design not in limits:
        print(f"no limits for {design} in {path or DEFAULT}")
        return 2
    m = run_metrics(workdir, limits[design].get("until"))
    bad = check(workdir, limits[design])
    print(json.dumps(m, indent=1))
    print("\n".join(bad) if bad else f"{design}: within limits")
    return 1 if bad else 0
