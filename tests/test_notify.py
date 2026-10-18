"""Telegram notifier: retries with backoff, reasons in the log, the bot token never does, never raises."""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
from hands import notify  # noqa: E402

TOKEN = "123456789:AAHfakeTOKENvalueXYZ_abc-def0123456789"
CHAT = "987654321"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code: int, headers=None, body=b"{}"):
    import email.message
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN + "/sendMessage", code, "status text", h, io.BytesIO(body))


class TestNotifier(unittest.TestCase):
    def setUp(self):
        # other test modules set HANDS_NO_NOTIFY=1 at import; this one needs real (fake-urlopen) sends
        self._saved = os.environ.pop("HANDS_NO_NOTIFY", None)
        self.tmp = Path(tempfile.mkdtemp())
        self.env = self.tmp / "telegram.env"
        self.env.write_text(f"TG_TOKEN={TOKEN}\nTG_CHAT_ID={CHAT}\n")
        self.env.chmod(0o600)
        self.logs: list[str] = []
        self.sleeps: list[float] = []
        self.calls: list[tuple] = []

    def tearDown(self):
        if self._saved is not None:
            os.environ["HANDS_NO_NOTIFY"] = self._saved

    def notifier(self, *outcomes):
        """outcomes: exceptions are raised, bytes are returned as the response body, in order."""
        queue = list(outcomes)

        def urlopen(url, data, timeout):
            self.calls.append((url, data, timeout))
            o = queue.pop(0)
            if isinstance(o, BaseException):
                raise o
            return FakeResponse(o)
        return notify.Notifier(self.env, "smoke", log=self.logs.append, sleep=self.sleeps.append, urlopen=urlopen)

    def assert_no_token(self):
        for ln in self.logs:
            self.assertNotIn(TOKEN, ln, ln)
            self.assertNotIn(TOKEN.split(":")[1], ln, ln)

    def test_transient_failure_then_success(self):
        n = self.notifier(urllib.error.URLError(OSError(111, "Connection refused")), b'{"ok": true}')
        self.assertTrue(n.send("hello"))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual(self.logs, [], "a recovered send logs nothing")
        self.assertEqual(self.calls[0][2], 15)
        self.assertIn(b"chat_id=" + CHAT.encode(), self.calls[0][1])

    def test_persistent_failure_three_attempts_one_warn_with_reasons(self):
        n = self.notifier(urllib.error.URLError("timed out"), TimeoutError("read timeout"),
                          urllib.error.URLError(OSError(-3, "Temporary failure in name resolution")))
        self.assertFalse(n.send("hello"))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.sleeps, [2.0, 5.0], "backoff before attempts 2 and 3, none after the last")
        self.assertEqual(len(self.logs), 1, self.logs)
        w = self.logs[0]
        self.assertTrue(w.startswith("WARN: telegram send failed after 3 attempt(s): "), w)
        for reason in ("URLError timed out", "TimeoutError: read timeout", "Temporary failure in name resolution"):
            self.assertIn(reason, w)
        self.assert_no_token()

    def test_400_is_not_retried(self):
        n = self.notifier(http_error(400, body=b'{"ok":false,"description":"Bad Request: chat not found"}'))
        self.assertFalse(n.send("hello"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.logs), 1)
        self.assertIn("HTTPError 400", self.logs[0])
        self.assertIn("after 1 attempt(s)", self.logs[0])
        self.assert_no_token()
        for code in (401, 403, 404):
            self.calls.clear(); self.logs.clear()
            self.assertFalse(self.notifier(http_error(code)).send("x"))
            self.assertEqual(len(self.calls), 1, code)

    def test_429_is_retried_and_respects_retry_after(self):
        n = self.notifier(http_error(429, headers={"Retry-After": "7"}), b'{"ok": true}')
        self.assertTrue(n.send("hello"))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [7.0])
        # retry_after in the body, and the cap so the runner never stalls for long
        self.calls.clear(); self.sleeps.clear()
        n = self.notifier(http_error(429, body=b'{"ok":false,"parameters":{"retry_after":3}}'),
                          http_error(429, headers={"Retry-After": "600"}), b'{"ok": true}')
        self.assertTrue(n.send("hello"))
        self.assertEqual(self.sleeps, [3.0, 30.0])
        self.assertEqual(self.logs, [])

    def test_5xx_and_ok_false_are_retried(self):
        n = self.notifier(http_error(502), b'{"ok": false}', b'{"ok": true}')
        self.assertTrue(n.send("hello"))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.sleeps, [2.0, 5.0])

    def test_token_never_appears_even_when_the_error_text_contains_it(self):
        err = urllib.error.URLError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")
        n = self.notifier(err, http_error(500), ValueError(f"bad json from bot{TOKEN}"))
        self.assertFalse(n.send("hello"))
        self.assertEqual(len(self.logs), 1)
        self.assertIn("[REDACTED:TG_TOKEN]", self.logs[0])
        self.assert_no_token()

    def test_never_raises_even_if_everything_breaks(self):
        def boom(*a, **k):
            raise RuntimeError("urlopen exploded")
        n = notify.Notifier(self.env, "smoke", log=self.logs.append, sleep=lambda s: None, urlopen=boom)
        self.assertFalse(n.send("hello"))
        self.assertIn("RuntimeError: urlopen exploded", self.logs[0])
        # a broken logger must not raise either
        def badlog(_):
            raise OSError("disk full")
        n = notify.Notifier(self.env, "smoke", log=badlog, sleep=lambda s: None, urlopen=boom)
        self.assertFalse(n.send("hello"))

    def test_disabled_sends_nothing(self):
        n = notify.Notifier(None, "smoke", log=self.logs.append, urlopen=lambda *a, **k: self.fail("called"))
        self.assertFalse(n.enabled)
        self.assertFalse(n.send("x"))
        os.environ["HANDS_NO_NOTIFY"] = "1"
        try:
            self.assertFalse(self.notifier().send("x"))
        finally:
            del os.environ["HANDS_NO_NOTIFY"]
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
