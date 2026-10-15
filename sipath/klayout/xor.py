# KLayout batch script (klayout -b -rd cfg_file=<json> -r xor.py): XOR of two GDS
# files of the same design (e.g. the KLayout and the Magic stream-out), layer by
# layer on the flattened top cell. Text shapes are not compared; layers listed in
# "ignore" ("l/d") are skipped. Writes <out> (JSON): per differing layer the
# number of XOR polygons, their area and up to five example boxes.
import json

import pya

cfg = json.load(open(cfg_file))  # noqa: F821  (cfg_file: KLayout -rd variable)
lay = {}
for key in ("a", "b"):
    ly = pya.Layout()
    ly.read(cfg[key])
    top = ly.cell(cfg["top"])
    if top is None:
        raise RuntimeError(f"top cell {cfg['top']} not in {cfg[key]}")
    lay[key] = (ly, top)
dbu = lay["a"][0].dbu
ignore = set(cfg.get("ignore", []))
layers = set()
for ly, _ in lay.values():
    for li in ly.layer_indexes():
        info = ly.get_info(li)
        layers.add((info.layer, info.datatype))


def region(key, ld):
    ly, top = lay[key]
    li = ly.find_layer(*ld)
    if li is None:
        return pya.Region()
    r = pya.Region(top.begin_shapes_rec(li))
    if abs(ly.dbu - dbu) > 1e-12:
        r = r.transformed(pya.ICplxTrans(ly.dbu / dbu))
    return r.merged()


diff, compared = {}, 0
for ld in sorted(layers):
    if f"{ld[0]}/{ld[1]}" in ignore:
        continue
    ra, rb = region("a", ld), region("b", ld)
    if ra.is_empty() and rb.is_empty():
        continue
    compared += 1
    x = ra ^ rb
    if not x.is_empty():
        boxes = [[round(v * dbu, 3) for v in (p.bbox().left, p.bbox().bottom, p.bbox().right, p.bbox().top)]
                 for p in list(x.each())[:5]]
        diff[f"{ld[0]}/{ld[1]}"] = {"polygons": x.count(), "area_um2": round(x.area() * dbu * dbu, 4),
                                    "examples": boxes, "only_in_a": not (ra & x).is_empty(),
                                    "only_in_b": not (rb & x).is_empty()}
with open(cfg["out"], "w") as f:
    json.dump({"layers_compared": compared, "differences": diff}, f, indent=1)
