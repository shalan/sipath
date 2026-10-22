"""What a run established, apart from whether its processes finished.

``verify(graph)`` reads a run's attempt graph (and the signoff results its
attempts wrote) along the selected chain, from the first step to the last,
and returns:

- ``execution``: ``completed``, ``failed`` (the latest step's attempts failed
  or were interrupted), ``cancelled``, ``budget`` (a budget stopped it;
  ``--resume`` continues), or ``empty``; a step whose attempts all
  ran and were rejected (an ECO that lost slack) is a note, not a failure;
- ``verification``: ``passed`` (every check that ran passed and the standard
  ones all ran), ``failed`` (a required check failed, even with
  ``fail_on_violation: false``), ``inconclusive`` (nothing failed, something
  could not be decided, e.g. an equivalence timeout), ``omitted`` (nothing
  failed or undecided, but standard checks did not run: no signoff, a subset,
  equivalence off or skipped) or ``completed`` (no check ran at all);
- ``checks``: each check with its status (passed, failed, inconclusive,
  skipped), whether it was required, the attempt that ran it and a detail;
- ``omitted``: the standard checks that did not run; ``notes``: what limits
  the checks that did run (black-box and abstract macros, unconstrained
  endpoints, density judged at the parent level, a timeout accepted, a
  failure that did not fail its step).

A later attempt in the chain overrides an earlier one's result for the same
check (a quick signoff, then the full one).
"""
from __future__ import annotations

import json
import os

from .params import SIGNOFF_REQUIRED

TIMING = ("setup", "hold", "drv", "annotation", "unconstrained")


def _chain(g: dict) -> tuple[list[dict], str, list[str]]:
    """The selected chain to the latest step that selected an attempt (first step first), the execution status,
    and notes on steps whose attempts all ran but were refused (an ECO that lost slack: a rule, not a failure)."""
    by_id = {a["id"]: a for a in g.get("attempts") or []}
    steps = g.get("steps") or {}
    if not steps:
        return [], "empty", []
    execution, notes = "completed", []
    names = list(steps)
    last = names[-1]
    if not steps[last].get("selected"):
        mine = [a for a in by_id.values() if a.get("step") == last]
        states = {a.get("status") for a in mine}
        if "cancelled" in states:
            execution = "cancelled"
        elif "budget" in states and not states & {"failed", "interrupted"}:
            execution = "budget"  # stopped by a run budget: --resume continues
        elif states & {"failed", "interrupted", "running", "pending"} or not mine:
            execution = "failed"
        else:
            notes.append(f"{last}: every attempt ran and was rejected "
                         f"({'; '.join((a.get('error') or 'by its score')[:120] for a in mine)})")
    # a step stopped before it selected (cancelled, a run budget, every attempt failed by an exception) has no entry
    loose = {a.get("status") for a in by_id.values() if a.get("step") not in steps and a.get("status") != "stale"}
    if execution == "completed" and loose & {"cancelled", "budget", "failed", "interrupted", "running", "pending"}:
        execution = ("cancelled" if "cancelled" in loose else "budget" if "budget" in loose and
                     not loose & {"failed", "interrupted"} else "failed")
    from .scheduler import run_chain
    return run_chain(g), execution, notes  # the run's head (graph.json "head") and its ancestry


def _equivalence(a: dict, checks: dict, notes: list, omitted: list):
    eq = (a.get("outputs") or {}).get("equiv")
    if eq is None:
        if (a.get("params") or {}).get("equiv") == "off":
            omitted.append("equivalence (equiv: off)")
        return
    r = str(eq.get("result", ""))
    if r.startswith("equivalent"):
        checks["equivalence"] = {"status": "passed", "required": True, "attempt": a["id"],
                                 "detail": f"{r} (bounded, not a full proof)"}
    elif r == "mismatch":
        checks["equivalence"] = {"status": "failed", "required": True, "attempt": a["id"], "detail": r}
    elif r.startswith("inconclusive"):
        accepted = eq.get("on_timeout") == "pass"
        checks["equivalence"] = {"status": "passed" if accepted else "inconclusive", "required": True,
                                 "attempt": a["id"], "detail": f"{r} after {eq.get('depth')} cycles' worth of search"}
        if accepted:
            notes.append("equivalence: a timeout was accepted as a pass (equiv_on_timeout: pass)")
    elif r == "skipped":
        checks["equivalence"] = {"status": "skipped", "required": True, "attempt": a["id"],
                                 "detail": eq.get("reason", "")}
        omitted.append(f"equivalence ({eq.get('reason', 'skipped')})")


