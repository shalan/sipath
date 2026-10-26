"""Unit tests: no OpenROAD binary, no PDK runs (python3 -m unittest sipath.tests.test_unit;
under a minute)."""
from __future__ import annotations

from .common import *  # noqa: F401,F403  (helpers, fixtures and the modules the tests use)
from .common import _big_def, _check_limits, _check_outputs, _lib, _pins, _read_json, _rmtree, _workdir, _write_json  # noqa: F401


# ----------------------------------------------------------------------------- units

class RouteSearchPlans(unittest.TestCase):
    def test_both_examples_have_valid_bounded_plans(self):
        from pathlib import Path
        from sipath import designfile
        examples = Path(__file__).resolve().parents[1] / "examples"
        for name, tail in (("openframe_gcd8_route_search", ["signoff", "precheck", "integrate"]),
                           ("gcd8_macro_route_search", ["signoff", "views"])):
            with self.subTest(name=name):
                path = examples / (name + ".yaml")
                d, spec = designfile.load(str(path))
                plan = designfile.plan(str(path))
                self.assertEqual(plan["problems"], [])
                self.assertTrue(all(os.path.isfile(p) for p in d.input_files()))
                place = next(s for s in spec if s["step"] == "place")
                self.assertEqual(place["variants"][0], {})
                self.assertEqual((place["keep"], place["decide_at"], place["decide_score"]),
                                 (2, "extract", "setup_wns"))
                self.assertEqual([s["step"] for s in spec][-len(tail):], tail)
                if d.harness:
                    original, _ = designfile.load(str(examples / "openframe_gcd8.yaml"))
                    self.assertEqual(d.rtl, original.rtl)
                    self.assertEqual(d.sdc, original.sdc)
                    self.assertEqual(d.harness_options, original.harness_options)
                else:
                    original, _ = designfile.load(str(examples / "gcd8.yaml"))
                    self.assertEqual(d.rtl, original.rtl)
                    self.assertEqual(d.sdc, original.sdc)
                    self.assertEqual(d.core_utilization, original.core_utilization)

    def test_examples_respect_the_callers_thread_budget(self):
        from pathlib import Path
        from sipath import designfile
        examples = Path(__file__).resolve().parents[1] / "examples"
        for name in ("openframe_gcd8_route_search", "gcd8_macro_route_search"):
            for threads in (2, 12):
                with self.subTest(name=name, threads=threads), unittest.mock.patch.dict(
                        os.environ, {"SIPATH_THREADS": str(threads)}):
                    plan = designfile.plan(str(examples / (name + ".yaml")))
                    self.assertEqual(plan["budget"]["threads"], threads)


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

    def test_part_selects_vectors_and_constants_in_connections(self):
        back = parse_verilog("module t(a, b);\n input [7:0] a;\n input [3:0] b;\n wire [1:0] w;\n"
                             " RAM u (.A(a[5:3]), .B(b), .C({w, 2'b10, a[0]}), .D(a[7]), .E());\nendmodule\n")
        c = back.instances[0].conns
        self.assertEqual([c[f"A[{k}]"] for k in range(3)], ["a[3]", "a[4]", "a[5]"])
        self.assertEqual([c[f"B[{k}]"] for k in range(4)], ["b[0]", "b[1]", "b[2]", "b[3]"])
        self.assertEqual([c[f"C[{k}]"] for k in range(5)], ["a[0]", "1'b0", "1'b1", "w[0]", "w[1]"])
        self.assertEqual(c["D"], "a[7]")
        self.assertNotIn("E", c)

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

    def test_tap_slots_keep_every_cell_within_the_tap_distance(self):
        self.assertEqual(datapath._with_slots([4, 4, 4, 4], (1, 9)), ([1, 5, 10, 14], [0, 9], 18))
        self.assertEqual(datapath._with_slots([4, 4], None), ([0, 4], [], 8))
        pitch, tap_w = 13000, 460
        cells = [[f"c{r}_{k}" for k in range(9)] for r in range(3)]
        sizes = {c: (2760, 2720) for row in cells for c in row}  # 9 x 2.76 um = 24.8 um per row
        pl = datapath.pack_array(cells, (0, 0), self.rows(), sizes, (tap_w, pitch))
        datapath.check_internal(pl)
        for y in {p.y for p in pl}:
            slots = sorted(p.x for p in pl if p.y == y and p.tap)
            self.assertEqual(len(slots), 3)  # left end, then whenever 13 um would be exceeded
            for p in (p for p in pl if p.y == y and not p.tap):
                left = max(s for s in slots if s < p.x)
                self.assertLessEqual(p.x + p.w - (left + tap_w), pitch)  # the cell's far edge to its tap
        slices = [[f"s{b}_{k}" for k in range(6)] for b in range(4)]
        sizes = {c: (3680, 2720) for row in slices for c in row}
        pl = datapath.pack_slices(slices, (0, 0), self.rows(), sizes, slot=(tap_w, pitch))
        datapath.check_internal(pl)
        by_row = {}
        for p in pl:
            by_row.setdefault(p.y, []).append((p.x, p.tap))
        self.assertEqual(len({tuple(sorted(v)) for v in by_row.values()}), 1)  # slot columns and stages aligned

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
        with open(p, "w") as f:  # older OpenROAD (LibreLane's 2026-02 build) says overflow:
            f.write(self.REPORT.replace("congestion:", "overflow:"))
        self.assertEqual(congestion.parse(p)["overflow"], 5)
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


class Precheck(unittest.TestCase):
    """ChipFoundry's cf-precheck: project export, golden root, command, results."""

    def test_export_golden_and_command(self):
        from sipath import harness, precheck as pc
        d = tempfile.mkdtemp()
        gds, net = os.path.join(d, "w.gds"), os.path.join(d, "w.v")
        for f in (gds, net):
            _write_text(f, "x")
        out = pc.export_project(os.path.join(d, "proj"), "openframe_project_wrapper", gds, net)
        self.assertTrue(out["gds"].endswith("proj/gds/openframe_project_wrapper.gds"))
        self.assertTrue(out["netlist"].endswith("proj/verilog/gl/openframe_project_wrapper.v"))
        cfg = _read_json(out["lvs_config"])  # cf-precheck's own config for the flat wrapper
        self.assertEqual(cfg, {"INCLUDE_CONFIGS": ["$LVS_ROOT/tech/$PDK/lvs_config.openframe_project_wrapper.json"]})
        from sipath import scores
        self.assertGreater(scores.auto({"precheck_pass": True, "precheck_failed": 0}),
                           scores.auto({"precheck_pass": False, "precheck_failed": 1}))
        h = harness.get("openframe", {})
        root = pc.golden_root(os.path.join(h.root, h.spec["precheck"]["golden"]), os.path.join(d, "golden"))
        for f in ("Makefile", "verilog/gl/caravel_openframe.v", "gds/openframe_project_wrapper_empty.gds"):
            self.assertTrue(os.path.getsize(os.path.join(root, f)) > 0, f)  # the .gds.gz is decompressed
        cmd = pc.command("/p", "/pdk/sky130A", "/g", "/o", ["lvs"], True)
        self.assertEqual(cmd, ["cf-precheck", "-i", "/p", "-p", "/pdk/sky130A", "-c", "/g", "-o", "/o",
                               "--magic-drc", "--skip-checks", "lvs"])

    def test_results_never_an_empty_pass(self):
        from sipath import precheck as pc
        d = tempfile.mkdtemp()
        self.assertFalse(pc.results(d)["pass"])  # no file
        os.makedirs(os.path.join(d, ".cf"))
        path = os.path.join(d, ".cf", "project.json")

        def write(blob):
            with open(path, "w") as f:
                json.dump({"other": 1, "precheck": blob}, f)
        write({"version": "1.3.7", "passed": True, "checks": {}})
        self.assertFalse(pc.results(d)["pass"])  # no checks
        write({"version": "1.3.7", "passed": True, "checks": {"xor": {"status": "pass"}, "lvs": {"status": "pass"},
                                                              "magic_drc": {"status": "skip"}}})
        r = pc.results(d)
        self.assertTrue(r["pass"])
        self.assertEqual((r["passed"], r["skipped"], r["version"]), (["lvs", "xor"], ["magic_drc"], "1.3.7"))
        write({"version": "1.3.7", "passed": False, "checks": {"xor": {"status": "pass"},
                                                               "lvs": {"status": "fail", "details": "mismatch"}}})
        r = pc.results(d)
        self.assertEqual((r["pass"], r["failed"]), (False, ["lvs"]))
        write({"version": "1.3.7", "passed": True, "checks": {"lvs": {"status": "fail"}}})  # tool and checks disagree
        self.assertFalse(pc.results(d)["pass"])

    def test_step_registered_as_a_tool(self):
        from sipath import params, steps
        self.assertTrue(steps.STEPS["precheck"].tool)
        self.assertNotIn("threads", params.DECLARED["precheck"])


class TutorialNotebook(unittest.TestCase):
    """notebooks/sipath_tutorial.ipynb: generated by build_tutorial.py, every recipe, real commands."""
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_tutorial",
                                                      os.path.join(self.ROOT, "notebooks", "build_tutorial.py"))
        self.nb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.nb)

    def test_committed_notebook_is_the_generated_one(self):
        with open(self.nb.OUT) as f:
            self.assertEqual(f.read(), self.nb.text(), "run python3 notebooks/build_tutorial.py")

    def test_every_recipe_and_real_commands(self):
        from sipath import __main__ as cli
        heads = [s for k, s in self.nb.CELLS if k == "markdown"]
        with open(os.path.join(self.ROOT, "docs", "examples.md")) as f:
            anchors = re.findall(r"\(#(\d+-[^)]+)\)", f.read())
        for a in anchors:  # each recipe of docs/examples.md has a section linking to it
            self.assertTrue(any(f"examples.md#{a}" in h for h in heads), a)
        for kind, src in self.nb.CELLS:
            for m in re.finditer(r"python3 -m sipath ([\w-]+)", src):
                self.assertIn(f"python3 -m sipath {m.group(1)}", cli.__doc__, m.group(0))
            for m in re.finditer(r'"-m", "sipath", "([\w-]+)"', src):
                self.assertIn(f"python3 -m sipath {m.group(1)}", cli.__doc__, m.group(0))
            for m in re.finditer(r'derive\("(\w+)"', src):
                self.assertTrue(os.path.isfile(os.path.join(self.ROOT, "sipath", "examples", m.group(1) + ".yaml")), m.group(0))


class TutorialSmoke(unittest.TestCase):
    """notebooks/smoke.py: any `!` command that fails fails its cell; a retry the notebook means (a subprocess
    loop, only its last try counting) is not a failure."""
    ROOT = TutorialNotebook.ROOT

    def test_no_cell_retries_through_shell_commands(self):
        nb = TutorialNotebook.setUp.__get__(self)  # build_tutorial's cells
        nb()
        for kind, src in self.nb.CELLS:
            if kind == "code" and "_exit_code" in src:  # a `!` retry: smoke.py counts its failed tries
                self.fail("a cell retries through `!` and _exit_code: use a subprocess loop\n" + src)

    def smoke(self, cells):
        import subprocess
        try:
            import IPython  # noqa: F401
        except ImportError:
            self.skipTest("IPython is not installed (the tutorial job installs it)")
        with tempfile.TemporaryDirectory() as td:
            nb = os.path.join(td, "nb.ipynb")
            _write_json(nb, {"cells": [{"cell_type": "code", "source": c} for c in cells], "metadata": {},
                             "nbformat": 4, "nbformat_minor": 5})
            return subprocess.run([sys.executable, os.path.join(self.ROOT, "notebooks", "smoke.py"), nb],
                                  capture_output=True, text=True).returncode

    def test_a_meant_retry_passes_and_a_failed_command_fails(self):
        retry = ("import subprocess, sys, os, tempfile\nf = os.path.join(tempfile.gettempdir(), 'smoke_retry_try')\n"
                 "open(f, 'w').close() if not os.path.exists(f) else None\n"
                 "for k in range(3):\n"
                 "    r = subprocess.run(['sh', '-c', f'test -s {f} || {{ echo x > {f}; exit 1; }}'])\n"
                 "    if r.returncode == 0:\n        break\nos.remove(f)\n")
        self.assertEqual(self.smoke([retry]), 0)
        self.assertEqual(self.smoke(["!false\n!true\n"]), 1)


class NativeTools(unittest.TestCase):
    """install-tools: the locked image unpacked without Docker (no network here)."""

    def test_wrappers_leave_the_images_glibc_to_the_hosts_loader(self):
        """A newer host's loader dies on the image's glibc (SIGSEGV with no output, Ubuntu 24.04): the library
        path keeps every other library of the image and none of glibc's."""
        from sipath.tools import native
        with tempfile.TemporaryDirectory() as td:
            libdir = os.path.join(td, "usr", "lib", "x86_64-linux-gnu")
            os.makedirs(os.path.join(td, "opt", "or-tools", "lib"))
            os.makedirs(libdir)
            os.symlink("usr/lib", os.path.join(td, "lib"))  # the image's /lib is /usr/lib
            for n in ("libc.so.6", "libm.so.6", "libpthread.so.0", "ld-linux-x86-64.so.2", "libnss_files.so.2",
                      "libstdc++.so.6", "libtcl8.6.so", "libmagic.so.1", "libcrypt.so.1"):
                open(os.path.join(libdir, n), "w").close()
            open(os.path.join(td, "opt", "or-tools", "lib", "libortools.so"), "w").close()
            dirs = native.without_glibc(td, ["opt/or-tools/lib", "lib/x86_64-linux-gnu", "usr/lib/x86_64-linux-gnu"])
            self.assertEqual(dirs, ["opt/or-tools/lib", "sipath-lib/lib__x86_64-linux-gnu"])  # /lib listed once
            farm = os.path.join(td, dirs[1])
            self.assertEqual(sorted(os.listdir(farm)), ["libcrypt.so.1", "libmagic.so.1", "libstdc++.so.6",
                                                        "libtcl8.6.so"])
            self.assertTrue(os.path.samefile(os.path.join(farm, "libtcl8.6.so"), os.path.join(libdir, "libtcl8.6.so")))
            path = re.search(r'LD_LIBRARY_PATH="([^"]*)"', native._wrapper(td, "openroad", "x", dirs)).group(1)
            self.assertEqual([p for p in path.split(":") if p.endswith(("/lib/x86_64-linux-gnu", "-linux-gnu}"))], [])


    def _layer(self, entries):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, kind, data in entries:
                ti = tarfile.TarInfo(name)
                if kind == "dir":
                    ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                    tar.addfile(ti)
                elif kind == "sym":
                    ti.type, ti.linkname = tarfile.SYMTYPE, data
                    tar.addfile(ti)
                else:
                    ti.size = len(data)
                    tar.addfile(ti, io.BytesIO(data))
        buf.seek(0)
        return buf

    def test_layers_whiteouts_links_and_skipped_parts(self):
        from sipath import nativetools as nt
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        nt._unpack(self._layer([("usr", "dir", None), ("usr/bin", "dir", None), ("usr/bin/a", "file", b"1"),
                                ("usr/bin/gone", "file", b"x"), ("opt/d", "dir", None), ("opt/d/old", "file", b"o"),
                                ("usr/include/h.h", "file", b"h"), ("bin", "sym", "usr/bin"),
                                ("usr/bin/abs", "sym", "/usr/bin/a")]), root)
        nt._unpack(self._layer([("usr/bin/.wh.gone", "file", b""), ("opt/d/.wh..wh..opq", "file", b""),
                                ("opt/d/new", "file", b"n"), ("usr/bin/a", "file", b"2")]), root)
        self.assertEqual(_read_text(os.path.join(root, "usr/bin/a")), "2")  # a later layer wins
        self.assertFalse(os.path.lexists(os.path.join(root, "usr/bin/gone")))  # whiteout
        self.assertEqual(os.listdir(os.path.join(root, "opt/d")), ["new"])  # opaque directory
        self.assertFalse(os.path.exists(os.path.join(root, "usr/include")))  # skipped part
        link = os.path.join(root, "usr/bin/abs")
        self.assertFalse(os.readlink(link).startswith("/"))  # absolute link kept inside root
        self.assertEqual(_read_text(link), "2")

    def test_a_cut_download_resumes_and_is_checked(self):
        import hashlib
        import http.client
        import io
        from sipath import nativetools as nt
        blob = bytes(range(256)) * 4000
        layer = {"digest": "sha256:" + hashlib.sha256(blob).hexdigest(), "size": len(blob)}
        starts = []

        class Resp(io.BytesIO):
            def __init__(self, data, status, cut=False):
                super().__init__(data)
                self.status, self.cut = status, cut

            def read(self, n=-1):
                if self.cut and self.tell() >= 100_000:  # the connection drops after 100 kB
                    raise http.client.IncompleteRead(b"")
                return super().read(min(n, 100_000) if n > 0 else 100_000)

        def get(url, token, accept=None, start=0):
            starts.append(start)
            return Resp(blob[start:], 206 if start else 200, cut=not start)
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        with unittest.mock.patch.object(nt, "_get", get), unittest.mock.patch.object(nt, "_token", lambda h, r: None), \
                unittest.mock.patch("time.sleep"):
            path = nt._download("h", "r", layer, os.path.join(root, "l"), log=lambda s: None)
            self.assertEqual(_read_bytes(path), blob)
            self.assertEqual(starts[0], 0)
            self.assertGreater(starts[1], 0)  # the second try asked for the rest only
            bad = {"digest": "sha256:" + "0" * 64, "size": len(blob)}
            with self.assertRaisesRegex(RuntimeError, "does not match its digest"):
                nt._download("h", "r", bad, os.path.join(root, "m"), log=lambda s: None)

    def test_registry_refs_wrappers_and_host_check(self):
        from sipath import nativetools as nt
        self.assertEqual(nt._registry("openroad/orfs@sha256:ab"), ("registry-1.docker.io", "openroad/orfs", "sha256:ab"))
        self.assertEqual(nt._registry("ghcr.io/x/y@sha256:cd"), ("ghcr.io", "x/y", "sha256:cd"))
        with self.assertRaisesRegex(ValueError, "not pinned"):
            nt._registry("openroad/orfs:latest")
        w = nt._wrapper("/r", "klayout", nt.TOOLS["klayout"])
        self.assertIn('exec "$R/usr/bin/klayout" "$@"', w)
        self.assertIn("$R/opt/or-tools/lib", w)
        self.assertIn("PYTHONHOME", w)
        if sys.platform != "linux":
            with self.assertRaisesRegex(RuntimeError, "Linux x86_64"):
                nt.install(tempfile.mkdtemp())


class PictureTargets(unittest.TestCase):
    """render/draw name a run's checkpoint by work directory and step (viz.checkpoints)."""

    def setUp(self):
        self.wd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.wd)
        at = lambda st, k, s, ck, par: {"id": f"{st}.{k}", "step": st, "status": s, "checkpoint": ck,  # noqa: E731
                                        "parent": par}
        _write_json(os.path.join(self.wd, "graph.json"), {"attempts": [
            at("synth", 0, "selected", "s/0/top.synth.v", None), at("place", 0, "rejected", "p/0/place.odb", "synth.0"),
            at("place", 1, "selected", "p/1/place.odb", "synth.0"), at("cts", 0, "selected", "c/0/cts.odb", "place.1")],
            "steps": {"synth": {"selected": "synth.0"}, "place": {"selected": "place.1", "parent": "synth.0"},
                      "cts": {"selected": "cts.0", "parent": "place.1"}}, "head": "cts.0"})

    def test_step_attempt_all_and_default(self):
        from sipath import viz
        self.assertEqual(viz.checkpoints(self.wd, "place"), [("place.1", "p/1/place.odb")])
        self.assertEqual(viz.checkpoints(self.wd, "place.0"), [("place.0", "p/0/place.odb")])  # a rejected one too
        self.assertEqual(viz.checkpoints(self.wd, "all"), [("place.1", "p/1/place.odb"), ("cts.0", "c/0/cts.odb")])
        self.assertEqual(viz.checkpoints(self.wd), [("cts.0", "c/0/cts.odb")])

    def test_errors_name_what_the_run_has(self):
        from sipath import viz
        with self.assertRaisesRegex(ValueError, "steps with one: place, cts"):
            viz.checkpoints(self.wd, "route_digital")
        with self.assertRaisesRegex(ValueError, "no layout database"):
            viz.checkpoints(self.wd, "synth.0")
        with self.assertRaises(FileNotFoundError):
            viz.checkpoints(os.path.join(self.wd, "nope"))

    def test_command_line_outputs(self):
        from sipath import viz
        pics = os.path.join(self.wd, "_pictures")
        self.assertEqual(viz.picture_args([self.wd, "all"], "png"),
                         [("p/1/place.odb", os.path.join(pics, "place.1.png")), ("c/0/cts.odb", os.path.join(pics, "cts.0.png"))])
        self.assertEqual(viz.picture_args([self.wd, "place", "-o", "x.svg", "--ring"], "svg"), [("p/1/place.odb", "x.svg")])
        self.assertEqual(viz.picture_args(["a/b.odb", "o.png"], "png"), [("a/b.odb", "o.png")])  # the old form
        with self.assertRaisesRegex(ValueError, "one file"):
            viz.picture_args([self.wd, "all", "-o", "x.png"], "png")


class ExploreFlow(unittest.TestCase):
    def test_routing_scores_need_no_drc_in_explore_mode(self):
        from sipath.designfile import explore_flow
        out = explore_flow([{"step": "synth"}, {"step": "place", "score": "routability_then_wns"},
                            {"step": "route_digital", "variants": [{"repair_after_grt": True}],
                             "score": "drc_then_slack"}, {"step": "signoff"}])
        steps = {i["step"]: i for i in out}
        self.assertNotIn("signoff", steps)
        self.assertEqual(steps["route_digital"]["score"], "setup_wns")  # no DRC count at global routing
        self.assertEqual(steps["route_digital"]["variants"], [{"repair_after_grt": True, "detailed": False}])
        self.assertEqual(steps["place"]["score"], "routability_then_wns")


class DocExamples(unittest.TestCase):
    """docs/examples.md: every snippet uses keys, steps, parameters, scores and
    commands SiPath accepts."""

    DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "docs", "examples.md")

    def blocks(self, lang):
        with open(self.DOC) as f:
            text = f.read()
        return re.findall(r"```" + lang + r"\n(.*?)```", text, re.S)

    def test_yaml_snippets_are_valid(self):
        import yaml
        from sipath import schema, scores, steps
        blocks = self.blocks("yaml")
        self.assertGreaterEqual(len(blocks), 10)
        for b in blocks:
            data = yaml.safe_load(b)
            schema.validate(data, required=False)  # every key and type, nested
            for item in data.get("flow") or []:
                ref = item.get("fn", item["step"])
                if ":" in ref:  # a step by import path (the doc's own example package)
                    continue
                steps.check_params(ref, item.get("variants"))
                if "score" in item:
                    self.assertIn(item["score"], scores.SCORES, item)

    def test_commands_exist(self):
        from sipath import __main__ as cli
        usage = cli.__doc__ or ""
        for b in self.blocks("bash"):
            for line in b.splitlines():
                m = re.match(r"python3 -m sipath (\S+)", line.strip())
                if m:
                    self.assertIn(f"python3 -m sipath {m.group(1)}", usage, line)
                m = re.match(r"python3 (bench/\S+\.py)", line.strip())
                if m:
                    root = os.path.dirname(os.path.dirname(self.DOC))
                    self.assertTrue(os.path.isfile(os.path.join(root, m.group(1))), line)

    def test_examples_named_exist(self):
        root = os.path.dirname(os.path.dirname(self.DOC))
        with open(self.DOC) as f:
            text = f.read()
        for path in set(re.findall(r"`(sipath/examples/[\w./]+)`", text)):
            self.assertTrue(os.path.exists(os.path.join(root, path)), path)


class StepsPackage(unittest.TestCase):
    """P2.3: steps split by stage, signoff checks as steps, steps by import path."""

    def test_every_name_still_importable(self):
        from sipath import steps
        for n in ("floorplan", "macros", "physical", "place", "cts", "route_analog", "route_digital",
                  "route_repair", "fill", "extract", "signoff", "views", "integrate", "ppa", "synth",
                  "check_placement", "run_rcx", "reabut_all", "fence_violations", "enforce_fences",
                  "datapath_check", "reconnect_supplies", "grid_nets", "fit_straps", "_drt_numbers",
                  "_via_array", "_no_supply_shorts", "design_rule_counts", "check_antennas", "shield_nets",
                  "analog_coupling", "STEPS", "STEP_PARAMS", "check_params"):
            self.assertTrue(hasattr(steps, n), n)
        self.assertTrue(steps.STEPS["integrate"].tool)
        self.assertEqual(steps.STEPS["route_analog"].__module__, "sipath.steps.analog_route")

    def test_signoff_checks_as_steps(self):
        from sipath import params, steps
        for name, checks in params.CHECK_STEPS.items():
            self.assertIn(name, steps.STEPS)
            self.assertIn(", ".join(checks), steps.STEPS[name].__doc__)
        self.assertEqual(params.DECLARED["lvs_full"]["require"].default, ("lvs", "lvs_full"))
        self.assertIn("drc_split", params.DECLARED["drc"])
        steps.check_params("timing", [{"require": ["setup", "hold"]}])
        with self.assertRaises(ValueError):
            steps.check_params("timing", [{"require": ["drc"]}])  # not one of its checks

    def test_step_by_import_path(self):
        from sipath import registry, steps
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "p23_user_steps.py"), "w") as f:
            f.write("from sipath.params import Param\n"
                    "def count_cells(ctx):\n    ctx.metrics['n'] = ctx.params['k']\n"
                    "count_cells.params = [Param('k', 'int', 1, 'a number', min=0)]\n")
        sys.path.insert(0, d)
        self.addCleanup(sys.path.remove, d)
        ref = registry.import_step("p23_user_steps:count_cells")
        self.assertIs(steps.STEPS[ref].__name__, "count_cells")
        steps.check_params(ref, [{"k": 3}])
        with self.assertRaises(ValueError):
            steps.check_params(ref, [{"k": -1}])
        self.assertEqual(registry.import_step(ref), ref)  # once
        for bad in ("p23_user_steps:nope", "no_such_module_p23:f", "p23_user_steps"):
            with self.assertRaises(registry.RegistryError):
                registry.import_step(bad)


