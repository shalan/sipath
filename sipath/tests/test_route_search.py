"""Route-aware examples: plans and functional RTL (Icarus required for simulation).

python3 -m unittest -v sipath.tests.test_route_search
Physical verification lives in LiveRouteSearchMacro and LiveRouteSearchOpenFrame.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from sipath import designfile

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class RouteSearchPlans(unittest.TestCase):
    def test_both_examples_have_valid_bounded_plans(self):
        for name, tail in (("openframe_gcd8_route_search", ["signoff", "precheck", "integrate"]),
                           ("gcd8_macro_route_search", ["signoff", "views"])):
            with self.subTest(name=name):
                path = EXAMPLES / (name + ".yaml")
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
                    original, _ = designfile.load(str(EXAMPLES / "openframe_gcd8.yaml"))
                    self.assertEqual(d.rtl, original.rtl)
                    self.assertEqual(d.sdc, original.sdc)
                    self.assertEqual(d.harness_options, original.harness_options)
                else:
                    original, _ = designfile.load(str(EXAMPLES / "gcd8.yaml"))
                    self.assertEqual(d.rtl, original.rtl)
                    self.assertEqual(d.sdc, original.sdc)
                    self.assertEqual(d.core_utilization, original.core_utilization)


@unittest.skipUnless(shutil.which("iverilog") and shutil.which("vvp"), "needs Icarus Verilog (iverilog and vvp)")
class RouteSearchRtl(unittest.TestCase):
    def simulate(self, sources, defines=()):
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "sim.vvp")
            cmd = ["iverilog", "-g2012", "-s", "tb", "-o", out, *["-D" + d for d in defines],
                   *[str(EXAMPLES / s) for s in sources]]
            build = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            sim = subprocess.run(["vvp", out], capture_output=True, text=True, timeout=30)
            self.assertEqual(sim.returncode, 0, sim.stdout + sim.stderr)
            self.assertIn("PASS", sim.stdout)

    def test_gcd8_macro(self):
        self.simulate(["rtl/gcd8.v", "tb/gcd8_tb.v"])

    def test_openframe_template(self):
        self.simulate(["rtl/gcd8.v", "rtl/openframe_gcd8.v", "tb/openframe_gcd8_tb.v"],
                      ["OPENFRAME_IO_PADS=44", "GCD_W=4"])
