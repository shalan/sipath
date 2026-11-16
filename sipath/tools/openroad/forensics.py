"""Local pre-repair evidence, saved while OpenROAD is still alive.

The snapshot is for inspection, never a scheduler parent. Replay repeats this
attempt's commands from its original inputs: ODB/guides do not serialize the
global router's in-memory state. No design data is uploaded or copied elsewhere.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from .tcl import q


def replay_script(history: list[dict], attempt_dir: str, threads: int) -> str:
    """Replay in a new output directory, retaining each command's error policy."""
    escaped_dir = "".join("\\" + c if c in '\\{}[]$"; \t\n' else c for c in attempt_dir)
    out = [
        "# Run with the recorded OpenROAD build and the same input mounts.",
        "# Reconstructs router state from the parent; not an in-memory crash dump.",
        "set ::sipath_replay_dir [file join [file dirname [file normalize [info script]]] replay-[pid]-[clock clicks]]",
        "file mkdir $::sipath_replay_dir",
        "cd $::sipath_replay_dir",
        r'set ::sipath_escaped_dir [regsub -all {[][\\{}$"; \t\n]} $::sipath_replay_dir {\\&}]',
        f"set_thread_count {threads}",
    ]
    for i, command in enumerate(history, 1):
        encoded = base64.b64encode(command["script"].encode()).decode()
        out += [f"# Command {i}",
                f"set ::sipath_script [encoding convertfrom utf-8 [binary decode base64 {q(encoded)}]]",
                f"set ::sipath_script [string map [list {q(escaped_dir)} $::sipath_escaped_dir "
                f"{q(attempt_dir)} $::sipath_replay_dir] $::sipath_script]"]
        if command["check"]:
            out.append("uplevel #0 $::sipath_script")
        else:
            out.append("catch {uplevel #0 $::sipath_script} ::sipath_ignored")
    return "\n".join(out) + "\n"


def pre_repair_bundle(ctx, repair_script: str):
    """Publish a complete bundle before entering repair; an export failure stops repair."""
    root = Path(ctx.path("debug/pre-grt-repair"))
    root.mkdir(parents=True, exist_ok=False)
    manifest_path = root / "manifest.json"
    ctx.outputs["pre_repair_bundle"] = str(manifest_path)
    runner = getattr(ctx.session, "runner", None)
    history = getattr(ctx.session, "command_history", None)
    manifest = {
        "schema_version": 1, "status": "writing", "stage": "before-global-route-repair",
        "diagnostic_only": True, "attempt": ctx.attempt.id,
        "parent": ctx.parent.id if ctx.parent else None,
        "parent_checkpoint": ctx.parent.checkpoint if ctx.parent else None,
        "threads": ctx.threads, "params": ctx.params,
        "runtime": runner.provenance() if runner else {},
        "run_graph": str(Path(ctx.dir).parents[1] / "graph.json"),
        "replay_mode": "reconstruct-from-attempt-inputs",
        "files": {},
    }

    def publish():
        tmp = manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp.replace(manifest_path)

    publish()
    try:
        if not history:
            raise RuntimeError("session does not provide command history for replay")
        # Freeze before exports: replay must not write into the evidence directory.
        commands = [dict(c) for c in history] + [{"script": repair_script, "check": True}]
        (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        (root / "repair.tcl").write_text(repair_script + "\n")
        (root / "replay.tcl").write_text(replay_script(commands, str(Path(ctx.dir).resolve()), ctx.threads or 1))
        for name, command in (("before.odb", "write_db"), ("before.sdc", "write_sdc"),
                              ("before.guide", "write_guides")):
            path = root / name
            ctx.session.run(f"{command} {q(str(path))}")
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError(f"{command} produced no nonempty artifact at {path}")
        (root / "README.txt").write_text(
            "Local diagnostic bundle; never a selectable/resumable flow checkpoint.\n"
            "Run: openroad -no_init -exit /absolute/path/to/replay.tcl\n"
            "Use the recorded runtime, threads and original input mounts/paths.\n"
            "Replay reloads the parent and repeats this attempt through repair to rebuild router state.\n"
            "The ODB, SDC and guides are inspection snapshots, not a serialized router process.\n"
            "Replay writes attempt outputs under a fresh replay-* directory beside this file.\n"
            "Original inputs and parent checkpoint must remain available and unchanged; see graph.json.\n"
            "This is not a portable or minimized reproducer; plugins may have external side effects.\n"
            "Contains design data and local paths. Review before sharing; nothing is uploaded automatically.\n")
        for path in sorted(root.iterdir()):
            if path.name in {"manifest.json", "manifest.tmp"}:
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest["files"][path.name] = {
                "bytes": path.stat().st_size, "sha256": digest.hexdigest(),
            }
        manifest["status"] = "complete"
    except Exception as e:
        manifest["status"] = "incomplete"
        manifest["error"] = f"{type(e).__name__}: {e}"
        publish()
        raise
    publish()
