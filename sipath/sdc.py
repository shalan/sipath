"""SDC read without a tool: the clock ports a constraints file defines.

``clock_ports(path)`` returns the ports of its ``create_clock ... [get_ports X]``
commands, following simple ``set name value`` variables (ORFS writes
``set clk_port_name clk`` ... ``create_clock ... $clk_port``). Virtual clocks
(no port) are left out. Anything it cannot resolve gives no port, never a guess.
"""
from __future__ import annotations

import re

_SET = re.compile(r"^\s*set\s+(\w+)\s+(.+?)\s*$")
_PORTS = re.compile(r"get_ports\s+(\{[^}]*\}|\"[^\"]*\"|[^\]\s]+)")


def _subst(text: str, env: dict[str, str]) -> str:
    return re.sub(r"\$\{?(\w+)\}?", lambda m: env.get(m.group(1), m.group(0)), text)


def clock_ports(path: str) -> list[str]:
    """Clock ports of the ``create_clock`` commands of an SDC file, in order, each once."""
    env: dict[str, str] = {}
    out: list[str] = []
    with open(path, errors="replace") as f:
        text = f.read().replace("\\\n", " ")  # continued lines
    for line in text.splitlines():
        line = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        m = _SET.match(line)
        if m:
            env[m.group(1)] = _subst(m.group(2), env).strip()
            continue
        if not re.match(r"^\s*create_clock\b", line):
            continue
        for p in _PORTS.findall(_subst(line, env)):
            for name in p.strip("{}\"").split():
                if not name.startswith("$") and name not in out:
                    out.append(name)
    return out
