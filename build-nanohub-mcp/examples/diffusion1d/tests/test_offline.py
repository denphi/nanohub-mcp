"""Offline tests for the diffusion1d example — no hub, no session, no solver
binary. Import errors FAIL loudly (a silently-skipped suite reports green
while testing nothing — see verification.md).

Run:  python3 -m pytest tests/ -q
"""

import importlib.util
import os
import secrets
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(_HERE, os.pardir, "bin", "diffusion1d.py")

spec = importlib.util.spec_from_file_location("diffusion1d", _APP)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

try:
    from jsonschema import Draft202012Validator, validate
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


def _tool(name):
    return mod.server._tools[name]


class TestContracts(unittest.TestCase):
    def test_every_tool_has_description_and_valid_schemas(self):
        for name, entry in mod.server._tools.items():
            d = entry["definition"].to_dict()
            self.assertTrue((d.get("description") or "").strip(), name)
            if HAVE_JSONSCHEMA:
                Draft202012Validator.check_schema(d["inputSchema"])
                if d.get("outputSchema"):
                    Draft202012Validator.check_schema(d["outputSchema"])

    def test_run_simulation_is_async(self):
        self.assertTrue(_tool("run_simulation").get("is_async"))


class TestPhysics(unittest.TestCase):
    """Defaults are physics: the zero-argument workflow must be correct."""

    @classmethod
    def setUpClass(cls):
        cls.created = _tool("create_diffusion_sim")["handler"]()
        cls.run_handle = cls.created["run_handle"]
        # Calling the async handler directly runs it synchronously — the
        # framework's thread/task machinery is not what we test here.
        cls.run_result = _tool("run_simulation")["handler"](cls.run_handle)

    @classmethod
    def tearDownClass(cls):
        _tool("delete_run")["handler"](cls.run_handle)

    def test_outputs_validate_against_schemas(self):
        if not HAVE_JSONSCHEMA:
            self.skipTest("jsonschema not installed")
        validate(self.created,
                 _tool("create_diffusion_sim")["definition"].to_dict()["outputSchema"])
        validate(self.run_result,
                 _tool("run_simulation")["definition"].to_dict()["outputSchema"])

    def test_run_finished_without_warnings(self):
        self.assertEqual(self.run_result["status"], "finished")
        self.assertEqual(self.run_result.get("warnings"), [])

    def test_mass_is_conserved(self):
        hist = _tool("get_peak_history")["handler"](self.run_handle)
        mass = hist["total_mass"]
        self.assertAlmostEqual(mass[-1] / mass[0], 1.0, places=6)

    def test_peak_decays_monotonically(self):
        hist = _tool("get_peak_history")["handler"](self.run_handle)
        peaks = hist["peak_concentration"]
        self.assertLess(peaks[-1], 0.9 * peaks[0])
        self.assertTrue(all(b <= a * (1 + 1e-12)
                            for a, b in zip(peaks, peaks[1:])))

    def test_profile_is_decimated(self):
        profile = _tool("get_concentration_profile")["handler"](
            self.run_handle, max_points=50)
        self.assertLessEqual(len(profile["x_um"]), 50)
        self.assertEqual(len(profile["x_um"]), len(profile["concentration"]))


class TestErrorPaths(unittest.TestCase):
    def test_readers_reject_unknown_run_handle(self):
        for name in ("get_concentration_profile", "get_peak_history",
                     "run_simulation"):
            result = _tool(name)["handler"]("../diffusion1d_escape")
            self.assertTrue(getattr(result, "is_error", False), name)

    def test_delete_refuses_foreign_paths(self):
        result = _tool("delete_run")["handler"]("/tmp")
        self.assertTrue(getattr(result, "is_error", False))

    def test_create_rejects_bad_inputs(self):
        result = _tool("create_diffusion_sim")["handler"](duration_s=-1)
        self.assertTrue(getattr(result, "is_error", False))

    def test_create_rejects_runaway_work(self):
        result = _tool("create_diffusion_sim")["handler"](points=5001)
        self.assertTrue(getattr(result, "is_error", False))

    def test_create_rejects_nonfinite_and_wrong_types(self):
        for kwargs in ({"sigma_um": 0}, {"duration_s": float("nan")},
                       {"points": True}):
            result = _tool("create_diffusion_sim")["handler"](**kwargs)
            self.assertTrue(getattr(result, "is_error", False), kwargs)

    def test_invalid_handle_error_does_not_reflect_input(self):
        marker = "IGNORE_PREVIOUS_INSTRUCTIONS"
        result = _tool("get_peak_history")["handler"](marker)
        self.assertTrue(getattr(result, "is_error", False))
        self.assertNotIn(marker, getattr(result, "content", ""))

    def test_symlink_handle_is_rejected(self):
        handle = "diffusion1d_" + secrets.token_hex(12)
        link = os.path.join(mod._RUN_ROOT, handle)
        os.symlink("/tmp", link)
        try:
            result = _tool("get_peak_history")["handler"](handle)
            self.assertTrue(getattr(result, "is_error", False))
        finally:
            os.unlink(link)

    def test_delete_refuses_active_run(self):
        created = _tool("create_diffusion_sim")["handler"]()
        handle = created["run_handle"]
        lock = mod._lock_for(handle)
        lock.acquire()
        try:
            result = _tool("delete_run")["handler"](handle)
            self.assertTrue(getattr(result, "is_error", False))
        finally:
            lock.release()
        result = _tool("delete_run")["handler"](handle)
        self.assertEqual(result["status"], "deleted")


if __name__ == "__main__":
    unittest.main()
