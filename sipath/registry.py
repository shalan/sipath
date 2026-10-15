"""Extension points: steps, scores, signoff checks, harnesses, design templates
and design-file extensions, registered into the same tables the built-ins use.

A plugin is a Python module that registers what it adds when imported::

    from sipath import registry
    from sipath.params import Param

    @registry.step("fill", params=[Param("density", "float", 0.3, "target density", min=0, max=1)])
    def fill(ctx): ...

    @registry.score
    def area_then_wns(m): ...

    @registry.check("xor")
    def xor(ctx, summary):
        return {"pass": True}

    registry.register_extension("mypkg", {"layers": schema.Field("list[str]", "fill layers")})

It is loaded by a design file (``plugins: [mypkg.sipath_ext]``, imported
before the file is validated) or, for an installed package, through the
``sipath.plugins`` entry-point group. Registered steps get what built-in steps
have: typed parameters, ``describe_step``, the generated reference, variants,
scores and resume. A registered signoff check runs after the built-in checks;
it returns a dict with ``pass`` (and anything else to report) and fails
signoff when it is required (``require``) and does not pass.
"""
from __future__ import annotations

import importlib
from typing import Callable

from . import params as _params
from . import scores as _scores

CHECKS: dict[str, Callable] = {}  # signoff check name -> fn(ctx, summary) -> {"pass": bool, ...}
HARNESSES: dict[str, type] = {}  # harness name -> Harness subclass
EXTENSIONS: dict[str, dict] = {}  # design-file extensions namespace -> {key: schema.Field}
TEMPLATES: dict[str, str] = {}  # design template name -> its directory (sipath.templates)
_LOADED: set[str] = set()
_ENTRY_POINTS_DONE = False


class RegistryError(ValueError):
    pass


# ----------------------------------------------------------------------- steps
def register_step(name: str, fn: Callable, params=(), tool: bool | None = None, replace: bool = False):
    """Make ``fn(ctx)`` a step called ``name`` (design files, MCP, the CLI).
    ``params``: its sipath.params.Param declarations; ``tool``: run without
    OpenROAD (the step writes outputs["checkpoint"])."""
    from . import steps
    if name in steps.STEPS and not replace:
        raise RegistryError(f"step {name!r} is already registered")
    if tool is not None:
        fn.tool = bool(tool)
    steps.STEPS[name] = fn
    _params.STEP_PARAMS[name] = list(params)
    _params.DECLARED[name] = {p.name: p for p in params}
    return fn


def step(name: str | None = None, params=(), tool: bool | None = None):
    """Decorator form of register_step (the step name defaults to the function's)."""
    def wrap(fn):
        return register_step(name or fn.__name__, fn, params, tool)
    return wrap


# ---------------------------------------------------------------------- scores
def register_score(fn: Callable, name: str | None = None, replace: bool = False):
    """A named score: fn(metrics) -> comparable (higher is better) or None."""
    key = name or fn.__name__
    if key in _scores.SCORES and not replace:
        raise RegistryError(f"score {key!r} is already registered")
    _scores.SCORES[key] = fn
    return fn


def score(fn: Callable):
    return register_score(fn)


# ---------------------------------------------------------------------- checks
def _refresh_signoff_params():
    """The signoff ``checks`` / ``require`` parameters accept the registered checks."""
    allowed = tuple(_params.SIGNOFF_CHECKS) + tuple(_params.SIGNOFF_OPTIONAL) + tuple(CHECKS)
    new = []
    for p in _params.STEP_PARAMS["signoff"]:
        if p.name == "checks":
            p = _params.Param("checks", "list[str]", tuple(_params.SIGNOFF_CHECKS) + tuple(CHECKS), p.doc,
                              choices=allowed)
        elif p.name == "require":
            req = (tuple(_params.SIGNOFF_REQUIRED) + tuple(_params.SIGNOFF_OPTIONAL)
                   + tuple(k for k, f in CHECKS.items() if getattr(f, "required", False)))
            p = _params.Param("require", "list[str]", req, p.doc, choices=allowed)
        new.append(p)
    _params.STEP_PARAMS["signoff"] = new
    _params.DECLARED["signoff"] = {p.name: p for p in new}


