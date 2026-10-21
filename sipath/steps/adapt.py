"""Adaptive rounds: what a step tries next from what its selected attempt measured.

``adapt_place`` reads the selected placement's heatmaps (placement density and
RUDY by default) and proposes two attempts: the same parameters with partial
placement blockages over the hotspot boxes (a local answer, ``max_density``),
and the same parameters with a lower global ``density`` (a global answer; only
while it stays above the cells' utilization, which global placement cannot
spread below).
Each proposal says which hotspots it answers. Registered in ``registry.ADAPTERS``.
"""
from __future__ import annotations

from .. import heatmap, params as params_mod

DEFAULTS = {"maps": ["placement", "rudy"], "max_density": 70, "density_step": 0.05, "min_density": 0.3,
            "max_blockages": 6}


def _options(fn, opts):
    fn.options = opts
    return fn


def _covered(box, boxes, share: float = 0.5) -> bool:
    """``box`` mostly inside one of ``boxes`` (overlap above ``share`` of its area)."""
    area = (box[2] - box[0]) * (box[3] - box[1]) or 1.0
    return any(heatmap.overlap(box, b) / area > share for b in boxes)


def adapt_place(flow, attempt, cfg: dict) -> list[tuple[dict, str]]:
    cfg = {**DEFAULTS, **(cfg or {})}
    maps = (attempt.outputs or {}).get("heatmaps") or {}
    spots = [(m, h) for m in cfg["maps"] for h in (maps.get(m) or {}).get("hotspots") or []]
    if not spots:
        return []
    p = dict(attempt.params)
    out = []
    have = [b["box"] for b in p.get("blockages") or []]
    new = []
    for m, h in sorted(spots, key=lambda s: -s[1]["peak"]):
        if not _covered(h["box"], have + [b for b, _ in new]):
            new.append((h["box"], f"{m} {h['peak']:.3g} at {h['box']}"))
    room = max(0, cfg["max_blockages"] - len(have))
    if new and room:
        new = new[:room]
        blk = list(p.get("blockages") or []) + [{"box": b, "max_density": cfg["max_density"]} for b, _ in new]
        out.append(({**p, "blockages": blk},
                    f"blockages at {cfg['max_density']}% over hotspots: " + "; ".join(why for _, why in new)))
    density = params_mod.resolve("place", p, flow.pdk, flow.design).get("density")
    lower = round(density - cfg["density_step"], 3) if density is not None else None
    util = (attempt.metrics or {}).get("std_util")  # global placement cannot spread below the cells' utilization
    if lower is not None and lower >= cfg["min_density"] and (util is None or lower > util + 0.01):
        worst = max(spots, key=lambda s: s[1]["peak"])
        out.append(({**p, "density": lower},
                    f"density {density:g} -> {lower:g} for {len(spots)} hotspot(s), worst {worst[0]} "
                    f"{worst[1]['peak']:.3g}"))
    return out


_options(adapt_place, DEFAULTS)