class PdkRelease(unittest.TestCase):
    """The open_pdks release of a PDK tree, pinned in toolchain.json."""

    def setUp(self):
        from sipath import toolchain
        self.tc = toolchain
        self.root = tempfile.mkdtemp()
        self.locked = toolchain.lock()["pdks"]["sky130"]["version"]

    def _tree(self, name, sources=None, version_dir=None):
        base = os.path.join(self.root, "ciel", "sky130", "versions", version_dir) if version_dir else self.root
        tree = os.path.join(base, name)
        os.makedirs(tree)
        if sources is not None:
            with open(os.path.join(tree, "SOURCES"), "w") as f:
                f.write(sources)
        return tree

    def _pdk(self, tree):
        return types.SimpleNamespace(name="sky130A", release={"family": "sky130", "tree": tree})

    def test_release_from_sources_or_ciel_path(self):
        self.assertEqual(self.tc.pdk_release(self._tree("a", "open_pdks abc1234def\nmagic 8.3\n")), "abc1234def")
        v = self._tree("sky130A", version_dir="0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(self.tc.pdk_release(v), "0123456789abcdef0123456789abcdef01234567")
        self.assertIsNone(self.tc.pdk_release(self._tree("b")))

    def test_other_release_refused_unless_allowed(self):
        tree = self._tree("c", "open_pdks 0000000\n")
        st = self.tc.pdk_status(self._pdk(tree))
        self.assertEqual((st["version"], st["locked"], st["match"]), ("0000000", self.locked, False))
        with self.assertRaisesRegex(RuntimeError, "install-pdk sky130"):
            self.tc.require_pdk(self._pdk(tree), types.SimpleNamespace(require_pdk_release=True))
        allowed = self.tc.require_pdk(self._pdk(tree), types.SimpleNamespace(require_pdk_release=False))
        self.assertFalse(allowed["match"])  # recorded in provenance, not refused
        good = self._tree("d", f"open_pdks {self.locked}\n")
        self.assertTrue(self.tc.require_pdk(self._pdk(good), types.SimpleNamespace(require_pdk_release=True))["match"])

    def test_missing_libraries_reported(self):
        tree = self._tree("e", f"open_pdks {self.locked}\n")
        os.makedirs(os.path.join(tree, "libs.ref", "sky130_fd_sc_hd"))
        st = self.tc.pdk_status(self._pdk(tree))
        self.assertTrue(st["match"])
        self.assertIn("sky130_fd_sc_hs", st["missing_libraries"])
        self.assertNotIn("sky130_fd_sc_hd", st["missing_libraries"])

    def test_unpinned_pdk_and_install_command(self):
        self.assertIsNone(self.tc.pdk_status(types.SimpleNamespace(name="sky130hd", release={})))
        cmd = self.tc.install_command("sky130", "/x/pdks")
        self.assertEqual(cmd[:6], ["ciel", "enable", "--pdk-root", "/x/pdks", "--pdk-family", "sky130"])
        self.assertEqual(cmd[-1], self.locked)
        libs = cmd[7:-1:2]  # one -l per library: ciel's default set lacks the hs cells
        self.assertEqual(cmd[6:-1:2], ["-l"] * len(libs))
        self.assertIn("sky130_fd_sc_hs", libs)
        self.assertIn("sky130_fd_sc_hd", libs)
        with self.assertRaises(ValueError):
            self.tc.install_command("gf180mcu")


class SpefCoupling(unittest.TestCase):
    SPEF = """*SPEF "ieee 1481-1999"
*C_UNIT 1 PF

*NAME_MAP
*1 ana\\.in
*2 clk
*3 vref
*4 d0

*D_NET *1 0.050
*CONN
*I *10:A I
*CAP
1 *1:1 0.040
2 *1:2 *2:5 0.003
3 *1:2 *3:1 0.002
4 *1:3 *20:Y 0.001
*RES
1 *1:1 *1:2 1.0
*END

*D_NET *2 0.020
*CONN
*I *30:Y O
*CAP
1 *2:5 0.010
2 *2:6 *1:3 0.004
*END

*D_NET *4 0.010
*CONN
*I *20:Y O
*CAP
1 *4:1 0.009
*END
"""

    def test_pairs_from_either_section_and_instance_pins(self):
        pairs, total = analog.spef_coupling(self.SPEF)
        self.assertAlmostEqual(pairs[("ana.in", "clk")], 7.0)  # 3 fF in ana's section + 4 fF in clk's
        self.assertAlmostEqual(pairs[("ana.in", "vref")], 2.0)
        self.assertAlmostEqual(pairs[("ana.in", "d0")], 1.0)  # far node *20:Y is d0's driver pin
        self.assertAlmostEqual(total["ana.in"], 50.0)

    def test_report_splits_digital_and_analog(self):
        path = os.path.join(tempfile.mkdtemp(), "x.spef")
        with open(path, "w") as f:
            f.write(self.SPEF)
        r = analog.coupling_report([path], ["ana.in", "vref"])["ana.in"]
        self.assertAlmostEqual(r["digital_ff"], 8.0)
        self.assertEqual([o for o, _ in r["aggressors"]], ["clk", "d0"])
        self.assertEqual(list(r["analog_ff"]), ["vref"])


class Shielding(unittest.TestCase):
    """Shield wires beside an analog net: offset, clipping, grounding, keep-away."""

    def setUp(self):
        from sipath import shield
        self.sh = shield
        lay = {"dir": "HORIZONTAL", "width": 300, "space": 300, "area": 0}
        self.rules = shield.Rules({"met3": lay, "met4": dict(lay, dir="VERTICAL")},
                                  {"met3": [(10 ** 12, 300)], "met4": [(10 ** 12, 300)]}, 5)
        self.vias = {"met3": {"name": "M3M4", "pads": {"met3": (-190, -190, 190, 190), "met4": (-200, -200, 200, 200)}}}
        self.special = [("ana", "met3", "wire", (0, 1000, 20000, 1600)),     # the analog wire, 0.6 um wide
                        ("VSS", "met4", "wire", (10000, 0, 11600, 5000)),    # a ground stripe crossing it
                        ("d0", "met3", "wire", (4000, 2000, 5000, 3000))]    # another net near its top side

    def test_pieces_clipped_grounded_and_floating_ones_dropped(self):
        r = self.sh.plan(["ana"], "VSS", self.special, [], [], self.rules, ["met3", "met4"], {"met3", "met4"},
                         self.vias)["ana"]
        self.assertEqual(sorted(r["pieces"]), [("met3", (0, 400, 20000, 700)), ("met3", (5300, 1900, 20000, 2200))])
        self.assertEqual(r["dropped"], 1)  # the top piece left of d0 crosses no ground
        # a via per piece on the stripe, its met3 pad flush with the shield's inner edge
        self.assertEqual(sorted(r["vias"]), [("M3M4", 10800, 510), ("M3M4", 10800, 2090)])
        self.assertEqual((r["covered"], r["sides"]), (34700, 40000))

    def test_no_via_on_or_beside_another_ground_via(self):
        # a grid via already sits where the top shield's via would go (the stripe crossing): that piece is
        # dropped rather than stacking cuts; the bottom one still gets its via
        special = self.special + [("VSS", "met4", "via", (10650, 1950, 10950, 2250))]
        r = self.sh.plan(["ana"], "VSS", special, [], [], self.rules, ["met3", "met4"], {"met3", "met4"},
                         self.vias)["ana"]
        self.assertEqual(r["vias"], [("M3M4", 10800, 510)])
        self.assertEqual(r["dropped"], 2)

    def test_no_sliver_touch_with_another_ground_shape(self):
        from sipath.shield import _sliver
        self.assertTrue(_sliver((0, 0, 1000, 300), (960, 250, 2000, 600), 300))  # 40 x 50 overlap: a neck
        self.assertFalse(_sliver((0, 0, 1000, 300), (500, 0, 2000, 300), 300))   # a full-width join
        self.assertFalse(_sliver((0, 0, 1000, 300), (1000, 0, 2000, 300), 300))  # abutting: not an overlap

    def test_shield_keeps_the_spacing_from_the_wire(self):
        (top, _, _), (bot, _, _) = self.sh.candidates((0, 1000, 20000, 1600), "met3", self.rules, 300)
        self.assertEqual(top[1] - 1600, 300)
        self.assertEqual(1000 - bot[3], 300)
        self.assertEqual(self.sh.candidates((0, 0, 500, 600), "met3", self.rules, 300), [])  # a via landing

    def test_layers_not_allowed_are_reported(self):
        r = self.sh.plan(["ana"], "VSS", self.special, [], [], self.rules, ["met3", "met4"], {"met4"}, self.vias)
        self.assertEqual((r["ana"]["pieces"], r["ana"]["skipped_layers"]), ([], ["met3"]))

    def test_tie_strap_to_a_parallel_stripe(self):
        # a met4 analog wire beside a parallel met4 ground stripe: nothing crosses its shields, so the
        # right one gets a met3 strap with a via on the shield and one inside the stripe; the left one
        # reaches no ground and is dropped
        special = [("ana", "met4", "wire", (10000, 0, 10600, 30000)), ("VSS", "met4", "wire", (20000, 0, 21600, 30000))]
        r = self.sh.plan(["ana"], "VSS", special, [], [], self.rules, ["met3", "met4"], {"met3", "met4"}, self.vias,
                         strap_layers={"met3"}, reach=15000)["ana"]
        self.assertEqual(r["pieces"], [("met4", (10900, 0, 11200, 30000))])
        self.assertEqual(r["ties"], [("met3", (10910, 14810, 20990, 15190))])
        self.assertEqual(sorted(r["vias"]), [("M3M4", 11100, 15000), ("M3M4", 20800, 15000)])
        self.assertEqual(r["dropped"], 1)
        none = self.sh.plan(["ana"], "VSS", special, [], [], self.rules, ["met3", "met4"], {"met3", "met4"},
                            self.vias, strap_layers={"met3"}, reach=5000)["ana"]  # the stripe is out of reach
        self.assertEqual((none["pieces"], none["dropped"]), ([], 2))

    def test_tie_strap_to_a_stripe_one_layer_beyond(self):
        # a met2 shield (outside the rows: nothing crosses it), a met3 strap outward, a via up into a met4 stripe
        lay = lambda w: {"dir": "VERTICAL", "width": w, "space": w, "area": 0}  # noqa: E731
        rules = self.sh.Rules({"met2": lay(140), "met3": lay(300), "met4": lay(300)},
                              {"met2": [(10 ** 12, 140)], "met3": [(10 ** 12, 300)], "met4": [(10 ** 12, 300)]}, 5)
        vias = {"met2": {"name": "M2M3", "pads": {"met2": (-140, -185, 140, 185), "met3": (-165, -165, 165, 165)}},
                "met3": self.vias["met3"]}
        special = [("ana", "met2", "wire", (10000, 0, 10280, 30000)), ("VSS", "met4", "wire", (15000, 0, 16600, 30000))]
        r = self.sh.plan(["ana"], "VSS", special, [], [], rules, ["met2", "met3", "met4"], {"met2", "met3", "met4"},
                         vias, strap_layers={"met3"}, reach=10000)["ana"]
        self.assertEqual(r["pieces"], [("met2", (10420, 0, 10560, 30000))])
        self.assertEqual(r["ties"], [("met3", (10395, 14810, 15990, 15190))])
        self.assertEqual(sorted(r["vias"]), [("M2M3", 10560, 15000), ("M3M4", 15800, 15000)])

    def test_guard_keeps_the_ndr_spacing_and_clears_pins(self):
        boxes = self.sh.guard({"ana": {"met3": 600}}, self.special, [("met3", (5000, 2000, 5500, 2500))], [],
                              self.rules)
        self.assertEqual(boxes, [("met3", (-300, 700, 4700, 1900)), ("met3", (5800, 700, 20300, 1900))])
        self.assertEqual(self.sh.guard({"ana": {"met3": 300}}, self.special, [], [], self.rules), [])  # NDR = minimum

    def test_keepaway_cut_around_pins(self):
        boxes = self.sh.keepaway({"ana"}, self.special, [("met4", (15000, 1000, 15500, 1500))], [], self.rules,
                                 ["met3", "met4"], set())
        self.assertEqual(boxes, [("met4", (0, 400, 14700, 2200)), ("met4", (15800, 400, 20000, 2200))])
        self.assertEqual(self.sh.keepaway({"ana"}, self.special, [], [], self.rules, ["met3", "met4"], {"met4"}), [])


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


class Layers(unittest.TestCase):
    """The layers (docs/modules.md): core knows no tool, tools know no step, nothing below flows imports a
    flow, and library code imports the moved modules from sipath.tools (not their compatibility aliases)."""
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    CORE = {"scheduler", "params", "scores", "registry", "config", "report", "recipes", "toolchain", "fingerprint",
            "verdict"}
    MODEL = {"design", "pdk", "netlist", "lef", "heatmap", "sdc"}
    FLOWS = {"designfile", "mcp_server", "__main__", ""}  # "": the package's __init__ (its public API)
    ALIASES = {"session", "tcl", "tclquote", "nativetools"}  # = sipath.tools.openroad.*, sipath.tools.native
    FORBIDDEN = {("synth", "signoff")}  # synthesis reaches its tools through sipath.tools

    def graph(self):
        import ast
        mods = {}
        for dp, dn, fn in os.walk(self.ROOT):
            dn[:] = [x for x in dn if x not in ("tests", "third_party", "examples", "templates", "__pycache__")]
            for f in fn:
                if f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dp, f), self.ROOT)[:-3].replace(os.sep, ".")
                    mods[rel.removesuffix("__init__").rstrip(".")] = os.path.join(dp, f)
        out = {}
        for name, path in mods.items():
            if name in self.ALIASES:
                continue
            pkg = name if path.endswith("__init__.py") else name.rpartition(".")[0]
            deps = set()
            with open(path) as fh:
                tree = ast.parse(fh.read())
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.level:
                    base = [x for x in pkg.split(".") if x]
                    base = base[:len(base) - (n.level - 1)] if n.level > 1 else base
                    m = ".".join(base + ([n.module] if n.module else []))
                    # from . import x: the module x (not the package's __init__)
                    cands = ([m] if n.module else []) + [f"{m}.{a.name}".lstrip(".") for a in n.names]
                elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("sipath"):
                    cands = [n.module[len("sipath."):] if n.module != "sipath" else ""]
                elif isinstance(n, ast.Import):
                    cands = [a.name[len("sipath."):] for a in n.names if a.name.startswith("sipath.")]
                else:
                    continue
                deps |= {c for c in cands if c in mods and c != name}
            out[name] = deps
        return out

    def layer(self, m):
        if m in self.CORE:
            return "core"
        if m in self.MODEL:
            return "model"
        if m == "tools" or m.startswith("tools."):
            return "tools"
        if m in self.FLOWS:
            return "flows"
        if m in self.ALIASES:
            return "alias"
        return "steps"

    def test_import_rules(self):
        allowed = {"core": {"core", "model"}, "model": {"core", "model"}, "tools": {"core", "model", "tools"},
                   "steps": {"core", "model", "tools", "steps"}, "flows": {"core", "model", "tools", "steps", "flows"}}
        g = self.graph()
        self.assertIn("scheduler", g)
        self.assertIn("tools.openroad.checkpoint", g)
        bad = sorted(f"{m} ({self.layer(m)}) imports {d} ({self.layer(d)})" for m, deps in g.items() for d in deps
                     if self.layer(d) not in allowed[self.layer(m)] or (m, d) in self.FORBIDDEN)
        self.assertEqual(bad, [])

    def test_steps_talk_to_openroad_only_through_its_operations(self):
        """Phase 2: the step modules keep the stage logic; every Tcl command is in sipath.tools.openroad.ops. B3.4: so
        do the libraries eco, macroplace, congestion and minarea."""
        import ast
        bad = []
        sdir = os.path.join(self.ROOT, "steps")
        files = [os.path.join(sdir, f) for f in sorted(os.listdir(sdir)) if f.endswith(".py")]
        files += [os.path.join(self.ROOT, f"{m}.py") for m in ("eco", "macroplace", "congestion", "minarea")]
        for path in files:
            f = os.path.relpath(path, self.ROOT)
            with open(path) as fh:
                tree = ast.parse(fh.read())
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("run", "value")):
                    continue
                recv = n.func.value  # a session: s, session, or <anything>.session
                if (isinstance(recv, ast.Name) and recv.id in ("s", "session")) or \
                        (isinstance(recv, ast.Attribute) and recv.attr == "session"):
                    bad.append(f"{f}:{n.lineno}: .{n.func.attr}(...)")
        self.assertEqual(bad, [])

    def test_the_rules_catch_a_violation(self):
        g = self.graph()
        self.assertNotIn("eco", g["scheduler"])  # what this phase removed
        self.assertNotIn("designfile", g["harness"])
        self.assertEqual(self.layer("eco"), "steps")
        self.assertEqual(self.layer("tools.openroad.tcl"), "tools")
        self.assertEqual(self.layer("tcl"), "alias")


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
        # the release ships no Calibre RCX rules for sky130B: the PDK config's spef_extractor set
        self.assertTrue(b.rcx_rules.endswith("rules.openrcx.sky130B.nom.spef_extractor"))
        self.assertTrue({x.name: x for x in b.corners}["ff_min"].rcx_rules.endswith("sky130B.min.spef_extractor"))
        self.assertTrue(all(os.path.isfile(f) for f in b.files()))
        self.assertTrue(self.p.rcx_rules.endswith("rules.openrcx.sky130A.nom.calibre"))

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
        t = tcl.rc_setup(self.p)  # in-flow sessions: the corners the flow repairs against
        self.assertEqual(t.count("set_layer_rc -layer met2"), 3)
        self.assertIn("-corner ss_max", t)
        self.assertEqual(tcl.rc_setup(self.p, None, self.p.signoff_corners()).count("set_layer_rc -layer met2"), 9)

    def test_pad_liberty_per_process(self):
        from sipath import designfile
        d, _ = designfile.load(os.path.join(os.path.dirname(designs.__file__), "mcu8.yaml"))
        p = self.p
        names = {c: [os.path.basename(f) for f in tcl.corner_libs(p, d, c) if "gpiov2" in f]
                 for c in ("tt_nom", "ss_max", "ff_min")}
        self.assertEqual(names, {"tt_nom": ["sky130_ef_io__gpiov2_pad_wrapped_tt_tt_025C_1v80_3v30.lib"],
                                 "ss_max": ["sky130_ef_io__gpiov2_pad_wrapped_ss_ss_100C_1v60_3v00.lib"],
                                 "ff_min": ["sky130_ef_io__gpiov2_pad_wrapped_ff_ff_n40C_1v95_5v50.lib"]})
        self.assertTrue(all(os.path.isfile(f) for by in p.io_libs.values() for f in by.values()))
        self.assertTrue(set(p.io_libs["sky130_ef_io__gpiov2_pad_wrapped"].values()) <= set(p.files()))  # mounted
        with open(d.extra_libs[0]) as fh:  # the black-box Liberty no longer models the pad
            self.assertNotIn("gpiov2", fh.read())

    def test_flow_repairs_on_three_corners_signoff_checks_nine(self):
        self.assertEqual([c.name for c in self.p.analysis_corners()], ["tt_nom", "ss_max", "ff_min"])
        self.assertEqual(len(self.p.signoff_corners()), 9)
        self.assertEqual(len(tcl.spef_paths(self.p, "/x/d.spef")), 9)  # extraction serves every corner
        r = tcl.rcx(self.p, "/x/d.spef")
        self.assertEqual(r.count("extract_parasitics"), 3)  # one per RCX rule file (min, nom, max)
        self.assertEqual(r.count("read_spef"), 3)  # into the session's three corners
        self.assertIn("define_corners tt_nom ss_max ff_min", "\n".join(tcl._define_corners(self.p)))

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
        self.assertEqual(r.count("read_spef -corner"), 3)  # the session's corners; SPEFs written for all nine
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
        self.assertEqual((len(hd.exclude), len(hs.exclude)), (60, 51))  # hd: + 7 cells the LVS deck cannot pair
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
                             ("signoff", {"checks": ["drc", "nosuch"]}, "'nosuch' is not one of"),
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
        sc = synth.synth_script(pdk, d, "/o/gcd8.synth.v", "/o/r.abc", constr="/o/abc.constr")
        self.assertIn("synth -top gcd8 -flatten -noabc", sc)
        self.assertIn("-constr /o/abc.constr -script /o/r.abc", sc)  # recipe file, boundary model
        self.assertIn("rename -wire -suffix _reg t:$_DFF*", sc)  # register names kept
        self.assertNotIn(" -D ", sc)  # the delay target lives in the recipe file
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
        # the reference flow ends in signoff on a PDK with signoff decks (sky130A), as gcd8.yaml spells it out;
        # the PDK is given, so the chain does not depend on a PDK tree being installed here
        from types import SimpleNamespace
        decks = SimpleNamespace(signoff={"fill_rules": "fill.drc", "drc": "drc.lydrc"})
        self.assertEqual([s["step"] for s in designfile.default_flow(d, decks)], [s["step"] for s in steps])
        self.assertEqual([s["step"] for s in designfile.default_flow(d, decks)][-3:], ["fill", "extract", "signoff"])
        self.assertEqual([s["step"] for s in designfile.default_flow(d, SimpleNamespace(signoff={}))][-2:],
                         ["route_repair", "extract"])  # without signoff decks it ends at extraction
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


class RtlEquivalence(unittest.TestCase):
    """The synth step's RTL-vs-netlist check (script and reset choice; the SAT
    run itself is in the live RTL test)."""

    def test_script_and_reset(self):
        from sipath import synth
        pdk = load_pdk("sky130A") if SKY130A else None
        if pdk is None:
            self.skipTest("no sky130A")
        d = Design("t", "t", None, "s.sdc", "macro", rtl=["/r/t.v"])
        s = synth.equiv_script(pdk, d, "/n/t.v", 7, ("rst_n", 0), 99)
        self.assertLess(s.index("rename t gold"), s.index("read_verilog /n/t.v"))
        self.assertIn("miter -equiv -flatten -make_assert -ignore_gold_x gold gate miter", s)
        self.assertIn("-set-at 1 in_rst_n 0 -seq 7 -prove-skip 1 -timeout 99 miter", s)
        self.assertNotIn("-set-at", synth.equiv_script(pdk, d, "/n/t.v", 7, None, 99))
        self.assertEqual([n for n, _ in synth.RESET_NAMES][:2], ["rst_n", "resetn"])
        self.assertEqual(synth.find_reset(["PCLK", "PRESETn", "PSEL"]), ("PRESETn", 0))  # AMBA names, any case
        self.assertEqual(synth.find_reset(["clk", "rst_ni"]), ("rst_ni", 0))
        self.assertEqual(synth.find_reset(["clk", "RST"]), ("RST", 1))
        self.assertIsNone(synth.find_reset(["clk", "en"]))


    def test_timeout_policy(self):
        """equiv_on_timeout: warn reports inconclusive, pass records it as accepted, fail fails the step."""
        from types import SimpleNamespace
        from unittest import mock
        from sipath import synth
        with tempfile.TemporaryDirectory() as td:
            net = os.path.join(td, "t.v")
            with open(net, "w") as fh:
                fh.write("module t(input clk, input rst_n, output y); endmodule\n")

            def fake_yosys(ctx, script, log, out):  # what Yosys writes when the SAT run hits its limit
                with open(log, "w") as fh:
                    fh.write("Solving problem with 1234 variables...\nSAT solving timeout reached.\n")
            for policy, want in (("warn", "warn"), ("pass", "pass")):
                ctx = SimpleNamespace(design=Design("t", "t", None, "s.sdc", "macro", rtl=["/r/t.v"]), pdk=None,
                                      params={"equiv_on_timeout": policy, "equiv_timeout": 5}, outputs={},
                                      path=lambda n: os.path.join(td, n))
                with mock.patch.object(synth.yosys, "script", fake_yosys), \
                        mock.patch.object(synth, "equiv_script", lambda *a, **k: ""):
                    r = synth.equivalence(ctx, net)
                self.assertEqual((r["result"], r["on_timeout"]), ("inconclusive (timeout)", want))
            ctx.params = {"equiv_on_timeout": "fail", "equiv_timeout": 5}
            with mock.patch.object(synth.yosys, "script", fake_yosys), \
                    mock.patch.object(synth, "equiv_script", lambda *a, **k: ""):
                with self.assertRaisesRegex(RuntimeError, "timed out after 5 s"):
                    synth.equivalence(ctx, net)


class BlackboxModels(unittest.TestCase):
    """Black-box Liberty models written from a macro's LEF (opt-in)."""
    DAC = DAC_LEF

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


class Plugins(unittest.TestCase):
    """Extension points (sipath.registry) with the example plugin, on stand-in sessions."""

    @classmethod
    def setUpClass(cls):
        from sipath import registry
        registry.load_plugins(["sipath.examples.plugin_example"])

    @classmethod
    def tearDownClass(cls):  # the registry is global: later tests see the built-ins only
        from sipath import params, registry, scores, steps
        steps.STEPS.pop("cell_census", None)
        params.STEP_PARAMS.pop("cell_census", None)
        params.DECLARED.pop("cell_census", None)
        scores.SCORES.pop("fewest_cells", None)
        registry.CHECKS.pop("cell_budget", None)
        registry.EXTENSIONS.pop("example", None)
        registry._LOADED.discard("sipath.examples.plugin_example")
        sys.modules.pop("sipath.examples.plugin_example", None)  # a later load registers again
        registry._refresh_signoff_params()

    def test_registered_things_join_the_builtins(self):
        from sipath import params, registry, scores, steps
        from sipath.mcp_server import Server, tools
        self.assertIn("cell_census", steps.STEPS)
        steps.check_params("cell_census", [{"prefix": "u_core."}])
        with self.assertRaisesRegex(ValueError, "cell_census.prefix: expected str"):
            steps.check_params("cell_census", [{"prefix": 3}])
        self.assertIn("fewest_cells", scores.SCORES)
        self.assertIn("cell_budget", params.DECLARED["signoff"]["checks"].choices)  # signoff accepts it
        self.assertIn("cell_census", next(t for t in tools() if t["name"] == "run_step")["inputSchema"]
                      ["properties"]["step"]["enum"])
        self.assertIn("prefix", Server().call("describe_step", {"step": "cell_census"})["cell_census"]["properties"])
        self.assertEqual(registry.loaded()["plugins"], ["sipath.examples.plugin_example"])
        with self.assertRaises(registry.RegistryError):  # no silent override
            registry.register_step("place", lambda ctx: None)

    def test_design_file_loads_plugins_and_checks_extensions(self):
        from sipath import designfile, schema
        base = {"name": "t", "top": "t", "mode": "macro", "pdk": "sky130A", "netlist": "n.v", "sdc": "s.sdc",
                "floorplan_def": "f.def", "plugins": ["sipath.examples.plugin_example"]}
        d = designfile.design_from_dict(dict(base, extensions={"example": {"max_cells": 10}}), "/x")
        self.assertEqual(d.extensions["example"]["max_cells"], 10)
        with self.assertRaisesRegex(ValueError, "extensions.example.max_cells: expected int"):
            schema.validate(dict(base, extensions={"example": {"max_cells": "ten"}}))
        with self.assertRaisesRegex(ValueError, "extensions.nosuch: no plugin registered"):
            schema.validate(dict(base, extensions={"nosuch": {}}))
        with self.assertRaisesRegex(ValueError, "plugin 'no.such.module' could not be imported"):
            designfile.design_from_dict(dict(base, plugins=["no.such.module"]), "/x")

    def test_plugin_step_runs_with_variants_and_score(self):
        from sipath import steps
        with tempfile.TemporaryDirectory() as td:
            d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
            counts = iter(["120", "80"])

            def fac(w):  # each attempt's stand-in session answers the census with its own count
                return FakeSession(w, responses={"::flow_timing_prep": "placement", "set n 0": next(counts)})
            f = Flow(d, sky130hd("/p"), td, fac, validate_inputs=False)
            r = f.step("cell_census", steps.STEPS["cell_census"], variants=[{"prefix": ""}, {"prefix": "u_"}],
                       score="fewest_cells")
            self.assertEqual(sorted(a.metrics["census_cells"] for a in r.attempts), [80, 120])
            self.assertEqual(r.selected.metrics["census_cells"], 80)

    def test_signoff_check_contract(self):
        from sipath import registry
        fn = registry.CHECKS["cell_budget"]
        ctx = types.SimpleNamespace(design=types.SimpleNamespace(extensions={"example": {"max_cells": 100}}),
                                    session=FakeSession(".", responses={"llength": "150"}))
        res = fn(ctx, {})
        self.assertEqual((res["pass"], res["cells"]), (False, 150))
        ctx.design.extensions = {}
        self.assertTrue(fn(ctx, {})["pass"])


class Harnesses(unittest.TestCase):
    """Frames (sipath.harness) on the shipped OpenFrame template, without OpenROAD."""

    def setUp(self):
        from sipath import harness
        self.h = harness.get("openframe")
        self.tpl = self.h.path("template_def")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_template_pins(self):
        from sipath.harness import parse_def_pins
        die, dbu, pins = parse_def_pins(self.tpl)
        self.assertEqual((die, dbu), ((0, 0, 3166630, 4766630), 1000))
        self.assertEqual(len(pins), 917)
        self.assertEqual(len({p["name"] for p in pins}), 887)
        vccd = [p for p in self.h.power_pins() if p["net"] == "vccd1"]
        self.assertTrue(vccd and all(p["layer"] == "met3" for p in vccd))
        self.assertTrue(any(p["rect"][2] > die[2] for p in pins))  # pins overhang the boundary

    def test_rows_only_in_the_core_region(self):
        from sipath.harness import floorplan_def
        out = floorplan_def(self.tpl, os.path.join(self.tmp, "fp.def"), "unithd", 0.46, 2.72, 40,
                            region=(1400, 1950, 1800, 2350))
        with open(out) as f:
            rows = re.findall(r"ROW ROW_(\d+) unithd (\d+) (\d+) (N|FS) DO (\d+)", f.read())
        self.assertEqual(len(rows), int(400 / 2.72))
        self.assertEqual([r[3] for r in rows[:3]], ["N", "FS", "N"])
        self.assertTrue(all(int(r[1]) % 460 == 0 and int(r[1]) >= 1400000 for r in rows))
        with self.assertRaisesRegex(ValueError, "empty or outside"):
            floorplan_def(self.tpl, os.path.join(self.tmp, "x.def"), "unithd", 0.46, 2.72, 40,
                          region=(0, 0, 30, 30))

    def test_pin_channels(self):
        from sipath import harness
        pdk = types.SimpleNamespace(name="sky130A", site="unithd")
        h = harness.get("openframe", {"core_region": [2900.0, 300.0, 3120.0, 4650.0]})
        out = os.path.join(self.tmp, "fp.def")
        # gpio_in[0] (east edge) is next to the region; resetb_l (south edge, x 551 um) is 2.35 mm from it
        self.assertEqual(h.floorplan(pdk, out, ["gpio_in[0]"])[1], [])
        _, ch = h.floorplan(pdk, out, ["gpio_in[0]", "resetb_l"])
        # two rows on the region's row grid at the core's bottom, from the pin under the whole region
        self.assertEqual(ch, [(-95, 540960, 3119720), (-94, 540960, 3119720)])
        with open(out) as f:
            rows = re.findall(r"ROW (ROW|PINCH)_(\d+) unithd (\d+) (\d+) (N|FS) DO (\d+)", f.read())
        self.assertEqual(rows[0][2:5], ("2900300", "300000", "N"))
        self.assertEqual(rows[-2:], [("PINCH", str(len(rows) - 2), "540960", "41600", "FS", "5606"),
                                     ("PINCH", str(len(rows) - 1), "540960", "44320", "N", "5606")])
        self.assertEqual(harness.get("openframe", {"core_region": [2900.0, 300.0, 3120.0, 4650.0],
                                                   "pin_channels": False}).floorplan(pdk, out, ["resetb_l"])[1], [])
        # beside the region (west, at its heights): up to it, never over its rows, a strap pitch long
        die, dbu, _ = harness.parse_def_pins(self.tpl)
        ch = harness.pin_channels([(0, 1000000)], die, dbu, 0.46, 2.72, 40, (2900, 300, 3120, 4650), 300,
                                  min_len=213.1)
        self.assertEqual([k for k, _, _ in ch], [257, 258])
        self.assertTrue(all(a == 40020 and b <= 2895300 for _, a, b in ch))

    def test_pin_repeater_path(self):
        from sipath.steps.place import _along
        path = [(551, 0), (551, 44), (3000, 44), (3000, 2300), (3100, 2300)]  # pin, channel, region, loads
        self.assertEqual(_along(path, 0), (551, 0))
        self.assertEqual(_along(path, 44 + 1000), (1551, 44))
        self.assertEqual(_along(path, 44 + 2449 + 256), (3000, 300))
        self.assertEqual(_along(path, 10 ** 6), (3100, 2300))

    def test_drv_upsize_next_drive_strength(self):
        from sipath.eco import upsize
        cells = ["lib__clkbuf_1", "lib__clkbuf_2", "lib__clkbuf_4", "lib__clkbuf_16", "lib__clkinv_2", "lib__o211ai_4"]
        self.assertEqual(upsize("lib__clkbuf_1", cells), "lib__clkbuf_2")
        self.assertEqual(upsize("lib__clkbuf_2", cells, lambda c: c.endswith("_4")), "lib__clkbuf_16")
        self.assertIsNone(upsize("lib__clkbuf_16", cells))
        self.assertIsNone(upsize("lib__o211ai_2", cells, lambda c: c == "lib__o211ai_4"))  # excluded: none

    def test_apply_makes_the_design_the_wrapper(self):
        pdk = types.SimpleNamespace(name="sky130A", site="unithd")
        d = Design("t", "openframe_project_wrapper", None, "s.sdc", "macro")
        info = self.h.apply(d, pdk, self.tmp)
        self.assertTrue(os.path.exists(d.floorplan_def) and d.die is None and d.is_core)
        self.assertEqual((d.max_route_layer, d.net("VDD"), d.net("VSS"), d.net("clk")),
                         ("met4", "vccd1", "vssd1", "clk"))
        self.assertEqual(d.pdn.ring_layers, ("met4", "met5"))
        self.assertEqual(info["supplies"], {"VDD": "vccd1", "VSS": "vssd1"})
        from sipath import steps
        self.assertEqual(steps.grid_nets(d), {"vccd1", "vssd1"})
        with self.assertRaisesRegex(ValueError, "top must be openframe_project_wrapper"):
            self.h.apply(Design("t", "other", None, "s.sdc", "macro"), pdk, self.tmp)

    def test_conformance(self):
        with open(self.tpl) as f:
            text = f.read()
        same = os.path.join(self.tmp, "same.def")
        with open(same, "w") as f:
            f.write(text)
        self.assertTrue(self.h.conformance(same)["pass"])
        moved = os.path.join(self.tmp, "moved.def")  # gpio_in[15] shifted by 1 nm
        with open(moved, "w") as f:
            f.write(re.sub(r"(- gpio_in\[15\] .*?PLACED \( )(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1),
                           text, count=1, flags=re.S))
        r = self.h.conformance(moved)
        self.assertFalse(r["pass"])
        self.assertEqual((r["missing_count"], r["extra_count"]), (1, 1))
        self.assertIn("gpio_in[15]", r["missing"][0])

    def test_pin_clip_and_restore_scripts(self):
        tcl_restore = self.h.restore_pins_tcl()
        from sipath.harness import parse_def_pins
        p = next(p for p in parse_def_pins(self.tpl)[2] if p["name"] == "gpio_in[15]")
        self.assertIn("findBTerm {gpio_in[15]}", tcl_restore)
        self.assertIn(f"findLayer {p['layer']}] {' '.join(map(str, p['rect']))}", tcl_restore)
        self.assertNotIn("findBTerm {vccd1}", tcl_restore)  # supply pins keep their own shapes
        self.assertIn("isSpecial", self.h.clip_pins_tcl())

    def test_every_frame_supply_is_one_pin_with_all_its_shapes(self):
        tcl_pins = self.h.create_power_pins_tcl()
        self.assertEqual(tcl_pins.count("odb::dbBTerm_create"), 14)  # vccd ... vssio, used or not
        self.assertEqual(tcl_pins.count("odb::dbBPin_create"), len(self.h.power_pins()))
        self.assertNotIn("vccd1.1", tcl_pins)
        self.assertIn("setSigType GROUND", tcl_pins.split("findNet vssio]")[1].split("findNet")[0])

    def test_zero_extension_corners_get_squared(self):
        from sipath.signoff import fix_zero_ext_corners
        path = os.path.join(self.tmp, "w.def")
        nets = ("NETS 3 ;\n"
                "    - a ( PIN a ) ( u1 X ) + USE SIGNAL\n"
                "      + ROUTED li1 ( 100 200 ) L1M1_PR ( 300 * ) M1M2_PR ( * 900 0 ) ( 500 * ) ( * 1200 )\n"
                "      NEW met2 ( 700 100 0 ) ( * 400 ) ( 900 * 0 ) ;\n"  # path ends: kept
                "    - b ( u1 A ) ( u2 Y ) + USE SIGNAL\n"
                "      + ROUTED met2 ( 10 10 ) ( * 50 0 ) ( * 90 ) ( 20 * 0 ) M2M3_PR ( * 120 ) ;\n"  # straight / via: kept
                "    - c ( u3 A ) ( u4 Y ) + USE SIGNAL\n"  # the router's zero-length start on the new layer: kept
                "      + ROUTED met2 ( 40 10 ) ( * 30 ) M1M2_PR ( * * 0 ) ( 60 * ) ;\n"
                "END NETS\n")
        with open(path, "w") as f:
            f.write("VERSION 5.8 ;\n" + nets + "END DESIGN\n")
        self.assertEqual(fix_zero_ext_corners(path), [{"net": "a", "at": (300, 900)}])
        with open(path) as f:
            text = f.read()
        self.assertIn("M1M2_PR ( * 900 ) ( 500 * )", text)
        self.assertIn("( 700 100 0 ) ( * 400 ) ( 900 * 0 )", text)
        self.assertIn("( * 50 0 ) ( * 90 ) ( 20 * 0 ) M2M3_PR", text)
        self.assertIn("M1M2_PR ( * * 0 ) ( 60 * )", text)  # squaring it would draw a zero-length path
        self.assertEqual(fix_zero_ext_corners(path), [])  # idempotent

    def test_frame_integration_inputs(self):
        copied = self.h.prepare_frame(self.tmp)
        self.assertEqual(copied, ["caravel_openframe.mag", "chip_io_openframe.mag", "frame/"])
        # the views name their GDS uncompressed (simple_por: ../gds/simple_por.gds)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "frame", "gds", "simple_por.gds")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "frame", "mag", "primitives",
                                                    "sky130_fd_pr__res_xhigh_po_0p69_S5N9F3.mag")))
        script = self.h.integrate_script(self.tmp, "/w/wrapper.gds", "/o/chip.gds")
        self.assertLess(script.index("gds read {/w/wrapper.gds}"), script.index("load caravel_openframe -dereference"))
        self.assertIn("cif *hier write disable", script)
        self.assertIn("gds write {/o/chip.gds}", script)
        seal = self.h.seal_ring(self.tmp)  # decompressed next to the frame views
        self.assertEqual(os.path.basename(seal), "advSeal_6um_gen.gds")
        self.assertTrue(os.path.getsize(seal) > 0)
        self.assertEqual(self.h.seal_cell(seal), "advSeal_6um_gen")
        kdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "klayout")
        self.assertTrue(os.path.isfile(os.path.join(kdir, "sealring.py")))  # placed by KLayout after Magic
        from sipath import steps
        self.assertTrue(steps.STEPS["integrate"].tool)

    def test_harness_excludes_a_floorplan(self):
        from sipath import schema
        with self.assertRaisesRegex(ValueError, "die: not allowed with harness"):
            schema.validate({"name": "t", "top": "openframe_project_wrapper", "mode": "macro", "pdk": "sky130A",
                             "rtl": ["a.v"], "sdc": "s.sdc", "harness": "openframe", "die": [0, 0, 1, 1]})

    def test_bus_assigns_with_slices_and_concatenations(self):
        m = parse_verilog("""module t (a, b, y);
  input [3:0] a; input b; output [5:0] y;
  wire [1:0] w;
  assign w = a[3:2];
  assign y = {w, b, a[1:0], b};
endmodule""")
        self.assertEqual(m.assigns, [("w[1]", "a[3]"), ("w[0]", "a[2]"), ("y[5]", "w[1]"), ("y[4]", "w[0]"),
                                     ("y[3]", "b"), ("y[2]", "a[1]"), ("y[1]", "a[0]"), ("y[0]", "b")])
        with self.assertRaisesRegex(ValueError, "constant"):
            parse_verilog("module t (y); output y; assign y = 1'b0; endmodule")


