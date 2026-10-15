"""Mode 1 example: one soft macro (dp8) whose floorplan comes from a DEF.

    python3 -m sipath.examples.dp8 [workdir]
    python3 -m sipath run sipath/examples/dp8.yaml [workdir]     (same thing)

The design and its steps are described in ``dp8.yaml``; this module only
loads it and lets callers replace the sweeps.
"""
from __future__ import annotations

import os
import sys

from .. import designfile
from ..design import Design

YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dp8.yaml")


def make_design() -> Design:
    return designfile.load(YAML)[0]


def run(workdir: str, runner=None, place_variants=None, route_variants=None, parallel: int = 2):
    over = {"place": {"parallel": parallel}, "route_digital": {"parallel": parallel}}
    if place_variants:
        over["place"]["variants"] = place_variants
    if route_variants:
        over["route_digital"]["variants"] = route_variants
    return designfile.run(YAML, workdir, runner, overrides=over)


if __name__ == "__main__":
    wd = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "runs", "dp8")
    f = run(wd)
    ex = f.steps["extract"].selected
    print("DEF :", ex.outputs["def_"])
    print("SPEF:", ex.outputs["spef"])
    print("metrics:", ex.metrics)
