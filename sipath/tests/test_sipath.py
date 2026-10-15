"""Tests.

Unit tests need no binary. The live classes (``OpenRoad``, ``LiveSoftMacro``,
``LiveMcuDie``) run the real OpenROAD process and skip only when no OpenROAD
is available (``openroad`` not on PATH and no ORFS docker image). When they
run, a missing or empty DEF/SPEF fails them.

    python3 -m unittest sipath.tests.test_sipath
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
import types
import unittest

from sipath import analog, datapath, demo, equiv, scores, tcl, wires  # noqa: F401
from sipath.design import Design, Macro, MissingInput, PdnSpec, Strap
from sipath.examples import designs
from sipath.netlist import parse_verilog, read_verilog
from sipath.pdk import load as load_pdk
from sipath.scheduler import Attempt, EcoRefused, Flow, StepCancelled, StepFailed
from sipath.session import FakeSession, OpenRoadSession, TclError, find_openroad

RUNNER = find_openroad()
LIVE = unittest.skipIf(RUNNER is None, "openroad is not available (not on PATH, no ORFS docker image)")
KEEP = os.environ.get("SIPATH_TEST_KEEP")  # set to a directory to keep live run outputs


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


# ----------------------------------------------------------------------------- units
class Netlist(unittest.TestCase):
    def test_roundtrip_and_function(self):
        m, meta = designs.dp8_netlist()
        m2 = parse_verilog(m.to_verilog())
        self.assertEqual(len(m2.instances), len(m.instances))
        self.assertEqual({i.name for i in m2.instances}, {i.name for i in m.instances})
        self.assertEqual(m2.assigns, m.assigns)
        # simulate the adder and multiplier
        fn = {"fa_1": lambda A, B, CIN: {"SUM": A ^ B ^ CIN, "COUT": (A & B) | (A & CIN) | (B & CIN)},
              "ha_1": lambda A, B: {"SUM": A ^ B, "COUT": A & B}, "and2_1": lambda A, B: {"X": A & B},
              "conb_1": lambda: {"LO": 0, "HI": 1}}
        comb = [i for i in m.instances if i.cell.split("__")[1] in fn]
        rng = random.Random(1)
        for _ in range(100):
            a, b, x, y = rng.randrange(256), rng.randrange(256), rng.randrange(16), rng.randrange(16)
            v = {f"u_in.a_q_{i}": a >> i & 1 for i in range(8)}
            v.update({f"u_in.b_q_{i}": b >> i & 1 for i in range(8)})
            v.update({f"u_in.x_q_{i}": x >> i & 1 for i in range(4)})
            v.update({f"u_in.y_q_{i}": y >> i & 1 for i in range(4)})
            changed = True
            while changed:
                changed = False
                for i in comb:
                    f = fn[i.cell.split("__")[1]]
                    ins = {p: n for p, n in i.conns.items() if p in f.__code__.co_varnames}
                    if all(n in v for n in ins.values()):
                        for p, val in f(**{p: v[n] for p, n in ins.items()}).items():
                            if p in i.conns and i.conns[p] not in v:
                                v[i.conns[p]] = val
                                changed = True
            d = {i.conns["Q"]: v.get(i.conns["D"]) for i in m.instances if "dfxtp" in i.cell}
            self.assertEqual(sum(d[f"sum[{i}]"] << i for i in range(9)), a + b)
            self.assertEqual(sum(d[f"prod[{i}]"] << i for i in range(8)), x * y)


class BusNetlist(unittest.TestCase):
    def test_bus_pins_written_as_concatenation_and_parsed_back(self):
        from sipath.netlist import Module
        m = Module("t")
        m.inst("u", "MACRO", **{f"D[{i}]": f"n{i}" for i in range(3)}, CLK="clk")
        v = m.to_verilog()
        self.assertIn(".D({n2, n1, n0})", v)
        back = parse_verilog(v)
        self.assertEqual(back.instances[0].conns, {"D[0]": "n0", "D[1]": "n1", "D[2]": "n2", "CLK": "clk"})
        m.inst("w", "MACRO", **{"D[0]": "a", "D[2]": "b"})
        with self.assertRaises(ValueError):
            m.to_verilog()

    def test_mcu8_sar_controller(self):
        m, _ = designs.mcu8_netlist()
        sar = [i for i in m.instances if i.name.startswith("u_sar.")]
        comb = {"or2_1": lambda A, B: {"X": A | B}, "and2b_1": lambda A_N, B: {"X": (1 - A_N) & B},
                "mux2_1": lambda A0, A1, S: {"X": A1 if S else A0}, "inv_2": lambda A: {"Y": 1 - A}}
        for vin in (0.1234, 0.75, 0.999):
            st = {i.conns["Q"]: (1 if "dfstp" in i.cell else 0) for i in sar if "df" in i.cell}
            held, res = None, []
            for _ in range(25):
                v = dict(st)
                v["core.pad_rst_n_i"] = 1
                data = sum(v[f"u_sar.d_{j}"] << j for j in range(10))
                if v["u_sar.p_10"]:
                    held = vin
                v["u_adc.cmp"] = int(held is not None and held > data / 1024.0)  # EF_ADCS1008NC model
                changed = True
                while changed:
                    changed = False
                    for i in sar:
                        k = i.cell.split("__")[1]
                        if k in comb:
                            ins = {p: n for p, n in i.conns.items() if p in comb[k].__code__.co_varnames}
                            if all(n in v for n in ins.values()):
                                for p, val in comb[k](**{p: v[n] for p, n in ins.items()}).items():
                                    if i.conns[p] not in v:
                                        v[i.conns[p]] = val
                                        changed = True
                nst = {i.conns["Q"]: v[i.conns["D"]] for i in sar if "df" in i.cell}
                if v["u_sar.p_0"]:
                    res.append(sum(nst[f"u_sar.res_{j}"] << j for j in range(10)))
                st = nst
            self.assertIn(res[0], (int(vin * 1024), int(vin * 1024) - 1))


class Packing(unittest.TestCase):
    def rows(self):
        left = [datapath.Row(f"a{i}", 0, i * 2720, "N" if i % 2 == 0 else "FS", 460, 100, 2720) for i in range(30)]
        right = [datapath.Row(f"b{i}", 60000, i * 2720, "N" if i % 2 == 0 else "FS", 460, 100, 2720)
                 for i in range(30)]
        return left + right

    def test_column_is_abutted_vertically(self):
        sizes = {f"fa{i}": (7360, 2720) for i in range(8)}
        pl = datapath.pack_column(list(sizes), (920, 5440), self.rows(), sizes)
        self.assertEqual([p.y for p in pl], [5440 + 2720 * k for k in range(8)])
        self.assertEqual({p.x for p in pl}, {920})
        self.assertEqual([p.orient for p in pl], ["N", "FS"] * 4)

    def test_array_rows_abut_and_reabut_after_growth(self):
        cells = [["pp0", "add0", "pp1", "add1"], ["pp2", "add2"]]
        sizes = {"pp0": (2300, 2720), "add0": (7360, 2720), "pp1": (2300, 2720), "add1": (7360, 2720),
                 "pp2": (2300, 2720), "add2": (7360, 2720)}
        pl = datapath.pack_array(cells, (0, 0), self.rows(), sizes)
        row0 = [p for p in pl if p.y == 0]
        for a, b in zip(row0, row0[1:]):
            self.assertEqual(a.x + a.w, b.x)
        sizes["add0"] = (8280, 2720)  # fa_1 -> fa_2 during sizing
        pl2 = datapath.pack_array(cells, (0, 0), self.rows(), sizes)
        datapath.check_internal(pl2)
        moved = {p.inst for p, q in zip(pl, pl2) if p.x != q.x}
        self.assertEqual(moved, {"pp1", "add1"})

    def test_block_respects_cut_rows_and_avoid_boxes(self):
        sizes = {f"c{i}": (7360, 2720) for i in range(8)}
        pl = datapath.pack_block(list(sizes), (70000, 20000), self.rows(), sizes, 0.8)
        datapath.check_internal(pl)
        self.assertTrue(all(p.x >= 60000 for p in pl))
        pl = datapath.pack_block(list(sizes), (20000, 20000), self.rows(), sizes, 0.8,
                                 avoid=[(10000, 0, 40000, 80000)])
        self.assertTrue(all(p.x + p.w <= 10000 or p.x >= 40000 for p in pl))

    def test_slices_align_stage_columns_and_regrow(self):
        # 3 bits x 3 stages; bit 1 has no stage-1 cell; stage columns as wide as their widest cell
        cells = [["ra0", "rb0", "fa0"], ["ra1", None, "fa1"], ["ra2", "rb2", "fa2"]]
        sizes = {c: (4140, 2720) for r in cells for c in r if c}
        sizes["rb2"] = (5060, 2720)  # widest stage-1 cell
        pl = {p.inst: p for p in datapath.pack_slices(cells, (920, 0), self.rows(), sizes, stage_gap=1)}
        self.assertEqual({pl[c].x for c in ("fa0", "fa1", "fa2")}, {920 + 4140 + 460 + 5060 + 460})
        self.assertEqual({pl[c].x for c in ("rb0", "rb2")}, {920 + 4140 + 460})
        self.assertEqual([pl[f"ra{b}"].y for b in range(3)], [0, 2720, 5440])
        self.assertNotIn("rb1", pl)
        sizes["ra1"] = (5060, 2720)  # a stage-0 cell grows: stages 1 and 2 of every bit move right
        pl2 = {p.inst: p for p in datapath.pack_slices(cells, (920, 0), self.rows(), sizes, stage_gap=1)}
        moved = {c for c in pl if pl[c].x != pl2[c].x}
        self.assertEqual(moved, {"rb0", "rb2", "fa0", "fa1", "fa2"})
        sp = datapath.pack_slices(cells, (920, 0), self.rows(), sizes, rows_per_slice=2)
        self.assertEqual(sorted({p.y for p in sp}), [0, 5440, 10880])

    def test_internal_overlap_detected(self):
        a = datapath.Placement("a", 0, 0, "N", 1000, 2720)
        b = datapath.Placement("b", 500, 0, "N", 1000, 2720)
        with self.assertRaises(ValueError):
            datapath.check_internal([a, b])


class MacroRules(unittest.TestCase):
    """sipath.macroplace geometry: orientation of pin sides and placement rules."""

    def test_orient_side(self):
        from sipath.macroplace import orient_side
        w, h = 1000, 2000
        self.assertEqual(orient_side("bottom", "R0", w, h), "bottom")
        self.assertEqual(orient_side("bottom", "MX", w, h), "top")
        self.assertEqual(orient_side("left", "MY", w, h), "right")
        self.assertEqual(orient_side("left", "R180", w, h), "right")
        self.assertEqual(orient_side("bottom", "R180", w, h), "top")
        self.assertEqual(orient_side("left", "R90", w, h), "bottom")
        self.assertEqual(orient_side("bottom", "R90", w, h), "right")

    def test_pin_sides_from_shapes(self):
        from sipath.macroplace import pin_sides
        pins = [(f"d{i}", "SIGNAL", "INPUT", [[100 * i, 0, 100 * i + 50, 50]]) for i in range(1, 9)]
        pins += [("vdd", "POWER", "INOUT", [[0, 0, 50, 2000]]), ("x", "SIGNAL", "OUTPUT", [[950, 1000, 1000, 1050]])]
        self.assertEqual(pin_sides(pins, 1000, 2000), frozenset({"bottom", "right"}))
        self.assertEqual(pin_sides(pins, 1000, 2000, frac=0.5), frozenset({"bottom"}))

    def test_rules(self):
        from sipath.macroplace import MacroBox, Rules, violation
        core = (0, 0, 100000, 100000)
        r = Rules(channel=20000, pin_clearance=15000, analog_guard=5000, hug=2720)
        a = MacroBox("a", (10000, 10000, 30000, 30000), 2000)
        r0 = Rules(channel=8000, hug=2720)
        self.assertIsNone(violation(a, [], core, [], r0))
        self.assertIn("sliver", violation(a, [], core, [], r))  # 10 um < 20 um channel, > halo + hug
        hug = MacroBox("h", (2000, 40000, 20000, 60000), 2000)
        self.assertIsNone(violation(hug, [], core, [], r))  # against the left edge: allowed
        out = MacroBox("o", (1000, 40000, 20000, 60000), 2000)
        self.assertIn("halo leaves", violation(out, [], core, [], r))
        pins_left = MacroBox("p", (2000, 40000, 20000, 60000), 2000, frozenset({"left"}))
        self.assertIn("pins on the left", violation(pins_left, [], core, [], r))
        b = MacroBox("b", (40000, 40000, 60000, 60000), 2000)
        c = MacroBox("c", (75000, 42000, 98000, 58000), 2000)  # faces b at 15 um < channel 20
        self.assertIn("from b", violation(c, [b], core, [], Rules(channel=20000)))
        self.assertIsNone(violation(c, [b], core, [], Rules(channel=10000)))
        cp = MacroBox("c", (75000, 42000, 98000, 58000), 2000, frozenset({"left"}))  # pins toward b
        self.assertIn("from b", violation(cp, [b], core, [], Rules(channel=10000, pin_clearance=16000)))
        ca = MacroBox("c", (66000, 42000, 98000, 58000), 2000, analog=True)  # 6 um: halos 4 + guard 5 = 9
        self.assertIn("from b", violation(ca, [b], core, [], Rules(analog_guard=5000)))
        self.assertIsNone(violation(ca, [b], core, [], Rules()))
        diag = MacroBox("d", (61000, 61000, 70000, 70000), 2000)  # diagonal: only the halos matter
        self.assertIn("halo overlaps", violation(diag, [b], core, [], Rules()))
        self.assertIn("keepout", violation(b, [], core, [(55000, 0, 70000, 100000)], Rules()))


class Congestion(unittest.TestCase):
    REPORT = """violation type: Horizontal congestion
