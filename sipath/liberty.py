"""Cell functions from Liberty, for the structural equivalence check.

A cell is described by its pins (direction, Liberty ``function`` for outputs)
as OpenSTA read them (``query``, in the live session: works for any PDK and
for files that live inside the runner) or from a dict (tests).

``CellLibrary`` derives from that, with no cell-name patterns:

* output pins: direction ``output`` (``internal`` state pins are ignored);
* function class: per output pin, the truth table of its function over the
  cell's input pins (state variables such as ``IQ`` are inputs of the table),
  plus the input pin names; drive strengths of the same function share it;
* buffer: one input, one output, output function == that input;
* inverter: likewise with the negated input;
* physical-only: no output pins (fill, decap, tap, antenna diode) or no
  Liberty view at all for a cell whose LEF type is not a logic type.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .tcl import qlist, tcl_parse

# --------------------------------------------------------------- expressions
TOKEN = re.compile(r"\s*(?:(?P<id>[A-Za-z_][A-Za-z0-9_\[\]\.]*)|(?P<const>[01])|(?P<op>[!()'&*|+^]))")


def _tokens(expr: str) -> list[str]:
    out, pos = [], 0
    expr = expr.strip().strip('"')
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"bad Liberty function {expr!r} at {expr[pos:]!r}")
        out.append(m.group(m.lastgroup))
        pos = m.end()
    return out


class _Parser:
    """Liberty function grammar: | + (or), ^ (xor), & * or juxtaposition (and),
    ! prefix / ' postfix (not), parentheses, 0/1."""

    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self):
        if self.i >= len(self.t):
            raise ValueError("unexpected end of function")
        self.i += 1
        return self.t[self.i - 1]

    def parse(self):
        e = self.or_()
        if self.peek() is not None:
            raise ValueError(f"trailing tokens {self.t[self.i:]}")
        return e

    def or_(self):
        e = self.xor()
        while self.peek() in ("|", "+"):
            self.take()
            e = ("or", e, self.xor())
        return e

    def xor(self):
        e = self.and_()
        while self.peek() == "^":
            self.take()
            e = ("xor", e, self.and_())
        return e

    def and_(self):
        e = self.unary()
        while True:
            p = self.peek()
            if p in ("&", "*"):
                self.take()
                e = ("and", e, self.unary())
            elif p is not None and (p in ("!", "(", "0", "1") or p[0].isalpha() or p[0] == "_"):
                e = ("and", e, self.unary())  # juxtaposition
            else:
                return e

    def unary(self):
        if self.peek() == "!":
            self.take()
            return ("not", self.unary())
        e = self.atom()
        while self.peek() == "'":
            self.take()
            e = ("not", e)
        return e

    def atom(self):
        p = self.take() if self.peek() is not None else None
        if p is None:
            raise ValueError("unexpected end of function")
        if p == "(":
            e = self.or_()
            if self.take() != ")":
                raise ValueError("missing )")
            return e
        if p in ("0", "1"):
            return ("const", p == "1")
        if p[0].isalpha() or p[0] == "_":
            return ("var", p)
        raise ValueError(f"unexpected token {p!r}")


def parse_function(expr: str):
    return _Parser(_tokens(expr)).parse()


def variables(tree) -> set[str]:
    if tree[0] == "var":
        return {tree[1]}
    if tree[0] == "const":
        return set()
    return set().union(*(variables(t) for t in tree[1:]))


def evaluate(tree, env: dict) -> bool:
    k = tree[0]
    if k == "var":
        return env[tree[1]]
    if k == "const":
        return tree[1]
    if k == "not":
        return not evaluate(tree[1], env)
    a, b = evaluate(tree[1], env), evaluate(tree[2], env)
    return (a and b) if k == "and" else (a or b) if k == "or" else (a != b)


def truth_table(expr: str, inputs: list[str]) -> tuple:
    tree = parse_function(expr)
    extra = sorted(variables(tree) - set(inputs))  # state variables (IQ, IQN, ...)
    names = list(inputs) + extra
    if len(names) > 12:
        raise ValueError(f"function {expr!r} has too many inputs for a truth table")
    return tuple(evaluate(tree, dict(zip(names, bits))) for bits in itertools.product((False, True), repeat=len(names)))


# --------------------------------------------------------------- cells
LOGIC_TYPES = {"CORE", "BLOCK", "PAD", "PAD_INPUT", "PAD_OUTPUT", "PAD_INOUT", "CORE_TIEHIGH", "CORE_TIELOW"}


