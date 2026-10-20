"""Command line.

    python3 -m sipath validate          inputs present + scheduler graph rules (FakeSession; no design)
    python3 -m sipath gen-inputs        (re)write sipath/examples/inputs
    python3 -m sipath run <design.yaml|dp8|mcu8|ihp_add8> [workdir] [--resume] [--explore] [--config file] [-v]
                                      real OpenROAD run -> routed DEF + SPEF; --resume continues the
                                      attempt graph in workdir (finished steps are not run again);
                                      --explore: a fast first cut at PPA (typical corner, global routing,
                                      no signoff) with a suggested clock period
    python3 -m sipath harden <design.yaml|example> <outdir> [workdir] [--resume] [-v]
                                      run through signoff + views; copy the hardened macro views
                                      (LEF, Liberty per corner, GDS, CDL, macro.yaml) to outdir
    python3 -m sipath templates         design templates (a starter project per target, sipath/templates)
    python3 -m sipath new <dir> --template <name> [--set key=value ...] [--force]
                                      write a project from a template (design file, RTL, SDC)
    python3 -m sipath pdks              PDK descriptions available (sipath/pdks)
    python3 -m sipath plugins [module ...]   what plugins (entry points and the given modules) register
    python3 -m sipath docs [out.md]     reference: design-file keys, step parameters, config, PDK keys
                                      (default docs/reference.md)
    python3 -m sipath schema [out.json] JSON Schema of a design file (stdout without a file)
    python3 -m sipath draw <odb> <svg> [--ring]   schematic SVG of a checkpoint (--ring: IO ring only)
    python3 -m sipath render <odb> <png>          OpenROAD layout render of a checkpoint (save_image)
    python3 -m sipath report <workdir> [--render]   run report (report.md, report.html; --render: layout.png)
    python3 -m sipath mcp               MCP server on stdio (tools: open_run, run_step, run_eco, ...)
    python3 -m sipath toolchain [--check]   locked images, tool versions and PDK releases (--check: the
                                      configured images and installed PDK trees against the lock,
                                      sipath/toolchain.json)
    python3 -m sipath install-pdk [family] [--pdk-root dir] [--dry-run]   the locked open_pdks release
                                      through ciel (downloads it; default: the first locked family,
                                      into $PDK_ROOT or ~/.ciel)
    python3 -m sipath setup-placer [dir] [--python p]   the macro placer's Python (PyTorch CPU, numpy) for
                                      the macros method cdlns (default ~/.cache/sipath/placer-venv)
    python3 -m sipath lvs <gds> <top> <schematic.spice ...> --pdk p [--lib cells.spice] [--out dir]
                                      full-device LVS (the PDK's KLayout deck) of a layout, e.g. a macro
    python3 -m sipath limits <workdir> <design> [--file f] [--suggest [--until step]]
                                      a run's results against its regression limits
                                      (sipath/tests/limits.yaml; --suggest: an entry from this run)
"""
from __future__ import annotations

import os
import sys
import tempfile


def install_pdk(argv) -> int:
    """``ciel enable`` of the open_pdks release locked in toolchain.json."""
    import shlex
    import shutil
    import subprocess
    from .toolchain import install_command, lock
    rest, root = [a for a in argv if not a.startswith("--")], None
    if "--pdk-root" in argv:
        root = argv[argv.index("--pdk-root") + 1]
        rest = [a for a in rest if a != root]
    cmd = install_command(rest[0] if rest else next(iter(lock().get("pdks", {})), ""), root)
    print(" ".join(shlex.quote(c) for c in cmd))
    if "--dry-run" in argv:
        return 0
    if not shutil.which("ciel"):
        print("ciel is not installed: `python3 -m pip install ciel`, then run this again", file=sys.stderr)
        return 1
    return subprocess.run(cmd).returncode


