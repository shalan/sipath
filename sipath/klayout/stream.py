# KLayout batch script (klayout -zz -rd cfg_file=<json> -r stream.py): routed DEF -> GDS.
#
# Reads the DEF with the PDK's KLayout technology and LEF/DEF layer map, keeps
# DEF net names on shapes, labels each supply net once (text on its largest
# shape on the highest labelled layer, so LVS can name the supply nets), then
# replaces every cell by its layout from the given GDS files and writes the
# top cell's tree. Cells named in "abstract" keep their LEF view (pins on
# drawing layers plus pin-name labels): LVS black boxes. Cells no GDS defines
# and orphan cells are reported in <out>.json; sipath.signoff fails on them.
# Follows OpenROAD-flow-scripts flow/util/def2stream.py (BSD 3-Clause).
import json
import os

import pya

cfg = json.load(open(cfg_file))  # noqa: F821  (cfg_file: KLayout -rd variable)
tech = pya.Technology()
tech.load(cfg["tech"])
opt = tech.load_layout_options
opt.lefdef_config.map_file = cfg["map"]
opt.lefdef_config.lef_files = cfg["lefs"]
opt.lefdef_config.read_lef_with_def = False
opt.lefdef_config.net_property_name = "NET"
# LEF geometry for every macro, FOREIGN or not: the cells are named after the
# LEF macros (the GDS replaces them; black boxes keep the LEF view)
opt.lefdef_config.macro_resolution_mode = 1

ly = pya.Layout()
ly.read(cfg["def"], opt)
top = ly.cell(cfg["top"])
if top is None:
    raise RuntimeError(f"top cell {cfg['top']} not in {cfg['def']}")

labels = {}
order = list(cfg["label_layers"])
for net in cfg["supplies"]:
    best = None
    for rank, lname in enumerate(order):
        dl, dd, tl, td = cfg["label_layers"][lname]
        li = ly.find_layer(dl, dd)
        if li is None:
            continue
        for sh in top.shapes(li).each():
            if sh.property("NET") != net or not (sh.is_box() or sh.is_polygon() or sh.is_path()):
                continue
            b = sh.bbox()
            key = (rank, b.width() * b.height())
            if best is None or key > best[0]:
                best = (key, b, tl, td, lname)
    if best:
        _, b, tl, td, lname = best
        top.shapes(ly.layer(tl, td)).insert(pya.Text(net, pya.Trans(b.center().x, b.center().y)))
        labels[net] = lname

abstract = set(cfg.get("abstract", []))
for c in ly.each_cell():
    if (c.cell_index() != top.cell_index() and not c.name.startswith("VIA_") and not c.name.endswith("_DEF_FILL")
            and c.name not in abstract):
        c.clear()
        c.ghost_cell = True  # a placeholder: the library or macro GDS fills it


def defined():
    return {c.name for c in ly.each_cell() if not c.is_ghost_cell()}


# owner of every cell: the GDS file that defined it ("def": the DEF/LEF read)
owners = {n: "def" for n in defined()}
# Black boxes keep their LEF view; the shapes of one LEF pin are separate
# pieces there (LEF connects them inside the macro). Each pin with several
# shapes gets its own datatype k: a copy of each shape on JOIN_BASE + i (i:
# the metal's index in label_layers) and its bounding box on JOIN_HULL. The
# LVS deck connects JOIN_BASE+i/k to metal i and to JOIN_HULL/k only, so the
# pieces become one net (one pin) and nothing else can touch them.
JOIN_BASE, JOIN_HULL = 1000, 1100
joins = 0
metals = list(cfg["label_layers"])
for c in ly.each_cell():
    if c.name not in abstract:
        continue
    cb = c.bbox()
    pieces = {}
    for i, lname in enumerate(metals):
        dl, dd, tl, td = cfg["label_layers"][lname]
        dli, tli = ly.find_layer(dl, dd), ly.find_layer(tl, td)
        if dli is None or tli is None:
            continue
        texts = [(t.text_string, t.text.trans.disp) for t in c.shapes(tli).each() if t.is_text()]
        for sh in c.shapes(dli).each():
            if sh.is_text():
                continue
            poly = sh.polygon
            for name, pt in texts:
                if poly.inside(pya.Point(pt.x, pt.y)) or poly.bbox().contains(pya.Point(pt.x, pt.y)):
                    pieces.setdefault(name, []).append((i, poly))
                    break
    for name, shapes in sorted(pieces.items()):
        if len(shapes) < 2:
            continue
        joins += 1
        hull = pya.Box()
        for i, poly in shapes:
            c.shapes(ly.layer(JOIN_BASE + i, joins)).insert(poly)
            hull += poly.bbox()
        c.shapes(ly.layer(JOIN_HULL, joins)).insert(hull & cb)

# library GDS: the first definition of a cell wins (IO libraries repeat cells)
lopt = pya.LoadLayoutOptions()
lopt.cell_conflict_resolution = pya.LoadLayoutOptions.SkipNewCell
for g in cfg["gds"]:
    before = defined()
    ly.read(g, lopt)
    owners.update({n: os.path.basename(g) for n in defined() - before})
# Macro GDS may carry their own copies of cells named like library cells or
# like cells of another macro (e.g. two IPs with different "follower_amp"s):
# rename those instead of merging them, so every macro keeps its own layout.
renamed = {}
for g in cfg.get("macro_gds", []):
    before = defined()
    mopt = pya.LoadLayoutOptions()
    mopt.cell_conflict_resolution = pya.LoadLayoutOptions.RenameCell
    ly.read(g, mopt)
    new = sorted(defined() - before)
    owners.update({n: os.path.basename(g) for n in new})
    renamed[g] = [n for n in new if "$" in n]

out = pya.Layout()
out.dbu = ly.dbu
t = out.create_cell(cfg["top"])
t.copy_tree(top)
# missing = never defined by any GDS (still a ghost); a cell the library itself
# defines empty (e.g. a placeholder inside an IO cell) is reported apart
used = [ly.cell(i) for i in top.called_cells()]
empty = sorted(c.name for c in used if c.is_ghost_cell())
empty_defined = sorted(c.name for c in used if c.is_empty() and not c.is_ghost_cell())
orphan = sorted(c.name for c in out.each_cell() if c.name != cfg["top"] and c.parent_cells() == 0)
out.write(cfg["out"])
json.dump({"empty": empty, "orphan": orphan, "labels": labels, "cells": out.cells(), "renamed": renamed,
           "empty_defined": empty_defined, "abstract": sorted(abstract),
           "joins": joins, "join_metals": metals,
           "owners": {c.name: owners.get(c.name, "?") for c in used}}, open(cfg["out"] + ".json", "w"))
print(f"streamed {cfg['out']}: {out.cells()} cells, empty {len(empty)}, orphan {len(orphan)}, labels {labels}")
