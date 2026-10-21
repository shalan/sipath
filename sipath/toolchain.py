"""Locked toolchain (P1.6): the images SiPath is tested with, pinned by digest,
the tool versions inside them and the open_pdks release (``toolchain.json``).
This module reads the lock (core: no tool runs here); probing the images'
tools is ``sipath.tools.toolchain``.

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

LOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toolchain.json")

def lock() -> dict:
    with open(LOCK) as f:
        return json.load(f)


def image(name: str) -> str:
    """The locked reference (name@sha256:...) of image ``name`` (orfs, magic, librelane_arm64)."""
    return lock()["images"][name]["ref"]


def pinned(ref: str) -> bool:
    return "@sha256:" in ref


def pdk_release(tree: str) -> str | None:
    """The open_pdks release (commit) of an installed PDK tree: its SOURCES
    file (``open_pdks <commit>``), else ciel's ``versions/<commit>/`` in the
    tree's real path; None when neither says."""
    src = os.path.join(tree, "SOURCES")
    if os.path.isfile(src):
        with open(src, errors="replace") as f:
            m = re.search(r"^open_pdks\s+([0-9a-f]{7,40})\b", f.read(), re.M)
        if m:
            return m.group(1)
    m = re.search(r"/versions/([0-9a-f]{7,40})/", os.path.realpath(tree) + "/")
    return m.group(1) if m else None


def pdk_status(pdk) -> dict | None:
    """The PDK's release against the lock: {family, tree, version, locked,
    match}; None for a PDK without a pinned release (e.g. ORFS platform files,
    pinned with their image)."""
    rel = getattr(pdk, "release", None) or {}
    family = rel.get("family")
    entry = lock().get("pdks", {}).get(family) if family else None
    if not entry:
        return None
    tree = rel.get("tree", "")
    got = pdk_release(tree) if tree and os.path.isdir(tree) else None
    want = entry["version"]
    missing = [lib for lib in entry.get("libraries", [])
               if tree and os.path.isdir(tree) and not os.path.isdir(os.path.join(tree, "libs.ref", lib))]
    return {"family": family, "tree": tree, "version": got, "locked": want,
            "match": bool(got) and (want.startswith(got) or got.startswith(want)), "missing_libraries": missing}


def require_pdk(pdk, config) -> dict | None:
    """``pdk_status``, raising when the tree is of another release (or of an
    unknown one) and ``config.require_pdk_release`` is on."""
    st = pdk_status(pdk)
    if st and not st["match"] and getattr(config, "require_pdk_release", True):
        found = f"release {st['version']}" if st["version"] else "no release (no SOURCES file, not a ciel version)"
        raise RuntimeError(f"PDK {pdk.name}: {st['tree']} has {found}; SiPath is locked to open_pdks "
                           f"{st['locked']} ({LOCK}). Run `python3 -m sipath install-pdk {st['family']}`, or set "
                           "SIPATH_REQUIRE_PDK=0 to run anyway (the run records the release it used)")
    return st


def install_command(family: str, pdk_root: str | None = None) -> list[str]:
    """The ciel command that installs and enables the locked release of ``family``."""
    entry = lock().get("pdks", {}).get(family)
    if not entry:
        raise ValueError(f"no locked PDK family {family!r}; locked: {sorted(lock().get('pdks', {}))}")
    root = os.path.expanduser(pdk_root or os.environ.get("PDK_ROOT") or "~/.ciel")
    libs = [a for lib in entry.get("libraries", []) for a in ("-l", lib)]  # ciel's default set lacks some
    return ["ciel", "enable", "--pdk-root", root, "--pdk-family", family, *libs, entry["version"]]
