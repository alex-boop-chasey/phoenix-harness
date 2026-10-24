"""hands.designloop: staleness after a redo, redo refusal and limits, prompt sections."""
import json
import tempfile
import unittest
from pathlib import Path

from hands import designloop, prompts, shots
from hands import spec as specmod

PNG = Path(__file__).resolve().parents[2] / "acceptance" / "fixtures" / "c1-media" / "clean.png"


def build():
    root = Path(tempfile.mkdtemp())
    spec, state = root / "spec", root / "state"
    (spec / "docs").mkdir(parents=True)
    (spec / "docs" / "conventions.md").write_text("# c\n")
    (spec / "docs" / "design.md").write_text("# d\n")
    (spec / "acceptance").mkdir()
    (spec / "acceptance" / "a.test.mjs").write_text("")
    code = {"id": "S01", "title": "s", "goal": "g", "acceptance": ["node --test acceptance/a.test.mjs"],
            "screenshots": ["/directions/1"]}
    gate = {"id": "G01", "title": "g", "goal": "g", "type": "human", "gate": "directions"}
    (spec / "steps.json").write_text(json.dumps({"version": 1, "project": "p", "allowed_paths": ["src/**"],
                                                  "always_refs": ["docs/conventions.md"], "steps": [code, gate]}))
    raw = root / "raw"
    raw.mkdir()
    for vp in ("desktop", "mobile"):
        (raw / f"directions-1-{vp}.png").write_bytes(PNG.read_bytes())
    (raw / "shots.json").write_text(json.dumps([{"route": "/directions/1", "viewport": vp, "file": f"directions-1-{vp}.png"}
                                                for vp in ("desktop", "mobile")]))
    sha = shots.finish(raw, state / "design" / "shots" / "S01", "S01", "a" * 40)["shots_sha"]
    (state / "state.json").write_text(json.dumps({"status": "waiting_design", "steps": {"S01": {"status": "passed"}}}))
    return spec, state, sha


PICK = {"directions": [{"n": 1, "keep": ["hero"], "note": "bold"}]}


class DesignLoopTest(unittest.TestCase):
    def test_select_with_hash_from_before_a_redo_is_stale(self):
        spec, state, sha = build()
        designloop.redo(spec, state, "G01", "more contrast")
        with self.assertRaises(designloop.Stale):
            designloop.select(spec, state, "G01", sha, PICK)

    def test_redo_limit_and_refusal(self):
        spec, state, _ = build()
        with self.assertRaises(designloop.Invalid):
            designloop.redo(spec, state, "G01", "x", refusal=lambda d, s, x: "merged work is never rebuilt")
        for i in range(designloop.MAX_REDOS):
            designloop.redo(spec, state, "G01", f"n{i}")
        with self.assertRaisesRegex(designloop.Invalid, "amend"):
            designloop.redo(spec, state, "G01", "again")

    def test_prompt_section_and_feedback(self):
        spec, state, sha = build()
        self.assertEqual(designloop.prompt_section(state), "")
        designloop.select(spec, state, "G01", sha, PICK)
        self.assertIn("Direction 1: keep hero", designloop.prompt_section(state))
        self.assertIn("## Design feedback from Alex", designloop.feedback_section("more contrast"))
        sp = specmod.load(spec)
        text = prompts.coder(sp, sp.step("S01"), attempt=1, max_attempts=3, progress=[], git_log="", feedback=[],
                             prev_diff="", state_step={"design_feedback": "more contrast"})
        self.assertIn("more contrast", text)

    def test_validate_tweaks_rejects_unknown_group(self):
        opts = [{"id": c, "label": c} for c in "abc"]
        with self.assertRaises(designloop.Invalid):
            designloop.validate_tweaks({"schema": 1, "groups": [{"id": "font", "label": "F", "options": opts}]})


if __name__ == "__main__":
    unittest.main()
