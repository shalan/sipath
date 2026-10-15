"""Design templates: a starter project for a target (a harness's frame, a
die, a macro) that signs off as generated.

A template is a directory with ``template.yaml`` (name, description, the
harness it targets, typed ``params``, the ``files`` to write: output path ->
source file, both with ``{{param}}`` placeholders, and optionally ``derive``:
a function registered here that checks the values and adds derived ones) and
the source files. Built-in templates live in ``sipath/templates/<name>``; a
plugin registers more with ``registry.register_template(name, directory)``.

    python3 -m sipath templates
    python3 -m sipath new <dir> --template openframe --set inputs=8 --set outputs=6

Rendering writes a ready project: the design file, the RTL, the SDC; ``run``
it as any design file.
"""
from __future__ import annotations

import os
import re

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TYPES = {"str": str, "int": int, "float": float, "bool": lambda v: v if isinstance(v, bool) else str(v).lower() in
         ("1", "true", "yes", "on")}
DERIVE = {}  # name -> fn(values) -> extra values; raises ValueError on a bad combination


def derive(name):
    def wrap(fn):
        DERIVE[name] = fn
        return fn
    return wrap


@derive("openframe_pads")
def _openframe_pads(v: dict) -> dict:
    """Pad 0 clock, then the core's inputs, then its outputs, all on the east
    edge (pads 0-14), next to the template's row strip."""
    n, m = v["inputs"], v["outputs"]
    if 1 + n + m > 15:
        raise ValueError(f"1 clock + {n} inputs + {m} outputs = {1 + n + m} pads: the east edge has 15 (0-14)")
    return {"in_hi": n, "out_lo": n + 1, "out_hi": n + m, "inputs_m1": n - 1, "inputs_w": n - 1,
            "outputs_m1": m - 1, "in_bits": f"gpio_in[{n}:1]"}


class Template:
    def __init__(self, root: str):
        from .pdk import read_file
        self.root = root
        self.spec = read_file(os.path.join(root, "template.yaml"))
        self.name = self.spec["name"]

    def values(self, given: dict | None = None) -> dict:
        """Typed parameter values: the given ones (strings from the command
        line are converted) over the defaults; unknown names and values out of
        range are errors; then the template's derived values."""
        given = dict(given or {})
        decl = self.spec.get("params") or {}
        unknown = sorted(set(given) - set(decl))
        if unknown:
            raise ValueError(f"template {self.name}: unknown parameter(s) {unknown}; known: {sorted(decl)}")
        out = {}
        for k, p in decl.items():
            v = given.get(k, p.get("default"))
            if v is None:
                raise ValueError(f"template {self.name}: {k} is required ({p.get('doc', '')})")
            try:
                v = TYPES[p.get("type", "str")](v)
            except (TypeError, ValueError):
                raise ValueError(f"template {self.name}: {k}={v!r} is not a {p.get('type', 'str')}") from None
            if "min" in p and v < p["min"]:
                raise ValueError(f"template {self.name}: {k}={v} is below {p['min']}")
            if "max" in p and v > p["max"]:
                raise ValueError(f"template {self.name}: {k}={v} is above {p['max']}")
            if p.get("choices") and v not in p["choices"]:
                raise ValueError(f"template {self.name}: {k}={v!r} is not one of {p['choices']}")
            out[k] = v
        if self.spec.get("derive"):
            if self.spec["derive"] not in DERIVE:
                raise ValueError(f"template {self.name}: no derive function {self.spec['derive']!r}")
            out.update(DERIVE[self.spec["derive"]](out))
        return out

    @staticmethod
    def fill(text: str, values: dict) -> str:
        def sub(m):
            if m.group(1) not in values:
                raise KeyError(f"no value for {{{{{m.group(1)}}}}}")
            return str(values[m.group(1)])
        return re.sub(r"\{\{(\w+)\}\}", sub, text)

    def render(self, outdir: str, given: dict | None = None, force: bool = False) -> list[str]:
        """Write the project into ``outdir``; refuses to overwrite files unless ``force``."""
        v = self.values(given)
        plan = {os.path.join(outdir, self.fill(dst, v)): os.path.join(self.root, src)
                for dst, src in (self.spec.get("files") or {}).items()}
        clash = sorted(p for p in plan if os.path.exists(p))
        if clash and not force:
            raise FileExistsError(f"would overwrite {clash[:5]} (use --force)")
        for dst, src in plan.items():
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            with open(src) as f:
                text = self.fill(f.read(), v)
            with open(dst, "w") as f:
                f.write(text)
        return sorted(plan)


def load_builtin() -> list[str]:
    """Register the templates shipped in sipath/templates (once)."""
    from . import registry
    names = []
    for d in sorted(os.listdir(HERE)) if os.path.isdir(HERE) else []:
        if os.path.isfile(os.path.join(HERE, d, "template.yaml")) and d not in registry.TEMPLATES:
            registry.register_template(d, os.path.join(HERE, d))
            names.append(d)
    return names


def get(name: str) -> Template:
    from . import registry
    load_builtin()
    if name not in registry.TEMPLATES:
        raise ValueError(f"unknown template {name!r}; known: {sorted(registry.TEMPLATES)}")
    return Template(registry.TEMPLATES[name])


def listing() -> list[dict]:
    from . import registry
    load_builtin()
    out = []
    for n in sorted(registry.TEMPLATES):
        t = Template(registry.TEMPLATES[n])
        out.append({"name": n, "harness": t.spec.get("harness"), "description": " ".join(
            str(t.spec.get("description", "")).split()), "params": {k: p.get("default") for k, p in
                                                                   (t.spec.get("params") or {}).items()}})
    return out
