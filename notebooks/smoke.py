"""Run the tutorial notebook's code cells, in order, up to a cell, as Jupyter would (an IPython shell: `!`
commands, `_exit_code`, display calls). CI's smoke test of the tutorial:

    python notebooks/smoke.py [--until N] [notebook]

Cells starting with "# optional" are skipped. A cell that raises, or any `!` command that exits non-zero (each
one, not only a cell's last), fails the run (exit 1), naming the cell and the command.
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
    failed = []  # every `!` command's non-zero exit (IPython's _exit_code keeps only the latest)
    system = shell.system

    def checked(cmd):
        system(cmd)
        if shell.user_ns.get("_exit_code", 0):
            failed.append((cmd, shell.user_ns["_exit_code"]))
    shell.system = checked
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
        if not r.success or failed:
            print(f"cell {i} failed" + "".join(f"\n  `{cmd}` exited {code}" for cmd, code in failed), file=sys.stderr)
            return 1
    print("tutorial smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
