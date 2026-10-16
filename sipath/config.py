"""Run configuration: how OpenROAD is started and how attempts are run.

Values come from, in order (later wins): the defaults below, a config file
(``$SIPATH_CONFIG``, or the ``path`` given; YAML or JSON), environment
variables, the ``config:`` section of a design file, and keyword overrides.

=====================  ======================  =====================================
field                  environment             meaning
=====================  ======================  =====================================
threads                SIPATH_THREADS            OpenROAD ``-threads`` per process (default: all cores, as ORFS)
command_timeout        SIPATH_TIMEOUT            seconds one Tcl command may take
tool_timeout           SIPATH_TOOL_TIMEOUT       seconds one signoff tool run (KLayout DRC/LVS, Magic) may take
parallel_cap           SIPATH_PARALLEL_CAP       most attempts alive at once (0: none)
container_cpus         SIPATH_CPUS               docker ``--cpus`` per process
container_memory       SIPATH_MEMORY             docker ``--memory`` per process (e.g. 8g)
image                  SIPATH_OPENROAD_IMAGE     ORFS image; ``name@sha256:...`` pins it
openroad_binary        SIPATH_OPENROAD_BINARY    OpenROAD inside the image (default the ORFS path; "openroad" for
                                                 an image with it on PATH, e.g. LibreLane's)
platform               SIPATH_PLATFORM           image platform (linux/amd64; linux/arm64 for a native arm64 image)
magic_image            SIPATH_MAGIC_IMAGE        image with Magic (signoff magic_drc / xor; docker/magic)
native_tools           SIPATH_NATIVE_TOOLS       tools run on the host instead of the image (e.g. klayout,yosys)
require_digest         SIPATH_REQUIRE_DIGEST     refuse an image not pinned by digest
debug_failed           SIPATH_DEBUG_FAILED       write a debug ODB for failed attempts
log_level              SIPATH_LOG_LEVEL          ``logging`` level of the ``flow`` logger
=====================  ======================  =====================================
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields

ORFS_IMAGE = "openroad/orfs:latest"

ENV = {"threads": "SIPATH_THREADS", "command_timeout": "SIPATH_TIMEOUT", "tool_timeout": "SIPATH_TOOL_TIMEOUT", "parallel_cap": "SIPATH_PARALLEL_CAP",
       "container_cpus": "SIPATH_CPUS", "container_memory": "SIPATH_MEMORY", "image": "SIPATH_OPENROAD_IMAGE",
       "openroad_binary": "SIPATH_OPENROAD_BINARY", "platform": "SIPATH_PLATFORM",
       "magic_image": "SIPATH_MAGIC_IMAGE", "native_tools": "SIPATH_NATIVE_TOOLS",
       "require_digest": "SIPATH_REQUIRE_DIGEST", "debug_failed": "SIPATH_DEBUG_FAILED",
       "log_level": "SIPATH_LOG_LEVEL"}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    threads: int = field(default_factory=lambda: os.cpu_count() or 2)  # all cores (ORFS NUM_CORES)
    command_timeout: float = 3600.0
    tool_timeout: float = 7200.0
    parallel_cap: int = 0
    container_cpus: float | None = None
    container_memory: str | None = None
    image: str = ORFS_IMAGE
    openroad_binary: str = ""  # empty: the ORFS image's path
    platform: str = "linux/amd64"
    magic_image: str = "sipath/orfs-magic:8.3.685"  # built from docker/magic/Dockerfile
    native_tools: list = field(default_factory=list)  # e.g. ["klayout", "yosys"]: host binaries, not the image's
    require_digest: bool = False
    debug_failed: bool = False
    log_level: str = "WARNING"

    def __post_init__(self):
        self.validate()

    def validate(self):
        if int(self.threads) < 1:
            raise ConfigError("threads must be >= 1")
        if float(self.command_timeout) <= 0:
            raise ConfigError("command_timeout must be > 0")
        if int(self.parallel_cap) < 0:
            raise ConfigError("parallel_cap must be >= 0")
        if self.container_cpus is not None and float(self.container_cpus) <= 0:
            raise ConfigError("container_cpus must be > 0")
        if self.require_digest and "@sha256:" not in self.image:
            raise ConfigError(f"require_digest: image {self.image!r} is not pinned (use name@sha256:<digest>)")

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
