"""0.5 step 05: project tabs — hide/unhide (closed tabs) stored in ui.json. No docker or Claude needed."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401  (removes every mkdtemp dir after each test)
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import dashboard  # noqa: E402
from test_v04 import Dash  # noqa: E402


class TestHideUnhide(unittest.TestCase):
    def setUp(self):
        self.d = Dash()
        self.root = Path(tempfile.mkdtemp(prefix="hands-p-"))
        (self.d.home / "projects").mkdir()
        for name in ("alpha", "beta"):
            self.project(name)
        self.origin = f"http://127.0.0.1:{self.d.port}"

    def tearDown(self):
        self.d.close()

    def project(self, name):
        r = self.root / name
        (r / "state").mkdir(parents=True)
        (r / "state" / "state.json").write_text(json.dumps({"status": "done", "reason": "all steps passed", "steps": {},
                                                             "updated_at": "2026-10-16T08:10:15Z"}))
        (self.d.home / "projects" / f"{name}.toml").write_text(
            f'spec_dir = "{r}/specs"\napp_dir = "{r}/app"\nstate_dir = "{r}/state"\nimage = "hands:test"\n')

    def projects(self):
        st, _, body = self.d.req("GET", "/api/projects")
        self.assertEqual(st, 200)
        return {p["name"]: p for p in json.loads(body)}

    def test_hide_and_unhide_round_trip(self):
        self.assertEqual({n: p["hidden"] for n, p in self.projects().items()}, {"alpha": False, "beta": False})
        st, _, body = self.d.req("POST", "/api/p/alpha/hide", origin=self.origin)
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body), {"ok": True, "name": "alpha", "hidden": True})
        self.assertEqual({n: p["hidden"] for n, p in self.projects().items()}, {"alpha": True, "beta": False})
        st, _, body = self.d.req("GET", "/api/p/alpha")           # detail still works and carries the flag
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["hidden"])
        st, _, body = self.d.req("POST", "/api/p/alpha/hide", origin=self.origin)     # idempotent
        self.assertEqual(json.loads(body)["hidden"], True)
        st, _, body = self.d.req("POST", "/api/p/alpha/unhide", origin=self.origin)
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body), {"ok": True, "name": "alpha", "hidden": False})
        self.assertEqual({n: p["hidden"] for n, p in self.projects().items()}, {"alpha": False, "beta": False})

    def test_hiding_touches_only_ui_json(self):
        state = self.root / "alpha" / "state" / "state.json"
        toml = self.d.home / "projects" / "alpha.toml"
        before = (state.read_bytes(), toml.read_bytes(), sorted(os.listdir(self.root / "alpha")))
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hide", origin=self.origin)[0], 200)
        self.assertEqual((state.read_bytes(), toml.read_bytes(), sorted(os.listdir(self.root / "alpha"))), before)
        ui = self.d.home / "ui.json"
        self.assertTrue(ui.is_file())
        self.assertEqual(os.stat(ui).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(ui.read_text()), {"hidden": ["alpha"]})
        self.assertEqual([p for p in os.listdir(self.d.home) if p.startswith(".ui.json")], [])    # no temp file left

    def test_unknown_or_invalid_names_rejected(self):
        for name in ("nope", "..", "a%2fb", "al%2e%2e", "all", "x" * 50):
            for act in ("hide", "unhide"):
                st, _, _ = self.d.req("POST", f"/api/p/{name}/{act}", origin=self.origin)
                self.assertEqual(st, 404, (name, act))
        self.assertFalse((self.d.home / "ui.json").exists())
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hidden", origin=self.origin)[0], 404)   # no such verb

    def test_auth_and_csrf_required(self):
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hide", cookie=False, origin=self.origin)[0], 401)
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hide")[0], 403)                          # no Origin
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hide", origin="http://evil.example")[0], 403)
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hide", origin=self.origin, xh=False)[0], 403)
        self.assertEqual(self.d.req("GET", "/api/p/alpha/hide")[0], 404)                           # GET never mutates
        self.assertFalse((self.d.home / "ui.json").exists())
        self.assertFalse(self.projects()["alpha"]["hidden"])

    def test_hidden_survives_restart_and_ignores_junk(self):
        self.assertEqual(self.d.req("POST", "/api/p/alpha/hide", origin=self.origin)[0], 200)
        # a second server over the same config home (= dashboard restart) sees the same state
        srv = dashboard.Server(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_srv, old_port, old_sid = self.d.srv, self.d.port, self.d.sid
        try:
            self.d.srv, self.d.port = srv, srv.server_address[1]
            self.d.sid = srv.new_session()
            self.assertEqual({n: p["hidden"] for n, p in self.projects().items()}, {"alpha": True, "beta": False})
        finally:
            srv.shutdown()
            srv.server_close()
            self.d.srv, self.d.port, self.d.sid = old_srv, old_port, old_sid
        # a hidden entry for a project that no longer exists, or junk in the file, never breaks the list
        (self.d.home / "ui.json").write_text(json.dumps({"hidden": ["alpha", "gone", 7, "../x"], "other": 1}))
        self.assertEqual({n: p["hidden"] for n, p in self.projects().items()}, {"alpha": True, "beta": False})
        (self.d.home / "ui.json").write_text("{not json")
        self.assertEqual({n: p["hidden"] for n, p in self.projects().items()}, {"alpha": False, "beta": False})
        self.assertEqual(self.d.req("POST", "/api/p/beta/hide", origin=self.origin)[0], 200)      # rewrites cleanly
        self.assertEqual(json.loads((self.d.home / "ui.json").read_text()), {"hidden": ["beta"]})


if __name__ == "__main__":
    unittest.main()
