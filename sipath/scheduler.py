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
import weakref
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


def run_chain(g: dict) -> list[dict]:
    """The run's result as a chain of attempt records (graph.json), first step first, ending at the run's head.
    Every consumer of "the result" (verdict, report, CLI, benchmark export) reads it here. A graph from before
    heads were recorded: the latest step that selected an attempt, its ancestry."""
    by_id = {a["id"]: a for a in g.get("attempts") or []}
    head = by_id.get(g.get("head")) if g.get("head") else None
    if head is None and "head" not in g:
        steps = g.get("steps") or {}
        head = next((by_id.get(s["selected"]) for s in reversed(list(steps.values())) if s.get("selected")), None)
    chain = []
    while head is not None:
        chain.append(head)
        head = by_id.get(head.get("parent")) if head.get("parent") else None
    return list(reversed(chain))


def in_chain(chain: list[dict], step: str) -> dict:
    """The chain's latest attempt of ``step`` (a branch's ``step@...`` too), or {}."""
    return next((a for a in reversed(chain) if a["step"] == step or a["step"].startswith(step + "@")), {})


def declared_name(fn, name: str) -> str:
    """The name ``fn``'s parameters are declared under: the one it was registered as (registry.register_step, a
    ``module:function`` step), else its function name; ``name`` without a function."""
    if fn is None:
        return name
    return getattr(fn, "declared_as", None) or getattr(fn, "__name__", name)


def resolve_step_params(fn, name: str, params, pdk=None, design=None) -> dict:
    """The parameters a step runs with: the variant, then the design's and the PDK's step_defaults, then the
    declared defaults, by the step function's declared name (an alias, ``step: x`` with ``fn: place``, resolves
    as place). The one resolution execution, prerequisites, admission, fingerprints and the plan all use."""
    decl = declared_name(fn, name)
    try:
        return params_mod.resolve(decl, params or {}, pdk, design)
    except Exception:  # noqa: BLE001  (an undeclared step: its variant as given)
        return dict(params or {})


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


