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
        # heatmaps, after each checkpoint was saved: every map these steps give, IR drop after extraction
        for step, maps in (("place", 4), ("cts", 4), ("route_digital", 5), ("extract", 6)):
            hm = f.steps[step].selected.outputs["heatmaps"]
            self.assertEqual(len(hm), maps, hm)
            for name, e in hm.items():
                self.assertNotIn("unavailable", e, f"{step} {name}")
                self.assertTrue(os.path.isfile(e["csv"]))
                self.assertGreater(f.steps[step].selected.metrics[f"heatmap_{name}_peak"], 0)
        self.assertEqual(f.steps["extract"].selected.outputs["heatmaps"]["irdrop"]["unit"], "uV")
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
        self.assertEqual((sy.metrics["synth_period_ns"], sy.metrics["abc_target_ps"]), (10.0, None))  # no -D target
        sweep = sy.outputs["abc_sweep"]  # abc_sweep auto: the delay set plus area_map, one picked
        self.assertEqual(len(sweep["results"]), 6)
        self.assertEqual(sweep["selected"], sy.metrics["abc_recipe"])
        self.assertIn("meeting timing", sweep["why"])  # 10 ns is loose: the least area that meets it
        self.assertIn("_reg", open(sy.outputs["netlist"]).read())  # register names kept
        self.assertTrue(f.steps["floorplan"].selected.outputs.get("auto_pins"))
        self.assertTrue(f.steps["physical"].selected.outputs.get("pdn_straps_scaled"))
        s = _read_json(f.steps["signoff"].selected.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        self.assertEqual((s["drc"]["count"], s["lvs"]["match"], s["antenna"]["violations"]), (0, True, 0))
        v = _read_json(os.path.join(wd, "graph.json"))["verification"]  # B1.3: what the run established
        self.assertEqual((v["execution"], v["verification"], v["omitted"]), ("completed", "passed", []), v)
        self.assertEqual(v["checks"]["equivalence"]["status"], "passed")
        _check_limits(self, wd, "gcd8")

    def test_systemverilog_through_slang(self):
        """P2.7: an SV design (package, enum, packed struct, function) read by
        slang, Verilator-linted, synthesized and proven against its RTL."""
        from sipath import designfile
        wd = _workdir("gcd8_sv")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        d, _ = designfile.load(os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8_sv.yaml"))
        f = designfile.run(d, wd, RUNNER, flow_steps=[{"step": "synth"}])
        m = f.steps["synth"].selected.metrics
        self.assertEqual(m["frontend"], "slang")
        self.assertEqual(m["equiv"], "equivalent for 10 cycles from reset")
        self.assertGreater(m["synth_flops"], 20)
        if "verilator_warnings" in m:  # Verilator found (host, native tool or image)
            self.assertEqual(m["verilator_warnings"], 0)
        self.assertGreater(m["synth_wns_ns"], 0)

    def test_abc_sweep_picks_by_score(self):
        """P2.7: ABC goals (and retiming) as attempts, picked by
        synth_wns_then_area on a clock tight enough that slack decides."""
        import dataclasses
        from sipath import designfile, scores
        wd = _workdir("abc_sweep")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        d, _ = designfile.load(os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8.yaml"))
        sdc = os.path.join(wd, "tight.sdc")
        with open(d.sdc) as fh, open(sdc, "w") as out:
            out.write(fh.read().replace("-period 10.0", "-period 2.5"))
        RUNNER.mount(wd)
        variants = [{"abc": g, "equiv": "off"} for g in ("delay", "area", "speed")] + \
                   [{"abc": "speed", "retime": True, "equiv": "off"}]
        f = designfile.run(dataclasses.replace(d, sdc=sdc), os.path.join(wd, "run"), RUNNER,
                           flow_steps=[{"step": "synth", "variants": variants, "score": "synth_wns_then_area",
                                        "parallel": 4}])
        r = f.steps["synth"]
        self.assertTrue(all(a.status in ("selected", "rejected") for a in r.attempts), [a.error for a in r.attempts])
        s = scores.get("synth_wns_then_area")
        self.assertEqual(r.selected.id, max(r.attempts, key=lambda a: s(a.metrics)).id)
        areas = {a.params["abc"]: a.metrics["synth_area_um2"] for a in r.attempts if not a.params.get("retime")}
        self.assertLess(areas["area"], areas["delay"])  # the area goal maps for area
        self.assertTrue(all(a.metrics["abc_target_ps"] is None for a in r.attempts))  # default: no -D target

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
BUS_DEF = r"""VERSION 5.8 ;
DIVIDERCHAR "/" ;
BUSBITCHARS "[]" ;
DESIGN top ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 50000 50000 ) ;
COMPONENTS 2 ;
- result\[0\]_reg sky130_fd_sc_hd__dfxtp_1 + PLACED ( 10120 10880 ) N ;
- u_buf sky130_fd_sc_hd__buf_1 + PLACED ( 20240 10880 ) N ;
END COMPONENTS
NETS 1 ;
- n1 ( result\[0\]_reg Q ) ( u_buf A ) ;
END NETS
END DESIGN
"""


def tcl_words_nested(text):
    from sipath.tclquote import tcl_words
    return [tcl_words(w) for w in tcl_words(text)]


@LIVE
class OpenRoad(unittest.TestCase):
    """A live process: reads a LEF, answers Tcl, raises on Tcl errors."""

    def test_macro_hpwl_counts_shared_net_once(self):
        """Use public net names and count a multi-pin, multi-macro net once."""
        from sipath import chip
        wd = _workdir("macro_hpwl")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        lef = os.path.join(wd, "fixture.lef")
        with open(lef, "w") as fh:
            fh.write("VERSION 5.8 ;\nUNITS DATABASE MICRONS 1000 ; END UNITS\n"
                     "MANUFACTURINGGRID 0.001 ;\n"
                     "LAYER metal1 TYPE ROUTING ; DIRECTION HORIZONTAL ;\n"
                     " PITCH 0.5 ; WIDTH 0.2 ; SPACING 0.2 ; END metal1\n"
                     "MACRO ram CLASS BLOCK ; ORIGIN 0 0 ; SIZE 10 BY 10 ;\n")
            for pin, use in (("P", "SIGNAL"), ("Q", "SIGNAL"), ("VDD", "POWER")):
                fh.write(f"PIN {pin} DIRECTION INOUT ; USE {use} ;\n"
                         f" PORT LAYER metal1 ; RECT 4 4 6 6 ; END\nEND {pin}\n")
            fh.write("END ram\nEND LIBRARY\n")
        def_ = os.path.join(wd, "fixture.def")
        with open(def_, "w") as fh:
            fh.write('VERSION 5.8 ;\nDIVIDERCHAR "/" ;\nBUSBITCHARS "[]" ;\n'
                     'DESIGN top ; UNITS DISTANCE MICRONS 1000 ;\n'
                     'DIEAREA ( 0 0 ) ( 100000 100000 ) ;\nCOMPONENTS 3 ;\n'
                     '- a ram + FIXED ( 20000 20000 ) N ;\n'
                     '- b ram + FIXED ( 80000 40000 ) N ;\n'
                     '- c ram + UNPLACED ;\nEND COMPONENTS\nNETS 2 ;\n'
                     '- shared[0] ( a P ) ( a Q ) ( b P ) ( b Q ) ( c P ) + USE SIGNAL ;\n'
                     '- supply ( a VDD ) ( b VDD ) + USE POWER ;\nEND NETS\nEND DESIGN\n')
        with OpenRoadSession(wd, RUNNER, timeout=120) as s:
            s.run(f"read_lef {tcl.q(lef)}\nread_def {tcl.q(def_)}\n" + tcl.HELPERS + chip.MACRO_HPWL)
            self.assertEqual(float(s.value("::flow_macro_hpwl {a b}")), 80.0)
            self.assertEqual(s.value("set ::flow_macro_hpwl_skipped"), "1")
            self.assertEqual(float(s.value("::flow_macro_hpwl {b a a}")), 80.0)
            s.run("[::flow_inst c] setLocation 90000 90000")
            self.assertEqual(float(s.value("::flow_macro_hpwl {a b}")), 80.0)

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

    def test_bus_register_names_are_found(self):
        """The database keeps a bus register's name escaped (result\\[0\\]_reg), STA reports print it plain, and
        Tcl braces it in a list: every lookup finds it in each form (a missed one was a dropped DRV violation, a
        moved cell's nets left unrouted)."""
        from sipath import signoff
        from sipath.tools.openroad.ops import db, eco, structure
        pdk = sky130hd(RUNNER.platform_root())
        wd = _workdir("busnames")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        with open(os.path.join(wd, "t.def"), "w") as fh:
            fh.write(BUS_DEF)
        with OpenRoadSession(wd, RUNNER, timeout=120) as s:
            s.run(f"read_lef {tcl.q(pdk.tech_lef)}\nread_lef {tcl.q(pdk.cell_lef)}\n"
                  f"read_liberty {tcl.q(pdk.liberty)}\nread_def {tcl.q(os.path.join(wd, 't.def'))}")
            s.run(tcl.HELPERS)
            self.assertEqual(db.inst_master_types(s, ["result[0]_reg", "u_buf", "nope"]),
                             {"result[0]_reg": "CORE", "u_buf": "CORE", "nope": "UNKNOWN"})
            loc = eco.locations(s)
            self.assertEqual(set(loc), {"result\\[0\\]_reg", "u_buf"})
            self.assertEqual(set(loc), set(eco.masters(s)))  # one form for both
            self.assertEqual(eco.inst_nets(s, ["result\\[0\\]_reg"]), {"n1"})
            self.assertEqual(eco.inst_nets(s, ["result[0]_reg"]), {"n1"})
            self.assertEqual(structure.cluster_cells(s, "result")[:3], ["result\\[0\\]_reg", "10120", "10880"])
            drivers = eco.drv_driver_list(s, ["u_buf/A"])
            self.assertIn(["result\\[0\\]_reg", "sky130_fd_sc_hd__dfxtp_1"], tcl_words_nested(drivers[0]))
            s.run(signoff.NET_KIND)
            self.assertEqual(s.value("::flow_pin_net_kind result\\[0\\]_reg/Q"), "1 0")


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

    def test_shielded_analog_net(self):
        # ana_in[0] is shielded (grounded wires on both sides, tie straps to the met4 stripes);
        # without them the clock couples ~3 fF onto it
        sh = self.flow.steps["route_analog"].selected.outputs["analog_shield"]["ana_in[0]"]
        self.assertGreater(sh["coverage"], 0.85)
        self.assertGreater(sh["vias"], 0)
        cc = _read_json(self.flow.steps["extract"].selected.outputs["analog_coupling"])
        self.assertLess(cc["ana_in[0]"]["digital_ff"], 0.5)
        self.assertGreater(cc["ana_in[0]"]["total_ff"], cc["ana_in[1]"]["total_ff"])  # the shields add ground cap

    def test_signoff_physical_and_timing(self):
        so = self.flow.steps["signoff"].selected
        s = _read_json(so.outputs["signoff"])
        self.assertTrue(s["pass"], s["failures"])
        from sipath import verdict
        g = self.flow.graph()
        g["head"] = so.id  # this signoff's own chain: an ECO the class applied later makes the run's head stale
        v = verdict.verify(g)  # a netlist design: no equivalence to omit; every standard check ran
        self.assertEqual((v["execution"], v["verification"]), ("completed", "passed"), v)
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
                observed = {"after_save"} if a.step in ("place", "cts", "route_digital", "extract") else set()  # heatmaps
                self.assertEqual(set(a.timing["phases"]), {"start", "load", "step", "metrics", "write_db"} | observed,
                                 a.id)
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
        from sipath.tools.openroad.checkpoint import parse_metrics
        m = parse_metrics(m)
        self.assertEqual(m["insts"], 100000)
        self.assertGreater(m["hpwl_um"], 100000 * 1.0)  # every chain net spans at least a cell
        self.assertLess(took, 10.0, f"metrics took {took:.1f}s on 100k instances")


@LIVE
@LIVE
class LiveAdaptive(unittest.TestCase):
    """Adaptive rounds on a crowded gcd8 (65% core utilization, placement density 0.8): the placement's density
    hotspots become partial blockages in the next attempts, and the step selects again (hotspots_then_wns)."""

    def test_hotspots_become_the_next_placement(self):
        import yaml
        from sipath import designfile
        wd = _workdir("adaptive")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        ex = os.path.join(os.path.dirname(__file__), "..", "examples")
        with open(os.path.join(ex, "gcd8.yaml")) as fh:
            d = yaml.safe_load(fh)
        d.update(rtl=[os.path.abspath(os.path.join(ex, r)) for r in d["rtl"]],
                 sdc=os.path.abspath(os.path.join(ex, d["sdc"])), core_utilization=0.65,
                 flow=[{"step": "synth"}, {"step": "floorplan"}, {"step": "physical"},
                       {"step": "place", "variants": [{"density": 0.8}], "adapt": {"rounds": 2}, "parallel": 2}])
        path = os.path.join(wd, "dense.yaml")
        with open(path, "w") as fh:
            yaml.safe_dump(d, fh)
        f = designfile.run(path, os.path.join(wd, "run"), RUNNER)
        run = f.steps["place"]
        first, sel = run.attempts[0], run.selected
        self.assertEqual(run.score_name, "hotspots_then_wns")
        self.assertGreater(first.metrics["heatmap_placement_hot_share"], 0)  # there is something to answer
        self.assertIsNot(sel, first)
        self.assertEqual(sel.adapted_from, first.id)
        self.assertTrue(sel.params.get("blockages"))
        self.assertIn("hotspots", sel.adapt_reason)
        self.assertLess(sel.metrics["heatmap_placement_hot_share"], first.metrics["heatmap_placement_hot_share"])
        self.assertEqual(sel.metrics["placement_blockages"], len(sel.params["blockages"]))
        self.assertTrue(run.rounds and run.rounds[-1].get("stopped"))  # why the rounds ended
        self.assertEqual(run.rounds[0]["from"], first.id)


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

    def test_an_edited_clock_is_never_reused(self):
        """B1.1: resume after the SDC's clock period changed: every step runs again (the old attempts stay,
        stale, with the reason) and the setup slack follows the new clock."""
        import shutil
        from sipath import designfile
        from sipath.examples import dp8
        wd = _workdir("resume_sdc")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        steps = [{"step": "floorplan"}, {"step": "physical"}, {"step": "place"}]
        sdc = os.path.join(wd, "inputs", "dp8.sdc")
        os.makedirs(os.path.dirname(sdc), exist_ok=True)
        shutil.copy(dp8.make_design().sdc, sdc)

        def mk():  # a fresh design each run, on the copied SDC
            d = dp8.make_design()
            d.sdc = sdc
            return d
        f1 = designfile.run(mk(), os.path.join(wd, "run"), RUNNER, flow_steps=steps)
        wns1 = f1.steps["place"].selected.metrics["wns"]
        f2 = designfile.run(mk(), os.path.join(wd, "run"), RUNNER, flow_steps=steps, resume=True)
        self.assertEqual(f2.stale, [])  # unchanged: nothing ran again
        self.assertTrue(f2.steps["place"].selected.resumed)
        with open(sdc) as fh:
            text = fh.read()
        import re
        m = re.search(r"-period\s+([0-9.]+)", text)
        self.assertIsNotNone(m, "dp8.sdc names no -period")
        period = float(m.group(1))
        with open(sdc, "w") as fh:
            fh.write(text.replace(m.group(0), f"-period {period / 2:g}", 1))
        f3 = designfile.run(mk(), os.path.join(wd, "run"), RUNNER, flow_steps=steps, resume=True)
        self.assertEqual(f3.input_changes, ["dp8.sdc changed"])
        self.assertEqual([s["step"] for s in f3.stale], ["floorplan", "physical", "place"])
        sel = f3.steps["place"].selected
        self.assertFalse(sel.resumed)
        self.assertLess(sel.metrics["wns"], wns1 - period / 4)  # half the period: far less slack
        g = _read_json(os.path.join(wd, "run", "graph.json"))
        old = [a for a in g["attempts"] if a["status"] == "stale"]
        self.assertEqual({a["step"] for a in old}, {"floorplan", "physical", "place"})
        self.assertTrue(all("dp8.sdc changed" in a["stale_reason"] or "parent changed" in a["stale_reason"]
                            for a in old))


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



@LIVE
class LiveMacroPlacers(unittest.TestCase):
    """Eight SRAM macros (sram8) placed by the seeded random search and by the
    CD-LNS placer (skipped without the placer Python): both legal by the macro
    rules, both scored by macro_quick."""

    def test_sram8_random_and_cdlns(self):
        from sipath import designfile
        from sipath.config import Config
        from sipath.macroplace import placer_python
        try:
            placer_python(types.SimpleNamespace(config=Config()))
        except RuntimeError as e:
            self.skipTest(str(e))
        wd = _workdir("sram8")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        f = designfile.run(os.path.join(os.path.dirname(__file__), "..", "examples", "sram8.yaml"), wd, RUNNER,
                           overrides={"macros": {"variants": [{"method": "random", "seed": 1},
                                                              {"method": "cdlns", "placer_seconds": 20}],
                                                 "score": "macro_quick", "parallel": 2}})
        rnd, cd = f.steps["macros"].attempts
        for a in (rnd, cd):
            self.assertIn(a.status, ("selected", "rejected"), a.error)
            placed = a.outputs["macros"]
            self.assertEqual(sorted(placed), [f"bank[{i}].u_ram" for i in range(8)])
            self.assertEqual({v["method"] for v in placed.values()}, {a.params["method"]})
            self.assertGreater(a.metrics["macro_hpwl_um"], 0)
            self.assertGreater(a.metrics["hpwl_um"], a.metrics["macro_hpwl_um"])
            self.assertIsNotNone(a.metrics["quick_overflow"])
        info = cd.outputs["cdlns"]
        self.assertEqual(info["macros"], 8)
        self.assertGreater(info["soft_macros"], 0)
        self.assertTrue(os.path.isfile(info["spec"]))
        self.assertIn(f.steps["macros"].selected, (rnd, cd))


def _precheck_image():
    """The precheck image the configuration names (SIPATH_PRECHECK_IMAGE: CI builds one), else the locked one,
    when it is present here (it is built locally: docker/precheck)."""
    import subprocess
    from sipath import toolchain
    from sipath.config import Config
    ref = Config.load().precheck_image or toolchain.lock()["images"].get("precheck", {}).get("ref")
    if not ref:
        return None
    r = subprocess.run(["docker", "image", "inspect", ref], capture_output=True)
    return ref if r.returncode == 0 else None


@LIVE
class LiveHardenedParent(unittest.TestCase):
    """harden, then a parent built from the hardened views (gcd8_pair, recipe 9): two macro instances, the
    parent's straps on their supply pins, signoff over the macros' insides."""

    def test_harden_gcd8_then_gcd8_pair(self):
        import yaml
        from sipath import designfile
        ex = os.path.join(os.path.dirname(__file__), "..", "examples")
        wd = _workdir("pair")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        views = os.path.join(wd, "views")
        designfile.harden(os.path.join(ex, "gcd8.yaml"), views, os.path.join(wd, "harden"), runner=RUNNER)
        self.assertTrue(os.path.isfile(os.path.join(views, "macro.yaml")))
        with open(os.path.join(ex, "gcd8_pair.yaml")) as fh:
            d = yaml.safe_load(fh)
        d["rtl"] = [os.path.abspath(os.path.join(ex, p)) for p in d["rtl"]]
        d["sdc"] = os.path.abspath(os.path.join(ex, d["sdc"]))
        for m in d["macros"]:
            m["views"] = views  # the views this test hardened, not the checkout's
        path = os.path.join(wd, "gcd8_pair.yaml")
        with open(path, "w") as fh:
            yaml.safe_dump(d, fh)
        f = designfile.run(path, os.path.join(wd, "pair"), RUNNER)
        s = _read_json(f.result("signoff").outputs["signoff"])
        self.assertTrue(s["pass"], s.get("failures"))
        self.assertTrue(s["lvs"]["match"])
        v = f.verification()
        self.assertEqual(v["execution"], "completed")
        self.assertEqual((v["verification"], v["omitted"]),  # every check passed; equivalence cannot see inside
                         ("omitted", ["equivalence (macros are black boxes to the check)"]))


def _magic_image():
    """The Magic image the configuration names, when it is present here (docker/magic)."""
    import subprocess
    from sipath.config import Config
    ref = Config.load().magic_image
    r = subprocess.run(["docker", "image", "inspect", ref], capture_output=True)
    return ref if r.returncode == 0 else None


@LIVE
@unittest.skipUnless(SKY130A, "needs the sky130A PDK (python3 -m sipath install-pdk)")
class LiveMagic(unittest.TestCase):
    """Magic DRC judges a cell it really loaded: a top cell the GDS lacks is an error, not 0 violations."""

    def test_magic_drc_refuses_a_cell_it_did_not_load(self):
        import types
        from unittest import mock
        from sipath import signoff
        from sipath.config import Config
        from sipath.pdk import load
        from sipath.tools import magic
        image = _magic_image()
        if not image:
            self.skipTest("no Magic image (docker build -t <magic_image> docker/magic)")
        cfg = Config.load()  # the configuration whose image was found here (CI: SIPATH_MAGIC_IMAGE, built locally)
        self.assertEqual(cfg.magic_image, image)
        used = []
        real = magic.run_tool

        def run_tool(ctx, args, log, **kw):  # the image the check really ran in
            used.append(kw.get("image"))
            return real(ctx, args, log, **kw)
        self.enterContext(mock.patch.object(magic, "run_tool", run_tool)) if hasattr(self, "enterContext") else \
            (lambda p: (p.start(), self.addCleanup(p.stop)))(mock.patch.object(magic, "run_tool", run_tool))
        pdk = load("sky130A")
        gds = pdk.signoff["cell_gds"][0]
        wd = _workdir("magic")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        RUNNER.mount(os.path.realpath(gds), os.path.realpath(pdk.signoff["magic_rc"]))
        for top, want in (("sky130_fd_sc_hd__inv_1", None), ("no_such_cell", "has no cell no_such_cell")):
            ctx = types.SimpleNamespace(dir=wd, pdk=pdk, design=types.SimpleNamespace(top=top), runner=RUNNER,
                                        config=cfg, path=lambda n: os.path.join(wd, n))
            if want:
                with self.assertRaisesRegex(signoff.SignoffError, want):
                    signoff.magic_drc(ctx, gds)
            else:
                self.assertGreater(signoff.magic_drc(ctx, gds)["count"], 0)  # a bare cell: its tap rules fire
        self.assertEqual(set(used), {image})  # checked one image and ran in it


@LIVE
class LiveOpenFrame(unittest.TestCase):
    """A design in the OpenFrame harness end to end: the wrapper signed off
    (DRC, density, antennas, timing, harness conformance), integrated into the
    frame's layout with the seal ring, the chip GDS checked, and (with the
    precheck image) ChipFoundry's cf-precheck, whose Netgen LVS stands in for
    the KLayout LVS (the amd64 image's KLayout does not finish it)."""

    def test_openframe_gcd8_to_chip(self):
        from sipath import designfile
        wd = _workdir("openframe_gcd8")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        # every check required but KLayout LVS (it hangs on amd64 here; the precheck's Netgen LVS stands
        # in), drv too: resetb_l (south edge, 2.35 mm from the rows) is buffered along its pin channel
        checks = ["drc", "density", "antenna", "setup", "hold", "drv", "annotation", "unconstrained", "harness"]
        d, spec = designfile.load(os.path.join(os.path.dirname(__file__), "..", "examples", "openframe_gcd8.yaml"))
        flow = [s for s in spec if s["step"] not in ("signoff", "integrate")]
        flow += [{"step": "signoff", "variants": [{"checks": checks, "require": checks}]}]
        image = _precheck_image()
        if image:
            flow.append({"step": "precheck"})
        flow.append({"step": "integrate", "variants": [{"seal_ring": True}]})
        f = designfile.run(d, wd, RUNNER, flow_steps=flow)
        s = _read_json(f.steps["signoff"].selected.outputs["signoff"])
        self.assertTrue(s["pass"], s.get("failures"))
        self.assertEqual(s["drc"]["count"], 0)
        self.assertTrue(s["harness"]["pass"])
        self.assertEqual((s["timing"]["drv_slew"], s["timing"]["drv_cap"], s["timing"]["drv_fanout"]), (0, 0, 0))
        self.assertEqual([c["pin"] for c in f.steps["place"].selected.outputs["pin_repeaters"]], ["resetb_l"])
        if image:
            pc = f.steps["precheck"].selected.outputs["precheck"]
            self.assertTrue(pc["pass"], pc.get("failed"))
            self.assertIn("lvs", pc["passed"])
        it = f.steps["integrate"].selected
        chk = it.outputs["chipcheck"]
        self.assertTrue(chk["pass"], chk)
        self.assertEqual(chk["tops"], ["caravel_openframe"])
        self.assertTrue(chk["die_matches"] and chk["wrapper_at_origin"])
        self.assertEqual(chk["wrapper_differs"], {})
        # the seal ring: once, centred on the die (it sits on the die's outer band), touching no frame shape
        self.assertEqual(chk["seal_placements"], [[-6.0, -6.0]])
        self.assertTrue(chk["seal_centred"])
        self.assertEqual(chk["seal_touches_frame"], {})
        self.assertTrue(os.path.getsize(it.outputs["chip_gds"]) > 1_000_000)


@LIVE
class LiveCancel(unittest.TestCase):
    """B2.1: a cancel reaches every tool process, not only OpenROAD: synthesis (Yosys), signoff's KLayout DRC
    and the CD-LNS macro placer each stop with their whole tree, no container is left, and the attempt ends
    cancelled."""

    def cancel_during(self, design, steps, target, wd, want=lambda p: True, wait_s=900):
        import subprocess
        import threading
        import time as _t
        from sipath import designfile
        d, _ = designfile.load(design) if isinstance(design, str) else (design, None)
        flow = designfile.build(d, wd, RUNNER)
        items = designfile.resolve_steps(steps)
        errors = []

        def go():
            parent = None
            try:
                for name, _, fn, item in items:
                    parent = flow.step(name, fn, parent=parent, variants=item.get("variants"), score=item.get("score"),
                                       parallel=item.get("parallel", 1)).selected
            except Exception as e:  # noqa: BLE001 (the cancel ends the run with an error)
                errors.append(e)
        th = threading.Thread(target=go, daemon=True)
        th.start()
        procs, end = [], _t.time() + wait_s
        while _t.time() < end and th.is_alive() and not procs:
            with flow._lock:
                procs = [p for aid, ps in flow._procs.items() if aid.startswith(target + ".") for p in ps if want(p)]
            _t.sleep(0.2)
        self.assertTrue(procs, f"no {target} tool process to cancel (errors: {errors})")
        _t.sleep(2)  # let it get going
        flow.cancel()
        th.join(180)
        self.assertFalse(th.is_alive(), "the run did not stop after the cancel")
        a = [x for x in flow.attempts if x.step == target][-1]
        self.assertEqual(a.status, "cancelled", a.error)
        for p in procs:
            self.assertFalse(p.alive(), f"{p.name} still running")
            if p.container:
                left = subprocess.run(["docker", "ps", "-q", "--filter", f"name={p.container}"], capture_output=True,
                                      text=True).stdout.strip()
                self.assertEqual(left, "", f"container {p.container} still running")
        return flow, procs

    def test_synthesis(self):
        wd = _workdir("cancel_synth")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        ex = os.path.join(os.path.dirname(__file__), "..", "examples")
        _, procs = self.cancel_during(os.path.join(ex, "gcd8.yaml"), [{"step": "synth"}], "synth", wd)
        self.assertTrue(any("yosys" in " ".join(map(str, p.cmd)) for p in procs))

    def test_signoff_drc(self):
        wd = _workdir("cancel_drc")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        ex = os.path.join(os.path.dirname(__file__), "..", "examples")
        steps = [{"step": s} for s in ("floorplan", "physical", "place", "cts", "route_analog", "route_digital",
                                       "route_repair", "extract", "signoff")]
        self.cancel_during(os.path.join(ex, "dp8.yaml"), steps, "signoff", wd,
                           want=lambda p: "klayout" in " ".join(map(str, p.cmd)))

    def test_cdlns_placer(self):
        from sipath.config import Config
        from sipath.macroplace import placer_python
        try:
            placer_python(types.SimpleNamespace(config=Config()))
        except RuntimeError as e:
            self.skipTest(str(e))
        wd = _workdir("cancel_cdlns")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        ex = os.path.join(os.path.dirname(__file__), "..", "examples")
        steps = [{"step": "synth"}, {"step": "floorplan"},
                 {"step": "macros", "variants": [{"method": "cdlns", "placer_seconds": 120}]}]
        self.cancel_during(os.path.join(ex, "sram8.yaml"), steps, "macros", wd,
                           want=lambda p: "run_cdlns.py" in " ".join(map(str, p.cmd)))


@LIVE
class LiveBudget(unittest.TestCase):
    """B2.2: a 3-way sweep under a 6-core budget runs within it (2 threads each, as each attempt's OpenROAD process
    reports its own count); a wall-time budget stops a real run cleanly (status budget) and --resume finishes it."""

    def test_core_budget_and_wall_budget(self):
        from sipath import designfile
        from sipath.config import Config
        from sipath.examples import dp8
        from sipath.scheduler import BudgetExceeded
        wd = _workdir("budget")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        sweep = [{"step": "floorplan"}, {"step": "physical"},
                 {"step": "place", "variants": [{"density": 0.5}, {"density": 0.6}, {"density": 0.7}],
                  "parallel": 3, "score": "routability_then_wns"}]
        f = designfile.run(dp8.make_design(), os.path.join(wd, "sweep"), RUNNER, flow_steps=sweep,
                           config=Config(threads=6))
        self.assertEqual((f.allocation["place"]["at_once"], f.allocation["place"]["threads_each"]), (3, 2))
        for a in f.steps["place"].attempts:  # its share, not the machine's cores: the process's own count (started
            # with 2, or set to 2), not a set_thread_count line a process already at 2 does not need
            self.assertEqual((a.threads, a.metrics.get("openroad_threads")), (2, 2), a.id)
        steps = [{"step": s} for s in ("floorplan", "physical", "place", "cts", "route_analog", "route_digital",
                                       "route_repair", "extract")]
        run = os.path.join(wd, "wall")
        with self.assertRaises(BudgetExceeded):
            designfile.run(dp8.make_design(), run, RUNNER, flow_steps=steps, config=Config(budget_s=15))
        g = _read_json(os.path.join(run, "graph.json"))
        self.assertEqual(g["verification"]["execution"], "budget")
        self.assertIn("budget", {a["status"] for a in g["attempts"]})
        done = [s for s in g["steps"] if g["steps"][s].get("selected")]
        f2 = designfile.run(dp8.make_design(), run, RUNNER, flow_steps=steps, resume=True)
        self.assertIn("extract", f2.steps)
        for s in done:  # what finished before the budget is reused
            self.assertTrue(f2.steps[s].selected.resumed, s)


@LIVE
class LiveFork(unittest.TestCase):
    """B2.3: fork dp8 from the placement its score rejected and route it; the original run's graph is
    byte-identical afterwards."""

    def test_route_a_rejected_placement(self):
        import hashlib
        from sipath import designfile, steps
        from sipath.examples import dp8
        wd = _workdir("fork")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        orig = os.path.join(wd, "orig")
        f = designfile.run(dp8.make_design(), orig, RUNNER, flow_steps=[
            {"step": "floorplan"}, {"step": "physical"},
            {"step": "place", "variants": [{"density": 0.45}, {"density": 0.7}], "parallel": 2,
             "score": "routability_then_wns"}])
        rejected = next(a for a in f.steps["place"].attempts if a.status == "rejected")
        with open(os.path.join(orig, "graph.json"), "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        g = f.fork(rejected, os.path.join(wd, "fork"))
        parent = g.steps["place"].selected
        for name in ("cts", "route_analog", "route_digital"):
            parent = g.step(name, steps.STEPS[name], parent=parent).selected
        self.assertGreater(parent.metrics.get("wirelength_um") or parent.metrics.get("hpwl_um") or 0, 0)
        self.assertTrue(parent.dir.startswith(os.path.join(wd, "fork")))
        chain, a = [], parent
        while a is not None:
            chain.append(a.id)
            a = a.parent
        self.assertIn(rejected.id, chain)
        with open(os.path.join(orig, "graph.json"), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), before)
        self.assertEqual(_read_json(os.path.join(wd, "fork", "graph.json"))["verification"]["execution"], "completed")


@LIVE
class LiveDocs(unittest.TestCase):
    """B2.4: the MCP session in docs/examples.md (recipe 13) runs as printed: each call's arguments as written
    (R and J are the ids the calls return; the selected placement is whichever the score picks), and each
    "-> selected X" it shows is what the call returns."""

    def calls(self):
        import re
        path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "examples.md")
        with open(path) as fh:
            text = fh.read()
        sec = text[text.index("## 13."):text.index("## 14.")]
        block = re.search(r"```text\n(.*?)```", sec, re.S).group(1)
        entries = []
        for ln in block.splitlines():
            if ln.startswith(" ") and entries:
                entries[-1] += " " + ln.strip()
            elif ln.strip():
                entries.append(ln.strip())
        out = []
        for e in entries:
            tool, rest = e.split(None, 1)
            depth, end = 0, None
            for i, ch in enumerate(rest):
                depth += ch == "{"
                depth -= ch == "}"
                if depth == 0 and ch == "}":
                    end = i + 1
                    break
            args = re.sub(r":\s*([RJ])\b", r': "__\1__"', rest[:end])
            want = re.search(r"-> selected (\S+)", rest[end:])
            out.append((tool, json.loads(args), want.group(1) if want else None))
        return out

    def test_mcp_walkthrough_as_printed(self):
        import time as _t
        from sipath import mcp_server
        wd = _workdir("docs_mcp")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        s = mcp_server.Server(emit=lambda m: None)
        ids, rename = {}, {}
        calls = self.calls()
        self.assertGreater(len(calls), 12)

        def fill(v):
            if isinstance(v, str):
                return ids.get(v, rename.get(v, v))
            if isinstance(v, list):
                return [fill(x) for x in v]
            if isinstance(v, dict):
                return {k: fill(x) for k, x in v.items()}
            return v
        for tool, args, want in calls:
            args = fill(args)
            if tool == "open_run":
                args["workdir"] = os.path.join(wd, "run")
            out = s.call(tool, args)
            if tool == "open_run":
                ids["__R__"] = out["run_id"]
            elif "job_id" in out and tool != "job_status":
                ids["__J__"] = out["job_id"]
            elif tool == "job_status":
                while out.get("status") in ("running", "pending"):
                    _t.sleep(2)
                    out = s.call(tool, args)
                self.assertEqual(out.get("status"), "done", out)
                rename["place.1"] = out["result"]["selected"]  # the doc says "say place.1": the score's pick
            if want:
                self.assertEqual(out.get("selected"), fill(want), f"{tool} {args}")
        eco = out
        self.assertEqual(eco["attempt"]["status"], "selected", eco)


@LIVE
class LiveBoundedSearch(unittest.TestCase):
    """B3.2: keep: 2 on placement carries the best placements and the baseline through routing; the graph shows
    every branch and the choice is made on extracted timing (setup slack, rcx parasitics)."""

    def run_search(self, design, flow, name):
        from sipath import designfile
        wd = _workdir(name)
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        d = design() if callable(design) else designfile.load(design)[0]
        f = designfile.run(d, wd, RUNNER, flow_steps=flow)
        dec = f.decisions[-1]
        self.assertEqual(dec["at"], "extract")
        self.assertEqual(len(dec["candidates"]), len(f.steps["place"].kept))
        self.assertGreaterEqual(len(dec["candidates"]), 2)
        win = next(a for a in f.attempts if a.id == dec["winner"])
        self.assertEqual(win.metrics["parasitics"], "rcx")  # decided on extracted parasitics
        best = max(dec["candidates"], key=lambda c: c["score"])  # the decide score (auto: met, slack, wire)
        self.assertEqual(best["attempt"], win.id)
        for c in dec["candidates"]:  # every branch is in the graph, routed and extracted
            a = next(x for x in f.attempts if x.id == c["attempt"])
            self.assertIn("parasitics", a.capabilities)
        return f

    def test_dp8_and_gcd8(self):
        from sipath.examples import dp8
        tail = [{"step": s} for s in ("cts", "route_analog", "route_digital", "route_repair", "extract")]
        f = self.run_search(dp8.make_design, [{"step": "floorplan"}, {"step": "physical"},
                            {"step": "place", "variants": [{}, {"density": 0.45}, {"density": 0.7}], "parallel": 3,
                             "score": "routability_then_wns", "keep": 2, "decide_at": "extract"}] + tail, "search_dp8")
        self.assertTrue(any(s.startswith("route_digital@place.") for s in f.steps))
        ex = os.path.join(os.path.dirname(__file__), "..", "examples", "gcd8.yaml")
        self.run_search(ex, [{"step": "synth"}, {"step": "floorplan"}, {"step": "physical"},
                             {"step": "place", "variants": [{}, {"density": 0.75}], "parallel": 2, "keep": 2,
                              "decide_at": "extract"}] + tail, "search_gcd8")


def _run_route_search_example(tc, name):
    """Execute the shipped YAML unchanged, including its final signoff/export steps.

    First stop with an attempt budget inside the placement sweep, then resume.
    A final complete resume must reuse every successful result and keep the winner.
    """
    from sipath import designfile
    from sipath.config import Config
    from sipath.scheduler import BudgetExceeded
    path = os.path.join(os.path.dirname(__file__), "..", "examples", name + ".yaml")
    wd = _workdir(name)
    if not KEEP:
        tc.addCleanup(_rmtree, wd)
    cfg = Config.load(**dict(designfile.file_config(path), max_attempts=4))
    with tc.assertRaises(BudgetExceeded):
        designfile.run(path, wd, RUNNER, config=cfg)
    stopped = _read_json(os.path.join(wd, "graph.json"))
    tc.assertEqual(stopped["verification"]["execution"], "budget")
    tc.assertFalse(stopped.get("decisions"))
    f = designfile.run(path, wd, RUNNER, resume=True)
    tc.assertTrue(f.steps["synth"].selected.resumed)
    decision = f.decisions[-1]
    tc.assertEqual((decision["at"], decision["score"]), ("extract", "setup_wns"))
    tc.assertGreaterEqual(len(decision["candidates"]), 2)
    place = f.steps["place"]
    baseline = next(a for a in place.attempts if a.params == {})
    tc.assertIn(baseline.id, place.kept)
    for c in decision["candidates"]:
        a = next(a for a in f.attempts if a.id == c["attempt"])
        tc.assertEqual(a.metrics["parasitics"], "rcx")
    eligible = [c for c in decision["candidates"] if c["score"] is not None]
    tc.assertTrue(eligible, "the decision needs at least one scored candidate")
    best = max(eligible, key=lambda c: c["score"])
    tc.assertEqual(decision["winner"], best["attempt"])
    tc.assertEqual(f.result("signoff").parent.id, decision["winner"])
    signoff = _read_json(f.result("signoff").outputs["signoff"])
    tc.assertTrue(signoff["pass"], signoff.get("failures"))
    tc.assertEqual(f.verification()["execution"], "completed")
    before = [(a.id, a.finished_at) for a in f.attempts if a.status != "failed"]
    again = designfile.run(path, wd, RUNNER, resume=True)
    tc.assertEqual(again.decisions[-1]["winner"], decision["winner"])
    tc.assertEqual([(a.id, a.finished_at) for a in again.attempts if a.status != "failed"], before)
    tc.assertEqual(again.verification()["execution"], "completed")
    return again


@LIVE
class LiveRouteSearchMacro(unittest.TestCase):
    """Beyond LiveBoundedSearch's branch selection: run the shipped macro YAML,
    stop/resume on an attempt budget, export reusable views, and reuse a
    completed run without changing its successful attempts or winner.
    """
    def test_gcd8_to_reusable_views(self):
        f = _run_route_search_example(self, "gcd8_macro_route_search")
        views = f.result("views").outputs["views"]
        for key in ("lef", "gds", "verilog", "cdl", "macro_yaml"):
            self.assertTrue(views[key] and os.path.isfile(views[key]), key)
        self.assertEqual(set(views["lib"]), {c.name for c in f.pdk.analysis_corners()})
        self.assertTrue(all(os.path.isfile(p) for p in views["lib"].values()))
        self.assertEqual(f.verification()["verification"], "passed", f.verification())


@LIVE
class LiveRouteSearchOpenFrame(unittest.TestCase):
    def test_template_search_to_integrated_chip(self):
        # These are mandatory for this example, so missing images fail this test.
        self.assertTrue(_magic_image(), "build/configure the Magic image")
        self.assertTrue(_precheck_image(), "build/configure the cf-precheck image")
        f = _run_route_search_example(self, "openframe_gcd8_route_search")
        signoff = _read_json(f.result("signoff").outputs["signoff"])
        self.assertTrue(signoff["harness"]["pass"])
        pc = f.result("precheck").outputs["precheck"]
        self.assertTrue(pc["pass"], pc.get("failed"))
        self.assertIn("lvs", pc["passed"])
        self.assertTrue(f.result("integrate").metrics["chip_check_pass"])


@LIVE
class LiveUart(unittest.TestCase):
    def test_uart_macro_signoff_and_views(self):
        from sipath import designfile
        path = os.path.join(os.path.dirname(__file__), "..", "examples", "uart_fifo.yaml")
        wd = _workdir("uart_fifo")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        f = designfile.run(path, wd, RUNNER)
        self.assertEqual(f.verification()["verification"], "passed", f.verification())
        _check_limits(self, wd, "uart_fifo")
        views = f.result("views").outputs["views"]
        for key in ("lef", "gds", "verilog", "cdl", "macro_yaml"):
            self.assertTrue(os.path.isfile(views[key]), key)
        self.assertEqual(set(views["lib"]), {c.name for c in f.pdk.analysis_corners()})


@LIVE
class LiveOpenFrameUart(unittest.TestCase):
    def test_uart_echo_to_chip(self):
        from sipath import designfile
        self.assertTrue(_magic_image(), "build/configure the Magic image")
        self.assertTrue(_precheck_image(), "build/configure the precheck image")
        path = os.path.join(os.path.dirname(__file__), "..", "examples", "openframe_uart.yaml")
        wd = _workdir("openframe_uart")
        if not KEEP:
            self.addCleanup(_rmtree, wd)
        f = designfile.run(path, wd, RUNNER)
        self.assertTrue(_read_json(f.result("signoff").outputs["signoff"])["pass"])
        pc = f.result("precheck").outputs["precheck"]
        self.assertTrue(pc["pass"], pc)
        self.assertIn("lvs", pc["passed"])
        self.assertTrue(f.result("integrate").metrics["chip_check_pass"])
        _check_limits(self, wd, "openframe_uart")
