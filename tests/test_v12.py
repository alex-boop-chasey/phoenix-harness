"""0.5 step 12: one Session manager session can be live on Telegram (claude --channels ...). No docker or Claude."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import sessions  # noqa: E402
from test_v10 import SECRETS, FakeBackend, Dash  # noqa: E402

CHANNEL = "plugin:telegram@claude-plugins-official"


class TestLiveArgv(unittest.TestCase):
    def test_live_argv_has_channels_flag_and_default_permissions(self):
        sid = sessions.new_id()
        for resume in (False, True):
            plain = sessions.claude_argv(sid, resume)
            live = sessions.claude_argv(sid, resume, telegram=True)
            self.assertEqual(live, plain + ["--settings", sessions.TELEGRAM_SETTINGS, "--channels", CHANNEL])
            # the per-session enable is exactly one plugin set to true, nothing else (requirement 8)
            self.assertEqual(json.loads(sessions.TELEGRAM_SETTINGS), {"enabledPlugins": {"telegram@claude-plugins-official": True}})
            self.assertEqual(CHANNEL, f"plugin:{sessions.TELEGRAM_PLUGIN}")
            self.assertNotIn("--settings", plain)
            self.assertEqual(live[1], "--resume" if resume else "--session-id")
            for bad in ("--dangerously-skip-permissions", "--permission-mode", "--dangerously-load-development-channels"):
                self.assertNotIn(bad, live)
            self.assertNotIn("--channels", plain)
            run = sessions.spawn_argv("/x", live)
            self.assertEqual(run[-2:], ["--channels", CHANNEL])
            self.assertNotIn("dangerously", " ".join(run))
            self.assertNotIn("--channels", sessions.spawn_argv("/x", plain))
        # the channel name is a constant the tests pin: nothing else may ever be passed to --channels
        self.assertEqual(sessions.TELEGRAM_CHANNEL, CHANNEL)
        # the record flag is what PtyBackend.start reads: a stray truthy value never widens it
        self.assertFalse(sessions.is_telegram({"id": sid, "cwd": "/x"}))
        self.assertFalse(sessions.is_telegram({"id": sid, "cwd": "/x", "telegram": "yes"}))
        self.assertTrue(sessions.is_telegram({"id": sid, "cwd": "/x", "telegram": True}))


class TestSetupSafety(unittest.TestCase):
    def test_bun_on_the_session_path_and_nothing_else_new(self):
        env = sessions.session_env()
        home = str(Path.home())
        self.assertIn(f"{home}/.bun/bin", env["PATH"].split(":"))
        self.assertEqual(set(env) - set(sessions.PASSTHROUGH), {"PATH", "HOME", "TERM", "LANG", "USER", "SHELL"})

    def test_slash_commands_never_become_titles(self):
        # `/telegram:configure <token>` typed as a first prompt must not surface in the session list
        d = Path(tempfile.mkdtemp(prefix="hands-t-"))
        p = d / "t.jsonl"
        lines = [{"type": "user", "message": {"role": "user", "content": "/telegram:configure 123456789:AAHsecretsecret"}},
                 {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "/telegram:access pair ab12cd"}]}},
                 {"type": "user", "message": {"role": "user", "content": "what folder are you in?"}}]
        p.write_text("".join(json.dumps(o) + "\n" for o in lines))
        self.assertEqual(sessions.read_title(p), "what folder are you in?")
        p.write_text(json.dumps(lines[0]) + "\n")
        self.assertIsNone(sessions.read_title(p))


class TestOneLiveSession(unittest.TestCase):
    def setUp(self):
        self.d = Dash()
        self.home = Path(tempfile.mkdtemp(prefix="hands-h-"))
        (self.home / "ops").mkdir()
        (self.home / "work").mkdir()
        self.claude = Path(tempfile.mkdtemp(prefix="hands-cl-"))
        self.old_env = dict(os.environ)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude)
        os.environ.update(SECRETS)
        self.old_home = sessions.HOME
        sessions.HOME = self.home
        self.old_backend = sessions.BACKEND
        sessions.BACKEND = self.fake = FakeBackend()

    def tearDown(self):
        self.d.close()
        self.fake.close()
        sessions.HOME = self.old_home
        sessions.BACKEND = self.old_backend
        os.environ.clear()
        os.environ.update(self.old_env)

    def post(self, path, body=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.d.port}", "Cookie": f"{__import__('hands.dashboard').dashboard.COOKIE}={self.d.sid}",
             "Origin": f"http://127.0.0.1:{self.d.port}", "X-Hands": "1"}
        c.request("POST", path, headers=h, body=json.dumps(body if body is not None else {}).encode())
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw or b"{}")

    def get(self, path):
        st, _, body = self.d.req("GET", path)
        return st, json.loads(body or b"{}")

    def last_argv(self):
        return self.fake.started[-1][1]

    def test_only_one_live_session(self):
        st, a = self.post("/api/sessions/new", {"telegram": True})
        self.assertEqual(st, 200, a)
        self.assertTrue(a["telegram"])
        self.assertTrue(a["live"])
        self.assertEqual(self.last_argv()[-2:], ["--channels", CHANNEL])
        self.assertNotIn("--dangerously-skip-permissions", self.last_argv())
        # a second live session is refused while the first runs; a plain session is fine
        st, err = self.post("/api/sessions/new", {"telegram": True, "cwd": str(self.home / "work")})
        self.assertEqual(st, 409, err)
        self.assertIn("Telegram", err["error"])
        self.assertEqual(len(self.fake.started), 1)                  # nothing was started
        st, b = self.post("/api/sessions/new", {"cwd": str(self.home / "work")})
        self.assertEqual(st, 200, b)
        self.assertFalse(b["telegram"])
        self.assertNotIn("--channels", self.last_argv())
        # the listing carries the flag; the record keeps it on disk
        st, rows = self.get("/api/sessions")
        self.assertEqual({r["id"]: r["telegram"] for r in rows}, {a["id"]: True, b["id"]: False})
        self.assertTrue(sessions.Store().get(a["id"])["telegram"])
        self.assertFalse(sessions.Store().get(b["id"]).get("telegram", False))
        # resuming b as live while a is live: refused, and b is not started or re-flagged
        self.post(f"/api/sessions/{b['id']}/stop")
        st, err = self.post(f"/api/sessions/{b['id']}/resume", {"telegram": True})
        self.assertEqual(st, 409, err)
        self.assertNotIn(b["id"], self.fake.alive_ids)
        self.assertFalse(sessions.Store().get(b["id"]).get("telegram", False))
        # once a stops, its flag stays (resume keeps it live) and the slot is free for b
        st, r = self.post(f"/api/sessions/{a['id']}/stop")
        self.assertEqual((st, r["live"], r["telegram"]), (200, False, True))
        st, r = self.post(f"/api/sessions/{b['id']}/resume", {"telegram": True})
        self.assertEqual((st, r["live"], r["telegram"]), (200, True, True), r)
        self.assertEqual(self.last_argv()[1:], ["--resume", b["id"], "--settings", sessions.TELEGRAM_SETTINGS, "--channels", CHANNEL])
        st, err = self.post(f"/api/sessions/{a['id']}/resume")             # a still flagged: conflicts with b
        self.assertEqual(st, 409, err)
        self.assertNotIn(a["id"], self.fake.alive_ids)
        # a can come back without Telegram (the flag is cleared and persisted); then b can be made plain too
        st, r = self.post(f"/api/sessions/{a['id']}/resume", {"telegram": False})
        self.assertEqual((st, r["live"], r["telegram"]), (200, True, False), r)
        self.assertEqual(self.last_argv()[1:], ["--resume", a["id"]])
        self.assertFalse(sessions.Store().get(a["id"])["telegram"])
        # bad flag values are 400 and change nothing
        self.post(f"/api/sessions/{b['id']}/stop")
        for bad in ("yes", 1, [], {}):                     # (JSON null = "keep the flag", tested above by plain resume)
            st, err = self.post(f"/api/sessions/{b['id']}/resume", {"telegram": bad})
            self.assertEqual(st, 400, (bad, err))
            self.assertNotIn(b["id"], self.fake.alive_ids)
        st, err = self.post("/api/sessions/new", {"telegram": "true"})
        self.assertEqual(st, 400, err)
        # the defaults tell the page which channel a live session uses
        st, info = self.get("/api/sessions/defaults")
        self.assertEqual(info["telegram_channel"], CHANNEL)

    def test_live_session_without_flag_is_the_default(self):
        st, a = self.post("/api/sessions/new")
        self.assertEqual(st, 200, a)
        self.assertFalse(a["telegram"])
        self.assertNotIn("--channels", self.last_argv())
        st, r = self.post(f"/api/sessions/{a['id']}/stop")
        st, r = self.post(f"/api/sessions/{a['id']}/resume")
        self.assertEqual((st, r["telegram"]), (200, False))
        self.assertNotIn("--channels", self.last_argv())


if __name__ == "__main__":
    unittest.main()
