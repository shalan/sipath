"""Attempt graph scheduler.

Public API
----------
``Flow(design, pdk, workdir, session_factory)``
``Flow.step(name, fn, parent=None, variants=None, score=None, parallel=1)``
    Runs one attempt per variant (params dict), at most ``parallel`` at a
    time. Every attempt gets its own session (one OpenROAD process), loads
    the parent checkpoint (or the netlist for a root step), runs ``fn(ctx)``,
    and writes its own ODB. The best successful attempt by the named score
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

from . import params as params_mod
from . import scores as score_mod
from . import tcl
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


class StepCancelled(StepFailed):
    pass


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
            "debug_checkpoint": self.debug_checkpoint, "resumed": self.resumed,
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


class Flow:
    def __init__(self, design, pdk, workdir: str, session_factory: Callable, validate_inputs: bool = True,
                 runner_prefixes: tuple[str, ...] = (), config: Config | None = None, resume: bool = False,
                 provenance: dict | None = None, runner=None):
        self.runner = runner  # for tool steps (e.g. Yosys); None: such steps cannot run
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
        self.progress: Callable | None = None  # called with (event, attempt) as attempts start and end
        if validate_inputs:
            design.validate(pdk, runner_prefixes=runner_prefixes)
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
                        resumed=True)
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
                                       resumed=True)
        log.info("resumed %s: %d attempts, %d completed steps", path, len(self.attempts), len(self.steps))

    def _recorded(self, name, parent, variants, score) -> StepRun | None:
        """The recorded result of an identical step, if this flow was resumed."""
        run = self.steps.get(name)
        if run is None or not run.resumed:
            return None
        same = (_same(run.variants, variants) and run.score_name == score
                and run.parent == (parent.id if parent else None))
        if not same:
            raise ValueError(f"step {name} already ran with other parent/variants/score; use another name "
                             "or a fresh workdir")
        return run

    def _forget_unfinished(self, name):
        """Drop attempts of ``name`` that did not finish with a checkpoint and
        may be tried again: interrupted or failed tries recorded by an earlier
        run, and cancelled tries. Finished ones are reused."""
        with self._lock:
            self.attempts = [a for a in self.attempts
                             if not (a.step == name and ((a.resumed and a.status != "ok") or a.status == "cancelled"))]

    # ---------------------------------------------------------------- cancel
    def cancel(self):
        """Stop running attempts (their OpenROAD processes are killed) and keep
        new ones from starting, until ``reset_cancel``."""
        self._cancel.set()
        with self._lock:
            live = list(self._live.items())
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
        rec = self._recorded(name, parent, variants, score)
        if rec is not None:
            log.info("step %s: recorded result reused (selected %s)", name, rec.selected.id)
            return rec
        if name in self.steps:
            raise ValueError(f"step {name} already ran")
        self._forget_unfinished(name)
        if self.cancelled:
            raise StepCancelled(f"step {name}: flow is cancelled")
        kept = {a.index: a for a in self.attempts if a.step == name and a.status == "ok"}
        if any(a.step == name for a in self.attempts if a.index not in kept or a.status != "ok"):
            raise ValueError(f"step {name} already ran")
        attempts = []
        for k, v in enumerate(variants):
            old = kept.get(k)
            if old is not None and _same(old.params, v) and old.parent is parent:
                attempts.append(old)  # finished before the interruption: not run again
            elif old is not None:
                raise ValueError(f"step {name}: recorded attempt {old.id} has other params or parent")
            else:
                attempts.append(self._new_attempt(name, k, v, parent, kind="tool" if getattr(fn, "tool", False)
                                                  else "step"))
        todo = [a for a in attempts if a.status != "ok"]
        log.info("step %s: %d attempts (%d to run, %d recorded), parallel %d", name, len(attempts), len(todo),
                 len(attempts) - len(todo), self.config.cap(parallel))
        with ThreadPoolExecutor(max_workers=self.config.cap(parallel)) as ex:
            list(ex.map(lambda a: self._run_attempt(a, fn), todo))
        if any(a.status == "cancelled" for a in attempts):
            self.save_graph()
            raise StepCancelled(f"step {name} cancelled: " + ", ".join(f"{a.id} {a.status}" for a in attempts))
        return self._select(name, attempts, score, score_fn, parent=parent, variants=variants)

    def _name_taken(self, name: str) -> bool:
        return name in self.steps or any(a.step == name for a in self.attempts)

    def eco(self, name: str, parent: Attempt, ops: list[dict], fn: Callable | None = None,
            max_patch_nets: int = 25, score: str | None = None, max_slack_loss: float | None = 0.05) -> StepRun:
        """ECO from ``parent``. Rejected (checkpoint kept, StepFailed raised) when
        setup or hold slack drops by more than ``max_slack_loss`` ns against the
        parent (None disables the check)."""
        score = score or "drc_then_slack"
        params = {"ops": ops, "max_patch_nets": max_patch_nets}
        run = self.steps.get(name)
        if run is not None and run.resumed and _same(run.variants, [params]) and run.parent == parent.id:
            return run  # the same ECO, recorded by an earlier run
        self._forget_unfinished(name)
        if self._name_taken(name):
            raise EcoRefused(f"ECO {name}: a step or attempt with that name exists; its checkpoints would be lost")
        if parent.status != "selected":
            raise EcoRefused(f"ECO {name}: parent {parent.id} is {parent.status}, not the selected checkpoint")
        child = [a for a in self.attempts if a.parent is parent and a.status == "selected"]
        if child:
            raise EcoRefused(f"ECO {name}: parent {parent.id} already continued as {child[0].id}; "
                             "start from the head of the selected chain")
        if fn is None:
            from .eco import run_eco
            fn = run_eco
        a = self._new_attempt(name, 0, params, parent, kind="eco")
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
        return self._select(name, [a], score, score_mod.get(score), parent=parent, variants=[params])

    # --------------------------------------------------------------- internals
    def _new_attempt(self, step, k, params, parent, kind="step") -> Attempt:
        # a directory is never reused: a retry (after a cancel or an interruption)
        # gets a fresh one and the old one stays for inspection. (Reusing a just
        # deleted path also confuses Docker Desktop's file sharing.)
        d = os.path.join(self.workdir, step, str(k))
        n = 0
        while os.path.exists(d):
            n += 1
            d = os.path.join(self.workdir, step, f"{k}_r{n}")
        a = Attempt(id=f"{step}.{k}", step=step, index=k, params=dict(params), parent=parent, dir=d, kind=kind)
        with self._lock:
            self.attempts.append(a)
        return a

    def _run_attempt(self, a: Attempt, fn: Callable):
        if self.cancelled:
            a.status, a.error = "cancelled", "cancelled before it started"
            self.save_graph()
            return
        a.status = "running"
        a.resumed = False
        t0 = time.time()
        phases = {}
        odb = os.path.join(a.dir, f"{a.step}.{a.index}.odb")
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
            session.run(tcl.HELPERS)
            if a.parent is None:
                session.run(tcl.load_root(self.pdk, self.design))
            elif a.parent.kind == "tool":  # e.g. after synthesis: its netlist is the design
                session.run(tcl.load_root(self.pdk, self.design, netlist=a.parent.checkpoint))
            else:
                session.run(tcl.load_checkpoint(self.pdk, self.design, a.parent.checkpoint))
            t1 = mark("load", t1)
            # the step sees every declared parameter (variant, then design and PDK
            # step_defaults, then the declared default); the attempt records the variant
            decl = "eco" if a.kind == "eco" else getattr(fn, "__name__", a.step)
            ctx = Ctx(session, self.design, self.pdk, params_mod.resolve(decl, a.params, self.pdk, self.design),
                      a.dir, a.parent, a, config=self.config)
            fn(ctx)
            t1 = mark("step", t1)
            parasitics = session.value("::flow_timing_prep")
            metrics = parse_metrics(session.value("::flow_metrics"))
            metrics["parasitics"] = parasitics
            metrics.update(ctx.metrics)
            t1 = mark("metrics", t1)
            session.run(f"write_db {tcl.q(odb)}")
            if not os.path.isfile(odb) or os.path.getsize(odb) == 0:
                raise RuntimeError(f"write_db produced no checkpoint at {odb}")
            mark("write_db", t1)
            a.metrics, a.outputs, a.checkpoint = metrics, ctx.outputs, odb
            a.status = "ok"
            log.info("attempt %s: ok in %.1fs", a.id, time.time() - t0)
        except Exception as e:  # the attempt fails; nothing is invented
            a.status = "cancelled" if self.cancelled else "failed"
            a.error = f"{type(e).__name__}: {e}"
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
                  a.dir, a.parent, a, runner=self.runner, config=self.config)
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
            dbg = os.path.join(a.dir, "debug", f"{a.step}.{a.index}.failed.odb")
            try:
                os.makedirs(os.path.dirname(dbg), exist_ok=True)
                session.run(f"write_db {tcl.q(dbg)}", timeout=600)
                if os.path.isfile(dbg) and os.path.getsize(dbg) > 0:
                    a.debug_checkpoint = dbg
            except Exception as e:  # the session may be what failed
                a.outputs["debug_checkpoint_error"] = f"{type(e).__name__}: {e}"

    def _select(self, name, attempts, score_name, score_fn, parent=None, variants=None) -> StepRun:
        ok = [a for a in attempts if a.status == "ok"]
        for a in ok:
            a.score = score_fn(a.metrics)
        ok = [a for a in ok if a.score is not None]
        best = max(ok, key=lambda a: a.score) if ok else None
        for a in attempts:
            if a.status == "ok":
                a.status = "selected" if a is best else "rejected"
        run = StepRun(name, attempts, score_name, best, parent.id if parent else None, variants)
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
                "provenance": self.provenance, "config": self.config.to_dict(),
                "attempts": [a.to_json() for a in self.attempts],
                "steps": {k: {"score": v.score_name, "selected": v.selected.id if v.selected else None,
                              "parent": v.parent, "variants": v.variants}
                          for k, v in self.steps.items()}}

    def save_graph(self):
        with self._lock:  # attempts finish concurrently
            data = self.graph()
            tmp = os.path.join(self.workdir, "graph.json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, default=str)
            os.replace(tmp, os.path.join(self.workdir, "graph.json"))


def _slowest(session, n: int = 8) -> list:
    """The ``n`` slowest Tcl commands of a session: [label, seconds]."""
    times = getattr(session, "timings", None) or []
    return [[lbl, round(sec, 3)] for lbl, sec in sorted(times, key=lambda x: -x[1])[:n]]


def parse_metrics(s: str) -> dict:
    toks = s.split()
    out = {}
    for k, v in zip(toks[0::2], toks[1::2]):
        try:
            f = float(v)
        except ValueError:
            out[k] = v
            continue
        if k in ("wns", "tns", "whs") or k.startswith(("wns_", "whs_")):
            f = None if (f != f or abs(f) > 1e30) else f  # sta reports user units (ns)
        out[k] = f
    return out
