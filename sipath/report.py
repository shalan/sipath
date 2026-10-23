"""Run report: one page per run, from its attempt graph (P2.4).

``write(workdir)`` writes ``report.md`` and ``report.html`` (self-contained,
relative links) next to ``graph.json``: the design and provenance, every
step with its attempts (status, parameters, seconds, the selected one's key
metrics), failures with their errors, the signoff summary (each check and
its result), links to every report and output file the attempts list, and
layout pictures found in the run (``*.png``). The scheduler rewrites it after
every step, so a run that stops early still has its page.

    python3 -m sipath report <workdir> [--render]

``--render`` first draws the selected final checkpoint (OpenROAD
``save_image``) into ``<workdir>/layout.png``.
"""
from __future__ import annotations

import glob
import html
import json
import os

from . import heatmap

KEY_METRICS = ("synth_cells", "synth_area_um2", "equiv", "insts", "utilization", "hpwl_um", "wns", "tns", "whs",
               "drc", "wirelength_um", "vias", "antenna_violations", "fill_shapes", "drc_signoff",
               "density_violations", "lvs_match", "signoff_pass", "magic_drc_violations", "xor_violations",
               "chip_check_pass", "view_corners", "ppa_fmax_mhz", "ppa_power_w")


def _rel(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def collect(workdir: str) -> dict:
    with open(os.path.join(workdir, "graph.json")) as f:
        g = json.load(f)
    attempts = g.get("attempts", [])
    by_step: dict[str, list] = {}
    for a in attempts:
        by_step.setdefault(a["step"], []).append(a)
    from .scheduler import run_chain
    chain = run_chain(g)  # the run's result: its head and ancestry (a branch's selection is not the result)
    on_chain = {a["id"] for a in chain}
    steps = []
    for name, info in (g.get("steps") or {}).items():
        atts = [a for a in by_step.get(name, []) if a.get("status") != "stale"]  # stale: listed apart
        sel = next((a for a in atts if a["id"] == info.get("selected")), None)
        steps.append({"name": name, "selected": info.get("selected"), "attempts": atts, "sel": sel,
                      "on_chain": bool(sel and sel["id"] in on_chain)})
    signoff = None
    for a in reversed(chain):
        p = (a.get("outputs") or {}).get("signoff")
        if p:
            if os.path.isfile(p):
                with open(p) as f:
                    signoff = {"path": p, "summary": json.load(f)}
            break
    files = []
    for s in steps:
        for a in s["attempts"]:
            out = a.get("outputs") or {}
            for k, v in (out.get("reports") or {}).items():
                if isinstance(v, str):
                    files.append((a["id"], k, v))
            for k in ("def_", "spef", "gds", "verilog", "chip_gds", "netlist", "signoff"):
                if isinstance(out.get(k), str):
                    files.append((a["id"], k, out[k]))
    heatmaps = []  # the selected attempts' heatmaps, drawn next to their CSVs
    for s in steps:
        for name, e in ((s["sel"] or {}).get("outputs", {}).get("heatmaps") or {}).items():
            if "csv" not in e or not os.path.isfile(e["csv"]):
                continue
            svg = os.path.splitext(e["csv"])[0] + ".svg"
            if not os.path.isfile(svg) or os.path.getmtime(svg) < os.path.getmtime(e["csv"]):
                unit, bins = heatmap.read(e["csv"])
                with open(svg, "w") as f:
                    f.write(heatmap.svg(name, unit, bins))
            heatmaps.append({"step": s["name"], "attempt": s["selected"], "map": name, "svg": svg, **e})
    ppa = None  # the result's latest PPA first cut (explore mode, or a ppa step)
    for a in reversed(chain):
        if isinstance((a.get("outputs") or {}).get("ppa"), dict):
            ppa = {"step": a["step"], **a["outputs"]["ppa"]}
            break
    pictures = sorted(glob.glob(os.path.join(workdir, "*.png")) + glob.glob(os.path.join(workdir, "*", "*", "*.png")))
    order = [a["id"] for a in g.get("attempts", [])]  # sipath render: _pictures/<attempt>.png, in flow order
    pictures += sorted(glob.glob(os.path.join(workdir, "_pictures", "*.png")), key=lambda p: (
        order.index(os.path.basename(p)[:-4]) if os.path.basename(p)[:-4] in order else len(order), p))
    return {"design": g.get("design", {}), "pdk": g.get("pdk"), "provenance": g.get("provenance", {}),
            "steps": steps, "signoff": signoff, "ppa": ppa, "files": files, "pictures": pictures,
            "heatmaps": heatmaps, "stale": g.get("stale") or [], "verification": g.get("verification"),
            "head": chain[-1]["id"] if chain else None, "decisions": g.get("decisions") or []}


def markdown(workdir: str, r: dict) -> str:
    d = r["design"]
    name = d.get("name") if isinstance(d, dict) else d
    out = [f"# Run report: {name}", ""]
    prov = r["provenance"]
    out.append(f"PDK `{r['pdk']}`; runner {prov.get('kind', '?')} {prov.get('image', '')}; workdir `{workdir}`")
    out.append("")
    v = r.get("verification")
    if v:  # first: what the run established, apart from whether its steps finished
        out += [f"## Verification: {v['verification'].upper()} (execution {v['execution']})", ""]
        if v["checks"]:
            out += ["| check | status | required | attempt | detail |", "|---|---|---|---|---|"]
            out += [f"| {c} | {x['status']} | {'yes' if x['required'] else 'no'} | {x['attempt']} | "
                    f"{(x['detail'] or '').replace('|', '/')[:160]} |" for c, x in v["checks"].items()]
            out.append("")
        if v["omitted"]:
            out += ["Not checked: " + "; ".join(v["omitted"]) + ".", ""]
        if v["notes"]:
            out += ["Limits of what was checked:", ""] + [f"- {n}" for n in v["notes"]] + [""]
    if r.get("head"):
        out += [f"Result: `{r['head']}` and its ancestry"
                + "".join(f"; bounded search decided at {d['at']} by {d['score']}: {d['winner']} of "
                          f"{len(d['candidates'])}" for d in r.get("decisions") or []) + ".", ""]
    out += ["## Steps", "", "| step | attempts | selected | seconds | key metrics |", "|---|---|---|---|---|"]
    for s in r["steps"]:
        sel = s["sel"]
        ok = sum(1 for a in s["attempts"] if a.get("status") in ("selected", "ok", "rejected"))
        mets = sel.get("metrics", {}) if sel else {}
        key = ", ".join(f"{k} {_fmt(mets[k])}" for k in KEY_METRICS if k in mets)
        mark = "" if not s["selected"] or s["on_chain"] else " (branch)"
        out.append(f"| {s['name']} | {ok}/{len(s['attempts'])} | {s['selected'] or 'none'}{mark} | "
                   f"{_fmt(sel.get('seconds')) if sel else '-'} | {key or '-'} |")
    if r.get("stale"):
        out += ["", "## Ran again on resume", "", "Recorded results not reused; their attempts are kept as `stale`.", ""]
        out += [f"- {s['step']}: {s['reason']}" for s in r["stale"]]
    fails = [a for s in r["steps"] for a in s["attempts"] if a.get("error")]
    if fails:
        out += ["", "## Failures", ""]
        for a in fails:
            err = (a["error"] or "").strip().splitlines()
            out.append(f"- **{a['id']}** ({a.get('status')}), params {a.get('params') or {}}: "
                       f"{' / '.join(err[:3])[:500]}")
    pp = r.get("ppa")
    if pp:
        ar, tm, pw = pp["area"], pp["timing"], pp.get("power") or {}
        out += ["", f"## PPA (first cut from {pp['step']}: {pp['parasitics']} parasitics, corner {pp['corner']})", "",
                "| quantity | value |", "|---|---|",
                f"| cell area | {ar['std_cell_um2']} um2 ({ar['cells']} cells), utilization {ar['utilization']} |",
                f"| WNS / TNS / WHS | {_fmt(tm['wns_ns'])} / {_fmt(tm['tns_ns'])} / {_fmt(tm['whs_ns'])} ns |"]
        for c in tm.get("clocks", []):
            out.append(f"| clock {c['clock']} | period {c['period_ns']} ns, achievable {c['achievable_period_ns']} ns "
                       f"(fmax {c['fmax_mhz']} MHz), suggested period {c['suggested_period_ns']} ns |")
        if pw:
            out.append(f"| power | {pw['total_w']:.3e} W (leakage {pw['leakage_w']:.3e}) |")
        if pp.get("wirelength_um"):
            out.append(f"| wirelength ({pp['wirelength_kind']}) | {round(pp['wirelength_um'])} um |")
    so = r["signoff"]
    if so:
        s = so["summary"]
        out += ["", f"## Signoff: {'PASS' if s.get('pass') else 'FAIL'}", "",
                "| check | result |", "|---|---|"]
        for c in s.get("checks", []):
            v = s.get(c, {}) if isinstance(s.get(c), dict) else {}
            if c in ("setup", "hold", "drv", "annotation", "unconstrained"):
                v = s.get("timing", {})
                res = {"setup": lambda: min((x["wns"] for x in v.get("corners", {}).values() if x["wns"] is not None),
                                            default=None),
                       "hold": lambda: min((x["whs"] for x in v.get("corners", {}).values() if x["whs"] is not None),
                                           default=None),
                       "drv": lambda: "clean" if v.get("drv_clean") else f"slew {v.get('drv_slew')} cap {v.get('drv_cap')}",
                       "annotation": lambda: len(v.get("unannotated_loaded", [])),
                       "unconstrained": lambda: len(v.get("unconstrained", []))}[c]()
                res = f"worst {res:.3f} ns" if isinstance(res, float) else res
            elif "match" in v:
                res = "match" if v["match"] else f"no match ({v.get('reason') or v.get('errors')})"
            elif "count" in v:
                res = f"{v['count']} {v.get('by_rule') or ''}".strip()
            elif "violations" in v:
                res = v["violations"]
            elif v.get("skipped"):
                res = f"skipped: {v['skipped']}"
            elif "pass" in v:
                res = "pass" if v["pass"] else v.get("reason", "fail")
            else:
                res = "-"
            mark = "required" if c in s.get("required", []) else "reported"
            out.append(f"| {c} ({mark}) | {res} |")
        for f in s.get("failures", []):
            out.append(f"\n- {f[:400]}")
    if r.get("heatmaps"):
        out += ["", "## Heatmaps", "", "Selected attempts; each map scaled to its own peak (blue low, red high).",
                "", "| attempt | map | unit | peak | p95 | hot share (threshold) | top hotspot |", "|---|---|---|---|---|---|---|"]
        for h in r["heatmaps"]:
            top = h["hotspots"][0] if h.get("hotspots") else None
            where = ""
            if top:
                objs = top.get("instances") or top.get("nets") or {}
                where = f"[{', '.join(f'{v:g}' for v in top['box'])}] {_fmt(top['peak'])}"
                if objs:
                    where += f": {objs['count']} {'nets' if 'nets' in top else 'instances'}, e.g. {', '.join(objs['top'][:3])}"
            th = "" if h.get("hot_threshold") is None else f" (>= {_fmt(h['hot_threshold'])})"
            out.append(f"| {h['attempt']} | {h['map']} | {h['unit']} | {_fmt(h['peak'])} | {_fmt(h['p95'])} | "
                       f"{h['hot_share']:.1%}{th} | {where} |")
        out.append("")
        for h in r["heatmaps"]:
            out.append(f"![{h['attempt']} {h['map']}]({_rel(h['svg'], workdir)})")
    if r["pictures"]:
        out += ["", "## Pictures", ""] + [f"![{os.path.basename(p)}]({_rel(p, workdir)})" for p in r["pictures"]]
    if r["files"]:
        out += ["", "## Files", ""] + [f"- {aid} {k}: [{os.path.basename(p)}]({_rel(p, workdir)})"
                                       for aid, k, p in r["files"]]
    return "\n".join(out) + "\n"


def to_html(md: str, title: str) -> str:
    """Small Markdown subset (headings, tables, lists, links, images, bold,
    code) to a standalone page."""
    import re
    body, in_table = [], False
    def inline(s):
        s = html.escape(s)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r'<img alt="\1" src="\2" style="max-width:100%">', s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    for ln in md.splitlines():
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if all(set(c) <= {"-"} for c in cells):
                continue
            if not in_table:
                body.append("<table>")
                in_table = True
                body.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in cells) + "</tr>")
            else:
                body.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if ln.startswith("## "):
            body.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            body.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("- "):
            body.append(f"<li>{inline(ln[2:])}</li>")
        elif ln.strip():
            body.append(f"<p>{inline(ln)}</p>")
    if in_table:
        body.append("</table>")
    style = ("body{font-family:system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 16px}"
             "table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #ccc;padding:4px 8px;"
             "text-align:left;vertical-align:top}code{background:#f3f3f3;padding:0 3px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{style}</style></head><body>\n" + "\n".join(body) + "\n</body></html>\n")


