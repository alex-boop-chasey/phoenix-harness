"""Dashboard resolver route (Task 29): the view reads resolver.json, resolver.jsonl and the pending retry;
the POST only accepts a boolean and runs `hands <p> resolver on|off`."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hands import dashboard


class ResolverRoute(unittest.TestCase):
    def setUp(self):
        self.sd = Path(tempfile.mkdtemp(prefix="hands-dash-resolver-"))
        self.addCleanup(shutil.rmtree, self.sd, True)
        self.cfg = SimpleNamespace(name="demo", state_dir=self.sd, is_self=True)
        p = mock.patch.object(dashboard, "load_cfg", lambda n: self.cfg)
        p.start()
        self.addCleanup(p.stop)

    def test_default_view_is_off_and_empty(self):
        v = dashboard.resolver_view("demo")
        self.assertFalse(v["enabled"])
        self.assertTrue(v["is_self"])
        self.assertEqual((v["history"], v["pending"], v["runs_today"]), ([], None, 0))

    def test_view_lists_newest_first_with_pending_and_runs_today(self):
        now = time.time()
        (self.sd / "resolver.json").write_text(json.dumps({"schema": 1, "enabled": True, "self_enabled": False, "daily_cap": 6}))
        (self.sd / "state.json").write_text(json.dumps({"status": "paused_resolver", "resolver_pending": {"step": "S1", "resume_at": 5}}))
        rows = [{"at": now - 100000, "step": "S1", "cause": "same_failure", "action": "hint_and_retry", "executed": True},
                {"at": now - 10, "step": "S2", "cause": "runner_error", "action": "escalate_to_alex", "executed": True,
                 "reason": "r", "note": "n", "root_command": "ls"}]
        (self.sd / "resolver.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        v = dashboard.resolver_view("demo")
        self.assertTrue(v["enabled"])
        self.assertEqual([h["step"] for h in v["history"]], ["S2", "S1"])
        self.assertEqual(v["history"][0]["root_command"], "ls")
        self.assertEqual(v["runs_today"], 1)
        self.assertEqual(v["pending"]["step"], "S1")

    def test_post_needs_a_boolean_and_runs_the_cli(self):
        with mock.patch.object(dashboard.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                dashboard.resolver_post("demo", {"enabled": "yes"})
            run.assert_not_called()
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(dashboard.resolver_post("demo", {"enabled": False}), {"ok": True})
            self.assertEqual(run.call_args[0][0][-2:], ["resolver", "off"])


if __name__ == "__main__":
    unittest.main()