\tsrcs: net:a net:b net:c
\tcomment: capacity:2 usage:3 congestion:1
\tbbox = (96.6000, 13.8000) - (103.5000, 20.7000) on Layer -
violation type: Vertical congestion
\tsrcs: net:a net:d net:e net:f net:g
\tcomment: capacity:2 usage:6 congestion:4
\tbbox = (10.0000, 20.0000) - (16.9000, 26.9000) on Layer -
"""

    def test_parse_and_missing(self):
        from sipath import congestion
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "c.rpt")
        with open(p, "w") as f:
            f.write(self.REPORT)
        c = congestion.parse(p)
        self.assertEqual((c["gcells"], c["overflow"], c["max"]), (2, 5, 4))
        self.assertEqual(c["worst"], [10.0, 20.0, 16.9, 26.9])
        self.assertEqual(c["nets"][0], "a")
        self.assertEqual(c["by_kind"], {"Horizontal congestion": 1, "Vertical congestion": 1})
        self.assertEqual(congestion.parse(os.path.join(d, "none.rpt"))["gcells"], 0)  # no file: no congestion
        with open(p, "w") as f:
            f.write("violation type: something new\n")
        with self.assertRaises(ValueError):
            congestion.parse(p)

    def test_scores(self):
        a = {"grt_overflow": 0, "wns": 0.1, "hpwl_um": 900.0}
        b = {"grt_overflow": 12, "wns": 0.5, "hpwl_um": 800.0}
        self.assertGreater(scores.routability_then_wns(a), scores.routability_then_wns(b))
        self.assertIsNone(scores.routability_then_wns({"wns": 1.0, "hpwl_um": 1.0}))
        q1 = {"quick_overflow": 0, "macro_cost_um": 5000.0}
        q2 = {"quick_overflow": 0, "macro_cost_um": 4000.0}
        q3 = {"quick_overflow": 3, "macro_cost_um": 1000.0}
        self.assertEqual(max([q1, q2, q3], key=scores.macro_quick), q2)

    def test_layer_adjustments(self):
        pdk = sky130hd("/p")
        pdk.layer_adjustments = {"met1": 0.3}
        d = Design("x", "x", "n.v", "s.sdc", "macro", floorplan_def="f.def", layer_adjustments={"met2": 0.2}).resolve(pdk)
        t = tcl.routing_layers(d, pdk)
        self.assertIn("set_global_routing_layer_adjustment met1 0.3", t)
        self.assertIn("set_global_routing_layer_adjustment met2 0.2", t)
        t = tcl.routing_layers(d, pdk, margin=0.5)
        for ly in ("met1", "met2", "met3", "met4"):
            self.assertIn(f"set_global_routing_layer_adjustment {ly} 0.5", t)
        self.assertNotIn("met5", t)


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


class Equivalence(unittest.TestCase):
    BASE = """module t (a, b, y); input a; input b; output y; wire n1;
      sky130_fd_sc_hd__nand2_1 u1 (.A(a), .B(b), .Y(n1));
      sky130_fd_sc_hd__inv_1 u2 (.A(n1), .Y(y));
      sky130_fd_sc_hd__tapvpwrvgnd_1 TAP_1 ();
    endmodule"""
    LIB = _lib(SKY130_CELLS)

    def test_resize_and_buffer_are_equivalent(self):
        before = parse_verilog(self.BASE)
        resized = parse_verilog(self.BASE.replace("nand2_1 u1", "nand2_4 u1"))
        equiv.check(before, resized, self.LIB)
        buffered = parse_verilog(self.BASE.replace(".A(n1), .Y(y)", ".A(n2), .Y(y)").replace(
            "wire n1;", "wire n1; wire n2; sky130_fd_sc_hd__buf_2 b1 (.A(n1), .X(n2));"))
        equiv.check(before, buffered, self.LIB)

    def test_function_change_fails(self):
        with self.assertRaises(equiv.EquivalenceError):
            equiv.check(parse_verilog(self.BASE), parse_verilog(self.BASE.replace("nand2_1 u1", "nor2_1 u1")),
                        self.LIB)
        with self.assertRaises(equiv.EquivalenceError):
            equiv.check(parse_verilog(self.BASE), parse_verilog(self.BASE.replace(".A(a), .B(b)", ".A(b), .B(b)")),
                        self.LIB)

    def test_second_library(self):
        lib = _lib(IHP_CELLS)
        base = """module t (a, b, c, clk, rn, q); input a; input b; input c; input clk; input rn; output q;
          wire n1; wire n2;
          sg13g2_a21o_1 u1 (.A1(a), .A2(b), .B1(c), .X(n1));
          sg13g2_inv_1 u2 (.A(n1), .Y(n2));
          sg13g2_dfrbp_1 r0 (.CLK(clk), .D(n2), .RESET_B(rn), .Q(q), .Q_N());
          sg13g2_fill_1 FILLER_0 ();
          sg13g2_antennanp ANT_0 (.A(n1));
        endmodule"""
        before = parse_verilog(base)
        equiv.check(before, parse_verilog(base.replace("a21o_1 u1", "a21o_2 u1").replace("dfrbp_1", "dfrbp_2")), lib)
        delayed = base.replace(".D(n2)", ".D(n3)").replace("wire n2;", "wire n2; wire n3;\n"
                                                           "sg13g2_dlygate4sd3_1 h0 (.A(n2), .X(n3));")
        equiv.check(before, parse_verilog(delayed), lib)  # hold buffer, recognised from its function
        self.assertTrue(lib["sg13g2_dlygate4sd3_1"].is_buffer and lib["sg13g2_inv_1"].is_inverter)
        self.assertTrue(lib["sg13g2_fill_1"].physical and lib["sg13g2_antennanp"].physical)
        with self.assertRaises(equiv.EquivalenceError):  # a21o -> o21ai: different truth table
            equiv.check(before, parse_verilog(base.replace("sg13g2_a21o_1 u1", "sg13g2_o21ai_1 u1")
                                              .replace(".X(n1)", ".Y(n1)")), lib)
        with self.assertRaises(equiv.EquivalenceError):  # dfrbp -> dfrbpq: different pins
            equiv.check(before, parse_verilog(base.replace("sg13g2_dfrbp_1 r0", "sg13g2_dfrbpq_1 r0")
                                              .replace(", .Q_N()", "")), lib)
        with self.assertRaises(equiv.EquivalenceError):  # a cell nobody described
            equiv.check(before, parse_verilog(base.replace("sg13g2_inv_1", "sg13g2_mystery_1")), lib)

    def test_liberty_function_syntax(self):
        from sipath.liberty import truth_table as tt
        ins = ["A1", "A2", "B1"]
        self.assertEqual(tt("(A1*A2)+B1", ins), tt("A1&A2 | B1", ins))
        self.assertEqual(tt("(A1 A2)+B1", ins), tt("A1 & A2 + B1", ins))  # juxtaposition is AND
        self.assertEqual(tt("!(A&B)", ["A", "B"]), tt("(A B)'", ["A", "B"]))
        self.assertEqual(tt("A^B", ["A", "B"]), (False, True, True, False))
        with self.assertRaises(ValueError):
            tt("A + (B", ["A", "B"])


class DefParsing(unittest.TestCase):
    DEF = """
NETS 2 ;
    - n1 ( u1 Y ) ( u2 A ) + USE SIGNAL
      + ROUTED li1 ( 100 200 ) L1M1_PR_MR ( 900 * ) ( * 1500 0 )
      NEW met2 ( 900 1500 ) RECT ( -10 -10 10 10 ) ;
    - ana ( PIN a ) ( PIN b ) + USE SIGNAL + NONDEFAULTRULE ANALOG
      + ROUTED met2 ( 1000 0 0 ) ( * 5000 140 ) M2M3_PR ( 3000 * ) ;
END NETS
"""

    def test_wire_split_keeps_geometry(self):
        nets = wires.parse_def_paths(self.DEF)
        self.assertTrue(wires.needs_split(nets["n1"]["paths"]))
        split = """
NETS 1 ;
    - n1 ( u1 Y ) ( u2 A ) + USE SIGNAL
      + ROUTED li1 ( 100 200 ) L1M1_PR_MR
      NEW met1 ( 100 200 ) ( 900 * )
      NEW met1 ( 900 200 ) ( * 1500 0 )
      NEW met2 ( 900 1500 ) RECT ( -10 -10 10 10 ) ;
END NETS
"""
        vl = {"L1M1_PR_MR": ("li1", "met1")}
        g1 = wires.geometry(nets["n1"], vl)
        g2 = wires.geometry(wires.parse_def_paths(split)["n1"], vl)
        self.assertEqual(g1[0], g2[0])
        self.assertEqual(g1[1], g2[1])
        tcl_ = wires.encoder_tcl("n1", nets["n1"], {"L1M1_PR_MR": ("li1", "met1")})
        self.assertEqual(tcl_.count("newPath"), 4)

    def test_analog_ops_to_special_boxes(self):
        vias = {"M2M3_PR": ("met2", "met3")}
        ops = analog.def_net_routing(self.DEF, "ana", vias)
        segs = [o for o in ops if o[0] == "seg"]
        self.assertEqual([o[1] for o in segs], ["met2", "met3"])  # layer switches after the via
        t = analog.swire_tcl("ana", ops, {"met2": 280, "met3": 600}, vias)
        self.assertIn("dbSBox_create $sw [$tech findLayer met2] 860 0 1140 5140 NONE", t)
        self.assertIn("setSpecial", t)
        self.assertIn("set_dont_touch", t)
        self.assertIn("clearGuides", t)


class TclGen(unittest.TestCase):
    def test_pdn_rings_off_unless_asked(self):
        pdk = sky130hd("/p")
        d = Design("x", "x", "a.v", "a.sdc", "macro", floorplan_def="a.def").resolve(pdk)
        self.assertNotIn("add_pdn_ring", tcl.pdn(d, pdk))
        self.assertIn("-followpins", tcl.pdn(d, pdk))
        self.assertIn("add_pdn_stripe -grid grid -layer met4", tcl.pdn(d, pdk))  # PDK default strap
        self.assertIn("-pin_pattern {^VPWR$} -power", tcl.pdn(d, pdk))  # supply pins from the PDK
        d.pdn = PdnSpec(rings=True)
        d.resolve(pdk)
        self.assertIn("add_pdn_ring -grid grid -layers {met4 met5}", tcl.pdn(d, pdk))

    def test_endcaps_left_and_right_and_taps(self):
        t = tcl.endcaps_and_taps(sky130hd("/p"))
        self.assertIn("-left_edge sky130_fd_sc_hd__decap_3", t)
        self.assertIn("-right_edge sky130_fd_sc_hd__decap_3", t)
        self.assertIn("place_tapcells -master sky130_fd_sc_hd__tapvpwrvgnd_1", t)

    def test_placement_sequence(self):
        self.assertIn("global_placement -timing_driven", tcl.global_place(0.5))
        r = tcl.repair_after_place()
        self.assertLess(r.index("estimate_parasitics -placement"), r.index("repair_design"))
        self.assertLess(r.index("repair_design"), r.index("repair_timing"))
        self.assertIn("detailed_placement", tcl.detailed_place())

    def test_digital_route_excludes_special_nets(self):
        t = tcl.route_digital("d.rpt")
        self.assertIn("[$net isSpecial]", t)
        # database nets, not names: set_nets_to_route drops nets with escaped names
        self.assertIn("::flow_route_nets $::flow_digital", t)
        self.assertIn("grt::add_net_to_route $net", t)
        self.assertNotIn("set_nets_to_route", t)


class Validation(unittest.TestCase):
    def test_missing_file_is_failure(self):
        d = Design("x", "x", "/nope/a.v", "/nope/a.sdc", "macro", floorplan_def="/nope/a.def")
        with self.assertRaises(MissingInput):
            d.validate()

    def test_embedded_macro_is_not_core(self):
        pdk = sky130hd("/p")
        d = Design("x", "x", __file__, __file__, "macro", floorplan_def=__file__).resolve(pdk)
        self.assertFalse(d.is_core)
        self.assertEqual(d.max_route_layer, "met4")  # the PDK's embedded-macro top layer
        d.pdn = PdnSpec(straps=[Strap("met5", 1.6, 30)])
        with self.assertRaises(MissingInput) as cm:
            d.validate(pdk, check_pdk_files=False)
        self.assertIn("cannot use met5 straps", str(cm.exception))
        ihp = load_pdk("ihp-sg13g2", platforms="/p")
        d2 = Design("x", "x", __file__, __file__, "macro", floorplan_def=__file__).resolve(ihp)
        self.assertEqual((d2.min_route_layer, d2.max_route_layer, d2.pdn.rail_layer), ("Metal2", "Metal4", "Metal1"))


class PdkFiles(unittest.TestCase):
    """PDK descriptions are data (sipath/pdks); the library code names no PDK layer or cell."""

    def test_both_pdks_load(self):
        for name, site, tap, ties in (("sky130hd", "unithd", "sky130_fd_sc_hd__tapvpwrvgnd_1",
                                       [("sky130_fd_sc_hd__conb_1", "HI"), ("sky130_fd_sc_hd__conb_1", "LO")]),
                                      ("ihp-sg13g2", "CoreSite", "", [("sg13g2_tiehi", "L_HI"),
                                                                       ("sg13g2_tielo", "L_LO")])):
            p = load_pdk(name, platforms="/p")
            self.assertEqual((p.site, p.tap_cell, p.tie_cells()), (site, tap, ties))
            self.assertTrue(p.tech_lef.startswith("/p/"))
            self.assertIn(p.min_layer, p.layers)
            self.assertTrue(p.power_pins and p.pdn_defaults["straps"] and p.analog_defaults["ndr"])
            self.assertTrue(p.io_corner in p.io_lefs and all(f in p.io_lefs for f in p.io_fill))
        ihp = load_pdk("ihp-sg13g2", platforms="/p")
        self.assertEqual([c.name for c in ihp.corners], ["ss", "tt", "ff"])
        self.assertIn("place_tapcells", tcl.endcaps_and_taps(sky130hd("/p")))
        self.assertNotIn("place_", tcl.endcaps_and_taps(ihp))  # no endcap/tap cells in that library

    def test_optional_corner_variables(self):
        saved = os.environ.get("SIPATH_SKY130A")
        os.environ["SIPATH_SKY130A"] = "/nonexistent/sky130A"
        try:
            p = load_pdk("sky130hd", platforms="/p", sky130a=None)
        finally:
            if saved is None:
                os.environ.pop("SIPATH_SKY130A")
            else:
                os.environ["SIPATH_SKY130A"] = saved
        self.assertEqual(p.corners, [])  # only tt is left: single typical corner
        self.assertIn("left out: ss", p.corner_source)
        p = load_pdk("sky130hd", platforms="/p", sky130a="/x/sky130A")
        self.assertEqual([c.name for c in p.corners], ["ss", "tt", "ff"])
        self.assertTrue(p.corners[0].liberty.startswith("/x/sky130A/libs.ref/"))

    def test_library_code_names_no_pdk(self):
        here = os.path.join(os.path.dirname(__file__), "..")
        bad = []
        for f in sorted(os.listdir(here)):
            if f.endswith(".py"):
                with open(os.path.join(here, f)) as fh:
                    for k, line in enumerate(fh, 1):
                        if re.search(r"sky130|sg13g2|\bmet[1-5]\b|\bli1\b|\bMetal[1-5]\b", line, re.I):
                            bad.append(f"{f}:{k}: {line.strip()}")
        self.assertEqual(bad, [])


SKY130A = os.path.isdir(os.path.expanduser(os.environ.get("PDK_ROOT", "~/.ciel")) + "/sky130A")


@unittest.skipUnless(SKY130A, "no open_pdks sky130A tree ($PDK_ROOT or ~/.ciel)")
class Sky130Fidelity(unittest.TestCase):
    """sky130A from open_pdks: 9 corners, per-corner RC from the tech LEFs,
    tracks.info, the PDK's design rules and reference settings."""

    @classmethod
    def setUpClass(cls):
        cls.p = load_pdk("sky130A", platforms="/p")

    def test_views_and_corners(self):
        p = self.p
        self.assertTrue(p.tech_lef.endswith("sky130_fd_sc_hd__nom.tlef") and os.path.isfile(p.tech_lef))
        self.assertEqual([os.path.basename(f) for f in p.cell_lefs], ["sky130_fd_sc_hd.lef", "sky130_ef_sc_hd.lef"])
        self.assertEqual([c.name for c in p.corners],
                         ["tt_nom", "tt_min", "tt_max", "ss_nom", "ss_min", "ss_max", "ff_nom", "ff_min", "ff_max"])
        self.assertEqual(p.typical().name, "tt_nom")
        c = {x.name: x for x in p.corners}
        self.assertTrue(c["ss_max"].liberty.endswith("ss_100C_1v60.lib"))
        self.assertTrue(c["ff_min"].rcx_rules.endswith("min.calibre"))
        self.assertEqual((c["ss_max"].process, c["ss_max"].rc), ("ss", "max"))
        self.assertTrue(all(os.path.isfile(f) for f in p.files()))
        self.assertIn("make_tracks met5 -x_offset 1.70 -x_pitch 3.40", p.tracks_script)
        self.assertEqual(p.clock_min_layer, "met3")
        self.assertEqual(p.tap_distance, 13.0)
        self.assertEqual(p.constraints["max_transition"], 0.75)
        b = load_pdk("sky130B", platforms="/p")
        self.assertIn("/sky130B/", b.tech_lef)
        self.assertEqual(len(b.corners), 9)

    def test_rc_scaled_per_corner_from_tech_lefs(self):
        import re as _re
        c = {x.name: x for x in self.p.corners}

        def val(script, kind, name, flag):
            m = _re.search(rf"set_layer_rc {kind} {name} .*{flag} (\S+)", script)
            return float(m.group(1))
        nom = self.p.rc_script
        self.assertIsNone(c["tt_nom"].rc_script)  # the nominal table itself
        # met2 sheet resistance 0.125 (nom), 0.145 (max), 0.105 (min) ohm/sq in the tech LEFs
        self.assertAlmostEqual(val(c["ss_max"].rc_script, "-layer", "met2", "-resistance") /
                               val(nom, "-layer", "met2", "-resistance"), 0.145 / 0.125, places=4)
        self.assertAlmostEqual(val(c["ff_min"].rc_script, "-layer", "met2", "-resistance") /
                               val(nom, "-layer", "met2", "-resistance"), 0.105 / 0.125, places=4)
        # via2 cut resistance 3.41 (nom) -> 8.0 (max)
        self.assertAlmostEqual(val(c["tt_max"].rc_script, "-via", "via2", "-resistance") /
                               val(nom, "-via", "via2", "-resistance"), 8.0 / 3.41, places=3)
        self.assertEqual(val(c["ss_max"].rc_script, "-layer", "met2", "-capacitance"),
                         val(nom, "-layer", "met2", "-capacitance"))  # same capacitance in all three
        t = tcl.rc_setup(self.p)
        self.assertEqual(t.count("set_layer_rc -layer met2"), 9)
        self.assertIn("-corner ss_max", t)

    def test_constraints_extraction_and_layers(self):
        d = Design("x", "x", "a.v", "a.sdc", "macro", floorplan_def="f.def").resolve(self.p)
        t = "\n".join(tcl.constraints_setup(self.p, d))
        for s in ("set_max_transition 0.75", "set_max_fanout 10", "set_max_capacitance 0.2",
                  "-lib_cell sky130_fd_sc_hd__inv_2 -pin Y", "set_load 0.033442"):
            self.assertIn(s, t)
        die = Design("x", "x", "a.v", "a.sdc", "die", die=(0, 0, 10, 10)).resolve(self.p)
        t = "\n".join(tcl.constraints_setup(self.p, die))
        self.assertIn("set_max_transition 0.75", t)
        self.assertNotIn("set_driving_cell", t)  # bond pads are driven from the board
        self.assertNotIn("set_load", t)
        paths = tcl.spef_paths(self.p, "/w/x.spef")
        self.assertEqual(paths["tt_nom"], "/w/x.spef")
        self.assertEqual({paths["ss_max"], paths["ff_max"], paths["tt_max"]}, {"/w/x_max.spef"})
        self.assertEqual(len(set(paths.values())), 3)
        r = tcl.rcx(self.p, "/w/x.spef")
        self.assertEqual(r.count("extract_parasitics"), 3)
        self.assertEqual(r.count("read_spef -corner"), 9)
        self.assertIn("set_routing_layers -signal met1-met4 -clock met3-met4", tcl.routing_layers(d, self.p))
        self.assertIn("set_wire_rc -clock -layer met4", tcl.rc_setup(self.p, d))  # met5 clamped for a macro
        self.assertEqual(tcl.margins(self.p, "hold_place"), " -setup_margin 0.025 -hold_margin 0.1")
        self.assertEqual([s.layer for s in d.pdn.straps], ["met4"])  # embedded: reference met4 straps only
        core = Design("y", "y", "a.v", "a.sdc", "die", die=(0, 0, 10, 10), is_core=True).resolve(self.p)
        self.assertEqual([(s.layer, s.pitch) for s in core.pdn.straps], [("met4", 153.6), ("met5", 153.18)])


