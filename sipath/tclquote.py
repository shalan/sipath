"""Tcl quoting and Tcl-list parsing.

``q`` makes any Python value one safe Tcl word (no command or variable
substitution can happen in it). ``tcl_parse`` reads a Tcl list result back
into nested Python lists, honouring braces, double quotes and backslashes.
"""
from __future__ import annotations

import re

_SAFE = re.compile(r"[A-Za-z0-9_.:/@+\-]+")
_SPECIAL = set('\\{}[]$"; \t\n')


def q(s) -> str:
    """Quote a value as one Tcl word.

    Plain words pass through; a value with balanced braces and no backslash is
    brace-quoted (nothing is substituted inside braces); anything else is
    backslash-escaped character by character."""
    s = str(s)
    if s and _SAFE.fullmatch(s):
        return s
    depth, ok = 0, "\\" not in s
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                ok = False
    if ok and depth == 0:
        return "{" + s + "}"
    return "".join("\\" + ch if ch in _SPECIAL else ch for ch in s)


def qe(s) -> str:
    """Quote a value as a string operand inside ``expr`` (where a bare word is
    not allowed): always braced, or backslash-escaped inside double quotes."""
    out = q(s)
    if out.startswith("{"):
        return out
    if _SAFE.fullmatch(str(s)):
        return "{" + str(s) + "}"
    return '"' + out + '"'


def qlist(items) -> str:
    """Tcl list literal; every item is quoted with ``q``."""
    return "{" + " ".join(q(i) for i in items) + "}"


def _words(text: str):
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        c = text[i]
        if c == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            if depth:
                raise ValueError(f"unbalanced braces in Tcl list: {text[:80]!r}")
            out.append(("brace", text[i + 1:j - 1]))
            i = j
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise ValueError(f"unbalanced quote in Tcl list: {text[:80]!r}")
            out.append(("word", "".join(buf)))
            i = j + 1
        else:
            buf = []
            while i < n and not text[i].isspace():
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            out.append(("word", "".join(buf)))
    return out


def tcl_parse(s: str):
    """Parse a Tcl list into nested Python lists of strings.

    A braced element containing whitespace (or empty) is parsed as a sub-list;
    a braced element without whitespace stays a string (so ``{a[0]}`` is the
    string ``a[0]``)."""
    def conv(kind, val):
        if kind == "brace" and (val == "" or any(ch.isspace() for ch in val)):
            return [conv(k, v) for k, v in _words(val)]
        return val

    return [conv(k, v) for k, v in _words(s)]


def tcl_words(s: str) -> list[str]:
    """Top-level elements of a Tcl list as strings (no recursion)."""
    return [v for _, v in _words(s)]
