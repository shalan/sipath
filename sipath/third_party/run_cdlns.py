"""Run Team Macro Polo's CD-LNS placer on a floorplan exported by SiPath.

    <placer python> run_cdlns.py <spec.json> <out.json>

spec: name, canvas [w, h] (um, origin at the canvas corner), macros (hard
first: name, x, y = centre, w, h, fixed), num_hard, ports [[x, y], ...],
pin_offsets (per hard macro, [[dx, dy], ...] from its centre), nets (lists of
[owner, slot]: owner < num macros is a macro, else a port num_macros + i),
grid [rows, cols], seed, seconds, gap, budget. out: positions (centres of
the hard macros) and the placer's log tail.

budget "work" (default): the placer reads the work clock (work_clock.py): the same spec
and seed give the same placement. budget "time": the wall clock, as the placer was
written (results vary with the machine).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402

import macropolo.cdlns_placer as _placer_module  # noqa: E402
from macro_place.benchmark import Benchmark  # noqa: E402
from macropolo.cdlns_placer import CDLNSPlacer  # noqa: E402

from work_clock import work_clock  # noqa: E402


def main(spec_path: str, out_path: str) -> int:
    with open(spec_path) as f:
        s = json.load(f)
    macros, nh = s["macros"], int(s["num_hard"])
    n = len(macros)
    nets = [[tuple(e) for e in net] for net in s["nets"]]
    b = Benchmark(
        name=s["name"], canvas_width=float(s["canvas"][0]), canvas_height=float(s["canvas"][1]),
        num_macros=n,
        macro_positions=torch.tensor([[m["x"], m["y"]] for m in macros], dtype=torch.float32),
        macro_sizes=torch.tensor([[m["w"], m["h"]] for m in macros], dtype=torch.float32),
        macro_fixed=torch.tensor([bool(m["fixed"]) for m in macros], dtype=torch.bool),
        macro_names=[m["name"] for m in macros],
        num_nets=len(nets),
        net_nodes=[torch.tensor(sorted({o for o, _ in net if o < n}), dtype=torch.int64) for net in nets],
        net_weights=torch.ones(len(nets)),
        grid_rows=int(s["grid"][0]), grid_cols=int(s["grid"][1]),
        port_positions=torch.tensor(s["ports"], dtype=torch.float32).reshape(-1, 2),
        macro_pin_offsets=[torch.tensor(o, dtype=torch.float32).reshape(-1, 2) for o in s["pin_offsets"]],
        net_pin_nodes=[torch.tensor(net, dtype=torch.int64).reshape(-1, 2) for net in nets],
        hard_macro_indices=list(range(nh)), soft_macro_indices=list(range(nh, n)),
        num_hard_macros=nh, num_soft_macros=n - nh)
    if s.get("budget", "work") == "work":
        _placer_module.time = work_clock(_placer_module.__file__)
    placer = CDLNSPlacer(seed=int(s.get("seed", 42)), max_seconds=float(s.get("seconds", 120)),
                         gap=float(s.get("gap", 0.05)), true_proxy_check_every=0.0)
    out = placer.place(b)
    with open(out_path, "w") as f:
        json.dump({"positions": out[:nh].tolist()}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