class HardenedViewsAndFill(unittest.TestCase):
    """P2.1 views directories, P0.2 fill/density and P1.5 opt-in checks, without OpenROAD."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_macro_from_a_views_directory(self):
        from sipath import designfile
        v = os.path.join(self.tmp, "views", "blk")
        os.makedirs(v)
        with open(os.path.join(v, "macro.yaml"), "w") as f:
            f.write("# hardened\n" + json.dumps({"macros": [{"inst": "<instance name>", "lef": "blk.lef",
                    "gds": "blk.gds", "lib": {"tt_nom": "blk__tt_nom.lib"}, "location": None,
                    "supplies": {"^VDD$": "VDD", "^VSS$": "VSS"}, "cdl": "blk.cdl"}]}))
        base = {"name": "t", "top": "t", "mode": "macro", "pdk": "sky130A", "netlist": "n.v", "sdc": "s.sdc",
                "floorplan_def": "f.def"}
        d = designfile.design_from_dict(dict(base, macros=[{"inst": "u0", "views": "views/blk", "location": [1, 2]},
                                                          {"inst": "u1", "views": "views/blk", "gds": "mine.gds"}]),
                                        self.tmp)
        m0, m1 = d.macros
        self.assertEqual((m0.lef, m0.cdl, m0.lib), (os.path.join(v, "blk.lef"), os.path.join(v, "blk.cdl"),
                                                     {"tt_nom": os.path.join(v, "blk__tt_nom.lib")}))
        self.assertEqual((m0.location, m0.supplies), ((1, 2), {"^VDD$": "VDD", "^VSS$": "VSS"}))
        self.assertEqual(m1.gds, os.path.join(self.tmp, "mine.gds"))  # given in the entry: relative to the design file
        with self.assertRaisesRegex(ValueError, r"macros\[0\]: lef, views or sram is required"):
            designfile.design_from_dict(dict(base, macros=[{"inst": "u0"}]), self.tmp)
        with self.assertRaisesRegex(FileNotFoundError, "harden the macro first"):
            designfile.design_from_dict(dict(base, macros=[{"inst": "u0", "views": "nowhere"}]), self.tmp)

    def test_signoff_checks_magic_ones_opt_in(self):
        from sipath import params
        r = params.resolve("signoff", {})
        self.assertIn("density", r["checks"])
        self.assertNotIn("magic_drc", r["checks"])  # needs the Magic image: asked for, not default
        params.check("signoff", {"checks": ["drc", "magic_drc", "xor"]})
        self.assertIn("xor", r["require"])  # when run, they gate signoff

    @unittest.skipUnless(SKY130A, "no open_pdks sky130A tree")
    def test_pdk_fill_rules_and_signoff_keys(self):
        from sipath.pdk import load
        p = load("sky130A")
        with open(p.signoff["fill_rules"]) as f:
            rules = json.load(f)
        self.assertEqual(set(rules["layers"]), set(p.signoff["fill_gds"]))
        for name, layer in rules["layers"].items():  # fill on the PDK's fill datatype
            self.assertEqual([layer["layer"], layer["datatype"]], p.signoff["fill_gds"][name])
        for k in ("density_deck", "fill_rules", "magic_rc"):
            self.assertIn(p.signoff[k], p.files())  # mounted for the tools


class ExploreMode(unittest.TestCase):
    def test_explore_flow_is_a_fast_first_cut(self):
        from sipath import designfile
        spec = [{"step": "synth", "variants": [{"abc": "speed"}, {"abc": "area"}]}, {"step": "floorplan"},
                {"step": "place"}, {"step": "cts"}, {"step": "route_digital"}, {"step": "route_repair"},
                {"step": "fill"}, {"step": "extract"}, {"step": "signoff"}, {"step": "views"},
                {"step": "chip", "fn": "integrate"}]
        out = designfile.explore_flow(spec)
        self.assertEqual([s["step"] for s in out], ["synth", "floorplan", "place", "cts", "route_digital"])
        self.assertEqual(out[0]["variants"], [{"abc": "speed", "equiv": "off"}, {"abc": "area", "equiv": "off"}])
        self.assertEqual(out[-1]["variants"], [{"detailed": False}])
        self.assertEqual(spec[0]["variants"], [{"abc": "speed"}, {"abc": "area"}])  # the file's flow is untouched
        # a flow that stops before routing closes with a ppa step
        self.assertEqual([s["step"] for s in designfile.explore_flow([{"step": "synth"}, {"step": "place"}])],
                         ["synth", "place", "ppa"])

    def test_explore_params_are_declared(self):
        from sipath.params import check
        check("route_digital", {"detailed": False})
        check("synth", {"equiv": "off"})
        check("ppa", {"margin": 0.1})

    @unittest.skipUnless(SKY130A, "no open_pdks sky130A tree")
    def test_explore_build_optimizes_on_the_typical_corner(self):
        from sipath import designfile
        d, _ = designfile.load(os.path.join(os.path.dirname(designs.__file__), "gcd8.yaml"))
        with tempfile.TemporaryDirectory() as td:
            runner = NativeRunner("openroad")
            runner.platform_root = lambda: "/p"
            full = designfile.build(d, os.path.join(td, "a"), runner)
            fast = designfile.build(d, os.path.join(td, "b"), runner, explore=True)
            self.assertEqual([c.name for c in full.pdk.analysis_corners()], ["tt_nom", "ss_max", "ff_min"])
            self.assertEqual([c.name for c in fast.pdk.analysis_corners()], ["tt_nom"])
            self.assertEqual(len(fast.pdk.signoff_corners()), 9)


class Templates(unittest.TestCase):
    """Design templates (sipath.templates), rendered without running a flow."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_openframe_template_renders_a_loadable_project(self):
        from sipath import designfile, templates
        self.assertIn("openframe", [x["name"] for x in templates.listing()])
        files = templates.get("openframe").render(self.tmp, {"name": "demo", "inputs": "6", "outputs": "4"})
        self.assertEqual([os.path.relpath(f, self.tmp) for f in files],
                         ["design.yaml", "rtl/demo.sdc", "rtl/demo_wrapper.v", "rtl/user_core.v"])
        d, flow = designfile.load(os.path.join(self.tmp, "design.yaml"))
        self.assertEqual((d.name, d.top, d.harness), ("demo", "openframe_project_wrapper", "openframe"))
        self.assertEqual(flow[-1]["step"], "integrate")
        with open(os.path.join(self.tmp, "rtl", "demo_wrapper.v")) as f:
            v = f.read()
        self.assertIn("localparam IN_LO = 0, IN_HI = 6, OUT_LO = 7, OUT_HI = 10;", v)
        self.assertIn(".in(gpio_in[6:1])", v)
        self.assertNotIn("{{", v)
        with self.assertRaises(FileExistsError):
            templates.get("openframe").render(self.tmp, {"name": "demo"})

    def test_example_and_template_flows_use_declared_parameters(self):
        import glob
        from sipath import designfile, steps, templates
        templates.get("openframe").render(self.tmp, {})
        files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "examples", "*.yaml")))
        for path in files + [os.path.join(self.tmp, "design.yaml")]:
            try:
                _, flow = designfile.load(path, generate=False)
            except FileNotFoundError:
                continue  # a parent of a macro not hardened here
            for item in flow:
                steps.check_params(item.get("fn", item["step"]), item.get("variants"))

    def test_template_parameters_are_checked(self):
        from sipath import templates
        t = templates.get("openframe")
        with self.assertRaisesRegex(ValueError, "17 pads: the east edge has 15"):
            t.values({"inputs": 10, "outputs": 6})
        with self.assertRaisesRegex(ValueError, "unknown parameter"):
            t.values({"pads": 3})
        with self.assertRaisesRegex(ValueError, "is not a int"):
            t.values({"inputs": "many"})
        with self.assertRaisesRegex(ValueError, "below 1"):
            t.values({"outputs": 0})


class UndefinedNames(unittest.TestCase):
    """Every global name a function of the package uses exists (a deleted or
    renamed helper is found here, not in a two-hour live run)."""

    def test_globals_referenced_by_functions_exist(self):
        import builtins
        import importlib
        import symtable
        here = os.path.join(os.path.dirname(__file__), "..")
        bad = []
        for f in sorted(os.listdir(here)):
            if not f.endswith(".py") or f == "__main__.py":
                continue
            mod = importlib.import_module(f"sipath.{f[:-3]}")
            with open(os.path.join(here, f)) as fh:
                table = symtable.symtable(fh.read(), f, "exec")
            stack = list(table.get_children())
            while stack:
                scope = stack.pop()
                stack.extend(scope.get_children())
                for s in scope.get_symbols():
                    if s.is_global() and s.is_referenced() and not hasattr(mod, s.get_name()) \
                            and not hasattr(builtins, s.get_name()):
                        bad.append(f"{f}: {scope.get_name()} uses {s.get_name()}")
        self.assertEqual(bad, [])


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


class UartExamples(unittest.TestCase):
    def test_uart_flows_plan_without_tools(self):
        from pathlib import Path
        from sipath import designfile
        root = Path(__file__).resolve().parents[1] / "examples"
        for name in ("uart_fifo", "openframe_uart"):
            with self.subTest(name=name):
                self.assertEqual(designfile.plan(str(root / (name + ".yaml")))["problems"], [])

    def test_clock_clustering_options_are_opt_in_and_validated(self):
        from sipath import params
        from sipath.tools.openroad import tcl
        pdk = sky130hd("/p")
        self.assertNotIn("-distance_between_buffers", tcl.cts(pdk, ["clk"]))
        cmd = tcl.cts(pdk, ["clk"], 8, 2, 20.0)
        for flag in ("-sink_clustering_size 8", "-sink_clustering_levels 2", "-distance_between_buffers 20.0"):
            self.assertIn(flag, cmd)
        with self.assertRaises(ValueError):
            params.check("cts", {"sink_clustering_size": 0})


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
    NASTY = TCL_NASTY

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


class CdlnsWorkClock(unittest.TestCase):
    """The cdlns placer's work clock (third_party/work_clock.py): its schedule follows work, not the machine."""

    def load(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "third_party", "work_clock.py")
        spec = importlib.util.spec_from_file_location("work_clock", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_reads_are_charged_by_line_and_only_for_the_pinned_file(self):
        import hashlib
        wc = self.load()
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "placer.py")
            with open(src, "w") as f:
                f.write("def run(clock):\n    a = clock.time()\n    b = clock.time()\n    return a, b\n")
            sha = hashlib.sha256(_read_bytes(src)).hexdigest()
            ns = {}
            exec(compile(_read_text(src), src, "exec"), ns)
            clock = wc.WorkClock(src, {3: 0.5}, tick=0.01)
            self.assertEqual(ns["run"](clock), (0.01, 0.51))  # line 2: the tick; line 3: its charge
            self.assertEqual(ns["run"](wc.WorkClock(src, {3: 0.5}, tick=0.01)), (0.01, 0.51))  # same work, same times
            self.assertEqual(wc.work_clock(src, sha).charges, wc.CHARGES)
            self.assertEqual(wc.work_clock(src, "0" * 64).charges, {})  # another version: the uniform tick
        self.assertAlmostEqual(wc.TICK * 2 * 282, 1.0)  # two reads per annealing iteration, 282 iterations/s


class Snapshots(unittest.TestCase):
    """Layout pictures after every step (config snapshots): which attempts, never fatal."""

    def test_selected_all_and_failures(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            def att(i, status, ck):
                a = Attempt(f"place.{i}", "place", i, {}, None, td, status=status)
                a.checkpoint = os.path.join(td, ck)
                open(a.checkpoint, "w").close()
                return a
            atts = [att(0, "selected", "p0.odb"), att(1, "rejected", "p1.odb"), att(2, "failed", "p2.odb")]
            run = types.SimpleNamespace(selected=atts[0], attempts=atts)
            flow = types.SimpleNamespace(workdir=td, runner=None, save_graph=lambda: None)
            drawn = []

            def fake_render(odb, png, runner=None):
                if odb.endswith("p1.odb"):
                    raise RuntimeError("no display")
                drawn.append(os.path.basename(png))
                open(png, "w").close()
            with unittest.mock.patch("sipath.viz.render", fake_render):
                self.assertEqual([os.path.basename(x) for x in designfile.snapshot(flow, run)], ["place.0.png"])
                with self.assertLogs("sipath", "WARNING"):
                    designfile.snapshot(flow, run, "all")  # place.1 fails: logged; place.2 failed: not drawn
            self.assertEqual(drawn, ["place.0.png"])  # drawn once: the second call found it up to date
            tool = types.SimpleNamespace(selected=att(3, "selected", "x.synth.v"), attempts=[])
            self.assertEqual(designfile.snapshot(flow, tool), [])  # not a layout


class Metrics2(unittest.TestCase):
    """Power per stage, peak memory, and metrics.json in METRICS2.1 names."""

    def test_power_report_parsing(self):
        from sipath.steps.common import power_metrics
        rpt = ("== corner tt_nom\nGroup Internal Switching Leakage Total\n"
               "Total                  4.04e-04   3.48e-04   1.44e-09   7.52e-04 100.0%\n"
               "== corner ss_max\nTotal                  3.23e-04   2.79e-04   1.02e-06   6.03e-04 100.0%\n")
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "p.rpt")
            _write_text(f, rpt)
            m = power_metrics(f)
            self.assertEqual((m["power_total_w"], m["power_leakage_w"], m["power_total_w_ss_max"]),
                             (7.52e-04, 1.44e-09, 6.03e-04))  # the typical corner first
            _write_text(f, "report_power failed: no libraries\n")
            self.assertEqual(power_metrics(f), {})
        self.assertEqual(power_metrics("/nonexistent.rpt"), {})

    def test_peak_memory_through_the_checkpoint_store(self):
        from sipath.tools.openroad.checkpoint import PEAK_MEMORY_TCL, OpenRoadCheckpoints
        s = FakeSession(".", responses={"::flow_metrics": "wns 1.0", "::flow_timing_prep": "placement",
                                        PEAK_MEMORY_TCL: "204800"})
        m = OpenRoadCheckpoints().metrics(s)
        self.assertEqual((m["wns"], m["mem_peak_mb"]), (1.0, 200.0))
        s2 = FakeSession(".", responses={"::flow_metrics": "wns 1.0"})
        self.assertNotIn("mem_peak_mb", OpenRoadCheckpoints().metrics(s2))  # no /proc: no number, not a guess

    def test_metrics2_names_stages_and_finals(self):
        from sipath import report
        g = {"design": "d", "steps": {"synth": {"selected": "synth.0"}, "cts": {"selected": "cts.0"},
                                      "signoff": {"selected": "signoff.0"}},
             "attempts": [{"id": "synth.0", "step": "synth", "parent": None, "seconds": 3.0,
                           "metrics": {"synth_area_um2": 100.0, "synth_wns_ns": -1.0}},
                          {"id": "cts.0", "step": "cts", "parent": "synth.0", "seconds": 5.0,
                           "metrics": {"wns": 0.5, "wns_ss_max": 0.2, "mem_peak_mb": 250.0,
                                       "heatmap_rudy_hot_share": 0.1, "parasitics": "placement"}},
                          {"id": "signoff.0", "step": "signoff", "parent": "cts.0", "seconds": 9.0,
                           "metrics": {"wns": 0.4, "lvs_match": True, "power_total_w": 1e-3, "drc_signoff": 0}}]}
        m = report.metrics2(g)
        self.assertEqual((m["synth__design__instance__area"], m["cts__timing__setup__ws"],
                          m["cts__timing__setup__ws__corner:ss_max"], m["cts__mem__peak"]), (100.0, 0.5, 0.2, 250.0))
        self.assertEqual((m["timing__setup__ws"], m["design__lvs_error__count"], m["power__total"],
                          m["klayout__drc_error__count"], m["heatmap__rudy__hot_share"]), (0.4, 0, 1e-3, 0, 0.1))
        self.assertNotIn("design__instance__area", m)  # synthesis's estimate keeps its prefix
        self.assertEqual(m["finish__runtime__total"], 9.0)


class SdcClocks(unittest.TestCase):
    """Clock ports from the SDC when a design file does not name them (sipath.sdc)."""

    def test_forms_variables_and_virtual_clocks(self):
        from sipath import sdc
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.sdc")
            with open(p, "w") as f:
                f.write("set clk_port_name PCLK\nset clk_port [get_ports $clk_port_name]\n"
                        "create_clock -name core -period 10 $clk_port\n"
                        "create_clock -name io -period 5\n"  # virtual: no port
                        "create_clock -name h -period 11 [get_ports {HCLK}]\n"
                        "# create_clock -name x -period 1 [get_ports nope]\n"
                        "create_clock -name g -period 20 [get_ports {gpio_in[0]}]\n")
            self.assertEqual(sdc.clock_ports(p), ["PCLK", "HCLK", "gpio_in[0]"])

    def test_design_files_take_the_sdc_ports_unless_given(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "t.sdc"), "w") as f:
                f.write("create_clock -name pclk -period 10 [get_ports PCLK]\n")
            base = {"name": "t", "top": "t", "mode": "macro", "netlist": "t.v", "sdc": "t.sdc", "pdk": "sky130A"}
            self.assertEqual(designfile.design_from_dict(base, td).clock_ports, ["PCLK"])
            self.assertEqual(designfile.design_from_dict({**base, "clock_ports": ["clk"]}, td).clock_ports, ["clk"])
            with open(os.path.join(td, "t.sdc"), "w") as f:
                f.write("create_clock -name v -period 10\n")  # nothing to take: the default stays
            self.assertEqual(designfile.design_from_dict(base, td).clock_ports, ["clk"])


class Heatmaps(unittest.TestCase):
    """Heatmaps (sipath.heatmap; collected after the checkpoint is saved, sipath.tools.openroad.heatmap)."""
    CSV = "x0,y0,x1,y1,value (%)\n" + "".join(
        f"{x},{y},{x + 10},{y + 10},{v}\n" for (x, y), v in {(0, 0): 20, (10, 0): 95, (20, 0): 92, (0, 10): 30,
                                                            (10, 10): 40, (20, 10): 99, (0, 20): 91, (10, 20): 10,
                                                            (20, 20): 15}.items())

    def write(self, td, text=None):
        path = os.path.join(td, "placement.csv")
        with open(path, "w") as f:
            f.write(self.CSV if text is None else text)
        return path

    def test_read_summary_regions_query_svg(self):
        from sipath import heatmap
        with tempfile.TemporaryDirectory() as td:
            unit, bins = heatmap.read(self.write(td))
        self.assertEqual((unit, len(bins)), ("%", 9))
        s = heatmap.summary("placement", unit, bins)
        self.assertEqual((s["peak"], s["hot_threshold"], s["hot_share"]), (99.0, 90.0, 4 / 9))
        # (10,0) (20,0) (20,10) touch: one region; (0,20) is apart
        self.assertEqual([(h["box"], h["peak"], h["bins"]) for h in s["hotspots"]],
                         [([10.0, 0.0, 30.0, 20.0], 99.0, 3), ([0.0, 20.0, 10.0, 30.0], 91.0, 1)])
        self.assertAlmostEqual(s["p95"], 97.4)
        self.assertEqual(len(heatmap.query(bins, [12, 12, 18, 18])), 1)  # one bin overlaps; touching ones do not
        self.assertEqual(len(heatmap.query(bins)), 9)
        self.assertEqual(heatmap.summary("power", "W", bins)["hot_threshold"], 0.8 * 99)  # relative elsewhere
        objs = {"big": (0, 0, 30, 30), "in": (11, 1, 12, 2), "out": (40, 40, 41, 41)}
        self.assertEqual(heatmap.inside([10, 0, 30, 20], objs), {"count": 2, "top": ["big", "in"]})
        self.assertIn("<svg", heatmap.svg("placement", unit, bins))
        self.assertEqual(heatmap.coarsen(bins, 3), bins)  # fits: unchanged
        coarse = heatmap.coarsen(bins, 2)  # 3 x 3 -> 2 x 2, each the largest it covers
        self.assertEqual(len(coarse), 4)
        self.assertEqual(max(b[4] for b in coarse), 99.0)
        self.assertEqual(coarse[0], (0.0, 0.0, 20.0, 20.0, 95.0))
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "c.csv")
            heatmap.write(out, unit, coarse)
            self.assertEqual(heatmap.read(out), (unit, coarse))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                heatmap.read(self.write(td, "not,a,map\n"))

    def test_collected_after_the_checkpoint_never_failing_it(self):
        seen = []

        def step(ctx):
            ctx.metrics["wns"] = 1.0
            ctx.after_save.append(lambda c: seen.append(os.path.isfile(os.path.join(c.dir, "s.0.odb"))))
            ctx.after_save.append(lambda c: c.metrics.update({"heatmap_placement_peak": 7.0, "wns": -5.0}))
            ctx.after_save.append(lambda c: 1 / 0)
        with tempfile.TemporaryDirectory() as td:
            f = Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                     demo.fake_factory(), validate_inputs=False)
            a = f.step("s", step).selected
        self.assertEqual(seen, [True])  # the checkpoint existed before any observation ran
        self.assertEqual(a.status, "selected")
        self.assertEqual((a.metrics["heatmap_placement_peak"], a.metrics["wns"]), (7.0, 1.0))  # added, not changed
        self.assertIn("ZeroDivisionError", a.outputs["after_save_errors"][0])
        self.assertIn("after_save", a.timing["phases"])

    def test_steps_schedule_only_known_maps(self):
        from sipath import params
        from sipath.tools.openroad import heatmap as collector
        ctx = types.SimpleNamespace(after_save=[])
        collector.schedule(ctx, [])
        self.assertEqual(ctx.after_save, [])
        collector.schedule(ctx, ["placement", "irdrop"])
        self.assertEqual(len(ctx.after_save), 1)
        with self.assertRaisesRegex(ValueError, "unknown map"):
            collector.schedule(ctx, ["heat"])
        with self.assertRaisesRegex(ValueError, "not one of"):
            params.check("place", {"heatmaps": ["heat"]})
        self.assertEqual(params.DECLARED["extract"]["heatmaps"].default[-1], "irdrop")

    def test_query_heatmap_over_mcp(self):
        from sipath import mcp_server
        from sipath.mcp_server import ToolError
        with tempfile.TemporaryDirectory() as td:
            d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
            f = Flow(d, sky130hd("/p"), td, demo.fake_factory(), validate_inputs=False)
            csv = self.write(td)
            a = Attempt("place.0", "place", 0, {}, None, td, status="selected")
            a.outputs = {"heatmaps": {"placement": {"csv": csv, "unit": "%", "peak": 99.0, "hotspots": []},
                                      "irdrop": {"unavailable": "GUI-0072"}}}
            f.attempts.append(a)
            s = mcp_server.Server(emit=lambda m: None)
            s.runs["r1"] = {"flow": f, "design": "fake", "job": None}
            out = s.query_heatmap("r1", "place.0", "placement", [12, 12, 18, 18])
            self.assertEqual((out["peak"], out["bins_in_box"], out["bin_values"][0]["value"]), (99.0, 1, 40.0))
            summary = s.query_heatmap("r1", "place.0", "placement")
            self.assertEqual((summary["bins_in_box"], summary["peak"]), (9, 99.0))
            self.assertNotIn("bin_values", summary)  # the summary alone unless the bins are asked for
            self.assertEqual(len(s.query_heatmap("r1", "place.0", "placement", bins=True)["bin_values"]), 9)
            for args, msg in (((["r1", "place.0", "rudy"]), "has no rudy"), ((["r1", "place.0", "irdrop"]), "GUI-0072"),
                              ((["r1", "place.0", "placement", [5, 5, 1, 1]]), "x0 < x1"),
                              ((["r1", "place.0", "nope"]), "unknown map")):
                with self.assertRaisesRegex(ToolError, msg):
                    s.query_heatmap(*args)


