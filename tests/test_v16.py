"""0.5 step 16: fixes from the independent security review (docs/review-0.5.md)."""
import base64
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import config as cfgmod, dashboard, gitops, planner, projectenv, sessions  # noqa: E402
import test_v08 as t8  # noqa: E402
from test_runner import Env, STEP1, GOOD1, EVAL_PASS  # noqa: E402

SECRET = t8.SECRET


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class TestRedactionFixes(unittest.TestCase):
    """Review findings 1a, 1b, 2, 10."""

    def test_dashboard_redacts_attempt_files_while_serving(self):
        from test_v04 import Dash
        d = Dash()
        try:
            home = d.home
            state = home / "state"
            (state / "attempts" / "S01-1").mkdir(parents=True)
            (state / "attempts" / "S01-1" / "coder.stderr").write_text(f"token {SECRET} seen\n")
            (state / "attempts" / "S01-1" / "accept-1.log").write_text(f"STRIPE_KEY={SECRET}\n")
            (state / "runner.log").write_text(f"leak {SECRET}\n")
            sec = home / "secrets" / "p"
            st = projectenv.Store(sec)
            st.set([{"key": "STRIPE_KEY", "value": SECRET, "public": False}])
            (home / "projects").mkdir(exist_ok=True)
            (home / "projects" / "p.toml").write_text(
                f'spec_dir = "{home}"\napp_dir = "{home}"\nstate_dir = "{state}"\nimage = "h:1"\n'
                f'project_env = "{sec}/project.env"\n')
            for f in ("coder.stderr", "accept-1.log"):
                code, _, raw = d.req("GET", f"/api/p/p/attempts/S01-1/{f}")
                self.assertEqual(code, 200)
                self.assertNotIn(SECRET.encode(), raw, f)
                self.assertIn(b"[REDACTED:STRIPE_KEY]", raw)
            code, _, raw = d.req("GET", "/api/p/p/log?lines=10")
            self.assertEqual(code, 200)
            self.assertNotIn(SECRET.encode(), raw)
            code, _, raw = d.req("GET", "/api/p/p/attempts")
            self.assertEqual(code, 200)
            self.assertNotIn(SECRET.encode(), raw)
        finally:
            d.close()

    def test_recover_redacts_interrupted_attempt(self):
        e = t8.with_env([STEP1])
        adir = e.cfg.state_dir / "attempts" / "S01-1"
        adir.mkdir(parents=True)
        (adir / "coder.json").write_text(f'{{"result": "cat gave {SECRET}"}}\n')
        (adir / "coder.stderr").write_text(f"{SECRET}\n")
        r = e.runner()
        base = git("rev-parse", "HEAD", cwd=e.cfg.app_dir).strip()
        r.state.data["current"] = {"step": "S01", "attempt": 1, "base": base, "phase": "coder"}
        r.state.save()
        r.load_project_env()
        r.recover()
        for f in ("coder.json", "coder.stderr"):
            data = (adir / f).read_bytes()
            self.assertNotIn(SECRET.encode(), data, f)
            self.assertIn(b"[REDACTED:STRIPE_KEY]", data)

    def test_preflight_refuses_reserved_keys_in_project_env(self):
        e = t8.with_env([STEP1])
        with open(e.cfg.project_env, "a") as f:
            f.write("GH_TOKEN=ghp_handwritten\n")
        with self.assertRaises(SystemExit) as cm:
            e.runner().preflight()
        self.assertIn("GH_TOKEN", str(cm.exception))
        self.assertIn("reserved", str(cm.exception))

    def test_redactor_matches_encoded_forms(self):
        r = projectenv.Redactor({"KEY": SECRET})
        b64 = base64.b64encode(SECRET.encode()).decode()
        quoted = urllib.parse.quote("pw:" + SECRET, safe="")
        self.assertIn("[REDACTED:KEY]", r.text(f"Authorization: Basic {b64}"))
        self.assertNotIn(SECRET, r.text(quoted))
        self.assertEqual(r.found(b64.encode()), ["KEY"])
        self.assertEqual(r.found(b"abc"), [])
        plain = projectenv.Redactor({"PLAIN": "simplevalue1"})       # no special characters: quoted form = raw
        self.assertEqual(plain.text("simplevalue1"), "[REDACTED:PLAIN]")


