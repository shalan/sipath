"""Sessions: a long-lived OpenROAD process that receives Tcl and answers.

Protocol: every ``run()`` sends one line that evaluates the base64-encoded
script inside ``catch`` at global level, then prints a sentinel carrying the
return code, the base64 result and the base64 errorInfo. Everything printed
before the sentinel is the command's transcript. A non-zero return code
raises ``TclError`` unless ``check=False``; nothing is swallowed.

``FakeSession`` answers from canned responses. It exists to exercise the
scheduler graph without a binary; it never produces a design.
"""
from __future__ import annotations

import base64
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass

from ...config import ORFS_IMAGE, Config
ORFS_OPENROAD = "/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad"
ORFS_PLATFORMS = "/OpenROAD-flow-scripts/flow/platforms"


class TclError(RuntimeError):
    def __init__(self, script: str, result: str, error_info: str, transcript: str):
        self.script, self.result, self.error_info, self.transcript = script, result, error_info, transcript
        head = script.strip().splitlines()[0][:120] if script.strip() else ""
        tail = "\n".join(transcript.strip().splitlines()[-15:])
        super().__init__(f"Tcl error in `{head}`: {result}\n--- transcript tail ---\n{tail}")


class SessionDied(RuntimeError):
    pass


@dataclass
class Result:
    rc: int
    value: str
    output: str


# -----------------------------------------------------------------------------
# how to start the binary
# -----------------------------------------------------------------------------
class NativeRunner:
    """``openroad`` on PATH (or an explicit path), or the locked image's tools unpacked by
    ``install-tools`` (``bindir``: its wrappers, used for the other tools too; ``platforms``: its
    ORFS platform tree)."""

    kind = "native"

    def __init__(self, binary: str, threads: int = 2, bindir: str | None = None, platforms: str | None = None):
        self.binary, self.threads, self.bindir, self.platforms = binary, threads, bindir, platforms

    def command(self, workdir: str, name: str) -> list[str]:
        return [self.binary, "-no_splash", "-no_init", "-threads", str(self.threads)]

    def provenance(self) -> dict:
        out = {"kind": self.kind, "binary": self.binary, "threads": self.threads}
        if self.bindir:
            out["tools"] = self.bindir
        return out

    def gui_command(self, workdir: str, script: str) -> tuple[list[str], dict]:
        """Headless GUI run of a script (for save_image)."""
        return [self.binary, "-gui", "-no_splash", "-no_init", script], {"QT_QPA_PLATFORM": "offscreen"}

    def kill(self, name: str):
        pass

    def tool_command(self, workdir: str, args: list[str], image: str | None = None,
                     env: dict | None = None) -> list[str]:
        """Command line running another tool of the installation (e.g. klayout,
        magic: on PATH here, so ``image`` does not apply; the tools tree's own wrapper when it has one)."""
        args = list(args)
        if self.bindir and args and os.sep not in args[0] and os.path.isfile(os.path.join(self.bindir, args[0])):
            args[0] = os.path.join(self.bindir, args[0])
        return (["env", *[f"{k}={v}" for k, v in env.items()]] if env else []) + args

    def mount(self, *paths):
        return self

    def version(self) -> str:
        out = subprocess.run([self.binary, "-version"], capture_output=True, text=True, timeout=60)
        return out.stdout.strip().splitlines()[0]

    def platform_root(self) -> str | None:
        return os.environ.get("SIPATH_PLATFORMS") or self.platforms

    def runner_prefixes(self) -> tuple[str, ...]:
        return ()


