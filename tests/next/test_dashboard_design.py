"""Dashboard design gates (Task 22): the screenshot route serves sanitised PNGs only; the current gate is the first
unpassed gate step of the spec."""
import shutil
import tempfile
import unittest
from pathlib import Path

from hands import dashboard

PNG = Path(__file__).resolve().parents[2] / "acceptance" / "fixtures" / "c1-media" / "clean.png"


class DesignShot(unittest.TestCase):
    def setUp(self):
        self.sd = Path(tempfile.mkdtemp(prefix="hands-dash-design-"))
        self.addCleanup(shutil.rmtree, self.sd, ignore_errors=True)
        d = self.sd / "design" / "shots" / "S01"
        d.mkdir(parents=True)
        shutil.copy(PNG, d / "directions-1-desktop.png")
        (d / "x.html").write_text("<script>1</script>")

    def test_png_is_served(self):
        data = dashboard.design_shot(self.sd, "S01", "directions-1-desktop.png")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_everything_else_is_not_found(self):
        for step, name in (("S01", "x.html"), ("S01", "missing-desktop.png"), ("..", "directions-1-desktop.png"),
                           ("S01", "../S01/directions-1-desktop.png"), ("S99", "directions-1-desktop.png")):
            with self.assertRaises(LookupError, msg=(step, name)):
                dashboard.design_shot(self.sd, step, name)

    def test_symlink_is_not_found(self):
        link = self.sd / "design" / "shots" / "S01" / "evil-desktop.png"
        link.symlink_to("/etc/passwd")
        with self.assertRaises(LookupError):
            dashboard.design_shot(self.sd, "S01", "evil-desktop.png")


class CurrentGate(unittest.TestCase):
    def test_first_unpassed_gate(self):
        steps = [{"id": "S01"}, {"id": "G01", "type": "human", "gate": "directions"}, {"id": "G02", "type": "human", "gate": "tweaks"}]
        self.assertEqual(dashboard.current_gate(steps, {"S01": {"status": "passed"}}), "G01")
        self.assertEqual(dashboard.current_gate(steps, {"G01": {"status": "passed"}}), "G02")
        self.assertIsNone(dashboard.current_gate(steps, {"G01": {"status": "passed"}, "G02": {"status": "passed"}}))


if __name__ == "__main__":
    unittest.main()
