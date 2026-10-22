"""Run configuration: how OpenROAD is started and how attempts are run.

Values come from, in order (later wins): the defaults below, a config file
(``$SIPATH_CONFIG``, or the ``path`` given; YAML or JSON), environment
variables, the ``config:`` section of a design file, and keyword overrides.

=====================  ======================  =====================================
field                  environment             meaning
=====================  ======================  =====================================
threads                SIPATH_THREADS            the run's cores: OpenROAD ``-threads`` of one attempt, shared
                                                 among attempts running at once and among a signoff's concurrent
                                                 checks (default: all cores, as ORFS; two thirds of them on macOS)
command_timeout        SIPATH_TIMEOUT            seconds one Tcl command may take
tool_timeout           SIPATH_TOOL_TIMEOUT       seconds one signoff tool run (KLayout DRC/LVS, Magic) may take
parallel_cap           SIPATH_PARALLEL_CAP       most attempts alive at once (0: none)
budget_s               SIPATH_BUDGET             wall-time budget of a run, in seconds (0: none): past it, running
                                                 attempts stop with status "budget"; --resume continues
step_budget_s          SIPATH_STEP_BUDGET        a time box per step, in seconds (0: none): attempts not finished by
                                                 then stop ("budget") and the step selects among those that did
max_attempts           SIPATH_MAX_ATTEMPTS       most attempts a run starts (0: none); the rest end "budget"
memory_gb              SIPATH_MEMORY_BUDGET      memory budget, in GB (0: none): fewer attempts at once when the
                                                 step's recorded peak memory says they would not fit
container_cpus         SIPATH_CPUS               docker ``--cpus`` per process
container_memory       SIPATH_MEMORY             docker ``--memory`` per process (e.g. 8g)
image                  SIPATH_OPENROAD_IMAGE     ORFS image (default: the locked digest, toolchain.json)
openroad_binary        SIPATH_OPENROAD_BINARY    OpenROAD inside the image (default the ORFS path; "openroad" for
                                                 an image with it on PATH, e.g. LibreLane's)
platform               SIPATH_PLATFORM           image platform (linux/amd64; linux/arm64 for a native arm64 image)
magic_image            SIPATH_MAGIC_IMAGE        image with Magic (signoff magic_drc / xor; docker/magic)
precheck_image         SIPATH_PRECHECK_IMAGE     image with ChipFoundry's cf-precheck and its tools (precheck
                                                 step; docker/precheck)
native_tools           SIPATH_NATIVE_TOOLS       tools run on the host instead of the image (e.g. klayout,yosys)
runtime                SIPATH_RUNTIME            where OpenROAD, Yosys and KLayout run: auto (native when installed,
                                                 else the image), native, or docker (see find_openroad)
tools_root             SIPATH_TOOLS_ROOT         the locked image's tools unpacked by `sipath install-tools`
                                                 (default ~/.cache/sipath/orfs-root)
placer_python          SIPATH_PLACER_PYTHON      Python with PyTorch for the macros step's cdlns method
                                                 (default ~/.cache/sipath/placer-venv, `sipath setup-placer`)
require_digest         SIPATH_REQUIRE_DIGEST     refuse an image not pinned by digest (default on)
require_pdk_release    SIPATH_REQUIRE_PDK        refuse an open_pdks tree whose release is not the locked one
                                                 (default on; sipath/toolchain.json, `sipath install-pdk`)
debug_failed           SIPATH_DEBUG_FAILED       write a debug ODB for failed attempts
snapshots              SIPATH_SNAPSHOTS          a layout picture after every step (`sipath run`): off, selected
                                                 (the attempt each step selects) or all (every finished attempt)
log_level              SIPATH_LOG_LEVEL          ``logging`` level of the ``flow`` logger
=====================  ======================  =====================================
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields

from .toolchain import image as _locked

ORFS_IMAGE = _locked("orfs")  # openroad/orfs@sha256:... (toolchain.json)
MAGIC_IMAGE = _locked("magic")

ENV = {"threads": "SIPATH_THREADS", "command_timeout": "SIPATH_TIMEOUT", "tool_timeout": "SIPATH_TOOL_TIMEOUT", "parallel_cap": "SIPATH_PARALLEL_CAP",
       "budget_s": "SIPATH_BUDGET", "step_budget_s": "SIPATH_STEP_BUDGET", "max_attempts": "SIPATH_MAX_ATTEMPTS",
       "memory_gb": "SIPATH_MEMORY_BUDGET",
       "container_cpus": "SIPATH_CPUS", "container_memory": "SIPATH_MEMORY", "image": "SIPATH_OPENROAD_IMAGE",
       "openroad_binary": "SIPATH_OPENROAD_BINARY", "platform": "SIPATH_PLATFORM",
       "magic_image": "SIPATH_MAGIC_IMAGE", "precheck_image": "SIPATH_PRECHECK_IMAGE",
       "native_tools": "SIPATH_NATIVE_TOOLS", "runtime": "SIPATH_RUNTIME", "tools_root": "SIPATH_TOOLS_ROOT",
       "placer_python": "SIPATH_PLACER_PYTHON",
       "require_digest": "SIPATH_REQUIRE_DIGEST", "require_pdk_release": "SIPATH_REQUIRE_PDK", "debug_failed": "SIPATH_DEBUG_FAILED",
       "log_level": "SIPATH_LOG_LEVEL", "snapshots": "SIPATH_SNAPSHOTS"}


def default_threads() -> int:
    """Every core (ORFS's NUM_CORES), except on macOS, where Docker's VM shares
    the cores with the host: two thirds of them. Measured on an 18-core M5 Max:
    LibreLane's arm64 OpenROAD takes 50 s for an aes global placement with 18
    threads and 1.9 s with 12; detailed routing gains under 5% past 12."""
    import sys
    n = os.cpu_count() or 2
    return max(1, n * 2 // 3) if sys.platform == "darwin" else n


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    threads: int = field(default_factory=lambda: default_threads())
    command_timeout: float = 3600.0
    tool_timeout: float = 7200.0
    parallel_cap: int = 0
    budget_s: float = 0.0  # run wall time (0: none)
    step_budget_s: float = 0.0  # each step's time box (0: none)
    max_attempts: int = 0  # attempts a run starts (0: none)
    memory_gb: float = 0.0  # memory for attempts at once (0: none)
    container_cpus: float | None = None
    container_memory: str | None = None
    image: str = ORFS_IMAGE
    openroad_binary: str = ""  # empty: the ORFS image's path
    platform: str = "linux/amd64"
    magic_image: str = MAGIC_IMAGE  # sipath/orfs-magic, built from docker/magic/Dockerfile
    precheck_image: str = ""  # sipath/precheck (docker/precheck); empty: the locked one, once built
    native_tools: list = field(default_factory=list)  # e.g. ["klayout", "yosys"]: host binaries, not the image's
    runtime: str = "auto"  # auto | native | docker
    tools_root: str = ""  # empty: ~/.cache/sipath/orfs-root (sipath install-tools)
    placer_python: str = ""  # empty: ~/.cache/sipath/placer-venv/bin/python (macros method cdlns)
    require_digest: bool = True  # an image not pinned by digest is refused (SIPATH_REQUIRE_DIGEST=0 allows it)
    require_pdk_release: bool = True  # an open_pdks tree of another release is refused (SIPATH_REQUIRE_PDK=0 allows it)
    debug_failed: bool = False
    log_level: str = "WARNING"
    snapshots: str = "off"  # off | selected | all: layout pictures after every step (designfile.run)

    def __post_init__(self):
        self.validate()

    def validate(self):
        if int(self.threads) < 1:
            raise ConfigError("threads must be >= 1")
        if float(self.command_timeout) <= 0:
            raise ConfigError("command_timeout must be > 0")
        if int(self.parallel_cap) < 0:
            raise ConfigError("parallel_cap must be >= 0")
        for k in ("budget_s", "step_budget_s", "max_attempts", "memory_gb"):
            if float(getattr(self, k)) < 0:
                raise ConfigError(f"{k} must be >= 0 (0: no budget)")
        if self.runtime not in ("auto", "native", "docker"):
            raise ConfigError(f"runtime must be auto, native or docker, not {self.runtime!r}")
        if self.snapshots not in ("off", "selected", "all"):
            raise ConfigError(f"snapshots must be off, selected or all, not {self.snapshots!r}")
        if self.container_cpus is not None and float(self.container_cpus) <= 0:
            raise ConfigError("container_cpus must be > 0")
        if self.require_digest:
            for name, ref in (("image", self.image), ("magic_image", self.magic_image),
                              ("precheck_image", self.precheck_image)):
                if not ref:  # precheck_image unset: the locked one
                    continue
                if "@sha256:" not in ref:
                    raise ConfigError(f"require_digest: {name} {ref!r} is not pinned (use name@sha256:<digest>, "
                                      "see sipath/toolchain.json; SIPATH_REQUIRE_DIGEST=0 allows it)")

    def cap(self, parallel: int) -> int:
        """Attempts that may run at once for a step asking for ``parallel``."""
        p = max(1, int(parallel))
        return min(p, self.parallel_cap) if self.parallel_cap else p

    def to_dict(self) -> dict:
        return asdict(self)

    def replace(self, **kw) -> "Config":
        unknown = set(kw) - {f.name for f in fields(self)}
        if unknown:
            raise ConfigError(f"unknown config fields: {sorted(unknown)}")
        return Config(**{**self.to_dict(), **{k: _coerce(k, v) for k, v in kw.items() if v is not None}})

    @classmethod
    def load(cls, path: str | None = None, **overrides) -> "Config":
        cfg = cls()
        path = path or os.environ.get("SIPATH_CONFIG")
        if path:
            from .pdk import read_file
            data = read_file(path) or {}
            cfg = cfg.replace(**data)
        env = {k: os.environ[v] for k, v in ENV.items() if os.environ.get(v) not in (None, "")}
        cfg = cfg.replace(**env)
        return cfg.replace(**overrides)


def _coerce(name: str, value):
    kind = {f.name: f.type for f in fields(Config)}[name]
    if isinstance(value, str):
        if "bool" in kind:
            return value.strip().lower() in ("1", "true", "yes", "on")
        if kind.startswith("int"):
            return int(value)
        if kind.startswith("float"):
            return float(value)
        if kind.startswith("list"):  # SIPATH_NATIVE_TOOLS=klayout,yosys
            return [x.strip() for x in value.split(",") if x.strip()]
    return value
