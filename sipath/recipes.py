"""ABC recipes for the synth step (``sipath/synth_recipes/*.abc``).

``abc: <recipe>`` picks one; ``{D}`` in a recipe becomes ``-D <ps>`` when a
delay target is set (``abc_target``) and is removed otherwise. Objective sets
(``sweep: delay|area|balanced`` on a synth step of a design file) expand to the
five recipes measured best for that objective, and the ``synth_select``
selector picks among them (least area meeting timing, else the knee of the
slack/area front). Most recipes and the sets come from shalan/synth_flow, see
``sipath/synth_recipes/README.md``.
"""
from __future__ import annotations

import os
import re

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synth_recipes")

# earlier abc goals, kept as names
ALIASES = {"delay": "yosys_default", "area": "area_map", "speed": "speed_sizing"}

# synth_flow's top 5 per objective (16-design bench, 2026-09-12)
SETS = {
    "delay": ["delay_map_resyn", "delay_map", "orfs_area", "delay_choice_deep_v3", "delay_syn2"],
    "area": ["delay_aggressive", "area_lut6", "area_max", "yosys_default", "area_classic"],
    "balanced": ["balanced_resyn", "balanced_resyn2x", "delay_triple", "delay_iter_heavy", "delay_map_resyn"],
}

# tie-break when results are equal (lower first): synth_flow's mean-WNS rank, references last
PRIORITY = ["delay_map", "delay_map_resyn", "delay_choice_deep_v3", "delay_triple", "delay_iter_heavy",
            "delay_syn2", "delay_choice_deep", "delay_choice_deep_v4", "delay_choice_deep_bb", "delay_aggressive",
            "balanced_resyn2x", "balanced_resyn", "orfs_area", "area_classic", "area_lut6", "area_max",
            "speed_sizing", "area_map", "orfs_speed", "yosys_default"]


def names() -> list[str]:
    """Every recipe available (file names without .abc)."""
    return sorted(f[:-4] for f in os.listdir(DIR) if f.endswith(".abc"))


def resolve(name: str) -> str:
    """The recipe a name stands for (aliases resolved); ValueError when unknown."""
    n = ALIASES.get(name, name)
    if n not in names():
        raise ValueError(f"unknown ABC recipe {name!r}; known: {names()} (aliases {ALIASES})")
    return n


def text(name: str) -> str:
    """The recipe's ABC commands, comments dropped, one command per ';'."""
    with open(os.path.join(DIR, resolve(name) + ".abc")) as f:
        body = " ".join(ln.split("#", 1)[0] for ln in f)
    return "; ".join(c.strip() for c in body.split(";") if c.strip())


def doc(name: str) -> str:
    """The recipe's first comment line."""
    with open(os.path.join(DIR, resolve(name) + ".abc")) as f:
        for ln in f:
            if ln.startswith("#"):
                return ln.lstrip("# ").strip()
    return ""


def materialize(name: str, target_ps: float | None, wire_load: bool = False) -> str:
    """The script ABC runs: ``{D}`` -> ``-D <ps>`` (or removed), and with
    ``wire_load`` the sizing commands told to use the Liberty's wire-load model
    (``-c``). Yosys substitutes ``{D}`` only in inline scripts, so SiPath writes
    the resolved script to a file itself."""
    t = text(name).replace("{D}", f"-D {target_ps:g}" if target_ps else "")
    if wire_load:
        t = re.sub(r"\b(buffer|upsize|dnsize|stime)\b(?! -c)", r"\1 -c", t)
    return re.sub(r"\s+", " ", t).strip()


def priority(name: str) -> int:
    n = ALIASES.get(name, name)
    return PRIORITY.index(n) if n in PRIORITY else len(PRIORITY)
