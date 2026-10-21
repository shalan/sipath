"""The work clock the cdlns placer reads instead of the wall clock (run_cdlns.py, budget "work").

Each read advances it by what the work since the previous read costs on the
reference machine (sram8, Apple-silicon Mac under Rosetta, measured with the
wall clock): a coordinate-descent pass 0.57 s, an annealing iteration 1/282 s
(two reads), a final-search iteration 0.077 s, any other read 1/564 s. The
placer's schedule then follows its work, not the machine: the same spec and
seed give the same placement (sram8 at 20 s and 120 s: the same cost and
iteration counts as the wall clock there, within 1%). The charges key on the
placer's source lines, so they apply only to the pinned file; another version
gets the uniform tick.
"""
from __future__ import annotations

import hashlib
import sys

TICK = 1.0 / 564  # seconds per clock read: 2 reads per annealing iteration at ~282 iterations/s
PLACER_SHA256 = "33407d53cdcf327c180ed15b480fe3aa2b8f3cb4bf29f42c3881d17f7b1108df"  # macropolo/cdlns_placer.py
CHARGES = {1094: 0.57, 1454: 0.077}  # the source line of a read -> its cost: CD pass check, LNS iteration check


class WorkClock:
    """A stand-in for the time module inside one source file: time() advances by the reference cost of the
    work since the previous read (``charges`` by the reading line of ``source``, else ``tick``)."""

    def __init__(self, source: str, charges: dict | None = None, tick: float = TICK):
        self.now, self.tick, self.source, self.charges = 0.0, tick, source, dict(charges or {})

    def time(self) -> float:
        f = sys._getframe(1)
        self.now += self.charges.get(f.f_lineno, self.tick) if f.f_code.co_filename == self.source else self.tick
        return self.now


def work_clock(source: str, expected_sha256: str = PLACER_SHA256) -> WorkClock:
    """The clock for ``source``: phase charges when it is the pinned placer, else the uniform tick."""
    with open(source, "rb") as f:
        pinned = hashlib.sha256(f.read()).hexdigest() == expected_sha256
    if not pinned:
        print(f"work_clock: {source} is not the pinned placer: uniform work clock", file=sys.stderr)
    return WorkClock(source, CHARGES if pinned else None)
