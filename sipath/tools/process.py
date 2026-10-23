"""Every tool process SiPath starts, under one manager: timeout, cancel, process-tree kill, one log format.

``start(cmd, ...)`` runs ``cmd`` in its own process group (``start_new_session``), so killing the group takes
the whole tree (Yosys and its ABC children, a shell and what it started); a command that runs a container
names it (``container``), and a kill also stops the container with ``docker kill`` (killing the ``docker``
client alone leaves the container running). ``run(...)`` starts, waits, writes the log and returns the
result; a process registers with its attempt's ``tracker`` (``Ctx.processes``), so ``Flow.cancel`` and
MCP's ``cancel_job`` reach it, and it is killed if the attempt is already cancelled.

Every log starts the same way::

    # <command>
    # exit <code> after <seconds>s[, timed out after <limit>s | , cancelled][, peak memory <MB> MB]
    <the process's output>

The peak memory is the process tree's largest resident set (``wait4``); for a container it is the
``docker`` client's, so it is left out there.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass


class ProcessCancelled(RuntimeError):
    """The process was killed because its attempt (or the flow) was cancelled."""


class ProcessTimeout(RuntimeError):
    """The process ran longer than its limit; its tree (and container) were killed."""


def container_name(prefix: str = "sipath-tool") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Result:
    rc: int
    output: str
    seconds: float
    peak_mb: float | None = None
    timed_out: bool = False
    cancelled: bool = False


class Process:
    """A started child in its own process group (and optionally a named container)."""

    def __init__(self, cmd, cwd=None, env=None, container: str | None = None):
        self.cmd, self.container = list(cmd), container
        self.t0 = time.time()
        self.p = subprocess.Popen(self.cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, start_new_session=True)
        self.name = container or f"pid {self.p.pid}"
        self.killed = False
        self._chunks: list[bytes] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.rusage = None

    def _read(self):
        try:
            for chunk in iter(lambda: self.p.stdout.read(65536), b""):
                self._chunks.append(chunk)
        except (ValueError, OSError):  # the pipe was closed under it (close())
            pass

    def close(self, timeout: float = 10.0):
        """Collect the rest of the output (until the pipe's writers are gone, at most ``timeout``) and close it."""
        self._reader.join(timeout=timeout)
        try:
            self.p.stdout.close()
        except OSError:
            pass

    def alive(self) -> bool:
        """The process or anything left in its group (a background child outlives its parent)."""
        return self.p.returncode is None or self.group_alive()

    def group_alive(self) -> bool:
        try:
            os.killpg(self.p.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def kill(self, grace: float = 3.0, cancel: bool = True):
        """Stop the whole tree: its container (docker kill), then SIGTERM to the group, SIGKILL after ``grace``.
        ``cancel`` False: clearing what a finished tool left behind, not a cancel of the tool."""
        if cancel:
            self.killed = True
        if self.container:
            try:
                subprocess.run(["docker", "kill", self.container], capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(self.p.pid, sig)  # the whole group, also when its leader has already exited
            except (ProcessLookupError, PermissionError):
                return
            end = time.time() + wait
            while time.time() < end and self.group_alive():
                if self.p.returncode is None:
                    self.p.poll()  # reap the leader so the group can empty
                time.sleep(0.05)
            if not self.group_alive():
                return

    def wait(self, timeout: float | None = None) -> bool:
        """Reap the child (polling, so its resource usage is read); False when ``timeout`` passed first."""
        end = None if timeout is None else self.t0 + timeout
        while self.p.returncode is None:
            try:
                pid, status, ru = os.wait4(self.p.pid, os.WNOHANG)
            except ChildProcessError:  # reaped elsewhere
                self.p.wait()
                break
            if pid:
                self.p.returncode = os.waitstatus_to_exitcode(status)
                self.rusage = ru
                break
            if end is not None and time.time() >= end:
                return False
            time.sleep(0.05)
        if not self.group_alive():  # nothing left that holds the pipe: the output is complete
            self.close()
        return True  # a child the tool left behind still holds it: run() ends the group, then closes

    def output(self) -> str:
        return b"".join(self._chunks).decode(errors="replace")

    def peak_mb(self) -> float | None:
        if self.rusage is None or self.container:
            return None
        rss = self.rusage.ru_maxrss  # kilobytes on Linux, bytes on macOS
        return round(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024, 1)


def _label(label, cmd) -> str:
    """What the log names: the caller's label (a string or the tool's own argument list), else the command."""
    if isinstance(label, str):
        return label
    return " ".join(map(str, label or cmd))


def run(cmd, log: str | None = None, *, cwd=None, env=None, timeout: float | None = None, tracker=None,
        container: str | None = None, label=None) -> Result:
    """Run ``cmd`` to the end (or its timeout, or a cancel), log it, and return the result. ``env``: added to
    this process's environment. ``tracker``: the attempt's (add/discard/cancelled). Raises ProcessCancelled
    when cancelled; a timeout returns ``timed_out`` (the caller decides what it means) after the tree died."""
    if tracker is not None and getattr(tracker, "cancelled", False):
        raise ProcessCancelled(f"{_label(label, cmd)}: cancelled before it started")
    proc = Process(cmd, cwd=cwd, env={**os.environ, **env} if env else None, container=container)
    if tracker is not None:
        tracker.add(proc)
    try:
        finished = proc.wait(timeout)
        if not finished:
            proc.kill()
            proc.wait()
        elif proc.group_alive():  # the tool exited and left children behind: they end with it
            proc.kill(grace=1.0, cancel=False)
    except BaseException:  # interrupted (Ctrl-C, the interpreter exiting): the tool does not outlive its caller
        proc.kill(grace=1.0)
        raise
    finally:
        proc.close()
        if tracker is not None:
            tracker.discard(proc)
    res = Result(proc.p.returncode if proc.p.returncode is not None else -9, proc.output(),
                 round(time.time() - proc.t0, 1), proc.peak_mb(), timed_out=not finished,
                 cancelled=proc.killed and finished)
    if log:
        notes = [f"exit {res.rc} after {res.seconds}s"]
        if res.timed_out:
            notes.append(f"timed out after {timeout}s")
        if res.cancelled:
            notes.append("cancelled")
        if res.peak_mb is not None:
            notes.append(f"peak memory {res.peak_mb} MB")
        with open(log, "w") as f:
            f.write(f"# {_label(label, cmd)}\n# {', '.join(notes)}\n{res.output}")
    if res.cancelled:
        raise ProcessCancelled(f"{_label(label, cmd)}: cancelled")
    return res
