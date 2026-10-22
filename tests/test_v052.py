"""0.5.2 (step 17, owner fixes): dashboard connection handling and logging, in-page dialogs, and the
other owner requirements. No docker, tmux or Claude."""
import http.client
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

from hands import dashboard, sessions, usage  # noqa: E402
from test_v04 import Dash  # noqa: E402

JS = [ROOT / "hands" / "static" / n for n in ("app.js", "sessions.js", "store.js", "logpanel.js")]


class TestKeepAlive(unittest.TestCase):
    """Item 2: a kept-alive connection waits KEEPALIVE_IDLE for its next request (the browser closes first);
    the first request line, headers and bodies keep the short timeout."""

    def setUp(self):
        self.old = dashboard.Handler.timeout, dashboard.KEEPALIVE_IDLE
        dashboard.Handler.timeout, dashboard.KEEPALIVE_IDLE = 0.6, 2.0
        self.d = Dash()

    def tearDown(self):
        self.d.close()
        dashboard.Handler.timeout, dashboard.KEEPALIVE_IDLE = self.old

    def raw(self, sock, method, path):
        sock.sendall(f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                     f"Cookie: {dashboard.COOKIE}={self.d.sid}\r\nX-Hands: 1\r\nOrigin: http://127.0.0.1:{self.d.port}\r\n"
                     f"Content-Length: 0\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                return None, data
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        n = int(re.search(rb"Content-Length: (\d+)", head).group(1))
        while len(rest) < n:
            rest += sock.recv(65536)
        return head.decode(), rest

    def test_second_request_after_old_timeout_still_works_then_idle_close(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        try:
            head, body = self.raw(s, "GET", "/api/projects")
            self.assertIn(" 200 ", head.splitlines()[0])
            self.assertRegex(head, r"Keep-Alive: timeout=\d+")
            time.sleep(1.2)                                       # longer than the per-read timeout (0.6)
            head, body = self.raw(s, "GET", "/api/projects")
            self.assertIsNotNone(head, "server closed the kept-alive connection too early")
            self.assertIn(" 200 ", head.splitlines()[0])
            t0 = time.monotonic()
            self.assertEqual(s.recv(10), b"")                     # idle: closed by the server after KEEPALIVE_IDLE
            self.assertGreater(time.monotonic() - t0, 1.5)
        finally:
            s.close()

    def test_first_request_line_keeps_the_short_timeout(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        try:
            t0 = time.monotonic()
            self.assertEqual(s.recv(10), b"")
            self.assertLess(time.monotonic() - t0, 1.8)
        finally:
            s.close()

    def test_headers_after_request_line_keep_the_short_timeout(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        try:
            self.raw(s, "GET", "/api/projects")                   # now a keep-alive connection (long idle wait)
            s.sendall(b"GET /api/projects HTTP/1.1\r\nHost: 127.0.0")   # request line in, headers stall
            t0 = time.monotonic()
            self.assertEqual(s.recv(10), b"")
            self.assertLess(time.monotonic() - t0, 1.8)
        finally:
            s.close()


class TestRequestLog(unittest.TestCase):
    def setUp(self):
        self.d = Dash()

    def tearDown(self):
        self.d.close()

    def test_requests_logged_without_query_and_errors_marked(self):
        self.d.req("GET", "/api/projects")
        self.d.req("POST", "/api/p/nope/unhide", origin=f"http://127.0.0.1:{self.d.port}")
        self.d.req("GET", "/login?c=notthecode", cookie=False)
        log = dashboard.log_path()
        self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
        text = log.read_text()
        self.assertRegex(text, r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ 127\.0\.0\.1 GET /api/projects 200 \d+ms")
        self.assertRegex(text, r"POST /api/p/nope/unhide 404 \d+ms")
        self.assertIn("GET /login 403", text)                      # the login code itself never reaches the log
        self.assertNotIn("notthecode", text)
        self.assertNotIn("c=", text)
        self.assertNotIn(self.d.sid, text)

    def test_connection_errors_logged(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=5)
        s.sendall(b"POST /api/p/x/unhide HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 10\r\n\r\n")
        s.close()                                                 # body never arrives: the server sees EOF/reset
        for _ in range(50):
            text = dashboard.log_path().read_text() if dashboard.log_path().exists() else ""
            if "ERR" in text or "/api/p/x/unhide" in text:
                break
            time.sleep(0.1)
        self.assertIn("/api/p/x/unhide", text)

    def test_log_rotates(self):
        old = dashboard.LOG_MAX
        dashboard.LOG_MAX = 200
        try:
            for i in range(20):
                dashboard.log_line(f"filler {i} " + "x" * 40)
            self.assertTrue(dashboard.log_path().with_suffix(".log.1").exists())
            self.assertLess(dashboard.log_path().stat().st_size, 400)
        finally:
            dashboard.LOG_MAX = old

    def test_log_never_follows_a_symlink(self):
        target = self.d.home / "elsewhere"
        dashboard.log_path().unlink(missing_ok=True)
        os.symlink(target, dashboard.log_path())
        dashboard.log_line("hello")
        self.assertFalse(target.exists())


class TestNoBrowserDialogs(unittest.TestCase):
    """Item 2 / requirement 5: every error shows inside the page; no alert/confirm/prompt anywhere."""

    def test_no_window_dialogs_in_page_code(self):
        for p in JS:
            js = p.read_text()
            for pat in (r"(?<![\w.])alert\(", r"(?<![\w.])confirm\(", r"(?<![\w.])prompt\(", r"window\.(alert|confirm|prompt)"):
                self.assertIsNone(re.search(pat, js), f"{p.name}: {pat}")
        app = JS[0].read_text()
        self.assertIn("function toast(", app)
        self.assertIn("function openDialog(", app)
        self.assertNotIn("showModal();", app.replace("dlg.showModal(); return;", ""))   # only openDialog() calls it

    def test_unhide_retries_once_on_a_network_error(self):
        app = JS[0].read_text()
        m = re.search(r"async function reopen\(name\) \{(.*?)\n\}", app, re.S)
        self.assertIsNotNone(m)
        self.assertIn("if (!e.network) throw e; await unhide();", m.group(1))
        self.assertIn("err.network = true", app)


class TestPlusButton(unittest.TestCase):
    """Item 3 / requirement 4: the "+" node is created once and reused; renders that change nothing skip the
    rebuild. The click itself is exercised by the rig (tools/shots.js plusDuringRefresh)."""

    def test_add_button_is_persistent(self):
        app = JS[0].read_text()
        self.assertEqual(app.count('class: "tab-add"'), 1)
        self.assertRegex(app, r"\nconst addBtn = el\(\"button\", \{ class: \"tab-add\"")
        body = re.search(r"function renderTabs\(open, hidden\) \{(.*?)\n\}", app, re.S).group(1)
        self.assertNotIn("replaceChildren", body)                 # nothing in the strip is removed and re-added
        self.assertIn("if (!strip.contains(addBtn)) strip.append(addBtn);", body)
        self.assertIn("if (key === lastTabsKey)", body)
        self.assertIn("strip.insertBefore(t, cursor)", body)      # tabs reconciled in place by name
        self.assertNotIn('el("button", { class: "tab-add"', body)


class TestUsageExpired(unittest.TestCase):
    """Item 4 / requirement 6: a window whose reset time has passed is marked expired/stale by the server
    (same clock as the data) and the page shows it as stale with a refresh, never as current."""

    def setUp(self):
        self.d = Dash()

    def tearDown(self):
        self.d.close()

    def test_view_marks_expired_windows(self):
        now = 1_800_000_000
        usage.record({"status": "allowed", "at": now - 7200, "five_hour": {"used_pct": 42.0, "resets_at": now - 60},
                      "seven_day": {"used_pct": 10.0, "resets_at": now + 86400}})
        v = usage.view(False, now=now)
        self.assertTrue(v["five_hour"]["expired"])
        self.assertFalse(v["seven_day"]["expired"])
        self.assertTrue(v["stale"])
        self.assertEqual(v["now"], now)
        self.assertEqual(v["five_hour"]["used_pct"], 42.0)       # the number stays, marked as from a window that is over
        v = usage.view(True, now=now - 3600)
        self.assertFalse(v["five_hour"]["expired"])
        self.assertFalse(v["stale"])
        self.assertTrue(v["refreshing"])
        self.assertTrue(usage.view(False, now=now)["stale"] if not usage.read() else True)

    def test_route_serves_the_flags(self):
        usage.record({"status": "allowed", "at": 1, "five_hour": {"used_pct": 1.0, "resets_at": 2}})
        st, _, body = self.d.req("GET", "/api/usage")
        v = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(v["five_hour"]["expired"])
        self.assertTrue(v["stale"])
        self.assertIn("now", v)
        usage.path().unlink()
        v = json.loads(self.d.req("GET", "/api/usage")[2])
        self.assertTrue(v["stale"])
        self.assertEqual(v["refreshing"], False)

    def test_page_shows_expired_as_stale_and_auto_refreshes_once(self):
        app = JS[0].read_text()
        body = re.search(r"function meter\(label, w, withDay\) \{(.*?)\n\}", app, re.S).group(1)
        self.assertIn("if (w.expired)", body)
        self.assertIn('class: "meter expired"', body)
        self.assertIn("stale, refresh for the current window", body)
        auto = re.search(r"function autoRefreshUsage\(u\) \{(.*?)\n\}", app, re.S).group(1)
        self.assertIn("hands.usage.autorefresh", auto)             # once per expired window, remembered across reloads
        self.assertIn("if (done === key) return;", auto)
        self.assertIn("if (!key || u.refreshing) return;", auto)


class TestSessionFolder(unittest.TestCase):
    """Item 5 / requirement 9: new sessions start in the home folder, never ~/ops; the home itself is valid."""

    def test_default_is_home_not_ops(self):
        self.assertEqual(sessions.default_cwd(), sessions.HOME)
        self.assertNotEqual(sessions.default_cwd().name, "ops")
        js = JS[1].read_text()
        self.assertNotIn("~/ops", js)
        self.assertIn("Your home folder (the default)", js)


if __name__ == "__main__":
    unittest.main()
