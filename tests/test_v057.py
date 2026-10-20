"""0.5.7 (step 23, B2): change rounds, per-step integrity record, regression rule. Fake docker, no Claude."""
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

from hands import gitops, rounds, prompts, spec as specmod  # noqa: E402
from hands.state import State  # noqa: E402
from test_runner import Env, STEP1, STEP2, GOOD1, GOOD2, EVAL_PASS, PASS, git, sh  # noqa: E402

C1 = {"id": "C1-01", "title": "pricing", "goal": "add pricing", "depends_on": ["S01"], "acceptance": ["test -f src/p.js"]}
C1B = {"id": "C1-02", "title": "faq", "goal": "add faq", "depends_on": ["C1-01"], "acceptance": ["test -f src/f.js"]}
C2 = {"id": "C2-01", "title": "blog", "goal": "add blog", "acceptance": ["test -f src/g.js"]}
CODER_P = {"role": "coder", "files": {"src/p.js": "p"}, "text": "ok\nSUMMARY: pricing"}
CODER_F = {"role": "coder", "files": {"src/f.js": "f"}, "text": "ok\nSUMMARY: faq"}


def origin_refs(e):
    return git(e.tmp / "app.git", "for-each-ref", "--format=%(refname) %(objectname)").strip().splitlines()


