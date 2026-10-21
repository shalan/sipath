"""The Magic adapter: batch Tcl scripts in the Magic image (``config.magic_image``,
built from docker/magic), with a PDK's rc file (Magic DRC, XOR, frame layouts)."""
from __future__ import annotations

import os

from .run import ToolFailed, run_tool

VERSION = ("magic --version", r"(\S+)")


def run(ctx, rc: str, script: str, name: str, error: type = ToolFailed) -> str:
    """Write ``script`` as ``<name>.tcl`` and run it with Magic (no display) and ``rc``;
    returns its output. The rc file loads its tech from $PDK_ROOT/<variant>."""
    # <root>/<variant>/libs.tech/magic/<variant>.magicrc
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(rc))))
    path = ctx.path(f"{name}.tcl")
    with open(path, "w") as f:
        f.write(script)
    cfg = getattr(ctx, "config", None)
    from ..config import MAGIC_IMAGE
    image = getattr(cfg, "magic_image", None) or MAGIC_IMAGE
    try:
        return run_tool(ctx, ["magic", "-dnull", "-noconsole", "-rcfile", rc, path], ctx.path(f"{name}.log"),
                        image=image, env={"PDK_ROOT": root}, error=error)
    except error as e:
        raise error(f"{e}\n(Magic runs from the image {image}: build it with "
                    f"docker build -t {image} docker/magic, or set magic_image)") from e