# METRICS2.1 names (as ORFS and LibreLane write them): SiPath step -> stage, SiPath metric -> category__metric
STAGES = {"synth": "synth", "floorplan": "floorplan", "macros": "floorplan", "physical": "floorplan",
          "place": "detailedplace", "cts": "cts", "route_analog": "globalroute", "route_digital": "detailedroute",
          "route_repair": "detailedroute", "fill": "finish", "extract": "finish", "signoff": "finish"}
NAMES = {"insts": "design__instance__count", "std_util": "design__instance__utilization",
         "synth_cells": "design__instance__count", "synth_area_um2": "design__instance__area",
         "wns": "timing__setup__ws", "tns": "timing__setup__tns", "whs": "timing__hold__ws",
         "synth_wns_ns": "timing__setup__ws", "synth_tns_ns": "timing__setup__tns",
         "wirelength_um": "route__wirelength", "hpwl_um": "route__wirelength__estimated",
         "drc": "route__drc_errors", "antenna_violations": "antenna__violating__nets",
         "grt_overflow": "global_route__overflow",
         "drv_slew": "design__max_slew_violation__count", "drv_cap": "design__max_cap_violation__count",
         "drv_fanout": "design__max_fanout_violation__count",
         "power_total_w": "power__total", "power_internal_w": "power__internal__total",
         "power_switching_w": "power__switching__total", "power_leakage_w": "power__leakage__total",
         "ir_worst_mV": "ir__drop__worst", "mem_peak_mb": "mem__peak",
         "drc_signoff": "klayout__drc_error__count", "antenna_signoff": "antenna__violating__nets__signoff"}


