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


class GroupMarker(unittest.TestCase):
    def test_precheck_passes_the_group_marker_and_rejects_bad_ones(self):
        ok = uploads.precheck(req(path=f"/x?{Q}&group=g1&group_n=3"))
        self.assertEqual((ok["code"], ok["query"]["group"], ok["query"]["group_n"]), (0, "g1", "3"))
        for bad in ("group=g1", "group=g1&group_n=1", "group=g1&group_n=99", "group=a/b&group_n=2", "group_n=2"):
            r = uploads.precheck(req(path=f"/x?{Q}&{bad}"))
            self.assertEqual(r["code"], 400, bad)

    def _cfg(self, tmp):
        from types import SimpleNamespace
        spec = Path(tmp) / "spec"
        spec.mkdir()
        return SimpleNamespace(state_dir=Path(tmp) / "state", spec_dir=spec, runner_home=Path(tmp), docker_bin="docker",
                               image="hands", project_env=None, design_caps={})

    def _receive(self, box, name, data, group="", n=0):
        import io
        from hands import designfiles
        return designfiles.receive(box, io.BytesIO(data), name, "style-pack", "", "b1", len(data), group, n)

    def test_grouped_uploads_become_one_style_pack_entry_when_the_last_one_is_processed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            box = Path(tmp) / "box"
            ids = [self._receive(box, n, b"x", "g1", 3) for n in ("a.md", "b.css", "c.txt")]
            self._receive(box, "solo.md", b"y")
            for ident in ids[:2]:
                res = uploads.process(cfg, box / ident)
                self.assertEqual(res["status"], "ready")
                self.assertNotIn("group_id", res)
            res = uploads.process(cfg, box / ids[2])
            entries = uploads.index(cfg)["files"]
            self.assertEqual([e["kind"] for e in entries], ["style-pack"])
            self.assertEqual(sorted(entries[0]["files"]), sorted(f"refs/design/{entries[0]['id']}/{n}" for n in ("a.md", "b.css", "c.txt")))
            self.assertEqual(res["group_id"], entries[0]["id"])

    def test_a_refused_member_is_skipped_and_a_lone_survivor_stays_single(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            box = Path(tmp) / "box"
            ids = [self._receive(box, "a.md", b"x", "g2", 2), self._receive(box, "b.json", b"not json", "g2", 2)]
            for ident in ids:
                res = uploads.process(cfg, box / ident)
            self.assertEqual(len(uploads.index(cfg)["files"]), 1)
            self.assertIn("group_error", res)


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
