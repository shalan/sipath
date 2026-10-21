"""The KLayout adapter: batch scripts (GDS streaming, XOR, the chip check, the
seal ring) and the PDK's DRC/LVS decks, run by ``sipath.signoff``."""
from __future__ import annotations

from .run import ToolFailed, run_tool

VERSION = ("klayout -v", r"KLayout (\S+)")


def batch(ctx, script: str, log: str, defines: dict | None = None, mode: str = "-b",
          error: type = ToolFailed) -> str:
    """``klayout <mode> -rd k=v ... -r <script>`` (``-b`` batch; ``-zz`` batch without
    the GUI's application layer, for streaming)."""
    args = ["klayout", mode]
    for k, v in (defines or {}).items():
        args += ["-rd", f"{k}={v}"]
    return run_tool(ctx, args + ["-r", script], log, error=error)
