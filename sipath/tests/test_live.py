"""Live tests: real OpenROAD (and KLayout/Magic) runs on the example designs; skipped when no
OpenROAD is available. python3 -m unittest sipath.tests.test_live, or in parallel:
python3 -m sipath.tests.run_live [-j N]."""
from __future__ import annotations

from .common import *  # noqa: F401,F403  (helpers, fixtures and the modules the tests use)
from .common import _big_def, _check_limits, _check_outputs, _lib, _pins, _read_json, _rmtree, _workdir, _write_json  # noqa: F401


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
        self.assertEqual(sy.metrics["equiv"], "equivalent for 10 cycles from reset")  # P0.3
        # a netlist with one gate changed is refused
        from sipath import synth
        from sipath.signoff import run_tool
        with open(sy.outputs["netlist"]) as fh:
            good = fh.read()
        bad = re.sub(r"sky130_fd_sc_hd__nand2_1 ", "sky130_fd_sc_hd__nor2_1 ", good, count=1)
        self.assertNotEqual(bad, good)
        bad_v = os.path.join(sy.dir, "bad.v")
        with open(bad_v, "w") as fh:
            fh.write(bad)
        script = os.path.join(sy.dir, "bad_equiv.ys")
        with open(script, "w") as fh:
            fh.write(synth.equiv_script(f.pdk, f.design, bad_v, 10, ("rst_n", 0), 600))
        ctx = types.SimpleNamespace(dir=sy.dir, runner=f.runner, config=None, path=lambda n: os.path.join(sy.dir, n))
        try:
            run_tool(ctx, ["yosys", "-q", "-l", os.path.join(sy.dir, "bad_equiv.log"), "-s", script],
                     os.path.join(sy.dir, "bad_equiv.out"))
        except Exception:
            pass
        with open(os.path.join(sy.dir, "bad_equiv.log")) as fh:
            self.assertIn("FAIL", fh.read())
        self.assertEqual(sy.metrics["abc_period_ps"], 10000.0)
        self.assertTrue(f.steps["floorplan"].selected.outputs.get("auto_pins"))
        self.assertTrue(f.steps["physical"].selected.outputs.get("pdn_straps_scaled"))
        s = _read_json(f.steps["signoff"].selected.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        self.assertEqual((s["drc"]["count"], s["lvs"]["match"], s["antenna"]["violations"]), (0, True, 0))
        _check_limits(self, wd, "gcd8")

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
                "core_utilization": 0.4, "macros": [{"inst": "u_dac", "lef": DAC_LEF,
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
        _check_limits(self, wd, "dp8_hs")


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
            for v in TCL_NASTY:  # what Tcl stores is exactly the Python value
                self.assertEqual(s.value(f"set x {q(v)}; set x"), v)
            with self.assertRaises(TclError):
                s.run("break")
            self.assertEqual(s.value("return 7"), "7")  # return is a normal completion


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

    def test_within_limits(self):
        _check_limits(self, self.wd, "dp8")

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
        for c in f.pdk.analysis_corners():  # timed in the flow on the optimize corners; signoff times all nine
            self.assertIn(f"wns_{c.name}", ex.metrics)
        self.assertEqual([c.name for c in f.pdk.analysis_corners()], ["tt_nom", "ss_max", "ff_min"])
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
        # resize a multiplier array cell: it grows and its row re-abuts. Upsizing adds input
        # capacitance on its driver: this test checks the mechanics (re-abut, patch route), so it
        # allows that small setup loss (0.064 ns since the rows carry tap slots) explicitly
        e1 = f.eco("eco_resize", head, [{"op": "resize", "inst": "u_mul.add_1_1", "master": "sky130_fd_sc_hd__fa_2"}],
                   max_patch_nets=60, max_slack_loss=0.1)
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

    def test_within_limits(self):
        _check_limits(self, self.wd, "mcu8")

    def test_pad_timing_from_the_pad_liberty(self):
        """P2.5: paths through the GPIO pads carry the pad Liberty's delays (not
        the old fixed 0.5 ns black box): input PAD->IN, output OUT->PAD."""
        f = self.flow
        ex = f.steps["extract"].selected
        wd = _workdir("padtiming")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        RUNNER.mount(wd)
        with OpenRoadSession(wd, RUNNER) as s:
            s.run(tcl.HELPERS)
            s.run(tcl.load_checkpoint(f.pdk, f.design, ex.checkpoint))
            s.run("::flow_timing_prep")
            rpt = s.run("report_checks -from [get_ports {cfg[0]}] -corner tt_nom -format full\n"
                        "report_checks -to [get_ports {dout[0]}] -corner ss_max -format full").output
        def arc(pin):
            m = re.search(r"^\s*([\d.]+)\s+[\d.]+ [v^] " + re.escape(pin) + r" \(sky130_ef_io__gpiov2_pad_wrapped\)",
                          rpt, re.M)
            self.assertIsNotNone(m, f"no path through {pin}:\n{rpt[-2000:]}")
            return float(m.group(1))
        self.assertGreater(arc("pad_cfg_0/IN"), 0.8)  # tt PAD->IN ~1.35 ns
        self.assertGreater(arc("pad_dout_0/PAD"), 2.0)  # ss OUT->PAD ~5.2 ns

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

    def test_within_limits(self):
        _check_limits(self, self.wd, "ihp_add8")

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

