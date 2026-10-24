"""hands.resolver: re-validation in execute, briefing facts."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from hands import resolver  # noqa: E402

NOW = 1_800_000_000


def state():
    return {"status": "escalation", "reason": "x",
            "steps": {"S": {"status": "failed", "attempts": 3, "reasons": ["r"]}}}


class ExecuteRevalidates(unittest.TestCase):
    def test_restart_not_allowed_for_never_auto_cause(self):
        st = state()
        r = resolver.execute(st, "S", "auth", {"valid": True, "action": "restart_after_crash", "reason": "r"}, NOW)
        self.assertFalse(r["executed"])
        self.assertEqual(st["status"], "escalation")
        self.assertEqual(st["steps"]["S"]["attempts"], 3)

    def test_never_auto_cause_cannot_reset(self):
        st = state()
        r = resolver.execute(st, "S", "secret_leak", {"valid": True, "action": "hint_and_retry", "reason": "r", "hint": "h"}, NOW)
        self.assertFalse(r["executed"])
        self.assertEqual(st["steps"]["S"]["attempts"], 3)

    def test_restart_allowed_for_crash_cause(self):
        st = state()
        r = resolver.execute(st, "S", "runner_error", {"valid": True, "action": "restart_after_crash", "reason": "r"}, NOW)
        self.assertTrue(r["executed"])
        self.assertEqual(st["status"], "running")


class Briefing(unittest.TestCase):
    def test_git_log_and_effective_max(self):
        spec = Path(tempfile.mkdtemp())
        (spec / "docs").mkdir()
        (spec / "acceptance").mkdir()
        (spec / "docs" / "conventions.md").write_text("# C\n")
        (spec / "steps.json").write_text(json.dumps({
            "version": 1, "project": "d", "allowed_paths": ["src/**"], "always_refs": ["docs/conventions.md"],
            "steps": [{"id": "S", "title": "T", "goal": "G", "acceptance": ["true"]}]}))
        app = Path(tempfile.mkdtemp())
        for c in (["init", "-q"], ["-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "--allow-empty", "-m", "first commit"]):
            subprocess.run(["git", "-C", str(app)] + c, check=True)
        sd = Path(tempfile.mkdtemp())
        b = resolver.briefing(spec, sd, "S", 1, "attempts_exhausted", "r", "nn", app_dir=app)
        self.assertIn("first commit", b)
        self.assertIn("(max 3)", b)
        self.assertNotIn("default", b.split("## Goal")[0])


if __name__ == "__main__":
    unittest.main()