class TestOtherFixes(unittest.TestCase):
    """Review findings 6, 7, 8, 11."""

    def test_refresh_app_refuses_dirty_clone(self):
        t = Path(tempfile.mkdtemp(prefix="hands-ra-"))
        git("init", "-q", "--bare", "-b", "main", str(t / "o.git"))
        git("clone", "-q", str(t / "o.git"), str(t / "c"))
        (t / "c" / "a.txt").write_text("1\n")
        git("add", "-A", cwd=t / "c")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a", cwd=t / "c")
        git("push", "-q", "origin", "main", cwd=t / "c")
        self.assertEqual(gitops.refresh_app(t / "c", "main"), git("rev-parse", "HEAD", cwd=t / "c").strip())
        (t / "c" / "wip.txt").write_text("someone's work\n")
        with self.assertRaises(gitops.GitError) as cm:
            gitops.refresh_app(t / "c", "main")
        self.assertIn("wip.txt", str(cm.exception))
        self.assertTrue((t / "c" / "wip.txt").is_file(), "local work must survive")

    def test_vendored_xterm_pinned(self):
        pins = {"xterm.min.js": "4196e242ef1cf4c2adead8d97f4a772a69576076f70b095e004b4abbb049e7bf",
                "addon-fit.min.js": "a6a7bbb33569f16aa3e18d71425e34d035fc89a0b7e8cba084f8855f91aa38f1",
                "xterm.min.css": "f7f724aea2bb620a6482bfb8e4bdecfae1152b0c7facef55fbda61f3b6cfedb2",
                # morphdom 2.7.4 dist/morphdom-umd.min.js (MIT): in-place patching of the builder page (0.6)
                "morphdom-2.7.4.min.js": "7de128d61778142f65dcd7ee02d6f2a2a2dfa086bf508d3083de4b42ebe464cc"}
        vend = ROOT / "hands" / "static" / "vendor"
        for name, sha in pins.items():
            self.assertEqual(hashlib.sha256((vend / name).read_bytes()).hexdigest(), sha, name)
        self.assertEqual(sorted(p.name for p in vend.iterdir() if p.is_file()), sorted(pins))

    def test_planner_prompt_marks_summary_untrusted(self):
        cfg = cfgmod.Config(name="x", spec_dir=Path("/s"), app_dir=Path("/a"), state_dir=Path("/t"), image="h:1")
        text = planner.prompt(cfg, "idea", "", [], False, code_summary="### Facts\n- x")
        self.assertIn("untrusted", text.lower())

    def test_session_delete_removes_subagent_dir(self):
        from test_v10 import TestStore
        t = TestStore("test_records_private_and_listing")
        t.setUp()
        try:
            st = sessions.Store()
            rec = st.create(t.home / "ops")
            tp = sessions.transcript_path(t.claude, t.home / "ops", rec["id"])
            tp.parent.mkdir(parents=True)
            tp.write_text("{}\n")
            sub = tp.with_suffix("")
            (sub / "agent-1").mkdir(parents=True)
            (sub / "agent-1" / "x.jsonl").write_text("{}\n")
            other = tp.parent / "keep"
            other.mkdir()
            (other / "o.jsonl").write_text("{}\n")
            outside = Path(tempfile.mkdtemp(prefix="hands-out-"))
            (outside / "victim").write_text("v")
            link = tp.with_name(f"{sessions.new_id()}")        # a symlinked "subagent dir" is never followed
            os.symlink(outside, link)
            st.delete(rec["id"])
            self.assertFalse(tp.exists())
            self.assertFalse(sub.exists())
            self.assertTrue((other / "o.jsonl").exists())
            self.assertTrue((outside / "victim").exists())
            rec2 = st.create(t.home / "ops")
            tp2 = sessions.transcript_path(t.claude, t.home / "ops", rec2["id"])
            os.symlink(outside, tp2.with_suffix(""))
            st.delete(rec2["id"])
            self.assertTrue((outside / "victim").exists(), "symlinked subagent dir must not be followed")
        finally:
            t.tearDown()


if __name__ == "__main__":
    unittest.main()
