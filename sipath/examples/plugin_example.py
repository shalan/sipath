"""Example SiPath plugin: one of each extension point.

Load it from a design file::

    plugins: [sipath.examples.plugin_example]
    extensions:
      example: {max_cells: 5000}
    flow:
    - ...
    - step: cell_census
      variants: [{prefix: ""}]
    - step: signoff            # cell_budget runs with the built-in checks

or list it with ``python3 -m sipath plugins sipath.examples.plugin_example``.
"""
from sipath import registry, schema
from sipath.params import Param

registry.register_extension("example", {
    "max_cells": schema.Field("int", "cell_budget: fail signoff above this many instances"),
})


@registry.step("cell_census", params=[Param("prefix", "str", "", "count only instances whose name starts with this")],
               requires={"netlist"}, provides=())  # counts the linked netlist's instances; adds nothing
def cell_census(ctx):
    """Count the design's instances (optionally under a hierarchy prefix)."""
    prefix = ctx.params.get("prefix") or ""
    n = int(ctx.session.value(
        "set n 0; foreach i [[ord::get_db_block] getInsts] { "
        f"if {{[string first {{{prefix}}} [$i getName]] == 0}} {{ incr n }} }}; set n"))
    ctx.metrics["census_cells"] = n


@registry.score
def fewest_cells(m):
    """Fewest instances counted by cell_census."""
    return None if m.get("census_cells") is None else -m["census_cells"]


@registry.check("cell_budget")
def cell_budget(ctx, summary):
    """At most extensions.example.max_cells instances (skipped without a limit)."""
    limit = (ctx.design.extensions.get("example") or {}).get("max_cells")
    if limit is None:
        return {"pass": True, "skipped": "no extensions.example.max_cells"}
    n = int(ctx.session.value("llength [[ord::get_db_block] getInsts]"))
    return {"pass": n <= limit, "cells": n, "limit": limit, "reason": f"{n} instances, budget {limit}"}
