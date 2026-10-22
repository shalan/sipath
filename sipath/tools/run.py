"""Running an installation's tools next to OpenROAD (the run's runner: Docker
images or native binaries), with their output kept in a log."""
from __future__ import annotations

import os


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
    ``timeout``: default config.tool_timeout (7200 s). The process (and its container) is
    the attempt's: a cancel or the timeout kills its whole tree (sipath.tools.process)."""
    from . import process
    if timeout is None:
        timeout = float(getattr(getattr(ctx, "config", None), "tool_timeout", None) or 7200)
    r = runner(ctx, error)
    name = process.container_name() if getattr(r, "kind", "") == "docker" else None
    cmd = command(r, ctx.dir, args, image=image, env=env, name=name)
    native = name is not None and cmd[:1] != ["docker"]  # a native_tools tool: no container to stop
    res = process.run(cmd, log, cwd=ctx.dir, timeout=timeout, tracker=getattr(ctx, "processes", None),
                      container=None if native else name, label=args)
    if res.timed_out:
        raise error(f"{os.path.basename(args[0])} timed out after {timeout:.0f} s and was stopped (log {log})")
    if res.rc != 0:
        tail = "\n".join(res.output.strip().splitlines()[-15:])
        raise error(f"{os.path.basename(args[0])} exited {res.rc} (log {log}):\n{tail}")
    return res.output


def command(r, workdir: str, args: list[str], image=None, env=None, name=None) -> list[str]:
    """The runner's command line for a tool; ``name`` passed only to runners that take it (a plugin's may not)."""
    import inspect
    kw = {k: v for k, v in (("image", image), ("env", env)) if v}
    if name and "name" in inspect.signature(r.tool_command).parameters:
        kw["name"] = name
    return r.tool_command(workdir, args, **kw)
