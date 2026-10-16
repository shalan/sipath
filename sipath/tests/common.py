"""Shared helpers and fixtures of the SiPath tests (test_unit, test_live)."""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys
import tempfile
import types
import unittest

from sipath import analog, datapath, demo, equiv, scores, tcl, wires  # noqa: F401
from sipath.design import Design, Macro, MissingInput, PdnSpec, Strap
from sipath.examples import designs
from sipath.netlist import parse_verilog, read_verilog
from sipath.pdk import load as load_pdk
from sipath.scheduler import Attempt, EcoRefused, Flow, StepCancelled, StepFailed
from sipath.session import FakeSession, NativeRunner, OpenRoadSession, TclError, find_openroad

RUNNER = find_openroad()
LIVE = unittest.skipIf(RUNNER is None, "openroad is not available (not on PATH, no ORFS docker image)")
KEEP = os.environ.get("SIPATH_TEST_KEEP")  # set to a directory to keep live run outputs


# values Tcl quoting must keep exactly (TclQuoting, OpenRoad)
TCL_NASTY = ["a}b", "clk[0]", "x$y", "a b", 'he said "hi"', "back\\slash", "{x}", "}{", "", "semi;colon", "u_add.fa_0"]
# ChipFoundry's DAC LEF: a macro without Liberty (BlackboxModels, LiveRtl)
DAC_LEF = os.path.join(os.path.dirname(__file__), "..", "examples", "inputs", "chipfoundry",
                       "sky130_ef_ip__rdac3v_8bit", "sky130_ef_ip__rdac3v_8bit.lef")


def sky130hd(platforms):
    """The Sky130 HD description (sipath/pdks/sky130hd.yaml) on a platform tree."""
    return load_pdk("sky130hd", platforms=platforms)


def _read_json(path):
    with open(path) as fh:
        return json.load(fh)


def _write_json(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh)


def _rmtree(d):
    """Remove a run dir; a just-exited container can hold the mount briefly."""
    import time
    for _ in range(10):
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.exists(d):
            return
        time.sleep(0.5)


def _check_limits(tc, workdir, design):
    """The run in ``workdir`` within ``design``'s regression limits (sipath/tests/limits.yaml)."""
    from sipath import limits
    entry = limits.load().get(design)
    if entry is None:
        tc.skipTest(f"no limits for {design}")
    tc.assertEqual(limits.check(workdir, entry), [], f"{design} outside its limits: {limits.run_metrics(workdir, entry.get('until'))}")


def _workdir(name):
    if KEEP:
        d = os.path.join(os.path.abspath(KEEP), name)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        return d
    # under the repo so the docker runner can mount it on any host
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "runs")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"test_{name}_", dir=base)


def _lib(cells):
    from sipath.liberty import CellLibrary
    return CellLibrary.from_dict(cells)


def _pins(outs, ins=()):
    p = {o: {"direction": "output", "function": f} for o, f in outs.items()}
    p.update({i: {"direction": "input", "function": ""} for i in ins})
    return {"pins": p}


# cell descriptions as OpenSTA reports them from each library's Liberty
SKY130_CELLS = {
    **{f"sky130_fd_sc_hd__nand2_{k}": _pins({"Y": "!A | !B"}, "AB") for k in (1, 4)},
    "sky130_fd_sc_hd__nor2_1": _pins({"Y": "!A & !B"}, "AB"),
    "sky130_fd_sc_hd__inv_1": _pins({"Y": "!A"}, "A"),
    "sky130_fd_sc_hd__buf_2": _pins({"X": "A"}, "A"),
    "sky130_fd_sc_hd__tapvpwrvgnd_1": {"type": "CORE_WELLTAP", "pins": {}},
}
IHP_CELLS = {  # second library: different names, pins and function syntax
    **{f"sg13g2_a21o_{k}": _pins({"X": "(A1*A2)+B1"}, ("A1", "A2", "B1")) for k in (1, 2)},
    "sg13g2_o21ai_1": _pins({"Y": "!((A1+A2)*B1)"}, ("A1", "A2", "B1")),
    "sg13g2_inv_1": _pins({"Y": "!A"}, "A"),
    "sg13g2_dlygate4sd3_1": _pins({"X": "A"}, "A"),  # a delay cell is a buffer by function
    **{f"sg13g2_dfrbp_{k}": {"pins": {"Q": {"direction": "output", "function": "IQ"},
                                      "Q_N": {"direction": "output", "function": "IQN"},
                                      "IQ": {"direction": "internal", "function": ""},
                                      "CLK": {"direction": "input", "function": ""},
                                      "D": {"direction": "input", "function": ""},
                                      "RESET_B": {"direction": "input", "function": ""}}} for k in (1, 2)},
    "sg13g2_dfrbpq_1": {"pins": {"Q": {"direction": "output", "function": "IQ"},
                                 "CLK": {"direction": "input", "function": ""},
                                 "D": {"direction": "input", "function": ""},
                                 "RESET_B": {"direction": "input", "function": ""}}},
    "sg13g2_fill_1": {"type": "CORE_SPACER", "pins": {}},
    "sg13g2_antennanp": {"type": "CORE_ANTENNACELL", "pins": {"A": {"direction": "input", "function": ""}}},
}


