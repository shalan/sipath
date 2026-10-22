"""Attempt graph scheduler.

Public API
----------
``Flow(design, pdk, workdir, session_factory)``
``Flow.step(name, fn, parent=None, variants=None, score=None, parallel=1)``
    Runs one attempt per variant (params dict), at most ``parallel`` at a
    time. Every attempt gets its own session (one tool process), loads the
    parent checkpoint (or the netlist for a root step), runs ``fn(ctx)``, and
    saves its own checkpoint. A checkpoint is an opaque path here: the
    checkpoint store (``registry.checkpoint_store()``, by default the OpenROAD
    adapter's databases) prepares the process, loads, measures and saves. The best successful attempt by the named score
    is ``selected``; the others are ``rejected`` and stay in the graph.
    Returns a ``StepRun``; raises ``StepFailed`` if no attempt succeeded.
``Flow.eco(name, parent, ops, max_patch_nets=..., fn=...)``
    ECO from a checkpoint. Refused unless ``parent`` is the selected attempt
    of its step and has no selected child yet (the head of the chain).
``Flow.graph()`` / ``Flow.save_graph()``
``Flow(..., resume=True)``
    Rebuilds the attempts, selections and checkpoints from ``graph.json``.
    A step asked again with the same parent, variants and score returns its
    recorded result without running; attempts of an interrupted step that
    finished with a checkpoint are kept, the others run again. A selected
    attempt's directory is never deleted.
``Flow.cancel()``
    Kills the OpenROAD processes of running attempts; they and the attempts
    not started yet end ``cancelled`` and the step raises ``StepCancelled``.

A step function receives ``Ctx`` with ``session``, ``design``, ``pdk``,
``params``, ``dir``, ``parent`` and may set ``ctx.metrics`` and
``ctx.outputs``. Any exception (``TclError`` included) fails the attempt
and no checkpoint is written for it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import fingerprint as fp_mod
from . import params as params_mod
from . import scores as score_mod
from . import registry
from .config import Config

log = logging.getLogger("sipath")
log.addHandler(logging.NullHandler())  # silent unless the caller configures logging


def worst_slack(m: dict):
    """Worse of setup and hold slack (ns) in a metrics dict, or None."""
    vals = [m[k] for k in ("wns", "whs") if isinstance(m.get(k), (int, float))]
    return min(vals) if vals else None


def overlap_seconds(a: "Attempt", b: "Attempt") -> float:
    """How long two attempts' OpenROAD processes were alive at the same time."""
    if None in (a.started_at, a.finished_at, b.started_at, b.finished_at):
        return 0.0
    return max(0.0, min(a.finished_at, b.finished_at) - max(a.started_at, b.started_at))


def peak_concurrency(attempts) -> int:
    """Most attempts whose OpenROAD processes were alive at the same instant."""
    events = []
    for a in attempts:
        if a.started_at is not None and a.finished_at is not None:
            events += [(a.started_at, 1), (a.finished_at, -1)]
    peak = cur = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):
        cur += d
        peak = max(peak, cur)
    return peak


class StepFailed(RuntimeError):
    pass


class EcoRefused(RuntimeError):
    pass


class RetryWith(RuntimeError):
    """Raised by a step: this attempt fails, but one fresh attempt (a new
    process, from the same parent) with ``params`` merged into its own may
    succeed, e.g. a full re-route where an incremental one lost nets. The
    scheduler runs it once and records both (the new one's ``retry_of``)."""

    def __init__(self, params: dict, reason: str):
        super().__init__(f"{reason}; retrying with {params}")
        self.params, self.reason = dict(params), reason


class StepCancelled(StepFailed):
    pass


class BudgetExceeded(StepFailed):
    """The run's wall-time or attempt budget ran out (config budget_s / max_attempts); --resume continues."""


class MissingPrerequisite(ValueError):
    """A step asked of a parent chain without something it needs (checked before any tool starts)."""


# what steps need and give (registry.register_step requires= / provides=; the built-ins: sipath.steps.CAPS)
CAPABILITIES = {
    "netlist": "a gate-level netlist (the design's, or synthesis')",
    "floorplan": "die, core, rows, IO pins (and the pad ring of a die)",
    "macros_placed": "hard macros placed",
    "power_grid": "power grid, tap and endcap cells",
    "placed": "standard cells placed and legal",
    "clock_tree": "clock tree built",
    "analog_routed": "analog nets routed and frozen",
    "routed": "signal nets routed",
    "filled": "metal fill",
    "parasitics": "extracted parasitics (a SPEF per corner)",
    "signoff": "signoff checks ran",
    "gds": "a GDS of the design",
    "powered_netlist": "a netlist with supply pins",
    "views": "hardened-macro views",
    "chip_gds": "the design integrated into its frame",
}
# the step that first gives each capability, named in a refusal
GIVEN_BY = {"netlist": "synth", "floorplan": "floorplan", "macros_placed": "macros", "power_grid": "physical",
            "placed": "place", "clock_tree": "cts", "analog_routed": "route_analog", "routed": "route_digital",
            "filled": "fill", "parasitics": "extract", "signoff": "signoff", "gds": "signoff",
            "powered_netlist": "signoff", "views": "views", "chip_gds": "integrate"}


def needs(fn, params: dict) -> set | None:
    """What ``fn`` requires of its parent chain for ``params``; None when it declares nothing."""
    req = getattr(fn, "requires", None)
    if req is None:
        return None
    return set(req(params or {}) if callable(req) else req)


def missing_text(name: str, missing, have) -> str:
    adds = ", ".join(f"{c} ({CAPABILITIES.get(c, c)}; given by {GIVEN_BY[c]})" if c in GIVEN_BY else c
                     for c in sorted(missing))
    return (f"step {name} needs {adds}, which its parent chain does not have "
            f"(it has: {', '.join(sorted(have)) or 'nothing'})")


