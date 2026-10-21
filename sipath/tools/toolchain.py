"""The locked images' tools, probed (``python3 -m sipath toolchain [--check]``).

Runs each locked image (Docker, never pulled) and compares the versions its
tools report with the lock (``sipath.toolchain``, ``toolchain.json``).
"""
from __future__ import annotations

import re
import subprocess

from ..toolchain import LOCK, lock, pdk_status
from . import klayout, magic, netgen, yosys

# how each tool reports its version, per image layout: (command, regex); the adapters hold their own
PROBES = {
    "openroad": ("{openroad} -version", r"(\S+)"),
    "yosys": yosys.VERSION,
    "klayout": klayout.VERSION,
    "magic": magic.VERSION,
    "cf-precheck": ("cf-precheck --version", r"cf-precheck (\S+)"),
    "netgen": netgen.VERSION,
    "cvc": ("cvc_rv --version", r"Version (\S+)"),
}


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
    from .openroad.session import ORFS_OPENROAD
    problems = []
    locked = {v["ref"]: (k, v) for k, v in lock()["images"].items()}
    pre = getattr(config, "precheck_image", "") or lock()["images"].get("precheck", {}).get("ref", "")
    for field, ref in (("image", config.image), ("magic_image", config.magic_image), ("precheck_image", pre)):
        if not ref:
            continue
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
    for fam, e in data.get("pdks", {}).items():
        print(f"pdk {fam}: open_pdks {e['version']} ({', '.join(e['variants'])}); {e['source']}")
    if "--check" in argv:
        from ..config import Config
        from ..pdk import load as load_pdk
        problems = check(Config.load())
        for fam, e in data.get("pdks", {}).items():
            for v in e["variants"]:
                try:
                    st = pdk_status(load_pdk(v))
                except Exception as ex:  # a PDK that does not load is a problem, not a crash
                    problems.append(f"pdk {v}: {ex}")
                    continue
                if st and not st["match"]:
                    problems.append(f"pdk {v}: {st['tree']} is {st['version'] or 'of an unknown release'} "
                                    f"(locked {st['locked']})")
                if st and st["missing_libraries"]:
                    problems.append(f"pdk {v}: {st['tree']} lacks {', '.join(st['missing_libraries'])} "
                                    f"(python3 -m sipath install-pdk {fam})")
        print("\n".join(problems) if problems else "configured images and PDK trees match the lock")
        return 1 if problems else 0
    return 0
