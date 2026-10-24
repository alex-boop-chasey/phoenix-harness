import json
import tempfile
import unittest
from pathlib import Path

from hands import nextstate, sitecopy


class Validate(unittest.TestCase):
    def test_defaults(self):
        v = sitecopy.validate({})
        self.assertEqual(v, {"schema": 1, "audience": "", "offer": "", "proof": [], "tone_notes": "",
                             "banned_words": [], "directions": 6})

    def test_trims_and_drops_empty_items(self):
        v = sitecopy.validate({"proof": [" a ", "", "  "], "banned_words": ["x ", ""], "audience": " me "})
        self.assertEqual((v["proof"], v["banned_words"], v["audience"]), (["a"], ["x"], "me"))

    def test_limits(self):
        for bad in ({"directions": 1}, {"directions": 9}, {"directions": True}, {"directions": "6"},
                    {"audience": "a" * 501}, {"offer": "o" * 501}, {"tone_notes": "t" * 1001},
                    {"proof": ["p"] * 21}, {"proof": ["p" * 301]}, {"banned_words": ["w"] * 51},
                    {"banned_words": ["w" * 41]}, {"extra": 1}, {"schema": 2}, {"proof": "x"}, []):
            with self.assertRaises(sitecopy.InvalidInputs, msg=str(bad)[:40]):
                sitecopy.validate(bad)

    def test_write_goes_to_the_outbox(self):
        with tempfile.TemporaryDirectory() as d:
            sitecopy.write({"audience": "bakers"}, Path(d))
            data = json.loads((Path(d) / "refs" / "site-inputs.json").read_text())
            self.assertEqual(data["audience"], "bakers")


class NextState(unittest.TestCase):
    def test_set_copy_roundtrip_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "next.json"
            self.assertIsNone(nextstate.load(f)["copy"])
            nextstate.set_copy(f, {"offer": "bread", "directions": 3})
            self.assertEqual(nextstate.load(f)["copy"]["directions"], 3)
            with self.assertRaises(nextstate.InvalidInput):
                nextstate.set_copy(f, {"directions": 12})


class Wiring(unittest.TestCase):
    def test_dashboard_site_inputs_flags(self):
        from hands import dashboard
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sub" / "si.json"
            self.assertEqual(dashboard.site_inputs_flags({}, f), [])
            self.assertEqual(dashboard.site_inputs_flags({"site_inputs": {"offer": "x"}}, f), ["--site-inputs", str(f)])
            self.assertEqual(json.loads(f.read_text())["offer"], "x")
            with self.assertRaises(ValueError):
                dashboard.site_inputs_flags({"site_inputs": {"directions": 99}}, f)

    def test_evaluator_prompt_carries_the_copy_check(self):
        from hands import prompts
        text = prompts.copy_check_section([{"file": "a.html", "line": 3, "rule": "banned", "text": "synergy"}])
        self.assertIn("## Copy check (runner)", text)
        self.assertIn("`a.html:3` banned", text)
        self.assertIn("(no findings)", prompts.copy_check_section([]))


if __name__ == "__main__":
    unittest.main()
