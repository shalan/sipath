"""Full-device LVS (P0.4): the PDK-maintained KLayout LVS deck (``signoff.lvs_full_deck``)
beside SiPath's standard-cell deck. It extracts every device class the PDK has
(low- and high-voltage MOS, MIM caps, poly and diffusion resistors, diodes,
varactors), so analog and IO layouts can be checked against their schematics,
not only standard-cell logic.

    python3 -m sipath lvs <gds> <top> <schematic.spice> [more.spice ...] --pdk <pdk> [--out dir]
                          [--lib cells.spice ...] [--opt switch=value ...]

checks a layout (e.g. a hard macro) against its schematic. In signoff the
``lvs_full`` check (opt-in) runs the same deck on the design's GDS against the
reference netlist of the standard-cell LVS.

The schematic is prepared first: device names the deck spells differently are
mapped (PDK ``signoff.lvs_full_aliases``, e.g. a schematic editor's MIM-cap
name -> the deck's), bare CDL device models get the PDK's ``device_prefix``,
and empty (black-box) subcircuits are replaced by the definition a ``--lib``
file gives.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

# switches as open_pdks' run_lvs.py passes them by default
DECK_DEFAULTS = {"run_mode": "deep", "spice_net_names": "true", "spice_comments": "false", "scale": "false",
                 "verbose": "false", "schematic_simplify": "false", "net_only": "false", "top_lvl_pins": "false",
                 "combine": "false", "purge": "false", "purge_nets": "false"}

SUBCKT = re.compile(r"^\.subckt\s+(\S+)[^\n]*\n(.*?)^\.ends[^\n]*\n?", re.S | re.M | re.I)


def _joined(text: str) -> str:
    """SPICE continuation lines ('+') joined to their line."""
    return re.sub(r"[ \t]*\n\+[ \t]*", " ", text)


UNITS = {"": 1.0, "u": 1e-6, "n": 1e-9, "p": 1e-12, "m": 1e-3, "k": 1e3}
MOS = re.compile(r"^([XM]\S*)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S*(?:[np]fet)\S*)((?:\s+\S+=\S+)*)\s*$", re.I)


def _num(v: str) -> float:
    """A SPICE value in the deck's convention: bare numbers are um, 'u' scales
    (1.5e+06u -> 1.5), as the PDK's cell SPICE and schematic editors write them."""
    m = re.fullmatch(r"([-+0-9.eE]+)([a-zA-Z]?)", v)
    return float(m.group(1)) * UNITS.get(m.group(2).lower(), 1.0) if m else float("nan")


def combine_parallel(body: str) -> tuple[str, int]:
    """Parallel MOS devices of one subcircuit (same model, gate, bulk, source/drain
    pair and length) merged into one whose width is the sum (``m`` counted),
    as the deck merges them in the extracted netlist. Returns (body, merged)."""
    groups, order, other = {}, [], []
    for ln in body.splitlines():
        m = MOS.match(ln)
        if not m:
            other.append(ln)
            continue
        name, d, g, s, b, model, params = m.groups()
        kv = {k.lower(): v for k, v in (p.split("=", 1) for p in params.split())}
        if "w" not in kv or "l" not in kv:
            other.append(ln)
            continue
        key = (model.upper(), g, b, frozenset((d, s)), round(_num(kv["l"]), 6))
        w = _num(kv["w"]) * float(kv.get("m", 1))  # W is the total width (nf fingers within it)
        if key not in groups:
            groups[key] = [name, d, g, s, b, model, round(_num(kv["l"]), 6), 0.0, 0]
            order.append((len(other), key))
            other.append(None)
        groups[key][7] += w
        groups[key][8] += 1
    merged = sum(v[8] - 1 for v in groups.values())
    out = list(other)
    for pos, key in order:
        name, d, g, s, b, model, l_, w, _ = groups[key]
        out[pos] = f"{name} {d} {g} {s} {b} {model} W={round(w, 6)} L={l_}"
    return "\n".join(out), merged


def prepare(texts: list[str], aliases: dict | None = None, device_prefix: str = "",
            libs: list[str] | None = None, combine: bool = True) -> tuple[str, dict]:
    """One schematic from ``texts``: aliases applied, bare MOS models prefixed,
    empty subcircuits replaced by the ``libs`` definitions of the same name
    (only those the schematic uses), parallel MOS merged (``combine``, as the
    deck merges them in the layout's netlist). Returns (text, info)."""
    text = "\n".join(_joined(t) for t in texts)
    info = {"aliased": 0, "prefixed": 0, "from_libs": []}
    for a, b in (aliases or {}).items():
        text, n = re.subn(rf"(?<![\w]){re.escape(a)}(?![\w])", b, text)
        info["aliased"] += n
    if device_prefix:
        def pre(m):
            info["prefixed"] += 1
            return f"{m.group(1)}{device_prefix}{m.group(2)}"
        # CDL MOS: M<name> d g s b <model> ...; only models without the prefix
        text = re.sub(rf"^(M\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+)(?!{re.escape(device_prefix)})((?:special_)?[np]fet\S*)",
                      pre, text, flags=re.M | re.I)
    if libs:
        lib = {}
        for t in libs:
            for m in SUBCKT.finditer(_joined(t)):
                lib.setdefault(m.group(1), m.group(0))
        out = []
        for m in SUBCKT.finditer(text):
            body = [ln for ln in m.group(2).splitlines() if ln.strip() and not ln.lstrip().startswith("*")]
            if not body and m.group(1) in lib:
                out.append((m.span(), lib[m.group(1)]))
                info["from_libs"].append(m.group(1))
        for (a, b), repl in reversed(out):
            text = text[:a] + repl.rstrip("\n") + "\n" + text[b:]
        # library cells the schematic calls but never defines
        defined = {m.group(1) for m in SUBCKT.finditer(text)}
        called = {ln.split()[-1] for ln in text.splitlines() if ln[:1] in "Xx" and len(ln.split()) > 1}
        extra = sorted((called - defined) & set(lib))
        if extra:
            text += "\n" + "\n".join(lib[c].rstrip("\n") for c in extra) + "\n"
            info["from_libs"] += extra
    if combine:
        info["parallel_merged"] = 0

        def comb(m):
            body, n = combine_parallel(m.group(2))
            info["parallel_merged"] += n
            head = m.group(0)[:m.start(2) - m.start(0)]
            return head + (body + "\n" if body else "") + m.group(0)[m.end(2) - m.start(0):]
        text = SUBCKT.sub(comb, text)
    return text, info