class Prerequisites(unittest.TestCase):
    """B1.2: steps declare what they need and give; a step whose parent chain lacks something is refused
    before any tool starts, naming what is missing and the step that gives it."""

    def flow(self, td, rtl=False):
        d = Design("fake", "fake", None if rtl else "x.v", "x.sdc", "macro", floorplan_def="x.def",
                   rtl=["x.v"] if rtl else [])
        return Flow(d, sky130hd("/p"), td, demo.fake_factory(), validate_inputs=False)

    def parent(self, f, caps, step="physical"):
        a = Attempt(f"{step}.0", step, 0, {}, None, f.workdir, status="selected", capabilities=sorted(caps))
        f.attempts.append(a)
        return a

    def test_refusals_name_what_is_missing(self):
        from sipath import steps
        from sipath.scheduler import MissingPrerequisite
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            fp = self.parent(f, {"netlist", "floorplan"}, "floorplan")
            with self.assertRaisesRegex(MissingPrerequisite, r"place needs power_grid .*given by physical"):
                f.step("place", steps.STEPS["place"], parent=fp)
            routed = self.parent(f, {"netlist", "floorplan", "power_grid", "placed", "routed"}, "route_repair")
            with self.assertRaisesRegex(MissingPrerequisite, r"precheck needs gds .*powered_netlist"):
                f.step("precheck", steps.STEPS["precheck"], parent=routed)
            with self.assertRaisesRegex(MissingPrerequisite, r"signoff needs parasitics"):  # timing reads SPEFs
                f.step("signoff", steps.STEPS["signoff"], parent=routed)
            f.check_prerequisites("signoff", steps.STEPS["signoff"], routed, [{"checks": ["drc", "lvs"]}])  # none
            with self.assertRaisesRegex(MissingPrerequisite, "timing needs parasitics"):
                f.check_prerequisites("timing", steps.STEPS["timing"], routed)
            self.assertEqual(f.attempts, [fp, routed])  # nothing started
            with self.assertRaisesRegex(MissingPrerequisite, "ECO e: needs a routed parent"):
                f.eco("e", fp, [{"op": "resize", "inst": "u", "master": "m"}])
        with tempfile.TemporaryDirectory() as td:  # an RTL design: the first step must make the netlist
            f = self.flow(td, rtl=True)
            with self.assertRaisesRegex(MissingPrerequisite, r"floorplan needs netlist .*given by synth"):
                f.step("floorplan", steps.STEPS["floorplan"])

    def test_capabilities_follow_the_chain_and_are_recorded(self):
        from sipath import registry
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)

            def mk(ctx):
                ctx.metrics["wns"] = 1.0
            mk.requires, mk.provides = frozenset({"netlist"}), frozenset({"floorplan"})
            a = f.step("fp", mk, score="setup_wns").selected
            self.assertEqual(a.capabilities, ["floorplan", "netlist"])
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual(g["attempts"][0]["capabilities"], ["floorplan", "netlist"])
            with self.assertLogs("sipath", "WARNING") as logs:  # undeclared: runs, unchecked, warned once
                b = f.step("custom", demo.fake_step(), parent=a, score="setup_wns").selected
            self.assertIn("declares no prerequisites", " ".join(logs.output))
            self.assertIsNone(b.capabilities)  # unknown after it: later steps are not checked
            f.check_prerequisites("place", registry.builtin_steps()["place"], b)

    def test_whole_flows_are_checked_before_they_run(self):
        import glob
        from sipath import designfile
        for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "examples", "*.yaml"))):
            try:
                d, spec = designfile.load(path)
            except FileNotFoundError:  # a parent of a macro not hardened here
                continue
            self.assertEqual(designfile.flow_problems(d, spec), [], os.path.basename(path))
            self.assertEqual(designfile.flow_problems(d, designfile.default_flow(d)), [], os.path.basename(path))
        d, _ = designfile.load(os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8.yaml"))
        problems = designfile.flow_problems(d, [{"step": "synth"}, {"step": "place"}])  # the old broken doc example
        self.assertEqual(len(problems), 1)
        self.assertIn("place needs floorplan", problems[0])
        self.assertIn("power_grid", problems[0])

    def test_mcp_refuses_before_the_job_starts(self):
        from sipath import mcp_server
        from sipath.mcp_server import ToolError
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            self.parent(f, {"netlist", "floorplan"}, "floorplan")
            s = mcp_server.Server(emit=lambda m: None)
            s.runs["r1"] = {"flow": f, "design": "fake", "job": None}
            with self.assertRaisesRegex(ToolError, "place needs power_grid"):
                s.run_step("r1", "place", parent="floorplan.0")
            self.assertEqual(len(f.attempts), 1)


class Verdict(unittest.TestCase):
    """B1.3: what a run established, apart from whether it finished."""

    def graph(self, td, attempts, steps):
        return {"design": "d", "attempts": attempts, "steps": steps}

    def signoff(self, td, name, failures=(), checks=None, required=None, **extra):
        checks = list(checks or ["drc", "density", "lvs", "antenna", "setup", "hold", "drv", "annotation",
                                 "unconstrained", "harness"])
        s = {"checks": checks, "required": list(required or [c for c in checks if c != "unconstrained"]),
             "failures": list(failures), "pass": not failures, "timing": {"unconstrained": []}, **extra}
        p = os.path.join(td, f"{name}.json")
        _write_json(p, s)
        return p

    def chain(self, td, synth_eq=None, so=None, so_params=None, last_selected=True):
        a = [{"id": "synth.0", "step": "synth", "kind": "tool", "parent": None, "params": {},
              "outputs": {"equiv": synth_eq} if synth_eq else {}},
             {"id": "floorplan.0", "step": "floorplan", "kind": "step", "parent": "synth.0", "params": {}, "outputs": {}},
             {"id": "signoff.0", "step": "signoff", "kind": "step", "parent": "floorplan.0",
              "params": so_params or {}, "outputs": {"signoff": so} if so else {}}]
        steps = {"synth": {"selected": "synth.0"}, "floorplan": {"selected": "floorplan.0", "parent": "synth.0"},
                 "signoff": {"selected": "signoff.0" if last_selected else None, "parent": "floorplan.0"}}
        return self.graph(td, a, steps)

    def test_statuses(self):
        from sipath import verdict
        eq_ok = {"result": "equivalent for 10 cycles from reset", "depth": 10}
        with tempfile.TemporaryDirectory() as td:
            v = verdict.verify(self.chain(td, eq_ok, self.signoff(td, "ok")))
            self.assertEqual((v["execution"], v["verification"], v["omitted"]), ("completed", "passed", []))
            self.assertIn("bounded", v["checks"]["equivalence"]["detail"])
            bad = self.signoff(td, "bad", failures=["setup: worst -2.383 ns", "drv: slew 34"])
            v = verdict.verify(self.chain(td, eq_ok, bad, {"fail_on_violation": False}))
            self.assertEqual(v["verification"], "failed")  # even though the step passed
            self.assertEqual(v["checks"]["setup"]["status"], "failed")
            self.assertTrue(any("fail_on_violation: false" in n for n in v["notes"]))
            self.assertIn("failed: setup, drv", verdict.line(v))
            timeout = {"result": "inconclusive (timeout)", "depth": 10, "on_timeout": "warn"}
            v = verdict.verify(self.chain(td, timeout, self.signoff(td, "ok2")))
            self.assertEqual(v["verification"], "inconclusive")
            v = verdict.verify(self.chain(td, dict(timeout, on_timeout="pass"), self.signoff(td, "ok3")))
            self.assertEqual(v["verification"], "passed")
            self.assertTrue(any("accepted" in n for n in v["notes"]))
            v = verdict.verify(self.chain(td, eq_ok, None))  # no signoff in the chain
            self.assertEqual(v["verification"], "omitted")
            self.assertIn("signoff (no signoff", v["omitted"][0])
            sub = self.signoff(td, "sub", checks=["drc", "lvs"])
            v = verdict.verify(self.chain(td, eq_ok, sub))
            self.assertEqual(v["verification"], "omitted")
            self.assertIn("setup (not among the signoff checks)", v["omitted"])
            g = self.chain(td, None, self.signoff(td, "ok4"), last_selected=False)
            g["attempts"][-1]["status"] = "failed"
            v = verdict.verify(g)
            self.assertEqual((v["execution"], v["head"]), ("failed", "floorplan.0"))  # verified as far as it got
            g["attempts"][-1].update(status="rejected", error="ECO regressed timing")
            v = verdict.verify(g)
            self.assertEqual(v["execution"], "completed")  # ran, refused by a rule: a note
            self.assertTrue(any("rejected" in n for n in v["notes"]))
            v = verdict.verify(self.graph(td, [{"id": "p.0", "step": "p", "kind": "step", "parent": None,
                                                 "params": {}, "outputs": {}}], {"p": {"selected": "p.0"}}))
            self.assertEqual(v["verification"], "completed")  # a layout and no check at all: nothing established
            self.assertIn("signoff (no signoff", v["omitted"][0])
            self.assertEqual(verdict.verify({"attempts": [], "steps": {}})["execution"], "empty")
            g = self.chain(td, eq_ok, self.signoff(td, "ok5"))  # a later step stopped before it selected
            for st, want in (("cancelled", "cancelled"), ("budget", "budget"), ("failed", "failed")):
                g2 = dict(g, attempts=g["attempts"] + [{"id": "x.0", "step": "x", "kind": "step", "parent": "signoff.0",
                                                        "params": {}, "outputs": {}, "status": st}])
                self.assertEqual(verdict.verify(g2)["execution"], want)

    def test_a_change_after_a_check_makes_it_stale(self):
        """An ECO after signoff: the signoff described the design before it; the run does not inherit its pass."""
        from sipath import verdict
        eq_ok = {"result": "equivalent for 10 cycles from reset", "depth": 10}
        with tempfile.TemporaryDirectory() as td:
            g = self.chain(td, eq_ok, self.signoff(td, "ok"))
            g["attempts"].append({"id": "eco1.0", "step": "eco1", "kind": "eco", "parent": "signoff.0", "params": {},
                                  "outputs": {}, "status": "selected"})
            g["steps"]["eco1"] = {"selected": "eco1.0", "parent": "signoff.0"}
            g["head"] = "eco1.0"
            v = verdict.verify(g)
            self.assertEqual(v["verification"], "omitted")
            self.assertEqual(v["checks"]["drc"]["status"], "stale")
            self.assertIn("eco1.0", v["checks"]["drc"]["detail"])
            self.assertEqual(v["checks"]["equivalence"]["status"], "passed")  # RTL against the synthesized netlist
            so2 = self.signoff(td, "ok2")  # signoff again on the changed design: current again
            g["attempts"].append({"id": "signoff2.0", "step": "signoff", "kind": "step", "parent": "eco1.0",
                                  "params": {}, "outputs": {"signoff": so2}, "status": "selected"})
            g["head"] = "signoff2.0"
            self.assertEqual(verdict.verify(g)["verification"], "passed")

    def test_coverage_notes(self):
        from sipath import verdict
        with tempfile.TemporaryDirectory() as td:
            so = self.signoff(td, "n", lvs={"match": True, "blackboxed": {"u_adc": "EF_ADC"}},
                              drc={"count": 0, "abstract_macros": ["u_sram"]},
                              timing={"unconstrained": ["u_core.ro_0/D", "u_core.ro_1/D"]},
                              density={"count": 0, "scope": "embedded macro: informational"})
            v = verdict.verify(self.chain(td, {"result": "skipped", "reason": "macros are black boxes to the check"},
                                          so))
            text = " ".join(v["notes"])
            for want in ("black boxes checked at their pins only", "abstract macros", "2 unconstrained endpoints",
                         "density: informational"):
                self.assertIn(want, text)
            self.assertEqual(v["checks"]["equivalence"]["status"], "skipped")
            self.assertEqual(v["verification"], "omitted")  # equivalence skipped: not everything was checked

    def test_graph_report_and_metrics_carry_it(self):
        from sipath import report
        with tempfile.TemporaryDirectory() as td:
            f = Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                     demo.fake_factory(), validate_inputs=False)
            f.step("a", demo.fake_step(), score="setup_wns")
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual(g["verification"]["verification"], "completed")  # no check ran
            with open(os.path.join(td, "report.md")) as fh:
                self.assertIn("## Verification: COMPLETED (execution completed)", fh.read())
            m = report.metrics2(g)
            self.assertEqual((m["flow__verification__status"], m["flow__execution__status"]), ("completed", "completed"))


class Doctor(unittest.TestCase):
    """B1.4: sipath doctor, in stand-in environments (no Docker, no OpenROAD, a PDK of another release)."""

    def run_doctor(self, runner=None, pdks=None, status=None):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from sipath import toolchain
        from sipath.tools import doctor
        from sipath.tools.openroad import session
        lock = dict(toolchain.lock(), pdks=pdks or {})
        buf = io.StringIO()
        from types import SimpleNamespace
        from sipath import pdk as pdk_mod
        fake = SimpleNamespace(name="sky130A", files=lambda: [])  # no PDK tree needed here
        with mock.patch.object(doctor, "_docker", lambda rep: False), \
                mock.patch.object(pdk_mod, "load", lambda *a, **k: fake), \
                mock.patch.object(session, "find_openroad", lambda cfg=None: runner), \
                mock.patch.object(toolchain, "lock", lambda: lock), \
                mock.patch.object(toolchain, "pdk_status", lambda pdk: status), redirect_stdout(buf):
            rc = doctor.main([])
        return rc, buf.getvalue()

    def test_no_openroad_fails_with_the_fix(self):
        rc, out = self.run_doctor(runner=None)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  runtime: no OpenROAD found", out)
        self.assertIn("docker pull openroad/orfs@sha256:", out)
        self.assertTrue(out.rstrip().endswith("not ready: fix the FAIL lines"))

    def test_a_native_runner_is_named_and_a_pdk_of_another_release_fails(self):
        from sipath.session import NativeRunner
        tree = NativeRunner("/t/sipath-bin/openroad", bindir="/t/sipath-bin", platforms="/t/p")
        rc, out = self.run_doctor(runner=tree)
        self.assertEqual(rc, 0)
        self.assertIn("native: /t/sipath-bin/openroad (the locked tools unpacked by install-tools)", out)
        pdks = {"sky130": {"variants": ["sky130A"], "version": "8afc"}}
        rc, out = self.run_doctor(runner=tree, pdks=pdks, status={"tree": "/pdk/sky130A", "version": "0123",
                                                                  "locked": "8afc8346", "match": False})
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  pdk sky130A: /pdk/sky130A is 0123, locked 8afc8346", out)
        self.assertIn("install-pdk sky130", out)


class Processes(unittest.TestCase):
    """B2.1: every tool process under one manager: the whole tree dies on a timeout or a cancel, one log format."""

    def alive(self, pid) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_timeout_kills_the_whole_tree_and_logs_it(self):
        import time
        from sipath.tools import process
        with tempfile.TemporaryDirectory() as td:
            pidfile, log = os.path.join(td, "child.pid"), os.path.join(td, "t.log")
            # a shell that starts a grandchild and waits: killing the shell alone would leave the sleep running
            r = process.run(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; echo started; wait"], log, timeout=1.5)
            self.assertTrue(r.timed_out)
            with open(pidfile) as fh:
                grandchild = int(fh.read())
            time.sleep(0.2)
            self.assertFalse(self.alive(grandchild), "the grandchild survived the timeout")
            with open(log) as fh:
                head = fh.read().splitlines()
            self.assertTrue(head[0].startswith("# sh -c sleep 60"))
            self.assertIn("timed out after 1.5s", head[1])
            self.assertIn("started", head)

    def test_exit_code_output_and_peak_memory(self):
        from sipath.tools import process
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "ok.log")
            r = process.run([sys.executable, "-c", "x = bytearray(60 * 2**20); print('done'); raise SystemExit(3)"],
                            log, label=["mytool", "--flag"])
            self.assertEqual((r.rc, r.timed_out, r.cancelled), (3, False, False))
            self.assertIn("done", r.output)
            self.assertGreater(r.peak_mb, 50)  # the 60 MB it held
            with open(log) as fh:
                head = fh.read().splitlines()
            self.assertEqual(head[0], "# mytool --flag")
            self.assertRegex(head[1], r"# exit 3 after [0-9.]+s, peak memory [0-9.]+ MB")

    def test_a_cancel_reaches_a_running_tool(self):
        import threading
        import time
        from sipath.tools import process
        from sipath.scheduler import _Processes
        with tempfile.TemporaryDirectory() as td:
            f = Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                     demo.fake_factory(), validate_inputs=False)
            tracker = _Processes(f, "a.0")
            threading.Timer(0.5, f.cancel).start()
            t0 = time.time()
            with self.assertRaises(process.ProcessCancelled):
                process.run(["sleep", "30"], os.path.join(td, "c.log"), tracker=tracker)
            self.assertLess(time.time() - t0, 10)
            self.assertEqual(f._procs.get("a.0"), set())
            with self.assertRaisesRegex(process.ProcessCancelled, "before it started"):  # still cancelled
                process.run(["true"], None, tracker=tracker)

    def test_children_a_tool_leaves_behind_end_with_it(self):
        import time
        from sipath.tools import process
        with tempfile.TemporaryDirectory() as td:
            pf = os.path.join(td, "pid")
            t0 = time.time()
            r = process.run(["sh", "-c", f"sleep 60 & echo $! > {pf}; exit 0"], None, timeout=30)
            self.assertLess(time.time() - t0, 5)  # no wait on the pipe the child held (it waited 10 s)
            time.sleep(0.2)
            with open(pf) as fh:
                pid = int(fh.read())
            self.assertEqual((r.rc, r.cancelled), (0, False))  # finished normally ...
            self.assertFalse(self.alive(pid))  # ... and its background child did not outlive it
            p = process.Process(["sh", "-c", f"sleep 60 & echo $! > {pf}; exit 0"])
            p.wait()
            time.sleep(0.2)
            with open(pf) as fh:
                pid = int(fh.read())
            self.assertTrue(self.alive(pid))  # the leader is gone, its child is not
            p.kill()  # a cancel now still reaches the group
            p.close()
            time.sleep(0.2)
            self.assertFalse(self.alive(pid))

    def test_an_interrupted_caller_takes_its_tool_with_it(self):
        import time
        from unittest import mock
        from sipath.tools import process
        with tempfile.TemporaryDirectory() as td:
            pf = os.path.join(td, "pid")
            real_wait = process.Process.wait

            def interrupted(self, timeout=None):
                for _ in range(100):  # the tool has started
                    if os.path.isfile(pf) and os.path.getsize(pf):
                        break
                    time.sleep(0.05)
                raise KeyboardInterrupt
            with mock.patch.object(process.Process, "wait", interrupted), self.assertRaises(KeyboardInterrupt):
                process.run(["sh", "-c", f"echo $$ > {pf}; sleep 60"], None, timeout=30)
            self.assertIs(process.Process.wait, real_wait)
            time.sleep(0.2)
            with open(pf) as fh:
                self.assertFalse(self.alive(int(fh.read())))

    def test_a_container_is_stopped_by_name(self):
        from unittest import mock
        from sipath.tools import process
        killed = []
        real = process.subprocess.run

        def spy(cmd, *a, **k):
            if cmd[:2] == ["docker", "kill"]:
                killed.append(cmd[2])
                return process.subprocess.CompletedProcess(cmd, 0)
            return real(cmd, *a, **k)
        with mock.patch.object(process.subprocess, "run", spy):
            r = process.run(["sleep", "30"], None, timeout=0.5, container="sipath-tool-abc")
        self.assertTrue(r.timed_out)
        self.assertEqual(killed, ["sipath-tool-abc"])
        self.assertIsNone(r.peak_mb)  # a container's memory is not the docker client's
        from sipath.session import DockerRunner
        cmd = DockerRunner("img@sha256:" + "0" * 64).tool_command("/tmp", ["klayout", "-v"], name="sipath-tool-x")
        self.assertEqual(cmd[cmd.index("--name") + 1], "sipath-tool-x")


class Budgets(unittest.TestCase):
    """B2.2: compute budgets: a step's time box, the run's wall time and attempt count, admission by threads and
    memory, and the plan shown before a run."""

    def make(self, td, resume=False, **cfg):
        from sipath.config import Config
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        made = []

        def fac(w):
            s = KillableSession(w)
            made.append(s)
            return s
        return Flow(d, sky130hd("/p"), td, fac, validate_inputs=False, config=Config(**cfg), resume=resume), made

    @staticmethod
    def step(ctx):
        if ctx.params.get("slow"):
            ctx.session.run("block")  # until killed
        ctx.metrics["wns"] = float(ctx.params.get("k", 0))

    def test_a_step_time_box_selects_among_what_finished(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            f, _ = self.make(td, step_budget_s=1.0)
            t0 = time.time()
            r = f.step("s", self.step, variants=[{"k": 1}, {"k": 2, "slow": True}, {"k": 3}], score="setup_wns",
                       parallel=3)
            self.assertLess(time.time() - t0, 15)
            st = {a.id: a.status for a in r.attempts}
            self.assertEqual(st["s.1"], "budget")
            self.assertIn("step budget of 1 s reached", r.attempts[1].error)
            self.assertEqual(r.selected.id, "s.2")  # the best of those that finished

    def test_the_run_budget_stops_and_resume_continues(self):
        from sipath.scheduler import BudgetExceeded
        with tempfile.TemporaryDirectory() as td:
            f, _ = self.make(td, budget_s=1.0)
            f.step("a", self.step, variants=[{"k": 1}], score="setup_wns")
            with self.assertRaisesRegex(BudgetExceeded, r"run budget of 1 s reached.*--resume continues"):
                f.step("b", self.step, parent=f.steps["a"].selected, variants=[{"slow": True}], score="setup_wns")
            self.assertEqual(f.verification()["execution"], "budget")
            f2, made = self.make(td, resume=True)
            r = f2.step("a", self.step, variants=[{"k": 1}], score="setup_wns")
            self.assertEqual(made, [])  # recorded
            b = f2.step("b", self.step, parent=r.selected, variants=[{"k": 5}], score="setup_wns")
            self.assertEqual(b.selected.status, "selected")

    def test_attempt_budget_and_admission(self):
        from sipath.scheduler import BudgetExceeded
        with tempfile.TemporaryDirectory() as td:
            f, made = self.make(td, max_attempts=2)
            with self.assertRaisesRegex(BudgetExceeded, "attempt budget of 2"):
                f.step("a", self.step, variants=[{"k": 1}, {"k": 2}, {"k": 3}], score="setup_wns")
            self.assertEqual([a.status for a in f.attempts].count("budget"), 1)
            self.assertEqual(len(made), 2)
        with tempfile.TemporaryDirectory() as td:  # explicit threads larger than the cores: fewer at once
            f, _ = self.make(td, threads=8)
            f.step("a", self.step, variants=[{"k": i, "threads": 4} for i in range(3)], score="setup_wns", parallel=3)
            self.assertEqual(f.allocation["a"]["at_once"], 2)
            self.assertIn("4 threads each in 8 cores", f.allocation["a"]["limited_by"])
            f.attempts[0].metrics["mem_peak_mb"] = 3000  # a recorded peak: two would not fit in 4 GB
            f.config = f.config.replace(memory_gb=4)
            f.step("b", self.step, parent=f.steps["a"].selected, variants=[{"k": 1}, {"k": 2}], score="setup_wns",
                   parallel=2)
            self.assertEqual(f.allocation["b"]["at_once"], 1)
            self.assertIn("3000 MB each in 4 GB", f.allocation["b"]["limited_by"][0])

    def test_the_plan_shows_the_allocation(self):
        from sipath import designfile
        from sipath.config import Config
        path = os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8_explore.yaml")
        p = designfile.plan(path, config=Config(threads=12, parallel_cap=2, max_attempts=10))
        rows = {r["step"]: r for r in p["steps"]}
        self.assertEqual((rows["place"]["attempts"], rows["place"]["at_once"], rows["place"]["threads_each"]),
                         (3, 2, 6))
        self.assertEqual(p["problems"], [])
        self.assertTrue(any("max_attempts 10" in n for n in p["notes"]))
        if "signoff" in rows:  # the reference flow ends in signoff where the PDK has its decks
            self.assertIn("sta", rows["signoff"]["concurrent_checks"])
        self.assertIn("at once", designfile.plan_text(p))


class Fork(unittest.TestCase):
    """B2.3: a new run from any finished attempt, rejected ones included; the original run is never written."""

    def test_fork_from_a_rejected_attempt(self):
        import hashlib
        from sipath import mcp_server
        with tempfile.TemporaryDirectory() as td:
            orig = os.path.join(td, "orig")
            f = Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), orig,
                     demo.fake_factory(), validate_inputs=False)
            a = f.step("a", demo.fake_step(), score="setup_wns")
            p = f.step("p", demo.fake_step(), parent=a.selected, variants=[{"k": 0}, {"k": 1}], score="setup_wns")
            rejected = next(x for x in p.attempts if x.status == "rejected")

            def digest():
                with open(os.path.join(orig, "graph.json"), "rb") as fh:
                    return hashlib.sha256(fh.read()).hexdigest()
            before = digest()
            g = f.fork(rejected.id, os.path.join(td, "fork"))
            head = g.steps["p"].selected
            self.assertEqual((head.id, head.status, head.forked_from), (rejected.id, "selected", f"{orig}:{rejected.id}"))
            self.assertEqual(head.checkpoint, rejected.checkpoint)  # read from the original, not copied
            r = g.step("c", demo.fake_step(), parent=head, score="setup_wns")
            self.assertTrue(r.selected.dir.startswith(os.path.join(td, "fork")))
            self.assertEqual(digest(), before)  # the original graph is byte-identical
            self.assertEqual(rejected.status, "rejected")
            fg = _read_json(os.path.join(td, "fork", "graph.json"))
            self.assertEqual(fg["provenance"]["forked_from"], {"run": orig, "attempt": rejected.id})
            g2 = Flow(f.design, f.pdk, os.path.join(td, "fork"), demo.fake_factory(), validate_inputs=False,
                      resume=True)  # the fork resumes like any run
            self.assertEqual(g2.steps["c"].selected.parent.id, rejected.id)
            for bad, msg in (("nope.0", "no attempt"), (rejected.id, "already holds a run")):
                with self.assertRaisesRegex(ValueError, msg):
                    f.fork(bad, os.path.join(td, "fork"))
            with self.assertRaisesRegex(ValueError, "its own directory"):
                f.fork(rejected.id, os.path.join(orig, "inside"))
            s = mcp_server.Server(emit=lambda m: None)
            s.runs["r1"] = {"flow": f, "design": "fake", "job": None}
            out = s.fork_run("r1", rejected.id, os.path.join(td, "fork2"))
            self.assertEqual(out["recorded_steps"]["p"], rejected.id)
            self.assertIn(out["run_id"], s.runs)


class ExecutableDocs(unittest.TestCase):
    """B2.4: the documentation's code is checked, not only read: every YAML block validates as a design file or a
    flow (step names, parameters, prerequisites), every Python block compiles, every `sipath run` command makes a
    plan, every other `sipath` command exists. (The MCP walkthrough runs in the live suite.)"""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def blocks(self, lang):
        import glob
        import re
        out = []
        for f in ["README.md"] + sorted(glob.glob(os.path.join(self.ROOT, "docs", "*.md"))):
            path = os.path.join(self.ROOT, f) if not os.path.isabs(f) else f
            with open(path) as fh:
                text = fh.read()
            for m in re.finditer(r"```" + lang + r"\n(.*?)```", text, re.S):
                out.append((os.path.relpath(path, self.ROOT), text[:m.start()].count("\n") + 1, m.group(1)))
        return out

    def test_yaml_blocks_are_valid_designs_or_flows(self):
        import yaml
        from sipath import designfile, schema, steps
        from sipath.scheduler import chain_problems
        checked = 0
        for f, line, body in self.blocks("yaml"):
            where = f"{f}:{line}"
            data = yaml.safe_load(body)
            if isinstance(data, dict) and {"name", "top"} <= set(data):
                schema.validate(data, required=True)  # raises on a wrong key, type or missing field
            flow = data if isinstance(data, list) else (data or {}).get("flow") if isinstance(data, dict) else None
            if not flow:
                continue
            chain = []
            for item in flow:
                ref = item.get("fn", item["step"])
                if ":" in ref or ref not in steps.STEPS:  # a user's step by import path: names a function
                    self.assertIn(":", ref, f"{where}: unknown step {ref!r}")
                    continue
                steps.check_params(ref, item.get("variants"))
                chain.append((item["step"], steps.STEPS[ref], item.get("variants")))
            if chain and chain[0][0] in ("synth", "floorplan"):  # a whole flow: its prerequisites hold
                root = set() if chain[0][0] == "synth" else {"netlist"}
                self.assertEqual(chain_problems(root, chain), [], where)
            checked += 1
        self.assertGreater(checked, 10)

    def test_python_blocks_compile(self):
        found = self.blocks("python")
        self.assertTrue(found)
        for f, line, body in found:
            compile(body, f"{f}:{line}", "exec")

    def test_documented_commands_exist_and_run_commands_plan(self):
        import re
        import shlex
        import subprocess
        with open(os.path.join(self.ROOT, "sipath", "__main__.py")) as fh:
            main = fh.read()
        known = set(re.findall(r'cmd == "([a-z-]+)"', main)) | {"draw", "render"}
        planned = 0
        for f, line, body in self.blocks("bash"):
            for ln in body.replace("\\\n", " ").splitlines():
                ln = ln.split("  #")[0].strip()
                m = re.search(r"(?:python3 -m sipath|^sipath) ([a-z-]+)(.*)", ln)
                if not m or ln.startswith("#"):
                    continue
                self.assertIn(m.group(1), known, f"{f}:{line}: `sipath {m.group(1)}` is not a command")
                if m.group(1) != "run":
                    continue
                args = shlex.split(m.group(2))
                design = args[0] if args else ""
                path = design if os.path.isfile(os.path.join(self.ROOT, design)) else \
                    os.path.join(self.ROOT, "sipath", "examples", design + ".yaml")
                if not os.path.isfile(os.path.join(self.ROOT, path)):
                    continue  # a design of the reader's (my.yaml)
                keep = [a for a in args if a in ("--explore",)]
                r = subprocess.run([sys.executable, "-m", "sipath", "run", path, os.path.join(self.ROOT, "runs", "_docplan"),
                                    *keep, "--plan"], capture_output=True, text=True, cwd=self.ROOT, timeout=120)
                self.assertEqual(r.returncode, 0, f"{f}:{line}: {ln}\n{r.stdout[-800:]}{r.stderr[-800:]}")
                self.assertIn("plan for", r.stdout)
                planned += 1
        self.assertGreater(planned, 3)


