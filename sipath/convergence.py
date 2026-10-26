"""Read-only bounded-search diagnostics. Matching artifact bytes do not authorize reuse.

Only successful candidates in a completed decision are compared. Shared
ancestry is excluded. Missing files are unknown, and later stages may diverge.
"""
from __future__ import annotations

from .fingerprint import file_hash

SUCCESS = {"ok", "selected", "kept", "rejected"}


def analyze(graph: dict) -> list[dict]:
    by_id = {a["id"]: a for a in graph.get("attempts", [])}
    results = []
    for decision in graph.get("decisions", []):
        if not decision.get("complete", bool(decision.get("winner"))):
            continue
        chains = []
        for candidate in decision.get("candidates", []):
            chain, seen = [], set()
            a = by_id.get(candidate["attempt"])
            while a and a["id"] not in seen:
                seen.add(a["id"])
                chain.append(a)
                a = by_id.get(a.get("parent"))
            chains.append(list(reversed(chain)))
        if len(chains) < 2:
            continue
        # Compare distinct attempts; a checkpoint shared by all branches is not extra work.
        shared = set.intersection(*({a["id"] for a in c} for c in chains))
        stages = {}
        for chain in chains:
            for a in chain:
                if a["id"] not in shared and a.get("status") in SUCCESS:
                    stages.setdefault(a["step"].split("@", 1)[0], {})[a["id"]] = a
        groups, unavailable = [], []
        for stage, attempts in stages.items():
            if len(attempts) < 2:
                continue
            for kind in ("checkpoint", "def"):
                buckets = {}
                for a in attempts.values():
                    path = a.get("checkpoint") if kind == "checkpoint" else (a.get("outputs") or {}).get("def_")
                    if kind == "def" and not path:
                        continue  # this stage did not export a DEF
                    digest = file_hash(path) if path else None
                    if digest is None:
                        unavailable.append({"attempt": a["id"], "artifact": kind})
                    else:
                        buckets.setdefault(digest, []).append(a)
                for digest, same in buckets.items():
                    if len(same) < 2:
                        continue
                    seconds = [a.get("seconds") for a in same]
                    # Sum attempt durations, not elapsed wall time or a predicted saving.
                    extra = sum(seconds[1:]) if all(isinstance(x, (int, float)) for x in seconds) else None
                    groups.append({"stage": stage, "artifact": kind, "sha256_prefix": digest,
                                   "attempts": [a["id"] for a in same],
                                   "reference_attempt": same[0]["id"], "extra_attempt_seconds": extra})
        results.append({"decision_at": decision["at"], "winner": decision.get("winner"),
                        "groups": groups, "unavailable": unavailable})
    return results
