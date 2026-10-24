"""The OpenROAD checkpoint store: what the scheduler asks of a step's process.

The scheduler treats a checkpoint as an opaque artifact (a path); this store
knows it is an OpenROAD database: it prepares the process (threads, the flow's
Tcl helpers), loads an attempt's parent (the design's inputs, a tool step's
netlist, or a parent database), reads the metrics every attempt records, and
writes the database. Registered as the default store (``registry``).
"""
from __future__ import annotations

import os

from . import tcl


def parse_metrics(s: str) -> dict:
    """``::flow_metrics`` (key value pairs) as numbers; non-finite slacks become None."""
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


# the process's peak resident memory in kB (VmHWM), or nothing where /proc is missing (not Linux)
PEAK_MEMORY_TCL = (
    "if {[catch {set __f [open /proc/[pid]/status]; set __s [read $__f]; close $__f} ]} { list } "
    "elseif {[regexp {VmHWM:\\s+(\\d+)} $__s -> __kb]} { set __kb } else { list }")


class OpenRoadCheckpoints:
    """Checkpoints as OpenROAD databases (``<step>.<index>.odb``)."""

    suffix = ".odb"

    def path(self, attempt_dir: str, step: str, index: int) -> str:
        return os.path.join(attempt_dir, f"{step}.{index}{self.suffix}")

    def prepare(self, session, threads: int, started: int | None):
        """The process's thread count (when it started with another) and the flow's helpers."""
        if threads != started:  # the process started with the runner's -threads
            session.run(f"set_thread_count {threads}")
        session.run(tcl.HELPERS)

    def load(self, session, pdk, design, parent):
        """The design's inputs (no parent), a tool step's output as the netlist, or the parent's database."""
        if parent is None:
            session.run(tcl.load_root(pdk, design))
        elif parent.kind == "tool":  # e.g. after synthesis: its netlist is the design
            session.run(tcl.load_root(pdk, design, netlist=parent.checkpoint))
        else:
            session.run(tcl.load_checkpoint(pdk, design, parent.checkpoint))

    def metrics(self, session) -> dict:
        """Every attempt's metrics, as the flow's helpers report them, with the parasitics they used, the threads
        the OpenROAD process ran with (``openroad_threads``: its own count, however it got it: started with it
        or set_thread_count) and the process's peak memory (``mem_peak_mb``: VmHWM of /proc/<pid>/status;
        absent where there is no /proc)."""
        parasitics = session.value("::flow_timing_prep")
        metrics = parse_metrics(session.value("::flow_metrics"))
        metrics["parasitics"] = parasitics
        n = session.value("ord::thread_count").strip()
        if n.isdigit():
            metrics["openroad_threads"] = int(n)
        mem = session.value(PEAK_MEMORY_TCL)
        if mem.strip().isdigit():
            metrics["mem_peak_mb"] = round(int(mem) / 1024, 1)
        return metrics

    def save(self, session, path: str, timeout: float | None = None):
        if timeout is None:
            session.run(f"write_db {tcl.q(path)}")
        else:
            session.run(f"write_db {tcl.q(path)}", timeout=timeout)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise RuntimeError(f"write_db produced no checkpoint at {path}")
