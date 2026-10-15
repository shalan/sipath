"""Pad plan rules, checked on the pad list before any pad is placed.

Each pad gets a role from its master (PDK description: ``io_roles`` maps a
master-name pattern to a role) and, for signal pads, a kind (input, output or
analog) from the design. Rules (all configurable in ``PadRules``):

* supply pairs abut: every VCCD pad has a VSSD neighbour, VDDIO/VSSIO and
  VDDA/VSSA likewise (power next to its return keeps the current loop small);
* every edge with signal pads has at least one VDDIO/VSSIO pair (the GPIO
  drivers run from VDDIO);
* at most ``max_outputs_per_io_pair`` output pads per VDDIO/VSSIO pair on an
  edge (simultaneously switching outputs);
* core supply (VCCD/VSSD) pairs on at least ``min_core_supply_edges`` edges;
* analog signal pads sit on an edge that has a VDDA/VSSA pair, and are never
  adjacent to an output pad.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

PAIRS = {"vccd": "vssd", "vssd": "vccd", "vddio": "vssio", "vssio": "vddio", "vdda": "vssa", "vssa": "vdda"}


class PadPlanError(ValueError):
    pass


@dataclass
class PadRules:
    max_outputs_per_io_pair: int = 8
    min_core_supply_edges: int = 2
    require_io_supply_per_edge: bool = True
    require_supply_pairs_adjacent: bool = True
    analog_needs_analog_supply: bool = True
    analog_not_next_to_outputs: bool = True


def role_of(master: str, roles: dict[str, str]) -> str:
    for pattern, role in roles.items():
        if fnmatch.fnmatch(master, pattern):
            return role
    raise PadPlanError(f"no pad role for master {master}")


def check(pads, roles: dict[str, str], kinds: dict[str, str], rules: PadRules | None = None) -> dict:
    """``pads``: objects with inst, edge, order, master. ``kinds``: inst ->
    input|output|analog for signal pads. Raises PadPlanError listing every
    violated rule; returns a summary per edge."""
    rules = rules or PadRules()
    problems, summary = [], {}
    core_edges = set()
    for edge in ("north", "south", "east", "west"):
        seq = sorted((p for p in pads if p.edge == edge), key=lambda p: p.order)
        if not seq:
            continue
        rl = [role_of(p.master, roles) for p in seq]
        kd = [kinds.get(p.inst, "") if r == "signal" else r for p, r in zip(seq, rl)]
        io_pairs = min(rl.count("vddio"), rl.count("vssio"))
        outputs = kd.count("output")
        summary[edge] = {"pads": len(seq), "io_pairs": io_pairs, "outputs": outputs,
                         "order": [f"{p.inst}:{k}" for p, k in zip(seq, kd)]}
        if rules.require_supply_pairs_adjacent:
            for i, r in enumerate(rl):
                if r in PAIRS:
                    nb = {rl[j] for j in (i - 1, i + 1) if 0 <= j < len(rl)}
                    if PAIRS[r] not in nb:
                        problems.append(f"{edge}: {seq[i].inst} ({r}) has no adjacent {PAIRS[r]} pad")
        if rules.require_io_supply_per_edge and "signal" in rl and io_pairs == 0:
            problems.append(f"{edge}: signal pads but no VDDIO/VSSIO pair")
        if io_pairs and outputs > rules.max_outputs_per_io_pair * io_pairs:
            problems.append(f"{edge}: {outputs} outputs for {io_pairs} VDDIO/VSSIO pair(s) "
                            f"(max {rules.max_outputs_per_io_pair} per pair)")
        if "vccd" in rl and "vssd" in rl:
            core_edges.add(edge)
        if "analog" in kd:
            if rules.analog_needs_analog_supply and not ("vdda" in rl and "vssa" in rl):
                problems.append(f"{edge}: analog pads but no VDDA/VSSA pair on the edge")
            if rules.analog_not_next_to_outputs:
                for i, k in enumerate(kd):
                    if k == "analog":
                        for j in (i - 1, i + 1):
                            if 0 <= j < len(kd) and kd[j] == "output":
                                problems.append(f"{edge}: analog pad {seq[i].inst} next to output {seq[j].inst}")
    if len(core_edges) < rules.min_core_supply_edges:
        problems.append(f"core supply (VCCD/VSSD) pairs on {len(core_edges)} edge(s): {sorted(core_edges)}; "
                        f"need {rules.min_core_supply_edges}")
    if problems:
        raise PadPlanError("pad plan violates rules:\n  " + "\n  ".join(problems))
    summary["core_supply_edges"] = sorted(core_edges)
    return summary