def chain_problems(root: set, chain) -> list[str]:
    """Check a whole flow before it runs: ``chain`` is [(name, fn, variants)], each continuing from the one
    before; the problems (a step whose chain lacks what it needs), in order."""
    have, out = set(root), []
    for name, fn, variants in chain:
        miss = set()
        for v in variants or [{}]:
            n = needs(fn, v)
            if n is not None:
                miss |= n - have
        if miss:
            out.append(missing_text(name, miss, have))
        have |= set(getattr(fn, "provides", ()) or ())
    return out


def _same(a, b) -> bool:
    """Equal after a JSON round trip (tuples vs lists, key order)."""
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


@dataclass
class Attempt:
    id: str
    step: str
    index: int
    params: dict
    parent: "Attempt | None"
    dir: str
    kind: str = "step"  # step | eco | tool (a step run by an external tool, no OpenROAD: e.g. synthesis)
    status: str = "pending"  # pending running ok failed selected rejected
    checkpoint: str | None = None
    metrics: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    error: str | None = None
    score: object = None
    seconds: float = 0.0
    session_id: int | None = None
    started_at: float | None = None  # epoch seconds, when its OpenROAD process was started
    finished_at: float | None = None  # epoch seconds, when that process was closed
    process: str | None = None  # session/container name of its OpenROAD process
    timing: dict = field(default_factory=dict)  # phases and slowest Tcl commands (seconds)
    transcript_tail: list = field(default_factory=list)  # last OpenROAD output lines of a failed attempt
    debug_checkpoint: str | None = None  # failed attempt's database, for inspection only (never a parent)
    resumed: bool = False  # recorded by an earlier run of this flow, not run again
    retry_of: str | None = None  # the attempt whose RetryWith started this one
    retry_reason: str | None = None  # why that attempt asked for it (RetryWith.reason)
    adapted_from: str | None = None  # the attempt an adaptive round derived this one from (Flow.extend)
    adapt_reason: str | None = None  # what in it (e.g. the hotspots) the new params answer
    fingerprint: str | None = None  # the run's inputs, its step, params, declared defaults and parent (fingerprint)
    stale_reason: str | None = None  # why a resume did not reuse it (status "stale")
    capabilities: list | None = None  # what its chain has after it (CAPABILITIES); None: unknown (an old graph)
    retry: dict | None = field(default=None, repr=False)  # params a RetryWith asked for (not recorded)
    retry_why: str | None = field(default=None, repr=False)  # its reason, handed to the retry

    def to_json(self):
        return {
            "id": self.id, "step": self.step, "kind": self.kind, "index": self.index, "dir": self.dir,
            "params": self.params, "parent": self.parent.id if self.parent else None,
            "status": self.status, "checkpoint": self.checkpoint, "metrics": self.metrics,
            "outputs": self.outputs, "error": self.error,
            "score": self.score if isinstance(self.score, (int, float, type(None))) else list(self.score),
            "seconds": round(self.seconds, 2),
            "started_at": self.started_at, "finished_at": self.finished_at, "process": self.process,
            "timing": self.timing, "transcript_tail": self.transcript_tail,
            "debug_checkpoint": self.debug_checkpoint, "resumed": self.resumed, "retry_of": self.retry_of,
            "retry_reason": self.retry_reason, "adapted_from": self.adapted_from, "adapt_reason": self.adapt_reason,
            "fingerprint": self.fingerprint, **({"stale_reason": self.stale_reason} if self.stale_reason else {}),
            "capabilities": self.capabilities,
        }


@dataclass
class StepRun:
    name: str
    attempts: list[Attempt]
    score_name: str
    selected: Attempt | None
    parent: str | None = None  # parent attempt id
    variants: list | None = None
    resumed: bool = False
    rounds: list = field(default_factory=list)  # adaptive rounds (Flow.extend / Flow.stop_rounds), in order
    fingerprint: str | None = None  # fingerprint.step: what a resume compares
    reused: bool = False  # a recorded result already handed out by this session


@dataclass
class Ctx:
    session: object
    design: object
    pdk: object
    params: dict
    dir: str
    parent: Attempt | None
    attempt: Attempt
    metrics: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    runner: object = None  # how the installation's tools run (tool steps, signoff)
    config: object = None  # sipath.config.Config of the run (e.g. the Magic image)
    threads: int | None = None  # OpenROAD threads of this attempt (see Flow._threads)
    # fn(ctx) run after the checkpoint is saved, in the same process: observations of the saved state
    # (heatmaps) that may disturb the process but never the checkpoint or the metrics read before it
    after_save: list = field(default_factory=list)
    # the attempt's other processes (tools): add(p) / discard(p) / cancelled; Flow.cancel kills them (p.kill())
    processes: object = None

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def ancestor_output(self, key: str):
        """``key`` from this attempt's outputs or the nearest ancestor that has it."""
        if key in self.outputs:
            return self.outputs[key]
        a = self.parent
        while a is not None:
            if key in a.outputs:
                return a.outputs[key]
            a = a.parent
        raise KeyError(f"no attempt in the chain produced {key!r}")

    def parent_outputs(self, step: str) -> dict:
        """Outputs of the nearest ancestor attempt of ``step``."""
        a = self.parent
        while a is not None:
            if a.step == step:
                return a.outputs
            a = a.parent
        raise KeyError(f"no ancestor attempt of step {step}")


class _Processes:
    """The tool processes an attempt has running (sipath.tools.process registers them): what Flow.cancel kills
    besides the attempt's OpenROAD session."""

    def __init__(self, flow: "Flow", attempt_id: str):
        self.flow, self.id = flow, attempt_id

    def add(self, p):
        with self.flow._lock:
            self.flow._procs.setdefault(self.id, set()).add(p)
        if self.flow.cancelled:  # cancelled while it was starting
            p.kill()

    def discard(self, p):
        with self.flow._lock:
            self.flow._procs.get(self.id, set()).discard(p)

    @property
    def cancelled(self) -> bool:
        return self.flow.cancelled


