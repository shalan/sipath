# KLayout batch script (klayout -b -rd cfg_file=<json> -r sealring.py): adds a seal
# ring to an assembled chip GDS, centred on the die (its bounding box's centre on
# the die's centre), and writes the chip back. cfg: chip (GDS, rewritten), top,
# die [w, h] um, seal (GDS), out (JSON: the seal cell, its displacement in um).
import json

import pya

cfg = json.load(open(cfg_file))  # noqa: F821  (cfg_file: KLayout -rd variable)
chip = pya.Layout()
chip.read(cfg["chip"])
top = chip.cell(cfg["top"])
seal = pya.Layout()
seal.read(cfg["seal"])
st = seal.top_cells()[0]
if chip.cell(st.name) is not None:
    raise RuntimeError(f"the chip already has a cell {st.name}")
sb = st.dbbox()
w, h = cfg["die"]
dx, dy = w / 2 - sb.center().x, h / 2 - sb.center().y
# copy the seal ring's cell tree into the chip, then place it once in the top cell
new = chip.create_cell(st.name)
new.copy_tree(st)
top.insert(pya.DCellInstArray(new.cell_index(), pya.DTrans(pya.DVector(dx, dy))))
chip.write(cfg["chip"])
with open(cfg["out"], "w") as f:
    json.dump({"seal_cell": st.name, "displacement_um": [round(dx, 4), round(dy, 4)]}, f, indent=1)
