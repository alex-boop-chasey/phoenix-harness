"""0.5.8 (step 24, B3): merge tap, discard, merged-round rules, dashboard round state. Fake docker, no Claude."""
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

from hands import gitops, merge, rounds  # noqa: E402
from hands.state import Lock, State  # noqa: E402
from test_runner import EVAL_PASS, git  # noqa: E402
from test_v057 import Built, C1, C1B, CODER_F, CODER_P, origin_ref, origin_refs  # noqa: E402

LOG = []


def log(msg):
    LOG.append(msg)


class Round:
    """A project with round C1 (two steps) built and passed on change/C1."""

    def __init__(self, extra=(C1, C1B), queue=(CODER_P, EVAL_PASS, CODER_F, EVAL_PASS), max_steps=None, **kw):
        self.b = Built(list(extra), **kw)
        self.e = self.b.e
        self.e.queue(*queue)
        r = self.e.runner()
        assert r.run(max_steps=max_steps) == 0, r.state.data
        self.r = r
        self.head = origin_ref(self.e, "change/C1")
        self.main = self.b.main
        self.sent = []

    @property
    def cfg(self):
        return self.e.cfg

    def state(self):
        return State(self.cfg.state_dir)

    def merge(self, branch_sha=None, main_sha=None):
        return merge.merge_round(self.cfg, self.state(), log, self.sent.append,
                                 branch_sha or self.head, main_sha or self.main)

    def discard(self, branch_sha=None):
        return merge.discard_round(self.cfg, self.state(), log, self.sent.append, branch_sha or self.head)

    def tree(self, rev):
        return git(self.e.tmp / "app.git", "rev-parse", f"{rev}^{{tree}}").strip()


