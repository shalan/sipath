"""MCP server (stdio) exposing the flow to an agent.

    python3 -m sipath mcp

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio).
Implements ``initialize``, ``ping``, ``tools/list``, ``tools/call``,
``resources/list``, ``resources/read`` and the ``notifications/cancelled``
notification. Tool calls run on worker threads, so a cancellation is read
while a step runs.

Long calls: ``run_step`` / ``run_eco`` are jobs. In the foreground they send
``notifications/progress`` (when the request carries ``_meta.progressToken``)
as attempts start and end; with ``background: true`` they return a job id at
once, to poll with ``job_status``. ``cancel_job`` (or ``notifications/cancelled``
for the request) kills the job's OpenROAD and tool processes; its attempts end
``cancelled`` and write no checkpoint.

Resources: every run's graph and each attempt's DEF, SPEF (per corner),
reports and pictures, as ``flow://<run>/<attempt>/<name>``.
Every tool drives the same scheduler as the Python API: each attempt is
its own OpenROAD process and writes its own checkpoint; a sweep's attempts
run concurrently up to ``parallel``. Nothing is simulated: if OpenROAD is
not available, tools that need it return an error.

Runs live in this server process (``open_run`` returns a run id); their
attempt graph is also written to ``<workdir>/graph.json``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import heatmap, scores, steps
from .tools.openroad import tcl
from .scheduler import EcoRefused, StepCancelled, StepFailed
from .tools.openroad.session import OpenRoadSession, SessionDied, TclError, find_openroad

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
STEPS = steps.STEPS
MAX_RESOURCE_BYTES = 64 * 1024 * 1024


def _examples() -> dict:
    """Example design files (sipath/examples/*.yaml): name -> first comment line."""
    ex = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")
    out = {}
    for f in sorted(os.listdir(ex)):
        if f.endswith(".yaml") and not f.startswith("."):  # not editors' or macOS's hidden copies
            with open(os.path.join(ex, f), encoding="utf-8") as fh:
                first = fh.readline().lstrip("# ").strip()
            out[f[:-5]] = first
    return out


EXAMPLES = _examples()


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "list_designs", "description": "List the example designs and the step names each uses.",
     "inputSchema": _obj({}), "annotations": {"readOnlyHint": True}},
    {"name": "list_scores", "description": "List the named scores used to pick the best attempt (higher is better).",
     "inputSchema": _obj({}), "annotations": {"readOnlyHint": True}},
    {"name": "describe_step", "description": "Parameters a step accepts in run_step variants: JSON Schema with "
     "type, default, allowed values or range, unit and description of each (omit step for every step).",
     "inputSchema": _obj({"step": {"type": "string", "enum": sorted(STEPS)}}), "annotations": {"readOnlyHint": True}},
    {"name": "open_run", "description": "Create a run for an example design (name) or a design file (path to "
     "YAML/JSON). Writes generated inputs, checks every input file exists, and returns a run id. No OpenROAD "
     "process is started yet. resume: pick up the attempt graph already in workdir (recorded steps are not run "
     "again). explore: optimize at the typical corner only, for a fast first cut at PPA (then run route_digital "
     "with detailed: false, or the ppa step after place/cts; its outputs.ppa has area, timing, power and a "
     "suggested clock period).",
     "inputSchema": _obj({"design": {"type": "string", "description": f"one of {sorted(EXAMPLES)} or a file path"},
                          "workdir": {"type": "string", "description": "absolute directory for checkpoints/logs"},
                          "resume": {"type": "boolean"}, "explore": {"type": "boolean"}},
                         ["design"])},
    {"name": "fork_run", "description": "Start a new run from any finished attempt of a run, a rejected one "
     "included: the new run's graph holds that attempt and its ancestors (selected there, reading the original "
     "run's checkpoints, never writing them) and continues with run_step(parent=<that attempt>). The original run "
     "is not changed. Returns the new run id.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "attempt": {"type": "string", "description": "e.g. place.1"},
                          "workdir": {"type": "string", "description": "absolute directory for the new run "
                                                                       "(default: runs/mcp/<new run id>)"}},
                         ["run_id", "attempt"])},
    {"name": "run_step", "description": "Run one step as one or more attempts. Each variant is a params dict "
     "(describe_step lists each step's parameters with types and defaults; values are checked before anything "
     "runs). Attempts run concurrently up to "
     "`parallel`, each in its own OpenROAD process starting from the `parent` checkpoint (omit only for "
     "a run's first step: 'synth' for RTL, 'floorplan' for a netlist). The best by `score` is selected; the others are rejected but kept.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "step": {"type": "string", "enum": sorted(STEPS)},
                          "parent": {"type": "string", "description": "selected attempt id, e.g. 'place.1'"},
                          "variants": {"type": "array", "items": {"type": "object"}},
                          "score": {"type": "string", "enum": sorted(scores.SCORES)},
                          "parallel": {"type": "integer", "minimum": 1, "maximum": 16},
                          "background": {"type": "boolean", "description": "return a job id at once; poll "
                                         "job_status"}},
                         ["run_id", "step"])},
    {"name": "adapt_step", "description": "Adaptive rounds on a step that already ran (place): each round turns "
     "what its selected attempt measured (the heatmaps' hotspots) into new attempts of the same step (partial "
     "placement blockages over the hotspots; a lower density while above the cells' utilization), runs them from "
     "the same parent and selects again over all (by the step's score; hotspots_then_wns ranks by hot bins). Stops "
     "after `rounds`, when nothing is hot, when nothing new is proposed, or when no proposal scores better; the "
     "rounds and the reason are recorded in the step (list_attempts shows adapted_from and adapt_reason). "
     "options: the adapter's (place: maps, max_density, density_step, min_density, max_blockages).",
     "inputSchema": _obj({"run_id": {"type": "string"}, "step": {"type": "string"},
                          "rounds": {"type": "integer", "minimum": 1}, "options": {"type": "object"},
                          "parallel": {"type": "integer", "minimum": 1, "maximum": 16},
                          "background": {"type": "boolean"}}, ["run_id", "step"])},
    {"name": "run_eco", "description": "ECO from the head of the selected chain. ops: "
     "{op:'resize', inst, master} or {op:'buffer', net, master, loads?, name?}. Refused for a sibling or stale "
     "parent; an equivalence failure writes no checkpoint; more than max_patch_nets touched nets triggers a full "
     "digital route.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "parent": {"type": "string"}, "name": {"type": "string"},
                          "ops": {"type": "array", "items": {"type": "object"}},
                          "max_patch_nets": {"type": "integer", "minimum": 0},
                          "background": {"type": "boolean"}},
                         ["run_id", "parent", "ops"])},
    {"name": "job_status", "description": "State of a run_step/run_eco job: running, done, failed or cancelled; "
     "the attempt start/end events so far; the result when done.",
     "inputSchema": _obj({"job_id": {"type": "string"}}, ["job_id"]), "annotations": {"readOnlyHint": True}},
    {"name": "cancel_job", "description": "Cancel a running job: its OpenROAD and tool processes (Yosys, KLayout, Magic, ...) are killed, its attempts "
     "end 'cancelled' with no checkpoint, attempts not started yet do not start.",
     "inputSchema": _obj({"job_id": {"type": "string"}}, ["job_id"])},
    {"name": "list_jobs", "description": "Jobs of a run (or of every run).",
     "inputSchema": _obj({"run_id": {"type": "string"}}), "annotations": {"readOnlyHint": True}},
    {"name": "list_attempts", "description": "Attempt graph of a run: status, params, score, metrics, error, "
     "process name and start/finish times (to see which attempts ran at the same time).",
     "inputSchema": _obj({"run_id": {"type": "string"}, "step": {"type": "string"}}, ["run_id"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "get_outputs", "description": "Files an attempt produced (checkpoint, DEF, SPEF, reports) with sizes.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "attempt": {"type": "string"}}, ["run_id", "attempt"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "query_heatmap", "description": "A heatmap an attempt collected after its checkpoint was saved "
     "(place, cts, route_digital, extract; maps: placement density, RUDY, routing congestion, pin density, power, "
     "IR drop): unit, peak, p95, hot threshold and share of hot bins, and the hotspot boxes with the instances or "
     "nets inside them. The bins themselves (microns, values) only when asked: box [x0, y0, x1, y1] returns those "
     "overlapping it, bins: true returns all (at most 2500; give a box for a region of a larger map).",
     "inputSchema": _obj({"run_id": {"type": "string"}, "attempt": {"type": "string"},
                          "map": {"type": "string", "enum": list(heatmap.MAPS)},
                          "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                          "bins": {"type": "boolean", "description": "return the bins (default: only with a box)"}},
                         ["run_id", "attempt", "map"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "draw_checkpoint", "description": "Schematic SVG of an attempt's checkpoint: IO pads, corners, "
     "IO fillers, hard macros, core (ring_only: IO ring only, with a ring-continuity note). Returns the file path.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "attempt": {"type": "string"},
                          "ring_only": {"type": "boolean"}}, ["run_id", "attempt"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "render_checkpoint", "description": "OpenROAD's own layout picture (save_image, PNG) of an attempt's "
     "checkpoint. area: [x0,y0,x1,y1] microns (default die); resolution: microns per pixel.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "attempt": {"type": "string"},
                          "area": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                          "resolution": {"type": "number"}}, ["run_id", "attempt"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "query_checkpoint", "description": "Open an attempt's checkpoint in a fresh OpenROAD process, run Tcl, "
     "and return its result and transcript. Read-only guard: write_*, exec, file, source, socket are removed and "
     "open is limited to the session's own result files before the query runs (a guard against accidental writes, "
     "not a security sandbox). Times out after 300 s.",
     "inputSchema": _obj({"run_id": {"type": "string"}, "attempt": {"type": "string"}, "tcl": {"type": "string"}},
                         ["run_id", "attempt", "tcl"]), "annotations": {"readOnlyHint": True}},
]


QUERY_BLOCKED = ["write_db", "write_def", "write_verilog", "write_spef", "write_sdc", "write_lef", "write_abstract_lef",
                 "write_cdl", "write_guides", "write_global_route_segments", "write_macro_placement", "save_image",
                 "exec", "socket", "file", "source", "cd", "load"]
# `open` stays usable only for the session's own result files (the session
# protocol returns results through them); any other path is refused.
QUERY_OPEN_GUARD = r"""
# no command-name abbreviation or auto-exec: OpenSTA's namespace unknown
# handler (sta_unknown) would expand a removed `exec` to `execute_dft_plan`,
# so an unknown name must simply fail
proc ::__flow_unknown {args} { error "invalid command name \"[lindex $args 0]\" (read-only query)" }
namespace eval :: { namespace unknown ::__flow_unknown }
proc ::unknown {args} { ::__flow_unknown {*}$args }
rename open ::__flow_open
proc ::open {path args} {
  set dir [pwd]
  if {[string first "$dir/.flow_" $path] != 0} { error "open is disabled in read-only queries: $path" }
  return [uplevel 1 [list ::__flow_open $path {*}$args]]
}
"""


def tools() -> list:
    """TOOLS with the step and score names as registered now (plugins included)."""
    import copy
    from . import registry
    registry.load_entry_points()
    out = copy.deepcopy(TOOLS)
    for t in out:
        props = t["inputSchema"].get("properties", {})
        if "step" in props and "enum" in props["step"]:
            props["step"]["enum"] = sorted(STEPS)
        if "score" in props and "enum" in props["score"]:
            props["score"]["enum"] = sorted(scores.SCORES)
    return out


class ToolError(Exception):
    pass


class Server:
    def __init__(self, emit=None):
        self.runs: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.runner = None
        self.emit = emit or (lambda msg: None)  # sends a notification to the client
        self._lock = threading.RLock()
        self._req = threading.local()  # request id / progress token of the call on this thread

    # ------------------------------------------------------------- helpers
    def _runner(self):
        if self.runner is None:
            self.runner = find_openroad()
        if self.runner is None:
            raise ToolError("OpenROAD is not available (not on PATH, no ORFS docker image)")
        return self.runner

    def _run(self, run_id):
        if run_id not in self.runs:
            raise ToolError(f"unknown run_id {run_id!r}; call open_run first")
        return self.runs[run_id]

    @staticmethod
    def _attempt(flow, attempt_id):
        for a in flow.attempts:
            if a.id == attempt_id:
                return a
        raise ToolError(f"unknown attempt {attempt_id!r}")

    @staticmethod
    def _summary(a):
        j = a.to_json()
        j.pop("outputs", None)
        if j.get("error"):
            j["error"] = j["error"][:1500]
        return j

    # --------------------------------------------------------------- tools
    def list_designs(self):
        return {"designs": EXAMPLES}

    def describe_step(self, step=None):
        from . import params
        names = [step] if step else [s for s in STEPS if s in params.DECLARED]
        for n in names:
            if n not in params.DECLARED:
                raise ToolError(f"unknown step {n!r}; known: {sorted(STEPS)}")
        return {n: params.step_schema(n) for n in names}

    def list_scores(self):
        return {"scores": {k: (f.__doc__ or "").strip() or k for k, f in scores.SCORES.items()}}

    def open_run(self, design, workdir=None, resume=False, explore=False):
        """``design``: an example name or a design file (YAML/JSON)."""
        runner = self._runner()
        from . import designfile
        path = design if os.path.isfile(design) else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                  "examples", f"{design}.yaml")
        if not os.path.isfile(path):
            raise ToolError(f"unknown design {design!r}: not a file and not one of {sorted(EXAMPLES)}")
        try:
            d, _ = designfile.load(path)
        except (ValueError, OSError) as e:
            raise ToolError(f"cannot load {path}: {e}") from e
        name = d.name
        run_id = f"{name}-{uuid.uuid4().hex[:6]}"
        workdir = os.path.abspath(workdir or os.path.join(os.getcwd(), "runs", "mcp", run_id))
        try:
            flow = designfile.build(d, workdir, runner, resume=bool(resume), explore=bool(explore))
        except ValueError as e:
            raise ToolError(str(e)) from e
        self.runs[run_id] = {"flow": flow, "design": name, "job": None}
        return {"run_id": run_id, "workdir": workdir, "openroad": f"{runner.kind} {runner.version()}",
                "pdk": flow.pdk.name, "steps": [s for s in STEPS if d.macros or s != "macros"],
                "recorded_steps": {k: v.selected.id for k, v in flow.steps.items() if v.selected}}

    def fork_run(self, run_id, attempt, workdir=None):
        r = self._run(run_id)
        flow = r["flow"]
        new_id = f"{r['design']}-{uuid.uuid4().hex[:6]}"
        workdir = os.path.abspath(workdir or os.path.join(os.getcwd(), "runs", "mcp", new_id))
        try:
            f = flow.fork(attempt, workdir)
        except ValueError as e:
            raise ToolError(str(e)) from e
        self.runs[new_id] = {"flow": f, "design": r["design"], "job": None}
        return {"run_id": new_id, "workdir": workdir, "forked_from": {"run_id": run_id, "attempt": attempt},
                "head": attempt, "recorded_steps": {k: v.selected.id for k, v in f.steps.items() if v.selected}}

    def run_step(self, run_id, step, parent=None, variants=None, score=None, parallel=1, background=False):
        flow = self._run(run_id)["flow"]
        if step not in STEPS:
            raise ToolError(f"unknown step {step!r}; known: {sorted(STEPS)}")
        if score is not None and score not in scores.SCORES:
            raise ToolError(f"unknown score {score!r}; known: {sorted(scores.SCORES)}")
        if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= 16:
            raise ToolError(f"parallel must be an integer 1..16, not {parallel!r}")
        try:
            steps.check_params(step, variants)
        except ValueError as e:
            raise ToolError(str(e)) from e
        p = self._attempt(flow, parent) if parent else None
        if p is None and step not in ("synth", "floorplan"):  # a run starts at synthesis or at the floorplan
            raise ToolError("parent is required for every step but a run's first ('synth' or 'floorplan')")
        if p is not None and p.status != "selected":
            raise ToolError(f"parent {p.id} is {p.status}; continue only from a selected attempt")
        from .scheduler import MissingPrerequisite
        try:  # before the job starts: nothing runs for a step its parent cannot feed
            flow.check_prerequisites(step, STEPS[step], p, variants)
        except MissingPrerequisite as e:
            raise ToolError(str(e)) from e

        def body():
            try:
                r = flow.step(step, STEPS[step], parent=p, variants=variants, score=score, parallel=parallel)
            except StepCancelled:
                raise
            except StepFailed as e:
                raise ToolError(str(e)[:4000]) from e
            except ValueError as e:
                raise ToolError(str(e)) from e
            v = flow.verification()
            return {"step": step, "selected": r.selected.id, "score": r.score_name,
                    "recorded": bool(getattr(r, "resumed", False)),
                    "attempts": [self._summary(a) for a in r.attempts],
                    # the run so far: what its checks established (not just that the step finished)
                    "verification": {k: v[k] for k in ("execution", "verification", "omitted", "notes")}
                    | {"checks": {c: x["status"] for c, x in v["checks"].items()}}}
        return self._job(run_id, f"run_step {step}", body, len(variants or [{}]), background)

    def adapt_step(self, run_id, step, rounds=1, options=None, parallel=1, background=False):
        from . import designfile, registry
        flow = self._run(run_id)["flow"]
        if step not in flow.steps:
            raise ToolError(f"step {step!r} has not run in {run_id}; run it first (run_step)")
        if registry.adapter(step) is None:
            raise ToolError(f"step {step!r} has no adapter; adaptive rounds exist for {sorted(registry.ADAPTERS)}")
        if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
            raise ToolError(f"rounds must be an integer >= 1, not {rounds!r}")
        if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= 16:
            raise ToolError(f"parallel must be an integer 1..16, not {parallel!r}")
        opts = dict(options or {})
        known = set(getattr(registry.adapter(step), "options", {}))
        if set(opts) - known:
            raise ToolError(f"unknown option(s) {sorted(set(opts) - known)}; known: {sorted(known)}")

        def body():
            done = len(flow.steps[step].rounds)
            try:
                r = designfile.adapt(flow, step, step, STEPS[step], {"rounds": done + rounds, **opts}, parallel,
                                     start=done)
            except StepCancelled:
                raise
            except (StepFailed, ValueError) as e:
                raise ToolError(str(e)[:4000]) from e
            return {"step": step, "selected": r.selected.id, "score": r.score_name, "rounds": r.rounds[done:],
                    "attempts": [self._summary(a) for a in r.attempts]}
        return self._job(run_id, f"adapt_step {step}", body, 2 * rounds, background)

    def run_eco(self, run_id, parent, ops, name=None, max_patch_nets=25, background=False):
        flow = self._run(run_id)["flow"]
        p = self._attempt(flow, parent)
        if not isinstance(ops, list) or not ops:
            raise ToolError("ops must be a non-empty list")
        need = {"resize": {"inst", "master"}, "buffer": {"net", "master"}}
        for k, op in enumerate(ops):
            if not isinstance(op, dict) or op.get("op") not in need:
                raise ToolError(f"op {k}: needs op 'resize' or 'buffer': {op!r}")
            missing = need[op["op"]] - set(op)
            if missing:
                raise ToolError(f"op {k} ({op['op']}): missing {sorted(missing)}")
        name = name or f"eco_{sum(a.kind == 'eco' for a in flow.attempts) + 1}"

        def body():
            try:
                r = flow.eco(name, p, ops, max_patch_nets=max_patch_nets)
            except EcoRefused as e:
                raise ToolError(f"refused: {e}") from e
            except StepCancelled:
                raise
            except StepFailed as e:
                raise ToolError(str(e)[:4000]) from e
            a = r.selected
            return {"attempt": self._summary(a), "outputs": a.outputs}
        return self._job(run_id, f"run_eco {name}", body, 1, background)

    # ----------------------------------------------------------------- jobs
    def _job(self, run_id, kind, body, total, background):
        run = self._run(run_id)
        flow = run["flow"]
        with self._lock:
            busy = run.get("job") and self.jobs[run["job"]]["status"] == "running"
            if busy:
                raise ToolError(f"run {run_id} is busy with job {run['job']}; wait, or cancel_job it")
            job = {"id": f"job-{uuid.uuid4().hex[:8]}", "run_id": run_id, "kind": kind, "status": "running",
                   "events": [], "result": None, "error": None, "started": time.time(), "finished": None,
                   "request_id": getattr(self._req, "id", None), "total": total}
            self.jobs[job["id"]] = job
            run["job"] = job["id"]
        token = getattr(self._req, "progress_token", None)

        def progress(event, a):
            job["events"].append({"t": round(time.time(), 3), "event": event, "attempt": a.id, "status": a.status,
                                  "process": a.process})
            if token is not None:
                done = sum(e["event"] == "end" for e in job["events"])
                self.emit({"jsonrpc": "2.0", "method": "notifications/progress",
                           "params": {"progressToken": token, "progress": done, "total": total,
                                      "message": f"{a.id} {event}: {a.status}"}})

        def run_job():
            flow.progress = progress
            try:
                job["result"] = body()
                job["status"] = "done"
            except StepCancelled as e:
                job["status"], job["error"] = "cancelled", str(e)
            except ToolError as e:
                job["status"], job["error"] = "failed", str(e)
            except Exception as e:  # reported in the job, never lost
                job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
            finally:
                flow.progress = None
                flow.reset_cancel()
                job["finished"] = time.time()

        th = threading.Thread(target=run_job, daemon=True, name=job["id"])
        job["thread"] = th
        th.start()
        if background:
            return {"job_id": job["id"], "status": "running", "kind": kind}
        th.join()
        if job["status"] == "done":
            return dict(job["result"], job_id=job["id"])
        raise ToolError(f"{job['status']}: {job['error']}")

    def _job_view(self, job):
        return {k: v for k, v in job.items() if k not in ("thread",)}

    def job_status(self, job_id):
        if job_id not in self.jobs:
            raise ToolError(f"unknown job {job_id!r}")
        return self._job_view(self.jobs[job_id])

    def list_jobs(self, run_id=None):
        return {"jobs": [{k: v for k, v in self._job_view(j).items() if k not in ("events", "result")}
                         for j in self.jobs.values() if run_id is None or j["run_id"] == run_id]}

    def cancel_job(self, job_id):
        if job_id not in self.jobs:
            raise ToolError(f"unknown job {job_id!r}")
        job = self.jobs[job_id]
        if job["status"] != "running":
            return {"job_id": job_id, "status": job["status"], "note": "not running"}
        flow = self.runs[job["run_id"]]["flow"]
        flow.cancel()
        job["thread"].join(timeout=300)
        return self._job_view(job)

    def cancel_request(self, request_id):
        """``notifications/cancelled`` for an in-flight request."""
        for j in list(self.jobs.values()):
            if j["request_id"] == request_id and j["status"] == "running":
                self.runs[j["run_id"]]["flow"].cancel()

    # ------------------------------------------------------------ resources
    def _resources(self) -> dict:
        out = {}
        for run_id, run in self.runs.items():
            flow = run["flow"]
            g = os.path.join(flow.workdir, "graph.json")
            if os.path.isfile(g):
                out[f"flow://{run_id}/graph.json"] = (g, "application/json", "attempt graph")
            for a in flow.attempts:
                files = {}
                for k in ("def_", "spef"):
                    if isinstance(a.outputs.get(k), str):
                        files[k.rstrip("_")] = a.outputs[k]
                for c, f in (a.outputs.get("spef_corners") or {}).items():
                    files[f"spef/{c}"] = f
                for k, f in (a.outputs.get("reports") or {}).items():
                    if isinstance(f, str):
                        files[f"report/{k}"] = f
                if a.debug_checkpoint:
                    files["debug_checkpoint"] = a.debug_checkpoint
                for k, f in files.items():
                    if os.path.isfile(f):
                        out[f"flow://{run_id}/{a.id}/{k}"] = (f, _mime(f), f"{a.id} {k}")
            pics = os.path.join(flow.workdir, "_pictures")
            for n in sorted(os.listdir(pics)) if os.path.isdir(pics) else []:
                out[f"flow://{run_id}/pictures/{n}"] = (os.path.join(pics, n), _mime(n), f"picture {n}")
        return out

    def resources_list(self):
        return {"resources": [{"uri": u, "name": desc, "mimeType": m, "size": os.path.getsize(p)}
                              for u, (p, m, desc) in sorted(self._resources().items())]}

    def resources_read(self, uri):
        res = self._resources()
        if uri not in res:
            raise ToolError(f"unknown resource {uri!r}")
        path, mime, _ = res[uri]
        size = os.path.getsize(path)
        if size > MAX_RESOURCE_BYTES:
            raise ToolError(f"{uri} is {size} bytes (limit {MAX_RESOURCE_BYTES}); read {path} directly")
        if mime.startswith("text/") or mime == "application/json":
            with open(path, errors="replace") as f:
                return {"contents": [{"uri": uri, "mimeType": mime, "text": f.read()}]}
        with open(path, "rb") as f:
            return {"contents": [{"uri": uri, "mimeType": mime, "blob": base64.b64encode(f.read()).decode()}]}

    def list_attempts(self, run_id, step=None):
        flow = self._run(run_id)["flow"]
        return {"attempts": [self._summary(a) for a in flow.attempts if step is None or a.step == step],
                "steps": {k: v.selected.id if v.selected else None for k, v in flow.steps.items()}}

    def get_outputs(self, run_id, attempt):
        a = self._attempt(self._run(run_id)["flow"], attempt)
        files = {}
        for k, v in {"checkpoint": a.checkpoint, **a.outputs}.items():
            if isinstance(v, str) and os.path.isfile(v):
                files[k] = {"path": v, "bytes": os.path.getsize(v)}
        for n in sorted(os.listdir(a.dir)) if os.path.isdir(a.dir) else []:
            if n.endswith((".rpt", ".log", ".txt")):
                files.setdefault(n, {"path": os.path.join(a.dir, n), "bytes": os.path.getsize(os.path.join(a.dir, n))})
        return {"attempt": a.id, "status": a.status, "files": files,
                "outputs": {k: v for k, v in a.outputs.items() if not isinstance(v, str)}}

    def query_heatmap(self, run_id, attempt, map, box=None, bins=None):
        a = self._attempt(self._run(run_id)["flow"], attempt)
        maps = (a.outputs or {}).get("heatmaps") or {}
        if map not in heatmap.MAPS:
            raise ToolError(f"unknown map {map!r}; maps: {list(heatmap.MAPS)}")
        if map not in maps:
            raise ToolError(f"attempt {a.id} has no {map} heatmap (heatmaps are collected by place, cts, "
                            f"route_digital and extract; this attempt has {sorted(maps) or 'none'})")
        e = maps[map]
        if "unavailable" in e:
            raise ToolError(f"attempt {a.id}: the {map} heatmap was unavailable: {e['unavailable']}")
        if box is not None:
            if (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ToolError(f"box must be [x0, y0, x1, y1] microns with x0 < x1 and y0 < y1, not {box!r}")
        if not os.path.isfile(e.get("csv", "")):
            raise ToolError(f"attempt {a.id}: {map} heatmap file missing: {e.get('csv')}")
        unit, all_bins = heatmap.read(e["csv"])
        found = heatmap.query(all_bins, box)
        out = {"attempt": a.id, "map": map, **{k: v for k, v in e.items()}, "box": box, "bins_in_box": len(found)}
        if bins if bins is not None else box is not None:  # the summary alone unless the bins are asked for
            out.update(bins_returned=min(len(found), 2500), bin_values=found[:2500])
        return out

    def _ckpt(self, run_id, attempt):
        flow = self._run(run_id)["flow"]
        a = self._attempt(flow, attempt)
        if not a.checkpoint:
            raise ToolError(f"attempt {attempt} has no checkpoint (status {a.status})")
        return flow, a

    def draw_checkpoint(self, run_id, attempt, ring_only=False):
        from .viz import draw
        flow, a = self._ckpt(run_id, attempt)
        out = os.path.join(flow.workdir, "_pictures", f"{a.id}{'_ring' if ring_only else ''}.svg")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        return draw(a.checkpoint, out, self._runner(), ring_only=ring_only)

    def render_checkpoint(self, run_id, attempt, area=None, resolution=None):
        from .viz import render
        flow, a = self._ckpt(run_id, attempt)
        out = os.path.join(flow.workdir, "_pictures", f"{a.id}.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            return render(a.checkpoint, out, self._runner(), tuple(area) if area else None, resolution)
        except RuntimeError as e:
            raise ToolError(str(e)) from e

    def query_checkpoint(self, run_id, attempt, tcl_text):
        run = self._run(run_id)
        flow = run["flow"]
        a = self._attempt(flow, attempt)
        if not a.checkpoint:
            raise ToolError(f"attempt {attempt} has no checkpoint (status {a.status})")
        qdir = os.path.join(flow.workdir, "_queries", uuid.uuid4().hex[:8])
        with OpenRoadSession(qdir, self._runner(), timeout=300) as s:
            s.run(tcl.HELPERS)
            s.run(tcl.load_checkpoint(flow.pdk, flow.design, a.checkpoint))
            # read-only: after loading, remove every command that writes files or
            # reaches the host, so the query cannot alter checkpoints or run programs
            s.run("foreach c {" + " ".join(QUERY_BLOCKED) + "} { if {[llength [info commands $c]]} { rename $c {} } }")
            s.run(QUERY_OPEN_GUARD)
            try:
                r = s.run(tcl_text)
            except TclError as e:
                raise ToolError(f"Tcl error: {e.result}\n{e.transcript[-2000:]}") from e
            except SessionDied as e:
                raise ToolError(f"OpenROAD process ended: {e}") from e
        return {"result": r.value[:20000], "transcript_tail": r.output[-4000:]}

    # ------------------------------------------------------------ dispatch
    def call(self, name, args, request_id=None, progress_token=None):
        self._req.id, self._req.progress_token = request_id, progress_token
        args = dict(args or {})
        if name == "query_checkpoint":
            args["tcl_text"] = args.pop("tcl", "")
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            raise ToolError(f"unknown tool {name!r}")
        try:
            return fn(**args)
        except TypeError as e:
            raise ToolError(f"bad arguments for {name}: {e}") from e

    def handle(self, msg: dict):
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            if method == "notifications/cancelled":
                self.cancel_request((msg.get("params") or {}).get("requestId"))
            return None
        try:
            if method == "initialize":
                want = (msg.get("params") or {}).get("protocolVersion")
                return self._ok(mid, {
                    "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                    "serverInfo": {"name": "sipath", "version": "0.1"},
                    "instructions": "Physical design on a live OpenROAD. open_run -> run_step (floorplan, then "
                                    "each step with parent=<selected attempt>) -> list_attempts / get_outputs; "
                                    "run_eco from the selected head."})
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                return self._ok(mid, {"tools": tools()})
            if method in ("resources/list", "resources/read"):
                p = msg.get("params") or {}
                try:
                    out = self.resources_list() if method == "resources/list" else self.resources_read(p.get("uri"))
                except ToolError as e:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": str(e)}}
                return self._ok(mid, out)
            if method == "tools/call":
                p = msg.get("params") or {}
                try:
                    out = self.call(p.get("name"), p.get("arguments"), request_id=mid,
                                    progress_token=(p.get("_meta") or {}).get("progressToken"))
                    text = json.dumps(out, indent=1, default=str)
                    return self._ok(mid, {"content": [{"type": "text", "text": text}], "structuredContent": out,
                                          "isError": False})
                except ToolError as e:
                    return self._ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # internal error: report, keep serving
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}",
                                                          "data": traceback.format_exc()[-2000:]}}

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".def": "text/plain", ".spef": "text/plain", ".rpt": "text/plain", ".txt": "text/plain",
            ".log": "text/plain", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
            ".odb": "application/octet-stream"}.get(ext, "application/octet-stream")


def serve(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    out = stdout or sys.stdout
    sys.stdout = sys.stderr  # stray prints must never corrupt the protocol stream
    wlock = threading.Lock()

    def send(msg):
        with wlock:
            out.write(json.dumps(msg, default=str) + "\n")
            out.flush()

    server = Server(emit=send)
    pool = ThreadPoolExecutor(max_workers=8)  # tool calls run here; this loop keeps reading

    def work(msg):
        reply = server.handle(msg)
        if reply is not None:
            send(reply)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}})
            continue
        if msg.get("method") == "tools/call" and msg.get("id") is not None:
            pool.submit(work, msg)
        else:
            work(msg)
    pool.shutdown(wait=True)


if __name__ == "__main__":
    serve()
