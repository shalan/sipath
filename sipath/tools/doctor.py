"""``python3 -m sipath doctor [--deep]``: is this machine ready to run SiPath, and what would a run use?

Each line is ``ok``, ``warn`` (an optional part is missing or something deserves a look) or ``FAIL`` (a run
would not start), with the fix. Fast by default: images are inspected, not run; ``--deep`` also runs every
locked image and compares its tools' versions with the lock (``toolchain --check``). Exit 1 on a FAIL.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys


class Report:
    def __init__(self):
        self.lines: list[tuple[str, str, str, str]] = []

    def add(self, level: str, topic: str, msg: str, fix: str = ""):
        self.lines.append((level, topic, msg, fix))

    @property
    def failed(self) -> bool:
        return any(lv == "FAIL" for lv, *_ in self.lines)

    def text(self) -> str:
        out = []
        for lv, topic, msg, fix in self.lines:
            out.append(f"{lv:4}  {topic}: {msg}")
            if fix:
                out.append(f"      fix: {fix}")
        return "\n".join(out)


def _image(ref: str) -> str | None:
    """The local image ID of ``ref``, or None when it is not here (never pulled)."""
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _docker(rep: Report) -> bool:
    if not shutil.which("docker"):
        rep.add("warn", "docker", "not installed: the Docker runtime, Magic and the precheck are unavailable",
                "install Docker (Docker Desktop on macOS)")
        return False
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}} {{.Architecture}}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    if r is None or r.returncode != 0:
        rep.add("warn", "docker", "installed, but its daemon does not answer", "start Docker")
        return False
    rep.add("ok", "docker", f"server {r.stdout.strip()}")
    return True


def check(deep: bool = False) -> Report:  # noqa: C901 (one list of independent checks)
    from .. import toolchain
    from ..config import Config, ConfigError
    rep = Report()
    py = sys.version_info
    rep.add("ok" if py >= (3, 10) else "FAIL", "python", f"{platform.python_version()} ({sys.executable})",
            "" if py >= (3, 10) else "Python 3.10 or newer")
    osname, arch = sys.platform, platform.machine()
    rep.add("ok", "machine", f"{osname} {arch}, {os.cpu_count()} cores"
            + ("; the amd64 images run under Rosetta here (see docs/pdks-and-runtimes.md for arm64 options)"
               if osname == "darwin" and arch == "arm64" else ""))
    for mod, why in (("yaml", "design files and PDK descriptions"),):
        if importlib.util.find_spec(mod) is None:
            rep.add("FAIL", "python packages", f"{mod} missing ({why})", "python3 -m pip install -e .")
    try:
        cfg = Config.load()
    except (ConfigError, ValueError, OSError) as e:
        rep.add("FAIL", "config", str(e), "fix the SIPATH_* variable or the --config file it names")
        return rep
    rep.add("ok", "config", f"runtime {cfg.runtime}, {cfg.threads} threads"
            + (f", config file {os.environ['SIPATH_CONFIG']}" if os.environ.get("SIPATH_CONFIG") else ""))
    has_docker = _docker(rep)

    # the native tools tree (install-tools)
    from . import native
    root = os.path.abspath(os.path.expanduser(cfg.tools_root or native.DEFAULT))
    stamp = os.path.join(root, ".sipath-image")
    if os.path.isfile(stamp):
        ref = open(stamp).read().strip()
        rep.add("ok" if ref == cfg.image else "warn", "native tools", f"{root} holds {ref}"
                + ("" if ref == cfg.image else f", not the configured image {cfg.image}: not used"),
                "" if ref == cfg.image else f"python3 -m sipath install-tools {root} --force")
    elif osname == "linux" and arch in ("x86_64", "AMD64"):
        ok, libc = native.glibc_ok()
        rep.add("warn" if ok else "warn", "native tools", f"not installed in {root}"
                + ("" if ok else f" (and this host's glibc {libc} is older than the tools need)"),
                "python3 -m sipath install-tools" if ok else "use the Docker runtime")

    # the runtime a run would use
    from .openroad.session import NativeRunner, find_openroad
    runner = find_openroad(cfg)
    if runner is None:
        fix = ("python3 -m sipath install-tools (Linux x86_64), or "
               if osname == "linux" else "") + f"docker pull {cfg.image}"
        rep.add("FAIL", "runtime", f"no OpenROAD found (runtime {cfg.runtime})", fix)
    elif isinstance(runner, NativeRunner):
        why = ("the locked tools unpacked by install-tools" if runner.bindir else
               "$SIPATH_OPENROAD" if os.environ.get("SIPATH_OPENROAD") else "openroad on PATH")
        rep.add("ok" if runner.bindir or os.environ.get("SIPATH_OPENROAD") else "warn", "runtime",
                f"native: {runner.binary} ({why})",
                "" if runner.bindir else "a binary on PATH is used as is; its version is not the lock's unless "
                                         "you made it so (SIPATH_RUNTIME=docker uses the locked image)")
    else:
        locked = cfg.image == toolchain.image("orfs")
        rep.add("ok" if locked else "warn", "runtime", f"docker: {cfg.image}"
                + (" (the locked image)" if locked else " (not the locked image)"))

    # images: the locked ORFS one, Magic, the precheck
    if has_docker:
        pre = cfg.precheck_image or toolchain.lock()["images"].get("precheck", {}).get("ref", "")
        for name, ref, need, fix in (
                ("orfs image", cfg.image, runner is None or runner.kind == "docker", f"docker pull {cfg.image}"),
                ("magic image", cfg.magic_image, False,
                 "docker build --platform linux/amd64 -t sipath/orfs-magic:8.3.685 docker/magic, then name it "
                 "(docs/examples.md, recipe 8)"),
                ("precheck image", pre, False,
                 "after the Magic image: docker build --platform linux/amd64 -t sipath/precheck:1.3.7 "
                 "docker/precheck, then name it (docs/examples.md, recipe 8)")):
            if not ref:
                continue
            iid = _image(ref)
            if iid:
                rep.add("ok", name, f"{ref} present")
            else:
                rep.add("FAIL" if need else "warn", name, f"{ref} not here"
                        + ("" if need else " (optional: " + ("Magic DRC, XOR, integrate" if "magic" in name
                                                               else "the precheck step") + ")"), fix)

    # PDKs: the locked open_pdks release and the views the runs read
    from ..pdk import load as load_pdk
    for fam, e in toolchain.lock().get("pdks", {}).items():
        for i, variant in enumerate(e.get("variants", [])):
            required = i == 0  # the family's first variant is the one the examples use
            try:
                pdk = load_pdk(variant)
            except Exception as ex:  # noqa: BLE001 (a PDK that does not load is a finding)
                rep.add("FAIL" if required else "warn", f"pdk {variant}", str(ex).splitlines()[0][:200],
                        f"python3 -m sipath install-pdk {fam}")
                continue
            st = toolchain.pdk_status(pdk)
            missing = [f for f in pdk.files() if f and not os.path.exists(f)]
            if st and not st["match"]:
                rep.add("FAIL" if required else "warn", f"pdk {variant}",
                        f"{st['tree']} is {st['version'] or 'of an unknown release'}, locked {st['locked'][:12]}",
                        f"python3 -m sipath install-pdk {fam}")
            elif st and st.get("missing_libraries"):
                rep.add("FAIL" if required else "warn", f"pdk {variant}",
                        f"libraries missing: {st['missing_libraries']}", f"python3 -m sipath install-pdk {fam}")
            elif missing and st and st["match"]:  # the locked release itself lacks them: reinstalling won't help
                rep.add("FAIL" if required else "warn", f"pdk {variant}",
                        f"the description names {len(missing)} files the locked release does not ship, e.g. "
                        f"{[os.path.basename(m) for m in missing[:3]]}",
                        f"the PDK description (sipath/pdks) needs files this release has; steps reading them fail")
            elif missing:
                rep.add("FAIL" if required else "warn", f"pdk {variant}",
                        f"{len(missing)} views missing, e.g. {missing[:2]}", f"python3 -m sipath install-pdk {fam}")
            else:
                rep.add("ok", f"pdk {variant}", f"{st['tree'] if st else pdk.name}, release "
                        f"{(st or {}).get('version') or 'pinned with its image'}, every view present")

    # optional tools and packages
    opt = [("verilator", shutil.which("verilator"), "synthesis lint with Verilator (verilator: auto skips it)"),
           ("ciel", shutil.which("ciel") or importlib.util.find_spec("ciel"), "python3 -m sipath install-pdk"),
           ("placer python", os.path.isfile(os.path.expanduser(cfg.placer_python
                                                               or "~/.cache/sipath/placer-venv/bin/python")),
            "macros method cdlns (python3 -m sipath setup-placer)"),
           ("claude-agent-sdk", importlib.util.find_spec("claude_agent_sdk"), "sipath/examples/agent_sdk.py")]
    for name, found, what in opt:
        rep.add("ok" if found else "warn", name, "present" if found else f"not found (optional: {what})")

    if deep and has_docker:
        from .toolchain import check as tc
        problems = tc(cfg)
        rep.add("ok" if not problems else "FAIL", "image tools", "versions match the lock" if not problems
                else "; ".join(problems))
    return rep


def main(argv) -> int:
    rep = check(deep="--deep" in argv)
    print(rep.text())
    print("\nnot ready: fix the FAIL lines" if rep.failed else "\nready")
    return 1 if rep.failed else 0
