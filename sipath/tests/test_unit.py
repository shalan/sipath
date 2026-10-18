"""Unit tests: no OpenROAD binary, no PDK runs (python3 -m unittest sipath.tests.test_unit;
under a minute)."""
from __future__ import annotations

from .common import *  # noqa: F401,F403  (helpers, fixtures and the modules the tests use)
from .common import _big_def, _check_limits, _check_outputs, _lib, _pins, _read_json, _rmtree, _workdir, _write_json  # noqa: F401


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
        self.assertEqual((len(hd.exclude), len(hs.exclude)), (59, 51))  # hd: + 6 cells the LVS deck cannot pair
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
        nets = ("NETS 2 ;\n"
                "    - a ( PIN a ) ( u1 X ) + USE SIGNAL\n"
                "      + ROUTED li1 ( 100 200 ) L1M1_PR ( 300 * ) M1M2_PR ( * 900 0 ) ( 500 * ) ( * 1200 )\n"
                "      NEW met2 ( 700 100 0 ) ( * 400 ) ( 900 * 0 ) ;\n"  # path ends: kept
                "    - b ( u1 A ) ( u2 Y ) + USE SIGNAL\n"
                "      + ROUTED met2 ( 10 10 ) ( * 50 0 ) ( * 90 ) ( 20 * 0 ) M2M3_PR ( * 120 ) ;\n"  # straight / via: kept
                "END NETS\n")
        with open(path, "w") as f:
            f.write("VERSION 5.8 ;\n" + nets + "END DESIGN\n")
        self.assertEqual(fix_zero_ext_corners(path), [{"net": "a", "at": (300, 900)}])
        with open(path) as f:
            text = f.read()
        self.assertIn("M1M2_PR ( * 900 ) ( 500 * )", text)
        self.assertIn("( 700 100 0 ) ( * 400 ) ( 900 * 0 )", text)
        self.assertIn("( * 50 0 ) ( * 90 ) ( 20 * 0 ) M2M3_PR", text)
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

    def test_images_pinned_by_default(self):
        from sipath import toolchain
        from sipath.config import Config, ConfigError
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
        import tomllib
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
                                 "get_outputs", "query_checkpoint", "draw_checkpoint", "render_checkpoint",
                                 "job_status", "cancel_job", "list_jobs"})
        self.assertIn("resources", init["result"]["capabilities"])
        self.assertEqual(c.rpc("resources/list")["result"], {"resources": []})
        err, out, _ = c.call("list_designs")
        self.assertFalse(err)
        self.assertEqual(set(out["designs"]), {"dp8", "dp8_hs", "mcu8", "ihp_add8", "gcd8", "gcd8_sv", "openframe_gcd8", "gcd8_pair", "sram8"})
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
                 "metrics": {"wns": 3.0, "insts": 500, "wirelength_um": None}},
                {"id": "route.0", "step": "route", "status": "selected", "started_at": 110.0, "finished_at": 140.0,
                 "metrics": {"wns": 2.0, "drc": 0, "wirelength_um": 3000, "insts": 520}},
                {"id": "eco.0", "step": "eco", "status": "failed", "started_at": 200.0, "finished_at": 300.0}]
        steps = {"place": {"selected": "place.0"}, "route": {"selected": "route.0"}}
        if signoff:
            atts.append({"id": "signoff.0", "step": "signoff", "status": "selected", "started_at": 140.0,
                         "finished_at": 160.0, "metrics": {}, "outputs": {"signoff": so}})
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
        self.assertEqual(sorted(lim), ["dp8", "dp8_hs", "gcd8", "ihp_add8", "mcu8"])
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
        best, why = s(cs)
        self.assertEqual(best.params["abc"], "balanced_resyn")  # the knee, not the fastest
        self.assertIn("knee", why)
        best, why = s(cs[:2])  # under three front points: the best slack
        self.assertEqual(best.params["abc"], "delay_map")
        self.assertIn("best slack", why)
        tie = [self.cand("yosys_default", 0.5, 100.0), self.cand("delay_map", 0.5, 100.0)]
        self.assertEqual(s(tie)[0].params["abc"], "delay_map")  # ties by recipe rank

    def test_signed_declarations_stripped(self):
        from sipath import synth
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "n.v")
            with open(p, "w") as fh:
                fh.write("module m(a, y);\n  input signed [15:0] a;\n  wire signed [15:0] a;\n  output y;\n"
                         "  sky130_fd_sc_hd__inv_1 u (.A(a[0]), .Y(y));\nendmodule\n")
            self.assertEqual(synth.strip_signed(p), 2)
            text = open(p).read()
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
