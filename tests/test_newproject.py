"""0.4.1: hands <slug> create / destroy against a fake GitHub API and local bare repos."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import config as cfgmod, newproject  # noqa: E402


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class FakeGH:
    def __init__(self, remote_root: Path):
        self.root = remote_root
        self.calls = []
        self.fail = {}          # (method, path-prefix) -> code
        self.ids = {}
        self.plan = "team"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj=None):
                body = json.dumps(obj or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.calls.append((self.command, self.path, body, self.headers.get("Authorization")))
                for (m, pre), code in fake.fail.items():
                    if m == self.command and self.path.startswith(pre):
                        return self._reply(code, {"message": "nope"})
                p = self.path.split("/")
                if self.command == "GET" and self.path == "/meta":
                    return self._reply(200, {"ssh_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"]})
                if self.command == "GET" and p[1] == "orgs":
                    return self._reply(200, {"login": p[2], "plan": {"name": fake.plan}})
                if self.command == "GET" and p[1] == "repos":
                    if (fake.root / f"{p[3]}.git").exists():
                        return self._reply(200, {"id": fake.ids.get(p[3], 999)})
                    return self._reply(404)
                if self.command == "POST" and p[1] == "orgs":
                    name = body["name"]
                    bare = fake.root / f"{name}.git"
                    git("init", "-q", "--bare", "-b", "main", str(bare))
                    w = Path(tempfile.mkdtemp())
                    git("clone", "-q", str(bare), str(w / "c"))
                    (w / "c" / "README.md").write_text(f"# {name}\n")
                    git("add", "-A", cwd=w / "c")
                    git("-c", "user.name=gh", "-c", "user.email=gh@x", "commit", "-qm", "init", cwd=w / "c")
                    git("push", "-q", "origin", "main", cwd=w / "c")
                    fake.ids[name] = len(fake.ids) + 100
                    return self._reply(201, {"default_branch": "main", "id": fake.ids[name]})
                if self.command == "PUT" and "contents" in p:
                    repo, path = p[3], "/".join(p[5:])
                    w = Path(tempfile.mkdtemp())
                    git("clone", "-q", str(fake.root / f"{repo}.git"), str(w / "c"))
                    f = w / "c" / path
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_bytes(base64.b64decode(body["content"]))
                    git("add", "-A", cwd=w / "c")
                    git("-c", "user.name=gh", "-c", "user.email=gh@x", "commit", "-qm", body["message"], cwd=w / "c")
                    git("push", "-q", "origin", "main", cwd=w / "c")
                    return self._reply(201)
                if self.command == "POST":
                    return self._reply(201, {"id": 1})
                if self.command == "DELETE" and p[1] == "repos":
                    bare = fake.root / f"{p[3]}.git"
                    if not bare.exists():
                        return self._reply(404)
                    subprocess.run(["rm", "-rf", str(bare)], check=True)
                    return self._reply(204)
                return self._reply(404)

            do_GET = do_POST = do_PUT = do_DELETE = _do

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-new-"))
        self.home = self.tmp / "home"
        self.cfg_home = self.home / ".config" / "hands"
        (self.cfg_home / "projects").mkdir(parents=True)
        self.remote = self.tmp / "remote"
        self.remote.mkdir()
        self.gh = FakeGH(self.remote)
        keygen = self.tmp / "keygen"
        keygen.write_text('#!/bin/sh\nwhile [ "$1" != "-f" ]; do shift; done\n'
                          'echo PRIVATE > "$2"; echo "ssh-ed25519 AAAAfake test" > "$2.pub"\n')
        keygen.chmod(0o755)
        (self.cfg_home / "github.env").write_text("GH_TOKEN=ghp_test\n")
        (self.cfg_home / "github.env").chmod(0o600)
        (self.cfg_home / "new-project.toml").write_text(
            'org = "acme"\ncodeowner = "boss"\nimage = "hands:cc1-pw1"\ncoder_model = "claude-fable-5-1"\n')
        self.old = cfgmod.CONFIG_HOME
        cfgmod.CONFIG_HOME = self.cfg_home
        self.env_backup = {k: os.environ.get(k) for k in ("HANDS_HOME", "HANDS_SSH_KEYGEN", "HANDS_TEST_REMOTE_ROOT",
                                                         "HANDS_GH_SETTLE", "HANDS_GH_API", "HANDS_ALLOW_TEST_API")}
        os.environ.update(HANDS_HOME=str(self.home), HANDS_SSH_KEYGEN=str(keygen), HANDS_GH_API=self.gh.url,
                          HANDS_ALLOW_TEST_API="1", HANDS_TEST_REMOTE_ROOT=str(self.remote), HANDS_GH_SETTLE="0")

    def tearDown(self):
        self.gh.close()
        cfgmod.CONFIG_HOME = self.old
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestCreate(Base):
    def test_create_full(self):
        self.assertEqual(newproject.run_create("bookings", "Booking app for salons"), 0)
        root = self.home / "projects" / "bookings"
        cfg = cfgmod.load("bookings")
        self.assertTrue(cfg.managed)
        self.assertEqual((cfg.repo_specs, cfg.repo_app, cfg.github_org), ("bookings-specs", "bookings-app", "acme"))
        self.assertEqual(cfg.spec_dir, (root / "specs").resolve())
        self.assertEqual(cfg.image, "hands:cc1-pw1")
        self.assertTrue((root / "specs" / ".github" / "CODEOWNERS").read_text().startswith("* @boss"))
        self.assertIn("/acceptance/", (root / "app" / ".gitignore").read_text())
        self.assertEqual((root / "project.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((root / "description.txt").read_text().strip(), "Booking app for salons")
        self.assertFalse((self.home / ".ssh").exists(), "must not touch ~/.ssh")
        self.assertEqual((root / "keys" / "deploy_bookings-app").stat().st_mode & 0o777, 0o600)
        self.assertEqual((root / "keys").stat().st_mode & 0o777, 0o700)
        sshc = git("config", "core.sshCommand", cwd=root / "app").strip()
        self.assertIn("deploy_bookings-app", sshc)
        self.assertIn("-F /dev/null", sshc)
        self.assertIn("StrictHostKeyChecking=yes", sshc)
        self.assertIn("deploy_bookings-specs", git("config", "core.sshCommand", cwd=root / "specs"))
        self.assertTrue((root / "known_hosts").read_text().startswith("github.com ssh-ed25519 "))
        rec = json.loads((root / "created.json").read_text())
        self.assertEqual(set(rec["repos"]), {"bookings-specs", "bookings-app"})
        rules = {c[1]: c[2] for c in self.gh.calls if c[1].endswith("/rulesets")}
        spec_rules = [r["type"] for r in rules["/repos/acme/bookings-specs/rulesets"]["rules"]]
        app_rules = [r["type"] for r in rules["/repos/acme/bookings-app/rulesets"]["rules"]]
        self.assertIn("pull_request", spec_rules)
        self.assertNotIn("pull_request", app_rules)
        keys = {c[1]: c[2]["read_only"] for c in self.gh.calls if c[1].endswith("/keys")}
        self.assertEqual(keys, {"/repos/acme/bookings-specs/keys": True, "/repos/acme/bookings-app/keys": False})
        self.assertTrue(all(c[3] == "Bearer ghp_test" for c in self.gh.calls))
        self.assertEqual(json.loads(newproject.status_path("bookings").read_text())["state"], "done")

    def test_refuses_existing_repo_and_bad_slug(self):
        git("init", "-q", "--bare", str(self.remote / "taken-app.git"))
        self.assertEqual(newproject.run_create("taken", "x"), 1)
        self.assertFalse((self.cfg_home / "projects" / "taken.toml").exists())
        self.assertEqual(json.loads(newproject.status_path("taken").read_text())["state"], "failed")
        with self.assertRaises(newproject.CreateError):
            newproject.destroy("taken", "taken")          # no config: must never touch GitHub
        self.assertFalse(any(c[0] == "DELETE" for c in self.gh.calls))
        self.assertTrue((self.remote / "taken-app.git").exists())
        for bad in ("A", "ab", "-ab", "ab-", "a--b", "all", "x" * 40, "a_b", "../x"):
            with self.assertRaises(newproject.CreateError, msg=bad):
                newproject.validate_slug(bad)

    def test_token_file_must_be_private(self):
        (self.cfg_home / "github.env").chmod(0o644)
        self.assertEqual(newproject.run_create("demo", "x"), 1)
        self.assertEqual(self.gh.calls, [])

    def test_failure_midway_then_destroy_cleans_up(self):
        self.gh.fail[("POST", "/repos/acme/half-app/rulesets")] = 422
        self.assertEqual(newproject.run_create("half", "x"), 1)
        self.assertTrue((self.cfg_home / "projects" / "half.toml").exists())
        self.assertTrue((self.remote / "half-specs.git").exists())
        with self.assertRaises(newproject.CreateError):
            newproject.destroy("half", "wrong")
        self.assertEqual(newproject.destroy("half", "half"), 0)
        self.assertFalse((self.remote / "half-specs.git").exists())
        self.assertFalse((self.remote / "half-app.git").exists())
        self.assertFalse((self.cfg_home / "projects" / "half.toml").exists())
        self.assertFalse((self.home / "projects" / "half").exists())
        self.assertFalse(newproject.status_path("half").exists())

    def test_destroy_refuses_unmanaged(self):
        (self.cfg_home / "projects" / "old.toml").write_text(
            'spec_dir = "/x"\napp_dir = "/y"\nstate_dir = "/z"\nimage = "hands:a1"\n')
        with self.assertRaises(newproject.CreateError):
            newproject.destroy("old", "old")
        self.assertFalse(any(c[0] == "DELETE" for c in self.gh.calls))

    def test_destroy_only_deletes_recorded_repo_ids(self):
        newproject.run_create("one", "x")
        self.gh.ids["one-app"] = 5555                   # repo replaced by someone else since creation
        newproject.destroy("one", "one")
        self.assertTrue((self.remote / "one-app.git").exists())
        self.assertFalse((self.remote / "one-specs.git").exists())

    def test_rerun_create_on_existing_project_is_harmless(self):
        self.assertEqual(newproject.run_create("shop", "x"), 0)
        self.assertEqual(newproject.run_create("shop", "again"), 1)
        self.assertEqual(json.loads(newproject.status_path("shop").read_text())["state"], "done")
        self.assertTrue((self.remote / "shop-app.git").exists())

    def test_free_plan_refused_before_anything(self):
        self.gh.plan = "free"
        self.assertEqual(newproject.run_create("freeby", "x"), 1)
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in self.gh.calls))
        self.assertFalse((self.cfg_home / "projects" / "freeby.toml").exists())

    def test_reserved_and_settings_validation(self):
        for bad in ("dash", "dashboard", "all"):
            with self.assertRaises(newproject.CreateError):
                newproject.validate_slug(bad)
        (self.cfg_home / "new-project.toml").write_text('org = "acme/../x"\ncodeowner = "b"\nimage = "h:1"\n')
        with self.assertRaises(newproject.CreateError):
            newproject.settings()

    def test_api_override_ignored_without_test_flag(self):
        os.environ.pop("HANDS_ALLOW_TEST_API")
        try:
            self.assertEqual(newproject.api_base(), "https://api.github.com")
        finally:
            os.environ["HANDS_ALLOW_TEST_API"] = "1"


class TestDashboardCreate(Base):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "tests"))
        from test_v04 import Dash
        os.environ["HANDS_GH_API_FOR_CHILD"] = self.gh.url
        self.d = Dash()                      # re-points CONFIG_HOME at its own temp dir
        cfgmod.CONFIG_HOME = self.cfg_home
        os.environ["HANDS_CONFIG_HOME"] = str(self.cfg_home)

    def tearDown(self):
        self.d.close()
        super().tearDown()

    def post(self, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        data = json.dumps(body).encode()
        c.request("POST", "/api/new", body=data, headers={
            "Host": f"127.0.0.1:{self.d.port}", "Origin": f"http://127.0.0.1:{self.d.port}", "X-Hands": "1",
            "Cookie": f"hands_dash={self.d.sid}", "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_new_project_via_dashboard(self):
        import time
        try:
            st, body = self.post({"slug": "shop", "description": "Online shop"})
            self.assertEqual(st, 200, body)
            for _ in range(120):
                s = json.loads(newproject.status_path("shop").read_text())["state"]
                if s != "running":
                    break
                time.sleep(0.5)
            log = (newproject.creating_dir() / "shop.log").read_text()
            self.assertEqual(s, "done", log)
            st, _, raw = self.d.req("GET", "/api/creating/shop")
            self.assertTrue(json.loads(raw)["project_exists"])
            st, _, raw = self.d.req("GET", "/api/p/shop")
            d = json.loads(raw)
            self.assertTrue(d["managed"])
            self.assertFalse(d["setup"]["spec_ready"])
            self.assertEqual(d["setup"]["description"], "Online shop")
            self.assertEqual(self.post({"slug": "shop", "description": "again"})[0], 400)
            self.assertEqual(self.post({"slug": "Bad Slug", "description": "x"})[0], 400)
            self.assertEqual(self.post({"slug": "fine", "description": ""})[0], 400)
        finally:
            pass


if __name__ == "__main__":
    unittest.main()


class TestStaleStatus(Base):
    def test_dead_creator_reported_failed(self):
        from hands import dashboard
        newproject.write_status("ghost", "running")
        st = json.loads(newproject.status_path("ghost").read_text())
        st["at"] -= 60
        newproject.status_path("ghost").write_text(json.dumps(st))
        self.assertEqual(dashboard.create_info("ghost")["state"], "failed")