def register_check(name: str, fn: Callable, required: bool = False, replace: bool = False):
    """A signoff check run after the built-in ones (by default with them all;
    ``required``: its failure fails signoff unless ``require`` says otherwise)."""
    if (name in CHECKS or name in _params.SIGNOFF_CHECKS + _params.SIGNOFF_OPTIONAL) and not replace:
        raise RegistryError(f"signoff check {name!r} is already registered")
    fn.required = bool(required)
    CHECKS[name] = fn
    _refresh_signoff_params()
    return fn


def check(name: str, required: bool = False):
    def wrap(fn):
        return register_check(name, fn, required)
    return wrap


# ------------------------------------------------------------------- harnesses
def register_harness(name: str, cls: type, replace: bool = False):
    """A harness (frame) a design can target with ``harness: {name: ...}``."""
    if name in HARNESSES and not replace:
        raise RegistryError(f"harness {name!r} is already registered")
    cls.name = name
    HARNESSES[name] = cls
    return cls


def harness(name: str):
    def wrap(cls):
        return register_harness(name, cls)
    return wrap


# ------------------------------------------------------------------- templates
def register_template(name: str, directory: str, replace: bool = False):
    """A design template (a directory with template.yaml, see sipath.templates)."""
    import os
    if name in TEMPLATES and not replace:
        raise RegistryError(f"template {name!r} is already registered")
    if not os.path.isfile(os.path.join(directory, "template.yaml")):
        raise RegistryError(f"template {name!r}: no template.yaml in {directory}")
    TEMPLATES[name] = directory


# ------------------------------------------------------------------ extensions
def register_extension(namespace: str, fields: dict, replace: bool = False):
    """Keys a plugin reads from the design file's ``extensions: {namespace: {...}}``,
    as sipath.schema.Field declarations (checked like the design file's own keys)."""
    if namespace in EXTENSIONS and not replace:
        raise RegistryError(f"extension namespace {namespace!r} is already registered")
    EXTENSIONS[namespace] = dict(fields)


# --------------------------------------------------------------------- loading
def load_plugins(names) -> list[str]:
    """Import plugin modules (each once); entry points are loaded first."""
    load_entry_points()
    done = []
    for n in names or []:
        if n in _LOADED:
            continue
        try:
            importlib.import_module(n)
        except ImportError as e:
            raise RegistryError(f"plugin {n!r} could not be imported: {e}") from e
        _LOADED.add(n)
        done.append(n)
    return done


def load_entry_points() -> list[str]:
    """Plugins installed as packages: the ``sipath.plugins`` entry-point group
    (a module, or a callable called with no arguments). Loaded once."""
    global _ENTRY_POINTS_DONE
    if _ENTRY_POINTS_DONE:
        return []
    _ENTRY_POINTS_DONE = True
    from importlib.metadata import entry_points
    names = []
    for ep in entry_points(group="sipath.plugins"):
        obj = ep.load()
        if callable(obj) and not isinstance(obj, type):
            obj()
        names.append(ep.name)
    return names


def loaded() -> dict:
    """What plugins added (for provenance and the MCP server)."""
    from . import steps
    builtin = {"synth", "floorplan", "macros", "physical", "place", "cts", "route_analog", "route_digital",
               "route_repair", "fill", "extract", "signoff", "views", "integrate"}
    return {"plugins": sorted(_LOADED), "steps": sorted(set(steps.STEPS) - builtin),
            "checks": sorted(CHECKS), "harnesses": sorted(HARNESSES), "extensions": sorted(EXTENSIONS),
            "templates": sorted(TEMPLATES)}