def run(runner, gds: str, top: str, schematic: str, workdir: str, deck: str, threads: int = 8,
        options: dict | None = None, timeout: float | None = None) -> dict:
    """The PDK deck on ``gds`` (cell ``top``) against ``schematic``. A run that
    does not end with the deck's verdict is reported as not matching."""
    os.makedirs(workdir, exist_ok=True)
    report = os.path.join(workdir, f"{top}.full.lvsdb")
    extracted = os.path.join(workdir, f"{top}.full.extracted.cir")
    log = os.path.join(workdir, "lvs_full.log")
    args = ["klayout", "-b", "-r", deck, "-rd", f"input={gds}", "-rd", f"top_cell={top}", "-rd", f"report={report}",
            "-rd", f"schematic={schematic}", "-rd", f"target_netlist={extracted}", "-rd", f"thr={threads}"]
    for k, v in {**DECK_DEFAULTS, **(options or {})}.items():
        args += ["-rd", f"{k}={v}"]
    if hasattr(runner, "mount"):
        runner.mount(workdir, os.path.dirname(os.path.abspath(gds)), os.path.dirname(os.path.abspath(deck)),
                     os.path.dirname(os.path.abspath(schematic)))
    t0 = time.time()
    with open(log, "w") as f:
        p = subprocess.run(runner.tool_command(workdir, args), stdout=f, stderr=subprocess.STDOUT, timeout=timeout,
                           cwd=workdir)
    with open(log, errors="replace") as f:
        out = f.read()
    match = "Netlists match" in out and "don't match" not in out
    verdict = "Netlists match" in out or "don't match" in out
    errors = []
    if os.path.isfile(report):
        with open(report, errors="replace") as fh:
            errors = [m.group(1) for m in re.finditer(r"M\(E B\('([^']*)'\)\)", fh.read())]
    res = {"match": match, "report": report, "extracted": extracted, "log": log, "exit": p.returncode,
           "seconds": round(time.time() - t0, 1), "errors": len(errors), "first_errors": errors[:8]}
    if not verdict:
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
        res["reason"] = f"no verdict from the deck (exit {p.returncode}): {' / '.join(tail)[:400]}"
    return res


def main(argv) -> int:
    import json
    from .config import Config
    from .pdk import load
    from .tools.openroad.session import find_openroad
    args, opts, it = [], {}, iter(argv)
    for a in it:
        if a in ("--pdk", "--out"):
            opts[a] = next(it)
        elif a == "--lib":
            opts.setdefault("--lib", []).append(next(it))
        elif a == "--opt":  # a deck switch, e.g. schematic_simplify=true
            k, _, v = next(it).partition("=")
            opts.setdefault("--opt", {})[k] = v
        else:
            args.append(a)
    if len(args) < 3:
        print(__doc__)
        return 2
    gds, top, schems = os.path.abspath(args[0]), args[1], [os.path.abspath(s) for s in args[2:]]
    runner = find_openroad(Config.load())
    if "--pdk" not in opts:
        print("--pdk <name> is required (python3 -m sipath pdks lists them)")
        return 2
    pdk = load(opts["--pdk"], runner)
    so = pdk.signoff or {}
    if not so.get("lvs_full_deck"):
        print(f"PDK {pdk.name} has no signoff.lvs_full_deck")
        return 2
    out = os.path.abspath(opts.get("--out", os.path.join("runs", "lvs_full", top)))
    os.makedirs(out, exist_ok=True)
    texts = [open(s, errors="replace").read() for s in schems]
    libs = [open(f, errors="replace").read() for f in opts.get("--lib", [])]
    text, info = prepare(texts, so.get("lvs_full_aliases"), so.get("device_prefix", ""), libs)
    sch = os.path.join(out, f"{top}.schematic.spice")
    with open(sch, "w") as f:
        f.write(text)
    res = run(runner, gds, top, sch, out, so["lvs_full_deck"], int(so.get("lvs_threads", 8)),
              {**(so.get("lvs_full_options") or {}), **opts.get("--opt", {})})
    res.update(schematic=sch, **info)
    print(json.dumps(res, indent=1))
    return 0 if res["match"] else 1
