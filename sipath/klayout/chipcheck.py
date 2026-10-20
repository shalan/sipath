# KLayout batch script (klayout -b -rd cfg_file=<json> -r chipcheck.py): checks an
# assembled chip GDS against its frame and the hardened wrapper it carries.
# cfg: chip (GDS), top, die [w, h] um, wrapper (GDS), wrapper_cell, origin [x, y] um,
# seal_cell (or null), out (JSON). Reports: the top cells, the chip's extent
# against the die (the seal ring left out), every placement of the wrapper
# cell, the layers on which the wrapper inside the chip differs from the
# wrapper GDS (XOR, hierarchy flattened), and for a seal ring: placed once,
# centred on the die, touching none of the frame's shapes, and its opening.
import json

import pya

cfg = json.load(open(cfg_file))  # noqa: F821  (cfg_file: KLayout -rd variable)
chip = pya.Layout()
chip.read(cfg["chip"])
tops = [c.name for c in chip.top_cells()]
top = chip.cell(cfg["top"])
res = {"tops": tops, "top_found": top is not None}
seal = cfg.get("seal_cell")
if top is not None:
    b = pya.DBox()
    for inst in top.each_inst():  # the frame's extent: everything but the seal ring
        if inst.cell.name != seal:
            b += inst.dbbox()
    for li in chip.layer_indexes():  # the top cell's own shapes (not its children's)
        if not top.shapes(li).is_empty():
            b += pya.Region(top.shapes(li)).bbox().to_dtype(chip.dbu)
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
    if seal:
        sp = [i for i in top.each_inst() if i.cell.name == seal]
        res["seal_placements"] = [[round(i.dcplx_trans.disp.x, 4), round(i.dcplx_trans.disp.y, 4)] for i in sp]
        if len(sp) == 1:
            sb = sp[0].dbbox()
            res["seal_bbox_um"] = [sb.left, sb.bottom, sb.right, sb.top]
            res["seal_centred"] = abs(sb.center().x - w / 2) < 0.01 and abs(sb.center().y - h / 2) < 0.01
            # the frame's own shapes (every child but the seal ring, and the top's), per layer
            touching, frame_box = {}, pya.Box()
            for li in chip.layer_indexes():
                ring = pya.Region(sp[0].cell.begin_shapes_rec(li)).transformed(sp[0].cplx_trans)
                frame = pya.Region(top.shapes(li))
                for inst in top.each_inst():
                    if inst.cell.name != seal:
                        frame += pya.Region(inst.cell.begin_shapes_rec(li)).transformed(inst.cplx_trans)
                frame_box += frame.bbox()
                if not ring.is_empty() and not frame.is_empty():
                    n = (ring & frame).count()
                    if n:
                        info = chip.get_info(li)
                        touching[f"{info.layer}/{info.datatype}"] = n
            res["seal_touches_frame"] = touching
            # the ring's opening: the hole around the die's centre, which must hold the frame's contents
            allr = pya.Region()
            for li in chip.layer_indexes():
                allr += pya.Region(sp[0].cell.begin_shapes_rec(li)).transformed(sp[0].cplx_trans)
            allr.merge()
            ctr = pya.Point(int(round(w / 2 / chip.dbu)), int(round(h / 2 / chip.dbu)))
            opening = [p.bbox() for p in (pya.Region(allr.bbox()) - allr).each_merged() if p.inside(ctr)]
            if opening:
                o = opening[0]
                res["seal_opening_um"] = [o.left * chip.dbu, o.bottom * chip.dbu, o.right * chip.dbu, o.top * chip.dbu]
res["pass"] = bool(res.get("top_found") and tops == [cfg["top"]] and res.get("die_matches")
                   and res.get("wrapper_at_origin") and res.get("wrapper_found") and not res.get("wrapper_differs")
                   and (not seal or (len(res.get("seal_placements", [])) == 1 and res.get("seal_centred")
                                     and res.get("seal_touches_frame") == {})))
with open(cfg["out"], "w") as f:
    json.dump(res, f, indent=1)
