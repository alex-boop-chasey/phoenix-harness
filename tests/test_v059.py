"""0.6.0: dashboard render rethink. Task 1: version stamps and conditional GET on the project detail and log."""
import json
import os
import re
import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"

from hands import dashboard  # noqa: E402
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


class TestEvents(DashProject):
    """Task 2: GET /api/p/<name>/events streams Server-Sent Events: a `change` event whenever the project's
    version moves, a comment ping during silence, 503 past SSE_MAX concurrent streams."""

    def setUp(self):
        super().setUp()
        self.old = dashboard.SSE_POLL, dashboard.SSE_PING
        dashboard.SSE_POLL, dashboard.SSE_PING = 0.1, 0.3
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            s.close()
        dashboard.SSE_POLL, dashboard.SSE_PING = self.old
        super().tearDown()

    def stream(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        self.socks.append(s)
        s.sendall(f"GET /api/p/t/events HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                  f"Cookie: {dashboard.COOKIE}={self.d.sid}\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        return s, head.decode(), rest

    @staticmethod
    def read_until(s, marker, limit=8.0):
        data, t0 = b"", time.monotonic()
        while marker not in data and time.monotonic() - t0 < limit:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data

    def test_events_stream_sends_change_on_write(self):
        s, head, rest = self.stream()
        self.assertIn(" 200 ", head.splitlines()[0])
        self.assertIn("Content-Type: text/event-stream", head)
        self.assertIn("Cache-Control: no-store", head)
        self.assertTrue(rest.startswith(b"retry: 5000\n\n") or b"retry: 5000" in self.read_until(s, b"retry: 5000"))
        with open(self.state_dir / "runner.log", "a") as f:
            f.write("tick\n")
        data = self.read_until(s, b"event: change\n")
        m = re.search(rb'event: change\ndata: (\{.*\})\n\n', data)
        self.assertIsNotNone(m, data)
        self.assertEqual(json.loads(m.group(1))["version"], self.get("/api/p/t")[2]["version"])

    def test_events_ping_keeps_socket_alive(self):
        s, head, rest = self.stream()
        self.assertIn(b": ping\n\n", rest + self.read_until(s, b": ping\n\n"))

    def test_events_ninth_stream_refused(self):
        for _ in range(dashboard.SSE_MAX):
            s, head, _ = self.stream()
            self.assertIn(" 200 ", head.splitlines()[0])
        s, head, body = self.stream()
        self.assertIn(" 503 ", head.splitlines()[0])
        self.assertIn(b"too many streams", body + self.read_until(s, b"}"))

    def test_events_slot_released_when_client_leaves(self):
        held = [self.stream() for _ in range(dashboard.SSE_MAX)]
        held[0][0].close()
        time.sleep(0.5)                                   # the handler notices on its next write
        s, head, _ = self.stream()
        self.assertIn(" 200 ", head.splitlines()[0])

    def test_events_needs_login(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        self.socks.append(s)
        s.sendall(f"GET /api/p/t/events HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n\r\n".encode())
        self.assertIn(b" 401 ", self.read_until(s, b"\r\n\r\n"))
