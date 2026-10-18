"""Minimal structural (gate-level) Verilog model, writer and parser.

Only the subset needed for flattened netlists: one module, scalar or bus
ports, wires, cell instances with named connections, and ``assign a = b``.
Hierarchy in a flat netlist is carried by escaped instance names such as
``\\u_add.fa_3 `` (Yosys ``flatten`` style, ``.`` separator).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HIER_SEP = "."


def vname(name: str) -> str:
    """Return a Verilog identifier, escaping it when needed."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        return name
    m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_$]*)\[(\d+)\]", name)
    if m:  # bus bit reference
        return name
    return "\\" + name + " "


@dataclass
class Instance:
    name: str
    cell: str
    conns: dict[str, str] = field(default_factory=dict)


@dataclass
class Port:
    name: str
    direction: str  # input | output | inout
    width: int = 0  # 0 = scalar

    def bits(self) -> list[str]:
        if self.width == 0:
            return [self.name]
        return [f"{self.name}[{i}]" for i in range(self.width)]


@dataclass
class Module:
    name: str
    ports: list[Port] = field(default_factory=list)
    wires: list[str] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    assigns: list[tuple[str, str]] = field(default_factory=list)  # (lhs, rhs)

    # -- construction helpers -------------------------------------------------
    def port(self, name, direction, width=0) -> list[str]:
        p = Port(name, direction, width)
        self.ports.append(p)
        return p.bits()

    def wire(self, name) -> str:
        self.wires.append(name)
        return name

    def inst(self, name, cell, **conns) -> Instance:
        i = Instance(name, cell, dict(conns))
        self.instances.append(i)
        return i

    def port_bits(self) -> dict[str, str]:
        return {b: p.direction for p in self.ports for b in p.bits()}

    # -- output ---------------------------------------------------------------
    def to_verilog(self) -> str:
        out = [f"module {self.name} ({', '.join(p.name for p in self.ports)});"]
        for p in self.ports:
            rng = f" [{p.width - 1}:0]" if p.width else ""
            out.append(f"  {p.direction}{rng} {p.name};")
        for w in self.wires:
            out.append(f"  wire {vname(w)};")
        for lhs, rhs in self.assigns:
            out.append(f"  assign {vname(lhs)} = {vname(rhs)};")
        for i in self.instances:
            conns = ", ".join(f".{pin}({expr})" for pin, expr in _group_bus_pins(i))
            out.append(f"  {i.cell} {vname(i.name)} ({conns});")
        out.append("endmodule")
        return "\n".join(out) + "\n"


def _group_bus_pins(inst: Instance):
    """Yield (pin, verilog expr). Bit connections ``P[i]`` become one bus
    connection ``.P({P[n-1], ..., P[0]})``; every bit 0..n-1 must be given."""
    buses: dict[str, dict[int, str]] = {}
    order: list[str] = []
    for pin, net in inst.conns.items():
        m = re.fullmatch(r"(\w+)\[(\d+)\]", pin)
        if m:
            base = m.group(1)
            if base not in buses:
                buses[base] = {}
                order.append(base)
            buses[base][int(m.group(2))] = net
        else:
            order.append(pin)
            buses.setdefault(pin, None)
    for pin in order:
        bits = buses[pin]
        if bits is None:
            yield pin, vname(inst.conns[pin])
            continue
        n = max(bits) + 1
        if set(bits) != set(range(n)):
            raise ValueError(f"{inst.name}: bus {pin} has gaps {sorted(set(range(n)) - set(bits))}")
        yield pin, "{" + ", ".join(vname(bits[k]) for k in reversed(range(n))) + "}"


# -- parser -------------------------------------------------------------------
_TOKEN = re.compile(
    r"\\(\S+)\s"              # escaped identifier
    r"|([A-Za-z_][A-Za-z0-9_$]*)"  # identifier
    r"|(\d+'[bBhHdD][0-9a-fA-FxXzZ_]+)"  # sized constant
    r"|(\d+)"
    r"|(\S)"
)


def _tokens(text: str):
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for m in _TOKEN.finditer(text + " "):
        esc, ident, const, num, punct = m.groups()
        if esc is not None:
            yield ("id", esc)
        elif ident is not None:
            yield ("id", ident)
        elif const is not None:
            yield ("const", const)
        elif num is not None:
            yield ("num", num)
        else:
            yield ("p", punct)