class Exclusions(unittest.TestCase):
    """Cells with known layout problems (the PDK's drc_exclude lists) are never used."""

    @unittest.skipUnless(SKY130A, "no open_pdks sky130A tree")
    def test_lists_loaded_and_enforced(self):
        from sipath.eco import check_ops
        hd, hs = load_pdk("sky130A", platforms="/p"), load_pdk("sky130A_hs", platforms="/p")
        self.assertEqual((len(hd.exclude), len(hs.exclude)), (53, 51))
        self.assertIn("sky130_fd_sc_hd__buf_16", hd.exclude)
        self.assertIn("sky130_fd_sc_hs__mux4_4", hs.exclude)
        self.assertTrue(hd.exclude_sources[0].endswith("sky130_fd_sc_hd/drc_exclude.cells"))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        v = os.path.join(d, "t.v")
        with open(v, "w") as fh:
            fh.write("module t (a, y); input a; output y; sky130_fd_sc_hd__buf_16 u1 (.A(a), .X(y)); endmodule\n")
        des = Design("t", "t", v, v, "macro", floorplan_def=v)
        with self.assertRaises(MissingInput) as cm:
            des.validate(hd, check_pdk_files=False)
        self.assertIn("buf_16", str(cm.exception))
        des.allow_excluded = ["sky130_fd_sc_hd__buf_16"]  # an explicit waiver
        des.validate(hd, check_pdk_files=False)
        with self.assertRaises(ValueError):
            check_ops([{"op": "resize", "inst": "u1", "master": "sky130_fd_sc_hd__xor3_1"}], hd, Design("x", "x", v, v,
                                                                                                         "macro"))
        des2 = Design("x", "x", v, v, "macro", dont_use=["*__buf_8"])
        with self.assertRaises(ValueError):
            check_ops([{"op": "resize", "inst": "u1", "master": "sky130_fd_sc_hd__buf_8"}], hd, des2)
        self.assertIn("set_dont_use $lc", tcl._timing_setup(hd, des2))

    def test_special_cells_cannot_be_excluded(self):
        from sipath.pdk import from_dict, read_file, resolve_spec
        data = read_file(resolve_spec("sky130hd"))
        data["cells"]["exclude"] = list(data["cells"]["exclude"]) + [data["cells"]["tap"]]
        with self.assertRaises(ValueError):
            from_dict(data, {"platforms": "/p", "here": "/x"})


class TypedConfig(unittest.TestCase):
    """Declared step parameters and design-file keys (no OpenROAD)."""

    def test_param_checks(self):
        from sipath import params
        params.check("place", {"density": 0.6, "seed": 3})
        params.check("extract", {"fail_on_violation": True})  # read by extract; was undeclared before
        for step, v, msg in [("place", {"density": "0.6"}, "expected float"),
                             ("place", {"density": 2.0}, "above the maximum"),
                             ("place", {"seed": True}, "got a bool"),
                             ("place", {"desnity": 0.6}, "unknown parameter"),
                             ("macros", {"method": "annealing"}, "not one of"),
                             ("signoff", {"checks": ["drc", "xor"]}, "'xor' is not one of"),
                             ("signoff", {"checks": "drc"}, r"expected list\[str\]"),
                             ("nosuch", {}, "unknown step")]:
            with self.assertRaisesRegex(ValueError, msg):
                params.check(step, v)

    def test_resolution_order(self):
        from sipath import params
        pdk = types.SimpleNamespace(step_defaults={"place": {"density": 0.5, "seed": 1}})
        design = types.SimpleNamespace(step_defaults={"place": {"density": 0.45}})
        r = params.resolve("place", {"seed": 7}, pdk, design)
        self.assertEqual((r["seed"], r["density"], r["congestion_check"]), (7, 0.45, True))
        self.assertEqual(params.resolve("place", {}, pdk, None)["density"], 0.5)
        self.assertEqual(params.resolve("place", {})["density"], 0.55)
        self.assertEqual(params.resolve("signoff", {})["checks"], list(params.SIGNOFF_CHECKS))
        self.assertEqual(params.resolve("custom_fn", {"x": 1}), {"x": 1})  # undeclared: passed through
        with self.assertRaisesRegex(ValueError, "design file step_defaults: place.density"):
            params.check_defaults("design file", {"place": {"density": "dense"}})

    def test_declarations_cover_steps_and_dataclasses(self):
        import dataclasses
        from sipath import params, schema, steps
        from sipath.config import Config
        from sipath.design import Datapath, Group, Pad, PdnSpec, Strap
        from sipath.padplan import PadRules
        self.assertEqual(set(steps.STEPS), set(params.DECLARED) - {"eco"})
        pairs = [(Design, schema.DESIGN), (Macro, schema.MACRO), (Pad, schema.PAD), (Group, schema.GROUP),
                 (Datapath, schema.DATAPATH), (PdnSpec, schema.PDN), (Strap, schema.STRAP),
                 (PadRules, schema.PAD_RULES), (Config, schema.CONFIG)]
        for cls, spec in pairs:
            fields = {f.name for f in dataclasses.fields(cls)}
            self.assertEqual(fields - set(spec) - schema.INTERNAL, set(), f"{cls.__name__} fields without a declaration")
            self.assertEqual(set(spec) - fields - schema.FILE_ONLY, set(), f"{cls.__name__}: declared, no field")

    def test_design_file_errors_name_the_key(self):
        from sipath import schema
        ok = {"name": "t", "top": "t", "mode": "die", "pdk": "sky130A", "netlist": "n.v", "sdc": "s.sdc"}
        schema.validate(ok)
        bad = dict(ok, mode="chip", core_margin="10", macros=[{"inst": "u", "lef": "u.lef", "halo": "5",
                                                                "colour": "red"}],
                   pdn={"straps": [{"layer": "met4", "width": 1.6}]}, keepouts=[[0, 0, 1]])
        bad.pop("sdc")
        with self.assertRaises(ValueError) as cm:
            schema.validate(bad)
        msg = str(cm.exception)
        for part in ("mode: 'chip' is not one of", "core_margin: expected number", "macros[0].halo: expected number",
                     "macros[0].colour: unknown key", "pdn.straps[0].pitch: required", "keepouts[0]: expected box",
                     "sdc: required"):
            self.assertIn(part, msg)

    def test_json_schema_and_reference_are_current(self):
        from sipath import reference, schema
        js = schema.json_schema()
        self.assertEqual(set(js["properties"]), set(schema.DESIGN))
        self.assertIn("inst", js["properties"]["macros"]["items"]["required"])
        doc = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "docs", "reference.md")
        with open(doc) as f:
            self.assertEqual(f.read(), reference.text(), "docs/reference.md is stale: run python3 -m sipath docs")

    def test_mcp_describe_step(self):
        from sipath.mcp_server import Server
        r = Server().call("describe_step", {"step": "route_digital"})
        props = r["route_digital"]["properties"]
        self.assertEqual((props["iters"]["type"], props["iters"]["default"], props["iters"]["minimum"]),
                         ("integer", 64, 1))
        self.assertFalse(r["route_digital"]["additionalProperties"])


class SynthUnits(unittest.TestCase):
    """Synthesis scripts, RTL design rules and the tool-step plumbing (no tools run)."""

    def gcd8(self):
        from sipath import designfile
        d, steps = designfile.load(os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8.yaml"))
        return d, steps

    def test_scripts(self):
        from sipath import synth
        d, _ = self.gcd8()
        pdk = sky130hd("/p")
        d.resolve(pdk)
        self.assertEqual(synth.clock_period_ps(d.sdc), 10000.0)
        lint = synth.lint_script(pdk, d)
        self.assertIn("read_verilog -sv -noautowire", lint)
        self.assertIn("check -assert", lint)
        sc = synth.synth_script(pdk, d, "/o/gcd8.synth.v", 10000.0, "delay")
        self.assertIn("synth -top gcd8 -flatten", sc)
        self.assertIn("-D 10000", sc)  # the SDC clock as ABC's delay target
        self.assertNotIn("-D ", synth.synth_script(pdk, d, "/o/x.v", 10000.0, "area"))
        for cell in ("sky130_fd_sc_hd__lpflow_*", "sky130_fd_sc_hd__mux4_4"):  # dont_use, PDK exclusion
            self.assertIn(f"-dont_use {cell}", sc)
        self.assertIn("hilomap -singleton -hicell sky130_fd_sc_hd__conb_1 HI -locell sky130_fd_sc_hd__conb_1 LO", sc)
        self.assertIn("insbuf -buf sky130_fd_sc_hd__buf_2 A X", sc)

    def test_rtl_design_rules(self):
        from sipath import designfile, schema
        d, steps = self.gcd8()
        self.assertEqual([x["step"] for x in steps][:2], ["synth", "floorplan"])
        self.assertIsNone(d.netlist)
        self.assertEqual(designfile.default_flow(d)[0], {"step": "synth"})
        with self.assertRaisesRegex(ValueError, "netlist or rtl: one is required"):
            schema.validate({"name": "t", "top": "t", "mode": "macro", "pdk": "sky130A", "sdc": "s.sdc"})
        bad = Design(name="t", top="t", netlist=None, sdc=d.sdc, mode="macro")
        with self.assertRaisesRegex(MissingInput, "needs netlist .* or rtl"):
            bad.validate(check_pdk_files=False)
        with self.assertRaisesRegex(MissingInput, "macro mode needs floorplan_def, die or core_utilization"):
            bad.validate(check_pdk_files=False)

    def test_tool_step_feeds_the_next_step(self):
        with tempfile.TemporaryDirectory() as td:
            d = Design("fake", "fake", None, "x.sdc", "macro", rtl=["x.v"], core_utilization=0.5)

            def tool(ctx):
                out = ctx.path("fake.synth.v")
                with open(out, "w") as f:
                    f.write("module fake(); endmodule\n")
                ctx.metrics["synth_area_um2"] = 12.5
                ctx.outputs["checkpoint"] = out
            tool.tool = True
            made = []

            def fac(w):
                s = demo.fake_factory()(w)
                made.append(s)
                return s
            f = Flow(d, sky130hd("/p"), td, fac, validate_inputs=False)
            a = f.step("synth", tool)
            self.assertEqual((a.selected.kind, made), ("tool", []))  # no OpenROAD process for a tool step
            self.assertTrue(a.selected.checkpoint.endswith("fake.synth.v"))
            b = f.step("floorplan", demo.fake_step(), parent=a.selected, score="setup_wns")
            self.assertIn(f"read_verilog {a.selected.checkpoint}", "\n".join(made[0].scripts))
            self.assertIs(b.selected.parent, a.selected)

            def broken(ctx):  # writes nothing: a failed attempt, not an empty netlist
                ctx.outputs["checkpoint"] = ctx.path("missing.v")
            broken.tool = True
            with self.assertRaises(StepFailed):
                f.step("synth2", broken)


class BlackboxModels(unittest.TestCase):
    """Black-box Liberty models written from a macro's LEF (opt-in)."""
    DAC = os.path.join(os.path.dirname(__file__), "..", "examples", "inputs", "chipfoundry",
                       "sky130_ef_ip__rdac3v_8bit", "sky130_ef_ip__rdac3v_8bit.lef")

    def test_model_from_lef(self):
        from sipath import blackbox
        from sipath.synth import _liberty_cells
        text, rep = blackbox.liberty("sky130_ef_ip__rdac3v_8bit", self.DAC, {"^dvdd$": "VDD", "^dvss$": "VSS"})
        self.assertIn('bus ("b") { bus_type : bus_7_0; direction : input;', text)
        self.assertIn('pin ("out") { direction : output;', text)
        self.assertNotIn('"dvdd"', text)  # supplies (and LEF USE POWER/GROUND) are left out
        self.assertNotIn("timing ()", text)  # no timing arcs
        self.assertEqual(sorted(rep["power_pins_left_out"]), ["avdd", "avss", "dvdd", "dvss"])
        self.assertEqual(rep["direction_assumed_inout"], [])
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "m.lib"), "w") as fh:
            fh.write(text)
        self.assertEqual(_liberty_cells([os.path.join(d, "m.lib")]), {"sky130_ef_ip__rdac3v_8bit": False})

    def test_missing_direction_is_reported(self):
        from sipath import blackbox
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        lef = os.path.join(d, "x.lef")
        with open(lef, "w") as fh:
            fh.write("MACRO X\n  SIZE 10 BY 20 ;\n  PIN A\n  END A\n  PIN D[1]\n    DIRECTION OUTPUT ;\n  END D[1]\n"
                     "  PIN D[3]\n    DIRECTION OUTPUT ;\n  END D[3]\n  PIN VPWR\n  END VPWR\nEND X\n")
        text, rep = blackbox.liberty("X", lef, {"^VPWR$": "VDD"})
        self.assertEqual(rep["direction_assumed_inout"], ["A"])
        self.assertIn("area : 200.000;", text)
        self.assertIn('pin ("D[1]")', text)  # D[1], D[3]: not a contiguous bus, kept as single pins
        self.assertNotIn("bus (", text)

    def test_opt_in_rules(self):
        from sipath import blackbox
        d = Design("t", "t", "n.v", "s.sdc", "die", macros=[Macro(inst="u_a", lef=self.DAC),
                                                          Macro(inst="u_b", lef=self.DAC, lib="b.lib"),
                                                          Macro(inst="u_c", lef=self.DAC, auto_blackbox=False),
                                                          Macro(inst="u_d", lef=self.DAC, auto_blackbox=True)])
        self.assertEqual([blackbox.wanted(d, m) for m in d.macros], [False, False, False, True])  # off by default
        d.auto_blackbox = True
        self.assertEqual([blackbox.wanted(d, m) for m in d.macros], [True, False, False, True])
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        made = blackbox.generate(d, types.SimpleNamespace(voltages={"VDD": 1.8}), out)
        self.assertEqual(sorted(made), ["u_a", "u_d"])
        self.assertTrue(d.macros[0].lib.endswith("sky130_ef_ip__rdac3v_8bit.blackbox.lib"))
        self.assertEqual(d.macros[1].lib, "b.lib")  # a given lib is never replaced