class BenchmarkRecords(unittest.TestCase):
    """B3.1: the documentation's benchmark tables are written from records, and each record says where its numbers
    came from (metrics under names that say what they measure, verdict, toolchain, machine, inputs)."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def setUp(self):
        sys.path.insert(0, os.path.join(self.ROOT, "bench"))

    def test_tables_match_their_records(self):
        import tables
        for doc in sorted(set(tables.DOCS.values())):
            with open(os.path.join(self.ROOT, doc)) as fh:
                text = fh.read()
            self.assertEqual(text, tables.render(text), f"{doc}: a generated table is stale (python3 bench/tables.py)")
        for name, doc in tables.DOCS.items():  # every table is in its document
            with open(os.path.join(self.ROOT, doc)) as fh:
                self.assertIn(f"<!-- table:{name} -->", fh.read(), name)

    def test_every_record_is_complete(self):
        import records
        recs = records.load()
        self.assertGreater(len(recs), 20)
        for r in recs:
            for k in ("kind", "design", "flow", "measured", "metrics", "toolchain", "machine", "inputs", "backfilled"):
                self.assertIn(k, r, r["_file"])
            self.assertTrue(r["toolchain"].get("orfs", "").count("@sha256:"), r["_file"])  # pinned
            self.assertTrue(r["machine"].get("cores"), r["_file"])
            self.assertFalse({"wns_ns", "wirelength_um", "hpwl"} & set(r["metrics"]), r["_file"])  # say what it is
            if r["flow"] != "orfs" and (r.get("source") or "").endswith(("/run",)) or r["kind"] == "fallback":
                self.assertIn(r["verification"]["execution"], ("completed", "failed", "cancelled", "budget"),
                              r["_file"])  # a run graph backs it: its verdict is recorded
                self.assertIn("commit", r["sipath"], r["_file"])
            elif r["flow"] != "orfs":  # no graph of its own (a directory resumed later): said so
                self.assertTrue(r["metrics"].get("note"), r["_file"])
            if r["metrics"].get("exit"):  # a run that failed says why
                self.assertTrue(r.get("failure"), r["_file"])
            gaps = r.get("provenance_gaps") or []
            if r["flow"] != "orfs" and not (r.get("sipath") or {}).get("commit"):  # an unknown commit is said
                self.assertTrue(any(g.startswith("sipath.commit") for g in gaps), r["_file"])
            if r.get("inputs") == {} and "inputs_note" not in r:
                self.assertIn("inputs: unknown", gaps, r["_file"])
            if r["kind"] == "repeat":  # what the configuration set is recorded, not read from a later default
                eff = {**(r.get("overrides") or {}).get("synth", {}),
                       **{k.split(".", 1)[1]: v for k, v in (r.get("effective") or {}).items() if k.startswith("synth.")}}
                self.assertIn("synth_fallback", eff, r["_file"])


class BoundedSearch(unittest.TestCase):
    """B3.2: keep the best k attempts (and the baseline) through later steps and decide on the later result."""

    @staticmethod
    def place(ctx):
        ctx.metrics["wns"] = float(ctx.params.get("d", 0))  # by its own score, more is better here

    @staticmethod
    def route(ctx):
        a = ctx.parent
        while a is not None and not a.step.startswith("place"):
            a = a.parent
        d = a.params.get("d", 0)
        if d == 2 and ctx.params.get("fail_d2"):
            raise RuntimeError("router gave up")
        ctx.metrics["wns"] = -float(d)  # ... but the best placement routes worst: the baseline wins

    def items(self, route_variants=None, **place_opts):
        from sipath import demo
        return [("place", "place", self.place, {"step": "place", "variants": [{}, {"d": 1}, {"d": 2}, {"d": 3}],
                                                 "score": "setup_wns", **place_opts}),
                ("route", "route", self.route, {"step": "route", "score": "setup_wns",
                                                **({"variants": route_variants} if route_variants else {})}),
                ("sign", "sign", demo.fake_step(), {"step": "sign", "score": "setup_wns"})]

    def flow(self, td):
        return Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                    demo.fake_factory(), validate_inputs=False)

    def test_the_baseline_can_win_downstream(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            designfile.run_items(f, self.items(keep=2, decide_at="route"))
            self.assertEqual(f.steps["place"].selected.id, "place.3")
            self.assertEqual(f.steps["place"].kept, ["place.3", "place.2", "place.0"])  # best 2, then the baseline
            self.assertEqual({a.id: a.status for a in f.steps["place"].attempts},
                             {"place.0": "kept", "place.1": "rejected", "place.2": "kept", "place.3": "selected"})
            self.assertTrue({"route", "route@place.2", "route@place.0"} <= set(f.steps))
            d = f.decisions[-1]
            self.assertEqual((d["at"], d["winner"]), ("route", "route@place.0.0"))
            self.assertEqual(f.steps["sign"].selected.parent.id, "route@place.0.0")  # continues from the winner
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual(g["decisions"][-1]["winner"], "route@place.0.0")
            self.assertEqual(g["steps"]["place"]["kept"], ["place.3", "place.2", "place.0"])

    def test_a_failed_branch_drops_out_and_bad_settings_are_refused(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            designfile.run_items(f, self.items(route_variants=[{"fail_d2": True}], keep=2, decide_at="route"))
            self.assertEqual([c["attempt"] for c in f.decisions[-1]["candidates"]],
                             ["route.0", "route@place.0.0"])  # place.2's branch failed at route
        for opts, msg in (({"keep": 2}, "needs decide_at"), ({"keep": 2, "decide_at": "place"}, "needs decide_at"),
                          ({"decide_at": "route"}, "need keep > 1")):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(ValueError, msg):
                    designfile.run_items(self.flow(td), self.items(**opts))


class RunHead(unittest.TestCase):
    """Review of 04ee8af, 1 and 2: one authoritative result (the run's head) that every consumer reads through its
    ancestry; global termination is not a lost branch; search state survives a resume."""

    @staticmethod
    def place(ctx):
        ctx.metrics["wns"] = float(ctx.params.get("d", 0))

    @staticmethod
    def route(ctx):
        a = ctx.parent
        while not a.step.startswith("place"):
            a = a.parent
        d = a.params.get("d", 0)
        if d in ctx.params.get("fail", []):
            raise RuntimeError("router gave up")
        ctx.metrics["wns"] = float(ctx.params.get("sign", -1)) * d  # sign -1: the baseline wins; +1: the best

    def items(self, route_variant=None, then_sign=False):
        from sipath import demo
        it = [("place", "place", self.place, {"step": "place", "variants": [{}, {"d": 1}, {"d": 2}, {"d": 3}],
                                               "score": "setup_wns", "keep": 2, "decide_at": "route"}),
              ("route", "route", self.route, {"step": "route", "score": "setup_wns",
                                              **({"variants": [route_variant]} if route_variant else {})})]
        if then_sign:
            it.append(("sign", "sign", demo.fake_step(), {"step": "sign", "score": "setup_wns"}))
        return it

    def flow(self, td, resume=False, **cfg):
        from sipath.config import Config
        return Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                    demo.fake_factory(), validate_inputs=False, config=Config(**cfg), resume=resume)

    def consumers(self, f, td):
        """What every consumer says the result is."""
        from sipath import report, verdict
        g = _read_json(os.path.join(td, "graph.json"))
        r = report.collect(td)
        return {"flow": f.head.id, "result()": f.result().id, "graph": g["head"],
                "verdict": verdict.verify(g)["head"], "report": r["head"],
                "metrics2_wns": report.metrics2(g).get("route__timing__setup__ws")}

    def test_every_consumer_agrees_where_the_run_ends(self):
        from sipath import designfile
        for variant, then_sign, want in ((None, False, "route@place.0.0"),  # ends at decide_at; a kept branch wins
                                         ({"sign": 1}, False, "route.0"),  # the selected branch wins
                                         ({"fail": [0]}, False, "route@place.2.0"),  # the baseline's branch failed
                                         (None, True, "sign.0")):  # a step after the decision: on the winner
            with tempfile.TemporaryDirectory() as td:
                f = self.flow(td)
                designfile.run_items(f, self.items(variant, then_sign))
                c = self.consumers(f, td)
                ids = {k: v for k, v in c.items() if k != "metrics2_wns"}
                self.assertEqual(set(ids.values()), {want}, c)
                win = f.decisions[-1]["winner"]
                self.assertEqual(c["metrics2_wns"], next(a for a in f.attempts if a.id == win).metrics["wns"])
                if variant and variant.get("fail"):
                    self.assertEqual(f.decisions[-1]["failed_branches"][0]["branch"], "place.0")
                if then_sign:
                    self.assertEqual(f.result("route").id, win)

    def test_budget_and_cancel_stop_the_search_and_resume_finishes_it(self):
        from sipath import designfile
        from sipath.scheduler import BudgetExceeded, StepCancelled
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td, max_attempts=5)  # four placements, one route: the other branches never run
            with self.assertRaises(BudgetExceeded):
                designfile.run_items(f, self.items())
            self.assertEqual(f.decisions, [])  # no comparison was made, so none is recorded
            self.assertEqual(f.verification()["execution"], "budget")
            f2 = self.flow(td, resume=True)
            designfile.run_items(f2, self.items())
            self.assertEqual(f2.decisions[-1]["winner"], "route@place.0.0")
            self.assertTrue(f2.decisions[-1]["complete"])
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            calls = []

            def route_then_cancel(ctx):
                calls.append(ctx.attempt.id)
                if len(calls) == 2:
                    f.cancel()
                    raise RuntimeError("killed")
                self.route(ctx)
            it = self.items()
            it[1] = ("route", "route", route_then_cancel, it[1][3])
            with self.assertRaises(StepCancelled):
                designfile.run_items(f, it)
            self.assertEqual(f.decisions, [])

    def test_resume_reuses_the_decision_and_finds_the_baseline_by_its_parameters(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            designfile.run_items(f, self.items(then_sign=True))
            n, head = len(f.attempts), f.head.id
            f2 = self.flow(td, resume=True)
            self.assertEqual((len(f2.decisions), f2.head.id), (1, head))  # restored from graph.json
            designfile.run_items(f2, self.items(then_sign=True))
            self.assertEqual((len(f2.attempts), len(f2.decisions), f2.head.id), (n, 1, head))  # nothing new
            f3 = self.flow(td, resume=True)
            f3._make_stale("place", "test: inputs changed")  # the placements renumber after the stale ones
            designfile.run_items(f3, self.items(then_sign=True))
            kept = f3.steps["place"].kept
            base = next(a for a in f3.attempts if a.step == "place" and a.status != "stale" and a.params == {})
            self.assertIn(base.id, kept)  # the baseline, found by its parameters
            self.assertNotEqual(base.id, "place.0")

    def test_selector_scores_rank_by_applying_the_rule_again(self):
        from sipath import scores as score_mod

        def pick_lowest_d(atts):
            best = min(atts, key=lambda a: a.params.get("d", 0))
            return best, "lowest d"
        pick_lowest_d.selector = True
        score_mod.SCORES["_lowest_d"] = pick_lowest_d
        self.addCleanup(score_mod.SCORES.pop, "_lowest_d")
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            f.step("place", self.place, variants=[{"d": 3}, {"d": 1}, {"d": 2}], score="_lowest_d")
            kept = f.keep("place", 2)
            self.assertEqual([a.params["d"] for a in kept], [1, 2])  # the rule's choice, then its next choice


class ResolvedParameters(unittest.TestCase):
    """Review of 04ee8af, 3 and the earlier items: each attempt records the threads it ran with; admission,
    prerequisites and fingerprints use the parameters execution resolves (step_defaults too); retries stay inside the
    step's time box; a resume after SiPath's code changed does not reuse."""

    def flow(self, td, step_defaults=None, factory=None, resume=False, **cfg):
        from sipath.config import Config
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        d.step_defaults = dict(step_defaults or {})
        return Flow(d, sky130hd("/p"), td, factory or demo.fake_factory(), validate_inputs=False,
                    config=Config(**cfg), resume=resume)

    def test_each_attempt_records_its_threads_and_retries_get_their_own(self):
        from sipath.scheduler import RetryWith
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td, threads=12)

            def step(ctx):
                if ctx.params.get("k") == 1 and not ctx.params.get("again"):
                    raise RetryWith({"again": True}, "try again")
                ctx.metrics["wns"] = 1.0
            r = f.step("s", step, variants=[{"k": 0}, {"k": 1}], parallel=2, score="setup_wns")
            got = {a.id: a.threads for a in r.attempts}
            self.assertEqual(got, {"s.0": 6, "s.1": 6, "s.2": 12})  # two at once, then the retry alone
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual({a["id"]: a["threads"] for a in g["attempts"]}, got)
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bench"))
            import strategy_compare
            for a in g["attempts"]:
                a["seconds"] = 10.0
            p = os.path.join(td, "graph.json")
            _write_json(p, g)
            self.assertEqual(strategy_compare.core_seconds(td), 240.0)  # 6*10 + 6*10 + 12*10, not 180

    def test_admission_and_prerequisites_see_step_defaults(self):
        from sipath import steps
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td, step_defaults={"place": {"threads": 8}}, threads=8)

            def place(ctx):
                ctx.metrics["wns"] = 1.0
            place.__name__ = "place"  # declared as the place step: its step_defaults apply
            f.step("p", place, variants=[{}, {"density": 0.6}, {"density": 0.7}], parallel=3, score="setup_wns")
            self.assertEqual(f.allocation["p"]["at_once"], 1)  # 8 threads each (inherited) in 8 cores
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td, step_defaults={"signoff": {"checks": ["drc", "lvs"]}})
            routed = Attempt("route_repair.0", "route_repair", 0, {}, None, td, status="selected",
                             capabilities=["netlist", "floorplan", "power_grid", "placed", "routed"])
            f.attempts.append(routed)
            f.check_prerequisites("signoff", steps.STEPS["signoff"], routed)  # no timing check: no SPEF needed

    def test_a_retry_stays_inside_the_step_time_box(self):
        import time
        from sipath.scheduler import RetryWith
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td, factory=KillableSession, step_budget_s=1.0)

            def step(ctx):
                if not ctx.params.get("again"):
                    raise RetryWith({"again": True}, "try again")
                ctx.session.run("block")  # the retry would run until killed
            t0 = time.time()
            with self.assertRaises(StepFailed):
                f.step("s", step, score="setup_wns")
            self.assertLess(time.time() - t0, 15)
            self.assertEqual([a.status for a in f.attempts], ["failed", "budget"])

    def test_a_code_change_is_not_reused(self):
        from sipath import fingerprint
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            f.step("a", demo.fake_step(), score="setup_wns")
            saved = dict(fingerprint._CODE)
            fingerprint._CODE.update(digest="0" * 16)  # as if SiPath's sources changed
            self.addCleanup(lambda: (fingerprint._CODE.clear(), fingerprint._CODE.update(saved)))
            f2 = self.flow(td, resume=True)
            self.assertTrue(any("SiPath's code changed" in c for c in f2.input_changes))
            f2.step("a", demo.fake_step(), score="setup_wns")
            self.assertEqual(f2.stale[0]["step"], "a")


class BenchmarkRobustness(unittest.TestCase):
    """Review of 04ee8af, 4 and 5: every benchmark run leaves a record, the driver's exit says whether all completed,
    and a record keeps what the run was (its inputs as hashed when it ran, its source)."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def setUp(self):
        sys.path.insert(0, os.path.join(self.ROOT, "bench"))

    def graph(self, run_dir, signoff_fails=False, inputs=None, failed_step=None):
        os.makedirs(run_dir, exist_ok=True)
        so = os.path.join(run_dir, "signoff.json")
        _write_json(so, {"checks": ["setup"], "required": ["setup"], "pass": not signoff_fails,
                         "failures": ["setup: worst -1.0 ns"] if signoff_fails else [],
                         "timing": {"corners": {"ss_max": {"wns": -1.0 if signoff_fails else 0.5}}}})
        g = {"design": "x", "head": "signoff.0", "provenance": {"sipath": {"version": "0.1.0", "commit": "abc"}},
             "steps": {"signoff": {"selected": "signoff.0"}},
             "attempts": [{"id": "signoff.0", "step": "signoff", "parent": None, "status": "selected",
                           "kind": "step", "seconds": 1.0, "threads": 4, "dir": run_dir,
                           "outputs": {"signoff": so}, "metrics": {}}]}
        if inputs is not None:
            g["inputs"] = {"files": inputs}
        if failed_step:  # the run stopped at a later step, whose every attempt failed
            g["attempts"].append({"id": f"{failed_step}.0", "step": failed_step, "parent": "signoff.0",
                                  "status": "failed", "kind": "step", "seconds": 1.0,
                                  "error": "RuntimeError: [ERROR DRT-0206] checkConnectivity error."})
        _write_json(os.path.join(run_dir, "graph.json"), g)

    def drive(self, td, behaviour):
        """strategy_compare.run with the sipath run and the measurement stubbed: behaviour(out_dir) -> exit code."""
        from unittest import mock
        import strategy_compare as sc

        real = sc.subprocess.run

        def fake_run(cmd, **kw):
            if "-m" in cmd and "sipath" in cmd and "run" in cmd:  # the run itself: the behaviour under test
                out = os.path.dirname(cmd[cmd.index("run") + 2])
                return mock.Mock(returncode=behaviour(out))
            return real(cmd, **kw)  # rm, git: as they are
        with mock.patch.object(sc, "OUT", td), mock.patch.object(sc.subprocess, "run", fake_run), \
                mock.patch.dict(sc.DENSITIES, {"x": [0.6]}), \
                mock.patch.object(sc, "design", lambda n: ({"name": n, "sdc": "/x.sdc", "top": "t"}, ["place"])), \
                mock.patch.object(sc.records, "RESULTS", os.path.join(td, "results")), \
                mock.patch.object(sc.oc, "sipath_result", lambda w, tag: {}), \
                mock.patch.object(sc.oc, "measure", self.measure), \
                mock.patch("sipath.pdk.load", lambda *a, **k: types.SimpleNamespace(analysis_corners=lambda: [
                    types.SimpleNamespace(name="tt_nom", liberty="/tt.lib")])):
            return sc.run("x", "defaults", 1)

    measure = staticmethod(lambda r, cfg, lib, tag, w: {"wns_ns": 1.0, "cells": 10, "cell_area_um2": 5.0,
                                                         "wirelength_um": 100})

    def test_every_failure_kind_leaves_a_record(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.drive(td, lambda out: 1)  # failed before graph.json existed
            self.assertEqual((r["outcome"], r["metrics"]["exit"]), ("execution_failed", 1))
            self.assertTrue(r["failure"])
            self.assertTrue(os.path.isfile(os.path.join(td, "results", "strategy", "x__defaults__r1.json")))
        with tempfile.TemporaryDirectory() as td:
            def fails_late(out):
                self.graph(os.path.join(out, "run"), failed_step="route_repair")
                with open(os.path.join(out, "run.log"), "w") as fh:
                    fh.write("[ERROR DRT-0206] checkConnectivity error.\n")
                return 1
            r = self.drive(td, fails_late)
            self.assertEqual((r["outcome"], r["failure"]),
                             ("execution_failed", "route_repair.0: [ERROR DRT-0206] checkConnectivity error."))
        with tempfile.TemporaryDirectory() as td:
            self.measure = staticmethod(lambda *a: {"error": "measurement failed (log)"})
            r = self.drive(td, lambda out: (self.graph(os.path.join(out, "run")), 0)[1])
            self.assertEqual(r["outcome"], "measurement_failed")
        with tempfile.TemporaryDirectory() as td:
            self.measure = BenchmarkRobustness.measure
            r = self.drive(td, lambda out: (self.graph(os.path.join(out, "run"), signoff_fails=True), 1)[1])
            self.assertEqual(r["outcome"], "completed")  # a signoff that fails its checks is a result (exit 1) ...
            self.assertEqual(r["verification"]["verification"], "failed")  # ... and its verdict says so
            self.assertEqual(r["metrics"]["wns_tt_ns"], 1.0)  # measured

    def test_the_driver_exits_nonzero_when_a_run_did_not_complete(self):
        from unittest import mock
        import strategy_compare as sc
        outcomes = iter(["completed", "execution_failed"])
        with mock.patch.object(sc, "run", lambda n, s, i: {"outcome": next(outcomes), "metrics": {}, "failure": "x"}), \
                mock.patch.object(sc.records, "archive_source", lambda: {}):
            self.assertEqual(sc.main(["d", "--strategies", "defaults,sweep"]), 1)
        with mock.patch.object(sc, "run", lambda n, s, i: {"outcome": "completed", "metrics": {}}), \
                mock.patch.object(sc.records, "archive_source", lambda: {}):
            self.assertEqual(sc.main(["d", "--strategies", "defaults"]), 0)

    def test_a_record_keeps_the_inputs_as_they_were_when_the_run_ran(self):
        import records
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "x.sdc")
            with open(src, "w") as fh:
                fh.write("create_clock -period 10\n")
            from sipath.fingerprint import file_hash
            h0 = file_hash(src)
            self.graph(os.path.join(td, "run"), inputs={src: h0})
            with open(src, "w") as fh:
                fh.write("create_clock -period 5\n")  # changed after the run
            r = records.from_design_run(os.path.join(td, "run"), "t", "x", "f")
            self.assertEqual(list(r["inputs"].values()), [h0])
            os.remove(src)  # deleted: still the run's hash
            self.assertEqual(list(records.from_design_run(os.path.join(td, "run"), "t", "x", "f")["inputs"].values()), [h0])
            self.graph(os.path.join(td, "old"))  # a run from before input hashes were recorded
            r = records.from_design_run(os.path.join(td, "old"), "t", "x", "f")
            self.assertIsNone(r["inputs"])
            self.assertIn("unknown", r["inputs_note"])
            self.assertIn("by", r["exported"])  # the exporter's identity, apart from the run's
            self.assertEqual(r["sipath"]["commit"], "abc")  # the run's, from its provenance


class RunHeadFollowUp(unittest.TestCase):
    """Second review of the run head (6fdd7bc): persistence on resume, every consumer, the scoring policy of a
    reused decision, forks, handled branch failures, archives, table eligibility, plan/execution parity."""

    items = RunHead.items
    place, route = staticmethod(RunHead.place), staticmethod(RunHead.route)

    def flow(self, td, resume=False, **cfg):
        return RunHead.flow(self, td, resume=resume, **cfg)

    def go(self, td, items):
        from sipath import designfile
        f = self.flow(td)
        designfile.run_items(f, items)
        return f

    def test_resume_persists_the_head_it_ends_with(self):
        from sipath import designfile, report, verdict
        with tempfile.TemporaryDirectory() as td:
            self.go(td, self.items(then_sign=True))
            f2 = self.flow(td, resume=True)
            designfile.run_items(f2, self.items(then_sign=True))
            g = _read_json(os.path.join(td, "graph.json"))  # what a later reader sees
            self.assertEqual((g["head"], verdict.verify(g)["head"], report.collect(td)["head"]),
                             (f2.head.id,) * 3)
            self.assertEqual(f2.head.id, "sign.0")

    def test_pictures_and_limits_read_the_winner(self):
        from sipath import limits, viz
        with tempfile.TemporaryDirectory() as td:  # the selected branch wins: neither the last branch nor the baseline
            f = self.go(td, self.items({"sign": 1}))
            win = f.decisions[-1]["winner"]
            self.assertEqual(win, "route.0")
            self.assertNotEqual(f.attempts[-1].id, win)
            self.assertEqual(viz.checkpoints(td)[0][0], win)
            self.assertEqual(limits.run_metrics(td, until="route")["wns_ns"], 3.0)
            self.assertEqual(viz.checkpoints(td, "route@place.0.0")[0][0], "route@place.0.0")  # alternatives by name

    def test_a_changed_decide_score_decides_again(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            self.go(td, self.items())  # setup_wns: the baseline's branch wins
            f2 = self.flow(td, resume=True)
            designfile.run_items(f2, self.items())  # unchanged policy: the recorded decision
            self.assertEqual((f2.decisions[-1]["winner"], f2.decisions[-1]["score"]), ("route@place.0.0", "setup_wns"))
            it = self.items()
            it[0][3]["decide_score"] = "hpwl"
            for a in f2.attempts:
                if a.step.startswith("route"):
                    a.metrics["hpwl_um"] = 100.0 - 10 * float(a.metrics["wns"] * -1)  # the best placement: least wire
            f2.save_graph()
            f3 = self.flow(td, resume=True)
            n = len(f3.attempts)
            designfile.run_items(f3, it)
            self.assertEqual(len(f3.attempts), n)  # the candidates are reused ...
            self.assertEqual((f3.decisions[-1]["score"], f3.decisions[-1]["winner"]), ("hpwl", "route.0"))  # ... decided again
            self.assertEqual(len([d for d in f3.decisions if d["at"] == "route"]), 1)

    def test_a_fork_starts_with_its_head(self):
        from sipath.scheduler import Flow
        with tempfile.TemporaryDirectory() as td:
            f = self.go(os.path.join(td, "orig"), self.items())
            kept = next(a for a in f.attempts if a.status == "kept")  # a branch the search carried: forkable
            g = f.fork(kept.id, os.path.join(td, "fork"))
            self.assertEqual((g.head.id, g.result().id), (kept.id, kept.id))
            self.assertEqual(g.head.threads, kept.threads)
            self.assertEqual(_read_json(os.path.join(td, "fork", "graph.json"))["head"], kept.id)
            g2 = Flow(f.design, f.pdk, os.path.join(td, "fork"), demo.fake_factory(), validate_inputs=False, resume=True)
            self.assertEqual((g2.head.id, g2.verification()["head"]), (kept.id, kept.id))

    def test_a_handled_branch_failure_is_not_the_runs(self):
        from sipath import designfile
        from sipath.scheduler import StepFailed
        with tempfile.TemporaryDirectory() as td:
            f = self.go(td, self.items({"fail": [0]}))  # the baseline's branch fails; the search decides
            v = f.verification()
            self.assertEqual(v["execution"], "completed")
            self.assertTrue(any("search branch that failed" in n for n in v["notes"]))
        with tempfile.TemporaryDirectory() as td:  # every branch failing is the run's failure
            f = self.flow(td)
            with self.assertRaises(StepFailed):
                designfile.run_items(f, self.items({"fail": [0, 2, 3]}))
            self.assertEqual(f.verification()["execution"], "failed")


class RunHeadThirdReview(unittest.TestCase):
    """Third review: a reused decision needs the same candidate results; old handled failures do not hide a new
    cancellation; a named picture is the result's step; a single failed run is labeled; layouts carry digests."""

    RESULT: dict = {}
    items = RunHead.items
    place = staticmethod(RunHead.place)

    @staticmethod
    def route(ctx):
        a = ctx.parent
        while not a.step.startswith("place"):
            a = a.parent
        d = a.params.get("d", 0)
        if d in ctx.params.get("fail", []):
            raise RuntimeError("router gave up")
        ctx.metrics["wns"] = RunHeadThirdReview.RESULT.get(d, -float(d))

    def flow(self, td, resume=False, **cfg):
        return RunHead.flow(self, td, resume=resume, **cfg)

    def setUp(self):
        RunHeadThirdReview.RESULT = {}

    def test_a_rebuilt_candidate_is_decided_again(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            designfile.run_items(f, self.items())
            win = f.decisions[-1]["winner"]
            self.assertEqual(win, "route@place.0.0")
            os.remove(next(a for a in f.attempts if a.id == win).checkpoint)  # lost: rebuilt on resume ...
            RunHeadThirdReview.RESULT[0] = -100.0  # ... with a far worse result
            f2 = self.flow(td, resume=True)
            designfile.run_items(f2, self.items())
            self.assertEqual(f2.decisions[-1]["winner"], "route@place.2.0")
            self.assertEqual(f2.head.id, "route@place.2.0")

    def test_an_old_handled_failure_does_not_hide_a_new_cancellation(self):
        from sipath import designfile
        from sipath.scheduler import StepCancelled
        with tempfile.TemporaryDirectory() as td:
            designfile.run_items(self.flow(td), self.items({"fail": [0]}))  # handled: the search decided
            f2 = self.flow(td, resume=True)

            def cancelled(ctx):
                f2.cancel()
                raise RuntimeError("killed")
            it = self.items({"fail": [0]})
            it[1] = ("route", "route", cancelled, it[1][3])
            with self.assertRaises(StepCancelled):
                designfile.run_items(f2, it)
            self.assertEqual(f2.verification()["execution"], "cancelled")

    def test_a_losing_branch_that_fails_again_on_resume_stays_handled(self):
        from sipath import designfile
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            designfile.run_items(f, self.items({"fail": [0]}, then_sign=True))
            self.assertEqual(f.verification()["execution"], "completed")
            winner = f.decisions[-1]["winner"]
            failed = f.decisions[-1]["failed_branches"]
            for _ in range(2):  # retry the failed branch while reusing the winner and signoff
                f = self.flow(td, resume=True)
                designfile.run_items(f, self.items({"fail": [0]}, then_sign=True))
                self.assertEqual(f.decisions[-1]["winner"], winner)
                self.assertNotEqual(f.decisions[-1]["failed_branches"], failed)
                failed = f.decisions[-1]["failed_branches"]
                self.assertEqual(f.verification()["execution"], "completed")
                g = _read_json(os.path.join(td, "graph.json"))
                self.assertEqual(g["verification"]["execution"], "completed")

    def test_a_selector_decides_among_the_branches_as_asked(self):
        from sipath import designfile, registry, scores

        def lowest(attempts):  # a rule over attempts: the branch with the least slack (not what auto would pick)
            a = min((a for a in attempts if a.metrics.get("wns") is not None), key=lambda a: a.metrics["wns"])
            return a, "least slack"
        lowest.selector = True
        registry.register_score(lowest, "review_lowest")
        self.addCleanup(scores.SCORES.pop, "review_lowest", None)
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            it = self.items()
            it[0][3]["decide_score"] = "review_lowest"
            designfile.run_items(f, it)
            d = f.decisions[-1]
            routed = [a for a in f.attempts if a.step.startswith("route") and a.status != "failed"]
            self.assertEqual(d["score"], "review_lowest")  # the policy asked for, not auto
            self.assertEqual(d["winner"], min(routed, key=lambda a: a.metrics["wns"]).id)
            self.assertEqual(d["why"], "least slack")
        self.assertIsNot(scores.get("review_lowest"), scores.get("auto"))

    def test_a_named_picture_is_the_results_step(self):
        from sipath import designfile, viz
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            designfile.run_items(f, self.items())  # route.0 (the selected placement's) loses to a kept branch
            self.assertEqual(f.head.id, "route@place.0.0")
            self.assertEqual(viz.checkpoints(td, "route")[0][0], "route@place.0.0")

    def test_a_single_failed_run_is_labeled(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(ArchivesAndEligibility.ROOT, "bench"))
        import tables
        recs = [{"kind": "strategy", "design": "dp8", "flow": "defaults", "outcome": "measurement_failed",
                 "metrics": {"exit": 0, "wall_s": 9}}]
        with mock.patch.object(tables, "_by", lambda kind: recs if kind == "strategy" else []):
            row = next(ln for ln in tables.table_strategies().splitlines() if "| dp8 | defaults |" in ln)
        self.assertIn("0 of 1 (measurement failed)", row)

    def test_the_cost_column_is_against_the_defaults(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(ArchivesAndEligibility.ROOT, "bench"))
        import tables
        base = {"kind": "strategy", "design": "dp8", "outcome": "completed", "verification": {"verification": "passed"}}
        recs = [dict(base, flow="defaults", metrics={"exit": 0, "wns_tt_ns": 1.0, "core_seconds": 100.0}),
                dict(base, flow="search", metrics={"exit": 0, "wns_tt_ns": 1.0, "core_seconds": 265.0})]
        with mock.patch.object(tables, "_by", lambda kind: recs if kind == "strategy" else []):
            row = next(ln for ln in tables.table_strategies().splitlines() if "| dp8 | search |" in ln)
        self.assertIn("| 2.65 |", row)

    def test_records_carry_the_final_layouts_digest(self):
        import hashlib
        sys.path.insert(0, os.path.join(ArchivesAndEligibility.ROOT, "bench"))
        import records
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "final.def")
            with open(d, "w") as fh:
                fh.write("DESIGN x ;\n")
            g = {"head": "signoff.0", "attempts": [
                {"id": "extract.0", "step": "extract", "parent": None, "outputs": {"def_": "/missing.def"}},
                {"id": "signoff.0", "step": "signoff", "parent": "extract.0", "outputs": {"def_": d}}]}
            self.assertEqual(records.final_def_digest(g), hashlib.sha256(b"DESIGN x ;\n").hexdigest()[:16])
            g["attempts"].append({"id": "eco_1.0", "step": "eco_1", "kind": "eco", "parent": "signoff.0", "outputs": {}})
            g["head"] = "eco_1.0"
            self.assertIsNone(records.final_def_digest(g))  # signoff's DEF predates the ECO: stale, not final
            g["attempts"].pop()
            g["head"] = "signoff.0"
            g["attempts"][1]["outputs"] = {}
            self.assertIsNone(records.final_def_digest(g))  # extraction's is missing: no digest, not a guess


class ResumeIntegrity(unittest.TestCase):
    """Project review, batch 1: the run's result and its record stay right across resumes, re-selections,
    lost checkpoints, aliased and imported steps, and budgets."""

    def flow(self, td, resume=False, **cfg):
        from sipath.config import Config
        return Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                    demo.fake_factory(), validate_inputs=False, config=Config(**cfg), resume=resume)

    def test_a_reused_eco_is_the_result(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            p = f.step("place", demo.fake_step(), variants=[{"k": 1}])
            f.eco("e1", p.selected, [], fn=demo.fake_step(), score="setup_wns", max_slack_loss=None)
            f2 = self.flow(td, resume=True)
            p2 = f2.step("place", demo.fake_step(), variants=[{"k": 1}])
            e2 = f2.eco("e1", p2.selected, [], fn=demo.fake_step(), score="setup_wns", max_slack_loss=None)
            self.assertEqual((f2.head.id, _read_json(os.path.join(td, "graph.json"))["head"]), ("e1.0", "e1.0"))
            f2.step("sign", demo.fake_step(), parent=e2.selected)
            self.assertEqual((f2.head.id, f2.verification()["head"]), ("sign.0", "sign.0"))

    def test_a_reselected_step_moves_the_result_and_retires_what_followed(self):
        from sipath.scheduler import run_chain

        def place(ctx):
            ctx.metrics["wns"] = float(ctx.params.get("d", 0))
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            a = f.step("floorplan", demo.fake_step())
            b = f.step("place", place, parent=a.selected, variants=[{"d": 0}], score="setup_wns")
            c = f.step("cts", demo.fake_step(), parent=b.selected)
            r = f.extend("place", place, [{"d": 5}])  # an adaptive round after cts ran
            self.assertEqual((r.selected.id, f.head.id), ("place.1", "place.1"))
            self.assertEqual([x["id"] for x in run_chain(f.graph())], ["floorplan.0", "place.1"])
            with self.assertRaisesRegex(ValueError, "continues from place.0, which is rejected"):
                f.step("route", demo.fake_step(), parent=c.selected)

    def test_a_lost_selected_checkpoint_keeps_ids_unique_and_the_graph_readable(self):
        def chain(f):
            a = f.step("floorplan", demo.fake_step())
            b = f.step("place", demo.fake_step(), parent=a.selected, variants=[{"k": 1}, {"k": 2}])
            return b, f.step("cts", demo.fake_step(), parent=b.selected)
        with tempfile.TemporaryDirectory() as td:
            b, c = chain(self.flow(td))
            os.remove(b.selected.checkpoint)
            f2 = self.flow(td, resume=True)
            self.assertEqual(f2.head.id, "floorplan.0")  # what continued from the lost one is not the result
            b2, c2 = chain(f2)
            self.assertFalse(c2.resumed)  # cts ran again: its parent is a new attempt
            ids = [a["id"] for a in _read_json(os.path.join(td, "graph.json"))["attempts"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertNotIn(b2.selected.id, (b.selected.id,))
            g3 = self.flow(td, resume=True)  # the graph it wrote loads
            self.assertEqual(g3.head.id, c2.selected.id)

    def test_an_imported_steps_code_is_in_its_fingerprint(self):
        from sipath import designfile, params, steps
        with tempfile.TemporaryDirectory() as td:
            mod = os.path.join(td, "mods")
            os.makedirs(mod)
            src = os.path.join(mod, "review_step_mod.py")
            with open(src, "w") as fh:
                fh.write("def mystep(ctx):\n    ctx.metrics['wns'] = 1.0\n")
            sys.path.insert(0, mod)
            self.addCleanup(sys.path.remove, mod)
            self.addCleanup(lambda: (steps.STEPS.pop("review_step_mod:mystep", None),
                                     params.STEP_PARAMS.pop("review_step_mod:mystep", None),
                                     params.DECLARED.pop("review_step_mod:mystep", None),
                                     sys.modules.pop("review_step_mod", None)))
            run = os.path.join(td, "run")
            items = lambda: designfile.resolve_steps([{"step": "review_step_mod:mystep"}])  # noqa: E731
            designfile.run_items(self.flow(run), items())
            f2 = self.flow(run, resume=True)
            designfile.run_items(f2, items())
            self.assertEqual(f2.stale, [])  # unchanged: reused
            with open(src, "a") as fh:
                fh.write("    ctx.metrics['wns'] = 2.0\n")
            f3 = self.flow(run, resume=True)
            designfile.run_items(f3, items())
            self.assertEqual([s["step"] for s in f3.stale], ["mystep"])

    def test_two_functions_of_one_module_are_two_steps(self):
        from sipath import designfile, params, steps
        with tempfile.TemporaryDirectory() as td:
            mod = os.path.join(td, "mods")
            os.makedirs(mod)
            with open(os.path.join(mod, "review_two_mod.py"), "w") as fh:
                fh.write("def one(ctx):\n    ctx.metrics['wns'] = 1.0\n\n\ndef two(ctx):\n    ctx.metrics['wns'] = 2.0\n")
            sys.path.insert(0, mod)
            self.addCleanup(sys.path.remove, mod)
            for ref in ("review_two_mod:one", "review_two_mod:two"):
                self.addCleanup(lambda r=ref: (steps.STEPS.pop(r, None), params.STEP_PARAMS.pop(r, None),
                                               params.DECLARED.pop(r, None)))
            self.addCleanup(sys.modules.pop, "review_two_mod", None)
            run = os.path.join(td, "run")
            designfile.run_items(self.flow(run), designfile.resolve_steps([{"step": "x", "fn": "review_two_mod:one"}]))
            f2 = self.flow(run, resume=True)  # the same name and parameters, the module unchanged: another function
            designfile.run_items(f2, designfile.resolve_steps([{"step": "x", "fn": "review_two_mod:two"}]))
            self.assertEqual([s["step"] for s in f2.stale], ["x"])
            self.assertEqual(f2.steps["x"].selected.metrics["wns"], 2.0)

    def test_a_step_registered_under_another_name_gets_its_declared_defaults(self):
        from sipath import params, registry, steps
        seen = {}

        def my_fill_impl(ctx):
            seen.update(ctx.params)
            ctx.metrics["wns"] = 0.0
        registry.register_step("review_myfill", my_fill_impl, params=[params.Param("density", "float", 0.42, "target")])
        self.addCleanup(lambda: (steps.STEPS.pop("review_myfill"), params.STEP_PARAMS.pop("review_myfill"),
                                 params.DECLARED.pop("review_myfill")))
        with tempfile.TemporaryDirectory() as td:
            self.flow(td).step("review_myfill", registry.STEPS["review_myfill"])
        self.assertEqual(seen.get("density"), 0.42)

    def test_a_budget_stops_the_attempts_later_processes_too(self):
        import time as _t
        from sipath.tools import process
        log = []

        def step(ctx):  # several tool processes one after another, each failure caught (a signoff job does)
            for _ in range(3):
                try:
                    process.run(["sleep", "1.5"], tracker=ctx.processes)
                    log.append("ran")
                except process.ProcessCancelled:
                    log.append("cancelled")
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td, step_budget_s=0.5)
            t0 = _t.time()
            with self.assertRaises(StepFailed):  # no attempt finished inside the time box
                f.step("s", step)
            self.assertLess(_t.time() - t0, 1.4)
        self.assertEqual(log, ["cancelled"] * 3)
        self.assertEqual(f.attempts[0].status, "budget")


class CliExitCodes(unittest.TestCase):
    """Project review, batch 1: a run's exit status says what it established, not only that it ended."""

    place, route = staticmethod(RunHead.place), staticmethod(RunHead.route)

    def test_exit_code_of_a_verdict(self):
        from sipath.verdict import exit_code
        for execution, verification, want in (("completed", "passed", 0), ("completed", "omitted", 0),
                                              ("completed", "completed", 0), ("completed", "failed", 1),
                                              ("completed", "inconclusive", 3), ("failed", "passed", 1),
                                              ("budget", "passed", 3), ("cancelled", "omitted", 3),
                                              ("empty", "completed", 1)):
            self.assertEqual(exit_code({"execution": execution, "verification": verification}), want,
                             (execution, verification))

    def test_run_exits_nonzero_on_a_failed_verdict_and_on_a_stop(self):
        import contextlib
        import io
        from unittest import mock
        from sipath import __main__ as cli, designfile
        from sipath.scheduler import StepFailed
        with tempfile.TemporaryDirectory() as td:
            f = RunHead.flow(self, td)
            designfile.run_items(f, RunHead.items(self))
            v = dict(f.verification(), verification="failed", checks={"setup": {"status": "failed", "required": True}})
            f.verification = lambda: v
            out = io.StringIO()
            with mock.patch.object(designfile, "run", lambda *a, **k: f), contextlib.redirect_stdout(out):
                rc = cli.main(["run", "gcd8", td, "--resume"])
            self.assertEqual(rc, 1)
            self.assertIn("verification FAILED", out.getvalue())

            def stops(*a, **k):
                raise StepFailed("step route: no successful attempt")
            out = io.StringIO()
            with mock.patch.object(designfile, "run", stops), contextlib.redirect_stdout(out):
                rc = cli.main(["run", "gcd8", td, "--resume"])
            self.assertEqual(rc, 1)
            self.assertIn("StepFailed: step route", out.getvalue())


class SourceCorrectness(unittest.TestCase):
    """Project review, batch 1: smaller correctness defects in parsing, units and checks."""

    def test_sdc_options_and_commented_clocks(self):
        from sipath import sdc, synth
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.sdc")
            with open(p, "w") as fh:
                fh.write("# create_clock -period 2 [get_ports old]\n"
                         "create_clock -name c -period 10 [get_ports -quiet clk]\n")
            self.assertEqual(sdc.clock_ports(p), ["clk"])
            self.assertEqual(synth.clock_period_ps(p), 10000.0)

    def test_offset_buses_keep_their_indices(self):
        from sipath.netlist import parse_verilog
        src = ("module t(a, y);\ninput [8:1] a;\noutput [3:0] y;\nwire [2:1] w;\nassign y = a[4:1];\n"
               "sky130_fd_sc_hd__buf_1 b (.A(a), .X(w));\nendmodule\n")
        mods = parse_verilog(src)
        m = mods[0] if isinstance(mods, list) else next(iter(mods.values())) if isinstance(mods, dict) else mods
        self.assertEqual(m.ports[0].bits(), [f"a[{i}]" for i in range(1, 9)])
        self.assertEqual(m.wires, ["w[1]", "w[2]"])
        self.assertIn("input [8:1] a;", m.to_verilog() if hasattr(m, "to_verilog") else str(m))

    def test_eco_ops_refuse_the_pdks_dont_use_cells(self):
        from sipath import eco
        pdk = sky130hd("/p")
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        with self.assertRaisesRegex(ValueError, "dont_use"):
            eco.check_ops([{"op": "resize", "inst": "u1", "master": "sky130_fd_sc_hd__lpflow_inputiso0p_1"}], pdk, d)
        eco.check_ops([{"op": "resize", "inst": "u1", "master": "sky130_fd_sc_hd__buf_4"}], pdk, d)

    def test_micron_boxes_round_to_the_grid(self):
        from sipath.steps import structure
        self.assertEqual(int(round(1.005 * 1000)), 1005)
        with open(structure.__file__) as fh:
            src = fh.read()
        self.assertNotRegex(src, r"\bint\([^()]*\* dbu\)")  # truncation lost a site at exact boundaries

    def test_macro_supplies_connect_literally_and_by_ground_kind(self):
        from sipath import tcl
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def",
                   macros=[Macro("bank[0].u_ram", "ram", "ram.lef", supplies={"vccd1": "VDD", "vssd1": "VSS"})],
                   supply_names={"VDD": "VPWR", "VSS": "VGND"})
        out = tcl.global_connect(d, sky130hd("/p"))
        from sipath.tclquote import tcl_words
        line = next(ln for ln in out.splitlines() if "vccd1" in ln)
        self.assertEqual(tcl_words(line)[4], r"^bank\[0\]\.u_ram$")  # what Tcl's regexp gets: the name literally
        self.assertRegex(out, r"-net VGND .*-pin_pattern vssd1 -ground")
        self.assertRegex(out, r"-net VPWR .*-pin_pattern vccd1 -power")
        self.assertTrue(d.is_ground("VGND") and not d.is_ground("VPWR"))

    def test_limits_do_not_fall_back_to_the_routers_drc(self):
        from sipath import limits
        with tempfile.TemporaryDirectory() as td:
            so = os.path.join(td, "signoff.json")
            _write_json(so, {"drc": {"run": False}, "timing": {"corners": {}}, "pass": False})
            _write_json(os.path.join(td, "graph.json"), {"head": "signoff.0", "attempts": [
                {"id": "signoff.0", "step": "signoff", "parent": None, "status": "selected", "started_at": 1,
                 "finished_at": 2, "metrics": {"drc": 0}, "outputs": {"signoff": so}}],
                "steps": {"signoff": {"selected": "signoff.0"}}})
            self.assertIsNone(limits.run_metrics(td, "signoff")["drc"])

    def test_lvs_full_reports_why_without_a_verdict(self):
        with open(os.path.join(os.path.dirname(eco_module_path()), "lvs_full.py")) as fh:
            src = fh.read()
        self.assertNotIn("p.returncode", src)  # process.Result has rc: the old name raised AttributeError


def _write_text(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def _read_text(path):
    with open(path) as fh:
        return fh.read()


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def eco_module_path():
    from sipath import eco
    return eco.__file__


class WorkdirSafety(unittest.TestCase):
    """Project review, batch 3: one run writes a workdir at a time; a mistyped option never overwrites a run."""

    def flow(self, td, resume=False):
        return Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                    demo.fake_factory(), validate_inputs=False, resume=resume)

    def test_another_process_holding_the_workdir_is_refused(self):
        import subprocess
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            code = ("import sys, time; from sipath.tests.test_unit import WorkdirSafety as W; "
                    "f = W.flow(None, sys.argv[1]); print('held', flush=True); time.sleep(30)")
            p = subprocess.Popen([sys.executable, "-c", code, td], stdout=subprocess.PIPE, text=True,
                                 cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            try:
                self.assertEqual(p.stdout.readline().strip(), "held")
                with self.assertRaisesRegex(RuntimeError, "in use by another process"):
                    self.flow(td)
            finally:
                p.kill()
                p.wait()
                p.stdout.close()
            _t.sleep(0.1)
            self.flow(td)  # free again once it exited

    def test_a_newer_flow_in_this_process_takes_over_and_the_older_stops_writing(self):
        with tempfile.TemporaryDirectory() as td:
            old = self.flow(td)
            old.step("a", demo.fake_step())
            new = self.flow(td, resume=True)
            ran = []
            for call in (lambda: old.step("b", lambda ctx: ran.append(1), parent=old.steps["a"].selected),
                         lambda: old.extend("a", lambda ctx: ran.append(1), [{"k": 9}]),
                         lambda: old.keep("a", 2),
                         lambda: old.fork(old.steps["a"].selected.id, os.path.join(td, "fork"))):
                with self.assertRaisesRegex(RuntimeError, "opened again"):
                    call()
            self.assertEqual(ran, [])  # refused before any step function ran
            new.step("b", demo.fake_step(), parent=new.steps["a"].selected)
            self.assertIn("b", _read_json(os.path.join(td, "graph.json"))["steps"])

    def test_a_closed_flow_gives_the_workdir_up_and_refuses_work(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            f.step("a", demo.fake_step())
            f.close()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                f.step("b", demo.fake_step(), parent=f.steps["a"].selected)
            self.flow(td, resume=True).step("b", demo.fake_step())  # the workdir is free

    def test_the_cli_never_overwrites_a_recorded_run(self):
        import contextlib
        import io
        from sipath import __main__ as cli
        with tempfile.TemporaryDirectory() as td:
            _write_json(os.path.join(td, "graph.json"), {"attempts": []})
            err = io.StringIO()
            for argv in (["run", "gcd8", td], ["run", "gcd8", td, "--resum"], ["run", "gcd8", td, "--resume", "--fresh"]):
                with contextlib.redirect_stderr(err):
                    self.assertEqual(cli.main(argv), 2, argv)
            self.assertIn("--resume continues it, --fresh starts over", err.getvalue())
            self.assertIn("unknown option --resum", err.getvalue())

    def test_help_runs_nothing(self):
        import contextlib
        import io
        from unittest import mock
        from sipath import __main__ as cli
        out = io.StringIO()
        with mock.patch.object(cli, "install_pdk", side_effect=AssertionError("ran")), \
                mock.patch.object(cli, "setup_placer", side_effect=AssertionError("ran")), contextlib.redirect_stdout(out):
            for c in ("install-pdk", "setup-placer", "install-tools", "run"):
                self.assertEqual(cli.main([c, "--help"]), 0)
        self.assertIn("python3 -m sipath install-pdk", out.getvalue())
        self.assertNotIn("python3 -m sipath validate", out.getvalue().split("install-pdk", 1)[1][:300])


class McpSafety(unittest.TestCase):
    """Project review, batch 3: the MCP tools validate what they are given and say what they can do."""

    def test_query_checkpoint_is_not_advertised_as_read_only(self):
        from sipath import mcp_server
        q = next(t for t in mcp_server.TOOLS if t["name"] == "query_checkpoint")
        self.assertFalse(q["annotations"]["readOnlyHint"])
        self.assertIn("interp", mcp_server.QUERY_BLOCKED)

    def test_query_guard_refuses_pipes_and_child_interpreters(self):
        import shutil
        import subprocess
        from sipath import mcp_server
        if not shutil.which("tclsh"):
            self.skipTest("no tclsh")
        guard = "".join(f"catch {{rename {c} {{}}}}\n" for c in mcp_server.QUERY_BLOCKED) + mcp_server.QUERY_OPEN_GUARD
        for probe in ('open "|echo PIPE"', "interp create x"):
            r = subprocess.run(["tclsh"], input=guard + f"puts [catch {{{probe}}} m]\n", capture_output=True, text=True)
            self.assertEqual(r.stdout.strip().splitlines()[-1], "1", (probe, r.stdout, r.stderr))

    def test_the_agent_example_leaves_arbitrary_tcl_out(self):
        from sipath import mcp_server
        from sipath.examples import agent_sdk
        names = {f"mcp__sipath__{t['name']}" for t in mcp_server.TOOLS}
        default = agent_sdk.allowed_tools()
        self.assertNotIn("mcp__sipath__query_checkpoint", default)
        self.assertEqual(set(default), names - {"mcp__sipath__query_checkpoint"})  # by name, no wildcard
        self.assertFalse(any("*" in n for n in default))
        self.assertIn("mcp__sipath__query_checkpoint", agent_sdk.allowed_tools(allow_tcl=True))
        with open(agent_sdk.__file__) as fh:
            self.assertNotIn('"mcp__sipath__*"', fh.read())

    def test_tool_arguments_are_checked(self):
        from sipath.mcp_server import Server, ToolError
        s = Server()
        for bad in ("false", "yes", 1):
            with self.assertRaises(ToolError):
                s._bool(bad if bad != "false" else "maybe", "resume")
        self.assertFalse(s._bool("false", "resume"))
        with self.assertRaisesRegex(ToolError, "bad arguments"):
            s.call("list_designs", {"nope": 1})
        with tempfile.TemporaryDirectory() as td:
            _write_json(os.path.join(td, "graph.json"), {"attempts": []})
            with self.assertRaisesRegex(ToolError, "holds a run"):
                s.open_run("gcd8", td)


class UserSurface(unittest.TestCase):
    """Project review, batch 4: explore mode, plans and parameters say before a run what the run would refuse."""

    def test_explore_drops_the_check_steps(self):
        from sipath import designfile
        spec = [{"step": s} for s in ("synth", "floorplan", "place", "cts", "route_digital", "extract", "timing",
                                      "drc", "lvs", "precheck", "signoff")]
        self.assertEqual([i["step"] for i in designfile.explore_flow(spec)],
                         ["synth", "floorplan", "place", "cts", "route_digital"])

    def test_abc_sweep_names_are_checked(self):
        from sipath import params
        for ok in ("auto", "off", "delay", "delay_map, area_map", "speed"):
            params.check("synth", {"abc_sweep": ok})
        for bad in ("dealy", "delay_map,nope", ","):
            with self.assertRaises(ValueError):
                params.check("synth", {"abc_sweep": bad})

    def test_a_plan_names_missing_inputs_and_bad_parameters(self):
        import yaml
        from sipath import designfile
        ex = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
        with open(os.path.join(ex, "gcd8.yaml")) as fh:
            d = yaml.safe_load(fh)
        d["sdc"] = os.path.join(ex, d["sdc"])
        with tempfile.TemporaryDirectory() as td:
            d["rtl"] = [os.path.join(td, "nonexist.v")]
            d["flow"] = [{"step": "synth", "variants": [{"abc_sweep": "dealy"}]}, {"step": "floorplan"}]
            path = os.path.join(td, "d.yaml")
            with open(path, "w") as fh:
                yaml.safe_dump(d, fh)
            p = designfile.plan(path, td)
        self.assertTrue(any("abc_sweep" in x for x in p["problems"]), p["problems"])
        self.assertTrue(any("nonexist.v" in x for x in p["problems"]), p["problems"])
        self.assertIn("problem:", designfile.plan_text(p))

    def test_a_live_class_that_only_skipped_is_not_a_pass(self):
        from unittest import mock
        from sipath.tests import run_live
        fake = {"class": "LiveX", "rc": 0, "seconds": 1.0, "tests": 2, "skipped": 2, "durations": [], "output": ""}
        with mock.patch.object(run_live, "run_class", lambda n, env: dict(fake, **{"class": n})), \
                mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
            self.assertEqual(run_live.main(["-j", "1", "LiveX"]), 1)
            self.assertEqual(run_live.main(["-j", "1", "--allow-skips", "LiveX"]), 0)


class ToolStartup(unittest.TestCase):
    """Review: a tool that does not say its version is reported with its own diagnostic, not an IndexError."""

    def test_a_version_probe_reports_what_the_tool_said(self):
        import subprocess
        from unittest import mock
        from sipath.tools.openroad import session
        r = session.NativeRunner("/x/openroad")
        for proc, want in ((subprocess.CompletedProcess([], 0, "26Q3-1867\n", ""), "26Q3-1867"),):
            with mock.patch.object(session.subprocess, "run", return_value=proc):
                self.assertEqual(r.version(), want)
        for proc, said in ((subprocess.CompletedProcess([], 0, "", ""), "no output at all"),
                           (subprocess.CompletedProcess([], 127, "", "error while loading shared libraries: libX.so"),
                            "libX.so"),
                           (subprocess.CompletedProcess([], 1, "\n\n", "Segmentation fault"), "Segmentation fault")):
            with mock.patch.object(session.subprocess, "run", return_value=proc):
                with self.assertRaisesRegex(session.VersionError, said):
                    r.version()

    def test_validate_says_when_openroad_does_not_start(self):
        import contextlib
        import io
        from unittest import mock
        from sipath import __main__ as cli
        from sipath.tools.openroad import session
        runner = mock.Mock(kind="native", version=mock.Mock(side_effect=session.VersionError("openroad -version exited 127: libX.so")))
        err = io.StringIO()
        with mock.patch.object(session, "find_openroad", return_value=runner), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.validate(), 1)
        self.assertIn("does not start", err.getvalue())


class SignoffJudgment(unittest.TestCase):
    """Project review, batch 1: a check with nothing to judge is not a pass."""

    def test_one_unconstrained_endpoint_is_read(self):
        from sipath.signoff import check_setup_sections
        one = "Warning: There is 1 unconstrained endpoint.\n  out_q\nWarning: There are 2 combinational loops.\n"
        many = "Warning: There are 2 unconstrained endpoints.\n  a\n  b\n"
        self.assertEqual(check_setup_sections(one), (["out_q"], ["Warning: There are 2 combinational loops."]))
        self.assertEqual(check_setup_sections(many)[0], ["a", "b"])

    def test_no_constrained_path_fails_setup_and_hold(self):
        from sipath.signoff import sta_failures
        st = {"corners": {"tt": {"wns": None, "whs": None}, "ss": {"wns": None, "whs": None}}, "drv_clean": True,
              "unannotated_loaded": [], "unconstrained": ["out_q"]}
        f = sta_failures(st, ["setup", "hold", "drv", "annotation"])
        self.assertEqual([x.split(":")[0] for x in f], ["setup", "hold"])
        self.assertIn("no clock reaches an endpoint", f[0])
        self.assertEqual(sta_failures(st, ["drv"]), [])  # left out of the checks: not judged
        st["corners"]["ss"].update(wns=-0.2, whs=0.1)
        st["corners"]["tt"].update(wns=0.3, whs=0.1)
        self.assertEqual(sta_failures(st, ["setup", "hold"]), ["setup: worst -0.200 ns (['ss'])"])

    def test_offline_judgment_uses_the_reports_expected_corners_in_order(self):
        from sipath.signoff import sta_failures
        st = {"expected_corners": ["ss", "tt", "ff"],
              "corners": {"tt": {"wns": 1.0, "whs": 0.1},
                          "unexpected": {"wns": None, "whs": None}}}
        # Missing whole corners fail without an explicit expected argument;
        # an unrelated recorded corner is not required and PDK order survives.
        self.assertEqual(sta_failures(st, ["setup", "hold"]),
                         ["setup: no result at 2 of 3 corners (['ss', 'ff'])",
                          "hold: no result at 2 of 3 corners (['ss', 'ff'])"])
        self.assertEqual(sta_failures(st, ["setup", "hold"], expected=["tt"]), [])
        for corner in ("ss", "ff"):
            st["corners"][corner] = {"wns": 0.5, "whs": 0.2}
        self.assertEqual(sta_failures(st, ["setup", "hold"]), [])
        del st["expected_corners"]  # legacy reports fall back to recorded corners
        self.assertEqual(sta_failures(st, ["setup"]),
                         ["setup: no result at 1 of 4 corners (['unexpected'])"])

    def test_each_expected_corner_needs_finite_setup_and_hold(self):
        from sipath.signoff import sta_failures
        st = {"expected_corners": ["tt", "ss"], "corners": {"tt": {"wns": 1.0, "whs": 0.1}}}
        self.assertEqual(len(sta_failures(st, ["setup", "hold"])), 2)
        for missing in (None, float("nan"), float("inf"), -float("inf")):
            st["corners"]["ss"] = {"wns": missing, "whs": 0.2}
            self.assertEqual(sta_failures(st, ["setup", "hold"]),
                             ["setup: no result at 1 of 2 corners (['ss'])"])
            self.assertEqual(sta_failures(st, ["hold"]), [])
            self.assertEqual(sta_failures(st, ["setup"], expected=["tt", "ss"]),
                             ["setup: no result at 1 of 2 corners (['ss'])"])
        st["corners"]["ss"]["wns"] = 0.5
        self.assertEqual(sta_failures(st, ["setup", "hold"]), [])

    def test_a_corner_without_a_result_fails_its_check(self):
        from sipath.signoff import sta_failures
        st = {"corners": {"tt": {"wns": 0.4, "whs": 0.1}, "ss": {"wns": None, "whs": 0.2}}, "drv_clean": True,
              "unannotated_loaded": [], "unconstrained": []}
        self.assertEqual(sta_failures(st, ["setup", "hold"]), ["setup: no result at 1 of 2 corners (['ss'])"])
        # a corner the PDK signs off at but the STA never reported is missing too
        self.assertEqual(sta_failures(st, ["hold"], expected=["tt", "ss", "ff"]),
                         ["hold: no result at 1 of 3 corners (['ff'])"])

    def test_timing_met_is_unknown_without_constrained_paths(self):
        from sipath.steps.common import timing_met
        self.assertIsNone(timing_met(None, None))
        self.assertTrue(timing_met(0.1, None))
        self.assertFalse(timing_met(0.1, -0.01))

    def test_a_failed_design_rule_report_is_not_clean(self):
        from unittest import mock
        from sipath.steps import common
        with tempfile.TemporaryDirectory() as td:
            ctx = types.SimpleNamespace(session=object(), pdk=types.SimpleNamespace(analysis_corners=lambda: []),
                                        outputs={}, metrics={}, path=lambda n: os.path.join(td, n))

            def to_file(s, f, cmd):
                ok = "check_types" not in cmd
                if ok:
                    open(f, "w").close()
                return types.SimpleNamespace(rc=0 if ok else 1, value="STA-1234 crashed")
            with mock.patch.object(common.report_ops, "to_file", to_file):
                common.reports(ctx, "cts")
        self.assertIsNone(ctx.metrics["drv_clean"])
        self.assertEqual(ctx.outputs["cts_reports_failed"], ["design_rules"])

    def test_the_final_slack_is_signoffs_not_the_heads_estimate(self):
        from sipath import report
        g = {"design": "x", "head": "signoff.0", "attempts": [
            {"id": "extract.0", "step": "extract", "parent": None, "metrics": {"wns": 4.3}},
            {"id": "signoff.0", "step": "signoff", "parent": "extract.0",
             "metrics": {"wns": 4.416, "whs": 0.3, "parasitics": "placement", "sta_wns_tt": 4.3, "sta_wns_ss": 4.249,
                         "sta_whs_tt": 0.2, "sta_whs_ff": 0.171}}]}
        m = report.metrics2(g)
        self.assertEqual((m["timing__setup__ws"], m["timing__hold__ws"]), (4.249, 0.171))

    def test_magic_must_have_loaded_the_cell_and_the_tech(self):
        from sipath.signoff import SignoffError, magic_cell_problem
        magic_cell_problem("SIPATH_MAGIC_CELL sky130A 1\n", "top", "log")
        for out, why in (("SIPATH_MAGIC_CELL minimum 1", "no PDK technology"), ("SIPATH_MAGIC_CELL sky130A 0", "no cell top"),
                         ("Magic crashed", "did not report")):
            with self.assertRaisesRegex(SignoffError, why):
                magic_cell_problem(out, "top", "log")


class ArchivesAndEligibility(unittest.TestCase):
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def setUp(self):
        sys.path.insert(0, os.path.join(self.ROOT, "bench"))

    def test_archives_are_addressed_by_their_content_and_never_rewritten(self):
        from unittest import mock
        import records
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "bench"))
            src = os.path.join(td, "bench", "new.py")
            idents = []

            def ident():
                from sipath.fingerprint import file_hash
                return {"commit": "abc-dirty", "untracked": {"bench/new.py": file_hash(src)}}
            with mock.patch.object(records, "ROOT", td), mock.patch.object(records, "RESULTS", os.path.join(td, "res")), \
                    mock.patch("sipath.designfile.sipath_identity", ident):
                for text in ("one\n", "two\n"):
                    with open(src, "w") as fh:
                        fh.write(text)
                    idents.append(records.archive_source())
            a, b = idents
            self.assertNotEqual(a["archive_key"], b["archive_key"])  # untracked content is in the key
            with open(os.path.join(td, a["archive"], "untracked", "bench", "new.py")) as fh:
                self.assertEqual(fh.read(), "one\n")  # the first archive is intact

    def test_only_completed_and_measured_runs_count(self):
        from unittest import mock
        import tables
        base = {"kind": "strategy", "design": "dp8", "flow": "defaults", "verification": {"verification": "passed"}}
        recs = [dict(base, outcome="completed", metrics={"exit": 0, "wns_tt_ns": 1.0, "wall_s": 10}),
                dict(base, outcome="measurement_failed", metrics={"exit": 0, "wall_s": 999}),
                dict(base, metrics={"exit": 0, "wns_tt_ns": 1.0, "wall_s": 20})]  # an older record: no outcome
        with mock.patch.object(tables, "_by", lambda kind: recs if kind == "strategy" else []):
            row = next(ln for ln in tables.table_strategies().splitlines() if "| dp8 | defaults |" in ln)
        self.assertIn("2 of 3 (measurement failed)", row)
        self.assertIn("passed 2/2", row)
        self.assertIn("| 15 |", row)  # the median wall time of the two that count, not 999


class BenchmarkIntegrity(unittest.TestCase):
    """Project review, batch 2: a table shows only what a run completed and measured, says which code ran it,
    and a record says what it could not take from the run."""
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def setUp(self):
        sys.path.insert(0, os.path.join(self.ROOT, "bench"))

    def rec(self, **kw):
        base = {"kind": "repeat", "design": "aes", "flow": "best_slack", "measured": "2026-10-20",
                "verification": {"execution": "completed", "verification": "passed", "failed": []},
                "sipath": {"commit": "abc1234def56"}, "metrics": {"exit": 0, "wns_tt_ns": 0.5, "wall_s": 100}}
        return {**base, **kw}

    def test_completed_needs_an_exit_status_a_verdict_and_a_measurement(self):
        import tables
        self.assertTrue(tables.completed(self.rec()))
        for bad in (self.rec(metrics={"wall_s": 7000}),  # no exit status recorded
                    self.rec(metrics={"exit": 0, "wall_s": 5000}),  # nothing measured
                    self.rec(verification={"execution": "failed"}),  # the run's own verdict: it failed
                    self.rec(kind="orfs_compare", flow="orfs", verification=None, metrics={"exit": 2, "wns_tt_ns": -9})):
            self.assertFalse(tables.completed(bad), bad)

    def test_a_failed_orfs_run_shows_why_not_numbers(self):
        from unittest import mock
        import tables
        orfs = self.rec(kind="orfs_compare", flow="orfs", design="gcd", verification=None, sipath=None,
                        metrics={"exit": 2, "wns_tt_ns": -9.0}, failure="exit 2")
        with mock.patch.object(tables, "_by", lambda kind: [orfs] if kind == "orfs_compare" else []):
            row = next(ln for ln in tables.table_orfs(False).splitlines() if "| ORFS |" in ln)
        self.assertNotIn("9.000", row)
        self.assertIn("0 of 1", row)

    def test_fallback_rows_say_when_their_runs_failed(self):
        from unittest import mock
        import tables
        fb = [self.rec(kind="fallback", design="alu32", flow=f, metrics={"wns_ss_max_ns": v}, failure="signoff.0: "
                       "SignoffError: signoff failed", verification={"execution": "failed"}) for f, v in
              (("knee", -1.99), ("best_slack", -1.21))]
        with mock.patch.object(tables, "_by", lambda kind: fb if kind == "fallback" else []):
            row = next(ln for ln in tables.table_fallback().splitlines() if ln.startswith("| alu32"))
        self.assertTrue(row.endswith("| failed: signoff / failed: signoff |"), row)

    def test_strategy_rows_name_their_code_and_partial_medians(self):
        from unittest import mock
        import tables
        base = {"kind": "strategy", "design": "dp8", "outcome": "completed", "verification": {"verification": "passed"}}
        recs = [dict(base, flow="defaults", sipath={"commit": "aaaaaaa11111"},
                     metrics={"exit": 0, "wns_tt_ns": 1.0, "core_seconds": 100.0, "drc_klayout": 0}),
                dict(base, flow="search", repeat=1, sipath={"commit": "bbbbbbb22222-dirty"},
                     metrics={"exit": 0, "wns_tt_ns": 1.0, "core_seconds": 150.0, "drc_klayout": 0}),
                dict(base, flow="search", repeat=2, sipath={"commit": "bbbbbbb22222-dirty"},
                     metrics={"exit": 0, "wns_tt_ns": 1.0, "core_seconds": 150.0})]
        with mock.patch.object(tables, "_by", lambda kind: recs if kind == "strategy" else []):
            row = next(ln for ln in tables.table_strategies().splitlines() if "| dp8 | search |" in ln)
        self.assertIn("| 1.50† |", row)  # against defaults that ran other code
        self.assertTrue(row.endswith("| bbbbbbb* |"), row)
        self.assertIn("| 0 (1 of 2) |", row)  # DRC from one of the row's two runs

    def test_orfs_compare_records_carry_their_outcome(self):
        import records
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "gcd")
            os.makedirs(os.path.join(d, "orfs"))
            _write_json(os.path.join(d, "compare.json"), {
                "config": {"rtl": [], "sdc": None, "orfs_unmapped": {"TNS_END_PERCENT": "100"}},
                "orfs_image": "openroad/orfs@sha256:" + "0" * 64,
                "orfs": {"exit": 0, "seconds": 9, "measured": {"error": "measurement failed (log)"}}})
            r, = records.from_orfs_compare(d)
        self.assertEqual((r["outcome"], r["failure"]), ("measurement_failed", "measurement failed (log)"))
        self.assertEqual(r["orfs_unmapped"], {"TNS_END_PERCENT": "100"})
        self.assertEqual(r["toolchain"]["orfs"], "openroad/orfs@sha256:" + "0" * 64)
        self.assertNotIn("toolchain: the lock at export, not recorded by the run", r.get("provenance_gaps", []))
        self.assertEqual(records._outcome(None, None), ("execution_failed", "no exit status recorded"))

    def test_a_run_that_failed_only_verification_is_measured(self):
        """`sipath run` exits 1 when a required check fails; the benchmarks judge the run by its own execution
        status, keep measuring it and keep its verdict apart."""
        import records
        self.assertEqual(records._outcome(1, None, "completed"), ("completed", None))
        self.assertEqual(records._outcome(1, "no SPEF", "completed"), ("measurement_failed", "no SPEF"))
        self.assertEqual(records._outcome(1, None, "failed")[0], "execution_failed")
        self.assertEqual(records._outcome(0, None, "budget")[0], "execution_failed")
        with open(os.path.join(self.ROOT, "bench", "strategy_compare.py")) as fh:
            src = fh.read()
        self.assertNotIn('"completed" if not rc', src)  # the exit code no longer decides
        self.assertIn('if execution == "completed":', src)

    def test_records_list_what_the_run_did_not_record(self):
        import records
        r = records.make("repeat", "aes", "knee", {"exit": 0}, run={"sipath": {"commit": None}}, inputs_={})
        gaps = r["provenance_gaps"]
        for want in ("toolchain:", "machine.image_platform:", "measured:", "sipath.commit:", "inputs: unknown"):
            self.assertTrue(any(g.startswith(want) for g in gaps), (want, gaps))
        self.assertNotIn("image_platform", r["machine"])  # not defaulted to linux/amd64

    def test_a_measurement_never_reads_an_earlier_ones_files(self):
        from unittest import mock
        import orfs_compare as oc
        with tempfile.TemporaryDirectory() as td:
            for n in ("x.odb", "x.spef", "x.gds"):
                open(os.path.join(td, n), "w").close()
            with open(os.path.join(td, "drc_t.lyrdb"), "w") as fh:  # an earlier measurement's report
                fh.write("<report-database/>")
            with open(os.path.join(td, "final_t.def"), "w") as fh:
                fh.write("stale")

            def docker(args, mounts, log=None):
                if log:
                    open(log, "w").close()  # the tools ran but wrote nothing
                return 0
            r = {"odb": os.path.join(td, "x.odb"), "spef": os.path.join(td, "x.spef"), "gds": os.path.join(td, "x.gds")}
            with mock.patch.object(oc, "docker", docker):
                out = oc.measure(r, {"sdc": os.path.join(td, "x.sdc"), "top": "t", "drc_deck": "deck.drc"}, "lib", "t", td)
        self.assertIn("error", out)
        self.assertNotIn("klayout_drc", out)
        self.assertIsNone(out["wirelength_um"])

    def test_an_orfs_run_starts_from_an_empty_work_directory(self):
        from unittest import mock
        import orfs_compare as oc
        with tempfile.TemporaryDirectory() as td:
            old = os.path.join(td, "orfs", "results", "sky130hd", "gcd", "base", "6_final.odb")
            os.makedirs(os.path.dirname(old))
            open(old, "w").close()
            with mock.patch.object(oc, "docker", lambda *a, **k: 2):
                r = oc.run_orfs(td, "gcd")
            self.assertFalse(os.path.exists(old))
            self.assertEqual(r["exit"], 2)

    def test_the_source_identity_leaves_out_results_and_prose(self):
        import subprocess
        from sipath.designfile import source_diff
        with tempfile.TemporaryDirectory() as td:
            def git(*a):
                subprocess.run(["git", "-C", td, *a], check=True, capture_output=True)
            git("init", "-q")
            for rel in ("bench/results/r.json", "README.md", "sipath/x.py"):
                os.makedirs(os.path.dirname(os.path.join(td, rel)) or td, exist_ok=True)
                with open(os.path.join(td, rel), "w") as fh:
                    fh.write("a\n")
            git("add", "-A")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
            for rel in ("bench/results/r.json", "README.md"):
                with open(os.path.join(td, rel), "w") as fh:
                    fh.write("b\n")
            self.assertEqual(source_diff(td), b"")
            with open(os.path.join(td, "sipath", "x.py"), "w") as fh:
                fh.write("b\n")
            self.assertIn(b"sipath/x.py", source_diff(td))