class TestMerge(unittest.TestCase):
    def test_merge_squashes_pinned_round_into_main(self):
        p = Round()
        self.assertEqual(p.state().data["status"], "done")
        rec = p.merge()
        main = origin_ref(p.e, "main")
        self.assertNotEqual(main, p.main)
        self.assertEqual(git(p.e.tmp / "app.git", "rev-parse", "main^").strip(), p.main)   # one commit on the old base
        self.assertEqual(p.tree("main"), p.tree(f"refs/tags/hands/merged/C1"))            # exactly the tested tree
        self.assertEqual(origin_ref(p.e, "refs/tags/hands/merged/C1"), p.head)
        self.assertEqual(origin_ref(p.e, "refs/tags/hands/pre-C1"), p.main)
        self.assertEqual(origin_ref(p.e, "change/C1"), "")
        msg = git(p.e.tmp / "app.git", "log", "-1", "--format=%s%n%b", "main")
        self.assertIn("Change C1", msg)
        self.assertIn("C1-01, C1-02", msg)
        self.assertIn("hands/merged/C1", msg)
        self.assertEqual((rec["status"], rec["merged_sha"], rec["head_sha"], rec["merged_tag"]),
                         ("merged", main, p.head, "hands/merged/C1"))
        self.assertEqual(p.state().data["round"]["status"], "merged")
        # the clone sits on main at the merged commit, clean
        self.assertEqual(gitops.head(p.cfg.app_dir), main)
        self.assertEqual(gitops.git(p.cfg.app_dir, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertTrue(gitops.is_clean(p.cfg.app_dir))
        self.assertEqual(gitops.git(p.cfg.app_dir, "branch", "--list", "change/C1").strip(), "")
        ev = merge.events(p.cfg.state_dir)
        self.assertEqual([x["event"] for x in ev], ["opened", "merged"])
        self.assertEqual(ev[-1]["merged_sha"], main)
        self.assertTrue(any("merged C1 into main" in m for m in p.sent), p.sent)
        # second press: nothing to merge
        with self.assertRaises(merge.MergeError) as cm:
            p.merge()
        self.assertIn("no open round", str(cm.exception))

    def refuses(self, p, why, branch_sha=None, main_sha=None):
        before = origin_refs(p.e)
        st_before = json.dumps({k: v for k, v in p.state().data["round"].items() if k != "error"}, sort_keys=True)
        head = gitops.head(p.cfg.app_dir)
        with self.assertRaises(merge.MergeError) as cm:
            p.merge(branch_sha, main_sha)
        self.assertIn(why, str(cm.exception))
        self.assertEqual(origin_refs(p.e), before)
        rec = p.state().data["round"]
        self.assertEqual(rec["status"], "open")
        self.assertEqual(json.dumps({k: v for k, v in rec.items() if k != "error"}, sort_keys=True), st_before)
        self.assertEqual(gitops.head(p.cfg.app_dir), head)
        self.assertTrue(gitops.is_clean(p.cfg.app_dir))

    def test_refuses_when_branch_or_main_moved_since_shown(self):
        p = Round()
        self.refuses(p, "head of change/C1 moved", branch_sha=p.main)
        # someone pushed to the round branch after the page showed it
        src = p.e.tmp / "appsrc"
        git(src, "fetch", "-q", "origin")
        git(src, "checkout", "-q", "-B", "main", "origin/main")
        git(src, "fetch", "-q", "origin", "change/C1")
        git(src, "checkout", "-q", "-B", "change/C1", "FETCH_HEAD")
        (src / "extra.txt").write_text("x\n")
        git(src, "add", "-A")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "by hand")
        git(src, "push", "-q", "origin", "change/C1")
        self.refuses(p, "head of change/C1 moved")
        git(src, "push", "-q", "-f", "origin", f"{p.head}:refs/heads/change/C1")
        # main moved since the round opened
        git(src, "checkout", "-q", "main")
        (src / "hot.txt").write_text("fix\n")
        git(src, "add", "-A")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "hotfix")
        git(src, "push", "-q", "origin", "main")
        self.refuses(p, "main moved since round C1 opened", main_sha=origin_ref(p.e, "main"))
        self.refuses(p, "main moved since round C1 opened")

    def test_refuses_when_main_moves_between_fetch_and_push(self):
        p = Round()
        src = p.e.tmp / "appsrc"
        git(src, "fetch", "-q", "origin")
        git(src, "checkout", "-q", "-B", "main", "origin/main")
        orig = merge.gitops.push_ref

        def racy(repo, srcref, dst):
            if dst.endswith("/main"):
                merge.gitops.push_ref = orig
                git(src, "checkout", "-q", "main")
                (src / "race.txt").write_text("r\n")
                git(src, "add", "-A")
                git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "race")
                git(src, "push", "-q", "origin", "main")
            return orig(repo, srcref, dst)

        merge.gitops.push_ref = racy
        try:
            with self.assertRaises(merge.MergeError) as cm:
                p.merge()
        finally:
            merge.gitops.push_ref = orig
        self.assertIn("main moved", str(cm.exception))
        self.assertEqual(origin_ref(p.e, "change/C1"), p.head)
        self.assertEqual(origin_ref(p.e, "refs/tags/hands/merged/C1"), "")                  # nothing on origin changed
        self.assertEqual(p.state().data["round"]["status"], "open")
        self.assertEqual(gitops.git(p.cfg.app_dir, "rev-parse", "main").strip(), p.main)   # local main put back
        self.assertTrue(gitops.is_clean(p.cfg.app_dir))
        self.assertEqual([x["event"] for x in merge.events(p.cfg.state_dir)], ["opened", "merge_failed"])
        # the retry (origin main moved for real now) is refused cleanly too
        with self.assertRaises(merge.MergeError):
            p.merge()

    def test_refuses_incomplete_round_running_build_push_off_and_self(self):
        p = Round(extra=(C1, C1B), queue=(CODER_P, EVAL_PASS), max_steps=1)
        with self.assertRaises(merge.MergeError) as cm:
            p.merge()
        self.assertIn("C1-02", str(cm.exception))
        self.assertEqual(origin_ref(p.e, "main"), p.main)
        p = Round()
        with Lock(p.cfg.state_dir / "runner.lock"):
            with self.assertRaises(merge.MergeError) as cm:
                p.merge()
        self.assertIn("build is running", str(cm.exception))
        p.cfg.push = False
        with self.assertRaises(merge.MergeError) as cm:
            p.merge()
        self.assertIn("push", str(cm.exception))
        p.cfg.push = True
        p.cfg.is_self = True
        t = merge.tap_state(p.cfg, p.state().data, ["S01", "S02", "C1-01", "C1-02"])
        self.assertEqual((t["ready"], t["self"], t["all_passed"]), (False, True, True))
        self.assertIn("Install update", t["why"])
        with self.assertRaises(merge.MergeError) as cm:
            merge.check_tap(p.cfg, p.state().data, ["S01", "S02", "C1-01", "C1-02"])
        self.assertIn("Install update", str(cm.exception))
        p.cfg.is_self = False
        t = merge.tap_state(p.cfg, p.state().data, ["S01", "S02", "C1-01", "C1-02"])
        self.assertEqual((t["ready"], t["branch_sha"], t["main_sha"], t["passed"], t["total"]), (True, p.head, p.main, 2, 2))
        p.merge()                                   # the gate only decides who may press; the merge itself still works

    def test_refuses_on_tree_mismatch(self):
        p = Round()
        orig = merge._squash

        def bad(app, branch_sha, message):
            orig(app, branch_sha, message)
            (app / "sneak.txt").write_text("x\n")
            gitops.git(app, "add", "-A")
            gitops.git(app, *merge.IDENT, "commit", "-q", "--amend", "--no-edit")

        merge._squash = bad
        try:
            self.refuses(p, "merged tree differs")
        finally:
            merge._squash = orig