def parse_verilog(text: str, top: str | None = None) -> Module:
    """Parse a flat structural module (the first one, or ``top``)."""
    toks = list(_tokens(text))
    pos = 0
    modules: dict[str, Module] = {}

    def peek(k=0):
        return toks[pos + k] if pos + k < len(toks) else ("eof", "")

    def take(expect=None):
        nonlocal pos
        t = toks[pos]
        pos += 1
        if expect is not None and t[1] != expect:
            raise ValueError(f"verilog parse: expected {expect!r}, got {t[1]!r}")
        return t

    def ref() -> str:
        """Net reference: name, name[i], or constant."""
        kind, val = take()
        if kind == "const":
            return val
        if peek()[1] == "[":
            take("[")
            idx = take()[1]
            take("]")
            return f"{val}[{idx}]"
        return val

    while peek()[0] != "eof":
        if peek()[1] != "module":
            take()
            continue
        take("module")
        mod = Module(take()[1])
        if peek()[1] == "(":
            depth = 0
            while True:
                t = take()[1]
                depth += t == "("
                depth -= t == ")"
                if depth == 0:
                    break
        take(";")
        declared: dict[str, Port] = {}
        widths: dict[str, int] = {}  # declared name -> width (0: scalar)

        def bits_of(consts: bool = False) -> list[str]:
            """An operand as bits, most significant first: name, name[i],
            name[a:b], or {operand, ...}; with ``consts`` (instance connections)
            a sized constant gives one 1'bX per bit."""
            if peek()[1] == "{":
                take("{")
                out = []
                while True:
                    out += bits_of(consts)
                    if peek()[1] == ",":
                        take(",")
                        continue
                    break
                take("}")
                return out
            kind, val = take()
            if kind == "const":
                if not consts:
                    raise ValueError(f"verilog parse: constant {val!r} in an assign is not supported "
                                     "(map constants to tie cells)")
                return _const_bits(val)
            if peek()[1] == "[":
                take("[")
                hi = int(take()[1])
                lo = hi
                if peek()[1] == ":":
                    take(":")
                    lo = int(take()[1])
                take("]")
                step = -1 if hi >= lo else 1
                return [f"{val}[{i}]" for i in range(hi, lo + step, step)]
            w = widths.get(val, 0)
            return [f"{val}[{i}]" for i in range(w - 1, -1, -1)] if w else [val]

        while peek()[1] != "endmodule":
            kw = peek()[1]
            if kw in ("input", "output", "inout", "wire"):
                take()
                width = 0
                if peek()[1] == "[":
                    take("[")
                    msb = int(take()[1])
                    take(":")
                    lsb = int(take()[1])
                    take("]")
                    width = abs(msb - lsb) + 1
                while True:
                    name = take()[1]
                    widths[name] = width
                    if kw == "wire":
                        if width:
                            mod.wires.extend(f"{name}[{i}]" for i in range(width))
                        else:
                            mod.wires.append(name)
                    else:
                        declared[name] = Port(name, kw, width)
                    if peek()[1] == ",":
                        take(",")
                        continue
                    break
                take(";")
            elif kw == "assign":
                take()
                lhs = bits_of()
                take("=")
                rhs = bits_of()
                take(";")
                if len(lhs) != len(rhs):
                    raise ValueError(f"verilog parse: assign of {len(rhs)} bits to {len(lhs)}")
                mod.assigns += list(zip(lhs, rhs))
            else:
                cell = take()[1]
                if peek()[1] == "#":
                    raise ValueError("parameterised instances are not supported")
                name = take()[1]
                take("(")
                inst = Instance(name, cell)
                while peek()[1] != ")":
                    take(".")
                    pin = take()[1]
                    if peek()[1] == "[":  # .P[i](net)
                        take("[")
                        pin = f"{pin}[{take()[1]}]"
                        take("]")
                    take("(")
                    if peek()[1] != ")":  # a net, a part-select, a vector or {msb, ..., lsb}
                        concat = peek()[1] == "{"
                        bits = bits_of(consts=True)
                        if len(bits) == 1 and not concat:
                            inst.conns[pin] = bits[0]
                        else:
                            for k, net in enumerate(reversed(bits)):
                                inst.conns[f"{pin}[{k}]"] = net
                    take(")")
                    if peek()[1] == ",":
                        take(",")
                take(")")
                take(";")
                mod.instances.append(inst)
        take("endmodule")
        mod.ports = list(declared.values())
        modules[mod.name] = mod
    if not modules:
        raise ValueError("no module found")
    if top is None:
        return next(iter(modules.values()))
    return modules[top]


def _const_bits(val: str) -> list[str]:
    """A Verilog constant (4'hf, 1'b0, 3'd5, 0) as 1'b0 / 1'b1 bits, most
    significant first."""
    m = re.fullmatch(r"(\d+)?'([bBhHdDoO])([0-9a-fA-FxXzZ_]+)", val)
    if not m:
        return [val]
    width = int(m.group(1)) if m.group(1) else 32
    base = {"b": 2, "h": 16, "d": 10, "o": 8}[m.group(2).lower()]
    digits = m.group(3).replace("_", "")
    if any(c in "xXzZ" for c in digits):
        return [val] if width == 1 else [f"1'b{digits[-1].lower()}"] * width
    v = int(digits, base)
    return [f"1'b{(v >> i) & 1}" for i in range(width - 1, -1, -1)]


def read_verilog(path, top=None) -> Module:
    with open(path) as f:
        return parse_verilog(f.read(), top)