def validate() -> int:
    import glob
    from . import demo, designfile
    from .design import Design
    from .pdk import load as load_pdk
    from .scheduler import EcoRefused, Flow, StepFailed
    from .session import ORFS_PLATFORMS, find_openroad

    runner = find_openroad()
    prefixes = runner.runner_prefixes() if runner else (os.path.dirname(ORFS_PLATFORMS),)
    examples = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")
    pdk = None
    for path in sorted(glob.glob(os.path.join(examples, "*.yaml"))):
        try:
            d, _ = designfile.load(path)
        except FileNotFoundError as e:  # e.g. a parent of a macro not hardened yet
            print(f"skipped: {os.path.basename(path)}: {e}")
            continue
        pdk = load_pdk(d.pdk, runner) if runner else load_pdk(d.pdk, platforms=ORFS_PLATFORMS)
        designfile.resolve_srams(d, pdk)  # as a run does: sram: names the PDK's macro views
        d.validate(pdk, runner_prefixes=prefixes)
        print(f"inputs ok: {d.name} ({d.mode}, {pdk.name}, corners {pdk.corner_source}) {len(d.input_files())} files")

    # scheduler rules, with stand-in sessions (no design is produced)
    with tempfile.TemporaryDirectory() as td:
        d = Design("fake", "fake", os.path.join(td, "x.v"), os.path.join(td, "x.sdc"), "macro",
                   floorplan_def=os.path.join(td, "x.def"))
        conc = demo.Concurrency()
        f = Flow(d, pdk, td, demo.fake_factory(), validate_inputs=False)
        a = f.step("a", demo.fake_step(conc), variants=[{"k": i} for i in range(5)], parallel=2)
        assert conc.peak <= 2, conc.peak
        assert sum(x.status == "rejected" for x in a.attempts) == 4
        try:
            f.step("b", demo.fake_step(), parent=a.attempts[0] if a.attempts[0].status == "rejected"
                   else a.attempts[1])
            raise AssertionError("continued from a rejected attempt")
        except ValueError:
            pass
        try:
            f.step("c", demo.fake_step(fail_if=lambda p: True), parent=a.selected)
            raise AssertionError("failed step not reported")
        except StepFailed:
            pass
        e1 = f.eco("e1", a.selected, [], fn=demo.fake_step(), score="setup_wns")
        for parent in (a.selected, [x for x in a.attempts if x.status == "rejected"][0]):
            try:
                f.eco("e2", parent, [], fn=demo.fake_step(), score="setup_wns")
                raise AssertionError("sibling/stale ECO accepted")
            except EcoRefused:
                pass
        assert e1.selected is not None
        print("scheduler rules ok (FakeSession stand-in; this is not a design run)")
    print(f"openroad: {runner.kind + ' ' + runner.version() if runner else 'NOT AVAILABLE'}")
    return 0


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "validate":
        return validate()
    if cmd == "draw":
        from .viz import draw
        print(draw(argv[1], argv[2], ring_only="--ring" in argv[3:]))
        return 0
    if cmd == "render":
        from .viz import render
        print(render(argv[1], argv[2]))
        return 0
    if cmd == "report":
        import json as _json
        from . import report
        wd = os.path.abspath(argv[1]) if len(argv) > 1 else os.getcwd()
        if "--render" in argv:  # the last selected checkpoint that is a database
            from .viz import render
            with open(os.path.join(wd, "graph.json")) as f:
                g = _json.load(f)
            sel = {a["id"] for a in g["attempts"] if a.get("status") == "selected"}
            odbs = [a["checkpoint"] for a in g["attempts"] if a["id"] in sel and str(a.get("checkpoint")).endswith(".odb")]
            if odbs:
                print(render(odbs[-1], os.path.join(wd, "layout.png")))
        print("\n".join(report.write(wd)))
        return 0
    if cmd == "mcp":
        from .mcp_server import serve
        serve()
        return 0
    if cmd == "gen-inputs":
        from .examples import designs
        from .examples import ihp_add8
        for k, v in {**designs.write_dp8(), **designs.write_mcu8(), **ihp_add8.write_inputs()}.items():
            if isinstance(v, str) and os.path.isfile(v):
                print(v)
        return 0
    if cmd == "run":
        import logging
        from . import designfile
        from .config import Config
        resume, verbose, explore = "--resume" in argv, "-v" in argv, "--explore" in argv
        args = [a for a in argv[1:] if not a.startswith("-")]
        cfg_file = argv[argv.index("--config") + 1] if "--config" in argv else None
        if cfg_file in args:
            args.remove(cfg_file)
        argv = ["run", *args]
        cfg = Config.load(cfg_file)
        logging.basicConfig(level="INFO" if verbose else cfg.log_level,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        path = argv[1]
        if not os.path.isfile(path):  # an example name
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", argv[1] + ".yaml")
        name = os.path.splitext(os.path.basename(path))[0]
        wd = argv[2] if len(argv) > 2 else os.path.join(os.getcwd(), "runs", name)
        fcfg = designfile.file_config(path)
        flow = designfile.run(path, wd, config=Config.load(cfg_file, **fcfg) if (cfg_file or fcfg) else cfg,
                              resume=resume, explore=explore)
        last = list(flow.steps.values())[-1].selected
        ppa_md = (last.outputs.get("reports") or {}).get("ppa")
        if ppa_md and os.path.isfile(ppa_md):
            with open(ppa_md) as f:
                print(f.read())
        for k in ("def_", "spef"):
            if k in last.outputs:
                print(f"{k.rstrip('_').upper():4}:", last.outputs[k])
        print("graph:", os.path.join(wd, "graph.json"))
        return 0
    if cmd == "harden":
        import logging
        from . import designfile
        resume, verbose = "--resume" in argv, "-v" in argv
        args = [a for a in argv[1:] if not a.startswith("-")]
        if len(args) < 2:
            print("usage: python3 -m sipath harden <design.yaml|example> <outdir> [workdir] [--resume] [-v]")
            return 2
        logging.basicConfig(level="INFO" if verbose else "WARNING",
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        path = args[0]
        if not os.path.isfile(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", path + ".yaml")
        out = designfile.harden(path, os.path.abspath(args[1]), args[2] if len(args) > 2 else None, resume=resume)
        print("views:", out)
        return 0
    if cmd == "templates":
        from . import templates
        for t in templates.listing():
            print(f"{t['name']:14s} harness {t['harness'] or '-':10s} {t['description']}")
            print(f"{'':14s} params {t['params']}")
        return 0
    if cmd == "new":
        from . import templates
        args = [a for a in argv[1:]]
        if not args or "--template" not in args:
            print("usage: python3 -m sipath new <dir> --template <name> [--set key=value ...] [--force]")
            return 2
        name = args[args.index("--template") + 1]
        sets = {}
        for k, a in enumerate(args):
            if a == "--set":
                key, _, val = args[k + 1].partition("=")
                sets[key] = val
        outdir = args[0]
        files = templates.get(name).render(outdir, sets, force="--force" in args)
        print("\n".join(files))
        print(f"run it: python3 -m sipath run {os.path.join(outdir, 'design.yaml')}")
        return 0
    if cmd == "pdks":
        from .pdk import PDK_DIR
        for f in sorted(os.listdir(PDK_DIR)):
            if f.endswith((".yaml", ".yml", ".json")):
                print(f"{os.path.splitext(f)[0]:16s} {os.path.join(PDK_DIR, f)}")
        return 0
    if cmd == "plugins":
        import json
        from . import registry
        registry.load_plugins(argv[1:])
        print(json.dumps(registry.loaded(), indent=1))
        return 0
    if cmd == "setup-placer":
        return setup_placer(argv[1:])
    if cmd == "lvs":
        from .lvs_full import main as lvs_main
        return lvs_main(argv[1:])
    if cmd == "limits":
        from .limits import main as lm
        return lm(argv[1:])
    if cmd == "toolchain":
        from .toolchain import main as tc
        return tc(argv[1:])
    if cmd == "install-pdk":
        return install_pdk(argv[1:])
    if cmd == "docs":
        from .reference import write
        out = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "reference.md")
        print(write(out))
        return 0
    if cmd == "schema":
        import json
        from .schema import json_schema
        text = json.dumps(json_schema(), indent=1)
        if len(argv) > 1:
            with open(argv[1], "w") as f:
                f.write(text + "\n")
            print(argv[1])
        else:
            print(text)
        return 0
    print(f"unknown command {cmd}\n{__doc__}")
    return 2


def setup_placer(argv) -> int:
    """Create the macro placer's Python (PyTorch CPU + numpy), for the macros
    method cdlns: ~/.cache/sipath/placer-venv, or the directory given. Uses a
    Python PyTorch supports (3.10-3.13; --python to choose)."""
    import shutil
    import subprocess
    dest = os.path.expanduser(next((a for a in argv if not a.startswith("-")), "~/.cache/sipath/placer-venv"))
    py = argv[argv.index("--python") + 1] if "--python" in argv else next(
        (p for p in ("python3.12", "python3.13", "python3.11", "python3.10") if shutil.which(p)), None)
    if not py:
        print("no Python 3.10-3.13 found for PyTorch; give one with --python")
        return 2
    subprocess.run([py, "-m", "venv", dest], check=True)
    pip = os.path.join(dest, "bin", "pip")
    subprocess.run([pip, "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([pip, "install", "-q", "torch", "numpy", "--index-url", "https://download.pytorch.org/whl/cpu"],
                   check=True)
    print(f"placer Python: {os.path.join(dest, 'bin', 'python')} (set placer_python to use another)")
    return 0


def cli():
    """Console script ``sipath`` (pyproject.toml)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