class PlanParity(unittest.TestCase):
    """The plan resolves parameters as execution does: inherited threads and step aliases."""

    def test_plan_and_execution_agree(self):
        import yaml
        from sipath import designfile
        from sipath.config import Config
        with tempfile.TemporaryDirectory() as td:
            ex = os.path.join(os.path.dirname(__file__), "..", "examples")
            with open(os.path.join(ex, "gcd8.yaml")) as fh:
                d = yaml.safe_load(fh)
            for k in ("sdc",):
                d[k] = os.path.abspath(os.path.join(ex, d[k]))
            d["rtl"] = [os.path.abspath(os.path.join(ex, r)) for r in d["rtl"]]
            d["step_defaults"] = {"place": {"threads": 4}}
            d["flow"] = [{"step": "synth"}, {"step": "floorplan"}, {"step": "physical"},
                         {"step": "spread", "fn": "place", "variants": [{}, {"density": 0.6}, {"density": 0.7}],
                          "parallel": 3}]
            path = os.path.join(td, "d.yaml")
            with open(path, "w") as fh:
                yaml.safe_dump(d, fh)
            p = designfile.plan(path, config=Config(threads=6))
            row = next(r for r in p["steps"] if r["step"] == "spread")
            self.assertEqual((row["at_once"], row["threads_each"]), (1, 4))  # as admission will: 4 inherited of 6
            f = RunHead.flow(self, os.path.join(td, "run"), threads=6)
            f.design.step_defaults = {"place": {"threads": 4}}

            def place(ctx):
                ctx.metrics["wns"] = 1.0
            place.__name__ = "place"
            f.step("spread", place, variants=[{}, {"density": 0.6}, {"density": 0.7}], parallel=3, score="setup_wns")
            self.assertEqual((f.allocation["spread"]["at_once"], f.allocation["spread"]["threads_each"]), (1, 4))


