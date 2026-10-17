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

from hands import config as cfgmod, dashboard, files, gitops, planner, projectenv, sessions  # noqa: E402
import test_v13 as tf  # noqa: E402
import test_v08 as t8  # noqa: E402
from test_runner import Env, STEP1, GOOD1, EVAL_PASS  # noqa: E402

SECRET = t8.SECRET


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class TestFilesFixes(tf.Base):
    """Review findings 1c, 3, 4, 5, 9."""

    def project(self, name, state_dir):
        (self.d.home / "projects").mkdir(exist_ok=True)
        (self.d.home / "projects" / f"{name}.toml").write_text(
            f'spec_dir = "{self.work}/sub"\napp_dir = "{self.work}/sub"\nstate_dir = "{state_dir}"\nimage = "h:1"\n')

    def test_project_state_dirs_denied(self):
        state = self.work / "proj" / "state"
        (state / "attempts" / "S01-1").mkdir(parents=True)
        (state / "attempts" / "S01-1" / "coder.json").write_text(f'{{"secret": "{SECRET}"}}')
        (self.work / "proj" / "app").mkdir()
        self.project("proj", state)
        self.assertIn(str(state), files.denied_paths())
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="proj/state")))
        self.assertEqual(st, 403, body)
        st, _, raw = self.get("/api/files/read?" + self.q(root=str(self.work), path="proj/state/attempts/S01-1/coder.json"))
        self.assertEqual(st, 403)
        self.assertNotIn(SECRET.encode(), raw)
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="proj")))
        self.assertEqual(st, 200)
        self.assertEqual([e["name"] for e in body["entries"]], ["app"], "the state folder is not listed")
        st, body = self.j(self.get("/api/files/search?" + self.q(root=str(self.work), q="coder")))
        self.assertEqual(st, 200)
        self.assertFalse(any("state" in h["path"] for h in body["hits"]))
        self.assertIn("~/work/proj/state", files.denied_display())
        # a project config pointing the state dir somewhere strange still only adds that path
        self.project("weird", Path("/nonexistent/x"))
        self.assertIn("/nonexistent/x", files.denied_paths())
        self.assertEqual(self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="")))[0], 200)

    def test_hidden_roots_in_settings_file_dropped(self):
        (self.home / ".config" / "claude-harness").mkdir()
        (self.home / ".config" / "claude-harness" / "token").write_text("tok\n")
        (self.home / ".local").mkdir()
        files.settings_path().write_text(json.dumps({"roots": [str(self.home / ".config"), str(self.home / ".local"),
                                                               str(self.work)], "git_writes": False}))
        self.assertEqual(files.settings()["roots"], [str(self.work)])
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.home / ".config"), path="")))
        self.assertIn(st, (403, 404), body)

    def test_failed_upload_closes_the_connection(self):
        (self.work / "taken.txt").write_text("old")
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        body = b"x" * 5000
        q = self.q(root=str(self.work), path="taken.txt")
        req = (f"POST /api/files/upload?{q} HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
               f"Cookie: {dashboard.COOKIE}={self.d.sid}\r\nOrigin: http://127.0.0.1:{self.d.port}\r\nX-Hands: 1\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode()
        s.sendall(req)                                   # headers first: the server answers 409 before the body
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.1 409"), data[:80])
        self.assertIn(b"connection: close", data.lower())
        try:
            s.sendall(body)                              # the leftover body must not be parsed as a request
        except OSError:
            pass
        s.settimeout(5)
        try:
            rest = s.recv(65536)
        except OSError:
            rest = b""
        s.close()
        self.assertEqual(rest, b"", "the server must close the socket instead of reading the body as a request")
        self.assertEqual(self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="")))[0], 200)

    def test_git_guard_covers_folders_containing_git(self):
        clone = self.work / "clone"
        (clone / ".git" / "objects").mkdir(parents=True)
        (clone / "src").mkdir()
        (clone / "src" / "a.js").write_text("1")
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "clone", "recursive": True})[0], 403)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "clone", "to": "clone2"})[0], 403)
        self.assertTrue((clone / ".git").is_dir())
        # a plain sub-folder of the clone is still fine
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "clone/src", "to": "clone/lib"})[0], 200)
        files.save_settings({"roots": [str(self.work), str(self.other)], "git_writes": True})
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "clone", "recursive": True})[0], 200)
        self.assertFalse(clone.exists())

    def test_upload_temp_name_fits_name_max(self):
        name = "é" * 120 + ".txt"                      # 244 bytes: allowed, but 124 characters only
        st, body = self.j(self.post("/api/files/upload?" + self.q(root=str(self.work), path=name), raw=b"data",
                                    headers={"Content-Type": "application/octet-stream"}))
        self.assertEqual(st, 200, body)
        self.assertEqual((self.work / name).read_bytes(), b"data")
        self.assertFalse(any(p.name.startswith(".") and "upload" in p.name for p in self.work.iterdir()))


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
                "xterm.min.css": "f7f724aea2bb620a6482bfb8e4bdecfae1152b0c7facef55fbda61f3b6cfedb2"}
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
