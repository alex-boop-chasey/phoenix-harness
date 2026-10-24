"""hands.models: allowlist, precedence, atomic writes, check, usage, limit classifier."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from hands import models  # noqa: E402

NOW = 1_800_000_000


class Precedence(unittest.TestCase):
    def eff(self, toml=None, manual=None, state=None, now=NOW):
        return models.effective_all(toml or {}, {"roles": manual or {}}, state or {}, now)

    def test_defaults(self):
        r = self.eff()
        self.assertEqual(r["coder"]["source"], "default")
        self.assertEqual(r["subagents"]["model"], "claude-sonnet-5-5")
        self.assertEqual(r["resolver"]["model"], r["evaluator"]["model"])
        self.assertEqual(r["fallback"]["model"], "claude-opus-5-5")

    def test_order(self):
        toml = {"coder_model": "claude-opus-5-5"}
        man = {"coder": {"model": "claude-sonnet-5-5", "effort": "high"}}
        self.assertEqual(self.eff(toml)["coder"]["source"], "toml")
        self.assertEqual(self.eff(toml)["planner"]["model"], "claude-opus-5-5")
        m = self.eff(toml, man)["coder"]
        self.assertEqual((m["source"], m["effort"]), ("manual", "high"))
        ov = {"model_overrides": {"coder": {"model": "claude-haiku-5-5", "reason": "r", "until": NOW + 5}}}
        self.assertEqual(self.eff(toml, man, ov)["coder"]["source"], "auto-override")
        ov["model_overrides"]["coder"]["until"] = NOW - 1
        e = self.eff(toml, man, ov)["coder"]
        self.assertEqual((e["source"], e["override"]), ("manual", None))
        legacy = self.eff(state={"model_override": "claude-fable-5-1"})
        self.assertEqual(legacy["evaluator"]["source"], "auto-override")
        self.assertNotEqual(legacy["fallback"]["source"], "auto-override")


class Writes(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="models-test-"))
        self.f = self.d / "p.models.json"
        self.audit = self.d / "audit.jsonl"

    def test_set_backup_mode_audit(self):
        models.set_choice(self.f, "coder", "claude-opus-5-5", "high", self.audit, "a")
        r = models.set_choice(self.f, "coder", "claude-sonnet-5-5", "", self.audit, "a")
        self.assertEqual(r["old"], "claude-opus-5-5")
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)
        self.assertIn("claude-opus-5-5", (self.d / "p.models.json.bak").read_text())
        self.assertEqual(len(self.audit.read_text().splitlines()), 2)

    def test_invalid_changes_nothing(self):
        with self.assertRaises(models.InvalidModel):
            models.set_choice(self.f, "coder", "gpt-5", "", self.audit, "a")
        self.assertFalse(self.f.exists())

    def test_write_error_cleans_temp(self):
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                models.set_choice(self.f, "coder", "claude-opus-5-5", "", self.audit, "a")
        self.assertEqual(os.listdir(self.d), [])

    def test_reset(self):
        models.set_choice(self.f, "coder", "claude-opus-5-5", "", self.audit, "a")
        models.reset(self.f, self.audit, "a")
        self.assertEqual(models.load_manual(self.f)["roles"], {})


class Misc(unittest.TestCase):
    def test_validate(self):
        for args in (("coder", "gpt-5"), ("boss", "claude-opus-5-5"), ("coder", "claude-opus-5-5", "ultra"),
                     ("subagents", "claude-haiku-5-5", "high")):
            with self.assertRaises(models.InvalidModel):
                models.validate(*args)
        models.validate("resolver", "claude-sonnet-5-5", "xhigh")

    def test_check_and_usage_and_limit(self):
        self.assertEqual(models.check({"coder_model": "x"}, {"roles": {}}), ["unknown model x for coder"])
        u = models.usage([json.dumps({"model": "claude-opus-5-5", "models": ["claude-opus-5-5"], "cost_usd": 1}), "bad"])
        self.assertEqual(u, [{"model": "claude-opus-5-5", "sessions": 1, "cost_usd": 1}])
        self.assertEqual(models.classify_limit("hit your weekly Opus limit"), {"model_only": True, "family": "opus"})
        self.assertFalse(models.classify_limit("hit your session limit")["model_only"])


if __name__ == "__main__":
    unittest.main()
