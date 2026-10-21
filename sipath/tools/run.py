"""Running an installation's tools next to OpenROAD (the run's runner: Docker
images or native binaries), with their output kept in a log."""
from __future__ import annotations

import os
import subprocess
import time


class ToolFailed(RuntimeError):
    """A tool exited non-zero, or this context has no runner to start it."""


def runner(ctx, error: type = ToolFailed):
    """The runner of a step's tools: the context's, else its OpenROAD session's."""
    r = getattr(ctx, "runner", None) or getattr(ctx.session, "runner", None)
    if r is None or not hasattr(r, "tool_command"):
        raise error("this step needs a real runner (its tools run next to OpenROAD)")
    return r


def run_tool(ctx, args: list[str], log: str, timeout: float | None = None, image: str | None = None,
             env: dict | None = None, error: type = ToolFailed) -> str:
    """Run a tool of the installation (``image``: from another image, e.g. Magic); its
    output goes to ``log``. A non-zero exit raises ``error``; the output is returned.
    ``timeout``: default config.tool_timeout (7200 s)."""
    if timeout is None:
        timeout = float(getattr(getattr(ctx, "config", None), "tool_timeout", None) or 7200)
    r = runner(ctx, error)
    cmd = r.tool_command(ctx.dir, args, image=image, env=env) if (image or env) else r.tool_command(ctx.dir, args)
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ctx.dir)
    out = p.stdout + p.stderr
    with open(log, "w") as f:
        f.write(f"# {' '.join(args)}\n# exit {p.returncode} after {time.time() - t0:.1f}s\n{out}")
    if p.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-15:])
        raise error(f"{os.path.basename(args[0])} exited {p.returncode} (log {log}):\n{tail}")
    return out