class DockerRunner:
    """OpenROAD inside a container. Host paths are mounted at the same path,
    so file names in Tcl are identical inside and outside."""

    kind = "docker"

    def __init__(self, image: str = ORFS_IMAGE, binary: str = ORFS_OPENROAD, mounts: list[str] | None = None,
                 platform: str = "linux/amd64", threads: int = 2, cpus: float | None = None,
                 memory: str | None = None, native_tools: dict | None = None):
        self.image, self.binary, self.platform = image, binary, platform
        self.threads, self.cpus, self.memory = threads, cpus, memory
        self.mounts = list(mounts or [])
        # tool -> host binary run instead of the image's (config.native_tools): host paths are
        # the container's, so the same command line works
        self.native_tools = dict(native_tools or {})

    def identity(self) -> list[str]:
        """On Linux, run the container as this user (files it writes in the
        mounted directories stay ours, not root's) with a writable HOME;
        Docker Desktop (macOS, Windows) maps ownership itself.
        SIPATH_DOCKER_AS_ROOT=1 keeps the image's user."""
        if not sys.platform.startswith("linux") or os.environ.get("SIPATH_DOCKER_AS_ROOT") == "1":
            return []
        return ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]

    def limits(self) -> list[str]:
        out = []
        if self.cpus:
            out += ["--cpus", str(self.cpus)]
        if self.memory:
            out += ["--memory", str(self.memory)]
        return out

    def image_id(self) -> dict:
        """The image's local id and repo digest (what actually runs)."""
        fmt = "{{.Id}} {{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}"
        r = subprocess.run(["docker", "image", "inspect", "--format", fmt, self.image], capture_output=True,
                           text=True, timeout=60)
        parts = r.stdout.split()
        return {"image": self.image, "id": parts[0] if parts else None, "digest": parts[1] if len(parts) > 1 else None}

    def provenance(self) -> dict:
        out = {"kind": self.kind, **self.image_id(), "platform": self.platform, "binary": self.binary,
               "threads": self.threads, "cpus": self.cpus, "memory": self.memory}
        if self.native_tools:
            out["native_tools"] = {t: {"binary": b, "version": tool_version(b)} for t, b in self.native_tools.items()}
        return out

    def command(self, workdir: str, name: str) -> list[str]:
        cmd = ["docker", "run", "-i", "--rm", "--init", "--name", name, "--platform", self.platform, *self.limits(),
               *self.identity()]
        # a mount whose source was deleted (an old run dir) would make docker refuse to start
        for m in sorted({os.path.abspath(workdir), *map(os.path.abspath, self.mounts)}):
            if os.path.isdir(m):
                cmd += ["-v", f"{m}:{m}"]
        cmd += ["-w", os.path.abspath(workdir), self.image, self.binary, "-no_splash", "-no_init", "-threads",
                str(self.threads)]
        return cmd

    def gui_command(self, workdir: str, script: str) -> tuple[list[str], dict]:
        """Headless GUI run of a script (for save_image), Qt offscreen in the container."""
        cmd = ["docker", "run", "--rm", "--platform", self.platform, *self.limits(), *self.identity(),
               "-e", "QT_QPA_PLATFORM=offscreen"]
        for m in sorted({os.path.abspath(workdir), *map(os.path.abspath, self.mounts)}):
            if os.path.isdir(m):
                cmd += ["-v", f"{m}:{m}"]
        cmd += ["-w", os.path.abspath(workdir), self.image, self.binary, "-gui", "-no_splash", "-no_init", script]
        return cmd, {}

    def kill(self, name: str):
        subprocess.run(["docker", "kill", name], capture_output=True)

    def tool_command(self, workdir: str, args: list[str], image: str | None = None,
                     env: dict | None = None) -> list[str]:
        """Command line running another tool of the image (e.g. klayout) with
        the same mounts as an OpenROAD session; ``image``: another image (e.g.
        one with Magic), ``env``: environment variables in the container. A
        tool in ``native_tools`` runs on the host instead (no image)."""
        if args and args[0] in self.native_tools:
            return (["env", *[f"{k}={v}" for k, v in env.items()]] if env else []) + \
                [self.native_tools[args[0]], *args[1:]]
        cmd = ["docker", "run", "--rm", "--platform", self.platform, *self.limits(), *self.identity()]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        for m in sorted({os.path.abspath(workdir), *map(os.path.abspath, self.mounts)}):
            if os.path.isdir(m):
                cmd += ["-v", f"{m}:{m}"]
        return cmd + ["-w", os.path.abspath(workdir), image or self.image, *args]

    def mount(self, *paths):
        """Make host directories visible in the container at the same path."""
        for p in paths:
            if not os.path.exists(p) and not os.path.exists(os.path.dirname(os.path.abspath(p))):
                continue  # not a host path (e.g. a file inside the image)
            p = os.path.abspath(p if os.path.isdir(p) else os.path.dirname(p))
            if not any(p == m or p.startswith(m + os.sep) for m in self.mounts):
                self.mounts.append(p)
        return self

    def version(self) -> str:
        out = subprocess.run(["docker", "run", "--rm", "--platform", self.platform, self.image, self.binary, "-version"],
                             capture_output=True, text=True, timeout=300)
        return out.stdout.strip().splitlines()[0]

    def platform_root(self) -> str:
        return ORFS_PLATFORMS

    def runner_prefixes(self) -> tuple[str, ...]:
        return (os.path.dirname(ORFS_PLATFORMS.rstrip("/")),)