SKY130A = os.path.isdir(os.path.expanduser(os.environ.get("PDK_ROOT", "~/.ciel")) + "/sky130A")


class KillableSession(FakeSession):
    """A stand-in session whose ``block`` command waits until it is killed."""

    def __init__(self, workdir):
        super().__init__(workdir, responses={"::flow_timing_prep": "placement"})
        import threading
        self.dead = threading.Event()
        self.name = "fake-" + os.path.basename(workdir.rstrip("/"))

    def kill(self):
        self.dead.set()

    def run(self, script, check=True, timeout=None):
        if script == "block":
            if not self.dead.wait(30):
                raise AssertionError("never killed")
            from sipath.session import SessionDied
            raise SessionDied("killed")
        return super().run(script, check, timeout)


class McpClient:
    """Minimal MCP stdio client for tests: spawns `python3 -m sipath mcp`."""

    def __init__(self):
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.p = subprocess.Popen([sys.executable, "-m", "sipath", "mcp"], cwd=root, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.notes, self.early = [], {}

    def send(self, method, params=None, notify=False, raw=None):
        mid = None if notify or raw is not None else self._id()
        line = raw if raw is not None else json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}, **({} if mid is None else {"id": mid})})
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        return mid

    def wait(self, mid):
        """The reply with id ``mid``; notifications seen meanwhile go to ``notes``."""
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise EOFError("server closed")
            msg = json.loads(line)
            if "id" not in msg:
                self.notes.append(msg)
            elif msg["id"] == mid:
                return msg
            else:
                self.early[msg["id"]] = msg

    def rpc(self, method, params=None, notify=False, raw=None):
        mid = self.send(method, params, notify, raw)
        if notify:
            return None
        return self.early.pop(mid) if mid in self.early else self.wait(mid)

    def _id(self):
        self.n += 1
        return self.n

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["isError"], r.get("structuredContent"), r["content"][0]["text"]

    def close(self):
        self.p.stdin.close()
        self.p.stdout.close()
        self.p.wait(timeout=60)


def _check_outputs(tc, ex, design_name, analog_nets, rings=False):
    deff, spef = ex.outputs["def_"], ex.outputs["spef"]
    for f in (deff, spef):
        tc.assertTrue(os.path.isfile(f), f"{f} missing")
        tc.assertGreater(os.path.getsize(f), 0, f"{f} empty")
    with open(spef) as fh:
        head = fh.read(2000)
    tc.assertTrue('*PROGRAM "OpenROAD"' in head, "SPEF not from OpenROAD")
    tc.assertIn(f'*DESIGN "{design_name}"', head)
    tc.assertGreater(ex.metrics["spef_nets"], 0)
    with open(deff) as fh:
        text = fh.read()
    nets = text[text.index("\nNETS "):text.index("\nEND NETS")]
    tc.assertGreater(nets.count("+ ROUTED"), 50)
    special = text[text.index("\nSPECIALNETS "):text.index("\nEND SPECIALNETS")]
    for n in analog_nets:  # restored before RCX: regular routed nets with the analog NDR
        tc.assertTrue(re.search(r"- " + re.escape(n) + r" [^;]*\+ USE ANALOG[^;]*\+ NONDEFAULTRULE ANALOG"
                                r"[^;]*\+ ROUTED", nets), f"analog net {n} not routed with its NDR in NETS")
        tc.assertFalse(re.search(r"- " + re.escape(n) + r" ", special), f"analog net {n} left in SPECIALNETS")
    tc.assertTrue("+ SHAPE FOLLOWPIN" in special, "no followpin rails")  # followpin rails
    tc.assertTrue("+ SHAPE STRIPE" in special, "no straps")  # straps
    tc.assertEqual("+ SHAPE RING" in special, rings, "core ring presence")  # rings only when asked


def _big_def(path, n=100_000, cols=400):
    """A placed chain of ``n`` sky130 buf_1 cells, as a DEF with components and nets."""
    sw, rh = 460, 2720
    w = 4 * sw
    rows = (n + cols - 1) // cols
    with open(path, "w") as f:
        f.write(f"VERSION 5.8 ;\nDESIGN big ;\nUNITS DISTANCE MICRONS 1000 ;\n"
                f"DIEAREA ( 0 0 ) ( {cols * w + 20000} {rows * rh + 20000} ) ;\n")
        for r in range(rows):
            f.write(f"ROW ROW_{r} unithd 10000 {10000 + r * rh} {'N' if r % 2 == 0 else 'FS'} DO {cols * 4} BY 1 "
                    f"STEP {sw} 0 ;\n")
        f.write(f"COMPONENTS {n} ;\n")
        for i in range(n):
            r, c = divmod(i, cols)
            f.write(f"- b{i} sky130_fd_sc_hd__buf_1 + PLACED ( {10000 + c * w} {10000 + r * rh} ) "
                    f"{'N' if r % 2 == 0 else 'FS'} ;\n")
        f.write(f"END COMPONENTS\nNETS {n - 1} ;\n")
        for i in range(n - 1):
            f.write(f"- n{i} ( b{i} X ) ( b{i + 1} A ) ;\n")
        f.write("END NETS\nEND DESIGN\n")