def chain_problems(root: set, chain, resolve=None) -> list[str]:
    """Check a whole flow before it runs: ``chain`` is [(name, fn, variants)], each continuing from the one
    before; the problems (a step whose chain lacks what it needs), in order. ``resolve(fn, name, variant)``:
    the parameters the step will run with (step_defaults too), as Flow._resolve gives them."""
    have, out = set(root), []
    for name, fn, variants in chain:
        miss = set()
        for v in variants or [{}]:
            n = needs(fn, resolve(fn, name, v) if resolve else v)
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
    forked_from: str | None = None  # "<original run's workdir>:<attempt id>" when a fork took it over (Flow.fork)
    threads: int | None = None  # OpenROAD threads the scheduler gave it (None: a tool step, no OpenROAD process)
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
            "capabilities": self.capabilities, **({"forked_from": self.forked_from} if self.forked_from else {}),
            "threads": self.threads,
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
    kept: list = field(default_factory=list)  # attempts carried on as branches (Flow.keep), selected first


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
        if self.cancelled:  # cancelled (or out of budget) while it was starting
            p.kill()

    def discard(self, p):
        with self.flow._lock:
            self.flow._procs.get(self.id, set()).discard(p)

    @property
    def cancelled(self) -> bool:
        """The flow was cancelled, or a budget stopped this attempt: no process of it may start or go on."""
        return self.flow.cancelled or self.id in self.flow._budget_stopped


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
        self._lock_fd, self._superseded = None, False
        self._claim_workdir()
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
        self.decisions: list[dict] = []  # bounded search: where branches were compared and which one won
        # the run's result: the latest selection on the main chain, a search's winner, an accepted ECO. Every
        # consumer (verdict, report, CLI, benchmark export) reads the result through this head's ancestry.
        self.head: Attempt | None = None
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

    def _claim_workdir(self):
        """One run writes a workdir at a time: another process holding it is refused (an advisory lock on
        <workdir>/.sipath.lock); in this process, a newer Flow takes it over from an idle older one, which no
        longer writes graph.json, and one with attempts running is refused."""
        key = os.path.realpath(self.workdir)
        with _OPEN_LOCK:
            prev = _OPEN.get(key)
            prev = prev() if prev is not None else None
            if prev is not None and prev is not self and prev._lock_fd is not None:
                if prev._live or prev._procs and any(prev._procs.values()):
                    raise RuntimeError(f"{self.workdir} is in use by a run in this process (attempts running)")
                prev._superseded = True
                self._lock_fd, prev._lock_fd = prev._lock_fd, None
            else:
                fd = os.open(os.path.join(self.workdir, ".sipath.lock"), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:  # no advisory locks here (Windows): not checked
                    pass
                except OSError:
                    os.close(fd)
                    raise RuntimeError(f"{self.workdir} is in use by another process (a run holds "
                                       f"{os.path.join(self.workdir, '.sipath.lock')})") from None
                self._lock_fd = fd
            _OPEN[key] = weakref.ref(self)

    def _check_owner(self):
        """Refuse work on a Flow that no longer owns its workdir (a newer Flow took it over) or was closed: before
        any step function, tool or graph write, never after."""
        if self._superseded:
            raise RuntimeError(f"{self.workdir} was opened again by a newer run in this process; this one no "
                               "longer runs or writes anything")
        if self._lock_fd is None and getattr(self, "_closed", False):
            raise RuntimeError(f"this run of {self.workdir} was closed")

    def close(self):
        """Give the workdir up: the lock is released and this Flow refuses further work."""
        self._closed = True
        fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        fd = getattr(self, "_lock_fd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

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
        by_id, lost = {}, []
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
                        capabilities=j.get("capabilities"), forked_from=j.get("forked_from"),
                        threads=j.get("threads"))
            if j["parent"] and a.parent is None:
                raise ValueError(f"{path}: attempt {a.id} names unknown parent {j['parent']}")
            if a.status in ("running", "pending"):
                a.status = "interrupted"  # the process that ran it is gone
            if a.status in ("ok", "selected", "rejected", "kept") and not (a.checkpoint and os.path.isfile(a.checkpoint)):
                # kept as a record (its id is not handed out again; what continued from it is judged by its
                # parent when it is asked for again)
                a.status, a.stale_reason = "stale", f"checkpoint {a.checkpoint} missing on resume"
                lost.append(a)
            by_id[a.id] = a
            self.attempts.append(a)
        self.decisions = list(g.get("decisions") or [])
        for a in lost:
            self.stale.append({"step": a.step, "reason": a.stale_reason})
        self.head = self._valid_tip(by_id.get(g.get("head")) if g.get("head") else None)
        for name, s in (g.get("steps") or {}).items():
            atts = [a for a in self.attempts if a.step == name]
            sel = by_id.get(s.get("selected")) if s.get("selected") else None
            if sel is not None and sel.status != "selected":
                sel = None  # its checkpoint is gone: the step runs again
            if sel is None:
                continue
            self.steps[name] = StepRun(name, atts, s.get("score"), sel, s.get("parent"), s.get("variants"),
                                       resumed=True, rounds=list(s.get("rounds") or []),
                                       fingerprint=s.get("fingerprint"), kept=list(s.get("kept") or []))
        log.info("resumed %s: %d attempts, %d completed steps", path, len(self.attempts), len(self.steps))

    def _resolve(self, fn, name, params) -> dict:
        """resolve_step_params on this run's PDK and design."""
        return resolve_step_params(fn, name, params, self.pdk, self.design)

    def _defaults(self, name):
        d = params_mod.DECLARED.get(name)
        return {k: p.default for k, p in sorted(d.items())} if d else None

    def _attempt_fp(self, name, params, parent, fn=None) -> str:
        return fp_mod.attempt(self.inputs["digest"], name, self._resolve(fn, name, params),
                              parent.fingerprint if parent else None, None, fp_mod.step_code(fn))

    def _step_fp(self, name, variants, score, parent, fn=None) -> str:
        return fp_mod.step(self.inputs["digest"], name, [self._resolve(fn, name, v) for v in variants], score,
                           parent.fingerprint if parent else None, None, fp_mod.step_code(fn))

    def _recorded(self, name, parent, variants, score, fn=None) -> StepRun | None:
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
            self._advance_head(run.selected, name, save=True)
            return run
        if same_call and run.fingerprint == self._step_fp(name, variants, score, parent, fn):
            run.reused = True
            self._advance_head(run.selected, name, save=True)
            return run
        if run.parent != pid:
            why = f"its parent changed ({run.parent} -> {pid}): an earlier step ran again"
        elif not same_call:
            why = "its variants or score changed"
        else:
            why = "; ".join(self.input_changes) or "the step's function, its code (outside sipath) or its declared parameter defaults changed"
        self._make_stale(name, why)
        return None

    def check_prerequisites(self, name: str, fn: Callable, parent: "Attempt | None", variants=None) -> None:
        """MissingPrerequisite when ``parent``'s chain lacks what ``fn`` needs for any of ``variants``.
        A parent from a graph without capabilities, or a step that declares none, is not checked."""
        have = set(parent.capabilities) if parent is not None and parent.capabilities is not None else (
            None if parent is not None else set(self.root_capabilities))
        miss, declared = set(), False
        for v in variants or [{}]:
            n = needs(fn, self._resolve(fn, name, v))  # the parameters execution will see (step_defaults too)
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

    @staticmethod
    def _valid_tip(a):
        """``a``, or its nearest ancestor whose chain is still selected (or kept) all the way up: an attempt
        that continued from a checkpoint no longer selected is not a result."""
        tip, x = a, a
        while x is not None:
            if x.status not in ("selected", "kept"):
                tip = x.parent
            x = x.parent
        return tip

    def _advance_head(self, best, name, save: bool = False) -> bool:
        """The run's head moves to ``best`` when it continues the head's chain, re-selects a step of the head's
        chain (from the same parent: what came after it is no longer the result), or starts a chain (a root
        step); a branch's selection leaves it where it is. ``save``: write graph.json when it moved (a reused
        step: no selection saves it otherwise). Returns whether it moved."""
        h = self._valid_tip(self.head)
        replaces = False
        x = h
        while x is not None and not replaces:
            replaces = x.step == name and x.parent is best.parent if best is not None else False
            x = x.parent
        if best is not None and best is not h and (h is None or best.parent is None or best.parent is h
                                                   or replaces):
            self.head = best
            if save:
                self.save_graph()
            return True
        return False

    def _make_stale(self, name, why):
        """The recorded attempts of ``name`` are kept, marked stale with ``why``, and never reused."""
        with self._lock:
            for a in self.attempts:
                if a.step == name and a.status != "stale":
                    a.status, a.stale_reason = "stale", why
            self.steps.pop(name, None)
        self.head = self._valid_tip(self.head)  # back to what is still a result
        self.stale.append({"step": name, "reason": why})
        log.warning("step %s: recorded result not reused: %s", name, why)

    def _base_index(self, name) -> int:
        """First index for new attempts of ``name``: after its stale ones (attempt ids stay unique)."""
        return max([a.index + 1 for a in self.attempts if a.step == name and a.status == "stale"] + [0])

    def _free_index(self, name, k: int) -> int:
        """``k``, or the next index no attempt of ``name`` in the graph has (an id names one attempt)."""
        used = {a.index for a in self.attempts if a.step == name}
        while k in used:
            k += 1
        return k

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

    def _check_budget_stop(self, a: "Attempt"):
        """A step that returned after a budget stopped it (it caught its processes' cancellation) did not finish:
        its result is not one (status "budget")."""
        if a.id in self._budget_stopped:
            raise RuntimeError("the step returned after its budget stopped it")

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

    def _admit(self, name: str, todo: list, parallel: int, fn=None) -> int:
        """Attempts of ``name`` that may run at once: the step's parallel (and parallel_cap), fewer when the attempts'
        explicit threads or the memory budget would not fit; recorded in ``allocation``."""
        k, why = self.config.cap(parallel), []
        cores = max(1, int(getattr(self.config, "threads", 1) or 1))
        asked = [self._resolve(fn, name, a.params).get("threads") for a in todo]  # step_defaults too
        explicit = [int(x) for x in asked if x]
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
        self._check_owner()
        variants = variants or [{}]
        score = score or "auto"
        score_fn = score_mod.get(score)
        if parent is not None and parent.status not in ("selected", "kept"):
            raise ValueError(f"{name}: parent {parent.id} is {parent.status}; continue only from a selected (or "
                             "kept) checkpoint")
        if parent is not None and self._valid_tip(parent) is not parent:
            gone = next(x for x in _ancestors(parent) if x.status not in ("selected", "kept"))
            raise ValueError(f"{name}: parent {parent.id} continues from {gone.id}, which is {gone.status} now; "
                             "continue from the selected chain")
        self.check_prerequisites(name, fn, parent, variants)
        self._start_run_clock()
        rec = self._recorded(name, parent, variants, score, fn)
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
        fps = [self._attempt_fp(name, v, parent, fn) for v in variants]
        kept = {}  # finished before an interruption: matched to the variant it ran
        for k, v in enumerate(variants):
            a = next((a for a in live if a not in kept.values() and _same(a.params, v) and a.parent is parent
                      and a.fingerprint in (None, fps[k])), None)
            if a is not None:
                kept[k] = a
        if any(a not in kept.values() for a in live):
            self._make_stale(name, "; ".join(self.input_changes)
                             or "its recorded attempts have other params, parent or inputs")
            kept = {}
        base = self._base_index(name)
        attempts = []
        for k, v in enumerate(variants):
            if k in kept:
                attempts.append(kept[k])  # not run again
            else:
                a = self._new_attempt(name, self._free_index(name, base + k), v, parent,
                                      kind="tool" if getattr(fn, "tool", False) else "step", fn=fn)
                a.fingerprint = fps[k]
                attempts.append(a)
        self._run_batch(name, attempts, fn, parallel, parent)
        return self._select(name, attempts, score, score_fn, parent=parent, variants=variants,
                            fingerprint=self._step_fp(name, variants, score, parent, fn))

    def _run_batch(self, name, attempts, fn, parallel, parent):
        """Run the attempts not finished yet (at most ``parallel`` at once), then one retry for each that asked for
        it (RetryWith), appended to ``attempts``; StepCancelled when any was cancelled."""
        todo = [a for a in attempts if a.status != "ok"]
        k = self._admit(name, todo, parallel, fn)
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
            # a failed attempt that asked for it (RetryWith) gets one fresh attempt with the merged params, inside
            # the same time box (the deadline and its timer hold until the retries are done)
            nxt = max(a.index for a in self.attempts if a.step == name) + 1
            for a in [a for a in todo if a.status == "failed" and a.retry]:
                r = self._new_attempt(name, nxt, {**a.params, **a.retry}, parent, kind=a.kind, fn=fn)
                r.fingerprint = self._attempt_fp(name, r.params, parent, fn)
                r.retry_of, r.retry_reason, nxt = a.id, a.retry_why, nxt + 1
                r.adapted_from, r.adapt_reason = a.adapted_from, a.adapt_reason
                log.info("attempt %s: retry of %s with %s", r.id, a.id, a.retry)
                self._run_attempt(r, fn, 1)
                attempts.append(r)
        finally:
            if timer is not None:
                timer.cancel()
            self._step_deadline.pop(name, None)
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
        self._check_owner()
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
            a.fingerprint = self._attempt_fp(name, v, parent, fn)
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
        self._check_owner()
        run = self.steps[name]
        run.rounds = run.rounds + [{"from": run.selected.id if run.selected else None, "stopped": reason}]
        self.save_graph()
        return run

    def result(self, step: str | None = None) -> "Attempt | None":
        """The run's head, or its ancestor of ``step`` (a branch's ``step@...`` too): what the run produced."""
        a = self.head
        while a is not None and step is not None and not (a.step == step or a.step.startswith(step + "@")):
            a = a.parent
        return a

    def keep(self, name: str, k: int, baseline: "Attempt | None" = None) -> list["Attempt"]:
        """Bounded search: the step's best ``k`` finished attempts (the selected one first), plus ``baseline``
        when it finished and is not among them, to be continued as branches. Ranking: a value score orders the
        attempts by value; a selector score (a rule over the attempts, e.g. synth_select) is applied again to
        what remains, so the second is the one the rule would pick without the first, and so on. The others
        than the selected one get status "kept" (a parent like a selected attempt); the list is recorded."""
        self._check_owner()
        run = self.steps[name]
        done = [a for a in run.attempts if a.status in ("selected", "rejected", "kept")]
        fn = score_mod.get(run.score_name)
        if getattr(fn, "selector", False):
            ranked, rest = [], list(done)
            while rest:
                nxt, _ = fn(rest)
                if nxt is None:
                    break
                ranked.append(nxt)
                rest.remove(nxt)
        else:
            ranked = sorted((a for a in done if a.score is not None), key=lambda a: a.score, reverse=True)
        chosen = ([run.selected] if run.selected in done else []) + [a for a in ranked if a is not run.selected]
        chosen = chosen[:max(1, int(k))]
        if baseline is not None and baseline in done and baseline not in chosen:
            chosen.append(baseline)  # the default parameters always compete to the end
        for a in chosen:
            if a.status == "rejected":
                a.status = "kept"
        run.kept = [a.id for a in chosen]
        self.save_graph()
        return chosen

    def decide(self, at: str, candidates: list["Attempt"], score: str, failed: list[dict] | None = None) -> "Attempt":
        """Bounded search: compare the branches' attempts at step ``at`` by ``score``, record which won (and the
        branches that failed before ``at``: ``failed``), and make the winner the run's head. A resume that
        reaches the same comparison reuses the recorded decision."""
        self._check_owner()
        fn = score_mod.get(score)
        if getattr(fn, "selector", False):  # a rule over attempts, not a value per attempt: rank by auto
            score, fn = "auto", score_mod.get("auto")
        # reused only for the same place, scoring policy and candidate results: each candidate's id, fingerprint and
        # finish time (an attempt rebuilt under the same id has another finish time); anything else decides again
        # (over the reused candidates when only the policy changed) and replaces the old decision
        now = sorted((a.id, a.fingerprint, a.finished_at) for a in candidates)
        rec = next((d for d in self.decisions if d["at"] == at and d.get("complete") and d.get("score") == score
                    and sorted((c["attempt"], c.get("fingerprint"), c.get("finished_at"))
                               for c in d["candidates"]) == now), None)
        if rec is not None:
            best = next(a for a in candidates if a.id == rec["winner"])
            # the failed branches are this run's evidence, not the recorded one's: a losing branch that failed again
            # on resume is a new attempt (another finish time), which the verdict must find handled
            changed = (rec.get("failed_branches") or None) != (failed or None)
            if failed:
                rec["failed_branches"] = failed
            else:
                rec.pop("failed_branches", None)
            if self.head is not best or changed:
                self.head = best
                self.save_graph()
            return best
        self.decisions = [d for d in self.decisions if d["at"] != at]
        scored = [(fn(a.metrics), a) for a in candidates]
        ok = [(s, a) for s, a in scored if s is not None]
        if not ok:
            raise StepFailed(f"decide at {at}: no branch has a {score} score")
        best = max(ok, key=lambda x: x[0])[1]
        self.decisions.append({"at": at, "score": score, "winner": best.id, "complete": True,
                               "candidates": [{"attempt": a.id, "score": s if isinstance(s, (int, float, type(None)))
                                               else list(s), "fingerprint": a.fingerprint,
                                               "finished_at": a.finished_at} for s, a in scored],
                               **({"failed_branches": failed} if failed else {})})
        self.head = best
        self.save_graph()
        log.info("decide at %s by %s: %s", at, score, best.id)
        return best

    def fork(self, attempt, workdir: str) -> "Flow":
        """A new run in ``workdir`` that continues from ``attempt`` (an id or an Attempt): any finished attempt of
        this run, a rejected one included. The new run's graph starts with that attempt and its ancestors, each
        selected there and pointing at this run's checkpoints (read, never written; ``forked_from`` names the
        original); this run's graph is not touched. The fork resumes like any run."""
        self._check_owner()
        a = attempt if isinstance(attempt, Attempt) else next((x for x in self.attempts if x.id == attempt), None)
        if a is None:
            raise ValueError(f"no attempt {attempt!r} in {self.workdir}")
        if a.status not in ("ok", "selected", "rejected", "kept") or not (a.checkpoint and os.path.isfile(a.checkpoint)):
            raise ValueError(f"fork: {a.id} is {a.status} with no checkpoint; fork from a finished attempt")
        workdir = os.path.abspath(workdir)
        if workdir == self.workdir or workdir.startswith(self.workdir + os.sep):
            raise ValueError("fork: the new run needs its own directory, outside this run's")
        if os.path.isfile(os.path.join(workdir, "graph.json")):
            raise ValueError(f"fork: {workdir} already holds a run")
        if self.runner is not None and hasattr(self.runner, "mount"):
            self.runner.mount(self.workdir)  # the new run's processes read this run's checkpoints
        f = Flow(self.design, self.pdk, workdir, self.session_factory, validate_inputs=False, config=self.config,
                 provenance=dict(self.provenance, forked_from={"run": self.workdir, "attempt": a.id}),
                 runner=self.runner, checkpoints=self.checkpoints)
        if self.runner is not None and hasattr(self.runner, "mount"):
            self.runner.mount(workdir)  # it exists now (Flow made it): the new attempts write there
        chain, x = [], a
        while x is not None:
            chain.append(x)
            x = x.parent
        copies: dict[str, Attempt] = {}
        for x in reversed(chain):
            c = Attempt(id=x.id, step=x.step, index=x.index, params=dict(x.params),
                        parent=copies.get(x.parent.id) if x.parent else None, dir=x.dir, kind=x.kind,
                        status="selected", checkpoint=x.checkpoint, metrics=dict(x.metrics), outputs=dict(x.outputs),
                        score=x.score, seconds=x.seconds, started_at=x.started_at, finished_at=x.finished_at,
                        process=x.process, timing=x.timing, resumed=True, fingerprint=x.fingerprint,
                        capabilities=x.capabilities, forked_from=f"{self.workdir}:{x.id}", threads=x.threads,
                        retry_of=x.retry_of, retry_reason=x.retry_reason, adapted_from=x.adapted_from,
                        adapt_reason=x.adapt_reason)
            copies[x.id] = c
            f.attempts.append(c)
            f.steps[x.step] = StepRun(x.step, [c], "fork", c, x.parent.id if x.parent else None, [dict(x.params)],
                                      fingerprint=None, reused=True)
        f.head = copies[a.id]  # the fork's result, until a step continues from it
        f.save_graph()
        log.info("forked %s from %s:%s (%d attempts in its chain)", workdir, self.workdir, a.id, len(chain))
        return f

    def _name_taken(self, name: str) -> bool:
        return name in self.steps or any(a.step == name and a.status != "stale" for a in self.attempts)

    def eco(self, name: str, parent: Attempt, ops: list[dict], fn: Callable | None = None,
            max_patch_nets: int = 25, score: str | None = None, max_slack_loss: float | None = 0.05) -> StepRun:
        """ECO from ``parent``. Rejected (checkpoint kept, StepFailed raised) when
        setup or hold slack drops by more than ``max_slack_loss`` ns against the
        parent (None disables the check)."""
        self._check_owner()
        score = score or "drc_then_slack"
        params = {"ops": ops, "max_patch_nets": max_patch_nets}
        efp = fp_mod.attempt(self.inputs["digest"], name,  # eco's declarations, and what accepts its result
                             {**self._resolve(None, "eco", params), "_score": score, "_max_slack_loss": max_slack_loss},
                             parent.fingerprint, None)
        run = self.steps.get(name)
        if run is not None and run.resumed and not run.reused:
            if _same(run.variants, [params]) and run.parent == parent.id and run.fingerprint in (None, efp):
                run.reused = True
                self._advance_head(run.selected, name, save=True)
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
        a = self._new_attempt(name, self._free_index(name, self._base_index(name)), params, parent, kind="eco")
        a.fingerprint, a.capabilities = efp, parent.capabilities  # an ECO keeps what its parent had
        self._run_attempt(a, fn)
        if a.status == "cancelled":
            self.save_graph()
            raise StepCancelled(f"ECO {name} cancelled")
        if a.status == "ok" and max_slack_loss is not None:
            # like with like: the nearest attempt of the chain measured with the ECO's parasitics (a signoff or
            # fill head records the placement estimate; the ECO, extracted parasitics: extract's are the baseline)
            base, x = parent, parent
            while x is not None and x.metrics.get("parasitics") != a.metrics.get("parasitics"):
                x = x.parent
            if x is not None:
                base = x
            a.outputs["eco_baseline"] = {"attempt": base.id, "parasitics": base.metrics.get("parasitics"),
                                         "same_parasitics": x is not None}
            lost = []
            for k, label in (("wns", "setup"), ("whs", "hold")):  # each checked on its own
                b, c = base.metrics.get(k), a.metrics.get(k)
                if isinstance(b, (int, float)) and isinstance(c, (int, float)) and c < b - max_slack_loss:
                    lost.append(f"{label} {c:.3f} ns vs {base.id} {b:.3f} ns")
            if lost:
                a.status = "rejected"
                a.error = (f"ECO regressed timing: {'; '.join(lost)} (allowed loss {max_slack_loss} ns, "
                           f"{a.metrics.get('parasitics')} parasitics on both sides)" if x is not None else
                           f"ECO regressed timing: {'; '.join(lost)} (allowed loss {max_slack_loss} ns; no attempt "
                           f"of the chain measured with {a.metrics.get('parasitics')} parasitics: compared with "
                           f"{base.id}'s {base.metrics.get('parasitics')})")
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
        self._check_owner()  # a newer Flow may have taken the workdir while this one's attempts were queued
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
            params = (self._resolve(None, "eco", a.params) if a.kind == "eco"
                      else self._resolve(fn, a.step, a.params))  # the same resolution admission and the plan used
            threads = self._threads(params, together)
            a.threads = threads  # what this attempt got: priced from here, not from the step's latest allocation
            started = getattr(getattr(session, "runner", None), "threads", getattr(self.config, "threads", None))
            self.checkpoints.prepare(session, threads, started)
            self.checkpoints.load(session, self.pdk, self.design, a.parent)
            t1 = mark("load", t1)
            # the step sees every declared parameter (variant, then design and PDK
            # step_defaults, then the declared default); the attempt records the variant
            ctx = Ctx(session, self.design, self.pdk, params, a.dir, a.parent, a, config=self.config,
                      threads=threads, processes=_Processes(self, a.id))
            fn(ctx)
            self._check_budget_stop(a)
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
        ctx = Ctx(None, self.design, self.pdk, self._resolve(fn, a.step, a.params),
                  a.dir, a.parent, a, runner=self.runner, config=self.config, processes=_Processes(self, a.id))
        fn(ctx)
        self._check_budget_stop(a)
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
        self._advance_head(best, name)
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
                **({"decisions": self.decisions} if self.decisions else {}),
                "head": self.head.id if self.head is not None else None,
                **({"stale": self.stale} if self.stale else {}),
                "attempts": [a.to_json() for a in _parents_first(self.attempts)],
                "steps": {k: {"score": v.score_name, "selected": v.selected.id if v.selected else None,
                              "parent": v.parent, "variants": v.variants, "fingerprint": v.fingerprint,
                              **({"rounds": v.rounds} if v.rounds else {}), **({"kept": v.kept} if v.kept else {})}
                          for k, v in self.steps.items()}}

    def verification(self) -> dict:
        """What the run established (sipath.verdict.verify): execution and verification status, each check."""
        from . import verdict
        return verdict.verify(self.graph())

    def save_graph(self):
        self._check_owner()
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


_OPEN: dict = {}  # realpath of a workdir -> weakref to the Flow of this process that writes it
_OPEN_LOCK = threading.Lock()


def _ancestors(a):
    while a is not None:
        yield a
        a = a.parent


def _parents_first(attempts: list) -> list:
    """``attempts`` in their order, except that each comes after its parent."""
    out, seen = [], set()

    def put(a):
        if id(a) in seen:
            return
        if a.parent is not None and a.parent in attempts:
            put(a.parent)
        seen.add(id(a))
        out.append(a)
    for a in attempts:
        put(a)
    return out


def _slowest(session, n: int = 8) -> list:
    """The ``n`` slowest Tcl commands of a session: [label, seconds]."""
    times = getattr(session, "timings", None) or []
    return [[lbl, round(sec, 3)] for lbl, sec in sorted(times, key=lambda x: -x[1])[:n]]
