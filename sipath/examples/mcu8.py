"""Mode 2 example: an MCU die (mcu8) with ChipFoundry hard macros.

    python3 -m sipath.examples.mcu8 [workdir]
    python3 -m sipath run sipath/examples/mcu8.yaml [workdir]    (same thing)

The design and its steps are described in ``mcu8.yaml`` (hard macros from
sipath/examples/inputs/chipfoundry, see SOURCES.txt); this module only loads it
and lets callers change the macro sweep.
"""
from __future__ import annotations

import os
import sys

from .. import designfile
from ..design import Design

YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcu8.yaml")


def make_design(pdk=None) -> Design:
    d = designfile.load(YAML)[0]
    return d.resolve(pdk) if pdk is not None else d


def macro_variants(seeds=(1, 2, 3)) -> list[dict]:
    """Random search per seed (design orientation only), random search with an
    orientation search on the first seed, and rtl_macro_placer: one sweep,
    compared by the quick-placement score."""
    out = [{"method": "random", "seed": s, "samples": 40} for s in seeds]
    out.append({"method": "random", "seed": seeds[0], "samples": 40, "orients": ["R0", "MX", "MY", "R180"]})
    out.append({"method": "mpl"})
    return out


def run(workdir: str, runner=None, seeds=(1, 2, 3), parallel: int = 2):
    over = {"macros": {"variants": macro_variants(seeds), "parallel": parallel}, "place": {"parallel": parallel}}
    return designfile.run(YAML, workdir, runner, overrides=over)


if __name__ == "__main__":
    wd = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.getcwd(), "runs", "mcu8")
    f = run(wd)
    ex = f.steps["extract"].selected
    print("DEF :", ex.outputs["def_"])
    print("SPEF:", ex.outputs["spef"])
    print("metrics:", ex.metrics)