def tool_version(binary: str) -> str:
    """First line of ``<binary> -v`` / ``-V`` / ``--version`` (provenance)."""
    for flag in ("-v", "-V", "--version"):
        try:
            r = subprocess.run([binary, flag], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        line = (r.stdout or r.stderr).strip().splitlines()
        if r.returncode == 0 and line:
            return line[0][:120]
    return "?"


def native_tools(config: Config) -> dict:
    """config.native_tools that exist on the host: {tool: binary}. A tool
    asked for and not found is an error (silently falling back to the image
    would hide which build ran)."""
    out = {}
    for name in config.native_tools or []:
        b = shutil.which(name)
        if not b:
            raise FileNotFoundError(f"native_tools: {name} is not on PATH")
        out[name] = b
    return out


def installed_tools(config: Config | None = None) -> NativeRunner | None:
    """A native runner on the tools ``install-tools`` unpacked (``config.tools_root``), when they are
    there and are the configured image's: a tree of another image is left alone."""
    from .. import native
    cfg = config or Config.load()
    root = os.path.abspath(os.path.expanduser(cfg.tools_root or native.DEFAULT))
    binary = os.path.join(root, "sipath-bin", "openroad")
    try:
        with open(os.path.join(root, ".sipath-image")) as f:
            ref = f.read().strip()
    except OSError:
        return None
    if ref != cfg.image or not os.access(binary, os.X_OK):
        if ref != cfg.image:
            import logging
            logging.getLogger("sipath").warning("tools in %s are %s, not the configured image %s: not used",
                                                root, ref, cfg.image)
        return None
    return NativeRunner(binary, threads=cfg.threads, bindir=os.path.dirname(binary),
                        platforms=os.path.join(root, "OpenROAD-flow-scripts", "flow", "platforms"))


def find_openroad(config: Config | None = None):
    """Return a runner for an available OpenROAD, or None.

    ``config.runtime`` (``SIPATH_RUNTIME``) chooses. ``auto`` (the default), in order: $SIPATH_OPENROAD
    (path), ``openroad`` on PATH, the locked image's tools unpacked by ``install-tools``
    (``config.tools_root``, default ~/.cache/sipath/orfs-root), then the ORFS docker image
    (``config.image``: $SIPATH_OPENROAD_IMAGE, default the locked ORFS digest (toolchain.json),
    ``name@sha256:...`` to pin it) if docker is present and the image is already pulled. ``native``
    stops before the image; ``docker`` takes only the image. Threads and container limits come from
    ``config`` (default: ``Config.load()``).
    """
    cfg = config or Config.load()
    if cfg.runtime != "docker":
        explicit = os.environ.get("SIPATH_OPENROAD")
        if explicit:
            return NativeRunner(explicit, threads=cfg.threads)
        native = shutil.which("openroad")
        if native:
            return NativeRunner(native, threads=cfg.threads)
        tree = installed_tools(cfg)
        if tree is not None or cfg.runtime == "native":
            return tree
    if shutil.which("docker"):
        try:
            ok = subprocess.run(["docker", "image", "inspect", cfg.image], capture_output=True,
                                timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return DockerRunner(cfg.image, binary=cfg.openroad_binary or ORFS_OPENROAD, platform=cfg.platform,
                                threads=cfg.threads, cpus=cfg.container_cpus, memory=cfg.container_memory,
                                native_tools=native_tools(cfg))
    return None


# -----------------------------------------------------------------------------
# sessions
# -----------------------------------------------------------------------------
class Session:
    def run(self, script: str, check: bool = True, timeout: float | None = None) -> Result:
        raise NotImplementedError

    def value(self, script: str, timeout: float | None = None) -> str:
        return self.run(script, timeout=timeout).value

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_SENT = "@@FLOW-END@@"
_WERR = "@@FLOW-WERR@@"


class OpenRoadSession(Session):
    """One OpenROAD process. Used for exactly one attempt, then closed."""

    def __init__(self, workdir: str, runner=None, log_path: str | None = None, timeout: float = 3600.0):
        self.runner = runner or find_openroad()
        if self.runner is None:
            raise FileNotFoundError("no OpenROAD available (openroad not on PATH, no ORFS docker image)")
        self.workdir = os.path.abspath(workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self.timeout = timeout
        self.name = f"sipath-{uuid.uuid4().hex[:12]}"
        self.log = open(log_path or os.path.join(self.workdir, "openroad.log"), "a", buffering=1)
        self.log.write(f"# session {self.name} start {time.ctime()}\n")
        self.proc = subprocess.Popen(self.runner.command(self.workdir, self.name), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     cwd=self.workdir)
        self.lines: queue.Queue = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.count = 0
        self.closed = False
        self.killed = False
        self.timings: list[tuple[str, float]] = []  # (first line of each script, seconds)

    def kill(self):
        """Kill the process now (cancellation, from any thread). The reader sees
        EOF, so a waiting ``run`` raises SessionDied; pipes are closed by close()."""
        self.killed = True
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        finally:
            self.runner.kill(self.name)

    def _read(self):
        for line in self.proc.stdout:
            self.log.write(line)
            self.lines.put(line)
        self.lines.put(None)

    def run(self, script: str, check: bool = True, timeout: float | None = None) -> Result:
        if self.closed:
            raise SessionDied("session is closed")
        self.count += 1
        tag = f"{self.count}"
        b64 = base64.b64encode(script.encode()).decode()
        self.log.write(f"# >>> [{tag}]\n" + "".join(f"# | {line}\n" for line in script.strip().splitlines()))
        # Results travel through files in the (mounted) session directory:
        # OpenROAD's stdout channel re-emits bytes on lines longer than 4 KiB.
        res_path = os.path.join(self.workdir, f".flow_res_{tag}")
        info_path = os.path.join(self.workdir, f".flow_info_{tag}")
        # if the result files cannot be written the sentinel still comes, with rc 9
        wrapper = (
            f"set ::__src [encoding convertfrom utf-8 [binary decode base64 {b64}]]; "
            f"set ::__rc [catch {{uplevel #0 $::__src}} ::__res ::__opts]; "
            f"if {{[catch {{"
            f"set ::__f [open {{{res_path}}} w]; fconfigure $::__f -encoding utf-8; "
            f"puts -nonewline $::__f $::__res; close $::__f; "
            f"set ::__f [open {{{info_path}}} w]; fconfigure $::__f -encoding utf-8; "
            f"puts -nonewline $::__f [expr {{[dict exists $::__opts -errorinfo] ? [dict get $::__opts -errorinfo] : {{}}}}]; "
            f"close $::__f }} ::__werr]}} {{ puts \"\\n{_WERR} [join [split $::__werr \\n] {{ }}]\"; set ::__rc 9 }}; "
            f"puts \"\\n{_SENT} {tag} $::__rc [string length $::__src]\"; flush stdout\n"
        )
        try:
            self.proc.stdin.write(wrapper)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise SessionDied(f"openroad stdin closed: {e}") from e
        out: list[str] = []
        t_start = time.time()
        label = next((ln.strip() for ln in script.splitlines() if ln.strip() and not ln.strip().startswith("#")), "")
        deadline = time.time() + (timeout or self.timeout)
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                self._abort()
                raise SessionDied(f"timeout after {timeout or self.timeout}s running: {script.strip()[:100]}")
            if line is None:
                self.closed = True
                if self.killed:
                    raise SessionDied("openroad process killed (cancelled)")
                raise SessionDied("openroad exited unexpectedly\n" + "".join(out[-30:]))
            if line.startswith(_SENT):
                parts = line.split()
                if len(parts) < 4 or parts[1] != tag:
                    continue
                rc = int(parts[2])
                if rc == 9:  # the process could not write its result files
                    why = next((ln[len(_WERR):].strip() for ln in reversed(out) if ln.startswith(_WERR)), "")
                    raise SessionDied(f"openroad cannot write result files in {self.workdir}: {why}")
                if int(parts[3]) != len(script):
                    raise SessionDied(f"script corrupted in transit ({parts[3]} != {len(script)} chars)")
                with open(res_path, encoding="utf-8", errors="replace") as f:
                    res = f.read()
                with open(info_path, encoding="utf-8", errors="replace") as f:
                    info = f.read()
                os.remove(res_path)
                os.remove(info_path)
                transcript = "".join(out)
                self.timings.append((label[:80], time.time() - t_start))
                if rc in (3, 4):  # break/continue outside a loop: the script did not complete
                    res = f"script ended with {'break' if rc == 3 else 'continue'} outside a loop"
                    rc = 1
                if rc == 1 and check:
                    raise TclError(script, res, info, transcript)
                return Result(rc, res, transcript)  # rc 2 (return) is a normal completion
            out.append(line)

    def _abort(self):
        self.closed = True
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        finally:
            self.runner.kill(self.name)
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    pipe.close()
                except Exception:
                    pass

    def close(self):
        if self.closed:
            for pipe in (self.proc.stdin, self.proc.stdout):
                try:
                    pipe.close()
                except Exception:
                    pass
            self.log.close() if not self.log.closed else None
            return
        self.closed = True
        try:
            self.proc.stdin.write("exit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=120)
        except Exception:
            self._abort()
        self.reader.join(timeout=10)
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except Exception:
                pass
        self.log.write(f"# session {self.name} end rc={self.proc.returncode}\n")
        self.log.close()


class FakeSession(Session):
    """Canned answers. ``responses`` maps a substring of the script to the
    value returned. Records every script it is given. Writes placeholder
    checkpoint files so the scheduler graph can be exercised; those files
    say FAKE in them and are not OpenROAD databases."""

    def __init__(self, workdir: str = ".", responses: dict[str, str] | None = None, fail_on: tuple[str, ...] = ()):
        self.workdir = workdir
        self.responses = dict(responses or {})
        self.fail_on = fail_on
        self.scripts: list[str] = []

    def run(self, script: str, check: bool = True, timeout=None) -> Result:
        self.scripts.append(script)
        for bad in self.fail_on:
            if bad in script:
                if check:
                    raise TclError(script, f"fake failure on {bad}", "", "")
                return Result(1, f"fake failure on {bad}", "")
        if script.startswith("write_db "):
            path = script.split(None, 1)[1].strip().strip("{}")
            with open(path, "w") as f:
                f.write("FAKE checkpoint, not an OpenROAD database\n")
        for key, val in self.responses.items():
            if key in script:
                return Result(0, val, "")
        return Result(0, "", "")
