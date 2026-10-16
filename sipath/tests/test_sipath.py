"""Every SiPath test: the unit tests (test_unit) and the live ones (test_live).

    python3 -m unittest sipath.tests.test_sipath      all
    python3 -m unittest sipath.tests.test_unit        unit only (no OpenROAD; under a minute)
    python3 -m sipath.tests.run_live [-j N]           live classes in parallel processes, with times
"""
from .test_live import *  # noqa: F401,F403
from .test_unit import *  # noqa: F401,F403
