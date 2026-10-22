"""Run the tutorial notebook's code cells, in order, up to a cell, as Jupyter would (an IPython shell: `!`
commands, `_exit_code`, display calls). CI's smoke test of the tutorial:

    python notebooks/smoke.py [--until N] [notebook]

Cells starting with "# optional" are skipped. A cell that raises, or a `!` command that exits non-zero, fails
the run (exit 1), naming the cell.
"""
from __future__ import annotations

import json
import os
import sys


def main(argv) -> int:
    until = int(argv[argv.index("--until") + 1]) if "--until" in argv else 10 ** 6
    args = [a for a in argv if not a.startswith("--") and not a.isdigit()]
    path = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "sipath_tutorial.ipynb")
    from IPython.core.interactiveshell import InteractiveShell
    shell = InteractiveShell.instance()
    with open(path) as f:
        cells = json.load(f)["cells"]
    for i, c in enumerate(cells):
        if i > until:
            break
        if c["cell_type"] != "code":
            continue
        src = "".join(c["source"])
        if src.lstrip().startswith("# optional"):
            print(f"--- cell {i}: skipped (optional)", flush=True)
            continue
        print(f"--- cell {i}: {src.splitlines()[0][:100]}", flush=True)
        r = shell.run_cell(src, store_history=True)
        code = shell.user_ns.get("_exit_code", 0)
        if not r.success or code:
            print(f"cell {i} failed" + (f" (a command exited {code})" if code else ""), file=sys.stderr)
            return 1
    print("tutorial smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