class Flow:
    def __init__(self, design, pdk, workdir: str, session_factory: Callable, validate_inputs: bool = True,
                 runner_prefixes: tuple[str, ...] = (), config: Config | None = None, resume: bool = False,
                 provenance: dict | None = None, runner=None, checkpoints=None):
        self.runner = runner  # for tool steps (e.g. Yosys); None: such steps cannot run
        # how attempts load, measure and save checkpoints: an opaque path here (default: OpenROAD databases)
        self.checkpoints = checkpoints or registry.checkpoint_store()
        self.design, self.pdk = design.resolve(pdk), pdk  # unset design fields take the PDK's defaults
        self.workdir = os.path.abspath(workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self.session_factory = session_factory
        self.config = config or Config()
        self.provenance = dict(provenance or {})
        self.attempts: list[Attempt] = []
        self.steps: dict[str, StepRun] = {}
        self._lock = threading.RLock()
        self._session_count = 0
        self._cancel = threading.Event()
        self._live: dict[str, object] = {}  # attempt id -> its open session
        self._procs: dict[str, set] = {}  # attempt id -> its running tool processes (_Processes)
        self._t0: float | None = None  # when this process's first step started (the run's wall budget)
        self._started = 0  # attempts started by this process (max_attempts)
        self._budget_stopped: set = set()  # attempts stopped by a budget (status "budget")
        self._step_deadline: dict[str, float] = {}  # step -> its time box's end (step_budget_s)
        self._run_timer = None
        self.allocation: dict[str, dict] = {}  # step -> how many attempts ran at once, with how many threads, why
        self.progress: Callable | None = None  # called with (event, attempt) as attempts start and end
        if validate_inputs:
            design.validate(pdk, runner_prefixes=runner_prefixes)
        # what the results depend on (fingerprint.inputs); a resume compares it with the recorded run's
        self.inputs = fp_mod.inputs(self.design, pdk, self.config, self.provenance)
        self.input_changes: list[str] = []  # what differs from the recorded run (resume)
        self.stale: list[dict] = []  # steps a resume ran again, and why
        self._unfingerprinted = False  # a graph recorded before fingerprints: steps reused unchecked
        # what the design itself gives the first step: its netlist, when it has one instead of RTL
        self.root_capabilities = {"netlist"} if (getattr(self.design, "netlist", None)
                                                  and not getattr(self.design, "rtl", None)) else set()
        self._unchecked: set = set()  # steps that declare no prerequisites, warned about once
        if resume:
            self._load_graph()

    # ------------------------------------------------------------------ resume
    def _load_graph(self):
        path = os.path.join(self.workdir, "graph.json")
        if not os.path.isfile(path):
            return
        with open(path) as f:
            g = json.load(f)
        if g.get("design") != self.design.name:
            raise ValueError(f"{path} is a graph of design {g.get('design')!r}, not {self.design.name!r}")
        if g.get("inputs"):
            self.input_changes = fp_mod.changes(g["inputs"], self.inputs)
            if self.input_changes:
                log.warning("resume: %s; the recorded steps run again", "; ".join(self.input_changes))
        elif g.get("attempts"):
            self._unfingerprinted = True
            log.warning("resume: %s was written without input fingerprints; recorded steps are reused without "
                        "checking the inputs", path)
        by_id = {}
        for j in g["attempts"]:
            a = Attempt(id=j["id"], step=j["step"], index=j["index"], params=j["params"],
                        parent=by_id.get(j["parent"]) if j["parent"] else None,
                        dir=j.get("dir") or os.path.join(self.workdir, j["step"], str(j["index"])),
                        kind=j.get("kind", "step"),
                        status=j["status"], checkpoint=j.get("checkpoint"), metrics=j.get("metrics") or {},
                        outputs=j.get("outputs") or {}, error=j.get("error"),
                        score=tuple(j["score"]) if isinstance(j.get("score"), list) else j.get("score"),
                        seconds=j.get("seconds") or 0.0, started_at=j.get("started_at"),
                        finished_at=j.get("finished_at"), process=j.get("process"), timing=j.get("timing") or {},
                        transcript_tail=j.get("transcript_tail") or [], debug_checkpoint=j.get("debug_checkpoint"),
                        resumed=True, retry_of=j.get("retry_of"), retry_reason=j.get("retry_reason"),
                        adapted_from=j.get("adapted_from"), adapt_reason=j.get("adapt_reason"),
                        fingerprint=j.get("fingerprint"), stale_reason=j.get("stale_reason"),
                        capabilities=j.get("capabilities"))
            if j["parent"] and a.parent is None:
                raise ValueError(f"{path}: attempt {a.id} names unknown parent {j['parent']}")
            if a.status in ("running", "pending"):
                a.status = "interrupted"  # the process that ran it is gone
            if a.status in ("ok", "selected", "rejected") and not (a.checkpoint and os.path.isfile(a.checkpoint)):
                a.status, a.error = "interrupted", f"checkpoint {a.checkpoint} missing on resume"
            by_id[a.id] = a
            self.attempts.append(a)
        for name, s in (g.get("steps") or {}).items():
            atts = [a for a in self.attempts if a.step == name]
            sel = by_id.get(s.get("selected")) if s.get("selected") else None
            if sel is not None and sel.status != "selected":
                sel = None  # its checkpoint is gone: the step runs again
            if sel is None:
                continue
            self.steps[name] = StepRun(name, atts, s.get("score"), sel, s.get("parent"), s.get("variants"),
                                       resumed=True, rounds=list(s.get("rounds") or []),
                                       fingerprint=s.get("fingerprint"))
        log.info("resumed %s: %d attempts, %d completed steps", path, len(self.attempts), len(self.steps))

    def _defaults(self, name):
        d = params_mod.DECLARED.get(name)
        return {k: p.default for k, p in sorted(d.items())} if d else None

    def _attempt_fp(self, name, params, parent) -> str:
        return fp_mod.attempt(self.inputs["digest"], name, params, parent.fingerprint if parent else None,
                              self._defaults(name))

    def _step_fp(self, name, variants, score, parent) -> str:
        return fp_mod.step(self.inputs["digest"], name, variants, score, parent.fingerprint if parent else None,
                           self._defaults(name))

    def _recorded(self, name, parent, variants, score) -> StepRun | None:
        """The recorded result of an identical step, if this flow was resumed. A recorded step whose fingerprint
        differs (the run's inputs, its parent, variants, score or declared defaults changed) is made stale and
        runs again: None. Asked again in this session with other variants: an error, as for any step."""
        run = self.steps.get(name)
        if run is None or not run.resumed:
            return None
        pid = parent.id if parent else None
        same_call = _same(run.variants, variants) and run.score_name == score and run.parent == pid
        if run.reused or run.fingerprint is None:  # handed out already, or a graph without fingerprints
            if not same_call:
                raise ValueError(f"step {name} already ran with other parent/variants/score; use another name "
                                 "or a fresh workdir")
            run.reused = True
            return run
        if same_call and run.fingerprint == self._step_fp(name, variants, score, parent):
            run.reused = True
            return run
        if run.parent != pid:
            why = f"its parent changed ({run.parent} -> {pid}): an earlier step ran again"
        elif not same_call:
            why = "its variants or score changed"
        else:
            why = "; ".join(self.input_changes) or "the step's declared parameter defaults changed"
        self._make_stale(name, why)
        return None

    def check_prerequisites(self, name: str, fn: Callable, parent: "Attempt | None", variants=None) -> None:
        """MissingPrerequisite when ``parent``'s chain lacks what ``fn`` needs for any of ``variants``.
        A parent from a graph without capabilities, or a step that declares none, is not checked."""
        have = set(parent.capabilities) if parent is not None and parent.capabilities is not None else (
            None if parent is not None else set(self.root_capabilities))
        miss, declared = set(), False
        for v in variants or [{}]:
            n = needs(fn, v)
            if n is None:
                continue
            declared = True
            if have is not None:
                miss |= n - have
        if not declared and name not in self._unchecked:
            self._unchecked.add(name)
            log.warning("step %s declares no prerequisites (registry.step requires=/provides=): not checked", name)
        if miss:
            raise MissingPrerequisite(missing_text(name, miss, have))

    def _capabilities(self, fn, parent) -> list | None:
        """What the chain has after an attempt of ``fn``; None (unknown, so later steps are not checked) after a
        step that declares nothing (a stand-in, an undeclared plugin) or a parent whose capabilities are unknown."""
        have = parent.capabilities if parent is not None else sorted(self.root_capabilities)
        if have is None or fn is None or not (hasattr(fn, "requires") or hasattr(fn, "provides")):
            return None
        return sorted(set(have) | set(getattr(fn, "provides", ()) or ()))

    def _make_stale(self, name, why):
        """The recorded attempts of ``name`` are kept, marked stale with ``why``, and never reused."""
        with self._lock:
            for a in self.attempts:
                if a.step == name and a.status != "stale":
                    a.status, a.stale_reason = "stale", why
            self.steps.pop(name, None)
        self.stale.append({"step": name, "reason": why})
        log.warning("step %s: recorded result not reused: %s", name, why)

    def _base_index(self, name) -> int:
        """First index for new attempts of ``name``: after its stale ones (attempt ids stay unique)."""
        return max([a.index + 1 for a in self.attempts if a.step == name and a.status == "stale"] + [0])

    def _forget_unfinished(self, name):
        """Drop attempts of ``name`` that did not finish with a checkpoint and
        may be tried again: interrupted or failed tries recorded by an earlier
        run, and cancelled tries. Finished ones are reused; stale ones stay, as a record."""
        with self._lock:
            self.attempts = [a for a in self.attempts
                             if not (a.step == name and a.status != "stale"
                                     and ((a.resumed and a.status != "ok") or a.status == "cancelled"))]

    # ---------------------------------------------------------------- budgets
    def _over_budget(self, a: "Attempt") -> str | None:
        """Why ``a`` may not start (a budget ran out), or None."""
        c, now = self.config, time.time()
        if float(getattr(c, "budget_s", 0) or 0) and self._t0 is not None and now - self._t0 >= c.budget_s:
            return f"run budget of {c.budget_s:g} s reached"
        if int(getattr(c, "max_attempts", 0) or 0) and self._started >= c.max_attempts:
            return f"attempt budget of {c.max_attempts} reached"
        end = self._step_deadline.get(a.step)
        if end is not None and now >= end:
            return f"step budget of {c.step_budget_s:g} s reached"
        return None

    def _start_run_clock(self):
        if self._t0 is not None:
            return
        self._t0 = time.time()
        b = float(getattr(self.config, "budget_s", 0) or 0)
        if b:
            self._run_timer = threading.Timer(b, self._stop_for_budget, (None, f"run budget of {b:g} s reached"))
            self._run_timer.daemon = True
            self._run_timer.start()

    def _stop_for_budget(self, step: str | None, why: str):
        """Stop the running attempts (of ``step``; all with None) for a budget: their status is "budget"."""
        with self._lock:
            live = [(aid, s) for aid, s in self._live.items()] + \
                   [(aid, p) for aid, ps in self._procs.items() for p in list(ps)]
            live = [(aid, s) for aid, s in live if step is None or aid.rsplit(".", 1)[0] == step]
            self._budget_stopped |= {aid for aid, _ in live}
        for aid, s in live:
            log.warning("budget: %s, stopping %s", why, aid)
            kill = getattr(s, "kill", None)
            if kill:
                kill()

    def _budget_reason(self, a: "Attempt") -> str:
        c = self.config
        if self._step_deadline.get(a.step) is not None and float(getattr(c, "step_budget_s", 0) or 0):
            return f"step budget of {c.step_budget_s:g} s reached"
        return f"run budget of {float(getattr(c, 'budget_s', 0) or 0):g} s reached"

    def _memory_estimate(self, name: str) -> float | None:
        """MB one attempt of ``name`` needs: the largest peak recorded for the step, else for any step, else None."""
        def peaks(atts):
            return [a.metrics.get("mem_peak_mb") for a in atts
                    if isinstance(a.metrics.get("mem_peak_mb"), (int, float))]
        mine = peaks([a for a in self.attempts if a.step == name])
        anyp = mine or peaks(self.attempts)
        return max(anyp) if anyp else None

    def _admit(self, name: str, todo: list, parallel: int) -> int:
        """Attempts of ``name`` that may run at once: the step's parallel (and parallel_cap), fewer when the attempts'
        explicit threads or the memory budget would not fit; recorded in ``allocation``."""
        k, why = self.config.cap(parallel), []
        cores = max(1, int(getattr(self.config, "threads", 1) or 1))
        explicit = [int(a.params["threads"]) for a in todo if a.params.get("threads")]
        if explicit and k * max(explicit) > cores:
            k = max(1, cores // max(explicit))
            why.append(f"{max(explicit)} threads each in {cores} cores")
        gb = float(getattr(self.config, "memory_gb", 0) or 0)
        est = self._memory_estimate(name) if gb else None
        if est:
            fit = max(1, int(gb * 1024 // est))
            if fit < k:
                k, _ = fit, why.append(f"{est:.0f} MB each in {gb:g} GB")
        k = max(1, min(k, len(todo) or 1))
        self.allocation[name] = {"at_once": k, "threads_each": self._threads({}, k) if not explicit else max(explicit),
                                 "cores": cores, **({"memory_mb_each": est} if est else {}),
                                 **({"limited_by": why} if why else {})}
        return k

    # ---------------------------------------------------------------- cancel
    def cancel(self):
        """Stop running attempts (their OpenROAD processes and their tool processes are killed) and keep
        new ones from starting, until ``reset_cancel``."""
        self._cancel.set()
        with self._lock:
            live = list(self._live.items()) + [(aid, p) for aid, ps in self._procs.items() for p in list(ps)]
        for aid, s in live:
            log.warning("cancel: killing %s (%s)", aid, getattr(s, "name", s))
            kill = getattr(s, "kill", None)
            if kill:
                kill()

    def reset_cancel(self):
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _notify(self, event, a):
        if self.progress:
            try:
                self.progress(event, a)
            except Exception:  # a progress listener never fails an attempt
                log.exception("progress callback")

    # ------------------------------------------------------------------ steps
    def step(self, name: str, fn: Callable, parent: Attempt | None = None, variants: list[dict] | None = None,
             score: str | None = None, parallel: int = 1) -> StepRun:
        variants = variants or [{}]
        score = score or "auto"
        score_fn = score_mod.get(score)
        if parent is not None and parent.status != "selected":
            raise ValueError(f"{name}: parent {parent.id} is {parent.status}; continue only from a selected checkpoint")
        self.check_prerequisites(name, fn, parent, variants)
        self._start_run_clock()
        rec = self._recorded(name, parent, variants, score)
        if rec is not None:
            log.info("step %s: recorded result reused (selected %s)", name, rec.selected.id)
            return rec
        if name in self.steps:
            raise ValueError(f"step {name} already ran")
        self._forget_unfinished(name)
        if self.cancelled:
            raise StepCancelled(f"step {name}: flow is cancelled")
        live = [a for a in self.attempts if a.step == name and a.status != "stale"]
        if any(a.status != "ok" for a in live):
            raise ValueError(f"step {name} already ran")
        base = self._base_index(name)
        kept = {a.index - base: a for a in live}  # finished before an interruption
        fps = [self._attempt_fp(name, v, parent) for v in variants]
        if any(not (_same(a.params, variants[k]) and a.parent is parent and a.fingerprint in (None, fps[k]))
               for k, a in kept.items() if k < len(variants)):
            self._make_stale(name, "; ".join(self.input_changes)
                             or "its recorded attempts have other params, parent or inputs")
            base, kept = self._base_index(name), {}
        attempts = []
        for k, v in enumerate(variants):
            if k in kept:
                attempts.append(kept[k])  # not run again
            else:
                a = self._new_attempt(name, base + k, v, parent, kind="tool" if getattr(fn, "tool", False) else "step",
                                      fn=fn)
                a.fingerprint = fps[k]
                attempts.append(a)
        self._run_batch(name, attempts, fn, parallel, parent)
        return self._select(name, attempts, score, score_fn, parent=parent, variants=variants,
                            fingerprint=self._step_fp(name, variants, score, parent))

    def _run_batch(self, name, attempts, fn, parallel, parent):
        """Run the attempts not finished yet (at most ``parallel`` at once), then one retry for each that asked for
        it (RetryWith), appended to ``attempts``; StepCancelled when any was cancelled."""
        todo = [a for a in attempts if a.status != "ok"]
        k = self._admit(name, todo, parallel)
        log.info("step %s: %d attempts (%d to run, %d recorded), %d at once", name, len(attempts), len(todo),
                 len(attempts) - len(todo), k)
        together = max(1, min(len(todo), k))  # attempts alive at once share the threads
        sb = float(getattr(self.config, "step_budget_s", 0) or 0)
        timer = None
        if sb and todo:  # a time box: what has not finished by then stops, the step selects among the rest
            self._step_deadline[name] = time.time() + sb
            timer = threading.Timer(sb, self._stop_for_budget, (name, f"step budget of {sb:g} s reached"))
            timer.daemon = True
            timer.start()
        try:
            with ThreadPoolExecutor(max_workers=k) as ex:
                list(ex.map(lambda a: self._run_attempt(a, fn, together), todo))
        finally:
            if timer is not None:
                timer.cancel()
            self._step_deadline.pop(name, None)
        # a failed attempt that asked for it (RetryWith) gets one fresh attempt with the merged params
        nxt = max(a.index for a in self.attempts if a.step == name) + 1
        for a in [a for a in todo if a.status == "failed" and a.retry]:
            r = self._new_attempt(name, nxt, {**a.params, **a.retry}, parent, kind=a.kind, fn=fn)
            r.fingerprint = self._attempt_fp(name, r.params, parent)
            r.retry_of, r.retry_reason, nxt = a.id, a.retry_why, nxt + 1
            r.adapted_from, r.adapt_reason = a.adapted_from, a.adapt_reason
            log.info("attempt %s: retry of %s with %s", r.id, a.id, a.retry)
            self._run_attempt(r, fn, 1)
            attempts.append(r)
        if any(a.status == "cancelled" for a in attempts):
            self.save_graph()
            raise StepCancelled(f"step {name} cancelled: " + ", ".join(f"{a.id} {a.status}" for a in attempts))
        run_out = [a for a in attempts if a.status == "budget" and not (a.error or "").startswith("step budget")]
        if run_out:  # the run's budget, not this step's time box: stop here, resumable
            self.save_graph()
            raise BudgetExceeded(f"step {name}: {run_out[0].error}; --resume continues")

    def extend(self, name: str, fn: Callable, variants: list[dict], origins: list[dict] | None = None,
               parallel: int = 1) -> StepRun:
        """More attempts of a step that already ran (an adaptive round): from the same parent, ranked by the same
        score, then the step's selection is made again over all its attempts. ``origins`` (one per variant):
        {"from": attempt id, "reason": text}, recorded as the attempt's ``adapted_from`` and ``adapt_reason``.
        A variant equal to an attempt the step already has (a resumed round) is not run again. The round is
        recorded in the step's ``rounds``."""
        run = self.steps.get(name)
        if run is None:
            raise ValueError(f"step {name} has not run: nothing to extend")
        origins = list(origins or [{}] * len(variants))
        if len(origins) != len(variants):
            raise ValueError(f"step {name}: {len(variants)} variants but {len(origins)} origins")
        parent = next((a for a in self.attempts if a.id == run.parent), None) if run.parent else None
        mine = [a for a in self.attempts if a.step == name and a.status in ("ok", "selected", "rejected")]
        nxt = max([a.index for a in self.attempts if a.step == name] + [-1]) + 1
        new = []
        for v, o in zip(variants, origins):
            old = next((a for a in mine if _same(a.params, v)), None)
            if old is not None:
                continue  # already tried (by an earlier round, or recorded before a resume)
            a = self._new_attempt(name, nxt, v, parent, kind="tool" if getattr(fn, "tool", False) else "step", fn=fn)
            a.fingerprint = self._attempt_fp(name, v, parent)
            a.adapted_from, a.adapt_reason, nxt = o.get("from"), o.get("reason"), nxt + 1
            new.append(a)
        before = run.selected.id if run.selected else None
        if new:
            self._run_batch(name, new, fn, parallel, parent)
        everything = [a for a in self.attempts if a.step == name and a.status != "stale"]
        for a in everything:
            if a.status in ("selected", "rejected"):
                a.status = "ok"  # every finished attempt competes again
        rounds = run.rounds + [{"from": before, "attempts": [a.id for a in new]}]
        out = self._select(name, everything, run.score_name, score_mod.get(run.score_name), parent=parent,
                           variants=run.variants, rounds=rounds, fingerprint=run.fingerprint)
        out.rounds[-1]["selected"] = out.selected.id
        self.save_graph()
        return out

    def stop_rounds(self, name: str, reason: str) -> StepRun:
        """Record why a step's adaptive rounds stopped (nothing hot, nothing new to try), so a resume stops too."""
        run = self.steps[name]
        run.rounds = run.rounds + [{"from": run.selected.id if run.selected else None, "stopped": reason}]
        self.save_graph()
        return run

    def _name_taken(self, name: str) -> bool:
        return name in self.steps or any(a.step == name and a.status != "stale" for a in self.attempts)

    def eco(self, name: str, parent: Attempt, ops: list[dict], fn: Callable | None = None,
            max_patch_nets: int = 25, score: str | None = None, max_slack_loss: float | None = 0.05) -> StepRun:
        """ECO from ``parent``. Rejected (checkpoint kept, StepFailed raised) when
        setup or hold slack drops by more than ``max_slack_loss`` ns against the
        parent (None disables the check)."""
        score = score or "drc_then_slack"
        params = {"ops": ops, "max_patch_nets": max_patch_nets}
        efp = self._attempt_fp(name, params, parent)
        run = self.steps.get(name)
        if run is not None and run.resumed and not run.reused:
            if _same(run.variants, [params]) and run.parent == parent.id and run.fingerprint in (None, efp):
                run.reused = True
                return run  # the same ECO, recorded by an earlier run
            self._make_stale(name, "; ".join(self.input_changes) or "its parent or operations changed")
        self._forget_unfinished(name)
        if self._name_taken(name):
            raise EcoRefused(f"ECO {name}: a step or attempt with that name exists; its checkpoints would be lost")
        if parent.status != "selected":
            raise EcoRefused(f"ECO {name}: parent {parent.id} is {parent.status}, not the selected checkpoint")
        if parent.capabilities is not None and "routed" not in parent.capabilities:
            raise MissingPrerequisite(f"ECO {name}: needs a routed parent; {parent.id} has "
                                      f"{', '.join(parent.capabilities) or 'nothing'}")
        child = [a for a in self.attempts if a.parent is parent and a.status == "selected"]
        if child:
            raise EcoRefused(f"ECO {name}: parent {parent.id} already continued as {child[0].id}; "
                             "start from the head of the selected chain")
        if fn is None:
            fn = registry.eco_runner()
        a = self._new_attempt(name, self._base_index(name), params, parent, kind="eco")
        a.fingerprint, a.capabilities = efp, parent.capabilities  # an ECO keeps what its parent had
        self._run_attempt(a, fn)
        if a.status == "cancelled":
            self.save_graph()
            raise StepCancelled(f"ECO {name} cancelled")
        if a.status == "ok" and max_slack_loss is not None:
            lost = []
            for k, label in (("wns", "setup"), ("whs", "hold")):  # each checked on its own
                b, c = parent.metrics.get(k), a.metrics.get(k)
                if isinstance(b, (int, float)) and isinstance(c, (int, float)) and c < b - max_slack_loss:
                    lost.append(f"{label} {c:.3f} ns vs parent {b:.3f} ns")
            if lost:
                a.status = "rejected"
                a.error = f"ECO regressed timing: {'; '.join(lost)} (allowed loss {max_slack_loss} ns)"
        return self._select(name, [a], score, score_mod.get(score), parent=parent, variants=[params],
                            fingerprint=efp)

    # --------------------------------------------------------------- internals
    def _new_attempt(self, step, k, params, parent, kind="step", fn=None) -> Attempt:
        # a directory is never reused: a retry (after a cancel or an interruption)
        # gets a fresh one and the old one stays for inspection. (Reusing a just
        # deleted path also confuses Docker Desktop's file sharing.)
        d = os.path.join(self.workdir, step, str(k))
        n = 0
        while os.path.exists(d):
            n += 1
            d = os.path.join(self.workdir, step, f"{k}_r{n}")
        a = Attempt(id=f"{step}.{k}", step=step, index=k, params=dict(params), parent=parent, dir=d, kind=kind,
                    capabilities=self._capabilities(fn, parent))
        with self._lock:
            self.attempts.append(a)
        return a

    def _threads(self, params: dict, together: int = 1) -> int:
        """OpenROAD threads for one attempt: the step's ``threads`` parameter
        when set, else the run's ``threads`` divided among the ``together``
        attempts running at once (at least 1)."""
        given = params.get("threads")
        if given:
            return int(given)
        return max(1, int(getattr(self.config, "threads", 1) or 1) // max(1, together))

    def _run_attempt(self, a: Attempt, fn: Callable, together: int = 1):
        if self.cancelled:
            a.status, a.error = "cancelled", "cancelled before it started"
            self.save_graph()
            return
        why = self._over_budget(a)
        if why:
            a.status, a.error = "budget", f"{why}: not started"
            self.save_graph()
            return
        with self._lock:
            self._started += 1
        a.status = "running"
        a.resumed = False
        t0 = time.time()
        phases = {}
        odb = self.checkpoints.path(a.dir, a.step, a.index)
        session = None

        def mark(phase, since):
            phases[phase] = round(time.time() - since, 3)
            return time.time()

        try:
            self._guard_dir(a)
            if os.path.isdir(a.dir) and os.listdir(a.dir):
                raise RuntimeError(f"{a.id}: attempt directory {a.dir} is not empty; it is never reused")
            os.makedirs(a.dir, exist_ok=True)
            a.started_at = time.time()
            log.info("attempt %s: start %s", a.id, a.params)
            self._notify("start", a)
            if a.kind == "tool":
                self._run_tool_attempt(a, fn, t0, mark)
                return
            session = self.session_factory(a.dir)
            a.process = getattr(session, "name", type(session).__name__)
            with self._lock:  # one process per attempt, never shared
                owner = getattr(session, "_flow_attempt", None)
                if owner is not None:
                    raise RuntimeError(f"session already used by attempt {owner}")
                session._flow_attempt = a.id
                self._session_count += 1
                a.session_id = self._session_count
                self._live[a.id] = session
            if self.cancelled:  # cancelled while the process was starting
                raise RuntimeError("cancelled")
            t1 = mark("start", t0)
            decl = "eco" if a.kind == "eco" else getattr(fn, "__name__", a.step)
            params = params_mod.resolve(decl, a.params, self.pdk, self.design)
            threads = self._threads(params, together)
            started = getattr(getattr(session, "runner", None), "threads", getattr(self.config, "threads", None))
            self.checkpoints.prepare(session, threads, started)
            self.checkpoints.load(session, self.pdk, self.design, a.parent)
            t1 = mark("load", t1)
            # the step sees every declared parameter (variant, then design and PDK
            # step_defaults, then the declared default); the attempt records the variant
            ctx = Ctx(session, self.design, self.pdk, params, a.dir, a.parent, a, config=self.config,
                      threads=threads, processes=_Processes(self, a.id))
            fn(ctx)
            t1 = mark("step", t1)
            metrics = self.checkpoints.metrics(session)
            metrics.update(ctx.metrics)
            t1 = mark("metrics", t1)
            self.checkpoints.save(session, odb)
            t1 = mark("write_db", t1)
            if ctx.after_save:
                for observe in ctx.after_save:
                    try:
                        observe(ctx)
                    except Exception as e:  # an observation never fails the attempt it observes
                        ctx.outputs.setdefault("after_save_errors", []).append(f"{type(e).__name__}: {e}")
                metrics.update({k: v for k, v in ctx.metrics.items() if k not in metrics})
                mark("after_save", t1)
            a.metrics, a.outputs, a.checkpoint = metrics, ctx.outputs, odb
            a.status = "ok"
            log.info("attempt %s: ok in %.1fs", a.id, time.time() - t0)
        except Exception as e:  # the attempt fails; nothing is invented
            a.status = ("budget" if a.id in self._budget_stopped else "cancelled" if self.cancelled else "failed")
            a.error = f"{type(e).__name__}: {e}"
            if a.status == "budget":
                a.error = f"{self._budget_reason(a)}: stopped ({a.error.splitlines()[0][:200]})"
            if isinstance(e, RetryWith) and a.retry_of is None:  # one retry, never a retry of a retry
                a.retry, a.retry_why = e.params, e.reason
            log.warning("attempt %s: %s: %s", a.id, a.status, str(e).splitlines()[0] if str(e) else type(e).__name__)
            try:
                os.makedirs(a.dir, exist_ok=True)
                with open(os.path.join(a.dir, "error.txt"), "w") as f:
                    f.write(traceback.format_exc())
                if os.path.exists(odb):
                    os.remove(odb)
            except OSError:
                pass
            if a.status == "failed":
                self._forensics(a, session)
        finally:
            with self._lock:
                self._live.pop(a.id, None)
                leftover = list(self._procs.pop(a.id, ()))
            for p in leftover:  # a tool process its step left running dies with the attempt
                if getattr(p, "alive", lambda: False)():
                    log.warning("attempt %s: killing %s, still running after its step", a.id, getattr(p, "name", p))
                    p.kill()
            if session is not None:
                try:
                    session.close()
                except Exception as e:  # recorded, not hidden
                    a.outputs["close_error"] = f"{type(e).__name__}: {e}"
            a.finished_at = time.time()
            a.seconds = time.time() - t0
            a.timing = {"phases": phases, "slowest": _slowest(session)}
            self._notify("end", a)
            self.save_graph()

    def _run_tool_attempt(self, a: Attempt, fn: Callable, t0: float, mark):
        """A tool step: no OpenROAD process. The step writes its result file and
        names it in ``outputs["checkpoint"]``; a missing or empty file fails it."""
        if self.cancelled:
            raise RuntimeError("cancelled")
        a.process = "tool"
        decl = getattr(fn, "__name__", a.step)
        ctx = Ctx(None, self.design, self.pdk, params_mod.resolve(decl, a.params, self.pdk, self.design),
                  a.dir, a.parent, a, runner=self.runner, config=self.config, processes=_Processes(self, a.id))
        fn(ctx)
        mark("step", t0)
        out = ctx.outputs.get("checkpoint")
        if not out or not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise RuntimeError(f"tool step {a.step} wrote no result (outputs['checkpoint'] = {out!r})")
        a.metrics, a.outputs, a.checkpoint = dict(ctx.metrics), ctx.outputs, out
        a.status = "ok"
        log.info("attempt %s: ok in %.1fs", a.id, time.time() - t0)

    def _guard_dir(self, a: Attempt):
        """Never delete a directory that holds a selected attempt's checkpoint."""
        for o in self.attempts:
            if o is not a and o.status == "selected" and o.checkpoint and \
                    os.path.abspath(o.checkpoint).startswith(os.path.abspath(a.dir) + os.sep):
                raise RuntimeError(f"{a.id}: refusing to reuse {a.dir}, it holds selected {o.id}'s checkpoint")

    def _forensics(self, a: Attempt, session):
        """Transcript tail always; with ``config.debug_failed`` the failed
        database too, written apart from checkpoints (never usable as a parent)."""
        logf = os.path.join(a.dir, "openroad.log")
        if os.path.isfile(logf):
            with open(logf, errors="replace") as f:
                lines = [ln.rstrip("\n") for ln in f if not ln.startswith("# | ")]
            a.transcript_tail = lines[-40:]
        if self.config.debug_failed and session is not None and not getattr(session, "closed", False):
            dbg = self.checkpoints.path(os.path.join(a.dir, "debug"), a.step, f"{a.index}.failed")
            try:
                os.makedirs(os.path.dirname(dbg), exist_ok=True)
                self.checkpoints.save(session, dbg, timeout=600)
                if os.path.isfile(dbg) and os.path.getsize(dbg) > 0:
                    a.debug_checkpoint = dbg
            except Exception as e:  # the session may be what failed
                a.outputs["debug_checkpoint_error"] = f"{type(e).__name__}: {e}"

    def _select(self, name, attempts, score_name, score_fn, parent=None, variants=None, rounds=None,
                fingerprint=None) -> StepRun:
        ok = [a for a in attempts if a.status == "ok"]
        if getattr(score_fn, "selector", False):  # a rule over all attempts (e.g. synth_select)
            best, why = score_fn(ok) if ok else (None, "no successful attempt")
            for a in ok:
                a.score = 1 if a is best else 0
            if best is not None:
                best.outputs["selection"] = why
        else:
            for a in ok:
                a.score = score_fn(a.metrics)
            ok = [a for a in ok if a.score is not None]
            best = max(ok, key=lambda a: a.score) if ok else None
        for a in attempts:
            if a.status == "ok":
                a.status = "selected" if a is best else "rejected"
        run = StepRun(name, attempts, score_name, best, parent.id if parent else None, variants,
                      rounds=list(rounds if rounds is not None else getattr(self.steps.get(name), "rounds", [])),
                      fingerprint=fingerprint, reused=True)
        self.steps[name] = run
        self.save_graph()
        log.info("step %s: selected %s", name, best.id if best else None)
        if best is None:
            errs = "\n".join(f"  {a.id}: {a.error or 'no score'}" for a in attempts)
            raise StepFailed(f"step {name}: no successful attempt\n{errs}")
        return run

    # -------------------------------------------------------------------- graph
    def graph(self) -> dict:
        return {"design": self.design.name, "pdk": getattr(self.pdk, "name", None),
                "provenance": self.provenance, "config": self.config.to_dict(), "inputs": self.inputs,
                **({"allocation": self.allocation} if self.allocation else {}),
                **({"stale": self.stale} if self.stale else {}),
                "attempts": [a.to_json() for a in self.attempts],
                "steps": {k: {"score": v.score_name, "selected": v.selected.id if v.selected else None,
                              "parent": v.parent, "variants": v.variants, "fingerprint": v.fingerprint,
                              **({"rounds": v.rounds} if v.rounds else {})}
                          for k, v in self.steps.items()}}

    def verification(self) -> dict:
        """What the run established (sipath.verdict.verify): execution and verification status, each check."""
        from . import verdict
        return verdict.verify(self.graph())

    def save_graph(self):
        with self._lock:  # attempts finish concurrently
            data = self.graph()
            try:  # the verdict follows the graph; reading a result file never fails the run
                from . import verdict
                data["verification"] = verdict.verify(data)
            except Exception as e:  # noqa: BLE001
                log.warning("verification verdict not written: %s", e)
            tmp = os.path.join(self.workdir, "graph.json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, default=str)
            os.replace(tmp, os.path.join(self.workdir, "graph.json"))
            try:  # the run's report page follows the graph; it never fails the run
                from . import report
                report.write(self.workdir)
            except Exception as e:  # noqa: BLE001
                log.warning("run report not written: %s", e)


def _slowest(session, n: int = 8) -> list:
    """The ``n`` slowest Tcl commands of a session: [label, seconds]."""
    times = getattr(session, "timings", None) or []
    return [[lbl, round(sec, 3)] for lbl, sec in sorted(times, key=lambda x: -x[1])[:n]]
