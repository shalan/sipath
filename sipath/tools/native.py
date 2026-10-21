"""The locked ORFS image's tools, installed without Docker (Colab, a Linux box).

    python3 -m sipath install-tools [dir] [--force]   default ~/.cache/sipath/orfs-root

Downloads the image locked in ``toolchain.json`` (``openroad/orfs@sha256:...``)
straight from the registry (a cut transfer resumes; a re-run keeps the layers
already fetched), checks every layer against its digest, then unpacks the
layers into ``dir`` (the image's filesystem, without the parts no step
uses) and writes ``dir/sipath-bin/{openroad,sta,yosys,yosys-abc,klayout}``: wrappers
that run the image's binaries with the image's libraries. With ``dir/sipath-bin`` on
PATH, ``find_openroad()`` takes the native runner and the steps call the same
tool builds the Docker runs use; ``SIPATH_PLATFORMS`` points at the image's
platform tree (printed at the end).

The binaries are the image's Ubuntu 22.04 builds and use the host's C library,
so the host needs glibc 2.35 or newer (Ubuntu 22.04 and later, Colab).
Magic, Netgen and the precheck are other images: their checks stay optional.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import urllib.request

DEFAULT = os.path.expanduser("~/.cache/sipath/orfs-root")
INSTALL = "OpenROAD-flow-scripts/tools/install"
# the wrappers: name -> binary inside the image
TOOLS = {"openroad": f"{INSTALL}/OpenROAD/bin/openroad", "sta": f"{INSTALL}/OpenROAD/bin/sta",
         "yosys": f"{INSTALL}/yosys/bin/yosys", "yosys-abc": f"{INSTALL}/yosys/bin/yosys-abc",
         "klayout": "usr/bin/klayout"}
# parts of the image no step reads (ORFS's test designs, headers, docs)
SKIP = ("OpenROAD-flow-scripts/flow/designs/", "usr/include/", "usr/share/doc/", "usr/share/man/",
        "usr/share/locale/", "var/cache/", "var/lib/apt/", "dev/", "proc/", "sys/")
ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json"])
# library directories searched first (the image's ld.so.conf order); then every other directory of the
# image holding shared libraries, since binaries name some by absolute RUNPATH (/usr/lib/klayout, pulseaudio)
LIBS = ("opt/or-tools/lib", "usr/local/lib", "usr/local/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu",
        "usr/lib/x86_64-linux-gnu")
LIB_TREES = ("usr/lib", "usr/local/lib", "opt")


def _registry(ref: str) -> tuple[str, str, str]:
    """(registry host, repository, digest) of ``name@sha256:...`` on Docker Hub or another registry."""
    name, _, digest = ref.partition("@")
    if not digest.startswith("sha256:"):
        raise ValueError(f"{ref}: not pinned by digest")
    host, _, repo = name.partition("/")
    if "." not in host and ":" not in host:  # Docker Hub: openroad/orfs, or library/x
        host, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
    return host, repo, digest


def _get(url: str, token: str | None, accept: str | None = None, start: int = 0):
    req = urllib.request.Request(url, headers={**({"Authorization": f"Bearer {token}"} if token else {}),
                                               **({"Accept": accept} if accept else {}),
                                               **({"Range": f"bytes={start}-"} if start else {})})
    return urllib.request.urlopen(req, timeout=300)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return "sha256:" + h.hexdigest()


def _download(host, repo, layer, path, log, tries: int = 8) -> str:
    """One layer blob to ``path``, resuming a cut transfer; checked against its digest."""
    import http.client
    import time
    import urllib.error
    if os.path.isfile(path) and _sha256(path) == layer["digest"]:
        return path
    part = path + ".part"
    for attempt in range(1, tries + 1):
        start = os.path.getsize(part) if os.path.isfile(part) else 0
        try:
            token = _token(host, repo)  # registry tokens expire within minutes: one per try
            with _get(f"https://{host}/v2/{repo}/blobs/{layer['digest']}", token, start=start) as r:
                if start and r.status != 206:  # no range support here: start over
                    start = 0
                with open(part, "ab" if start else "wb") as f:
                    while b := r.read(1 << 20):
                        f.write(b)
            if os.path.getsize(part) >= layer.get("size", 0):
                break
            log(f"    cut at {os.path.getsize(part) / 1e6:.0f} MB, resuming")
        except (OSError, urllib.error.URLError, http.client.HTTPException) as e:
            log(f"    try {attempt}: {e}; resuming")
            time.sleep(min(30, 2 * attempt))
    if _sha256(part) != layer["digest"]:
        os.remove(part)
        raise RuntimeError(f"layer {layer['digest']}: downloaded data does not match its digest")
    os.replace(part, path)
    return path


def _token(host: str, repo: str) -> str | None:
    if host != "registry-1.docker.io":
        return None  # anonymous registries (ghcr.io needs a token too: not used by the lock)
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)["token"]


def _manifest(host, repo, digest, token, arch="amd64") -> dict:
    with _get(f"https://{host}/v2/{repo}/manifests/{digest}", token, ACCEPT) as r:
        body = r.read()
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise RuntimeError(f"{repo}@{digest}: the manifest does not match its digest")
    m = json.loads(body)
    if "manifests" in m:  # an index: the linux/<arch> image
        pick = [x for x in m["manifests"] if x.get("platform", {}).get("os") == "linux"
                and x.get("platform", {}).get("architecture") == arch]
        if not pick:
            raise RuntimeError(f"{repo}@{digest}: no linux/{arch} image")
        return _manifest(host, repo, pick[0]["digest"], token, arch)
    return m


def _skip(name: str) -> bool:
    n = name.lstrip("./")
    return any(n.startswith(s) or n + "/" == s for s in SKIP)


def _unpack(stream, root: str):
    """Extract one layer over ``root``, applying its whiteouts."""
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for m in tar:
            name = os.path.normpath(m.name.lstrip("./"))
            if name.startswith("..") or os.path.isabs(name) or _skip(name):
                continue
            base, leaf = os.path.split(name)
            if leaf == ".wh..wh..opq":  # opaque directory: drop what earlier layers put there
                d = os.path.join(root, base)
                for e in os.listdir(d) if os.path.isdir(d) else []:
                    p = os.path.join(d, e)
                    shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
                continue
            if leaf.startswith(".wh."):  # a whiteout: the file is deleted
                p = os.path.join(root, base, leaf[4:])
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                elif os.path.lexists(p):
                    os.remove(p)
                continue
            if m.ischr() or m.isblk() or m.isfifo():
                continue
            dest = os.path.join(root, name)
            if os.path.lexists(dest) and not (m.isdir() and os.path.isdir(dest)):
                shutil.rmtree(dest) if os.path.isdir(dest) and not os.path.islink(dest) else os.remove(dest)
            if m.islnk():  # hard links name the image path: link inside root
                target = os.path.join(root, os.path.normpath(m.linkname.lstrip("./")))
                if os.path.exists(target):
                    os.link(target, dest)
                continue
            if m.issym() and m.linkname.startswith("/"):  # absolute links point into root, not the host
                m.linkname = os.path.relpath(os.path.join(root, m.linkname.lstrip("/")), os.path.dirname(dest))
            m.name = name
            kw = {"filter": "tar"} if sys.version_info >= (3, 12) or hasattr(tarfile, "tar_filter") else {}
            tar.extract(m, root, set_attrs=not m.issym(), **kw)


def lib_dirs(root: str) -> list[str]:
    """The image's library directories (relative to ``root``): LIBS, then every directory
    under LIB_TREES holding a shared library (not Python packages' private copies)."""
    found = []
    for tree in LIB_TREES:
        for d, subdirs, files in os.walk(os.path.join(root, tree)):
            subdirs[:] = [s for s in subdirs if s not in ("site-packages", "dist-packages", "python3", "debug")
                          and not s.startswith("python3.")]
            if any(".so" in f for f in files):
                found.append(os.path.relpath(d, root))
    return list(LIBS) + sorted(x for x in found if x not in LIBS)


def _ruby_dirs(root: str) -> list[str]:
    """The image's Ruby library directories, versioned ones first (``pathname`` and the rest of stdlib)."""
    import glob
    out = []
    for base in ("usr/lib/ruby", "usr/lib/x86_64-linux-gnu/ruby", "usr/lib/ruby/vendor_ruby",
                 "usr/lib/x86_64-linux-gnu/ruby/vendor_ruby"):
        out += sorted(os.path.relpath(d, root) for d in glob.glob(os.path.join(root, base, "[0-9]*")))
    out += [d for d in ("usr/lib/ruby/vendor_ruby", "usr/lib/x86_64-linux-gnu/ruby/vendor_ruby")
            if os.path.isdir(os.path.join(root, d))]
    return out


def _wrapper(root: str, name: str, binary: str, libs: list[str] | None = None) -> str:
    libs = ":".join(f"$R/{d}" for d in (LIBS if libs is None else libs))
    extra = ""
    if name == "klayout":  # its embedded Python, Ruby (the DRC decks) and Qt from the image
        ruby = ":".join(f"$R/{d}" for d in _ruby_dirs(root))
        extra = ('export PYTHONHOME="$R/usr"\nexport QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"\n'
                 'export QT_PLUGIN_PATH="$R/usr/lib/x86_64-linux-gnu/qt5/plugins"\n'
                 f'export RUBYLIB="{ruby}"\nexport GEM_PATH="$R/usr/lib/ruby/gems/3.0.0:$R/usr/share/rubygems-integration/all"\n')
    if name == "openroad":
        extra = 'export QT_PLUGIN_PATH="$R/usr/lib/x86_64-linux-gnu/qt5/plugins"\n'
    return (f'#!/bin/sh\n# SiPath: {name} of the locked ORFS image, unpacked in {root}\nR="{root}"\n'
            f'export LD_LIBRARY_PATH="{libs}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"\n'
            f'export TCL_LIBRARY="$R/usr/share/tcltk/tcl8.6"\n{extra}exec "$R/{binary}" "$@"\n')


def glibc_ok() -> tuple[bool, str]:
    v = (os.confstr("CS_GNU_LIBC_VERSION") if hasattr(os, "confstr") else "") or ""
    try:
        ok = tuple(int(x) for x in v.split()[1].split(".")[:2]) >= (2, 35)
    except (IndexError, ValueError):
        ok = False
    return ok, v or "no glibc"


def install(root: str = DEFAULT, ref: str | None = None, force: bool = False,
            log=lambda s: print(s, flush=True)) -> str:
    """Unpack the locked ORFS image into ``root``; returns the wrappers' directory."""
    from ..toolchain import image
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise RuntimeError(f"install-tools needs Linux x86_64 (this is {sys.platform} {platform.machine()}); "
                           "use the Docker images instead")
    ok, libc = glibc_ok()
    if not ok:
        raise RuntimeError(f"the image's binaries need glibc 2.35 or newer (Ubuntu 22.04+); this host has {libc}")
    ref = ref or image("orfs")
    root = os.path.abspath(root)
    stamp = os.path.join(root, ".sipath-image")
    if os.path.isfile(stamp) and open(stamp).read().strip() == ref and not force:
        log(f"{root}: {ref} already installed")
    else:
        host, repo, digest = _registry(ref)
        layers = _manifest(host, repo, digest, _token(host, repo))["layers"]
        total = sum(x.get("size", 0) for x in layers)
        cache = root + ".layers"  # verified layers survive a failed install: a re-run fetches the rest
        os.makedirs(cache, exist_ok=True)
        log(f"{ref}: {len(layers)} layers, {total / 1e9:.2f} GB to download")
        files = []
        for i, layer in enumerate(layers, 1):
            log(f"  layer {i}/{len(layers)} {layer['digest'][7:19]} {layer.get('size', 0) / 1e6:.0f} MB")
            files.append(_download(host, repo, layer, os.path.join(cache, layer["digest"][7:]), log))
        if os.path.exists(root):
            shutil.rmtree(root)
        os.makedirs(root)
        log(f"unpacking into {root}")
        for path in files:  # every layer checked against its digest before any is unpacked
            with open(path, "rb") as f:
                _unpack(f, root)
        with open(stamp, "w") as f:
            f.write(ref + "\n")
        shutil.rmtree(cache)
    bindir = os.path.join(root, "sipath-bin")  # not root/bin: the image's /bin is a link to /usr/bin
    os.makedirs(bindir, exist_ok=True)
    libs = lib_dirs(root)
    for name, binary in TOOLS.items():
        if not os.path.exists(os.path.join(root, binary)):
            raise RuntimeError(f"{binary} not in {ref}")
        p = os.path.join(bindir, name)
        with open(p, "w") as f:
            f.write(_wrapper(root, name, binary, libs))
        os.chmod(p, 0o755)
    return bindir


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    root = os.path.abspath(args[0] if args else DEFAULT)
    try:
        bindir = install(root, force="--force" in argv)
    except (RuntimeError, ValueError, OSError, tarfile.TarError) as e:
        print(f"install-tools: {e}", file=sys.stderr)
        return 1
    print(f"tools in {bindir}: {', '.join(TOOLS)}\n"
          f'export PATH="{bindir}:$PATH"\n'
          f'export SIPATH_PLATFORMS="{os.path.join(root, "OpenROAD-flow-scripts", "flow", "platforms")}"')
    return 0