def origin_ref(e, name):
    p = subprocess.run(["git", "-C", str(e.tmp / "app.git"), "rev-parse", "--verify", "-q", name], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


class Built:
    """A project whose round-0 steps passed for real (S01, S02 on main), then amended with extra steps."""

    def __init__(self, extra, **kw):
        self.e = Env([STEP1, STEP2], **kw)
        self.e.queue(GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
        r = self.e.runner()
        assert r.run() == 0 and r.state.status == "done"
        self.main = origin_ref(self.e, "main")
        for n in ("docker.jsonl", "prompts.jsonl"):          # only the round's own sessions and containers count
            (self.e.tmp / n).unlink(missing_ok=True)
        self.amend(extra)

    def amend(self, extra, mutate=None):
        src = self.e.tmp / "specsrc"
        raw = json.loads((src / "steps.json").read_text())
        raw["steps"] = raw["steps"] + extra
        if mutate:
            mutate(raw, src)
        (src / "steps.json").write_text(json.dumps(raw))
        git(src, "add", "-A")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "amend")
        git(src, "push", "-q", "origin", "main")


class TestRounds(unittest.TestCase):
    def test_fresh_project_unchanged(self):
        e = Env([STEP1, STEP2])
        e.queue(GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        refs = origin_refs(e)
        self.assertEqual([x.split()[0] for x in refs], ["refs/heads/main"])
        self.assertNotIn("round", r.state.data)
        s1 = r.state.data["steps"]["S01"]
        self.assertEqual((s1["round"], s1["branch"]), (0, "main"))
        self.assertEqual(s1["spec_files"], ["check.sh"])
        self.assertEqual(len(s1["spec_hash"]), 64)
        # round 0: only setup + regression + the step's own commands (S02 does not re-run S01's check)
        recs = [x for x in e.prompt_records() if x["role"] == "coder"]
        self.assertNotIn("regression", recs[1]["prompt"].split("## Allowed paths")[0].lower())
        accept = [a for a in (json.loads(x) for x in (e.tmp / "docker.jsonl").read_text().splitlines()) if "HANDS_ROLE=accept" in a]
        self.assertEqual([a[-1] for a in accept], ["bash acceptance/check.sh", "test -f src/b.js"])

    def test_round_opens_change_branch_with_pre_tag_and_regression(self):
        b = Built([C1, C1B])
        e = b.e
        e.queue(CODER_P, EVAL_PASS, CODER_F, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        rec = r.state.data["round"]
        self.assertEqual((rec["id"], rec["branch"], rec["pre_tag"], rec["status"], rec["base_sha"]),
                         (1, "change/C1", "hands/pre-C1", "open", b.main))
        self.assertEqual(origin_ref(e, "main"), b.main)                               # main untouched
        self.assertEqual(origin_ref(e, "refs/tags/hands/pre-C1"), b.main)
        log = git(e.tmp / "app.git", "log", "--format=%s", "change/C1").split("\n")
        self.assertEqual(log[:2], ["C1-02: faq", "C1-01: pricing"])
        for sid in ("C1-01", "C1-02"):
            s = r.state.data["steps"][sid]
            self.assertEqual((s["status"], s["round"], s["branch"]), ("passed", 1, "change/C1"))
        self.assertEqual(r.state.data["steps"]["S01"]["round"], 0)
        # regression: every passed step's acceptance ran before C1-01's own, in spec order, and the prompt said so
        accept = [a[-1] for a in (json.loads(x) for x in (e.tmp / "docker.jsonl").read_text().splitlines()) if "HANDS_ROLE=accept" in a]
        self.assertEqual(accept, ["bash acceptance/check.sh", "test -f src/b.js", "test -f src/p.js",
                                  "bash acceptance/check.sh", "test -f src/b.js", "test -f src/p.js", "test -f src/f.js"])
        recs = [x for x in e.prompt_records() if x["role"] == "coder"]
        self.assertIn("change round C1", recs[0]["prompt"])
        self.assertIn("- `bash acceptance/check.sh`", recs[0]["prompt"].split("regression)")[1])
        # a second run is idempotent: same tag, same branch, nothing rebuilt
        before = origin_refs(e)
        r2 = e.runner()
        self.assertEqual(r2.run(), 0)
        self.assertEqual(origin_refs(e), before)
        self.assertEqual(r2.state.data["round"]["base_sha"], b.main)

    def test_regression_failure_blocks_the_step_and_keeps_passed_steps(self):
        b = Built([C1])
        e = b.e
        # the coder "fixes" pricing by deleting a.js, which S01's acceptance (test -f src/a.js) still requires
        e.queue({"role": "coder", "files": {"src/p.js": "p"}, "delete": ["src/a.js"], "text": "ok\nSUMMARY: pricing"},
                CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        c1 = r.state.data["steps"]["C1-01"]
        self.assertEqual((c1["status"], c1["attempts"]), ("passed", 2))
        first = r.state.read_jsonl("attempts.jsonl")[0]
        self.assertEqual(first["step"], "C1-01")
        self.assertIn("regression: acceptance of passed step S01 failed", first["reasons"][0])
        self.assertIn("bash acceptance/check.sh", first["reasons"][0])
        recs = [x for x in e.prompt_records() if x["role"] == "coder"]
        self.assertIn("regression: acceptance of passed step S01 failed", recs[1]["prompt"])
        self.assertEqual(r.state.data["steps"]["S01"]["status"], "passed")
        self.assertIn("src/a.js", git(e.tmp / "app.git", "ls-tree", "-r", "--name-only", "change/C1").split())
        self.assertEqual(origin_ref(e, "main"), b.main)

    def test_pre_tag_at_another_sha_escalates_before_anything_is_built(self):
        b = Built([C1])
        e = b.e
        other = e.tmp / "human"
        sh("git", "clone", "-q", str(e.tmp / "app.git"), str(other))
        git(other, "-c", "user.name=h", "-c", "user.email=h@h", "commit", "-q", "--allow-empty", "-m", "elsewhere")
        git(other, "tag", "hands/pre-C1")
        git(other, "push", "-q", "origin", "refs/tags/hands/pre-C1")
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 2)
        self.assertIn("tag hands/pre-C1 already exists on origin", r.state.data["reason"])
        self.assertNotIn("round", r.state.data)
        self.assertEqual(e.prompt_records(), [])
        self.assertEqual(origin_ref(e, "refs/heads/change/C1"), "")

    def test_passed_step_changed_in_spec_escalates(self):
        b = Built([C1], )
        e = b.e

        def edit(raw, src):
            raw["steps"][0]["goal"] = "make greet differently"
        b.amend([], mutate=edit)
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 2)
        self.assertIn("passed step(s) changed in the spec", r.state.data["reason"])
        self.assertIn("S01: its steps.json entry or owned acceptance file(s) (acceptance/check.sh) changed", r.state.data["reason"])
        self.assertEqual(e.prompt_records(), [])
        self.assertNotIn("round", r.state.data)
        # an owned test file removed: same guard, names the file
        b2 = Built([C1])

        def rm(raw, src):
            (src / "acceptance" / "check.sh").unlink()
            raw["steps"][0]["acceptance"] = ["true"]
        b2.amend([], mutate=rm)
        r2 = b2.e.runner()
        self.assertEqual(r2.run(), 2)
        self.assertIn("changed", r2.state.data["reason"])

    def test_record_hash_matches_planner_and_mark_done(self):
        e = Env([STEP1, STEP2])
        e.queue(GOOD1, EVAL_PASS)
        r = e.runner()
        r.run(max_steps=1)
        s1 = r.state.data["steps"]["S01"]
        self.assertEqual(rounds.make_record(e.cfg.spec_dir, "S01"), {"spec_hash": s1["spec_hash"], "spec_files": ["check.sh"]})
        self.assertEqual(rounds.check_record(e.cfg.spec_dir, "S01", s1), "")
        # a different owner set or content changes the hash; a record without a hash is never checked
        self.assertNotEqual(rounds.step_hash({"id": "S01"}, {"check.sh": b"x"}), rounds.step_hash({"id": "S01"}, {"check.sh": b"y"}))
        self.assertEqual(rounds.check_record(e.cfg.spec_dir, "S01", {"status": "passed"}), "")
        from hands.__main__ import main as cli
        import hands.config as cfgmod
        cfgmod.load = lambda name, path=None, _c=e.cfg: _c      # mark-done through the CLI on this temp project
        try:
            import hands.__main__ as m
            m.load = cfgmod.load
            self.assertEqual(cli(["t", "mark-done", "S02"]), 0)
        finally:
            import importlib
            importlib.reload(cfgmod)
            m.load = cfgmod.load
        s2 = State(e.cfg.state_dir).data["steps"]["S02"]
        self.assertEqual((s2["status"], s2["round"], s2["branch"], s2["spec_files"]), ("passed", 0, "main", []))
        self.assertEqual(len(s2["spec_hash"]), 64)

    def test_recovery_stop_and_watchdog_on_the_change_branch(self):
        b = Built([C1, C1B])
        e = b.e
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(max_steps=1), 0)
        self.assertEqual(git(e.cfg.app_dir, "rev-parse", "--abbrev-ref", "HEAD").strip(), "change/C1")
        base = gitops.head(e.cfg.app_dir)
        # a crash after the coder committed: the next run drops it, on the change branch, and keeps the record
        (e.cfg.app_dir / "src" / "half.js").write_text("x")
        git(e.cfg.app_dir, "add", "-A")
        git(e.cfg.app_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "C1-02: half")
        r2 = e.runner()
        r2.state.data["current"] = {"step": "C1-02", "attempt": 1, "base": base, "phase": "evaluator"}
        r2.state.set_status("running")
        r2.state.save()
        # STOP: the loop stops before building, then start resumes on the branch
        (e.cfg.state_dir / "STOP").touch()
        self.assertEqual(r2.run(), 1)
        (e.cfg.state_dir / "STOP").unlink()
        e.queue(CODER_F, EVAL_PASS)
        r3 = e.runner()
        self.assertEqual(r3.run(), 0)
        files = git(e.tmp / "app.git", "ls-tree", "-r", "--name-only", "change/C1").split()
        self.assertNotIn("src/half.js", files)
        self.assertIn("src/f.js", files)
        self.assertEqual(r3.state.data["round"]["base_sha"], b.main)
        self.assertEqual(origin_ref(e, "main"), b.main)
        # the watchdog restarts a runner that died while running (status-based; unchanged by rounds)
        from hands.__main__ import main as cli
        import hands.__main__ as m
        started = []
        orig_run, orig_load = subprocess.run, m.load

        def fake_run(argv, **kw):
            if argv[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            if argv[:2] == ["tmux", "new-session"]:
                started.append(argv)
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return orig_run(argv, **kw)
        m.subprocess.run = fake_run
        m.load = lambda name, path=None: e.cfg
        try:
            r3.state.set_status("running")
            self.assertEqual(cli(["t", "watchdog"]), 0)
            self.assertEqual(len(started), 1)
        finally:
            m.subprocess.run, m.load = orig_run, orig_load
        self.assertEqual(git(e.cfg.app_dir, "rev-parse", "--abbrev-ref", "HEAD").strip(), "change/C1")

    def test_one_open_round_at_a_time_and_reset_step_rule(self):
        b = Built([C1, C2])
        e = b.e
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 2)
        self.assertEqual(r.state.data["steps"]["C1-01"]["status"], "passed")
        self.assertIn("round C1 is built on change/C1 but not merged", r.state.data["reason"])
        self.assertEqual(origin_ref(e, "refs/heads/change/C2"), "")
        # reset-step of a round-0 step is refused while C1 is open; a C1 step may be reset
        from hands.__main__ import main as cli
        import hands.__main__ as m
        orig = m.load
        m.load = lambda name, path=None: e.cfg
        try:
            self.assertEqual(cli(["t", "reset-step", "S01"]), 1)
            self.assertEqual(State(e.cfg.state_dir).data["steps"]["S01"]["status"], "passed")
            self.assertEqual(cli(["t", "reset-step", "C1-01"]), 0)
            self.assertEqual(State(e.cfg.state_dir).data["steps"]["C1-01"]["status"], "pending")
        finally:
            m.load = orig
        # the planner refuses a new amendment while the round is open
        from hands import planner
        with self.assertRaises(planner.Refusal) as cm:
            planner._plan(e.cfg, "more", planner.Log(e.cfg), amend=True)
        self.assertIn("round C1 is built on change/C1 but not merged", str(cm.exception))
        # after B3 merges change/C1 into main and marks the round merged, round 2 opens from the new main
        git(e.tmp / "app.git", "update-ref", "refs/heads/main", origin_ref(e, "refs/heads/change/C1"))
        st = State(e.cfg.state_dir)
        st.data["round"]["status"] = "merged"
        st.data["steps"]["C1-01"] = {**r.state.data["steps"]["C1-01"]}
        st.set_status("idle")
        e.queue({"role": "coder", "files": {"src/g.js": "g"}, "text": "ok\nSUMMARY: blog"}, EVAL_PASS)
        r2 = e.runner()
        self.assertEqual(r2.run(), 0, r2.state.data.get("reason"))
        self.assertEqual((r2.state.data["round"]["id"], r2.state.data["round"]["branch"]), (2, "change/C2"))
        self.assertEqual(origin_ref(e, "refs/tags/hands/pre-C2"), origin_ref(e, "main"))
        self.assertEqual(origin_ref(e, "main"), origin_ref(e, "refs/heads/change/C1"))     # main untouched by round 2
        self.assertEqual(r2.state.data["steps"]["C2-01"]["round"], 2)

    def test_unpushed_round0_commit_is_pushed_before_the_round_opens(self):
        b = Built([C1])
        e = b.e
        # an accepted round-0 commit whose push failed: local main is ahead of origin/main
        (e.cfg.app_dir / "src" / "late.js").write_text("x")
        git(e.cfg.app_dir, "add", "-A")
        git(e.cfg.app_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "S02: late")
        late = gitops.head(e.cfg.app_dir)
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0, r.state.data.get("reason"))
        self.assertEqual(origin_ref(e, "main"), late)                       # pushed first
        self.assertEqual(r.state.data["round"]["base_sha"], late)            # the round starts from it
        self.assertEqual(origin_ref(e, "refs/tags/hands/pre-C1"), late)
        self.assertIn("src/late.js", git(e.tmp / "app.git", "ls-tree", "-r", "--name-only", "change/C1").split())

    def test_passed_step_removed_from_spec_escalates(self):
        b = Built([C1])
        e = b.e

        def drop(raw, src):
            raw["steps"] = [s for s in raw["steps"] if s["id"] != "S02"]
        b.amend([], mutate=drop)
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 2)
        self.assertIn("S02: no longer in steps.json", r.state.data["reason"])
        self.assertEqual(e.prompt_records(), [])

    def test_new_file_matched_by_a_passed_steps_pattern_escalates(self):
        """S01 runs `bash acceptance/check.sh` (owns that file). A later amendment may not add a file S01's
        acceptance would pick up; with a dir pattern that is a real risk, so the record checks ownership now."""
        e = Env([{**STEP1, "acceptance": ["for f in acceptance/s01/*.sh; do bash $f || exit 1; done"],
                  "acceptance_files": ["s01/"]}], extra_acceptance={"s01/check.sh": "test -f src/a.js\n"})
        e.queue(GOOD1, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state.data["steps"]["S01"]["spec_files"], ["s01/check.sh"])
        src = e.tmp / "specsrc"
        (src / "acceptance" / "s01" / "extra.sh").write_text("exit 1\n")
        raw = json.loads((src / "steps.json").read_text())
        raw["steps"].append(C1)
        (src / "steps.json").write_text(json.dumps(raw))
        git(src, "add", "-A")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "amend")
        git(src, "push", "-q", "origin", "main")
        e.queue(CODER_P, EVAL_PASS)
        r2 = e.runner()
        self.assertEqual(r2.run(), 2)
        self.assertIn("S01: now owns acceptance file(s) it did not pass with: acceptance/s01/extra.sh", r2.state.data["reason"])
        self.assertEqual(rounds.check_record(e.cfg.spec_dir, "S01", r2.state.data["steps"]["S01"]).startswith("S01: now owns"), True)

    def test_prompt_cli_and_status_show_the_round(self):
        b = Built([C1])
        e = b.e
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        from hands.__main__ import main as cli
        import hands.__main__ as m
        import io, contextlib
        orig = m.load
        m.load = lambda name, path=None: e.cfg
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli(["t", "status"]), 0)
            self.assertIn("round C1: open on change/C1 (base " + b.main[:9], buf.getvalue())
        finally:
            m.load = orig

    def test_acceptance_plan_units(self):
        sp = specmod.Spec(root=Path("."), commit="", allowed_paths=["x"], allow_text_paths=[], setup=["s"], regression=["r"],
                          e2e_command="e2e {files}", always_refs=[], steps=[])
        a = specmod.Step(id="S01", title="", goal="", acceptance=["a", "r"], e2e=["a.spec"])
        b = specmod.Step(id="C1-01", title="", goal="", acceptance=["c"])
        self.assertEqual(prompts.acceptance_plan(sp, b, [a]), [("s", ""), ("r", ""), ("a", "S01"), ("e2e acceptance/a.spec", "S01"), ("c", "C1-01")])
        self.assertEqual(prompts.acceptance_commands(sp, b), ["s", "r", "c"])
        # a command the current step lists itself is its own, even when a passed step runs it too
        shared = specmod.Step(id="C1-02", title="", goal="", acceptance=["a", "d"])
        self.assertEqual(prompts.acceptance_plan(sp, shared, [a]), [("s", ""), ("r", ""), ("a", "C1-02"), ("e2e acceptance/a.spec", "S01"), ("d", "C1-02")])
        self.assertEqual(rounds.round_of("C12-03"), 12)
        self.assertEqual(rounds.round_of("S01"), 0)
        self.assertEqual(rounds.round_of("C0-1"), 0)
        self.assertEqual(rounds.active_branch({"round": {"id": 1, "branch": "change/C1", "status": "merged"}}, "main"), "main")
        self.assertEqual(rounds.active_branch({"round": {"id": 1, "branch": "change/C1", "status": "open"}}, "main"), "change/C1")


if __name__ == "__main__":
    unittest.main()
