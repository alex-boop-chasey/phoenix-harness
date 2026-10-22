"""hands.planaudit: the planner's self-audit table in PLAN.md is checked against the overlooked-areas checklist."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hands import planaudit

REPO = Path(__file__).resolve().parents[2]
CHECKLIST = ("# Overlooked areas\n\n"
             "- auth: Who can do what; sessions, tokens, logout.\n"
             "- errors: Error states, failure messages, retries.\n"
             "- empty-states: What a page shows with no data.\n")
FINDINGS = "\n## Audit findings\n- Checked the deploy path by reading the Dockerfile; added a healthcheck in Task 4.\n"
TABLE = ("# Plan\n\n## Summary\nA site.\n\n## Self-audit\n"
         "| Area | Status | Where / why |\n|------|--------|-------------|\n"
         "| auth | addressed | Task 2: single-user login with Access |\n"
         "| errors | addressed | Task 3 error banners and retry buttons |\n"
         "| empty-states | n/a | the only list is never empty (seeded) |\n")
GOOD = TABLE + FINDINGS


class TestParseChecklist(unittest.TestCase):
    def test_parses_id_and_text(self):
        self.assertEqual(planaudit.parse_checklist(CHECKLIST),
                         [("auth", "Who can do what; sessions, tokens, logout."),
                          ("errors", "Error states, failure messages, retries."),
                          ("empty-states", "What a page shows with no data.")])

    def test_ignores_other_lines(self):
        text = "# Title\n\nprose line\n- not an item\n-  spaced-id :  text here \n* auth: wrong bullet\n"
        self.assertEqual(planaudit.parse_checklist(text), [("spaced-id", "text here")])


class TestCheck(unittest.TestCase):
    def test_good_plan_passes(self):
        self.assertEqual(planaudit.check(GOOD, CHECKLIST), [])

    def test_missing_area_and_bad_status(self):
        plan = ("# Plan\n\n## Self-audit\n| Area | Status | Where / why |\n|---|---|---|\n"
                "| auth | maybe | Task 2 |\n| empty-states | n/a | short |\n")
        self.assertEqual(planaudit.check(plan, CHECKLIST),
                         ["bad status for auth: maybe", "reason too short for auth", "reason too short for empty-states",
                          "missing area: errors", "missing section: ## Audit findings"])

    def test_normalises_case_and_spaces(self):
        plan = ("# Plan\n\n## Self-audit\n| Area | Status | Where / why |\n| --- | --- | --- |\n"
                "|  Auth  | Addressed | Task 2: single-user login with Access |\n"
                "| ERRORS | N/A | there is no network path that can fail here |\n"
                "| Empty-States | addressed | Task 3 renders a hint when the list is empty |\n" + FINDINGS)
        self.assertEqual(planaudit.check(plan, CHECKLIST), [])

    def test_reason_length_counts_trimmed_text(self):
        plan = TABLE.replace("| the only list is never empty (seeded) |", "|    1234567    |") + FINDINGS
        self.assertEqual(planaudit.check(plan, CHECKLIST), ["reason too short for empty-states"])
        plan = TABLE.replace("| the only list is never empty (seeded) |", "| 12345678 |") + FINDINGS
        self.assertEqual(planaudit.check(plan, CHECKLIST), [])

    def test_row_without_status_or_reason(self):
        plan = TABLE.replace("| errors | addressed | Task 3 error banners and retry buttons |", "| errors |") + FINDINGS
        self.assertEqual(planaudit.check(plan, CHECKLIST), ["bad status for errors: ", "reason too short for errors"])

    def test_findings_section_required(self):
        self.assertEqual(planaudit.check(TABLE, CHECKLIST), ["missing section: ## Audit findings"])
        self.assertEqual(planaudit.check(TABLE + "\n## Audit findings\n\n", CHECKLIST), ["empty section: ## Audit findings"])
        self.assertEqual(planaudit.check(TABLE + "\n## Audit findings\nprose, not a bullet\n-\n", CHECKLIST),
                         ["empty section: ## Audit findings"])
        self.assertEqual(planaudit.check(TABLE + "\n## Audit findings\n* Checked deploy with docker.\n", CHECKLIST), [])

    def test_sections_end_at_the_next_level_two_heading(self):
        plan = TABLE + "\n## Risks\n- none\n" + FINDINGS + "\n## Later\n- more\n"
        self.assertEqual(planaudit.check(plan, CHECKLIST), [])
        plan = ("# Plan\n\n## Self-audit\n\n## Audit findings\n- checked\n\n## Appendix\n"
                "| Area | Status | Where / why |\n|---|---|---|\n| auth | addressed | Task 2 does the login |\n")
        self.assertEqual(planaudit.check(plan, CHECKLIST),
                         ["missing area: auth", "missing area: errors", "missing area: empty-states"])

    def test_missing_self_audit_section(self):
        plan = "# Plan\n\nSummary: a site.\n"
        self.assertEqual(planaudit.check(plan, CHECKLIST),
                         ["missing section: ## Self-audit", "missing area: auth", "missing area: errors",
                          "missing area: empty-states", "missing section: ## Audit findings"])

    def test_duplicate_row_is_checked_too(self):
        plan = TABLE + "| auth | later | Task 9 maybe |\n" + FINDINGS
        self.assertEqual(planaudit.check(plan, CHECKLIST), ["bad status for auth: later"])

    def test_empty_checklist_only_needs_the_sections(self):
        self.assertEqual(planaudit.check(GOOD, ""), [])
        self.assertEqual(planaudit.check("# P\n", ""), ["missing section: ## Self-audit", "missing section: ## Audit findings"])


class TestRepoChecklist(unittest.TestCase):
    def test_repo_checklist_has_all_ids(self):
        ids = [i for i, _ in planaudit.parse_checklist(planaudit.CHECKLIST_FILE.read_text())]
        self.assertEqual(ids, list(planaudit.DESIGN_IDS))
        self.assertEqual(len(ids), 24)
        for _, text in planaudit.parse_checklist(planaudit.CHECKLIST_FILE.read_text()):
            self.assertGreaterEqual(len(text), 8)

    def test_plan_from_repo_checklist_passes(self):
        text = planaudit.CHECKLIST_FILE.read_text()
        rows = "".join(f"| {i} | addressed | covered by a task in this plan |\n" for i, _ in planaudit.parse_checklist(text))
        plan = "# P\n\n## Self-audit\n| Area | Status | Where / why |\n|---|---|---|\n" + rows + FINDINGS
        self.assertEqual(planaudit.check(plan, text), [])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        (self.tmp / "checklist.md").write_text(CHECKLIST)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "hands.planaudit", *args], cwd=REPO, capture_output=True, text=True)

    def test_good_plan_exits_zero(self):
        (self.tmp / "PLAN.md").write_text(GOOD)
        p = self.run_cli("check", str(self.tmp / "PLAN.md"), "--checklist", str(self.tmp / "checklist.md"))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_bad_plan_exits_one_listing_problems(self):
        (self.tmp / "PLAN.md").write_text("# Plan\n\n## Self-audit\n| Area | Status | Where / why |\n|---|---|---|\n"
                                          "| auth | maybe | Task 2 |\n| empty-states | n/a | short |\n")
        p = self.run_cli("check", str(self.tmp / "PLAN.md"), "--checklist", str(self.tmp / "checklist.md"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.splitlines(), ["bad status for auth: maybe", "reason too short for auth",
                                                 "reason too short for empty-states", "missing area: errors",
                                                 "missing section: ## Audit findings"])

    def test_default_checklist_is_the_repo_file(self):
        (self.tmp / "PLAN.md").write_text(GOOD)
        p = self.run_cli("check", str(self.tmp / "PLAN.md"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("missing area: deploy", p.stdout.splitlines())

    def test_unreadable_file_exits_two(self):
        p = self.run_cli("check", str(self.tmp / "nope.md"), "--checklist", str(self.tmp / "checklist.md"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("nope.md", p.stderr)
        (self.tmp / "PLAN.md").write_text(GOOD)
        p = self.run_cli("check", str(self.tmp / "PLAN.md"), "--checklist", str(self.tmp / "missing.md"))
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
