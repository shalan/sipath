"""What a run's results depend on, as hashes, so a resume can tell a recorded
step from an obsolete one.

``inputs(design, pdk, config, provenance)`` describes the run: the resolved
design description, the content of every input file (RTL and its include
directories, netlist, SDC, floorplan DEF, macro views, extra LEF/Liberty), the
PDK description and release, the tools (image digests, binaries, native tools,
plugins and their code) and the configuration keys that change results.
Threads, timeouts and limits are left out: they change how long a run takes,
not what it produces. ``changes(old, new)`` names what differs between two
such descriptions. ``attempt(...)`` and ``step(...)`` chain the run's digest
with a step's name, parameters, declared defaults and its parent's
fingerprint, so a change anywhere upstream reaches every step after it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os

# configuration keys whose value changes what a run produces (the rest: how fast, how verbose)
RESULT_CONFIG = ("image", "openroad_binary", "platform", "magic_image", "precheck_image", "native_tools",
                 "runtime", "tools_root", "placer_python")
# provenance keys that do not change results: how fast it ran, and SiPath's own version (recorded, not compared)
VOLATILE = ("threads", "cpus", "memory", "sipath")
_FILES: dict[tuple, str] = {}  # (path, size, mtime) -> sha256: a file is hashed once per process


def _json(x) -> str:
    def conv(o):
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, (set, frozenset)):
            return sorted(o, key=str)
        if isinstance(o, tuple):
            return list(o)
        return str(o)
    return json.dumps(x, sort_keys=True, default=conv)


def digest(x) -> str:
    return hashlib.sha256((x if isinstance(x, str) else _json(x)).encode()).hexdigest()[:16]


def file_hash(path: str) -> str | None:
    """sha256 of a file's content (16 hex digits), None when it is not a host file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    if key not in _FILES:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        _FILES[key] = h.hexdigest()[:16]
    return _FILES[key]


def _include_files(dirs) -> list[str]:
    out = []
    for d in dirs or []:
        if os.path.isdir(d):
            for root, _, names in os.walk(d):
                out += [os.path.join(root, n) for n in names if n.endswith((".v", ".vh", ".sv", ".svh", ".h"))]
    return sorted(out)


def inputs(design, pdk, config=None, provenance: dict | None = None) -> dict:
    """The run's description: {design, files, pdk, tools, config, digest}."""
    files = [f for f in (*design.input_files(), *_include_files(getattr(design, "rtl_includes", ()))) if f]
    file_hashes = {os.path.abspath(f): file_hash(f) for f in dict.fromkeys(files)}
    prov = {k: v for k, v in (provenance or {}).items() if k not in VOLATILE}
    import sys
    plugins = {}
    loaded = prov.get("plugins") if isinstance(prov.get("plugins"), dict) else {}
    for name in sorted(loaded.get("plugins") or []):  # registry.loaded(): the plugin modules by name
        f = getattr(sys.modules.get(name), "__file__", None)
        plugins[name] = file_hash(f) if f else None
    cfg = config.to_dict() if hasattr(config, "to_dict") else dict(config or {})
    out = {
        "design": digest(design),
        "files": file_hashes,
        "pdk": {"name": getattr(pdk, "name", None), "description": digest(pdk),
                "release": prov.get("pdk_release")},
        "tools": digest({k: v for k, v in prov.items() if k != "pdk_release"}),
        "plugin_code": plugins,
        "config": {k: cfg.get(k) for k in RESULT_CONFIG if k in cfg},
    }
    out["digest"] = digest({k: v for k, v in out.items() if k != "digest"})
    return out


def changes(old: dict | None, new: dict) -> list[str]:
    """What differs between two run descriptions, in words (empty: nothing)."""
    if not old:
        return []
    out = []
    of, nf = old.get("files") or {}, new.get("files") or {}
    for f in sorted(set(of) | set(nf)):
        if f not in of:
            out.append(f"new input {os.path.basename(f)}")
        elif f not in nf:
            out.append(f"input no longer used: {os.path.basename(f)}")
        elif of[f] != nf[f]:
            out.append(f"{os.path.basename(f)} changed")
    if old.get("design") != new.get("design"):
        out.append("the design description changed (a design-file key or a PDK default it takes)")
    op, np_ = old.get("pdk") or {}, new.get("pdk") or {}
    if op.get("release") != np_.get("release"):
        out.append(f"PDK release {op.get('release')} -> {np_.get('release')}")
    elif op.get("description") != np_.get("description"):
        out.append("the PDK description changed")
    if old.get("tools") != new.get("tools"):
        out.append("the tools changed (image, binary or native tool versions)")
    if old.get("plugin_code") != new.get("plugin_code"):
        out.append("plugin code changed")
    if old.get("config") != new.get("config"):
        keys = [k for k in set(old.get("config") or {}) | set(new.get("config") or {})
                if (old.get("config") or {}).get(k) != (new.get("config") or {}).get(k)]
        out.append("configuration changed: " + ", ".join(sorted(keys)))
    if not out and old.get("digest") != new.get("digest"):
        out.append("the run's inputs changed")
    return out


def attempt(run_digest: str, step: str, params: dict, parent_fp: str | None, defaults=None) -> str:
    """One attempt: the run, the step, its parameters and declared defaults, and its parent's fingerprint."""
    return digest({"run": run_digest, "step": step, "params": params, "parent": parent_fp, "defaults": defaults})


def step(run_digest: str, step_name: str, variants, score, parent_fp: str | None, defaults=None) -> str:
    """A step: the same over all its variants and the score that selects among them."""
    return digest({"run": run_digest, "step": step_name, "variants": variants, "score": score,
                   "parent": parent_fp, "defaults": defaults})
