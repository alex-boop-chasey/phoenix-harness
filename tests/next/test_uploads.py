"""hands.uploads: precheck decisions and the dashboard upload route (no body byte read when refused)."""
import http.client
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tests"))
from hands import dashboard, uploads  # noqa: E402

MB = 1048576
Q = "name=hero.png&kind=screenshot&note=hi&batch=b1"


def req(**over):
    r = {"method": "POST", "path": f"/api/p/demo/design-files/upload?{Q}",
         "headers": {"content-length": str(2 * MB), "X-Hands": "1"}, "logged_in": True, "same_origin": True,
         "batch": {"total": 0, "count": 0}}
    r.update(over)
    return r


def hdr(**h):
    return {"X-Hands": "1", **h}


class Precheck(unittest.TestCase):
    def test_table(self):
        cases = [
            ("ok", req(), 0),
            ("anonymous", req(logged_in=False), 401),
            ("origin", req(same_origin=False), 403),
            ("no x-hands", req(headers={"Content-Length": "10"}), 403),
            ("chunked", req(headers=hdr(**{"Transfer-Encoding": "chunked"})), 411),
            ("no length", req(headers=hdr()), 411),
            ("bad length", req(headers=hdr(**{"Content-Length": "abc"})), 400),
            ("image too big", req(headers=hdr(**{"Content-Length": str(15 * MB + 1)})), 413),
            ("video ok", req(path="/api/p/demo/design-files/upload?name=a.mp4&kind=video&batch=b",
                             headers=hdr(**{"Content-Length": str(50 * MB)})), 0),
            ("batch total", req(headers=hdr(**{"Content-Length": str(10 * MB)}), batch={"total": 195 * MB, "count": 1}), 413),
            ("batch count", req(batch={"total": 0, "count": 50}), 413),
            ("type", req(path="/api/p/demo/design-files/upload?name=a.exe&kind=brand&batch=b"), 400),
            ("kind", req(path="/api/p/demo/design-files/upload?name=a.png&kind=zz&batch=b"), 400),
            ("note", req(path=f"/api/p/demo/design-files/upload?name=a.png&kind=brand&batch=b&note={'n' * 501}"), 400),
            ("batch id", req(path="/api/p/demo/design-files/upload?name=a.png&kind=brand&batch=b%20c"), 400),
            ("no name", req(path="/api/p/demo/design-files/upload?kind=brand&batch=b"), 400),
        ]
        for label, r, code in cases:
            with self.subTest(label):
                out = uploads.precheck(r)
                self.assertEqual(out["code"], code, out)
                self.assertEqual(out["close"], code != 0)

    def test_messages_and_caps(self):
        r = uploads.precheck(req(headers=hdr(**{"Content-Length": str(15 * MB + 1)})))
        self.assertEqual(r["error"], "too large (max 15 MB)")
        self.assertEqual(uploads.precheck(req(batch={"total": 0, "count": 50}))["error"], "batch over 50 files")
        r = uploads.precheck(req(headers=hdr(**{"Content-Length": str(10 * MB)}), batch={"total": 195 * MB, "count": 1}))
        self.assertEqual(r["error"], "batch over 200 MB")
        r = uploads.precheck(req(headers=hdr(**{"Content-Length": str(2 * MB)})), {"image": MB})
        self.assertEqual(r["code"], 413)
        self.assertEqual(uploads.precheck(req())["query"]["name"], "hero.png")


class Guard(unittest.TestCase):
    def test_disk_and_pending_caps(self):
        gb = 1 << 30
        self.assertIsNone(uploads.guard(MB, 20 * gb, 8 * gb, 0))
        self.assertEqual(uploads.guard(MB, 8 * gb, 8 * gb, 0), (507, "disk low"))
        self.assertEqual(uploads.guard(MB, 20 * gb, 8 * gb, gb), (413, "design files over 1 GB"))


class DashboardUpload(unittest.TestCase):
    def setUp(self):
        from test_v04 import Dash
        self.d = Dash()

    def tearDown(self):
        self.d.close()

    def test_anonymous_upload_is_refused_without_reading_the_body(self):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        s.sendall((f"POST /api/p/demo/design-files/upload?{Q} HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                   f"Content-Length: {50 * MB}\r\nX-Hands: 1\r\n\r\n").encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        s.close()
        self.assertIn(b" 401 ", data.split(b"\r\n")[0])
        self.assertIn(b"Connection: close", data)

    def test_thumb_route_rejects_bad_ids(self):
        st, _, _ = self.d.req("GET", "/api/p/demo/design-files/x1/thumb")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
