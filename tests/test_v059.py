"""0.6.0: dashboard render rethink. Task 1: version stamps and conditional GET on the project detail and log."""
import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"

from test_v04 import Dash  # noqa: E402
from test_runner import Env, STEP1, STEP2, GOOD1, GOOD2, EVAL_PASS  # noqa: E402


class DashProject(unittest.TestCase):
    """A Dash with one built project `t` (two passed steps), like TestDashboard in test_v04."""

    def setUp(self):
        self.e = Env([STEP1, STEP2])
        self.e.queue({"role": "coder", "files": {"src/z.js": "x\n"}}, GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
        self.assertEqual(self.e.runner().run(), 0)
        self.d = Dash()
        (self.d.home / "projects").mkdir()
        c = self.e.cfg
        self.state_dir = c.state_dir
        (self.d.home / "projects" / "t.toml").write_text(
            f'spec_dir = "{c.spec_dir}"\napp_dir = "{c.app_dir}"\nstate_dir = "{c.state_dir}"\nimage = "hands:test"\n'
            f'claude_env = "{c.claude_env}"\n')

    def tearDown(self):
        self.d.close()

    def get(self, path, **kw):
        st, hdrs, body = self.d.req("GET", path, **kw)
        return st, hdrs, (json.loads(body) if body else b"")


class TestVersionStamp(DashProject):
    def test_detail_has_version_and_etag(self):
        code, hdrs, body = self.get("/api/p/t")
        self.assertEqual(code, 200)
        self.assertTrue(body["version"])
        self.assertEqual(hdrs["ETag"], f'"{body["version"]}"')

    def test_log_has_version_and_etag(self):
        code, hdrs, body = self.get("/api/p/t/log?lines=50")
        self.assertEqual(code, 200)
        self.assertEqual(hdrs["ETag"], f'"{body["version"]}"')

    def test_if_none_match_gives_304(self):
        _, hdrs, _ = self.get("/api/p/t")
        code, hdrs2, body = self.get("/api/p/t", headers={"If-None-Match": hdrs["ETag"]})
        self.assertEqual(code, 304)
        self.assertEqual(body, b"")
        self.assertEqual(hdrs2["ETag"], hdrs["ETag"])

    def test_stale_if_none_match_gives_200(self):
        code, _, body = self.get("/api/p/t", headers={"If-None-Match": '"nope"'})
        self.assertEqual(code, 200)
        self.assertEqual(body["name"], "t")

    def test_version_changes_when_state_written(self):
        v1 = self.get("/api/p/t")[2]["version"]
        p = self.state_dir / "state.json"
        p.write_text(p.read_text())
        later = time.time_ns() + 10**12      # 1000 s in the future: strictly newer than anything else
        os.utime(p, ns=(later, later))
        self.assertNotEqual(v1, self.get("/api/p/t")[2]["version"])

    def test_version_changes_when_log_grows(self):
        v1 = self.get("/api/p/t/log")[2]["version"]
        with open(self.state_dir / "runner.log", "a") as f:
            f.write("one more line\n")
        self.assertNotEqual(v1, self.get("/api/p/t/log")[2]["version"])


if __name__ == "__main__":
    unittest.main()