@dataclass
class Cell:
    name: str
    pins: dict  # pin -> {"direction": ..., "function": ...}
    lef_type: str = "CORE"
    liberty: bool = True
    _cls: tuple | None = field(default=None, repr=False)

    @property
    def inputs(self) -> list[str]:
        return sorted(p for p, v in self.pins.items() if v["direction"] in ("input", "inout"))

    @property
    def outputs(self) -> list[str]:
        return sorted(p for p, v in self.pins.items() if v["direction"] == "output")

    @property
    def physical(self) -> bool:
        if self.lef_type.startswith("PAD") or self.lef_type == "BLOCK":
            return False
        if not self.liberty:
            return self.lef_type not in LOGIC_TYPES
        return not self.outputs

    def function_class(self) -> tuple:
        if self._cls is None:
            ins = self.inputs
            outs = []
            for o in self.outputs:
                f = self.pins[o].get("function") or ""
                outs.append((o, truth_table(f, ins) if f else ("opaque", self.name)))
            self._cls = (tuple(ins), tuple(outs))
        return self._cls

    def _single(self):
        ins, outs = self.inputs, self.outputs
        if len(ins) != 1 or len(outs) != 1:
            return None
        f = self.pins[outs[0]].get("function") or ""
        return (ins[0], outs[0], truth_table(f, ins) if f else None)

    @property
    def is_buffer(self) -> bool:
        s = self._single()
        return bool(s and s[2] == (False, True))

    @property
    def buffer_pins(self) -> tuple[str, str]:
        s = self._single()
        return s[0], s[1]

    @property
    def is_inverter(self) -> bool:
        s = self._single()
        return bool(s and s[2] == (True, False))


class CellLibrary:
    def __init__(self, cells: dict[str, Cell]):
        self.cells = cells

    @classmethod
    def from_dict(cls, data: dict) -> "CellLibrary":
        """{cell: {"type": LEF type, "liberty": bool, "pins": {pin: {"direction", "function"}}}}"""
        return cls({n: Cell(n, v.get("pins", {}), v.get("type", "CORE"), v.get("liberty", True))
                    for n, v in data.items()})

    def __getitem__(self, name) -> Cell:
        try:
            return self.cells[name]
        except KeyError:
            raise KeyError(f"cell {name} not in the library description") from None

    def __contains__(self, name):
        return name in self.cells


QUERY_TCL = r"""
proc ::flow_cell_info {names} {
  set out {}
  foreach n $names {
    set m [[ord::get_db] findMaster $n]
    set type [expr {$m eq "NULL" ? "UNKNOWN" : [$m getType]}]
    set lc [lindex [get_lib_cells -quiet */$n] 0]
    set pins {}
    if {$lc ne ""} {
      foreach p [get_lib_pins -of_objects $lc] {
        if {[$p is_bus] || [$p is_bundle]} {
          set it [$p member_iterator]
          while {[$it has_next]} {
            set b [$it next]
            lappend pins [list [get_full_name $b] [sta::liberty_port_direction $b] [$b function]]
          }
          $it finish
          continue
        }
        lappend pins [list [get_property $p name] [sta::liberty_port_direction $p] [$p function]]
      }
    } elseif {$m ne "NULL"} {
      foreach mt [$m getMTerms] {
        lappend pins [list [$mt getName] [string tolower [$mt getIoType]] {}]
      }
    }
    lappend out [list $n $type [expr {$lc ne ""}] $pins]
  }
  return $out
}
"""


def query(session, masters) -> CellLibrary:
    """Cell descriptions for ``masters`` from the live session (Liberty via
    OpenSTA, LEF type via OpenDB)."""
    session.run(QUERY_TCL)
    data = {}
    for n, typ, has_lib, pins in tcl_parse(session.value(f"::flow_cell_info {qlist(sorted(set(masters)))}")):
        pd = {}
        for p in pins:
            name, direction, function = (p + ["", ""])[:3] if isinstance(p, list) else (p, "", "")
            name = name.split("/")[-1]
            direction = {"bidirect": "inout", "tristate": "output"}.get(direction, direction)
            pd[name] = {"direction": direction, "function": function}
        data[n] = {"type": typ, "liberty": has_lib == "1", "pins": pd}
    return CellLibrary.from_dict(data)
