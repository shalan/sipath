"""The Yosys adapter: synthesis, lint and equivalence scripts (``sipath.synth``)."""
from __future__ import annotations

from .run import ToolFailed, run_tool

VERSION = ("yosys -V", r"Yosys (\S+)")  # how the locked images are probed (sipath.tools.toolchain)


def script(ctx, path: str, log: str, out: str, timeout: float | None = None, error: type = ToolFailed) -> str:
    """Run a Yosys script quietly; Yosys's own log goes to ``log``, its output to ``out``."""
    return run_tool(ctx, ["yosys", "-q", "-l", log, "-s", path], out, timeout, error=error)


def command(ctx, args: list[str], out: str, timeout: float | None = None, error: type = ToolFailed) -> str:
    """Yosys with other arguments (e.g. ``-p "help read_slang"`` to probe a front end)."""
    return run_tool(ctx, ["yosys", *args], out, timeout, error=error)
