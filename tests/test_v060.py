"""0.6.0 change-round fix: baseline of the passed steps on a round's base (A), the route-B re-record seen by the
runner (B), honest escalation text for a passed step's failing test (C), and leftover state records. Fake docker."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import gitops, rounds  # noqa: E402
from hands.state import State  # noqa: E402
from test_runner import Env, STEP1, STEP2, GOOD1, GOOD2, EVAL_PASS, git, sh  # noqa: E402
from test_v057 import Built, C1, CODER_P, origin_ref  # noqa: E402

C2 = {"id": "C2-01", "title": "blog", "goal": "add blog", "depends_on": ["S01"], "acceptance": ["test -f src/g.js"]}
CODER_G = {"role": "coder", "files": {"src/g.js": "g"}, "text": "ok\nSUMMARY: blog"}


def accepts(e):
    p = e.tmp / "docker.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [a[-1] for a in rows if "HANDS_ROLE=accept" in a]


def coders(e):
    return [x for x in e.prompt_records() if x["role"] == "coder"]


def human_removes(e, path):
    """Someone changes main by hand (outside the runner): the round's base no longer satisfies S01."""
    other = e.tmp / f"human-{len(list(e.tmp.glob('human-*')))}"
    sh("git", "clone", "-q", str(e.tmp / "app.git"), str(other))
    git(other, "rm", "-q", path)
    git(other, "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", f"remove {path}")
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "HEAD").strip()


def push_spec(e, mutate):
    src = e.tmp / "specsrc"
    raw = json.loads((src / "steps.json").read_text())
    mutate(raw, src)
    (src / "steps.json").write_text(json.dumps(raw))
    git(src, "add", "-A")
    git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "spec change")
    git(src, "push", "-q", "origin", "main")


