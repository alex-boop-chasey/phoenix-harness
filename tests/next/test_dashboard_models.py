"""Dashboard models route (Task 26): the view mirrors models.effective_all; the POST validates before any CLI call
and returns the CLI's probe failure as a ValueError (HTTP 400)."""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hands import dashboard, models


class ModelsRoute(unittest.TestCase):
    def setUp(self):
        self.sd = Path(tempfile.mkdtemp(prefix="hands-dash-models-"))
        self.addCleanup(shutil.rmtree, self.sd, True)
        self.cfg = SimpleNamespace(name="demo", state_dir=self.sd, coder_model="", planner_model="", eval_model="",
                                   fallback_model="", resolver_model="")
        patches = [mock.patch.object(dashboard, "load_cfg", lambda n: self.cfg),
                   mock.patch.object(models, "models_file", lambda c: self.sd / "demo.models.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_view_marks_pending_only_while_alive(self):
        (self.sd / "state.json").write_text(json.dumps({"current": {"models": {"coder": "claude-haiku-5-5"}}}))
        (self.sd / "metrics.jsonl").write_text(json.dumps({"model": "claude-fable-5-1", "cost_usd": 1.5}) + "\n")
        with mock.patch.object(dashboard.Lock, "held", return_value=True):
            view = dashboard.models_view("demo")
        rows = {r["role"]: r for r in view["roles"]}
        self.assertEqual(list(rows), list(models.ROLES))
        self.assertTrue(rows["coder"]["pending"])
        self.assertFalse(rows["evaluator"]["pending"])
        self.assertEqual([a["id"] for a in view["allowlist"]], list(models.ALLOWLIST))
        self.assertEqual(view["usage"], [{"model": "claude-fable-5-1", "sessions": 1, "cost_usd": 1.5}])
        with mock.patch.object(dashboard.Lock, "held", return_value=False):
            self.assertFalse(any(r["pending"] for r in dashboard.models_view("demo")["roles"]))

    def test_post_validates_before_calling_the_cli(self):
        with mock.patch.object(dashboard.subprocess, "run") as run:
            for req in ({"role": "coder", "model": "gpt-9"}, {"role": "x", "model": "claude-opus-5-5"},
                        {"role": "fallback", "model": "claude-opus-5-5", "effort": "high"}):
                with self.assertRaises(ValueError):
                    dashboard.models_post("demo", req)
            run.assert_not_called()

    def test_post_returns_probe_failure_as_error(self):
        done = SimpleNamespace(returncode=1, stdout="probe failed for claude-haiku-5-5: model_access\n", stderr="")
        with mock.patch.object(dashboard.subprocess, "run", return_value=done) as run:
            with self.assertRaisesRegex(ValueError, "probe failed for claude-haiku-5-5"):
                dashboard.models_post("demo", {"role": "coder", "model": "claude-haiku-5-5", "effort": "low", "clear_override": True})
        argv = run.call_args.args[0]
        self.assertEqual(argv[-7:], ["models", "set", "coder", "claude-haiku-5-5", "--effort", "low", "--clear-override"])
        self.assertEqual(run.call_args.kwargs["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