class TestDiscard(unittest.TestCase):
    def test_discard_keeps_work_resets_steps_and_rebuilds(self):
        p = Round()
        p.discard()
        self.assertEqual(origin_ref(p.e, "change/C1"), "")
        self.assertEqual(origin_ref(p.e, "refs/tags/hands/pre-C1"), "")
        kept = [x for x in origin_refs(p.e) if "hands/discarded/C1-" in x]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].split()[1], p.head)
        self.assertEqual(origin_ref(p.e, "main"), p.main)
        d = p.state().data
        self.assertNotIn("round", d)
        self.assertEqual(d["status"], "idle")
        for sid in ("C1-01", "C1-02"):
            self.assertEqual(d["steps"][sid], {"status": "pending", "attempts": 0, "reasons": []})
        self.assertEqual(d["steps"]["S01"]["status"], "passed")
        self.assertEqual(gitops.git(p.cfg.app_dir, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertEqual(gitops.head(p.cfg.app_dir), p.main)
        self.assertEqual(gitops.git(p.cfg.app_dir, "tag", "-l", "hands/pre-C1").strip(), "")
        self.assertEqual([x["event"] for x in merge.events(p.cfg.state_dir)], ["opened", "discarded"])
        self.assertTrue(any("discarded round C1" in m for m in p.sent), p.sent)
        # the next start rebuilds the same steps in a fresh round C1 from the same base
        p.e.queue(CODER_P, EVAL_PASS, CODER_F, EVAL_PASS)
        r = p.e.runner()
        self.assertEqual(r.run(), 0)
        rec = r.state.data["round"]
        self.assertEqual((rec["id"], rec["status"], rec["base_sha"]), (1, "open", p.main))
        self.assertEqual(origin_ref(p.e, "refs/tags/hands/pre-C1"), p.main)
        self.assertNotEqual(origin_ref(p.e, "change/C1"), "")
        self.assertEqual([x["event"] for x in merge.events(p.cfg.state_dir)], ["opened", "discarded", "opened"])

    def test_discard_refusals(self):
        p = Round()
        with self.assertRaises(merge.MergeError) as cm:
            p.discard(p.main)
        self.assertIn("moved", str(cm.exception))
        with Lock(p.cfg.state_dir / "runner.lock"):
            with self.assertRaises(merge.MergeError):
                p.discard()
        self.assertEqual(origin_ref(p.e, "change/C1"), p.head)
        self.assertEqual(p.state().data["round"]["status"], "open")
        p.merge()
        with self.assertRaises(merge.MergeError) as cm:
            p.discard()
        self.assertIn("no open round", str(cm.exception))


def cli_env(e):
    import hands.__main__ as m
    orig = m.load
    m.load = lambda name, path=None: e.cfg
    return lambda: setattr(m, "load", orig)


class TestCliAndPlanner(unittest.TestCase):
    def test_cli_merge_discard_status_and_reset_rules(self):
        import contextlib
        import io
        from hands.__main__ import main as cli
        p = Round()
        restore = cli_env(p.e)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli(["t", "status"]), 0)
            self.assertIn("round C1: open on change/C1 (base " + p.main[:9], buf.getvalue())
            self.assertIn("2/2 passed, merge ready", buf.getvalue())
            self.assertEqual(cli(["t", "merge"]), 2)                                          # shas required
            self.assertEqual(cli(["t", "merge", "--branch-sha", p.main, "--main-sha", p.main]), 1)
            self.assertEqual(origin_ref(p.e, "change/C1"), p.head)
            self.assertIn("moved", p.state().data["round"]["error"])
            p.cfg.is_self = True
            self.assertEqual(cli(["t", "merge", "--branch-sha", p.head, "--main-sha", p.main]), 1)
            self.assertEqual(origin_ref(p.e, "change/C1"), p.head)
            p.cfg.is_self = False
            self.assertEqual(cli(["t", "merge", "--branch-sha", p.head, "--main-sha", p.main]), 0)
            rec = p.state().data["round"]
            self.assertEqual(rec["status"], "merged")
            self.assertEqual(origin_ref(p.e, "main"), rec["merged_sha"])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli(["t", "status"]), 0)
            self.assertIn(f"round C1: merged into main at {rec['merged_sha'][:9]}", buf.getvalue())
            # reset-step: steps of a merged round and round-0 steps are refused; nothing changes
            for sid in ("C1-01", "S01"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(cli(["t", "reset-step", sid]), 1)
                self.assertIn("Amend plan", buf.getvalue())
                self.assertEqual(p.state().data["steps"][sid]["status"], "passed")
            self.assertEqual(cli(["t", "discard", "--branch-sha", p.head]), 1)                 # nothing open
        finally:
            restore()
        # discard through the CLI on a fresh round
        p = Round()
        restore = cli_env(p.e)
        try:
            self.assertEqual(cli(["t", "discard", "--branch-sha", p.main]), 1)
            self.assertEqual(p.state().data["round"]["status"], "open")
            self.assertEqual(cli(["t", "discard", "--branch-sha", p.head]), 0)
            self.assertNotIn("round", p.state().data)
            self.assertEqual(p.state().data["steps"]["C1-01"]["status"], "pending")
            self.assertEqual(cli(["t", "reset-step", "S01"]), 0)                              # no round any more: as before
        finally:
            restore()

    def test_reset_step_messages_name_the_merged_commit(self):
        import contextlib
        import io
        from hands.__main__ import main as cli
        p = Round()
        rec = p.merge()
        restore = cli_env(p.e)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli(["t", "reset-step", "C1-02"]), 1)
            self.assertIn(f"merged into main as {rec['merged_sha'][:9]}", buf.getvalue())
            self.assertIn("never rebuilt", buf.getvalue())
        finally:
            restore()

    def test_app_snapshot_after_merge_ignores_the_squashed_round(self):
        from hands import planner
        p = Round()
        rec = p.merge()
        app = p.cfg.app_dir
        # the clone left on the (deleted) round branch: its commits are merged work, not unpushed work
        gitops.git(app, "checkout", "-q", "-B", "change/C1", rec["head_sha"])
        planner.pdir(p.cfg).mkdir(parents=True, exist_ok=True)
        sha, dest = planner.app_snapshot(p.cfg, log)
        self.assertEqual(sha, rec["merged_sha"])
        self.assertTrue((dest / "src" / "f.js").is_file())
        self.assertEqual(gitops.git(app, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        # a genuinely unpushed commit on top still refuses
        gitops.git(app, "checkout", "-q", "-B", "change/C1", rec["head_sha"])
        (app / "src" / "late.js").write_text("x")
        gitops.git(app, "add", "-A")
        gitops.git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "late")
        with self.assertRaises(planner.PlanError) as cm:
            planner.app_snapshot(p.cfg, log)
        self.assertIn("1 commit(s) not on origin/main", str(cm.exception))
        # and plan --amend is no longer refused after the merge (round not open)
        gitops.git(app, "checkout", "-q", "main")
        with self.assertRaises(planner.Refusal) as cm:
            planner._plan(p.cfg, "more", planner.Log(p.cfg), amend=True)
        self.assertNotIn("not merged", str(cm.exception))


class TestDashboardRounds(unittest.TestCase):
    def setUp(self):
        from test_v04 import Dash
        self.p = Round()
        self.d = Dash()
        (self.d.home / "projects").mkdir()
        c = self.p.cfg
        self.toml = self.d.home / "projects" / "t.toml"
        self.toml.write_text(f'spec_dir = "{c.spec_dir}"\napp_dir = "{c.app_dir}"\nstate_dir = "{c.state_dir}"\n'
                             f'image = "hands:test"\nclaude_env = "{c.claude_env}"\n')

    def tearDown(self):
        self.d.close()

    def post(self, path, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.d.port}", "Content-Type": "application/json", "X-Hands": "1",
             "Cookie": f"{dashboard_mod().COOKIE}={self.d.sid}", "Origin": f"http://127.0.0.1:{self.d.port}"}
        c.request("POST", path, body=json.dumps(body).encode(), headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw)

    def get(self, path):
        st, _, raw = self.d.req("GET", path)
        self.assertEqual(st, 200, raw)
        return json.loads(raw)

    def test_round_state_in_list_and_detail_then_merge_and_discard_taps(self):
        import time
        p = self.p
        row = [x for x in self.get("/api/projects") if x["name"] == "t"][0]
        self.assertEqual(row["round"], {"id": 1, "status": "open", "passed": 2, "total": 2, "merge_ready": True, "self": False})
        d = self.get("/api/p/t")
        m = d["round"]["merge"]
        self.assertEqual((m["ready"], m["branch_sha"], m["main_sha"], m["self"], m["all_passed"]), (True, p.head, p.main, False, True))
        self.assertEqual([x["event"] for x in d["round"]["history"]], ["opened"])
        self.assertEqual(d["round"]["history"][0]["id"], 1)
        self.assertEqual([s["id"] for s in d["steps"] if s["round"] == 1], ["C1-01", "C1-02"])
        # refusals: bad sha, wrong sha, self project
        st, r = self.post("/api/p/t/action/merge", {"branch_sha": "abc", "main_sha": p.main})
        self.assertEqual(st, 400, r)
        self.assertIn("40-hex", r["error"])
        st, r = self.post("/api/p/t/action/merge", {"branch_sha": p.main, "main_sha": p.main})
        self.assertEqual(st, 400, r)
        self.assertIn("moved", r["error"])
        self.toml.write_text(self.toml.read_text() + "self = true\n")
        row = [x for x in self.get("/api/projects") if x["name"] == "t"][0]
        self.assertEqual((row["round"]["merge_ready"], row["round"]["self"]), (False, True))
        self.assertIn("Install update", self.get("/api/p/t")["round"]["merge"]["why"])
        st, r = self.post("/api/p/t/action/merge", {"branch_sha": p.head, "main_sha": p.main})
        self.assertEqual(st, 400, r)
        self.assertIn("Install update", r["error"])
        self.assertEqual(origin_ref(p.e, "change/C1"), p.head)
        self.assertEqual(p.state().data["round"]["status"], "open")
        self.toml.write_text(self.toml.read_text().replace("self = true\n", ""))
        # the tap: record goes merging at once, the spawned CLI finishes the merge
        st, r = self.post("/api/p/t/action/merge", {"branch_sha": p.head, "main_sha": p.main})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["output"], "merge started")
        for _ in range(300):
            rec = p.state().data["round"]
            if rec["status"] == "merged":
                break
            self.assertEqual(rec["status"], "merging")
            time.sleep(0.1)
        self.assertEqual(rec["status"], "merged")
        self.assertEqual(origin_ref(p.e, "main"), rec["merged_sha"])
        d = self.get("/api/p/t")
        self.assertEqual(d["round"]["merge"]["state"], "merged")
        self.assertIn("merged round C1", d["round"]["log"])
        self.assertEqual([x["event"] for x in d["round"]["history"]], ["opened", "merged"])
        row = [x for x in self.get("/api/projects") if x["name"] == "t"][0]
        self.assertEqual((row["round"]["status"], row["round"]["merge_ready"]), ("merged", False))
        st, r = self.post("/api/p/t/action/merge", {"branch_sha": p.head, "main_sha": p.main})
        self.assertEqual(st, 400)
        st, r = self.post("/api/p/t/action/discard", {"branch_sha": p.head})
        self.assertEqual(st, 400)

    def test_discard_tap(self):
        import time
        p = self.p
        st, r = self.post("/api/p/t/action/discard", {"branch_sha": p.main})
        self.assertEqual(st, 400, r)
        self.assertIn("moved", r["error"])
        with Lock(p.cfg.state_dir / "runner.lock"):
            st, r = self.post("/api/p/t/action/discard", {"branch_sha": p.head})
            self.assertEqual(st, 400, r)
            self.assertIn("running", r["error"])
        st, r = self.post("/api/p/t/action/discard", {"branch_sha": p.head})
        self.assertEqual(st, 200, r)
        for _ in range(300):
            if "round" not in p.state().data:
                break
            time.sleep(0.1)
        self.assertNotIn("round", p.state().data)
        self.assertEqual(origin_ref(p.e, "change/C1"), "")
        d = self.get("/api/p/t")
        self.assertEqual(d["round"]["merge"]["state"], "none")
        self.assertEqual([x["event"] for x in d["round"]["history"]], ["opened", "discarded"])
        self.assertEqual([s["status"] for s in d["steps"]], ["passed", "passed", "pending", "pending"])
        row = [x for x in self.get("/api/projects") if x["name"] == "t"][0]
        self.assertIsNone(row["round"])


