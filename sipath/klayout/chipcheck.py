# KLayout batch script (klayout -b -rd cfg_file=<json> -r chipcheck.py): checks an
# assembled chip GDS against its frame and the hardened wrapper it carries.
# cfg: chip (GDS), top, die [w, h] um, wrapper (GDS), wrapper_cell, origin [x, y] um,
# out (JSON). Reports: the top cells, the chip's extent against the die, every
# placement of the wrapper cell, and the layers on which the wrapper inside the
# chip differs from the wrapper GDS (XOR, hierarchy flattened).
import json

import pya

cfg = json.load(open(cfg_file))  # noqa: F821  (cfg_file: KLayout -rd variable)
chip = pya.Layout()
chip.read(cfg["chip"])
tops = [c.name for c in chip.top_cells()]
top = chip.cell(cfg["top"])
res = {"tops": tops, "top_found": top is not None}
if top is not None:
    b = top.dbbox()
    res["bbox_um"] = [b.left, b.bottom, b.right, b.top]
    w, h = cfg["die"]
    res["die_matches"] = abs(b.left) < 0.1 and abs(b.bottom) < 0.1 and abs(b.right - w) < 0.1 and abs(b.top - h) < 0.1
    places = []
    for inst in top.each_inst():
        if inst.cell.name == cfg["wrapper_cell"]:
            t = inst.dcplx_trans
            places.append([round(t.disp.x, 4), round(t.disp.y, 4), round(t.angle, 4), bool(t.is_mirror())])
    res["wrapper_placements"] = places
    ox, oy = cfg["origin"]
    res["wrapper_at_origin"] = places == [[round(ox, 4), round(oy, 4), 0.0, False]]
    wl = pya.Layout()
    wl.read(cfg["wrapper"])
    wc, cc = wl.cell(cfg["wrapper_cell"]), chip.cell(cfg["wrapper_cell"])
    differ = {}
    if wc is not None and cc is not None:
        layers = {(wl.get_info(i).layer, wl.get_info(i).datatype) for i in wl.layer_indexes()}
        layers |= {(chip.get_info(i).layer, chip.get_info(i).datatype) for i in chip.layer_indexes()}
        for ld in sorted(layers):
            a, c = wl.find_layer(*ld), chip.find_layer(*ld)
            ra = pya.Region(wc.begin_shapes_rec(a)) if a is not None else pya.Region()
            rc = pya.Region(cc.begin_shapes_rec(c)) if c is not None else pya.Region()
            x = ra ^ rc
            if not x.is_empty():
                differ[f"{ld[0]}/{ld[1]}"] = x.count()
    res["wrapper_found"] = wc is not None and cc is not None
    res["wrapper_differs"] = differ
res["pass"] = bool(res.get("top_found") and tops == [cfg["top"]] and res.get("die_matches")
                   and res.get("wrapper_at_origin") and res.get("wrapper_found") and not res.get("wrapper_differs"))
with open(cfg["out"], "w") as f:
    json.dump(res, f, indent=1)
