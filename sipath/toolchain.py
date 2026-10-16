"""Locked toolchain (P1.6): the images SiPath is tested with, pinned by digest,
and the tool versions inside them (``toolchain.json``).

The run configuration's default images come from here, and a run refuses an
image that is not pinned by digest unless ``require_digest`` is turned off
(``SIPATH_REQUIRE_DIGEST=0``).

    python3 -m sipath toolchain            the locked images and versions
    python3 -m sipath toolchain --check    the configured images' tool versions against the lock
"""
from __future__ import annotations

import json
import os
import re
import subprocess

LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toolchain.json")

# how each tool reports its version, per image layout: (command, regex)
PROBES = {
    "openroad": ("{openroad} -version", r"(\S+)"),
    "yosys": ("yosys -V", r"Yosys (\S+)"),
    "klayout": ("klayout -v", r"KLayout (\S+)"),
    "magic": ("magic --version", r"(\S+)"),
}


def lock() -> dict:
    with open(LOCK) as f:
        return json.load(f)


def image(name: str) -> str:
    """The locked reference (name@sha256:...) of image ``name`` (orfs, magic, librelane_arm64)."""
    return lock()["images"][name]["ref"]


def pinned(ref: str) -> bool:
    return "@sha256:" in ref


def probe(ref: str, platform: str, tools, openroad: str = "openroad") -> dict:
    """{tool: version} reported inside image ``ref`` (never pulled: a missing image is an error)."""
    out = {}
    for t in tools:
        cmd, rx = PROBES[t]
        r = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--platform", platform, ref, "bash", "-c",
                            cmd.format(openroad=openroad)], capture_output=True, text=True, timeout=300)
        m = re.search(rx, (r.stdout or "") + (r.stderr or "")) if r.returncode == 0 else None
        out[t] = m.group(1) if m else f"error: {(r.stderr or r.stdout).strip().splitlines()[-1:] or r.returncode}"
    return out


def check(config) -> list[str]:
    """Differences between the configured images and the lock: an image that is
    not a locked one, or a tool whose version differs. Empty: matches."""
    from .session import ORFS_OPENROAD
    problems = []
    locked = {v["ref"]: (k, v) for k, v in lock()["images"].items()}
    for field, ref in (("image", config.image), ("magic_image", config.magic_image)):
        if ref not in locked:
            problems.append(f"{field} {ref} is not a locked image ({LOCK})")
            continue
        name, entry = locked[ref]
        binary = config.openroad_binary or (ORFS_OPENROAD if name != "librelane_arm64" else "openroad")
        got = probe(ref, entry["platform"], entry["tools"], binary)
        for tool, want in entry["tools"].items():
            if got.get(tool) != want:
                problems.append(f"{name}: {tool} {got.get(tool)} (locked {want})")
    return problems


def main(argv) -> int:
    data = lock()
    for name, e in data["images"].items():
        print(f"{name}: {e['ref']}\n  {e['tag']}, {e['platform']}; "
              + ", ".join(f"{t} {v}" for t, v in e["tools"].items()))
    if "--check" in argv:
        from .config import Config
        problems = check(Config.load())
        print("\n".join(problems) if problems else "configured images match the lock")
        return 1 if problems else 0
    return 0
