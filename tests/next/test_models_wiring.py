"""Models wired into runner sessions: fresh read per session, --effort, subagent model, model_limit, preflight."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import claude, models  # noqa: E402
from test_runner import EVAL_PASS, FAILV, GOOD1, STEP1, Env  # noqa: E402

EVAL_FAIL = {"role": "eval", "text": "no\n" + FAILV}


class Wiring(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="hands-models-home-"))
        p = mock.patch("hands.config.CONFIG_HOME", self.home)
        p.start()
        self.addCleanup(p.stop)
        self.mfile = self.home / "projects" / "t.models.json"

    def save(self, roles):
        self.mfile.parent.mkdir(parents=True, exist_ok=True)
        self.mfile.write_text(json.dumps({"schema": 1, "roles": roles}))

    def cmds(self, e, role):
        return [x["cmd"] for x in e.prompt_records() if x["role"] == role]

    def test_change_between_attempts_applies_to_the_second_only(self):
        e = Env([STEP1])
        e.queue(GOOD1, EVAL_FAIL, GOOD1, EVAL_PASS)
        r = e.runner()
        orig = r.record_session
        done = []

        def record(role, *a, **kw):
            orig(role, *a, **kw)
            if role == "eval" and not done:
                done.append(1)
                self.save({"coder": {"model": "claude-sonnet-5-5", "effort": "high"}})
        r.record_session = record
        self.assertEqual(r.run(), 0)
        coder = self.cmds(e, "coder")
        self.assertEqual(coder[0][coder[0].index("--model") + 1], "claude-fable-5-1")
        self.assertNotIn("--effort", coder[0])
        self.assertEqual(coder[1][coder[1].index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(coder[1][coder[1].index("--effort") + 1], "high")

    def test_agents_json_carries_the_subagent_model(self):
        self.save({"subagents": {"model": "claude-haiku-5-5", "effort": ""}})
        e = Env([STEP1])
        e.queue(GOOD1, EVAL_PASS)
        self.assertEqual(e.runner().run(), 0)
        cmd = self.cmds(e, "coder")[0]
        agents = json.loads(cmd[cmd.index("--agents") + 1])
        self.assertEqual({a["model"] for a in agents.values()}, {"claude-haiku-5-5"})

    def test_metrics_and_current_record_model_and_effort(self):
        self.save({"coder": {"model": "claude-opus-5-5", "effort": "low"}})
        e = Env([STEP1])
        e.queue(GOOD1, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        rec = [m for m in r.state.read_jsonl("metrics.jsonl") if m["role"] == "coder"][0]
        self.assertEqual((rec["model"], rec["effort"]), ("claude-opus-5-5", "low"))

    def test_model_only_limit_sets_override_with_until_and_retries(self):
        e = Env([STEP1])
        e.queue({"role": "coder", "text": "You've hit your fable limit · resets 2099-01-01T00:00:00Z", "is_error": True, "rc": 1},
                GOOD1, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        coder = self.cmds(e, "coder")
        self.assertEqual(coder[-1][coder[-1].index("--model") + 1], "claude-opus-5-5")
        ov = r.state.data["model_overrides"]["coder"]
        self.assertEqual(ov["model"], "claude-opus-5-5")
        self.assertIn("fable", ov["reason"])
        self.assertGreater(ov["until"], 4_000_000_000)
        self.assertEqual(r.state.data["steps"]["S01"]["attempts"], 1)

    def test_expired_overrides_are_dropped_at_session_start(self):
        e = Env([STEP1])
        r = e.runner()
        r.state.data["model_overrides"] = {"coder": {"model": "claude-opus-5-5", "reason": "x", "until": 5}}
        self.assertEqual(r.session_model("coder")[0], "claude-fable-5-1")
        self.assertNotIn("coder", r.state.data["model_overrides"])

    def test_unknown_model_is_a_preflight_problem(self):
        e = Env([STEP1], coder_model="claude-foo-1")
        with self.assertRaises(SystemExit) as cm:
            e.runner().preflight()
        self.assertIn("unknown model claude-foo-1 for coder", str(cm.exception))

    def test_classify_kind(self):
        res = claude.Result(False, 1, True, "success", "You've hit your opus limit", 0, 0, [], "", {"a": 1})
        self.assertEqual(res.kind, "model_limit")
        res.text = "You've hit your usage limit"
        self.assertEqual(res.kind, "rate_limit")


if __name__ == "__main__":
    unittest.main()