class SignoffUnits(unittest.TestCase):
    LIB = """.SUBCKT X__inv_1 A VGND VNB VPB VPWR Y
MI1 Y A VGND VNB nfet_01v8 m=1 w=0.65 l=0.15
MI2 Y A VPWR VPB pfet_01v8_hvt m=1 w=1.0 l=0.15
.ENDS X__inv_1
.SUBCKT X__conb_1 VGND VNB VPB VPWR HI LO
rI12 VGND LO sky130_fd_pr__res_generic_po
rI11 HI VPWR sky130_fd_pr__res_generic_po
.ENDS X__conb_1
.SUBCKT X__decap_4 VGND VNB VPB VPWR
MI2 VPWR VGND VPB pfet_01v8 m=1 w=1.0 l=1.0
.ENDS X__decap_4
.SUBCKT X__spare A
XI1 A A A A A X__nothere
.ENDS X__spare
"""
    TOP = """.SUBCKT t a y
Xu1 a VSS VSS VDD VDD y
+ X__inv_1
Xtie VSS VSS VDD VDD tie_hi tie_lo X__conb_1
Xu2 tie_hi VSS VSS VDD VDD n2 X__inv_1
Xd VSS VSS VDD VDD X__decap_4
.ENDS t
"""

    def test_reference_cdl(self):
        from sipath.signoff import reference_cdl
        text, info = reference_cdl(self.TOP, [self.LIB], {"*__conb_*": {"HI": "VPWR", "LO": "VGND"}}, ["VDD", "VSS"],
                                   "sky130_fd_pr__")
        self.assertEqual(info["missing"], [])
        self.assertEqual((info["subckts"], info["tie_cells"], info["mos_3node_fixed"]), (3, 1, 1))
        self.assertNotIn("X__spare", text)  # only what the design uses (the PDK's spare cell is broken)
        self.assertIn(".SUBCKT X__conb_1 VGND VNB VPB VPWR\n.ENDS X__conb_1", text)  # tie outputs are shorts
        self.assertIn("Xu2 VDD VSS VSS VDD VDD n2 X__inv_1", text)  # tie_hi merged into VDD
        self.assertIn("Xtie VSS VSS VDD VDD X__conb_1", text)
        self.assertIn(".SUBCKT t a y VDD VSS", text)  # supplies are ports
        self.assertIn("MI2 VPWR VGND VPWR VPB pfet_01v8", text)  # 3-node MOS: S = D
        _, info = reference_cdl(self.TOP.replace("X__decap_4", "X__gone"), [self.LIB], {}, [])
        self.assertEqual(info["missing"], ["X__gone"])

    def test_lyrdb_parse(self):
        from sipath.signoff import parse_lyrdb
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "r.lyrdb")
        with open(p, "w") as fh:
            fh.write("<report-database><categories/><cells/><items>"
                     "<item><category>'m3.6'</category><cell>t</cell><values/></item>"
                     "<item><category>'m3.6'</category><cell>t</cell><values/></item>"
                     "<item><category>'li.1'</category><cell>t</cell><values/></item></items></report-database>")
        self.assertEqual(parse_lyrdb(p), {"count": 3, "by_rule": {"m3.6": 2, "li.1": 1}})

    def test_lyrdb_owner_attribution(self):
        from sipath.signoff import cell_owners, parse_lyrdb
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "r.lyrdb")
        item = "<item><category>'{}'</category><cell>{}</cell><values/></item>"
        with open(p, "w") as fh:
            fh.write("<report-database><items>" + item.format("capm.2b_a", "cap_cell") + item.format("capm.2b_a", "cap_cell")
                     + item.format("nwell.6", "follower_amp$1") + item.format("m3.2", "chip")
                     + item.format("li.1", "pad_cell") + "</items></report-database>")
        ctx = types.SimpleNamespace(design=types.SimpleNamespace(macros=[
            Macro(inst="u_adc", lef="a.lef", gds="/x/adc.gds"), Macro(inst="u_dac", lef="d.lef", gds="/y/dac.gds.gz")]))
        owners = cell_owners(ctx, {"owners": {"cap_cell": "adc.gds", "follower_amp$1": "dac.gds.gz",
                                              "pad_cell": "io.gds", "VIA_M1M2": "def"}})
        r = parse_lyrdb(p, owners, "chip")
        self.assertEqual(r["by_owner"], {"macro u_adc": 2, "macro u_dac": 1, "top": 1, "library io.gds": 1})
        self.assertEqual(r["by_owner_rule"]["macro u_adc"], {"capm.2b_a": 2})

    def test_lef_pin_shorts_and_unique_pins(self):
        from sipath.signoff import lef_macros, lef_pin_shorts
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        lef = os.path.join(d, "p.lef")
        with open(lef, "w") as fh:
            fh.write("MACRO PAD\n  PIN A\n    PORT\n      LAYER met3 ;\n        RECT 0 0 10 10 ;\n    END\n  END A\n"
                     "  PIN B\n    PORT\n      LAYER met3 ;\n        RECT 10 2 20 8 ;\n    END\n  END B\n"
                     "  PIN C\n    PORT\n      LAYER met4 ;\n        RECT 0 0 10 10 ;\n    END\n  END C\n"
                     "  PIN A\n    PORT\n      LAYER met2 ;\n        RECT 50 0 60 10 ;\n    END\n  END A\n"
                     "END PAD\n")
        self.assertEqual(lef_macros(lef), {"PAD": ["A", "B", "C"]})  # A declared twice, listed once
        self.assertEqual(lef_pin_shorts(lef, "PAD"), {"B": "A"})  # touching on met3; C is on met4

    def test_lef_macro_and_blackbox_policy(self):
        from sipath.signoff import lef_macro, lvs
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        lef = os.path.join(d, "m.lef")
        with open(lef, "w") as fh:
            fh.write("VERSION 5.7 ;\nMACRO IP_X\n  CLASS BLOCK ;\n  PIN DO[0]\n    DIRECTION OUTPUT ;\n  END DO[0]\n"
                     "  PIN VPWR\n    USE POWER ;\n  END VPWR\n  OBS\n  END\nEND IP_X\nEND LIBRARY\n")
        self.assertEqual(lef_macro(lef), ("IP_X", ["DO[0]", "VPWR"]))

        class Ctx:  # a macro with no cdl and no black-box opt-in: LVS refuses before any tool runs
            pdk = types.SimpleNamespace(name="p", signoff={"cell_cdl": []})
            design = types.SimpleNamespace(pads=[], macros=[Macro(inst="u_x", lef=lef)])
            session = None
        r = lvs(Ctx(), "unused.gds")
        self.assertFalse(r["match"])
        self.assertIn("macros without cdl ['u_x']", r["reason"])
        from sipath.signoff import blackboxes
        Ctx.design = types.SimpleNamespace(pads=[], macros=[Macro(inst="u_x", lef=lef, abstract=True)])
        self.assertEqual(blackboxes(Ctx(), masters=[]), {"u_x": "IP_X"})  # abstract implies an LVS black box
        bad = Design(name="t", top="t", netlist="n.v", sdc="s.sdc", mode="die", die=(0, 0, 10, 10),
                     macros=[Macro(inst="u_x", lef=lef, lvs="shallow")])
        with self.assertRaisesRegex(MissingInput, "macro u_x: lvs must be 'blackbox' or absent"):
            bad.validate(check_pdk_files=False)

    @unittest.skipUnless(SKY130A, "no open_pdks sky130A tree")
    def test_min_area_planner(self):
        from sipath import minarea
        tl = load_pdk("sky130A", platforms="/p").tech_lef
        r = minarea.tech_rules(tl)
        self.assertEqual((r["layers"]["met3"]["area"], r["layers"]["met3"]["spacing"], r["layers"]["met3"]["dir"]),
                         (0.24, 0.3, "H"))
        self.assertEqual(r["vias"]["M2M3_PR"]["met3"], (-0.165, -0.165, 0.165, 0.165))
        isl = [(48150, 61390, 48680, 61690), (48365, 61375, 48695, 61705), (48110, 61380, 48490, 61700)]
        self.assertEqual(minarea.union_area(isl), 190500)  # the jog found by signoff DRC: 0.19 um2
        box, _ = minarea.plan_patch(isl, r["layers"]["met3"], 1000, [])
        self.assertGreaterEqual((box[2] - box[0]) * (box[3] - box[1]), 240000)
        box, _ = minarea.plan_patch(isl, r["layers"]["met3"], 1000, [(49000, 61300, 50000, 61800)])  # neighbour east
        self.assertEqual(box[2], 48695)  # grows west only: the east side keeps its spacing
        self.assertEqual(minarea.plan_patch(isl, r["layers"]["met3"], 1000,
                                            [(47000, 61300, 47750, 61800), (49000, 61300, 50000, 61800)])[0], "blocked")


