"""Structural equivalence for resize/buffer ECOs.

Compares two gate-level netlists written by OpenROAD (``write_verilog``)
after normalising the changes a resize/buffer ECO is allowed to make. What a
cell is comes from Liberty (``sipath.liberty.CellLibrary``), not from its name:

* drive strength: two cells are interchangeable when they have the same input
  pins and the same truth table on every output (e.g. ``nand2_1`` / ``nand2_4``);
* buffers: a net driven through any chain of buffers (one input, output equal
  to it) resolves to the chain's source;
* physical-only cells (no outputs: fill, decap, tap, diode) are ignored.

Every remaining instance must exist on both sides with the same function and,
pin by pin, the same resolved driver; every output port must resolve to the
same driver. This is a structural check, not a formal one, and it is exact
for the edits it is meant to guard.
"""
from __future__ import annotations

from .liberty import CellLibrary
from .netlist import Module


class EquivalenceError(RuntimeError):
    pass


def _cell(lib: CellLibrary, name: str):
    try:
        return lib[name]
    except KeyError as e:
        raise EquivalenceError(str(e)) from None


def _resolver(m: Module, lib: CellLibrary):
    alias: dict[str, str] = {}

    def find(n):
        while alias.get(n, n) != n:
            n = alias[n]
        return n

    for lhs, rhs in m.assigns:
        a, b = find(lhs), find(rhs)
        if a != b:
            alias[a] = b
    driver: dict[str, tuple] = {}
    for p in m.ports:
        if p.direction in ("input", "inout"):
            for b in p.bits():
                driver.setdefault(find(b), ("PORT", b))
    buffers = {}
    for i in m.instances:
        c = _cell(lib, i.cell)
        if c.physical:
            continue
        outs = set(c.outputs)
        for pin, net in i.conns.items():
            if pin in outs:
                driver[find(net)] = (i.name, pin)
        if c.is_buffer:
            buffers[i.name] = find(i.conns.get(c.buffer_pins[0], ""))

    def resolve(net, depth=0):
        net = find(net)
        d = driver.get(net, ("UNDRIVEN", net))
        if d[0] in buffers and depth < 1000:
            return resolve(buffers[d[0]], depth + 1)
        return d

    return resolve, buffers, find


def signature(m: Module, lib: CellLibrary) -> tuple[dict, dict]:
    resolve, buffers, find = _resolver(m, lib)
    insts = {}
    for i in m.instances:
        c = _cell(lib, i.cell)
        if c.physical or i.name in buffers:
            continue
        outs = set(c.outputs)
        ins = tuple(sorted((pin, resolve(net)) for pin, net in i.conns.items() if pin not in outs))
        insts[i.name] = (c.function_class(), ins, i.cell)
    ports = {}
    for p in m.ports:
        if p.direction in ("output", "inout"):
            for b in p.bits():
                ports[b] = resolve(b)
    return insts, ports


def check(before: Module, after: Module, lib: CellLibrary) -> dict:
    """Raise EquivalenceError on any functional difference. Returns stats."""
    bi, bp = signature(before, lib)
    ai, ap = signature(after, lib)
    problems = []
    for name in sorted(set(bi) | set(ai)):
        if name not in ai:
            problems.append(f"instance {name} removed")
        elif name not in bi:
            problems.append(f"instance {name} added ({ai[name][2]})")
        elif bi[name][0] != ai[name][0]:
            problems.append(f"instance {name} function {bi[name][2]} -> {ai[name][2]}")
        elif bi[name][1] != ai[name][1]:
            problems.append(f"instance {name} inputs changed")
    for port in sorted(set(bp) | set(ap)):
        if bp.get(port) != ap.get(port):
            problems.append(f"output {port} driver {bp.get(port)} -> {ap.get(port)}")
    if problems:
        raise EquivalenceError(f"{len(problems)} differences: " + "; ".join(problems[:10]))
    return {"instances": len(ai), "ports": len(ap)}
