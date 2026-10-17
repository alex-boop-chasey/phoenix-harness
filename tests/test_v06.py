"""0.5 step 06: per-project environment variables (hands/projectenv.py + dashboard API). No docker or Claude."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401  (removes every mkdtemp dir after each test)
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import config as cfgmod, dashboard, newproject, projectenv  # noqa: E402
from hands.state import Lock  # noqa: E402
from test_newproject import Base  # noqa: E402
from test_v04 import Dash  # noqa: E402

TRICKY = {
    "A_EQUALS": "a=b=c",
    "HASH": "value # not a comment",
    "QUOTES": "\"double\" and 'single' and \\backslash",
    "SPACES": "  leading, inner  and trailing  ",
    "UNICODE": "héllo wörld ✓ 日本語",
    "EMPTY": "",
    "URL": "postgres://user:p%40ss@host:5432/db?sslmode=require",
    "JSONISH": '{"k": [1, 2, {"x": "y"}]}',
    "BIG": "x" * 8192,
}


class TestValidation(unittest.TestCase):
    def test_keys(self):
        for ok in ("A", "_", "ABC_123", "X" * 128, "STRIPE_KEY", "MY_HANDS_X"):
            projectenv.validate_key(ok)
        for bad in ("", "a", "1A", "A-B", "A B", "A.B", "X" * 129, "ÄB", "KEY=", "\n"):
            with self.assertRaises(projectenv.EnvError, msg=bad):
                projectenv.validate_key(bad)

    def test_reserved(self):
        for bad in ("CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "TG_TOKEN", "TG_CHAT_ID", "PATH", "HOME",
                    "USER", "SHELL", "CI", "NODE_OPTIONS", "HANDS_ROLE", "HANDS_", "CLAUDE_X", "CLAUDE_"):
            with self.assertRaises(projectenv.EnvError, msg=bad):
                projectenv.validate_key(bad)

    def test_values(self):
        for ok in TRICKY.values():
            projectenv.validate_value(ok)
        for bad in ("a\nb", "a\rb", "a\0b", "x" * 8193, "é" * 4097, 5, None):
            with self.assertRaises(projectenv.EnvError, msg=repr(bad)):
                projectenv.validate_value(bad)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-env-"))
        self.secrets = self.tmp / "secrets"
        self.d = self.secrets / "proj"

    def test_round_trip_tricky_values_in_docker_env_file_format(self):
        st = projectenv.Store(self.d)
        st.set([{"key": k, "value": v, "public": k in ("URL", "EMPTY")} for k, v in TRICKY.items()])
        again = projectenv.Store(self.d)
        self.assertEqual(again.values, TRICKY)
        self.assertEqual({k for k, m in again.meta.items() if m["public"]}, {"URL", "EMPTY"})
        # docker --env-file semantics: the value is everything after the first "=", verbatim (no quoting layer)
        lines = (self.d / "project.env").read_text().split("\n")
        for k, v in TRICKY.items():
            self.assertIn(f"{k}={v}", lines, k)
        for ln in lines:
            if ln and not ln.startswith("#"):
                self.assertRegex(ln, r"^[A-Z_][A-Z0-9_]*=")
        self.assertNotIn("\0", (self.d / "project.env").read_bytes().decode())
        self.assertEqual(again.problems, [])

    def test_modes_and_atomic(self):
        st = projectenv.Store(self.d)
        st.set([{"key": "A", "value": "valueA", "public": False}])
        self.assertEqual(self.secrets.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.d.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.d / "project.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.d / "env.json").stat().st_mode & 0o777, 0o600)
        os.chmod(self.d / "project.env", 0o644)         # loosened by hand: the next save fixes it
        st.set([{"key": "B", "value": "2", "public": True}])
        self.assertEqual((self.d / "project.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(self.d)), ["env.json", "project.env"])    # no temp files left
        # a bad item in a batch changes nothing (validated before anything is written)
        before = (self.d / "project.env").read_bytes(), (self.d / "env.json").read_bytes()
        with self.assertRaises(projectenv.EnvError):
            st.set([{"key": "C", "value": "3", "public": False}, {"key": "PATH", "value": "x", "public": False}])
        self.assertEqual(((self.d / "project.env").read_bytes(), (self.d / "env.json").read_bytes()), before)
        self.assertEqual(projectenv.Store(self.d).values, {"A": "valueA", "B": "2"})
        meta = json.loads((self.d / "env.json").read_text())
        self.assertEqual(set(meta["vars"]), {"A", "B"})
        for m in meta["vars"].values():
            self.assertNotIn("value", m)                  # metadata never holds values
        self.assertRegex(meta["vars"]["A"]["updated"], r"^\d{4}-\d\d-\d\dT")
        self.assertNotIn("valueA", (self.d / "env.json").read_text())

    def test_replace_visibility_and_delete(self):
        st = projectenv.Store(self.d)
        st.set([{"key": "K", "value": "old", "public": False}, {"key": "P", "value": "pub", "public": True}])
        st.set([{"key": "K", "value": "new", "public": False}])
        self.assertEqual(projectenv.Store(self.d).values["K"], "new")
        st.set([{"key": "P", "public": False}])           # visibility only: value kept
        again = projectenv.Store(self.d)
        self.assertEqual(again.values["P"], "pub")
        self.assertFalse(again.meta["P"]["public"])
        with self.assertRaises(projectenv.EnvError):
            st.set([{"key": "NEW", "public": True}])      # no value and not existing
        with self.assertRaises(projectenv.EnvError):
            st.set([{"key": "P", "public": True}])        # secret -> public without the value would reveal it
        self.assertFalse(projectenv.Store(self.d).meta["P"]["public"])
        st.set([{"key": "P", "value": "pub", "public": True}])
        self.assertTrue(projectenv.Store(self.d).meta["P"]["public"])
        st.delete("K")
        self.assertEqual(projectenv.Store(self.d).values, {"P": "pub"})
        with self.assertRaises(projectenv.EnvError):
            st.delete("K")
        self.assertNotIn("K", json.loads((self.d / "env.json").read_text())["vars"])

    def test_listing_masks_secrets(self):
        st = projectenv.Store(self.d)
        st.set([{"key": "SECRET", "value": "s3cr3t", "public": False}, {"key": "PUB", "value": "open", "public": True}])
        rows = {r["key"]: r for r in st.listing()}
        self.assertEqual(rows["PUB"]["value"], "open")
        self.assertNotIn("value", rows["SECRET"])
        self.assertTrue(rows["SECRET"]["secret"] and not rows["PUB"]["secret"])
        self.assertNotIn("s3cr3t", json.dumps(st.listing()))
        self.assertNotIn("length", rows["SECRET"])

    def test_hand_written_file_loads_as_secret(self):
        self.d.mkdir(parents=True)
        (self.d / "project.env").write_text("# dev/test secrets\nSTRIPE_KEY=sk_test_123\nQUOTED=\"keeps quotes\"\n\n"
                                            "lower=case\nNOEQ\n  INDENTED=ok\nDUP=1\nDUP=2\n")
        st = projectenv.Store(self.d)
        self.assertEqual(st.values, {"STRIPE_KEY": "sk_test_123", "QUOTED": "\"keeps quotes\"", "INDENTED": "ok", "DUP": "2"})
        self.assertTrue(all(not m["public"] and m["updated"] is None for m in st.meta.values()))
        self.assertEqual(len(st.problems), 2, st.problems)          # lower=case, NOEQ
        self.assertNotIn("sk_test_123", " ".join(st.problems))
        rows = {r["key"]: r for r in st.listing()}
        self.assertTrue(rows["STRIPE_KEY"]["secret"])
        st.set([{"key": "NEW", "value": "v", "public": True}])     # rewrite keeps the hand-written keys
        again = projectenv.Store(self.d)
        self.assertEqual(again.values["STRIPE_KEY"], "sk_test_123")
        self.assertEqual(again.values["NEW"], "v")
        self.assertEqual(again.problems, [])
        self.assertEqual((self.d / "project.env").stat().st_mode & 0o777, 0o600)

    def test_symlink_refused(self):
        self.d.mkdir(parents=True)
        target = self.tmp / "elsewhere"
        target.write_text("X=1\n")
        os.symlink(target, self.d / "project.env")
        with self.assertRaises(projectenv.EnvError):
            projectenv.Store(self.d)
        self.assertEqual(target.read_text(), "X=1\n")


class TestPending(Base):
    def test_pending_moves_into_place_when_create_succeeds(self):
        p = projectenv.pending_store("shop")
        p.set([{"key": "STRIPE_KEY", "value": "sk_test_1", "public": False}, {"key": "PUBLIC_URL", "value": "http://x", "public": True}])
        pdir = projectenv.pending_dir("shop")
        self.assertTrue(pdir.is_dir())
        self.assertEqual(pdir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(newproject.run_create("shop", "Online shop"), 0)
        self.assertFalse(pdir.exists())
        sec = self.cfg_home / "secrets" / "shop"
        st = projectenv.Store(sec)
        self.assertEqual(st.values, {"STRIPE_KEY": "sk_test_1", "PUBLIC_URL": "http://x"})
        self.assertTrue(st.meta["PUBLIC_URL"]["public"] and not st.meta["STRIPE_KEY"]["public"])
        self.assertEqual((sec / "project.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((sec / "env.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / "projects" / "shop" / "project.env").exists())
        self.assertFalse((self.home / "projects" / "shop" / "env.json").exists())
        cfg = cfgmod.load("shop")
        self.assertEqual(cfg.project_env, sec / "project.env")

    def test_pending_discarded_when_create_fails(self):
        self.gh.fail[("POST", "/repos/acme/half-app/rulesets")] = 422
        projectenv.pending_store("half").set([{"key": "K", "value": "v", "public": False}])
        self.assertEqual(newproject.run_create("half", "x"), 1)
        self.assertFalse(projectenv.pending_dir("half").exists())
        self.assertFalse((self.cfg_home / "secrets" / "half" / "env.json").exists())
        # a failed create before anything was made (bad slug) also leaves nothing behind
        projectenv.pending_store("freeby").set([{"key": "K", "value": "v", "public": False}])
        self.gh.plan = "free"
        self.assertEqual(newproject.run_create("freeby", "x"), 1)
        self.assertFalse(projectenv.pending_dir("freeby").exists())


class TestEnvApi(unittest.TestCase):
    def setUp(self):
        self.d = Dash()
        self.root = Path(tempfile.mkdtemp(prefix="hands-p-"))
        (self.d.home / "projects").mkdir()
        self.sec = self.d.home / "secrets" / "t"
        (self.root / "state").mkdir(parents=True)
        (self.d.home / "projects" / "t.toml").write_text(
            f'spec_dir = "{self.root}/specs"\napp_dir = "{self.root}/app"\nstate_dir = "{self.root}/state"\n'
            f'image = "hands:test"\nproject_env = "{self.sec}/project.env"\n')
        (self.d.home / "projects" / "noenv.toml").write_text(
            f'spec_dir = "{self.root}/specs"\napp_dir = "{self.root}/app"\nstate_dir = "{self.root}/state"\nimage = "hands:test"\n')
        self.origin = f"http://127.0.0.1:{self.d.port}"

    def tearDown(self):
        self.d.close()

    def post(self, path, body, **kw):
        import http.client
        kw.setdefault("origin", self.origin)
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.d.port}", "Content-Type": "application/json"}
        if kw.get("cookie", True):
            h["Cookie"] = f"{dashboard.COOKIE}={self.d.sid}"
        if kw["origin"]:
            h["Origin"] = kw["origin"]
        if kw.get("xh", True):
            h["X-Hands"] = "1"
        c.request("POST", path, body=json.dumps(body).encode(), headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def test_set_list_replace_delete_never_leaks_secret(self):
        st, _, raw = self.d.req("GET", "/api/p/t/env")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(raw), {"vars": [], "file": f"{self.sec}/project.env", "active": True, "pending": False, "problems": []})
        st, raw = self.post("/api/p/t/env", {"set": [{"key": "STRIPE_KEY", "value": "sk_live_SECRET", "public": False},
                                                    {"key": "SITE_URL", "value": "https://x.test", "public": True}]})
        self.assertEqual(st, 200, raw)
        self.assertNotIn(b"sk_live_SECRET", raw)
        self.assertIn(b"https://x.test", raw)
        st, _, raw = self.d.req("GET", "/api/p/t/env")
        self.assertNotIn(b"sk_live_SECRET", raw)
        rows = {r["key"]: r for r in json.loads(raw)["vars"]}
        self.assertEqual(rows["SITE_URL"], {"key": "SITE_URL", "secret": False, "value": "https://x.test", "updated": rows["SITE_URL"]["updated"]})
        self.assertEqual(set(rows["STRIPE_KEY"]), {"key", "secret", "updated"})
        for path in ("/api/p/t", "/api/projects", "/api/p/t/log", "/api/p/t/attempts"):     # nowhere else either
            self.assertNotIn(b"sk_live_SECRET", self.d.req("GET", path)[2])
        self.assertEqual((self.sec / "project.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.d.home / "secrets").stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(self.root)), ["state"])                       # nothing in the project folder
        # replace, then make the public one secret (it disappears from responses), then delete
        st, raw = self.post("/api/p/t/env", {"set": [{"key": "STRIPE_KEY", "value": "sk_live_SECRET2", "public": False}]})
        self.assertEqual(st, 200)
        self.assertNotIn(b"SECRET", raw)
        self.assertEqual(projectenv.Store(self.sec).values["STRIPE_KEY"], "sk_live_SECRET2")
        st, raw = self.post("/api/p/t/env", {"set": [{"key": "SITE_URL", "public": False}]})
        self.assertEqual(st, 200)
        self.assertNotIn(b"https://x.test", raw)
        st, raw = self.post("/api/p/t/env", {"delete": ["STRIPE_KEY"]})
        self.assertEqual(st, 200)
        self.assertEqual([r["key"] for r in json.loads(raw)["vars"]], ["SITE_URL"])
        self.assertEqual(self.post("/api/p/t/env", {"delete": ["STRIPE_KEY"]})[0], 400)
        self.assertEqual(json.loads((self.sec / "env.json").read_text())["vars"].keys(), {"SITE_URL"})

    def test_validation_errors(self):
        for body in ({"set": [{"key": "bad", "value": "x"}]}, {"set": [{"key": "PATH", "value": "x"}]},
                     {"set": [{"key": "HANDS_X", "value": "x"}]}, {"set": [{"key": "A", "value": "x\ny"}]},
                     {"set": [{"key": "A", "value": "x" * 8193}]}, {"set": [{"key": "A"}]}, {"set": "nope"},
                     {"set": [{"key": "A", "value": 1}]}, {"delete": ["nope"]}, {"delete": "A"}, {}, []):
            st, raw = self.post("/api/p/t/env", body)
            self.assertEqual(st, 400, (body, raw))
        self.assertFalse((self.sec / "project.env").exists())
        self.assertEqual(self.post("/api/p/nope/env", {"set": [{"key": "A", "value": "1"}]})[0], 404)
        self.assertEqual(self.d.req("GET", "/api/p/nope/env")[0], 404)

    def test_auth_and_csrf(self):
        body = {"set": [{"key": "A", "value": "1", "public": True}]}
        self.assertEqual(self.post("/api/p/t/env", body, cookie=False)[0], 401)
        self.assertEqual(self.post("/api/p/t/env", body, origin=None)[0], 403)
        self.assertEqual(self.post("/api/p/t/env", body, origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/p/t/env", body, xh=False)[0], 403)
        self.assertEqual(self.d.req("GET", "/api/p/t/env", cookie=False)[0], 401)
        self.assertFalse((self.sec / "project.env").exists())

    def test_large_value_fits_and_oversized_body_rejected(self):
        st, raw = self.post("/api/p/t/env", {"set": [{"key": "BIG", "value": "x" * 8192, "public": True}]})
        self.assertEqual(st, 200, raw[:200])
        st, raw = self.post("/api/p/t/env", {"set": [{"key": f"K{i}", "value": "x" * 8000, "public": False} for i in range(10)]})
        self.assertEqual(st, 413)
        self.assertEqual(self.post("/api/p/t/action/stop", {"x": "y" * 12000})[0], 413)      # other routes keep 10 kB

    def test_project_without_project_env_setting(self):
        st, _, raw = self.d.req("GET", "/api/p/noenv/env")
        d = json.loads(raw)
        self.assertFalse(d["active"])
        self.assertEqual(d["file"], str(self.d.home / "secrets" / "noenv" / "project.env"))
        st, raw = self.post("/api/p/noenv/env", {"set": [{"key": "A", "value": "1", "public": True}]})
        self.assertEqual(st, 200)
        self.assertTrue((self.d.home / "secrets" / "noenv" / "project.env").is_file())

    def test_pending_while_creating(self):
        newproject.creating_dir().mkdir(parents=True, exist_ok=True)
        lk = Lock(newproject.lock_path("shop")).__enter__()
        try:
            newproject.write_status("shop", "running")
            st, raw = self.post("/api/p/shop/env", {"set": [{"key": "STRIPE_KEY", "value": "sk_SECRET", "public": False},
                                                           {"key": "PUB", "value": "1", "public": True}]})
            self.assertEqual(st, 200, raw)
            self.assertNotIn(b"sk_SECRET", raw)
            self.assertTrue(json.loads(raw)["pending"])
            st, _, raw = self.d.req("GET", "/api/p/shop/env")
            self.assertEqual(st, 200)
            self.assertNotIn(b"sk_SECRET", raw)
            self.assertEqual([r["key"] for r in json.loads(raw)["vars"]], ["STRIPE_KEY", "PUB"])
            pdir = projectenv.pending_dir("shop")
            self.assertEqual(projectenv.Store(pdir).values["STRIPE_KEY"], "sk_SECRET")
            self.assertEqual(pdir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.d.req("GET", "/api/p/shop")[0], 404)          # still not a project
        finally:
            lk.__exit__(None, None, None)
        newproject.write_status("shop", "failed", "boom")
        projectenv.discard_pending("shop")                                        # what run_create does on failure
        self.assertEqual(self.d.req("GET", "/api/p/shop/env")[0], 404)
        self.assertEqual(self.post("/api/p/shop/env", {"set": [{"key": "A", "value": "1"}]})[0], 404)
        self.assertEqual(self.post("/api/p/Bad%20Slug/env", {"set": [{"key": "A", "value": "1"}]})[0], 404)


if __name__ == "__main__":
    unittest.main()