def dashboard_mod():
    from hands import dashboard
    return dashboard


class TestOpenRoundRequest(unittest.TestCase):
    def test_round_record_carries_the_amendment_request_and_pr(self):
        b = Built([C1])
        e = b.e
        pd = e.cfg.state_dir / "plan"
        pd.mkdir(parents=True, exist_ok=True)
        (pd / "plan.json").write_text(json.dumps({"mode": "amend", "round": 1, "state": "approved", "request": "add a pricing page\nwith tiers",
                                                  "pr": 7, "pr_url": "https://x/pull/7"}))
        e.queue(CODER_P, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        rec = r.state.data["round"]
        self.assertEqual((rec["request"], rec["pr"], rec["pr_url"]), ("add a pricing page\nwith tiers", 7, "https://x/pull/7"))
        ev = merge.events(e.cfg.state_dir)
        self.assertEqual((ev[0]["event"], ev[0]["pr"]), ("opened", 7))
        sent = []
        merge.merge_round(e.cfg, State(e.cfg.state_dir), log, sent.append, origin_ref(e, "change/C1"), b.main)
        self.assertEqual(git(e.tmp / "app.git", "log", "-1", "--format=%s", "main").strip(), "Change C1: add a pricing page")
        self.assertIn("add a pricing page", sent[-1])


class TestReviewFixes(unittest.TestCase):
    def test_merge_refuses_a_branch_head_after_the_last_passed_step(self):
        """A hand-pushed commit fast-forwarded into the clone is not the tested head: refused even though it is
        exactly what the page would show."""
        p = Round()
        app = p.cfg.app_dir
        (app / "src" / "extra.js").write_text("x")
        gitops.git(app, "add", "-A")
        gitops.git(app, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "by hand")
        extra = gitops.head(app)
        gitops.git(app, "push", "-q", "origin", "change/C1")
        t = merge.tap_state(p.cfg, p.state().data, ["S01", "S02", "C1-01", "C1-02"])
        self.assertEqual((t["ready"], t["branch_sha"]), (True, extra))
        with self.assertRaises(merge.MergeError) as cm:
            p.merge(branch_sha=extra)
        self.assertIn("commits after the last passed step C1-02", str(cm.exception))
        self.assertEqual(origin_ref(p.e, "main"), p.main)
        self.assertEqual(origin_ref(p.e, "change/C1"), extra)

    def test_discard_resets_failed_steps_too_and_the_ui_lists_every_round_step(self):
        p = Round(extra=(C1, C1B), queue=(CODER_P, EVAL_PASS, {"role": "coder", "files": {}, "text": "no"}), max_steps=1)
        st = p.state()
        st.data["steps"]["C1-02"] = {"status": "failed", "attempts": 2, "reasons": ["x"]}      # as Runner.fail writes it
        st.save()
        t = merge.tap_state(p.cfg, st.data, ["S01", "S02", "C1-01", "C1-02"])
        self.assertEqual((t["passed"], t["total"], t["pending"]), (1, 2, ["C1-02"]))
        p.discard()
        d = p.state().data
        self.assertEqual(d["steps"]["C1-02"], {"status": "pending", "attempts": 0, "reasons": []})
        self.assertEqual(merge.events(p.cfg.state_dir)[-1]["steps"], ["C1-01", "C1-02"])

    def test_discard_without_push_is_pinned_to_the_local_head(self):
        p = Round()
        p.cfg.push = False
        with self.assertRaises(merge.MergeError) as cm:
            p.discard(p.main)
        self.assertIn("moved", str(cm.exception))
        self.assertEqual(p.state().data["round"]["status"], "open")
        p.discard()
        self.assertNotIn("round", p.state().data)
        self.assertEqual(origin_ref(p.e, "change/C1"), p.head)          # push off: origin untouched

    def test_lock_held_refusal_does_not_touch_state(self):
        p = Round()
        before = (p.cfg.state_dir / "state.json").read_text()
        with Lock(p.cfg.state_dir / "runner.lock"):
            with self.assertRaises(merge.LockedError):
                p.merge()
        self.assertEqual((p.cfg.state_dir / "state.json").read_text(), before)

    def test_branch_pushed_after_the_check_is_left_on_origin(self):
        p = Round()
        src = p.e.tmp / "appsrc"
        git(src, "fetch", "-q", "origin")
        orig = merge.gitops.push_ref

        def racy(repo, srcref, dst):
            if srcref == "" and dst.endswith("change/C1"):
                raise AssertionError("must not delete a moved branch")
            if dst.endswith("/main"):
                merge.gitops.push_ref = orig
                git(src, "checkout", "-q", "-B", "change/C1", "origin/change/C1")
                (src / "late.txt").write_text("l\n")
                git(src, "add", "-A")
                git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "late")
                git(src, "push", "-q", "origin", "change/C1")
                r = orig(repo, srcref, dst)
                merge.gitops.push_ref = racy
                return r
            return orig(repo, srcref, dst)

        merge.gitops.push_ref = racy
        try:
            rec = p.merge()
        finally:
            merge.gitops.push_ref = orig
        self.assertEqual(rec["status"], "merged")
        self.assertNotEqual(origin_ref(p.e, "change/C1"), "")              # kept, with the late commit
        self.assertEqual(origin_ref(p.e, "refs/tags/hands/merged/C1"), p.head)
        self.assertTrue(any("left on origin" in m for m in LOG))


if __name__ == "__main__":
    unittest.main()