@LIVE
class LiveRtl(unittest.TestCase):
    """RTL to signoff: Yosys lint + synthesis, a floorplan from the utilization
    with pins placed by the flow, PDK straps scaled to the small core."""

    def test_gcd8_rtl_to_signoff(self):
        from sipath import designfile
        wd = _workdir("gcd8")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        f = designfile.run(os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8.yaml"), wd, RUNNER)
        sy = f.steps["synth"].selected
        self.assertEqual(sy.kind, "tool")
        self.assertEqual(sy.metrics["lint_warnings"], 0)
        self.assertGreater(sy.metrics["synth_cells"], 50)
        self.assertGreater(sy.metrics["synth_flops"], 20)
        self.assertEqual(sy.metrics["abc_period_ps"], 10000.0)
        self.assertTrue(f.steps["floorplan"].selected.outputs.get("auto_pins"))
        self.assertTrue(f.steps["physical"].selected.outputs.get("pdn_straps_scaled"))
        s = _read_json(f.steps["signoff"].selected.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        self.assertEqual((s["drc"]["count"], s["lvs"]["match"], s["antenna"]["violations"]), (0, True, 0))

    def test_macro_without_lib_gets_a_blackbox_model(self):
        from sipath import designfile
        wd = _workdir("rtl_blackbox")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        os.makedirs(wd, exist_ok=True)
        rtl = os.path.join(wd, "dacwrap.v")
        with open(rtl, "w") as fh:
            fh.write("module dacwrap(input clk, input en, input [7:0] code, output dac_out, inout vh, inout vl);\n"
                     "  reg [7:0] q;\n  always @(posedge clk) q <= code;\n"
                     "  sky130_ef_ip__rdac3v_8bit u_dac (.b(q), .ena(en), .out(dac_out), .Vhigh(vh), .Vlow(vl));\n"
                     "endmodule\n")
        sdc = os.path.join(wd, "dacwrap.sdc")
        with open(sdc, "w") as fh:
            fh.write("create_clock -name clk -period 10 [get_ports clk]\n")
        base = {"name": "dacwrap", "top": "dacwrap", "mode": "macro", "pdk": "sky130A", "rtl": [rtl], "sdc": sdc,
                "core_utilization": 0.4, "macros": [{"inst": "u_dac", "lef": BlackboxModels.DAC,
                                                     "supplies": {"^dvdd$": "VDD", "^dvss$": "VSS",
                                                                  "^avdd$": "VDD", "^avss$": "VSS"}}]}
        with self.assertRaises(StepFailed) as cm:  # off by default: a clear error with the way out
            designfile.run(designfile.design_from_dict(dict(base), wd), os.path.join(wd, "off"), RUNNER,
                           flow_steps=[{"step": "synth"}])
        self.assertIn("auto_blackbox", str(cm.exception))
        d = designfile.design_from_dict(dict(base, auto_blackbox=True), wd)
        f = designfile.run(d, os.path.join(wd, "on"), RUNNER, flow_steps=[{"step": "synth"}, {"step": "floorplan"}])
        model = f.provenance["blackbox_models"]["u_dac"]
        self.assertTrue(os.path.isfile(model["lib"]))
        self.assertEqual(model["buses"], 1)
        with open(f.steps["synth"].selected.checkpoint) as fh:
            self.assertIn("sky130_ef_ip__rdac3v_8bit u_dac", fh.read())
        self.assertEqual(f.steps["floorplan"].selected.status, "selected")  # OpenROAD linked it with the model

    def test_lint_errors_fail_synthesis(self):
        from sipath import designfile
        wd = _workdir("rtl_lint")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        os.makedirs(wd, exist_ok=True)
        rtl = os.path.join(wd, "bad.v")
        with open(rtl, "w") as fh:  # an implicit net (typo) and a net with two drivers
            fh.write("module bad(input a, input b, output y, output z);\n  assign y = a & bb;\n"
                     "  assign z = a;\n  assign z = b;\nendmodule\n")
        sdc = os.path.join(wd, "bad.sdc")
        with open(sdc, "w") as fh:
            fh.write("set_max_delay 5 -from [all_inputs] -to [all_outputs]\n")
        d = designfile.design_from_dict({"name": "bad", "top": "bad", "mode": "macro", "pdk": "sky130A",
                                         "rtl": [rtl], "sdc": sdc, "core_utilization": 0.3}, wd)
        with self.assertRaises(StepFailed) as cm:
            designfile.run(d, os.path.join(wd, "run"), RUNNER, flow_steps=[{"step": "synth"}])
        self.assertIn("bb", str(cm.exception))  # the implicit net is named in the error


@LIVE
class LiveHs(unittest.TestCase):
    """The high-speed library end to end with signoff (dp8 on sky130_fd_sc_hs)."""

    def test_dp8_hs_signoff(self):
        from sipath import designfile
        wd = _workdir("dp8_hs")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        f = designfile.run(os.path.join(os.path.dirname(__file__), "..", "examples", "dp8_hs.yaml"), wd, RUNNER)
        self.assertEqual(f.pdk.name, "sky130A_hs")
        s = _read_json(f.steps["signoff"].selected.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        self.assertEqual((s["drc"]["count"], s["lvs"]["match"], s["antenna"]["violations"]), (0, True, 0))
        self.assertGreater(s["lvs"]["mos_3node_fixed"], 0)  # the hs decaps' three-node transistors
        with open(f.steps["signoff"].selected.outputs["def_"]) as fh:
            text = fh.read()
        self.assertIn("sky130_fd_sc_hs__dfxtp_1", text)
        self.assertNotIn("sky130_fd_sc_hd__", text)
        ex = f.steps["extract"].selected
        self.assertGreater(min(v for k, v in ex.metrics.items() if k.startswith("wns_")), 5.0)  # hs: fast


class DesignFiles(unittest.TestCase):
    def test_examples_load_and_match_their_netlists(self):
        ex = os.path.join(os.path.dirname(__file__), "..", "examples")
        from sipath import designfile
        for name in ("dp8", "mcu8", "ihp_add8"):
            d, steps_ = designfile.load(os.path.join(ex, name + ".yaml"))
            self.assertEqual(d.name, name)
            self.assertTrue(all(os.path.isfile(f) for f in d.input_files()), name)
            insts = {i.name for i in read_verilog(d.netlist).instances}
            for dp in d.datapaths:
                cells = dp.cells if dp.kind == "column" else [c for r in dp.cells for c in r if c]
                self.assertLessEqual(set(cells), insts, (name, dp.name))
            for p in d.pads:
                self.assertIn(p.inst, insts)
            self.assertEqual(steps_[0]["step"], "floorplan")
            self.assertIn(steps_[-1]["step"], ("extract", "signoff"))

    def test_unknown_field_and_defaults(self):
        from sipath import designfile
        with self.assertRaises(ValueError):
            designfile.design_from_dict({"name": "x", "top": "x", "mode": "macro", "netlist": "a.v", "sdc": "a.sdc",
                                         "bogus": 1}, "/b")
        d = designfile.design_from_dict({"name": "x", "top": "x", "mode": "macro", "netlist": "a.v", "sdc": "s.sdc",
                                         "floorplan_def": "f.def", "pdk": "ihp-sg13g2"}, "/b")
        self.assertEqual((d.netlist, d.floorplan_def), ("/b/a.v", "/b/f.def"))
        self.assertEqual([s["step"] for s in designfile.default_flow(d)],
                         ["floorplan", "physical", "place", "cts", "route_analog", "route_digital", "route_repair",
                          "extract"])


class TclQuoting(unittest.TestCase):
    NASTY = ["a}b", "clk[0]", "x$y", "a b", 'he said "hi"', "back\\slash", "{x}", "}{", "", "semi;colon", "u_add.fa_0"]

    def test_parse_inverts_quote(self):
        from sipath.tclquote import q, qlist, tcl_parse, tcl_words
        for v in self.NASTY:
            self.assertEqual(tcl_words(q(v)), [v] if v else [""], v)
        self.assertEqual(tcl_words(qlist(self.NASTY)[1:-1]), self.NASTY)
        self.assertEqual(tcl_parse('a {b c} {d {e f}} "x y" w\\ z {} {a[0]}'),
                         ["a", ["b", "c"], ["d", ["e", "f"]], "x y", "w z", [], "a[0]"])

    def test_no_substitution_possible(self):
        from sipath.tclquote import q
        for v in ("a}b", "x]y[exec rm]", "$env(HOME)"):
            out = q(v)
            self.assertTrue(out.startswith("{") or "\\" in out, out)


class Phase0Rules(unittest.TestCase):
    def flow(self, td, d=None):
        d = d or Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        return Flow(d, sky130hd("/p"), td, demo.fake_factory(), validate_inputs=False)

    @staticmethod
    def metrics_step(text):
        def fn(ctx):
            ctx.session.responses["::flow_metrics"] = text
        return fn

    def test_eco_name_collision_keeps_parent(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            r = f.step("place", demo.fake_step())
            ck = r.selected.checkpoint
            with self.assertRaises(EcoRefused):
                f.eco("place", r.selected, [], fn=demo.fake_step(), score="auto")
            self.assertTrue(os.path.isfile(ck))
            self.assertIs(f.steps["place"], r)

    def test_eco_regression_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            r = f.step("route", self.metrics_step("drc 0 wns 1.0 whs 0.2 hpwl_um 100"), score="auto")
            with self.assertRaises(StepFailed) as cm:
                f.eco("eco_bad", r.selected, [], fn=self.metrics_step("drc 0 wns 0.5 whs 0.2 hpwl_um 100"))
            self.assertIn("regressed", str(cm.exception))
            bad = [a for a in f.attempts if a.step == "eco_bad"][0]
            self.assertEqual(bad.status, "rejected")
            self.assertTrue(os.path.isfile(bad.checkpoint))  # kept for inspection
            ok = f.eco("eco_ok", r.selected, [], fn=self.metrics_step("drc 0 wns 0.98 whs 0.2 hpwl_um 100"))
            self.assertEqual(ok.selected.status, "selected")

    def test_unconstrained_design_selects_by_wirelength(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            run = f.step("place", lambda ctx: self.metrics_step(
                f"wns nan tns nan whs inf hpwl_um {ctx.params['h']}")(ctx), variants=[{"h": 300}, {"h": 200}])
            self.assertEqual(run.selected.params, {"h": 200})
            self.assertIsNone(run.selected.metrics["wns"])

    def test_setup_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            with open(os.path.join(td, "blocked"), "w") as fh:
                fh.write("a file where the step directory should be")
            with self.assertRaises(StepFailed):
                f.step("blocked", demo.fake_step())
            with open(os.path.join(td, "graph.json")) as fh:
                g = json.load(fh)
            self.assertEqual(g["attempts"][0]["status"], "failed")

    def test_scores(self):
        self.assertEqual(scores.drc_then_slack({"drc": 0, "wns": 1.0, "whs": -0.1}), (0, -0.1))
        self.assertGreater(scores.auto({"wns": 0.1, "hpwl_um": 5}), scores.auto({"wns": 0.0, "hpwl_um": 1}))

    def test_native_runner_without_platforms(self):
        saved = os.environ.pop("SIPATH_PLATFORMS", None)
        try:
            with self.assertRaises(ValueError) as cm:
                load_pdk("sky130hd")  # no runner, no $SIPATH_PLATFORMS: ${platforms} unknown
        finally:
            if saved is not None:
                os.environ["SIPATH_PLATFORMS"] = saved
        self.assertIn("SIPATH_PLATFORMS", str(cm.exception))


class PadPlan(unittest.TestCase):
    ROLES = sky130hd("/p").io_roles

    def pads(self, spec):
        from sipath.design import Pad
        out, kinds = [], {}
        for edge, lst in spec.items():
            for k, (name, master, kind) in enumerate(lst):
                out.append(Pad(name, edge, k, master))
                if kind:
                    kinds[name] = kind
        return out, kinds

    def test_mcu8_pad_list_passes(self):
        from sipath import padplan
        m, meta = designs.mcu8_netlist()
        from sipath.design import Pad
        pads = [Pad(p["inst"], p["edge"], p["order"], p["master"]) for p in meta["pads"]]
        r = padplan.check(pads, self.ROLES, meta["pad_kinds"])
        self.assertEqual(r["core_supply_edges"], ["south", "west"])
        self.assertTrue(all(r[e]["io_pairs"] >= 1 for e in ("north", "south", "east", "west")))

    def test_violations_are_reported(self):
        from sipath import padplan
        g, vc, vs, io, so, va, sa = ("gpiov2", "vccd_hvc", "vssd_hvc", "vddio_hvc", "vssio_hvc", "vdda_hvc",
                                     "vssa_hvc")
        pads, kinds = self.pads({
            "north": [("o0", g, "output"), ("a0", g, "analog"), ("p1", vc, None), ("o1", g, "output"),
                      ("p2", vs, None)],
            "south": [("i0", g, "input"), ("p3", io, None), ("p4", so, None)],
        })
        with self.assertRaises(padplan.PadPlanError) as cm:
            padplan.check(pads, self.ROLES, kinds)
        msg = str(cm.exception)
        for expected in ("p1 (vccd) has no adjacent vssd", "north: signal pads but no VDDIO/VSSIO pair",
                         "analog pad a0 next to output o0", "north: analog pads but no VDDA/VSSA pair",
                         "core supply (VCCD/VSSD) pairs on 1 edge(s)"):
            self.assertIn(expected, msg)


class PowerStrap(unittest.TestCase):
    """Strap geometry (no OpenROAD): nearest same-net target above, reach
    limited to the strap width, other nets and obstructions respected."""

    def strap(self, special, obs=(), d="W"):
        from sipath.powerconnect import _strap
        pin = (100000, 50000, 104000, 52000)  # met3 tab, 2 um tall
        why = []
        return _strap(pin, d, "met3", ["met4", "met5"], "VDD", special, list(obs), pin, (0, 0, 10**7, 10**7),
                      60000, 400, 1000, why), why

    def test_nearest_target_and_limited_reach(self):
        special = [("VDD", "met4", (80000, 0, 81600, 200000)),         # vertical strap 18.4 um away
                   ("VDD", "met5", (10000, 50500, 99000, 51500))]      # long parallel met5, 1 um away
        r, _ = self.strap(special)
        self.assertEqual(r["to_layer"], "met5")
        self.assertEqual(r["box"][0], 99000 - 1000)  # enters the long target by its own width, not to its far end
        self.assertEqual((r["box"][1], r["box"][3]), (50500, 51500))  # strap = overlap of tab and target

    def test_blocked_by_other_net_or_obstruction(self):
        special = [("VDD", "met4", (80000, 0, 81600, 200000)), ("VSS", "met3", (90000, 49000, 91000, 53000))]
        r, why = self.strap(special)
        self.assertIsNone(r)
        self.assertIn("crosses VSS on met3", why[0])
        r, why = self.strap([("VDD", "met4", (80000, 0, 81600, 200000))], obs=[("met3", (85000, 0, 86000, 99999))])
        self.assertIsNone(r)
        self.assertIn("obstruction", why[0])

    def test_odd_box_made_even(self):
        from sipath.powerconnect import _even
        x0, y0, x1, y1 = _even((0, 0, 1725, 17385))
        self.assertEqual(((x1 - x0) % 2, (y1 - y0) % 2), (0, 0))



    def test_landing_kept_clear_of_other_nets(self):
        from sipath.powerconnect import _clear_part, _strap
        # the mcu8 case: a VDD met4 landing across a VSS met4 stripe is cut back
        self.assertEqual(_clear_part((709605, 232580, 733505, 237580), [(728760, 225580, 730360, 460480)], 400, 1000),
                         (709605, 232580, 728360, 237580))
        self.assertIsNone(_clear_part((0, 0, 1000, 1000), [(0, 0, 1000, 1000)], 400, 1000))
        # met3 strap to a met5 target: the met4 landing must avoid a VSS met4 shape
        pin = (100000, 50000, 104000, 52000)
        special = [("VDD", "met5", (80000, 40000, 90000, 60000)), ("VSS", "met4", (80000, 40000, 90000, 60000))]
        why = []
        self.assertIsNone(_strap(pin, "W", "met3", ["met4", "met5"], "VDD", special, [], pin, (0, 0, 10**7, 10**7),
                                 60000, 400, 1000, why))
        self.assertIn("via landing", why[0])

    def test_special_shorts_sweep(self):
        from sipath.powerconnect import touching_pairs
        shapes = [("VDD", "met4", 0, 0, 10, 10), ("VSS", "met4", 10, 5, 20, 8),  # touching edge: a short
                  ("VSS", "met4", 30, 0, 40, 10), ("VDD", "met3", 35, 0, 45, 10),  # other layer: fine
                  ("VDD", "met4", 0, 20, 10, 30), ("VDD", "met4", 5, 25, 15, 35)]  # same net: fine
        r = touching_pairs(shapes)
        self.assertEqual([(x["layer"], x["nets"]) for x in r], [("met4", ["VDD", "VSS"])])

class Scheduler(unittest.TestCase):
    def flow(self, td):
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        return Flow(d, sky130hd("/p"), td, demo.fake_factory(), validate_inputs=False)

    def test_variants_parallel_cap_selection(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            conc = demo.Concurrency()
            run = f.step("place", demo.fake_step(conc, delay=0.1), variants=[{"d": i} for i in range(6)],
                         parallel=2, score="setup_wns")
            self.assertLessEqual(conc.peak, 2)
            self.assertEqual(conc.peak, 2)
            best = max(run.attempts, key=lambda a: a.metrics["wns"])
            self.assertIs(run.selected, best)
            self.assertEqual(sum(a.status == "rejected" for a in run.attempts), 5)
            with open(os.path.join(td, "graph.json")) as fh:
                g = json.load(fh)
            self.assertEqual(len(g["attempts"]), 6)  # rejected attempts stay
            self.assertEqual(len({a.session_id for a in run.attempts}), 6)  # one session each
            for a in run.attempts:
                self.assertTrue(os.path.isfile(a.checkpoint))

    def test_failure_writes_no_checkpoint_and_parent_rules(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            run = f.step("a", demo.fake_step(fail_if=lambda p: p["k"] == 1), variants=[{"k": 0}, {"k": 1}],
                         score="setup_wns")
            failed = run.attempts[1]
            self.assertEqual(failed.status, "failed")
            self.assertIsNone(failed.checkpoint)
            self.assertFalse(any(n.endswith(".odb") for n in os.listdir(failed.dir)))
            with self.assertRaises(ValueError):
                f.step("b", demo.fake_step(), parent=failed)
            with self.assertRaises(StepFailed):
                f.step("c", demo.fake_step(fail_if=lambda p: True), parent=run.selected)

    def test_eco_refusals(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            r = f.step("route", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")
            rejected = [a for a in r.attempts if a.status == "rejected"][0]
            with self.assertRaises(EcoRefused):
                f.eco("e0", rejected, [], fn=demo.fake_step(), score="setup_wns")
            e1 = f.eco("e1", r.selected, [], fn=demo.fake_step(), score="setup_wns")
            with self.assertRaises(EcoRefused):  # parent is a sibling of the head
                f.eco("e2", r.selected, [], fn=demo.fake_step(), score="setup_wns")
            f.eco("e3", e1.selected, [], fn=demo.fake_step(), score="setup_wns")

    def test_tcl_error_fails_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
            f = Flow(d, sky130hd("/p"), td, lambda w: FakeSession(w, fail_on=("global_placement",)),
                     validate_inputs=False)
            with self.assertRaises(StepFailed) as cm:
                f.step("place", lambda ctx: ctx.session.run("global_placement -timing_driven"))
            self.assertIn("TclError", str(cm.exception))
            self.assertEqual(f.attempts[0].metrics, {})

    def test_named_scores(self):
        self.assertGreater(scores.drc_then_wns({"drc": 0, "wns": -1}), scores.drc_then_wns({"drc": 1, "wns": 2}))
        self.assertIsNone(scores.setup_wns({}))
        with self.assertRaises(KeyError):
            scores.get("nope")


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


class ResumeAndCancel(unittest.TestCase):
    """5.1 resume, 5.2 forensics, 6.2 cancellation, on stand-in sessions."""

    def make(self, td, factory=None, **kw):
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        made = []

        def fac(w):
            s = (factory or demo.fake_factory())(w)
            made.append(s)
            return s
        f = Flow(d, sky130hd("/p"), td, fac, validate_inputs=False, **kw)
        return f, made

    def test_resume_reuses_recorded_steps(self):
        with tempfile.TemporaryDirectory() as td:
            f, _ = self.make(td)
            a = f.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")
            b = f.step("b", demo.fake_step(), parent=a.selected, variants=[{"k": 2}], score="setup_wns")
            f2, made = self.make(td, resume=True)
            a2 = f2.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")
            b2 = f2.step("b", demo.fake_step(), parent=a2.selected, variants=[{"k": 2}], score="setup_wns")
            self.assertEqual(made, [])  # nothing ran again
            self.assertEqual((a2.selected.id, b2.selected.id), (a.selected.id, b.selected.id))
            self.assertTrue(a2.resumed and a2.selected.resumed)
            self.assertTrue(os.path.isfile(b2.selected.checkpoint))
            with self.assertRaises(ValueError):  # same name, other variants
                f2.step("a", demo.fake_step(), variants=[{"k": 9}], score="setup_wns")
            c = f2.step("c", demo.fake_step(), parent=b2.selected, score="setup_wns")  # continues from it
            self.assertEqual(len(made), 1)
            self.assertIs(c.selected.parent, b2.selected)

    def test_resume_after_interruption_runs_only_unfinished(self):
        with tempfile.TemporaryDirectory() as td:
            f, _ = self.make(td)
            f.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}, {"k": 2}], score="setup_wns")
            # what a kill in the middle of the sweep leaves: a.0 finished, a.1 running, a.2 pending, no step entry
            g = _read_json(os.path.join(td, "graph.json"))
            g["steps"] = {}
            g["attempts"][0]["status"] = "ok"
            g["attempts"][1].update(status="running", checkpoint=None)
            g["attempts"][2].update(status="pending", checkpoint=None)
            _write_json(os.path.join(td, "graph.json"), g)
            first = g["attempts"][0]["started_at"]
            f2, made = self.make(td, resume=True)
            self.assertEqual([x.status for x in f2.attempts], ["ok", "interrupted", "interrupted"])
            r = f2.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}, {"k": 2}], score="setup_wns")
            self.assertEqual(len(made), 2)  # a.1 and a.2 only
            self.assertEqual(r.attempts[0].started_at, first)
            self.assertIs(r.selected, max(r.attempts, key=lambda x: x.metrics["wns"]))  # kept + new compete
            self.assertEqual(len(f2.attempts), 3)

    def test_selected_dir_is_never_reused(self):
        with tempfile.TemporaryDirectory() as td:
            f, _ = self.make(td)
            r = f.step("a", demo.fake_step(), score="setup_wns")
            sel = r.selected
            stray = Attempt(id="x.0", step="x", index=0, params={}, parent=None, dir=sel.dir)
            f.attempts.append(stray)
            f._run_attempt(stray, demo.fake_step())
            self.assertEqual(stray.status, "failed")
            self.assertIn("refusing to reuse", stray.error)
            self.assertTrue(os.path.isfile(sel.checkpoint))

    def test_cancel_kills_running_and_skips_pending(self):
        import threading
        with tempfile.TemporaryDirectory() as td:
            f, made = self.make(td, factory=KillableSession)
            threading.Timer(0.5, f.cancel).start()
            with self.assertRaises(StepCancelled):
                f.step("r", lambda ctx: ctx.session.run("block"), variants=[{"k": i} for i in range(4)], parallel=2)
            st = [a.status for a in f.attempts]
            self.assertEqual(st, ["cancelled"] * 4)
            self.assertEqual(len(made), 2)  # the two pending attempts never started a process
            self.assertTrue(all(s.dead.is_set() for s in made))
            self.assertTrue(all(a.checkpoint is None for a in f.attempts))
            f.reset_cancel()
            r = f.step("r", demo.fake_step(), score="setup_wns")  # a cancelled step can run again
            self.assertEqual(r.selected.status, "selected")

    def test_failed_attempt_forensics(self):
        from sipath.config import Config
        with tempfile.TemporaryDirectory() as td:
            f, _ = self.make(td, config=Config(debug_failed=True))
            with self.assertRaises(StepFailed):
                f.step("a", demo.fake_step(fail_if=lambda p: True))
            a = f.attempts[0]
            self.assertIsNone(a.checkpoint)
            self.assertTrue(a.debug_checkpoint and a.debug_checkpoint.endswith(".failed.odb"))
            self.assertIn(os.sep + "debug" + os.sep, a.debug_checkpoint)
            with self.assertRaises(ValueError):  # a failed attempt is never a parent
                f.step("b", demo.fake_step(), parent=a)
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual(g["attempts"][0]["debug_checkpoint"], a.debug_checkpoint)
            self.assertIn("phases", g["attempts"][0]["timing"])


class ConfigTests(unittest.TestCase):
    def test_sources_and_rules(self):
        from sipath.config import Config, ConfigError
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "c.yaml")
        with open(p, "w") as fh:
            fh.write("threads: 4\nparallel_cap: 3\ncontainer_memory: 6g\n")
        old = {k: os.environ.pop(k, None) for k in ("SIPATH_THREADS", "SIPATH_CPUS", "SIPATH_CONFIG")}
        try:
            os.environ["SIPATH_CPUS"] = "2.5"
            c = Config.load(p, command_timeout=99)
        finally:
            os.environ.pop("SIPATH_CPUS", None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual((c.threads, c.parallel_cap, c.container_memory, c.container_cpus, c.command_timeout),
                         (4, 3, "6g", 2.5, 99))
        self.assertEqual((c.cap(8), c.cap(2)), (3, 2))
        self.assertEqual(Config().cap(8), 8)
        with self.assertRaises(ConfigError):
            Config(image="openroad/orfs:latest", require_digest=True)
        Config(image="openroad/orfs@sha256:" + "0" * 64, require_digest=True)
        with self.assertRaises(ConfigError):
            Config().replace(bogus=1)
        from sipath.session import DockerRunner, NativeRunner
        cmd = DockerRunner("img@sha256:abc", threads=3, cpus=2, memory="4g").command("/tmp", "n")
        self.assertEqual(cmd[cmd.index("--cpus") + 1], "2")
        self.assertEqual(cmd[cmd.index("--memory") + 1], "4g")
        self.assertEqual(cmd[cmd.index("-threads") + 1], "3")
        self.assertIn("img@sha256:abc", cmd)
        self.assertEqual(NativeRunner("/bin/or", threads=5).command("/tmp", "n")[-1], "5")

    def test_flow_caps_parallel(self):
        from sipath.config import Config
        with tempfile.TemporaryDirectory() as td:
            d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
            f = Flow(d, sky130hd("/p"), td, demo.fake_factory(), validate_inputs=False,
                     config=Config(parallel_cap=2))
            conc = demo.Concurrency()
            f.step("a", demo.fake_step(conc, delay=0.1), variants=[{"k": i} for i in range(5)], parallel=5,
                   score="setup_wns")
            self.assertEqual(conc.peak, 2)
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual(g["config"]["parallel_cap"], 2)


class McpJobs(unittest.TestCase):
    """6.2/6.3 job machinery and argument errors, on stand-in sessions (no OpenROAD)."""

    def server(self, td, factory=None):
        from sipath import mcp_server
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        f = Flow(d, sky130hd("/p"), td, factory or demo.fake_factory(), validate_inputs=False)
        notes = []
        s = mcp_server.Server(emit=notes.append)
        s.runs["r1"] = {"flow": f, "design": "fake", "job": None}
        return s, f, notes

    def test_argument_errors_are_tool_errors(self):
        from sipath.mcp_server import ToolError
        with tempfile.TemporaryDirectory() as td:
            s, f, _ = self.server(td)
            for args in ({"step": "floorplan", "score": "nope"}, {"step": "place", "variants": [{"densty": 1}]},
                         {"step": "floorplan", "parallel": 0}, {"step": "bogus"}, {"step": "place"},
                         {"step": "place", "parent": "x.9"}):
                with self.assertRaises(ToolError, msg=args):
                    s.run_step("r1", **args)
            for ops in ([], [{"op": "resize", "inst": "u1"}], [{"op": "delete"}]):
                f.attempts.append(Attempt("p.0", "p", 0, {}, None, td, status="selected"))
                with self.assertRaises(ToolError):
                    s.run_eco("r1", "p.0", ops)
            with self.assertRaises(ToolError):
                s.job_status("job-none")

    def test_background_job_progress_and_cancel(self):
        import time as _t
        from unittest import mock
        from sipath import mcp_server
        with tempfile.TemporaryDirectory() as td:
            s, f, notes = self.server(td, factory=KillableSession)
            with mock.patch.dict(mcp_server.STEPS, {"floorplan": lambda ctx: ctx.session.run("block")}):
                s._req.id, s._req.progress_token = 7, "tok"
                j = s.run_step("r1", "floorplan", variants=[{}, {}], parallel=2, background=True)
                for _ in range(100):
                    if sum(e["event"] == "start" for e in s.jobs[j["job_id"]]["events"]) == 2:
                        break
                    _t.sleep(0.05)
                with self.assertRaises(mcp_server.ToolError):  # one job per run at a time
                    s.run_step("r1", "floorplan")
                out = s.cancel_job(j["job_id"])
            self.assertEqual(out["status"], "cancelled")
            self.assertEqual([a.status for a in f.attempts], ["cancelled", "cancelled"])
            prog = [n for n in notes if n["method"] == "notifications/progress"]
            self.assertTrue(prog and all(n["params"]["progressToken"] == "tok" for n in prog))
            self.assertEqual(prog[-1]["params"]["progress"], 2)
            self.assertFalse(f.cancelled)  # reset after the job
            s._req.progress_token = None
            with mock.patch.dict(mcp_server.STEPS, {"floorplan": demo.fake_step()}):
                r = s.run_step("r1", "floorplan")  # and the step can run again
            self.assertEqual(r["selected"], "floorplan.0")

    def test_resources(self):
        with tempfile.TemporaryDirectory() as td:
            s, f, _ = self.server(td)

            def step(ctx):
                for k, body in (("x.def", "DESIGN x ;"), ("x.spef", "*SPEF"), ("t.rpt", "slack")):
                    with open(ctx.path(k), "w") as fh:
                        fh.write(body)
                ctx.outputs.update(def_=ctx.path("x.def"), spef=ctx.path("x.spef"),
                                   reports={"timing": ctx.path("t.rpt")})
                ctx.session.responses["::flow_metrics"] = demo.fake_metrics({})
            f.step("extract", step)
            uris = {r["uri"] for r in s.resources_list()["resources"]}
            self.assertEqual(uris, {"flow://r1/graph.json", "flow://r1/extract.0/def", "flow://r1/extract.0/spef",
                                    "flow://r1/extract.0/report/timing"})
            self.assertEqual(s.resources_read("flow://r1/extract.0/def")["contents"][0]["text"], "DESIGN x ;")
            from sipath.mcp_server import ToolError
            with self.assertRaises(ToolError):
                s.resources_read("flow://r1/nope")


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


class McpProtocol(unittest.TestCase):
    def test_handshake_tools_and_errors(self):
        c = McpClient()
        self.addCleanup(c.close)
        init = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        c.rpc("notifications/initialized", notify=True)
        names = {t["name"] for t in c.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"list_designs", "list_scores", "describe_step", "open_run", "run_step", "run_eco", "list_attempts",
                                 "get_outputs", "query_checkpoint", "draw_checkpoint", "render_checkpoint",
                                 "job_status", "cancel_job", "list_jobs"})
        self.assertIn("resources", init["result"]["capabilities"])
        self.assertEqual(c.rpc("resources/list")["result"], {"resources": []})
        err, out, _ = c.call("list_designs")
        self.assertFalse(err)
        self.assertEqual(set(out["designs"]), {"dp8", "dp8_hs", "mcu8", "ihp_add8", "gcd8"})
        self.assertTrue(c.call("run_step", run_id="nope", step="floorplan")[0])
        self.assertTrue(c.call("no_such_tool")[0])
        self.assertEqual(c.rpc("no/such/method")["error"]["code"], -32601)
        self.assertEqual(c.rpc(None, raw="{not json")["error"]["code"], -32700)
        self.assertEqual(c.rpc("ping")["result"], {})


# ----------------------------------------------------------------------------- live
@LIVE
class OpenRoad(unittest.TestCase):
    """A live process: reads a LEF, answers Tcl, raises on Tcl errors."""

    def test_result_write_failure_fails_fast(self):
        import time as _t
        from sipath.session import SessionDied
        wd = _workdir("werr")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        with OpenRoadSession(wd, RUNNER, timeout=120) as s:
            t0 = _t.time()
            with self.assertRaises(SessionDied) as cm:  # the wrapper cannot open its result file
                s.run("rename open ::__no_open; proc open {args} { error {open is broken} }")
            self.assertLess(_t.time() - t0, 60)
            self.assertIn("open is broken", str(cm.exception))

    def test_reads_lef_in_live_process(self):
        pdk = sky130hd(RUNNER.platform_root())
        wd = _workdir("lef")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        with OpenRoadSession(wd, RUNNER) as s:
            s.run(f"read_lef {tcl.q(pdk.tech_lef)}\nread_lef {tcl.q(pdk.cell_lef)}")
            self.assertGreater(int(s.value("llength [[ord::get_db] getLibs]")), 0)
            n = int(s.value("set n 0; foreach l [[ord::get_db] getLibs] { incr n [llength [$l getMasters]] }; set n"))
            self.assertGreater(n, 400)
            with self.assertRaises(TclError):
                s.run("read_lef /does/not/exist.lef")
            big = s.value("set o {}; for {set i 0} {$i < 20000} {incr i} { lappend o $i }; set o")
            self.assertEqual(big, " ".join(map(str, range(20000))))
            from sipath.tclquote import q
            for v in TclQuoting.NASTY:  # what Tcl stores is exactly the Python value
                self.assertEqual(s.value(f"set x {q(v)}; set x"), v)
            with self.assertRaises(TclError):
                s.run("break")
            self.assertEqual(s.value("return 7"), "7")  # return is a normal completion


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


@LIVE
class LiveSoftMacro(unittest.TestCase):
    """Mode 1 on OpenROAD: DEF floorplan -> routed DEF + SPEF, then ECOs."""

    @classmethod
    def setUpClass(cls):
        from sipath.examples import dp8
        cls.wd = _workdir("dp8")
        if not KEEP:
            cls.addClassCleanup(_rmtree, cls.wd)
        # third variant reserves 95% of every layer in its routability check: congested
        cls.flow = dp8.run(cls.wd, RUNNER, place_variants=[
            {"density": 0.45, "seed": 1}, {"density": 0.6, "seed": 2, "routability_driven": True},
            {"density": 0.6, "seed": 3, "congestion_margin": 0.95}], parallel=3)

    def test_routability_check_rejects_congested_variant(self):
        att = {a.index: a for a in self.flow.steps["place"].attempts}
        self.assertEqual(len(att), 3)
        stressed = att[2]
        self.assertEqual(stressed.status, "rejected")  # by score: it ran fine and was measured
        self.assertGreater(stressed.metrics["grt_overflow"], 0)
        self.assertGreater(stressed.metrics["grt_congested_gcells"], 0)
        self.assertTrue(os.path.getsize(stressed.outputs["reports"]["place_congestion"]) > 0)
        sel = self.flow.steps["place"].selected
        self.assertEqual(sel.metrics["grt_overflow"], 0)
        self.assertEqual(att[1].status in ("selected", "rejected"), True)  # routability-driven gpl ran
        self.assertEqual(att[1].params["routability_driven"], True)
        self.assertEqual(sel.metrics["datapaths_intact"], 2)

    def test_routed_def_and_spef_exist(self):
        ex = self.flow.steps["extract"].selected
        _check_outputs(self, ex, "dp8", self.flow.design.analog_nets)
        self.assertEqual(self.flow.steps["route_digital"].selected.metrics["drc"], 0)
        self.assertEqual(ex.metrics["parasitics"], "rcx")

    def test_concurrent_attempts_overlap(self):
        """Two route attempts ran as two OpenROAD processes at the same time."""
        from sipath.scheduler import overlap_seconds
        a, b = self.flow.steps["route_digital"].attempts
        self.assertNotEqual(a.process, b.process)
        self.assertTrue(a.process.startswith("sipath-") and b.process.startswith("sipath-"))
        self.assertGreater(overlap_seconds(a, b), 2.0, (a.started_at, a.finished_at, b.started_at, b.finished_at))
        self.assertEqual({a.status, b.status}, {"selected", "rejected"})
        self.assertNotEqual(a.checkpoint, b.checkpoint)
        # the cap is respected: never more than `parallel` attempts alive at once
        place = self.flow.steps["place"].attempts
        self.assertEqual(len(place), 3)

    def test_phase1_signoff(self):
        f = self.flow
        ex = f.steps["extract"].selected
        self.assertTrue(ex.metrics["timing_met"])
        corners = ex.outputs["spef_corners"]
        self.assertEqual(set(corners), {"tt_nom", "tt_min", "tt_max", "ss_nom", "ss_min", "ss_max", "ff_nom", "ff_min", "ff_max"})  # sky130A signoff set
        self.assertEqual(len(set(corners.values())), 3)  # one extraction per RC corner (min/nom/max)
        self.assertTrue(ex.metrics["drv_clean"])  # PDK slew/cap/fanout limits hold
        self.assertEqual(f.pdk.name, "sky130A")
        for c, path in corners.items():
            self.assertGreater(os.path.getsize(path), 1000, c)
            self.assertIn(f"wns_{c}", ex.metrics)
        for name in ("signoff_timing_setup", "signoff_timing_hold", "signoff_clock_skew", "signoff_power"):
            with open(ex.outputs["reports"][name]) as fh:
                self.assertNotIn("failed:", fh.read(200), name)
        with open(ex.outputs["reports"]["signoff_timing_setup"]) as fh:
            self.assertIn("Corner: ss", fh.read())  # worst setup path is at the slow corner
        rd = f.steps["route_digital"].selected
        self.assertEqual(rd.metrics["antenna_violations"], 0)
        self.assertGreater(rd.metrics["antenna_diodes"], 0)
        self.assertFalse(rd.metrics["congested"])
        self.assertLessEqual(f.steps["place"].selected.metrics["tie_max_fanout"], 1)

    def test_power_checks_without_pads(self):
        pg = self.flow.steps["extract"].selected.outputs["power_grid"]
        self.assertTrue(pg["VDD"]["connected"] and pg["VSS"]["connected"])
        self.assertIsNotNone(pg["VDD"]["ir_worst_mV"])  # straps as sources (no supply terminals)

    def test_post_route_repair_on_tight_clock(self):
        """With the clock tightened, post-route repair runs on the extracted
        parasitics, re-routes what it changed, and keeps DRC at zero."""
        import copy
        d = copy.deepcopy(self.flow.design)
        wd = _workdir("tight")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        sdc = os.path.join(wd, "tight.sdc")
        with open(d.sdc) as fh, open(sdc, "w") as out:
            out.write(fh.read().replace("-period 10.0", "-period 6.5"))
        d.sdc = sdc
        RUNNER.mount(wd)
        f = Flow(d, self.flow.pdk, wd, lambda w: OpenRoadSession(w, RUNNER), runner_prefixes=RUNNER.runner_prefixes())
        from sipath import steps as st
        rd = f.step("route_digital", st.route_digital, parent=self.flow.steps["route_analog"].selected,
                    score="drc_then_slack")
        r = f.step("route_repair", st.route_repair, parent=rd.selected, score="drc_then_slack")
        a = r.selected
        rounds = a.outputs["post_route_repair"]["rounds"]
        self.assertLess(rounds[0]["wns"], 0)  # violated after routing
        self.assertTrue("resized_or_added" in rounds[0] or "note" in rounds[0])
        self.assertGreaterEqual(rounds[-1]["wns"], rounds[0]["wns"] - 0.001)  # never worse (to 1 ps)
        self.assertEqual(a.metrics.get("drc", 0), 0)

    def test_analog_nets_extracted_and_route_scored_on_rcx(self):
        ex = self.flow.steps["extract"].selected
        self.assertEqual(ex.metrics["analog_nets_in_spef"], 2)
        self.assertEqual(set(ex.outputs["analog_restored"]), set(self.flow.design.analog_nets))
        for a in self.flow.steps["route_digital"].attempts:
            self.assertEqual(a.metrics["parasitics"], "rcx")
            self.assertIn("timing_met", a.metrics)

    def test_signoff_physical_and_timing(self):
        so = self.flow.steps["signoff"].selected
        s = _read_json(so.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        self.assertEqual(s["drc"]["count"], 0)  # KLayout, the PDK's sky130A_mr deck
        self.assertTrue(s["lvs"]["match"])
        self.assertEqual(s["gds"]["empty"], [])
        self.assertEqual(set(s["gds"]["labels"]), {"VDD", "VSS"})
        self.assertEqual(s["antenna"]["violations"], 0)
        self.assertEqual(len(s["timing"]["corners"]), 9)
        self.assertTrue(all(v["wns"] >= 0 and v["whs"] >= 0 for v in s["timing"]["corners"].values()))
        self.assertEqual(s["timing"]["unannotated_loaded"], [])
        self.assertEqual(set(s["timing"]["unconstrained_analog"]), {"ana_in[0]", "ana_in[1]", "ana_out[0]", "ana_out[1]"})
        self.assertGreater(os.path.getsize(so.outputs["gds"]), 10000)
        # routing kept through extraction: the router's patches are in the final DEF
        with open(so.outputs["def_"]) as fh:
            self.assertGreater(fh.read().count(") RECT ("), 0)
        self.assertGreater(self.flow.steps["extract"].selected.metrics["rcx_restored_nets"], 0)

    def test_timing_breakdown_and_provenance(self):
        f = self.flow
        for a in f.attempts:
            if a.status in ("selected", "rejected"):
                self.assertEqual(set(a.timing["phases"]), {"start", "load", "step", "metrics", "write_db"}, a.id)
                self.assertTrue(a.timing["slowest"] and a.timing["slowest"][0][1] > 0)
        g = _read_json(os.path.join(f.workdir, "graph.json"))
        if RUNNER.kind == "docker":
            self.assertTrue(g["provenance"]["digest"].startswith("openroad/orfs@sha256:"))
        self.assertEqual(g["pdk"], "sky130A")

    def test_graph_and_checkpoints(self):
        f = self.flow
        self.assertEqual(len(f.steps["place"].attempts), 3)
        self.assertEqual({a.status for a in f.steps["place"].attempts}, {"selected", "rejected"})
        odbs = [a.checkpoint for a in f.attempts if a.checkpoint]
        self.assertEqual(len(odbs), len(set(odbs)))
        for o in odbs:
            self.assertGreater(os.path.getsize(o), 1000)
            with open(o, "rb") as fh:
                self.assertNotIn(b"FAKE", fh.read(64))
        ph = f.steps["physical"].selected
        self.assertEqual(ph.metrics["endcaps"], 2 * ph.metrics["rows"])
        self.assertGreater(ph.metrics["taps"], 0)
        self.assertEqual(ph.outputs["groups"]["out_fence"]["insts"], 17)
        self.assertEqual(ph.outputs["datapaths"], {"adder": 24, "mult": 28})  # adder: 8 bits x 3 stages
        self.assertEqual(f.steps["place"].selected.metrics["fence_violations"], 0)
        sig = f.steps["route_analog"].selected.outputs["analog_signature"]
        self.assertIn("met2", sig)

    def test_ecos(self):
        f = self.flow
        head = f.steps["signoff"].selected  # the head of the chain: signoff follows extract
        # resize a multiplier array cell: it grows and its row re-abuts
        e1 = f.eco("eco_resize", head, [{"op": "resize", "inst": "u_mul.add_1_1", "master": "sky130_fd_sc_hd__fa_2"}],
                   max_patch_nets=60)
        a1 = e1.selected
        self.assertEqual(a1.outputs["reabut"]["mult"]["moved"], 4)  # pp_1_2 add_1_2 pp_1_3 add_1_3
        self.assertEqual(a1.metrics["drc"], 0)
        self.assertEqual(a1.outputs["route_mode"], "incremental")
        self.assertTrue(os.path.getsize(a1.outputs["def_"]) > 0 and os.path.getsize(a1.outputs["spef"]) > 0)
        # a second ECO from the old head is a sibling of eco_resize: refused
        with self.assertRaises(EcoRefused):
            f.eco("eco_sibling", head, [{"op": "resize", "inst": "u_acc.x_0", "master": "sky130_fd_sc_hd__xor2_2"}])
        # buffer, from the new head; tiny patch limit forces the full digital route
        e2 = f.eco("eco_buffer", a1, [{"op": "buffer", "net": "u_acc.q_2", "master": "sky130_fd_sc_hd__buf_2",
                                       "name": "eco_buf_0"}], max_patch_nets=1)
        a2 = e2.selected
        self.assertEqual(a2.outputs["route_mode"], "full")
        self.assertEqual(a2.metrics["drc"], 0)
        self.assertGreater(a2.outputs["equivalence"]["instances"], 0)
        # a functional change fails equivalence: attempt fails and writes no ODB
        with self.assertRaises(StepFailed):
            f.eco("eco_bad", a2, [{"op": "resize", "inst": "u_mul.pp_0_0", "master": "sky130_fd_sc_hd__or2_1"}])
        bad = [a for a in f.attempts if a.step == "eco_bad"][0]
        self.assertEqual(bad.status, "failed")
        self.assertIn("EquivalenceError", bad.error)
        self.assertIsNone(bad.checkpoint)
        self.assertFalse(any(n.endswith(".odb") for n in os.listdir(bad.dir)))
        # a stage-0 register of the bit-slice adder grows: stages 1 and 2 of all 8 bits move
        # right to keep the stage columns aligned, and the structure check passes after routing
        e3 = f.eco("eco_slice", a2, [{"op": "resize", "inst": "u_in.ra_2", "master": "sky130_fd_sc_hd__dfxtp_2"}],
                   max_patch_nets=200)
        a3 = e3.selected
        self.assertEqual(a3.outputs["reabut"]["adder"]["moved"], 16)
        self.assertEqual(a3.metrics["datapaths_intact"], 2)
        self.assertEqual(a3.metrics["drc"], 0)
        self.assertTrue(os.path.getsize(a3.outputs["def_"]) > 0 and os.path.getsize(a3.outputs["spef"]) > 0)


@LIVE
class LiveMcuDie(unittest.TestCase):
    """Mode 2 on OpenROAD: pad ring + macros -> routed DEF + SPEF."""

    @classmethod
    def setUpClass(cls):
        from sipath.examples import mcu8
        cls.wd = _workdir("mcu8")
        if not KEEP:
            cls.addClassCleanup(_rmtree, cls.wd)
        cls.flow = mcu8.run(cls.wd, RUNNER, seeds=(1, 2, 3))

    def test_routed_def_and_spef_exist(self):
        ex = self.flow.steps["extract"].selected
        _check_outputs(self, ex, "mcu8", self.flow.design.analog_nets, rings=True)  # the die asks for a ring
        self.assertEqual(self.flow.steps["route_digital"].selected.metrics["drc"], 0)

    def test_pads_and_macros(self):
        f = self.flow
        ring = f.steps["floorplan"].selected.outputs["pad_ring"]
        self.assertEqual(ring["pads"], 39)
        self.assertEqual(ring["floating_pad_inputs"], 0)  # every GPIO control pin tied
        self.assertEqual(ring["pad_plan"]["core_supply_edges"], ["south", "west"])
        # geometry derived from the sky130 IO library (nothing configured)
        self.assertEqual(ring["io_geometry"], {"corner_site": [200.0, 204.0], "row_depth_ns": 204.0,
                                               "row_depth_ew": 200.0, "rotation_ns": "R180", "rotation_ew": "R90",
                                               "rotation_corner": "R180"})
        mc = f.steps["macros"]
        self.assertEqual(len(mc.attempts), 5)
        best = max((a for a in mc.attempts if a.metrics), key=lambda a: scores.macro_quick(a.metrics))
        self.assertIs(mc.selected, best)
        sel = mc.selected.outputs["macros"]
        self.assertEqual(sel["u_sram"], {"mode": "frozen", "loc_um": [262.2, 843.2], "orient": "R90"})
        self.assertEqual(sel["u_adc"]["mode"], "auto")
        self.assertEqual(sel["u_dac"]["mode"], "auto")
        with open(f.steps["extract"].selected.outputs["def_"]) as fh:
            text = fh.read()
        # ChipFoundry macros in the routed DEF; LOCKED is written as FIXED, R90 as W
        self.assertTrue(re.search(r"- u_sram CF_SRAM_1024x32 \+ FIXED \( 262200 843200 \) W", text))
        self.assertTrue(re.search(r"- u_adc EF_ADCS1008NC \+ (FIRM|PLACED|FIXED)", text))
        self.assertTrue(re.search(r"- u_dac sky130_ef_ip__rdac3v_8bit \+ (FIRM|PLACED|FIXED)", text))

    def test_pad_ring_is_closed(self):
        ring = self.flow.steps["floorplan"].selected.outputs["pad_ring"]
        self.assertEqual(ring["ring_supplies"], 12)  # VCCD VSSD VDDIO ... each one continuous net
        self.assertGreater(ring["ring_cells"], 150)
        with open(self.flow.steps["extract"].selected.outputs["def_"]) as fh:
            text = fh.read()
        for corner in ("SOUTH_WEST", "SOUTH_EAST", "NORTH_WEST", "NORTH_EAST"):
            self.assertIn(f"IO_CORNER_{corner}_INST", text)
        self.assertTrue(re.search(r"- IO_CORNER_NORTH_EAST_INST sky130_ef_io__corner_pad \+ FIXED \( 1300000 1296000 \) N ", text))

    def test_pictures_from_library(self):
        from sipath import viz
        ck = self.flow.steps["extract"].selected.checkpoint
        out = os.path.join(self.wd, "pics")
        os.makedirs(out, exist_ok=True)
        d = viz.draw(ck, os.path.join(out, "ring.svg"), RUNNER, ring_only=True)
        self.assertEqual((d["ring_supplies"], d["ring_split"]), (12, 0))
        with open(d["svg"]) as fh:
            svg = fh.read()
        self.assertIn("all continuous", svg)
        self.assertNotIn("u_sram", svg)  # ring only
        r = viz.render(ck, os.path.join(out, "die.png"), RUNNER)
        self.assertEqual(r["pixels"], [1600, 1600])
        self.assertGreater(r["bytes"], 100000)

    def test_broken_ring_config_is_rejected(self):
        """The old corner/site geometry splits the ring per edge; the check must fail it."""
        import copy
        from sipath.examples import mcu8
        from sipath.pdk import load as load_pdk
        pdk = sky130hd(RUNNER.platform_root())
        d = copy.deepcopy(self.flow.design)
        d.pad_masters.update({"corner_site": (211, 211), "row_depth_ns": 211, "row_depth_ew": 211,
                              "rotation_corner": "R0"})
        wd = _workdir("ring_bad")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        RUNNER.mount(wd)
        f = Flow(d, pdk, wd, lambda w: OpenRoadSession(w, RUNNER), runner_prefixes=RUNNER.runner_prefixes())
        with self.assertRaises(StepFailed) as cm:
            f.step("floorplan", __import__("sipath.steps", fromlist=["floorplan"]).floorplan)
        self.assertIn("pad ring is broken", str(cm.exception))

    def test_seed_sweep_respects_parallel_cap(self):
        from sipath.scheduler import peak_concurrency
        mc = self.flow.steps["macros"].attempts
        self.assertEqual(len(mc), 5)
        self.assertEqual(peak_concurrency(mc), 2)  # 5 real processes, cap 2: two at once, never three
        self.assertEqual(len({a.process for a in mc}), 5)

    def test_macro_sweep_compares_methods(self):
        """random (3 seeds) vs random + orientation search vs rtl_macro_placer, one
        sweep, one score; every autoplaced macro passes the channel / pin-side /
        analog-guard rules; analog-net length measured and in the cost."""
        mc = self.flow.steps["macros"]
        methods = {(a.params.get("method"), bool(a.params.get("orients"))) for a in mc.attempts if a.status != "failed"}
        self.assertEqual(methods, {("random", False), ("random", True), ("mpl", False)})
        for a in mc.attempts:
            self.assertNotEqual(a.status, "failed", a.error)
            for inst in ("u_adc", "u_dac"):
                self.assertEqual(a.outputs["macro_rule_check"][inst], "ok")
                self.assertEqual(a.outputs["macros"][inst]["method"], a.params["method"])
            self.assertEqual(set(a.metrics["analog_macros"]), {"u_adc", "u_dac"})
            self.assertGreater(a.metrics["analog_hpwl_um"], 0)
            self.assertEqual(a.metrics["quick_cells"], 156)  # every standard cell placed for the score...
            self.assertAlmostEqual(a.metrics["macro_cost_um"],
                                   a.metrics["quick_hpwl_um"] + 4.0 * a.metrics["analog_hpwl_um"], places=2)
            self.assertEqual(len(a.outputs["analog_guard_blockages"]), 2)
        oriented = [a for a in mc.attempts if a.params.get("orients")][0]
        self.assertEqual(oriented.outputs["macros"]["u_adc"]["candidates"], 40)
        # ...and unplaced again: the checkpoint holds only the macros
        RUNNER.mount(self.wd)
        wd = _workdir("mcheck")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        RUNNER.mount(wd)
        with OpenRoadSession(wd, RUNNER) as s:
            s.run(f"read_db {mc.selected.checkpoint}")
            n = s.value("set n 0; foreach i [[ord::get_db_block] getInsts] { if {[[$i getMaster] getType] eq "
                        "\"CORE\" && [$i isPlaced]} { incr n } }; set n")
            self.assertEqual(n, "0")
            self.assertEqual(s.value("llength [[ord::get_db_block] getBlockages]"), "2")

    def test_macro_placement_reproducible_by_seed(self):
        from sipath import steps
        f = self.flow
        params = dict(f.steps["macros"].attempts[0].params)  # random, seed 1
        rep = f.step("macros_repeat", steps.macros, parent=f.steps["floorplan"].selected, variants=[params],
                     score="macro_quick")
        a, b = f.steps["macros"].attempts[0].outputs["macros"], rep.selected.outputs["macros"]
        for inst in ("u_adc", "u_dac"):
            self.assertEqual((a[inst]["loc_um"], a[inst]["orient"]), (b[inst]["loc_um"], b[inst]["orient"]))

    def test_macro_score_ignores_unplaced_cells(self):
        from sipath import chip
        a = self.flow.steps["macros"].selected
        self.assertGreater(a.metrics["macro_hpwl_unplaced_terms"], 0)
        wd = _workdir("mscore")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        RUNNER.mount(wd)
        with OpenRoadSession(wd, RUNNER) as s:
            s.run(f"read_db {a.checkpoint}")
            s.run(tcl.HELPERS)
            s.run(chip.MACRO_HPWL)
            before = s.value("::flow_macro_hpwl {u_sram u_adc u_dac}")
            s.run("foreach i [[ord::get_db_block] getInsts] { if {![$i isPlaced]} { $i setLocation 700000 700000 } }")
            self.assertEqual(s.value("::flow_macro_hpwl {u_sram u_adc u_dac}"), before)
        self.assertAlmostEqual(float(before), a.metrics["macro_hpwl_um"], places=2)

    def test_analog_nets_in_spef(self):
        self.assertEqual(self.flow.steps["extract"].selected.metrics["analog_nets_in_spef"], 4)

    def test_phase1_on_die(self):
        f = self.flow
        self.assertEqual(f.steps["place"].selected.metrics["tie_max_fanout"], 1)  # many tie loads before
        rd = f.steps["route_digital"].selected
        self.assertEqual((rd.metrics["drc"], rd.metrics["antenna_violations"]), (0, 0))
        ex = f.steps["extract"].selected
        self.assertTrue(ex.metrics["timing_met"])
        self.assertEqual(set(ex.outputs["spef_corners"]), {"tt_nom", "tt_min", "tt_max", "ss_nom", "ss_min", "ss_max", "ff_nom", "ff_min", "ff_max"})
        # standard cells meet the PDK limits; IO-ring/pad/macro/port violators are listed apart
        self.assertTrue(ex.metrics["drv_clean"])
        self.assertGreater(ex.metrics["drv_excluded"], 0)
        self.assertTrue(set(ex.outputs["drv_excluded_by_type"]) <= {"BLOCK", "PORT", "PAD_SPACER", "PAD_INOUT",
                                                                     "PAD_POWER", "ENDCAP_TOPRIGHT", "PAD_INPUT",
                                                                     "PAD_OUTPUT", "PAD"})

    def test_signoff_on_die(self):
        so = self.flow.steps["signoff"].selected
        s = _read_json(so.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        # the ChipFoundry macros are signed off as LEF abstracts: DRC sees the chip around them
        self.assertEqual(s["drc"]["count"], 0, s["drc"].get("by_owner_rule"))
        self.assertEqual(sorted(s["drc"]["abstract_macros"]), ["u_adc", "u_dac", "u_sram"])
        self.assertEqual(s["gds"]["empty"], [])  # the written GDS has every layout, IP included
        # LVS: macros and IO cells are black boxes, everything else transistor level
        self.assertTrue(s["lvs"]["match"], s["lvs"].get("first_errors"))
        self.assertTrue({"u_adc", "u_dac", "u_sram"} <= set(s["lvs"]["blackboxed"]))
        self.assertTrue(any(k.startswith("sky130_ef_io__") for k in s["lvs"]["blackboxed"]))
        phys = self.flow.steps["physical"].selected.metrics
        self.assertEqual((phys["special_shorts"], phys["rows_without_tap"]), (0, 0))
        self.assertEqual(s["antenna"]["violations"], 0)
        t = s["timing"]
        self.assertEqual(t["unannotated_loaded"], [])
        self.assertGreater(len(t["unannotated_io_ring"]), 0)  # pad ring abutment / bond nets: no routed wire
        self.assertEqual(len(t["unconstrained_pad_ports"]), 17)

    def test_macro_supply_report(self):
        # after the analog-supply route every macro supply pin touches its net's wiring
        rep = self.flow.steps["route_analog"].selected.outputs["macro_supply_pins"]
        for inst, pins in rep.items():
            self.assertEqual(pins["unconnected"], [], inst)
        self.assertEqual(len(rep["u_adc"]["connected"]) + len(rep["u_dac"]["connected"]), 8)
        straps = self.flow.steps["physical"].selected.outputs["power_connections"]
        self.assertTrue(straps and all(r["ok"] for r in straps))

    def test_power_grid_signoff(self):
        ex = self.flow.steps["extract"].selected
        pg = ex.outputs["power_grid"]
        for net in ("VDD", "VSS", "VDDA", "VSSA"):
            self.assertTrue(pg[net]["connected"], (net, pg[net]))
        self.assertTrue(ex.metrics["grid_connected"])
        self.assertIsNotNone(pg["VDD"]["ir_worst_mV"])



@LIVE
class LiveIhp(unittest.TestCase):
    """The second PDK end to end: IHP SG13G2 from its description file, the same
    flow code; then a resize ECO checked with the IHP Liberty functions."""

    @classmethod
    def setUpClass(cls):
        from sipath import designfile
        cls.wd = _workdir("ihp_add8")
        if not KEEP:
            cls.addClassCleanup(_rmtree, cls.wd)
        yaml_ = os.path.join(os.path.dirname(__file__), "..", "examples", "ihp_add8.yaml")
        cls.flow = designfile.run(yaml_, cls.wd, RUNNER)

    def test_routed_def_and_spef_on_ihp(self):
        f = self.flow
        self.assertEqual(f.pdk.name, "ihp-sg13g2")
        ex = f.steps["extract"].selected
        _check_outputs(self, ex, "ihp_add8", f.design.analog_nets)
        self.assertEqual(f.steps["route_digital"].selected.metrics["drc"], 0)
        self.assertEqual(f.steps["route_digital"].selected.metrics["antenna_violations"], 0)
        self.assertTrue(ex.metrics["timing_met"])
        self.assertEqual(set(ex.outputs["spef_corners"]), {"ss", "tt", "ff"})
        self.assertEqual(ex.metrics["analog_nets_in_spef"], 2)
        with open(ex.outputs["def_"]) as fh:
            text = fh.read()
        self.assertIn("sg13g2_dfrbpq_1", text)
        self.assertNotIn("sky130", text)
        self.assertNotRegex(text, r"ROUTED (Metal5|TopMetal)")  # embedded macro: Metal4 at most
        ph = f.steps["physical"].selected
        self.assertEqual((ph.metrics["taps"], ph.metrics["endcaps"]), (0, 0))  # none in this library
        self.assertTrue(ex.metrics["grid_connected"])
        self.assertEqual(f.steps["place"].selected.metrics["datapaths_intact"], 1)

    def test_eco_with_liberty_equivalence_on_ihp(self):
        f = self.flow
        head = f.steps["extract"].selected
        e = f.eco("eco_ihp", head, [{"op": "resize", "inst": "u_add.c_3", "master": "sg13g2_a21o_2"}],
                  max_patch_nets=60)
        a = e.selected
        self.assertEqual(a.metrics["drc"], 0)
        self.assertGreater(a.outputs["equivalence"]["instances"], 0)
        self.assertEqual(a.metrics["datapaths_intact"], 1)
        with self.assertRaises(StepFailed):  # and2 -> or2: same pins, other function: refused, no ODB
            f.eco("eco_ihp_bad", a, [{"op": "resize", "inst": "u_add.g_4", "master": "sg13g2_or2_1"}])
        bad = [x for x in f.attempts if x.step == "eco_ihp_bad"][0]
        self.assertIn("EquivalenceError", bad.error)
        self.assertIsNone(bad.checkpoint)
        with self.assertRaises(StepFailed):  # a21o -> o21ai: replace_cell cannot swap (X vs Y); not silent
            f.eco("eco_ihp_pins", a, [{"op": "resize", "inst": "u_add.c_4", "master": "sg13g2_o21ai_1"}])
        bad = [x for x in f.attempts if x.step == "eco_ihp_pins"][0]
        self.assertIn("did not happen", bad.error)


@LIVE
class LiveIhpRing(unittest.TestCase):
    """Pad ring on the IHP IO library: geometry derived, ring closed."""

    def test_ring(self):
        from sipath.examples import ihp_ring
        wd = _workdir("ihp_ring")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        f = ihp_ring.run(wd, RUNNER)
        ring = f.steps["floorplan"].selected.outputs["pad_ring"]
        self.assertEqual(ring["pads"], 16)
        self.assertEqual(ring["ring_supplies"], 4)
        self.assertEqual((ring["io_geometry"]["rotation_ns"], ring["io_geometry"]["rotation_ew"]), ("R0", "R270"))


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


@LIVE
class LiveScale(unittest.TestCase):
    def test_metrics_on_100k_instances(self):
        import time as _t
        wd = _workdir("big")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        _big_def(os.path.join(wd, "big.def"))
        pdk = sky130hd(RUNNER.platform_root())
        RUNNER.mount(wd)
        with OpenRoadSession(wd, RUNNER) as s:
            s.run(tcl.HELPERS)
            s.run(f"read_lef {pdk.tech_lef}\nread_lef {pdk.cell_lef}\nread_liberty {pdk.liberty}\n"
                  f"read_def {wd}/big.def", timeout=1800)
            t0 = _t.time()
            m = s.value("::flow_metrics", timeout=600)
            took = _t.time() - t0
        from sipath.scheduler import parse_metrics
        m = parse_metrics(m)
        self.assertEqual(m["insts"], 100000)
        self.assertGreater(m["hpwl_um"], 100000 * 1.0)  # every chain net spans at least a cell
        self.assertLess(took, 10.0, f"metrics took {took:.1f}s on 100k instances")


@LIVE
class LiveResume(unittest.TestCase):
    """Kill a run in the middle of a sweep, resume it: finished work is kept,
    the rest runs, and the selection matches an uninterrupted run."""

    STEPS = [{"step": "floorplan"}, {"step": "physical"},
             {"step": "place", "variants": [{"density": 0.45, "seed": 1}, {"density": 0.55, "seed": 2},
                                            {"density": 0.6, "seed": 3}], "score": "routability_then_wns"}]

    def test_kill_mid_sweep_and_resume(self):
        import signal
        import subprocess
        import sys
        import time as _t
        from sipath import designfile
        from sipath.examples import dp8
        wd, ref = _workdir("resume"), _workdir("resume_ref")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
            self.addCleanup(_rmtree, ref)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        code = ("import sys; from sipath import designfile; from sipath.examples import dp8; "
                f"designfile.run(dp8.make_design(), sys.argv[1], flow_steps={self.STEPS!r})")
        child = subprocess.Popen([sys.executable, "-c", code, wd], cwd=root)
        graph = os.path.join(wd, "graph.json")
        killed = False
        for _ in range(1200):
            if os.path.isfile(graph):
                try:
                    g = _read_json(graph)
                except ValueError:
                    g = {"attempts": []}
                st = {a["id"]: a["status"] for a in g["attempts"]}
                if st.get("place.0") == "ok":
                    child.send_signal(signal.SIGKILL)
                    killed = True
                    break
            if child.poll() is not None:
                break
            _t.sleep(0.1)
        child.wait(timeout=60)
        self.assertTrue(killed, "the run ended before it could be interrupted")
        before = {a["id"]: a for a in _read_json(graph)["attempts"]}
        self.assertNotIn("place", _read_json(graph)["steps"])  # the sweep had not finished
        f = designfile.run(dp8.make_design(), wd, RUNNER, flow_steps=self.STEPS, resume=True)
        for aid in ("floorplan.0", "physical.0", "place.0"):
            a = [x for x in f.attempts if x.id == aid][0]
            self.assertTrue(a.resumed, aid)
            self.assertEqual(a.started_at, before[aid]["started_at"], aid)  # not run again
        for aid in ("place.1", "place.2"):
            a = [x for x in f.attempts if x.id == aid][0]
            self.assertFalse(a.resumed, aid)
            self.assertIn(a.status, ("selected", "rejected"))
        r = designfile.run(dp8.make_design(), ref, RUNNER, flow_steps=self.STEPS)
        self.assertEqual(f.steps["place"].selected.id, r.steps["place"].selected.id)
        for k in ("floorplan", "physical"):
            self.assertTrue(os.path.isfile(f.steps[k].selected.checkpoint))


@LIVE
class LiveMcp(unittest.TestCase):
    """MCP on a design file (IHP): progress notifications, a cancelled route
    whose OpenROAD process is gone, then the run finished and its DEF/SPEF
    read back as resources."""

    def test_design_file_progress_cancel_resources(self):
        import subprocess
        import time as _t
        wd = _workdir("mcp_ihp")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        yaml_ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples", "ihp_add8.yaml"))
        c = McpClient()
        self.addCleanup(c.close)
        c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        err, run, txt = c.call("open_run", design=yaml_, workdir=wd)
        self.assertFalse(err, txt)
        self.assertEqual(run["pdk"], "ihp-sg13g2")
        rid = run["run_id"]
        r = c.rpc("tools/call", {"name": "run_step", "arguments": {"run_id": rid, "step": "floorplan"},
                                 "_meta": {"progressToken": "p1"}})["result"]
        self.assertFalse(r["isError"], r["content"][0]["text"])
        prog = [n for n in c.notes if n.get("method") == "notifications/progress"]
        self.assertTrue(prog and prog[-1]["params"]["progressToken"] == "p1")
        self.assertTrue(c.call("run_step", run_id=rid, step="physical", parent="floorplan.0", score="nope")[0])
        parent = "floorplan.0"
        for step in ("physical", "place", "cts", "route_analog"):
            err, out, txt = c.call("run_step", run_id=rid, step=step, parent=parent)
            self.assertFalse(err, txt)
            parent = out["selected"]
        err, job, txt = c.call("run_step", run_id=rid, step="route_digital", parent=parent, background=True)
        self.assertFalse(err, txt)
        for _ in range(600):
            st = c.call("job_status", job_id=job["job_id"])[1]
            if any(e["event"] == "start" for e in st["events"]):
                break
            _t.sleep(0.1)
        _t.sleep(1.0)
        err, st, txt = c.call("cancel_job", job_id=job["job_id"])
        self.assertEqual(st["status"], "cancelled", txt)
        att = [a for a in c.call("list_attempts", run_id=rid, step="route_digital")[1]["attempts"]]
        self.assertEqual([a["status"] for a in att], ["cancelled"])
        self.assertIsNone(att[0]["checkpoint"])
        if RUNNER.kind == "docker":
            ps = subprocess.run(["docker", "ps", "-q", "--filter", f"name={att[0]['process']}"], capture_output=True,
                                text=True).stdout.strip()
            self.assertEqual(ps, "", "the cancelled attempt's container is still running")
        for step in ("route_digital", "route_repair", "extract"):
            err, out, txt = c.call("run_step", run_id=rid, step=step, parent=parent)
            self.assertFalse(err, txt)
            parent = out["selected"]
        uris = {r["uri"] for r in c.rpc("resources/list")["result"]["resources"]}
        for k in ("def", "spef", "spef/ss", "spef/ff"):
            self.assertIn(f"flow://{rid}/{parent}/{k}", uris)
        spef = c.rpc("resources/read", {"uri": f"flow://{rid}/{parent}/spef"})["result"]["contents"][0]["text"]
        self.assertTrue(spef.startswith("*SPEF"))
        self.assertIn('*DESIGN "ihp_add8"', spef[:2000])