def _signoff(a: dict, checks: dict, notes: list):
    path = (a.get("outputs") or {}).get("signoff")
    if not (path and os.path.isfile(path)):
        return None
    with open(path) as f:
        s = json.load(f)
    required = set(s.get("required") or [])
    failed = {f.split(":")[0]: f for f in s.get("failures") or []}
    for c in s.get("checks") or []:
        v = s.get("timing" if c in TIMING else c)
        v = v if isinstance(v, dict) else {}
        if c in failed:
            st, detail = "failed", failed[c]
        elif v.get("skipped"):
            st, detail = "skipped", str(v["skipped"])
        else:
            st, detail = "passed", ""
        checks[c] = {"status": st, "required": c in required, "attempt": a["id"], "detail": detail[:300]}
    if (a.get("params") or {}).get("fail_on_violation") is False and not s.get("pass"):
        notes.append(f"{a['id']}: failed checks did not fail the step (fail_on_violation: false)")
    lvs = s.get("lvs") or {}
    if lvs.get("blackboxed"):
        notes.append(f"lvs: black boxes checked at their pins only: {sorted(set(lvs['blackboxed'].values()))}")
    drc = s.get("drc") or {}
    if drc.get("abstract_macros"):
        notes.append(f"drc: abstract macros, their insides not checked: {drc['abstract_macros']}")
    tim = s.get("timing") or {}
    for k, what in (("unconstrained", "unconstrained endpoints"), ("unconstrained_analog", "unconstrained analog "
                                                                                           "endpoints"),
                    ("unconstrained_pad_ports", "unconstrained pad ports")):
        if tim.get(k):
            notes.append(f"timing: {len(tim[k])} {what}, e.g. {tim[k][:3]}")
    dens = s.get("density") or {}
    if str(dens.get("scope", "")).startswith("embedded"):
        notes.append("density: informational for an embedded macro (judged in its parent)")
    for c in sorted(set(s.get("checks") or []) - required):
        if checks.get(c, {}).get("status") == "failed":
            notes.append(f"{c}: failed but not required")
    return s


def _precheck(a: dict, checks: dict):
    """cf-precheck's own results (sipath.precheck.results): each check pass, fail or skip."""
    res = (a.get("outputs") or {}).get("precheck")
    if not isinstance(res, dict):
        return
    for name, v in (res.get("checks") or {}).items():
        st = {"pass": "passed", "fail": "failed", "skip": "skipped"}.get((v or {}).get("status"), "failed")
        checks[f"precheck:{name}"] = {"status": st, "required": True, "attempt": a["id"],
                                      "detail": str((v or {}).get("reason") or "")[:300]}
    if res.get("error"):  # no results, or the tool's verdict disagreeing with its checks
        checks["precheck"] = {"status": "failed", "required": True, "attempt": a["id"], "detail": res["error"]}


def verify(g: dict) -> dict:
    chain, execution, notes = _chain(g)
    checks, omitted = {}, []
    signoffs = []
    rtl = False
    for a in chain:
        if a.get("step") == "synth" or "equiv" in (a.get("outputs") or {}):
            rtl = True
            _equivalence(a, checks, notes, omitted)
        s = _signoff(a, checks, notes)
        if s is not None:
            signoffs.append(s)
        _precheck(a, checks)
    ran = {c for s in signoffs for c in s.get("checks") or []}
    layout = any(a.get("step") not in ("synth",) and a.get("kind") != "tool" for a in chain)
    if layout:
        missing = [c for c in SIGNOFF_REQUIRED if c not in ran]
        if not signoffs:
            omitted.append("signoff (no signoff or check step in the chain)")
        elif missing:
            omitted += [f"{c} (not among the signoff checks)" for c in missing]
    if rtl and "equivalence" not in checks and not any(o.startswith("equivalence") for o in omitted):
        omitted.append("equivalence (synthesis recorded no result)")
    statuses = [(c["status"], c["required"]) for c in checks.values()]
    if not checks:
        verification = "completed"
    elif any(st == "failed" and req for st, req in statuses):
        verification = "failed"
    elif any(st == "inconclusive" for st, _ in statuses):
        verification = "inconclusive"
    elif omitted:
        verification = "omitted"
    else:
        verification = "passed"
    return {"execution": execution, "verification": verification, "checks": checks, "omitted": omitted,
            "notes": notes, "head": chain[-1]["id"] if chain else None}


def line(v: dict) -> str:
    """One line for a CLI or a log: the verdict and what it rests on."""
    failed = [c for c, x in v["checks"].items() if x["status"] == "failed" and x["required"]]
    undecided = [c for c, x in v["checks"].items() if x["status"] == "inconclusive"]
    parts = [f"execution {v['execution']}", f"verification {v['verification'].upper()}"]
    if failed:
        parts.append("failed: " + ", ".join(failed))
    if undecided:
        parts.append("inconclusive: " + ", ".join(undecided))
    if v["omitted"]:
        parts.append(f"omitted: {len(v['omitted'])} ({v['omitted'][0]}{', ...' if len(v['omitted']) > 1 else ''})")
    if v["notes"]:
        parts.append(f"{len(v['notes'])} notes")
    return "; ".join(parts)
