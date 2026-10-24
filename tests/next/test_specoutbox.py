"""hands.specoutbox: the per-project mirror of harness-owned spec paths."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hands import specoutbox  # noqa: E402


def write(base: Path, rel: str, data: str = "x") -> Path:
    f = base / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(data)
    return f


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.tmp = Path(self._t.name)
        self.root = self.tmp / "ob"
        self.spec = self.tmp / "spec"
        write(self.spec, "steps.json", "{}")
        write(self.spec, "refs/old.txt", "old")
        write(self.spec, "refs/style-refs/01/report.md", "r")


class Module(Base):
    def test_paths_are_validated(self):
        for ok in ("brief.md", "brief.sha256", "briefs/change-2.md", "refs/index.json", "refs/design/d001/a.png"):
            self.assertTrue(specoutbox.is_harness_path(ok), ok)
        for bad in ("steps.json", "PLAN.md", "refs/../steps.json", "/etc/passwd", "briefs/a/b.md", "refs/a//b", "refs/./x"):
            self.assertFalse(specoutbox.is_harness_path(bad), bad)
        with self.assertRaises(ValueError):
            specoutbox.put_bytes(self.root, "steps.json", b"x")

    def test_put_then_delete_then_put_undeletes(self):
        specoutbox.delete(self.root, "refs/old.txt")
        self.assertEqual(specoutbox.listing(self.root)["deletions"], ["refs/old.txt"])
        specoutbox.put_bytes(self.root, "refs/old.txt", b"new")
        self.assertEqual(specoutbox.listing(self.root), {"files": ["refs/old.txt"], "deletions": []})

    def test_delete_removes_subtree_from_outbox(self):
        src = self.tmp / "src"
        write(src, "a.png")
        specoutbox.put_tree(self.root, "refs/design/d1", src)
        specoutbox.delete(self.root, "refs/design/d1")
        self.assertEqual(specoutbox.listing(self.root)["files"], [])

    def test_changed_overlay_check_and_clear(self):
        specoutbox.put_bytes(self.root, "refs/style-refs/01/report.md", b"r")
        specoutbox.put_bytes(self.root, "refs/index.json", b"{}")
        specoutbox.delete(self.root, "refs/old.txt")
        specoutbox.delete(self.root, "refs/gone.txt")
        self.assertEqual(specoutbox.changed(self.root, self.spec), {"changed": ["refs/index.json"], "deleted": ["refs/old.txt"]})
        cand = self.tmp / "cand"
        subprocess.run(["cp", "-r", str(self.spec), str(cand)], check=True)
        specoutbox.overlay(self.root, cand)
        self.assertFalse((cand / "refs/old.txt").exists())
        self.assertEqual(specoutbox.check(self.root, self.spec, cand), [])
        write(cand, "refs/index.json", "{1}")
        write(cand, "refs/old.txt", "back")
        (cand / "refs/style-refs/01/report.md").unlink()
        self.assertEqual(specoutbox.check(self.root, self.spec, cand), [
            "refs/index.json: harness-owned file changed by the planner",
            "refs/old.txt: harness-owned file changed by the planner",
            "refs/style-refs/01/report.md: harness-owned file deleted by the planner"])
        specoutbox.clear_merged(self.root, self.spec)
        self.assertEqual(specoutbox.listing(self.root), {"files": ["refs/index.json"], "deletions": ["refs/old.txt"]})

    def test_design_ids_lists_the_design_entries_a_diff_touches(self):
        paths = ["brief.md", "refs/design/d002/a.png", "refs/design/d002/b.md", "refs/design/d010/x.svg",
                 "refs/index.json", "refs/prototype/index.html", "refs/design/../x"]
        self.assertEqual(specoutbox.design_ids(paths), ["d002", "d010"])
        self.assertEqual(specoutbox.design_ids(["brief.md"]), [])

    def test_design_changed(self):
        self.assertTrue(specoutbox.design_changed(["brief.md", "refs/design/d1/a.png"]))
        self.assertTrue(specoutbox.design_changed(["refs/index.json"]))
        self.assertTrue(specoutbox.design_changed(["refs/prototype/index.html"]))
        self.assertFalse(specoutbox.design_changed(["brief.md", "refs/style-refs/01/report.md", "refs/setup.json"]))


class Cli(Base):
    def run_cli(self, *a):
        return subprocess.run([sys.executable, "-m", "hands.specoutbox", *a], cwd=ROOT, capture_output=True, text=True)

    def test_verbs(self):
        f = write(self.tmp, "in.json", "{}")
        self.assertEqual(self.run_cli("put", "--root", str(self.root), "--path", "refs/index.json", "--from", str(f)).returncode, 0)
        self.assertEqual(self.run_cli("put", "--root", str(self.root), "--path", "steps.json", "--from", str(f)).returncode, 1)
        self.assertEqual(self.run_cli("delete", "--root", str(self.root), "--path", "refs/old.txt").returncode, 0)
        r = self.run_cli("changed", "--root", str(self.root), "--spec", str(self.spec), "--json")
        self.assertEqual(json.loads(r.stdout), {"changed": ["refs/index.json"], "deleted": ["refs/old.txt"]})
        self.assertEqual(self.run_cli("clear-merged", "--root", str(self.root), "--spec", str(self.spec)).returncode, 0)


class PlannerWiring(Base):
    def cfg(self):
        from types import SimpleNamespace
        subprocess.run(["git", "init", "-q", str(self.spec)], check=True)
        subprocess.run(["git", "-C", str(self.spec), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.spec), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
        return SimpleNamespace(managed=True, github_org="o", repo_specs="r", spec_branch="main", spec_dir=self.spec,
                               state_dir=self.tmp)

    def test_open_pr_adds_outbox_changes_and_deletions_to_the_tree(self):
        from hands import gitops, planner
        cfg = self.cfg()
        ob = specoutbox.root(cfg)
        specoutbox.put_bytes(ob, "refs/design/d1/a.png", b"PNG")
        specoutbox.delete(ob, "refs/old.txt")
        head = gitops.head(self.spec)
        calls = []

        class GH:
            def call(self, method, path, body=None, ok=None):
                calls.append((method, path, body))
                if path.endswith("/git/ref/heads/main"):
                    return 200, {"object": {"sha": head}}
                if "/git/commits/" in path:
                    return 200, {"tree": {"sha": "t0"}}
                if path.endswith("/git/blobs"):
                    return 201, {"sha": "blob1"}
                if path.endswith("/git/trees"):
                    return 201, {"sha": "t1"}
                if path.endswith("/git/commits"):
                    return 201, {"sha": "c1"}
                if "/git/ref/heads/" in path:
                    return 404, {}
                if "/pulls?" in path:
                    return 200, {"items": []}
                if path.endswith("/pulls"):
                    return 201, {"number": 7}
                return 201, {}

        real = planner.github
        planner.github = lambda: GH()
        self.addCleanup(setattr, planner, "github", real)
        cand = self.tmp / "cand"
        cand.mkdir()
        planner.open_pr(cfg, cand, [], "t", "b", lambda m: None)
        tree = next(b for m, p, b in calls if p.endswith("/git/trees"))["tree"]
        self.assertEqual(sorted((e["path"], e["sha"]) for e in tree),
                         [("refs/design/d1/a.png", "blob1"), ("refs/old.txt", None)])

    def test_check_output_rejects_planner_edits_of_harness_files(self):
        from hands import planner
        cfg = self.cfg()
        cand = self.tmp / "cand"
        subprocess.run(["cp", "-r", str(self.spec), str(cand)], check=True)
        write(cand, "refs/old.txt", "edited")
        names = planner.changed_files(self.spec, cand, ["refs/old.txt"])
        self.assertEqual(names, [])
        self.assertIn("refs/old.txt: harness-owned file changed by the planner", planner.check_output(cfg, cand, names))