class Adaptive(unittest.TestCase):
    """Adaptive rounds: Flow.extend, designfile.adapt, the place adapter and its blockages."""

    def flow(self, td):
        d = Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def")
        return Flow(d, sky130hd("/p"), td, demo.fake_factory(), validate_inputs=False)

    CALLS: list = []

    @staticmethod
    def step(ctx):  # density lower is better here; blockages better still
        Adaptive.CALLS.append(ctx.params.get("density"))
        ctx.metrics["wns"] = 1.0 - ctx.params.get("density", 0.8) + 0.5 * len(ctx.params.get("blockages") or [])

    def test_extend_reselects_records_and_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            run = f.step("place", self.step, variants=[{"density": 0.8}], score="setup_wns")
            self.assertEqual(run.selected.id, "place.0")
            run = f.extend("place", self.step, [{"density": 0.7}, {"density": 0.8, "blockages": [{"box": [0, 0, 1, 1]}]}],
                           [{"from": "place.0", "reason": "hot"}, {"from": "place.0", "reason": "hotter"}])
            self.assertEqual(run.selected.id, "place.2")
            self.assertEqual([a.status for a in run.attempts], ["rejected", "rejected", "selected"])
            self.assertEqual((run.selected.adapted_from, run.selected.adapt_reason), ("place.0", "hotter"))
            self.assertEqual(run.rounds, [{"from": "place.0", "attempts": ["place.1", "place.2"], "selected": "place.2"}])
            f.stop_rounds("place", "nothing hot")
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual(g["steps"]["place"]["rounds"][-1]["stopped"], "nothing hot")
            self.assertEqual(g["steps"]["place"]["variants"], [{"density": 0.8}])  # the base variants
            Adaptive.CALLS.clear()  # the same step function on resume (another function would run again)
            f2 = Flow(f.design, f.pdk, td, demo.fake_factory(), validate_inputs=False, resume=True)
            r2 = f2.step("place", self.step, variants=[{"density": 0.8}], score="setup_wns")
            self.assertEqual((r2.selected.id, len(r2.rounds), Adaptive.CALLS), ("place.2", 2, []))  # recorded
            r3 = f2.extend("place", self.step, [{"density": 0.7}])
            self.assertEqual((Adaptive.CALLS, r3.rounds[-1]["attempts"]), ([], []))  # tried before: not run again
            with self.assertRaisesRegex(ValueError, "nothing to extend"):
                f2.extend("cts", self.step, [{}])

    def test_design_file_rounds_stop_with_a_reason(self):
        from sipath import designfile, registry
        calls = []

        def propose(flow, a, cfg):
            calls.append(a.id)
            d = a.params.get("density", 0.8)
            return [({**a.params, "density": round(d - 0.1, 2)}, f"lower than {d}")] if d > 0.55 else []
        propose.options = {"step": 0.1}
        old = registry.adapter("place")
        registry.ADAPTERS["place"] = propose
        try:
            with tempfile.TemporaryDirectory() as td:
                f = self.flow(td)
                f.step("place", self.step, variants=[{"density": 0.8}], score="setup_wns")
                run = designfile.adapt(f, "place", "place", self.step, {"rounds": 5})
                self.assertEqual([a.params["density"] for a in run.attempts], [0.8, 0.7, 0.6, 0.5])
                self.assertEqual(run.selected.params["density"], 0.5)
                self.assertEqual(run.rounds[-1]["stopped"], "nothing hot to answer")
                self.assertEqual(len(calls), 4)
                again = designfile.adapt(f, "place", "place", self.step, {"rounds": 6}, start=len(run.rounds))
                self.assertEqual(again.rounds[-1]["stopped"], "nothing hot to answer")  # a later call: rounds again
                self.assertEqual(len(calls), 5)
                with self.assertRaisesRegex(ValueError, "unknown option"):
                    designfile.adapt(f, "place", "place", self.step, {"rounds": 1, "bogus": 1})
                with self.assertRaisesRegex(ValueError, "no adapter"):
                    designfile.adapt(f, "place", "nope", self.step, {"rounds": 1})
                with self.assertRaisesRegex(ValueError, "rounds must be"):
                    designfile.adapt(f, "place", "place", self.step, {"rounds": 0})
        finally:
            registry.ADAPTERS["place"] = old

    def test_place_adapter_turns_hotspots_into_blockages(self):
        from sipath.steps.adapt import adapt_place
        hot = {"placement": {"hotspots": [{"box": [10, 0, 30, 20], "peak": 97.5}, {"box": [0, 20, 10, 30], "peak": 91}]},
               "rudy": {"hotspots": [{"box": [12, 2, 28, 18], "peak": 120}]}}  # inside the first: not repeated
        a = Attempt("place.0", "place", 0, {"density": 0.8}, None, "/x", status="selected")
        a.outputs, a.metrics = {"heatmaps": hot}, {"std_util": 0.5}
        f = types.SimpleNamespace(pdk=sky130hd("/p"), design=Design("fake", "fake", "x.v", "x.sdc", "macro"))
        props = adapt_place(f, a, {})
        local, glob_ = props
        self.assertEqual([b["box"] for b in local[0]["blockages"]], [[12, 2, 28, 18], [0, 20, 10, 30]])
        self.assertEqual(local[0]["blockages"][0]["max_density"], 70)
        self.assertIn("rudy 120", local[1])
        self.assertEqual(glob_[0], {"density": 0.75})
        a.metrics = {"std_util": 0.754}  # global placement cannot go below the cells' utilization
        self.assertEqual(len(adapt_place(f, a, {})), 1)
        a.outputs = {"heatmaps": {"placement": {"hotspots": []}}}
        self.assertEqual(adapt_place(f, a, {}), [])

    def test_blockages_are_checked_and_scores_rank_hotspots(self):
        from sipath import scores
        from sipath.steps.place import placement_blockages
        s = FakeSession(".")
        self.assertEqual(placement_blockages(s, [{"box": [1, 2, 3, 4], "max_density": 60}]), 1)
        self.assertTrue(any("create_blockage -region {1 2 3 4} -max_density 60" in x for x in s.scripts))
        self.assertTrue(any("flow_place_blockages" in x for x in s.scripts[-1:]))  # remembered, to remove them
        for bad in ([{"box": [3, 2, 1, 4]}], [{"box": [1, 2, 3]}], [{"box": [1, 2, 3, 4], "max_density": 100}]):
            with self.assertRaises(ValueError):
                placement_blockages(s, bad)
        sc = scores.get("hotspots_then_wns")
        cool = sc({"wns": 0.5, "grt_overflow": 0, "heatmap_placement_hot_share": 0.08, "heatmap_rudy_hot_share": 0.0})
        hot = sc({"wns": 0.9, "grt_overflow": 0, "heatmap_placement_hot_share": 0.2, "heatmap_rudy_hot_share": 0.0})
        self.assertGreater(cool, hot)  # fewer hot bins wins over slack
        self.assertIsNone(sc({"wns": 0.5}))  # no heatmaps: no score
        self.assertIsNone(sc({"wns": 0.5, "heatmap_placement_hot_share": 0.08}))  # no overflow: not the best, no score


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

    def test_eco_compared_with_like_parasitics(self):
        """A signoff head records the placement estimate; the ECO measures extracted parasitics: it is judged
        against the nearest attempt measured the same way (extract), not against the estimate."""
        def step(text, par):
            def fn(ctx):
                ctx.session.responses["::flow_metrics"] = text
                ctx.metrics["parasitics"] = par
            return fn
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            ex = f.step("extract", step("drc 0 wns 4.34 whs 0.11 hpwl_um 100", "rcx"), score="auto")
            so = f.step("signoff", step("drc 0 wns 4.47 whs 0.12 hpwl_um 100", "placement"), parent=ex.selected,
                        score="auto")
            r = f.eco("eco1", so.selected, [], fn=step("drc 0 wns 4.339 whs 0.11 hpwl_um 100", "rcx"))
            self.assertEqual(r.selected.status, "selected")  # 0.001 ns from extract, not 0.13 from the estimate
            self.assertEqual(r.selected.outputs["eco_baseline"], {"attempt": "extract.0", "parasitics": "rcx",
                                                                  "same_parasitics": True})
            with self.assertRaisesRegex(StepFailed, "vs eco1.0 4.339 ns .*rcx parasitics on both sides"):
                f.eco("eco2", r.selected, [], fn=step("drc 0 wns 4.2 whs 0.11 hpwl_um 100", "rcx"))

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

    def test_retry_with_runs_one_fresh_attempt(self):
        from sipath.scheduler import RetryWith, StepFailed
        calls = []

        def fragile(ctx):  # an incremental attempt that asks for a full one
            calls.append(dict(ctx.params))
            if not ctx.params.get("full"):
                raise RetryWith({"full": True}, "patch lost nets")
            ctx.metrics["wns"] = 0.5
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            run = f.step("patch", fragile, variants=[{"k": 1}], score="setup_wns")
            self.assertEqual([a.status for a in run.attempts], ["failed", "selected"])
            first, retry = run.attempts
            self.assertIn("patch lost nets", first.error)
            self.assertEqual((retry.params, retry.retry_of, retry.index), ({"k": 1, "full": True}, first.id, 1))
            self.assertEqual(len(calls), 2)
            g = _read_json(os.path.join(td, "graph.json"))
            self.assertEqual([a.get("retry_of") for a in g["attempts"] if a["step"] == "patch"], [None, first.id])
            self.assertEqual([a.get("retry_reason") for a in g["attempts"] if a["step"] == "patch"],
                             [None, "patch lost nets"])  # the retry says why it ran

        def always(ctx):  # a retry that asks again is not retried
            raise RetryWith({"full": True}, "still lost")
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            with self.assertRaises(StepFailed):
                f.step("patch", always, score="setup_wns")
            self.assertEqual(len([a for a in f.attempts if a.step == "patch"]), 2)

    def test_repair_on_a_failing_global_router_retries_without_repair(self):
        from sipath import eco
        from sipath.scheduler import RetryWith
        from sipath.session import TclError

        class Session:
            def __init__(self):
                self.scripts = []

            def run(self, script, check=True, timeout=None):
                self.scripts.append(script)
                if script.startswith("repair_timing"):  # aes: a resized pin leaves its net's guides
                    raise TclError(script, "GRT-0229", "", "[ERROR GRT-0229] Vertical edge usage exceeds ...")
        with tempfile.TemporaryDirectory() as td:
            ctx = types.SimpleNamespace(design=types.SimpleNamespace(analog_nets=[], analog_supplies=[], groups=[]),
                                        pdk=None, path=lambda n: os.path.join(td, n))
            with unittest.mock.patch.object(eco, "_slacks", lambda s: (-1.0, 0.1)), \
                    unittest.mock.patch.object(eco, "_locations", lambda s: {}), \
                    unittest.mock.patch.object(eco, "_masters", lambda s: {}), \
                    unittest.mock.patch.object(eco, "_wire_signature", lambda s, p: ""), \
                    unittest.mock.patch.object(eco, "patch_limit", lambda s, m: 40), \
                    unittest.mock.patch.object(eco.wires, "normalize", lambda s, p: None), \
                    unittest.mock.patch.object(eco.eco_ops.tcl, "margins", lambda *a: ""):
                ctx.session = Session()
                with self.assertRaises(RetryWith) as r:
                    eco.post_route_repair(ctx)
                self.assertEqual(r.exception.params, {"rounds": 0})
                ctx.session = Session()  # the retry: no repair_timing at all, and it says so
                out = eco.post_route_repair(ctx, max_iter=0)
                self.assertFalse(any(x.startswith("repair_timing") for x in ctx.session.scripts))
                self.assertEqual(out["rounds"][0]["note"], "post-route timing repair off (rounds 0)")

    def test_every_step_leaves_a_run_report(self):
        with tempfile.TemporaryDirectory() as td:
            f = self.flow(td)
            a = f.step("place", demo.fake_step(), variants=[{"d": 1}, {"d": 2}], score="setup_wns")
            try:
                f.step("cts", demo.fake_step(fail_if=lambda p: True), parent=a.selected)
            except StepFailed:
                pass
            with open(os.path.join(td, "report.md")) as fh:
                md = fh.read()
            self.assertIn("| place | 2/2 | place.", md)
            self.assertIn("| cts | 0/1 | none |", md)
            self.assertIn("## Failures", md)
            self.assertIn("**cts.0**", md)
            with open(os.path.join(td, "report.html")) as fh:
                self.assertIn("<table>", fh.read())

    def test_threads_per_step_and_shared_among_parallel_attempts(self):
        from sipath.config import Config
        with tempfile.TemporaryDirectory() as td:
            sessions = []
            def factory(d):
                s = FakeSession(d, responses={"::flow_timing_prep": "placement"})
                sessions.append(s)
                return s
            f = Flow(Design("fake", "fake", "x.v", "x.sdc", "macro", floorplan_def="x.def"), sky130hd("/p"), td,
                     factory, validate_inputs=False, config=Config(threads=12))
            seen = {}
            def place(ctx):
                demo.fake_step(delay=0)(ctx)
                seen[ctx.attempt.id] = ctx.threads
            set_count = lambda s: [x for x in s.scripts if x.startswith("set_thread_count")]  # noqa: E731
            f.step("place", place, score="setup_wns")  # one attempt: the run's threads, nothing to change
            self.assertEqual(seen["place.0"], 12)
            self.assertEqual(set_count(sessions[0]), [])
            r = f.step("cts", place, parent=f.steps["place"].selected, variants=[{"threads": 4}], score="setup_wns")
            self.assertEqual(seen[r.attempts[0].id], 4)
            self.assertEqual(set_count(sessions[1]), ["set_thread_count 4"])
            # three attempts at once share the run's 12
            r = f.step("route_digital", place, parent=r.selected, variants=[{"iters": i} for i in (1, 2, 3)],
                       parallel=3, score="setup_wns")
            self.assertEqual({seen[a.id] for a in r.attempts}, {4})
            self.assertEqual([set_count(s) for s in sessions[2:]], [["set_thread_count 4"]] * 3)

    def test_threads_param_on_openroad_steps_only(self):
        from sipath import params, registry, steps
        params.check("place", {"threads": 8})
        params.check("signoff", {"threads": 8})
        with self.assertRaises(ValueError):
            params.check("place", {"threads": 0})
        with self.assertRaises(ValueError):  # Yosys: no OpenROAD threads
            params.check("synth", {"threads": 8})
        def mine(ctx):
            pass
        registry.register_step("my_threads_step", mine)
        self.addCleanup(lambda: (steps.STEPS.pop("my_threads_step"), params.STEP_PARAMS.pop("my_threads_step"),
                                 params.DECLARED.pop("my_threads_step")))
        params.check("my_threads_step", {"threads": 2})

    def test_report_shows_the_ppa_first_cut(self):
        ppa = {"parasitics": "global routing", "corner": "tt_nom",
               "area": {"std_cell_um2": 812.5, "macro_um2": 0.0, "cells": 140, "core_um2": 2000.0, "utilization": 0.41},
               "timing": {"wns_ns": -0.4, "tns_ns": -3.1, "whs_ns": 0.05,
                          "clocks": [{"clock": "clk", "period_ns": 5.0, "achievable_period_ns": 5.4, "fmax_mhz": 185.2,
                                      "suggested_period_ns": 5.67, "sdc": "create_clock -name clk -period 5.67 ..."}]},
               "power": {"internal_w": 1e-4, "switching_w": 5e-5, "leakage_w": 1e-9, "total_w": 1.5e-4},
               "wirelength_um": 9000.0, "wirelength_kind": "HPWL"}
        def step(ctx):
            demo.fake_step(delay=0)(ctx)
            ctx.outputs["ppa"] = ppa
        with tempfile.TemporaryDirectory() as td:
            self.flow(td).step("route", step, score="setup_wns")
            with open(os.path.join(td, "report.md")) as fh:
                md = fh.read()
            self.assertIn("## PPA (first cut from route: global routing parasitics, corner tt_nom)", md)
            self.assertIn("achievable 5.4 ns (fmax 185.2 MHz), suggested period 5.67 ns", md)
            self.assertIn("| wirelength (HPWL) | 9000 um |", md)

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

    def inputs(self, td):
        """A fake design over real files, so a test can edit them between runs."""
        files = {}
        for n, text in (("x.v", "module fake; endmodule\n"), ("x.sdc", "create_clock -period 10 [get_ports clk]\n"),
                        ("x.def", "VERSION 5.8 ;\n")):
            files[n] = os.path.join(td, "in", n)
            os.makedirs(os.path.dirname(files[n]), exist_ok=True)
            with open(files[n], "w") as fh:
                fh.write(text)
        return files

    def make_real(self, td, files, **kw):
        d = Design("fake", "fake", files["x.v"], files["x.sdc"], "macro", floorplan_def=files["x.def"])
        made = []

        def fac(w):
            s = demo.fake_factory()(w)
            made.append(s)
            return s
        return Flow(d, sky130hd("/p"), os.path.join(td, "run"), fac, validate_inputs=False, **kw), made

    def chain(self, f):
        a = f.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")
        b = f.step("b", demo.fake_step(), parent=a.selected, score="setup_wns")
        return a, b

    def test_resume_reruns_after_an_input_changes(self):
        """An edited SDC is never silently reused: every step after it runs again, the old attempts stay
        as stale with the reason; an unchanged resume runs nothing."""
        with tempfile.TemporaryDirectory() as td:
            files = self.inputs(td)
            f, _ = self.make_real(td, files)
            a, b = self.chain(f)
            f2, made = self.make_real(td, files, resume=True)
            self.chain(f2)
            self.assertEqual((made, f2.stale, f2.input_changes), ([], [], []))  # nothing changed: nothing ran
            with open(files["x.sdc"], "w") as fh:
                fh.write("create_clock -period 5 [get_ports clk]\n")
            f3, made = self.make_real(td, files, resume=True)
            self.assertEqual(f3.input_changes, ["x.sdc changed"])
            a3, b3 = self.chain(f3)
            self.assertEqual(len(made), 3)  # a's two variants and b
            self.assertEqual([s["step"] for s in f3.stale], ["a", "b"])
            self.assertIn("x.sdc changed", f3.stale[0]["reason"])
            self.assertIn("parent changed", f3.stale[1]["reason"])
            self.assertEqual(sorted(x.id for x in a3.attempts), ["a.2", "a.3"])  # new ids after the stale ones
            g = _read_json(os.path.join(td, "run", "graph.json"))
            st = {x["id"]: (x["status"], x.get("stale_reason")) for x in g["attempts"]}
            self.assertEqual(st["a.0"][0], "stale")
            self.assertEqual(st[a.selected.id][0], "stale")
            self.assertTrue(os.path.isfile(a.selected.checkpoint))  # kept, never reused
            self.assertNotEqual(g["steps"]["a"]["fingerprint"], None)
            f4, made = self.make_real(td, files, resume=True)  # and the re-run is itself resumable
            self.chain(f4)
            self.assertEqual(made, [])

    def test_resume_ignores_what_only_changes_speed(self):
        from sipath.config import Config
        with tempfile.TemporaryDirectory() as td:
            files = self.inputs(td)
            f, _ = self.make_real(td, files, config=Config(threads=2, command_timeout=100))
            self.chain(f)
            f2, made = self.make_real(td, files, resume=True, config=Config(threads=7, command_timeout=900))
            self.chain(f2)
            self.assertEqual((made, f2.stale), ([], []))
            f3, made = self.make_real(td, files, resume=True,
                                      config=Config(image="other/orfs@sha256:" + "1" * 64))  # a result-changing key
            self.assertEqual(f3.input_changes, ["configuration changed: image"])
            self.chain(f3)
            self.assertEqual(len(made), 3)

    def test_resume_reruns_a_step_whose_variants_changed(self):
        with tempfile.TemporaryDirectory() as td:
            files = self.inputs(td)
            f, _ = self.make_real(td, files)
            self.chain(f)
            f2, made = self.make_real(td, files, resume=True)
            a2 = f2.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")  # reused
            b2 = f2.step("b", demo.fake_step(), parent=a2.selected, variants=[{"k": 5}], score="setup_wns")
            self.assertEqual(len(made), 1)
            self.assertEqual(f2.stale, [{"step": "b", "reason": "its variants or score changed"}])
            self.assertEqual(b2.selected.id, "b.1")
            with self.assertRaises(ValueError):  # asked again in the same session: an error, as before
                f2.step("a", demo.fake_step(), variants=[{"k": 9}], score="setup_wns")

    def test_resume_of_an_interrupted_sweep_checks_the_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            files = self.inputs(td)
            f, _ = self.make_real(td, files)
            f.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")
            path = os.path.join(td, "run", "graph.json")
            g = _read_json(path)
            g["steps"] = {}
            g["attempts"][0]["status"] = "ok"
            g["attempts"][1].update(status="running", checkpoint=None)
            _write_json(path, g)
            with open(files["x.v"], "a") as fh:
                fh.write("// edited\n")
            f2, made = self.make_real(td, files, resume=True)
            r = f2.step("a", demo.fake_step(), variants=[{"k": 0}, {"k": 1}], score="setup_wns")
            self.assertEqual(len(made), 2)  # the finished a.0 is not reused either
            self.assertEqual(sorted(x.id for x in r.attempts), ["a.1", "a.2"])
            self.assertEqual([s["step"] for s in f2.stale], ["a"])

    def test_resume_of_a_graph_without_fingerprints_reuses_with_a_warning(self):
        with tempfile.TemporaryDirectory() as td:
            files = self.inputs(td)
            f, _ = self.make_real(td, files)
            self.chain(f)
            path = os.path.join(td, "run", "graph.json")
            g = _read_json(path)
            g.pop("inputs")
            for s in g["steps"].values():
                s.pop("fingerprint")
            _write_json(path, g)
            with self.assertLogs("sipath", "WARNING") as logs:
                f2, made = self.make_real(td, files, resume=True)
            self.assertIn("without input fingerprints", " ".join(logs.output))
            self.chain(f2)
            self.assertEqual(made, [])

    def test_input_fingerprint_is_stable_across_processes(self):
        import subprocess
        import sys
        code = ("from sipath.examples import dp8; from sipath import fingerprint; from sipath.pdk import load; "
                "from sipath.config import Config; d = dp8.make_design(); p = load('sky130hd', platforms='/p'); "
                "d.resolve(p); print(fingerprint.inputs(d, p, Config(), {'kind': 'x'})['digest'])")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=root).stdout.strip()
               for _ in range(2)}
        self.assertEqual(len(out), 1, out)
        self.assertEqual(len(out.pop()), 16)

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

    def test_runtime_choice(self):
        """auto takes the locked tools install-tools unpacked (no PATH needed), only when they are the configured
        image's; docker skips them; native never falls back to the image."""
        from sipath.config import Config, ConfigError
        from sipath.tools.openroad import session as sess
        with self.assertRaises(ConfigError):
            Config(runtime="podman")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        os.makedirs(os.path.join(d, "sipath-bin"))
        for name in ("openroad", "klayout"):
            with open(os.path.join(d, "sipath-bin", name), "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(os.path.join(d, "sipath-bin", name), 0o755)
        cfg = Config(tools_root=d)
        with open(os.path.join(d, ".sipath-image"), "w") as fh:
            fh.write(cfg.image + "\n")
        saved = {k: os.environ.pop(k, None) for k in ("SIPATH_OPENROAD", "SIPATH_PLATFORMS")}
        path = os.environ["PATH"]
        os.environ["PATH"] = "/nonexistent"  # no openroad, no docker
        try:
            r = sess.find_openroad(cfg)
            self.assertEqual((r.kind, r.binary), ("native", os.path.join(d, "sipath-bin", "openroad")))
            self.assertEqual(r.platform_root(), os.path.join(d, "OpenROAD-flow-scripts", "flow", "platforms"))
            self.assertEqual(r.tool_command("/tmp", ["klayout", "-v"])[0], os.path.join(d, "sipath-bin", "klayout"))
            self.assertEqual(r.tool_command("/tmp", ["magic", "-v"])[0], "magic")  # not in the tree: PATH
            self.assertIsNone(sess.find_openroad(cfg.replace(runtime="docker")))
            with open(os.path.join(d, ".sipath-image"), "w") as fh:  # another image's tools are left alone
                fh.write("openroad/orfs@sha256:" + "1" * 64 + "\n")
            with self.assertLogs("sipath", "WARNING"):
                self.assertIsNone(sess.find_openroad(cfg.replace(runtime="native")))
        finally:
            os.environ["PATH"] = path
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_a_new_nested_workdir_is_mounted(self):
        """designfile.build creates the workdir before mounting it: Docker mounts only host paths that exist,
        so runs/mcp/<id> on a fresh checkout (no runs/mcp yet) was left out and the first OpenROAD step
        could not read the synthesized netlist."""
        from sipath import designfile
        from sipath.examples import dp8
        from sipath.session import DockerRunner
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        wd = os.path.join(d, "runs", "mcp", "x")
        r = DockerRunner("img@sha256:" + "0" * 64)
        r.mount(wd)
        self.assertEqual(r.mounts, [])  # what the runner alone does with a path whose parent is missing
        r.platform_root = lambda: None
        try:
            designfile.build(dp8.make_design(), wd, r)
        except Exception as e:  # build reads the PDK before it mounts
            self.skipTest(f"needs the sky130A PDK tree: {e}")
        self.assertTrue(any(wd == m or wd.startswith(m + os.sep) for m in r.mounts), r.mounts)

    def test_images_pinned_by_default(self):
        from unittest import mock
        from sipath import toolchain
        from sipath.config import Config, ConfigError
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("SIPATH_")},
                              clear=True)  # the defaults, not this machine's or this CI job's settings
        env.start()
        self.addCleanup(env.stop)
        c = Config()
        self.assertTrue(c.require_digest)
        self.assertEqual((c.image, c.magic_image), (toolchain.image("orfs"), toolchain.image("magic")))
        self.assertTrue(toolchain.pinned(c.image) and toolchain.pinned(c.magic_image))
        with self.assertRaises(ConfigError):  # an unpinned image is refused by default
            Config(image="openroad/orfs:latest")
        with self.assertRaises(ConfigError):
            Config(magic_image="sipath/orfs-magic:8.3.685")
        old = os.environ.get("SIPATH_REQUIRE_DIGEST")
        os.environ["SIPATH_REQUIRE_DIGEST"] = "0"
        try:  # unless asked
            self.assertEqual(Config.load(image="openroad/orfs:latest").image, "openroad/orfs:latest")
        finally:
            os.environ.pop("SIPATH_REQUIRE_DIGEST")
            if old is not None:
                os.environ["SIPATH_REQUIRE_DIGEST"] = old
        for name, e in toolchain.lock()["images"].items():  # every locked image names its tools
            self.assertTrue(toolchain.pinned(e["ref"]), name)
            self.assertTrue(e["tools"], name)

    def test_generators_leave_current_inputs_alone(self):
        import time
        from sipath.examples import designs, ihp_add8
        files = [os.path.join(designs.INPUTS, f) for f in ("dp8_flat.v", "dp8_floorplan.def", "ihp_add8.v")]
        before = [os.path.getmtime(f) for f in files]
        time.sleep(0.05)
        designs.write_dp8()
        ihp_add8.write_inputs()
        self.assertEqual([os.path.getmtime(f) for f in files], before)
        with tempfile.TemporaryDirectory() as td:  # a new place still gets every file
            out = designs.write_dp8(td)
            self.assertTrue(all(os.path.isfile(out[k]) for k in ("netlist", "floorplan_def", "sdc")))

    def test_package_metadata(self):
        try:
            import tomllib
        except ImportError:  # Python 3.10 (tomllib is 3.11+); the newer CI jobs check this
            self.skipTest("tomllib needs Python 3.11")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            meta = tomllib.load(fh)
        import sipath
        self.assertEqual(meta["project"]["version"], sipath.__version__)
        self.assertEqual(meta["project"]["scripts"]["sipath"], "sipath.__main__:cli")
        data = meta["tool"]["setuptools"]["package-data"]["sipath"]
        for needed in ("toolchain.json", "synth_recipes/*", "pdks/**/*", "examples/**/*", "templates/**/*",
                       "harnesses/**/*"):
            self.assertIn(needed, data)

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
            with self.assertRaisesRegex(ToolError, "'synth' or 'floorplan'"):
                s.run_step("r1", step="cts")
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
                                 "get_outputs", "query_checkpoint", "query_heatmap", "adapt_step", "draw_checkpoint", "render_checkpoint",
                                 "job_status", "cancel_job", "list_jobs", "fork_run"})
        self.assertIn("resources", init["result"]["capabilities"])
        self.assertEqual(c.rpc("resources/list")["result"], {"resources": []})
        err, out, _ = c.call("list_designs")
        self.assertFalse(err)
        self.assertEqual(set(out["designs"]), {"dp8", "dp8_hs", "mcu8", "ihp_add8", "gcd8", "gcd8_sv",
                                             "openframe_gcd8", "gcd8_pair", "sram8", "gcd8_explore",
                                             "openframe_gcd8_route_search", "gcd8_macro_route_search",
                                             "uart_fifo", "openframe_uart"})
        self.assertTrue(c.call("run_step", run_id="nope", step="floorplan")[0])
        self.assertTrue(c.call("no_such_tool")[0])
        self.assertEqual(c.rpc("no/such/method")["error"]["code"], -32601)
        self.assertEqual(c.rpc(None, raw="{not json")["error"]["code"], -32700)
        self.assertEqual(c.rpc("ping")["result"], {})



