"""Functional RTL test hook for the GCD8 block and OpenFrame wrapper (Icarus required).

python3 -m unittest -v sipath.tests.test_route_search
Plan tests live in test_unit.RouteSearchPlans.
Physical verification lives in LiveRouteSearchMacro and LiveRouteSearchOpenFrame.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


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
