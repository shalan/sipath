"""Global-route congestion reports.

``global_route -congestion_report_file`` writes one entry per congested gcell
region (only when congestion is left)::

    violation type: Horizontal congestion
        srcs: net:a net:b net:c
        comment: capacity:2 usage:3 congestion:1
        bbox = (96.6000, 13.8000) - (103.5000, 20.7000) on Layer -

``parse`` sums them up; a missing file means no congestion.
"""
from __future__ import annotations

import os
import re
from collections import Counter

ENTRY = re.compile(r"violation type: (?P<kind>[^\n]*)\n\s*srcs:(?P<srcs>[^\n]*)\n\s*comment: capacity:(?P<cap>-?\d+) "
                   r"usage:(?P<use>-?\d+) congestion:(?P<over>-?\d+)\n\s*bbox = \((?P<x0>[-\d.]+), (?P<y0>[-\d.]+)\) - "
                   r"\((?P<x1>[-\d.]+), (?P<y1>[-\d.]+)\)")


def parse(path: str | None) -> dict:
    """{"gcells", "overflow" (sum of usage over capacity), "max", "by_kind",
    "worst" (box of the worst region, um), "nets" (most frequent nets)}."""
    out = {"gcells": 0, "overflow": 0, "max": 0, "by_kind": {}, "worst": None, "nets": []}
    if not path or not os.path.isfile(path):
        return out
    with open(path) as f:
        text = f.read()
    nets = Counter()
    kinds = Counter()
    for m in ENTRY.finditer(text):
        over = int(m["over"])
        out["gcells"] += 1
        out["overflow"] += over
        kinds[m["kind"].strip()] += 1
        if over > out["max"]:
            out["max"] = over
            out["worst"] = [float(m[k]) for k in ("x0", "y0", "x1", "y1")]
        nets.update(t[4:] for t in m["srcs"].split() if t.startswith("net:"))
    if "violation type" in text and not out["gcells"]:
        raise ValueError(f"unrecognised congestion report format: {path}")
    out["by_kind"] = dict(kinds)
    out["nets"] = [n for n, _ in nets.most_common(8)]
    return out


def describe(c: dict) -> str:
    if not c["gcells"]:
        return "no congestion"
    return (f"{c['gcells']} congested gcell regions, total overflow {c['overflow']} (worst {c['max']} at "
            f"{c['worst']} um), {c['by_kind']}; nets most often involved: {', '.join(c['nets'][:5])}")