def metrics2(g: dict) -> dict:
    """The selected chain's metrics in METRICS2.1 names: ``<stage>__<category>__<metric>`` for every step
    (ORFS's stages; a later step of the same stage wins), then the last value of each without the stage prefix
    (LibreLane's final metrics; synthesis's estimates only with their prefix). Per-corner setup slack as ``timing__setup__ws__corner:<corner>``, runtime as
    ``runtime__total`` (seconds), LVS as ``design__lvs_error__count``, heatmaps as ``heatmap__<map>__<stat>``."""
    from .scheduler import run_chain
    out, final = {"flow__name": "sipath", "design__name": g.get("design")}, {}
    for a in run_chain(g):  # the result's chain: a branch's selections are not the run's metrics
        name = a["step"].split("@")[0]
        stage = STAGES.get(name, name)
        vals = {}
        for k, v in (a.get("metrics") or {}).items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                if k == "lvs_match" and isinstance(v, bool):
                    vals["design__lvs_error__count"] = 0 if v else 1
                continue
            if k in NAMES:
                vals[NAMES[k]] = v
            elif k.startswith(("wns_", "sta_wns_")):
                vals[f"timing__setup__ws__corner:{k.split('wns_', 1)[1]}"] = v
            elif k.startswith(("whs_", "sta_whs_")):
                vals[f"timing__hold__ws__corner:{k.split('whs_', 1)[1]}"] = v
            elif k.startswith("power_total_w_"):
                vals[f"power__total__corner:{k[len('power_total_w_'):]}"] = v
            elif k.startswith("heatmap_"):
                mp, _, stat = k[len("heatmap_"):].rpartition("_")
                if mp.endswith("_hot"):
                    mp, stat = mp[:-4], "hot_share"
                vals[f"heatmap__{mp}__{stat}"] = v
        if a.get("seconds") is not None:
            vals["runtime__total"] = a["seconds"]
        out.update({f"{stage}__{k}": v for k, v in vals.items()})
        if stage != "synth":  # final values describe the layout, not synthesis's estimates
            final.update(vals)
        sta = {k: v for k, v in (a.get("metrics") or {}).items() if k.startswith(("sta_wns_", "sta_whs_"))
               and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if sta:  # signoff's STA (extracted parasitics, every corner) is the final slack, not the head's estimate
            for kind, key in (("setup", "sta_wns_"), ("hold", "sta_whs_")):
                vs = [v for k, v in sta.items() if k.startswith(key)]
                if vs:
                    final[f"timing__{kind}__ws"] = min(vs)
    out.update(final)
    v = g.get("verification")
    if v:  # SiPath's own: METRICS2.1 has no verdict
        out["flow__execution__status"] = v["execution"]
        out["flow__verification__status"] = v["verification"]
        for st in ("failed", "inconclusive", "skipped"):
            out[f"flow__verification__{st}__count"] = sum(1 for x in v["checks"].values() if x["status"] == st)
        out["flow__verification__omitted__count"] = len(v["omitted"])
    return out


def write_metrics(workdir: str, g: dict) -> tuple[str, str]:
    """``metrics.json`` and ``metrics.csv`` (metric,value) beside graph.json."""
    import csv
    m = metrics2(g)
    jp, cp = os.path.join(workdir, "metrics.json"), os.path.join(workdir, "metrics.csv")
    with open(jp, "w") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    with open(cp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerows(sorted(m.items()))
    return jp, cp


def write(workdir: str) -> tuple[str, str]:
    r = collect(workdir)
    md = markdown(workdir, r)
    d = r["design"]
    title = f"Run report: {d.get('name') if isinstance(d, dict) else d}"
    mp, hp = os.path.join(workdir, "report.md"), os.path.join(workdir, "report.html")
    with open(mp, "w") as f:
        f.write(md)
    with open(hp, "w") as f:
        f.write(to_html(md, title))
    with open(os.path.join(workdir, "graph.json")) as f:
        write_metrics(workdir, json.load(f))
    return mp, hp