class Limits(unittest.TestCase):
    """P1.3: a run's results against its regression limits."""

    def run_dir(self, td, signoff=True, wns=1.0):
        so = os.path.join(td, "signoff.json")
        _write_json(so, {"pass": True, "drc": {"count": 0}, "lvs": {"match": True},
                         "timing": {"corners": {"tt": {"wns": wns, "whs": 0.2}, "ss": {"wns": wns - 0.5, "whs": 0.3}}}})
        atts = [{"id": "place.0", "step": "place", "status": "selected", "started_at": 100.0, "finished_at": 110.0,
                 "parent": None, "metrics": {"wns": 3.0, "insts": 500, "wirelength_um": None}},
                {"id": "route.0", "step": "route", "status": "selected", "started_at": 110.0, "finished_at": 140.0,
                 "parent": "place.0", "metrics": {"wns": 2.0, "drc": 0, "wirelength_um": 3000, "insts": 520}},
                {"id": "eco.0", "step": "eco", "status": "failed", "started_at": 200.0, "finished_at": 300.0,
                 "parent": "signoff.0" if signoff else "route.0"}]
        steps = {"place": {"selected": "place.0"}, "route": {"selected": "route.0"}}
        if signoff:
            atts.append({"id": "signoff.0", "step": "signoff", "status": "selected", "started_at": 140.0,
                         "finished_at": 160.0, "parent": "route.0", "metrics": {}, "outputs": {"signoff": so}})
            steps["signoff"] = {"selected": "signoff.0"}
        steps["eco"] = {"selected": None}
        _write_json(os.path.join(td, "graph.json"), {"attempts": atts, "steps": steps})
        return td

    def test_metrics_and_check(self):
        from sipath import limits
        with tempfile.TemporaryDirectory() as td:
            self.run_dir(td)
            m = limits.run_metrics(td)  # up to signoff: later (failed) steps are not counted
            self.assertEqual((m["wns_ns"], m["whs_ns"], m["drc"], m["lvs_match"], m["signoff_pass"]),
                             (0.5, 0.2, 0, True, True))
            self.assertEqual((m["wirelength_um"], m["insts"], m["runtime_s"]), (3000, 520, 60.0))
            ok = {"min_wns_ns": 0.25, "max_drc": 0, "max_wirelength_um": 3300, "max_runtime_s": 120,
                  "signoff_pass": True}
            self.assertEqual(limits.check(td, ok), [])
            bad = limits.check(td, {"min_wns_ns": 1.0, "max_wirelength_um": 2000, "lvs_match": False,
                                    "max_antenna": 0})
            self.assertEqual(len(bad), 4, bad)
            self.assertTrue(any("antenna: no value" in b for b in bad))  # a missing result is a violation
            self.assertEqual(limits.check(td, {"until": "nothing", "max_drc": 0}), ["step nothing did not run"])
            with unittest.mock.patch.dict(os.environ, {"SIPATH_LIMITS_IGNORE": "max_runtime_s, lvs_match"}):
                self.assertEqual(limits.check(td, {"max_runtime_s": 1, "lvs_match": False, "max_drc": 0}), [])
                self.assertEqual(len(limits.check(td, {"max_runtime_s": 1, "max_drc": -1})), 1)  # others still count

    def test_docker_runs_as_the_user_on_linux(self):
        from sipath.session import DockerRunner
        r = DockerRunner.__new__(DockerRunner)
        with unittest.mock.patch("sys.platform", "linux"), unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SIPATH_DOCKER_AS_ROOT", None)
            self.assertEqual(r.identity()[:2], ["--user", f"{os.getuid()}:{os.getgid()}"])
            os.environ["SIPATH_DOCKER_AS_ROOT"] = "1"
            self.assertEqual(r.identity(), [])
        with unittest.mock.patch("sys.platform", "darwin"):
            self.assertEqual(r.identity(), [])  # Docker Desktop maps ownership itself

    def test_without_signoff_and_suggest(self):
        from sipath import limits
        with tempfile.TemporaryDirectory() as td:
            self.run_dir(td, signoff=False)
            m = limits.run_metrics(td, "route")
            self.assertEqual((m["wns_ns"], m["drc"], m["runtime_s"]), (2.0, 0, 40.0))
            self.assertNotIn("lvs_match", m)
            e = limits.suggest(td, "route")
            self.assertEqual(e, {"until": "route", "min_wns_ns": 1.5, "max_drc": 0, "max_wirelength_um": 3300,
                                 "max_insts": 572, "max_runtime_s": 110})
            self.assertEqual(limits.check(td, e), [])

    def test_limits_file_names_the_live_designs(self):
        from sipath import limits
        lim = limits.load()
        self.assertEqual(sorted(lim), ["dp8", "dp8_hs", "gcd8", "ihp_add8", "mcu8",
                                       "openframe_uart", "uart_fifo"])
        for d, e in lim.items():
            self.assertIn("max_drc", e, d)
            self.assertIn("max_runtime_s", e, d)


class FullDeviceLvs(unittest.TestCase):
    """P0.4: schematics prepared for the PDK's full-device LVS deck."""

    def test_prepare_aliases_prefix_and_library_cells(self):
        from sipath import lvs_full
        sch = (".subckt top a b vss vdd\n"
               "XC1 a vss sky130_fd_pr__cap_mim_m3_1 W=5 L=5 m=2\n"
               "XC2 b vss sky130_fd_pr__cap_mim_m3_2 W=5\n+ L=5 m=1\n"
               "Xu1 a vss vss vdd vdd b sky130_fd_sc_hd__inv_1\n"
               ".ends\n"
               "* Black-box entry subcircuit for sky130_fd_sc_hd__inv_1 abstract view\n"
               ".subckt sky130_fd_sc_hd__inv_1 A VGND VNB VPB VPWR Y\n.ends\n")
        cdl = ".SUBCKT inv A Y VGND VPWR\nMMN Y A VGND VGND nfet_01v8 m=1 w=0.65 l=0.15\n" \
              "MMP Y A VPWR VPWR sky130_fd_pr__pfet_01v8_hvt m=1 w=1 l=0.15\n.ENDS\n"
        lib = (".subckt sky130_fd_sc_hd__inv_1 A VGND VNB VPB VPWR Y\n"
               "X0 Y A VGND VNB sky130_fd_pr__nfet_01v8 w=650000u l=150000u\n.ends\n"
               ".subckt sky130_fd_sc_hd__buf_1 A VGND VNB VPB VPWR X\nX0 X A VGND VNB sky130_fd_pr__nfet_01v8\n.ends\n")
        aliases = load_pdk("sky130A", platforms="/p").signoff["lvs_full_aliases"] if SKY130A else {
            "sky130_fd_pr__cap_mim_m3_1": "sky130_fd_pr__model__cap_mim",
            "sky130_fd_pr__cap_mim_m3_2": "sky130_fd_pr__model__cap_mim_m4"}
        text, info = lvs_full.prepare([sch, cdl], aliases, "sky130_fd_pr__", [lib], combine=False)
        self.assertIn("XC1 a vss sky130_fd_pr__model__cap_mim W=5", text)
        self.assertIn("XC2 b vss sky130_fd_pr__model__cap_mim_m4 W=5 L=5 m=1", text)  # continuation joined
        self.assertEqual(info["aliased"], 2)
        self.assertIn("MMN Y A VGND VGND sky130_fd_pr__nfet_01v8 m=1", text)  # bare CDL model prefixed
        self.assertIn("MMP Y A VPWR VPWR sky130_fd_pr__pfet_01v8_hvt", text)  # already prefixed: unchanged
        self.assertEqual(info["prefixed"], 1)
        self.assertIn("X0 Y A VGND VNB sky130_fd_pr__nfet_01v8 w=650000u", text)  # black box filled from the lib
        self.assertEqual(info["from_libs"], ["sky130_fd_sc_hd__inv_1"])
        self.assertNotIn("buf_1", text)  # unused library cells stay out

    def test_parallel_devices_merged_like_the_layout(self):
        from sipath import lvs_full
        cell = (".subckt inv_2 A VGND VNB VPB VPWR Y\n"
                "X0 VPWR A Y VPB sky130_fd_pr__pfet_g5v0d10v5 w=1.5e+06u l=500000u\n"
                "X1 Y A VPWR VPB sky130_fd_pr__pfet_g5v0d10v5 w=1.5e+06u l=500000u\n"  # source/drain swapped
                "X2 VGND A Y VNB sky130_fd_pr__nfet_g5v0d10v5 w=750000u l=500000u\n"
                "X3 Y A VGND VNB sky130_fd_pr__nfet_g5v0d10v5 w=750000u l=1e+06u\n"  # other length: kept
                "XM9 Y A VGND VNB sky130_fd_pr__nfet_g5v0d10v5 L=0.5 W=2 nf=2 m=3\n"
                "XC1 Y VGND sky130_fd_pr__cap_mim_m3_1 W=5 L=5 m=2\n.ends\n")
        text, info = lvs_full.prepare([cell])
        self.assertIn("X0 VPWR A Y VPB sky130_fd_pr__pfet_g5v0d10v5 W=3.0 L=0.5", text)
        self.assertNotIn("X1 ", text)
        self.assertIn("X2 VGND A Y VNB sky130_fd_pr__nfet_g5v0d10v5 W=6.75 L=0.5", text)  # 0.75 + 2 x 3
        self.assertIn("X3 Y A VGND VNB sky130_fd_pr__nfet_g5v0d10v5 W=0.75 L=1.0", text)
        self.assertIn("XC1 Y VGND sky130_fd_pr__cap_mim_m3_1 W=5 L=5 m=2", text)  # not a MOS: untouched
        self.assertEqual(info["parallel_merged"], 2)

    def test_signoff_option(self):
        from sipath import params
        self.assertIn("lvs_full", params.SIGNOFF_OPTIONAL)
        params.check("signoff", {"checks": ["drc", "lvs", "lvs_full"]})
        if SKY130A:
            p = load_pdk("sky130A", platforms="/p")
            self.assertTrue(os.path.isfile(p.signoff["lvs_full_deck"]))
            self.assertIn(p.signoff["lvs_full_deck"], p.files())  # mounted with the PDK


class FrontEnd(unittest.TestCase):
    """P2.7: RTL readers, ABC goals, Verilator stubs, synthesis scores."""

    def design(self, rtl=("x.v",), **kw):
        return Design("t", "top", None, "t.sdc", "macro", rtl=list(rtl), rtl_defines={"W": 8}, rtl_includes=["/inc"], **kw)

    def test_readers(self):
        from sipath import synth
        d = self.design(("/a/pkg.sv", "/a/top.sv"))
        self.assertEqual(synth._read_rtl(d, True, "yosys"), ["read_verilog -sv -DW=8 -I/inc /a/pkg.sv /a/top.sv"])
        self.assertEqual(synth._read_rtl(d, True, "slang"),
                         ["read_slang --top top --empty-blackboxes -DW=8 -I/inc /a/pkg.sv /a/top.sv"])
        self.assertEqual(synth._read_rtl(d, True, "slang-plugin")[0], "plugin -i slang")

    def test_frontend_choice(self):
        from sipath import synth
        ctx = types.SimpleNamespace(params={"frontend": "auto"}, design=self.design(("t.v",)))
        self.assertEqual(synth.frontend(ctx), "yosys")  # plain Verilog: no probe
        ctx = types.SimpleNamespace(params={"frontend": "yosys"}, design=self.design(("t.sv",)))
        self.assertEqual(synth.frontend(ctx), "yosys")
        saved = synth.slang_support
        try:
            synth.slang_support = lambda c: None
            ctx = types.SimpleNamespace(params={"frontend": "auto"}, design=self.design(("t.sv",)))
            self.assertEqual(synth.frontend(ctx), "yosys")  # no slang: Yosys's own SV subset
            ctx.params = {"frontend": "slang"}
            with self.assertRaises(RuntimeError):
                synth.frontend(ctx)
            synth.slang_support = lambda c: "slang-plugin"
            ctx.params = {"frontend": "auto"}
            self.assertEqual(synth.frontend(ctx), "slang-plugin")
        finally:
            synth.slang_support = saved

    def test_recipes_targets_and_options(self):
        from sipath import recipes, synth
        self.assertEqual(len(recipes.names()), 20)
        self.assertEqual(recipes.resolve("speed"), "speed_sizing")  # earlier goal names stay
        with self.assertRaises(ValueError):
            recipes.resolve("nope")
        for objective, rs in recipes.SETS.items():
            self.assertEqual(len(rs), 5, objective)
            self.assertTrue(all(recipes.resolve(r) for r in rs))
        self.assertEqual(recipes.materialize("delay_map", None),
                         "strash; dch -f; map; buffer -c; topo; upsize -c ; dnsize -c ; stime -c")
        self.assertIn("upsize -c -D 3600", recipes.materialize("delay_map", 3600))
        self.assertIn("buffer -c", recipes.materialize("orfs_speed", None, wire_load=True))
        self.assertEqual(synth.abc_target_ps("none", 5000.0), None)  # the default: no target
        self.assertEqual(synth.abc_target_ps("period", 5000.0), 5000.0)
        self.assertEqual(synth.abc_target_ps("2500", 5000.0), 2500.0)
        with self.assertRaises(ValueError):
            synth.abc_target_ps("soon", 5000.0)
        flags, post = synth.front_end_options(["booth", "adder=kogge-stone", "noshare", "opt_full"])
        self.assertEqual(flags, ["-booth", "-extra-map +/choices/kogge-stone.v", "-noshare"])
        self.assertEqual(post, ["opt -full", "techmap", "opt -fast"])
        with self.assertRaises(ValueError):
            synth.front_end_options(["adder=ripple"])
        sc = synth.synth_script(load_pdk("sky130hd", platforms="/p"),
                                Design("t", "top", None, "t.sdc", "macro", rtl=["t.v"]), "/o/t.v", "/o/r.abc",
                                retime=True, opts=["booth"], keep_names=False)
        self.assertIn("synth -top top -flatten -noabc -booth", sc)
        self.assertIn("abc -dff -D 1", sc)
        self.assertNotIn("rename -wire", sc)

    @unittest.skipUnless(SKY130A, "no open_pdks sky130A tree")
    def test_abc_boundary_from_sdc_or_pdk(self):
        from sipath import synth
        p = load_pdk("sky130A", platforms="/p")
        with tempfile.TemporaryDirectory() as td:
            lib = os.path.join(td, "l.lib")
            with open(lib, "w") as fh:
                fh.write("library (l) {\n  capacitive_load_unit (1, pf);\n}\n")
            sdc = os.path.join(td, "t.sdc")
            with open(sdc, "w") as fh:
                fh.write("set L 0.02\ncreate_clock -period 5 [get_ports clk]\n"
                         "set_driving_cell -lib_cell sky130_fd_sc_hd__buf_4 -pin X [all_inputs]\n"
                         "set_load -pin_load $L [all_outputs]\n")
            d = Design("t", "top", None, sdc, "macro", rtl=["t.v"])
            self.assertEqual(synth.abc_boundary(p, d, lib), ("sky130_fd_sc_hd__buf_4", 20.0))  # pF -> fF
            with open(sdc, "w") as fh:
                fh.write("create_clock -period 5 [get_ports clk]\n")
            cell, load = synth.abc_boundary(p, d, lib)  # the PDK's macro defaults
            self.assertEqual(cell, p.constraints["driving_cell"][0])
            self.assertAlmostEqual(load, p.constraints["output_load"] * 1000, places=2)
            die = Design("t", "top", None, sdc, "die", rtl=["t.v"])
            self.assertIsNone(synth.abc_boundary(p, die, lib))  # a die's ports are pads

    def test_clock_period_from_tcl_variables(self):
        from sipath import synth
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "c.sdc")
            with open(f, "w") as fh:
                fh.write("set clk_name clk\nset clk_period 3.6\ncreate_clock -name $clk_name -period $clk_period [get_ports clk]\n")
            self.assertEqual(synth.clock_period_ps(f), 3600.0)
            with open(f, "w") as fh:
                fh.write("create_clock -period 10.0 [get_ports clk]\n")
            self.assertEqual(synth.clock_period_ps(f), 10000.0)

    def test_liberty_stubs(self):
        from sipath import synth
        lib = ('library (l) {\n  type (bus3) { base_type : array; bit_width : 3; bit_from : 2; bit_to : 0; }\n'
               '  cell ("mac") {\n    pin ("CLK") { direction : input; }\n'
               '    bus (D) { bus_type : bus3; direction : input; }\n    pin ("Q") { direction : output; }\n'
               '    pin ("VPWR") { direction : inout; }\n  }\n  cell ("other") { pin ("A") { direction : input; } }\n}\n')
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "l.lib")
            with open(p, "w") as fh:
                fh.write(lib)
            v = synth.liberty_stubs([p], ["mac"])
        self.assertEqual(v, "module mac(input CLK, input [2:0] D, output Q, inout VPWR);\nendmodule\n")

    def test_synth_scores(self):
        s = scores.get("synth_wns_then_area")
        met_small = {"synth_area_um2": 100.0, "synth_wns_ns": 0.1}
        met_big = {"synth_area_um2": 120.0, "synth_wns_ns": 2.0}
        late = {"synth_area_um2": 90.0, "synth_wns_ns": -0.2}
        later = {"synth_area_um2": 80.0, "synth_wns_ns": -0.5}
        self.assertGreater(s(met_small), s(met_big))  # both met: smaller area
        self.assertGreater(s(met_big), s(late))  # met beats late
        self.assertGreater(s(late), s(later))  # both late: better slack
        self.assertEqual(scores.get("auto")(late), s(late))
        self.assertIsNone(s({}))

    @unittest.skipUnless(SKY130A, "no open_pdks sky130A tree")
    def test_timing_judged_at_the_slow_corner_with_wire_load(self):
        from sipath import synth
        p = load_pdk("sky130A", platforms="/p")
        c = synth.timing_corner(p)
        self.assertEqual(c.name, "ss_max")  # worst setup corner of optimize_corners
        self.assertEqual(synth.timing_corner(p, "tt_nom").name, "tt_nom")
        with self.assertRaises(ValueError):
            synth.timing_corner(p, "nope")
        self.assertEqual(synth.default_wire_load(c.liberty), "Small")
        std, _ = synth._corner_libs(p, self.design(), c)
        self.assertIn("ss_100C_1v60", std)
        import dataclasses
        self.assertIsNone(synth.timing_corner(dataclasses.replace(p, corners=[])))  # single-corner PDK: its Liberty
        self.assertEqual(synth.map_corner(p).name, "ss_max")  # mapped where it is judged
        self.assertEqual(synth.map_corner(p, "typical").name, "tt_nom")
        self.assertEqual(synth.map_corner(p, "ff_min").name, "ff_min")

    def test_params(self):
        from sipath import params
        params.check("synth", {"frontend": "slang", "abc": "speed", "retime": True, "verilator": "on", "timing": False,
                               "timing_corner": "ss_max"})
        with self.assertRaises(ValueError):
            params.check("synth", {"abc": "fast"})


class SynthSelection(unittest.TestCase):
    """synth_flow's selection rule, the in-step recipe sweep, design-file sweeps."""

    @staticmethod
    def cand(abc, wns, area, period=5.0):
        return types.SimpleNamespace(params={"abc": abc}, metrics={"synth_wns_ns": wns, "synth_area_um2": area,
                                                                    "synth_period_ns": period}, outputs={})

    def test_least_area_meeting_timing(self):
        s = scores.get("synth_select")
        self.assertTrue(getattr(s, "selector", False))
        cs = [self.cand("delay_map", 1.0, 120.0), self.cand("area_map", 0.1, 100.0), self.cand("yosys_default", -0.2, 80.0)]
        best, why = s(cs)
        self.assertEqual(best.params["abc"], "area_map")
        self.assertIn("2/3 meeting timing", why)

    def test_knee_when_nothing_meets(self):
        s = scores.get("synth_select")
        cs = [self.cand("delay_map", -0.1, 200.0), self.cand("balanced_resyn", -0.3, 110.0),
              self.cand("area_map", -2.0, 100.0), self.cand("orfs_area", -0.35, 150.0)]  # orfs_area: dominated
        best, why = s(cs, "knee")
        self.assertEqual(best.params["abc"], "balanced_resyn")  # the knee, not the fastest
        self.assertIn("knee", why)
        self.assertEqual(s(cs)[0].params["abc"], "delay_map")  # the default: best_slack, the fastest
        best, why = s(cs[:2], "knee")  # under three front points: the best slack
        self.assertEqual(best.params["abc"], "delay_map")
        self.assertIn("best slack", why)
        tie = [self.cand("yosys_default", 0.5, 100.0), self.cand("delay_map", 0.5, 100.0)]
        self.assertEqual(s(tie)[0].params["abc"], "delay_map")  # ties by recipe rank

    def test_fallbacks_when_nothing_meets(self):
        s = scores.get("synth_select")
        cs = [self.cand("delay_map", -0.1, 200.0), self.cand("balanced_resyn", -0.3, 110.0),
              self.cand("area_map", -2.0, 100.0), self.cand("orfs_area", -0.35, 150.0)]
        self.assertEqual(s(cs, "best_slack")[0].params["abc"], "delay_map")
        best, why = s(cs, "least_area")
        self.assertEqual(best.params["abc"], "area_map")
        self.assertIn("least area", why)
        meeting = [self.cand("delay_map", 0.2, 200.0), self.cand("area_map", 0.1, 100.0)]
        self.assertEqual(s(meeting, "best_slack")[0].params["abc"], "area_map")  # only when nothing meets

    def test_signed_declarations_stripped(self):
        from sipath import synth
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "n.v")
            with open(p, "w") as fh:
                fh.write("module m(a, y);\n  input signed [15:0] a;\n  wire signed [15:0] a;\n  output y;\n"
                         "  sky130_fd_sc_hd__inv_1 u (.A(a[0]), .Y(y));\nendmodule\n")
            self.assertEqual(synth.strip_signed(p), 2)
            text = _read_text(p)
            self.assertIn("  input [15:0] a;", text)
            self.assertEqual(len(parse_verilog(text, "m").instances), 1)  # SiPath's reader takes it now

    def test_in_step_sweep_lists(self):
        from sipath import recipes, synth
        self.assertEqual(synth.sweep_recipes({"abc_sweep": "auto"}), recipes.SETS["delay"] + ["area_map"])
        self.assertEqual(synth.sweep_recipes({"abc_sweep": "area"}), recipes.SETS["area"])
        self.assertEqual(synth.sweep_recipes({"abc_sweep": "off"}), ["delay_map_resyn"])
        self.assertEqual(synth.sweep_recipes({"abc_sweep": "delay_map, area_map"}), ["delay_map", "area_map"])
        self.assertEqual(synth.sweep_recipes({"abc": "speed", "abc_sweep": "auto"}), ["speed_sizing"])  # abc wins

    def test_design_file_sweep_expansion(self):
        from sipath import designfile, recipes, schema
        item = designfile.expand_sweep({"step": "synth", "sweep": "delay", "variants": [{"equiv": "off"}],
                                        "synth_opts_sweep": [[], ["booth"]]})
        self.assertEqual(len(item["variants"]), 10)
        self.assertEqual(item["variants"][1], {"equiv": "off", "abc": recipes.SETS["delay"][0], "synth_opts": ["booth"]})
        self.assertEqual((item["score"], item["parallel"]), ("synth_select", 4))
        self.assertEqual(designfile.expand_sweep({"step": "place"}), {"step": "place"})
        with self.assertRaises(ValueError):
            designfile.expand_sweep({"step": "synth", "sweep": "fastest"})
        errors = []
        schema.check_object("flow[0]", schema.FLOW_STEP, {"step": "synth", "sweep": ["delay_map"],
                                                        "synth_opts_sweep": [["booth"]]}, errors)
        self.assertEqual(errors, [])


class MacroPlacerCdlns(unittest.TestCase):
    """The cdlns macro placement method: SiPath's export to the challenge's Benchmark."""

    def test_legal_start_separates_overlapping_macros(self):
        from sipath.macroplace import legal_start
        box = lambda n, x, y, fixed=False: {"name": n, "x": x, "y": y, "w": 40.0, "h": 30.0, "fixed": fixed}  # noqa: E731
        boxes = [box("a", 50, 50), box("b", 55, 52), box("c", 60, 48), box("k", 50, 50, fixed=True)]
        self.assertEqual(legal_start(boxes, 200, 120, 5.0), 3)
        self.assertEqual((boxes[3]["x"], boxes[3]["y"]), (50, 50))  # fixed boxes stay
        for i, p in enumerate(boxes):
            self.assertTrue(p["w"] / 2 <= p["x"] <= 200 - p["w"] / 2 and p["h"] / 2 <= p["y"] <= 120 - p["h"] / 2)
            for q in boxes[i + 1:]:
                self.assertTrue(abs(p["x"] - q["x"]) >= 45 or abs(p["y"] - q["y"]) >= 35, (p, q))
        with self.assertRaisesRegex(ValueError, "no legal start"):
            legal_start([box(c, 50, 50) for c in "abcdefg"], 130, 70, 5.0)

    DUMP = "\n".join([
        "M u_a 100000 100000 140000 130000", "M u_b 300000 300000 340000 330000", "M u_fixed 10000 10000 40000 40000",
        "C c1 60000 60000 2000000", "C c2 61000 62000 2000000", "C c3 380000 380000 4000000",
        "P in 0 200000", "P out 400000 200000",
        "N", "I u_a 140000 115000 Q", "I c1", "I c2",
        "N", "I u_a 100000 115000 D", "B in",
        "N", "I u_b 340000 315000 Q", "B out", "I c3",
        "N", "I c1",  # one owner: dropped
        ""])

    def test_spec_from_a_floorplan_dump(self):
        from sipath import macroplace
        core = (0, 0, 400000, 400000)
        spec, hard = macroplace.cdlns_spec(self.DUMP, 1000, core, 10000, {"u_a": True, "u_b": True},
                                           [(200000, 0, 220000, 20000)], 7, 30.0, 4.0, "t")
        self.assertEqual(hard, ["u_a", "u_b", "u_fixed", "__keepout0"])  # movable first, then fixed, keepouts
        self.assertEqual(spec["num_hard"], 4)
        self.assertEqual(spec["canvas"], [380.0, 380.0])  # the core less the edge on each side
        a = spec["macros"][0]
        self.assertEqual((a["x"], a["y"], a["w"], a["h"], a["fixed"]), (110.0, 105.0, 40.0, 30.0, False))
        self.assertTrue(spec["macros"][2]["fixed"] and spec["macros"][3]["fixed"])
        self.assertEqual(spec["pin_offsets"][0], [[20.0, 0.0], [-20.0, 0.0]])  # Q and D from u_a's centre
        self.assertEqual(spec["ports"], [[-10.0, 190.0], [390.0, 190.0]])
        self.assertEqual(len(spec["nets"]), 3)
        n0 = spec["nets"][0]
        self.assertEqual(n0[0], [0, 0])
        self.assertEqual(len(n0), 2)  # c1 and c2 share a cluster: one soft owner
        nm = len(spec["macros"])
        self.assertIn([nm, 0], spec["nets"][1])  # the port 'in'
        self.assertEqual((spec["seed"], spec["seconds"], spec["gap"]), (7, 30.0, 4.0))

    def test_params_and_placer_python(self):
        from sipath import macroplace, params
        params.check("macros", {"method": "cdlns", "placer_seconds": 60})
        ctx = types.SimpleNamespace(config=types.SimpleNamespace(placer_python="/nonexistent/python"))
        with self.assertRaisesRegex(RuntimeError, "setup-placer"):
            macroplace.placer_python(ctx)
