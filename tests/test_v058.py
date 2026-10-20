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
        self.assertEqual(p.state().data["round"]["status"], "open")
        self.assertEqual(gitops.git(p.cfg.app_dir, "rev-parse", "main").strip(), p.main)   # local main put back
        self.assertTrue(gitops.is_clean(p.cfg.app_dir))

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


if __name__ == "__main__":
    unittest.main()