def built_round0():
    e = Env([STEP1, STEP2])
    e.queue(GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
    r = e.runner()
    assert r.run() == 0 and r.state.status == "done"
    for n in ("docker.jsonl", "prompts.jsonl"):
        (e.tmp / n).unlink(missing_ok=True)
    return e


class TestBaseline(unittest.TestCase):
    def test_baseline_escalates_on_the_base_before_any_coder_and_is_not_rerun_on_restart(self):
        b = Built([C1])
        e = b.e
        base = human_removes(e, "src/a.js")            # S01's `test -f src/a.js` now fails on main itself
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 2)
        why = r.state.data["reason"]
        self.assertIn("round C1 baseline: the acceptance of passed step S01 failed on the round's base "
                      f"{base[:9]}, before any change of this round (acceptance/check.sh; `bash acceptance/check.sh`, exit 1)", why)
        self.assertIn("No coder was started", why)
        self.assertIn("S01 already passed, so a spec PR cannot change it", why)
        self.assertIn("1. Amend plan with a code fix", why)
        self.assertIn("2. Amend plan changing that test", why)
        self.assertIn("hands t approve --ack-passed-tests", why)
        self.assertIn("discard it first", why)                     # the round is open
        self.assertIn("Logs: ", why)
        self.assertEqual(coders(e), [])
        self.assertEqual(accepts(e), ["bash acceptance/check.sh"])
        bl = r.state.data["round"]["baseline"]
        self.assertEqual((bl["status"], bl["step"], bl["files"], bl["cmd"], bl["rc"], bl["base_sha"]),
                         ("failed", "S01", ["check.sh"], "bash acceptance/check.sh", 1, base))
        self.assertNotIn("C1-01", r.state.data["steps"])                # never attempted
        # a restart without ack re-runs nothing (the escalation stands)
        (e.tmp / "docker.jsonl").unlink()
        r2 = e.runner()
        self.assertEqual(r2.run(), 2)
        self.assertEqual(accepts(e), [])
        self.assertEqual(coders(e), [])
        # after ack the human acted: it runs once more on the same base (and still fails here)
        st = State(e.cfg.state_dir)
        st.set_status("idle", "acknowledged")
        r3 = e.runner()
        self.assertEqual(r3.run(), 2)
        self.assertEqual(accepts(e), ["bash acceptance/check.sh"])
        self.assertEqual(coders(e), [])
        self.assertEqual(origin_ref(e, "main"), base)

    def test_passing_baseline_runs_once_and_old_records_load(self):
        b = Built([C1])
        e = b.e
        r = e.runner()
        self.assertEqual(r.run(max_steps=0), 0)          # opens C1 and runs the baseline, builds nothing
        self.assertEqual(accepts(e), ["bash acceptance/check.sh", "test -f src/b.js"])
        self.assertEqual(coders(e), [])
        bl = r.state.data["round"]["baseline"]
        self.assertEqual((bl["status"], bl["commands"], bl["base_sha"]), ("passed", 2, b.main))
        # restart: same key, not re-run
        r2 = e.runner()
        self.assertEqual(r2.run(max_steps=0), 0)
        self.assertEqual(len(accepts(e)), 2)
        self.assertEqual(r2.state.data["round"]["baseline"]["key"], bl["key"])
        # a round record written before 0.6.0 (no baseline field) loads unchanged and gets one
        st = State(e.cfg.state_dir)
        st.data["round"].pop("baseline")
        st.save()
        old = State(e.cfg.state_dir).data["round"]
        self.assertEqual((old["id"], old["status"], old["base_sha"]), (1, "open", b.main))
        r3 = e.runner()
        self.assertEqual(r3.run(max_steps=0), 0)
        self.assertEqual(len(accepts(e)), 4)
        self.assertEqual(r3.state.data["round"]["baseline"]["key"], bl["key"])
        # then the build carries on as before
        e.queue(CODER_P, EVAL_PASS)
        r4 = e.runner()
        self.assertEqual(r4.run(), 0, r4.state.data.get("reason"))
        self.assertEqual(r4.state.data["steps"]["C1-01"]["status"], "passed")
        self.assertEqual(accepts(e)[4:], ["bash acceptance/check.sh", "test -f src/b.js", "test -f src/p.js"])

    def test_round0_has_no_baseline(self):
        e = Env([STEP1, STEP2])
        e.queue(GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        self.assertNotIn("round", r.state.data)
        self.assertEqual(accepts(e), ["bash acceptance/check.sh", "test -f src/b.js"])


class TestEscalationText(unittest.TestCase):
    def test_regression_escalation_names_the_passed_step_and_file(self):
        b = Built([{**C1, "max_attempts": 1}])
        e = b.e
        e.queue({"role": "coder", "files": {"src/p.js": "p"}, "delete": ["src/a.js"], "text": "ok\nSUMMARY: pricing"})
        r = e.runner()
        self.assertEqual(r.run(), 2)
        why = r.state.data["reason"]
        self.assertIn("C1-01 failed 1 attempts. Last: regression: acceptance of passed step S01 failed", why)
        self.assertIn("The failing check is S01's test (acceptance/check.sh) and S01 already passed", why)
        self.assertIn("1. Amend plan with a code fix", why)
        self.assertIn("--ack-passed-tests", why)
        self.assertIn("Amendments are refused while round C1 is open on change/C1", why)

    def test_spec_problem_naming_a_passed_test_gets_the_real_options(self):
        b = Built([C1])
        e = b.e
        e.queue({"role": "coder", "files": {"src/p.js": "p"}, "delete": ["src/a.js"],
                 "text": "tried\nSPEC_PROBLEM: check.sh wants src/a.js, which the pricing page replaces\nSUMMARY: x"})
        r = e.runner()
        self.assertEqual(r.run(), 2)
        why = r.state.data["reason"]
        self.assertIn("the coder reports a problem in the spec/tests it cannot fix", why)
        self.assertIn("The failing check is S01's test (acceptance/check.sh)", why)
        self.assertIn("2. Amend plan changing that test", why)
        self.assertNotIn("Fix the spec repo (PR)", why)

    def test_spec_problem_in_the_current_step_keeps_the_generic_text(self):
        b = Built([C1])
        e = b.e
        e.queue({"role": "coder", "files": {"src/q.js": "q"},
                 "text": "tried\nSPEC_PROBLEM: C1-01 asks for a page the design never mentions\nSUMMARY: x"})
        r = e.runner()
        self.assertEqual(r.run(), 2)
        why = r.state.data["reason"]
        self.assertIn("Fix the spec repo (PR), then: hands t ack", why)
        self.assertNotIn("Options:", why)


class TestLiveState(unittest.TestCase):
    def test_leftover_pending_step_of_a_reverted_round_is_ignored(self):
        """phoenix-8-0 after discarding C1 and reverting its amendment: state.json keeps a pending C1-01 entry for a
        step that is no longer in steps.json. It loads, opens no round, and the next amendment is round C2."""
        e = built_round0()
        st = State(e.cfg.state_dir)
        st.data["steps"]["C1-01"] = {"status": "pending", "attempts": 0, "reasons": []}
        st.set_status("idle", "acknowledged")
        r = e.runner()
        self.assertEqual(r.run(), 0, r.state.data.get("reason"))
        self.assertEqual(r.state.status, "done")
        self.assertNotIn("round", r.state.data)
        self.assertEqual((accepts(e), coders(e)), ([], []))
        self.assertEqual(r.state.data["steps"]["C1-01"], {"status": "pending", "attempts": 0, "reasons": []})
        push_spec(e, lambda raw, src: raw["steps"].append(C2))
        e.queue(CODER_G, EVAL_PASS)
        r2 = e.runner()
        self.assertEqual(r2.run(), 0, r2.state.data.get("reason"))
        rec = r2.state.data["round"]
        self.assertEqual((rec["id"], rec["branch"], rec["baseline"]["status"]), (2, "change/C2", "passed"))
        self.assertEqual(r2.state.data["steps"]["C2-01"]["status"], "passed")
        self.assertEqual(r2.state.data["steps"]["C1-01"]["status"], "pending")

    def test_route_b_change_runs_the_changed_test_on_the_base_and_passes(self):
        """S01's test fails on main (like REQ-REPO-03's time limit). A hand-merged change of it escalates; the same
        change re-recorded by approve (route B) is accepted, and the C2 baseline runs the changed test on the base."""
        e = built_round0()
        old_hash = State(e.cfg.state_dir).data["steps"]["S01"]["spec_hash"]
        base = human_removes(e, "src/a.js")

        def change(raw, src):
            (src / "acceptance" / "check.sh").write_text("test -f README.md\n")
            raw["steps"].append(C2)
            raw["tests_changed"] = [{"path": "acceptance/check.sh", "change": "change", "why": "the test was wrong"}]
        push_spec(e, change)
        r = e.runner()
        self.assertEqual(r.run(), 2)                     # merged by hand: still escalates
        self.assertIn("S01: its steps.json entry or owned acceptance file(s) (acceptance/check.sh) changed",
                      r.state.data["reason"])
        self.assertEqual(accepts(e), [])
        # what approve --ack-passed-tests does after the merge
        commit = gitops.refresh_spec(e.cfg.spec_dir, "main")
        st = State(e.cfg.state_dir)
        lines = rounds.rerecord(st.data, e.cfg.spec_dir, {"S01": ["acceptance/check.sh"]}, commit, 2)
        self.assertEqual(len(lines), 1)
        st.set_status("idle", "acknowledged")
        self.assertNotEqual(st.data["steps"]["S01"]["spec_hash"], old_hash)
        e.queue(CODER_G, EVAL_PASS)
        r2 = e.runner()
        self.assertEqual(r2.run(), 0, r2.state.data.get("reason"))
        bl = r2.state.data["round"]["baseline"]
        self.assertEqual((bl["status"], bl["base_sha"]), ("passed", base))
        self.assertEqual(accepts(e)[:2], ["bash acceptance/check.sh", "test -f src/b.js"])
        self.assertEqual(r2.state.data["steps"]["C2-01"]["status"], "passed")
        acc = e.cfg.state_dir / "attempts" / "baseline-C2-1" / "accept-0.log"
        self.assertIn("bash acceptance/check.sh", acc.read_text())
        self.assertEqual(r2.state.data["steps"]["S01"]["tests_amended"]["commit"], commit)

    def test_rerecord_refuses_unknown_or_unpassed_steps(self):
        e = built_round0()
        st = State(e.cfg.state_dir)
        before = json.dumps(st.data, sort_keys=True)
        with self.assertRaises(rounds.RoundError):
            rounds.rerecord(st.data, e.cfg.spec_dir, {"S01": ["acceptance/check.sh"], "S09": ["acceptance/x"]}, "a" * 40, 1)
        self.assertEqual(json.dumps(st.data, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
