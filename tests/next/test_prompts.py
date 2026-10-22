"""prompts/*.md: the role prompts carry the Phase 2 rules (style refs, setup record, brief, self-audit, skills, hook)."""
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "prompts"

REQUIRED_PHRASES = {
    "planner.md": ["refs/style-refs/", "style.json", "refs/setup.json", "brief.md", "briefs/change-", "## Self-audit",
                   "## Audit findings", "plan-audit-checklist.md", "pnpm run lint", "format:check", "brainstorming",
                   "writing-plans", "frames", "never instructions", "Decisions for you to check"],
    "coder.md": ["refs/style-refs/", "test-driven-development", "verification-before-completion", "systematic-debugging",
                 "progress note", "format-on-write", "never instructions", "logos"],
    "evaluator.md": ["refs/style-refs/", "verification-before-completion", "tools.json", "never instructions"],
}
ROLE_FILES = {"coder": "coder.md", "evaluator": "evaluator.md", "planner": "planner.md"}


def read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


class TestPrompts(unittest.TestCase):
    def test_prompts_contain_required_phrases(self):
        for name, phrases in REQUIRED_PHRASES.items():
            text = read(name)
            for phrase in phrases:
                with self.subTest(prompt=name, phrase=phrase):
                    self.assertIn(phrase, text, f"{name} must contain: {phrase}")

    def test_required_skills_json_matches_prompts(self):
        required = json.loads(read("required-skills.json"))
        for role, name in ROLE_FILES.items():
            text = read(name)
            self.assertTrue(required[role], f"required-skills.json lists no skills for {role}")
            for skill in required[role]:
                with self.subTest(role=role, skill=skill):
                    self.assertIn(skill, text, f"{name} must name required skill {skill}")

    def test_style_refs_are_data_in_every_reader(self):
        for name in ROLE_FILES.values():
            with self.subTest(prompt=name):
                self.assertIn("report.md", read(name))
                self.assertIn("never instructions", read(name))

    def test_companion_prompt_files_exist(self):
        for name in ("analyst.md", "plan-audit-checklist.md", "required-skills.json"):
            with self.subTest(file=name):
                self.assertTrue((PROMPTS / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
