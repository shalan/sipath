"""Stand-in scores for exercising the scheduler graph with ``FakeSession``.

Nothing here touches a design. ``fake_metrics`` derives deterministic
numbers from an attempt's params so variant selection, parallel caps,
rejection and ECO refusal can be checked without OpenROAD. These numbers
are never reported as results; ``python3 -m sipath validate`` says so.
"""
from __future__ import annotations

import hashlib
import threading
import time

from .session import FakeSession


def fake_metrics(params: dict) -> str:
    h = int(hashlib.sha256(repr(sorted(params.items())).encode()).hexdigest()[:8], 16)
    wns = (h % 1000) / 1000.0 - 0.2
    return f"insts 100 wns {wns} tns 0 whs 0.1 hpwl_um {1000 + h % 500} std_util 0.5"


class Concurrency:
    """Tracks how many fake attempts run at once (to check parallel caps)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def enter(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self):
        with self.lock:
            self.now -= 1


def fake_step(conc: Concurrency | None = None, fail_if=None, delay: float = 0.05):
    def fn(ctx):
        if conc:
            conc.enter()
        try:
            time.sleep(delay)
            if fail_if and fail_if(ctx.params):
                raise RuntimeError("stand-in failure")
            ctx.session.responses["::flow_metrics"] = fake_metrics(ctx.params)
        finally:
            if conc:
                conc.leave()
    return fn


def fake_factory():
    return lambda d: FakeSession(d, responses={"::flow_timing_prep": "placement"})
