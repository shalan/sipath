"""Command line.

    python3 -m sipath validate          inputs present + scheduler graph rules (FakeSession; no design)
    python3 -m sipath gen-inputs        (re)write sipath/examples/inputs
    python3 -m sipath run <design.yaml|dp8|mcu8|ihp_add8> [workdir] [--resume] [--config file] [-v]
                                      real OpenROAD run -> routed DEF + SPEF; --resume continues the
                                      attempt graph in workdir (finished steps are not run again)
    python3 -m sipath pdks              PDK descriptions available (sipath/pdks)
    python3 -m sipath docs [out.md]     reference: design-file keys, step parameters, config, PDK keys
                                      (default docs/reference.md)
    python3 -m sipath schema [out.json] JSON Schema of a design file (stdout without a file)
    python3 -m sipath draw <odb> <svg> [--ring]   schematic SVG of a checkpoint (--ring: IO ring only)
    python3 -m sipath render <odb> <png>          OpenROAD layout render of a checkpoint (save_image)
    python3 -m sipath mcp               MCP server on stdio (tools: open_run, run_step, run_eco, ...)
"""
from __future__ import annotations

import os
import sys
import tempfile


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
        d, _ = designfile.load(path)
        pdk = load_pdk(d.pdk, runner) if runner else load_pdk(d.pdk, platforms=ORFS_PLATFORMS)
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
        resume, verbose = "--resume" in argv, "-v" in argv
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
                              resume=resume)
        last = list(sipath.steps.values())[-1].selected
        for k in ("def_", "spef"):
            if k in last.outputs:
                print(f"{k.rstrip('_').upper():4}:", last.outputs[k])
        print("graph:", os.path.join(wd, "graph.json"))
        return 0
    if cmd == "pdks":
        from .pdk import PDK_DIR
        for f in sorted(os.listdir(PDK_DIR)):
            if f.endswith((".yaml", ".yml", ".json")):
                print(f"{os.path.splitext(f)[0]:16s} {os.path.join(PDK_DIR, f)}")
        return 0
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
