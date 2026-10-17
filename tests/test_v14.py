"""0.5 step 14: File browser page. Backend additions (denied list for the settings view, image preview
route) and static checks on the page code (served, no colour literals or font declarations)."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401

from hands import files  # noqa: E402
from test_v13 import Base  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class TestRootsInfo(Base):
    def test_denied_paths_are_listed_for_the_settings_view(self):
        st, d = self.j(self.get("/api/files/roots"))
        self.assertEqual(st, 200)
        self.assertEqual(d["home"], str(self.home))
        for p in ("~/.config/hands", "~/.ssh", "~/.config/gh", "~/.git-credentials", "~/.claude"):
            self.assertIn(p, d["denied"])
        self.assertEqual(len(d["denied"]), len(set(d["denied"])))      # no duplicates (CONFIG_HOME may equal ~/.config/hands)
        self.assertIn("max_image", d["limits"])


class TestImage(Base):
    def setUp(self):
        super().setUp()
        (self.work / "dot.png").write_bytes(PNG)
        (self.work / "photo.JPG").write_bytes(b"\xff\xd8\xff\xe0fake")
        (self.work / "x.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
        (self.work / "page.png.html").write_text("<script>alert(1)</script>")

    def img(self, path):
        return self.get("/api/files/image?" + self.q(root=str(self.work), path=path))

    def test_serves_raster_images_with_their_type(self):
        st, hdr, body = self.img("dot.png")
        self.assertEqual(st, 200)
        self.assertEqual(body, PNG)
        self.assertEqual(hdr["Content-Type"], "image/png")
        self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'none'", hdr["Content-Security-Policy"])
        self.assertTrue(hdr["Content-Disposition"].startswith("inline"))
        self.assertEqual(self.img("photo.JPG")[1]["Content-Type"], "image/jpeg")

    def test_refusals(self):
        self.assertEqual(self.img("x.svg")[0], 400)          # SVG can carry script: shown as text, never as an image
        self.assertEqual(self.img("a.txt")[0], 400)
        self.assertEqual(self.img("page.png.html")[0], 400)
        self.assertEqual(self.img("sub")[0], 400)
        self.assertEqual(self.img("nope.png")[0], 404)
        self.assertEqual(self.img("../x.png")[0], 400)
        self.assertEqual(self.get("/api/files/image?" + self.q(root=str(self.work), path="to-hands/claude.env"))[0], 403)
        self.assertEqual(self.get("/api/files/image?" + self.q(root="/etc", path="x.png"))[0], 404)

    def test_size_cap(self):
        old = files.MAX_IMAGE
        files.MAX_IMAGE = 10
        try:
            self.assertEqual(self.img("dot.png")[0], 413)
        finally:
            files.MAX_IMAGE = old

    def test_needs_login(self):
        st, _, _ = self.get("/api/files/image?" + self.q(root=str(self.work), path="dot.png"), cookie=False)
        self.assertEqual(st, 401)


class TestPageAssets(Base):
    def test_files_js_is_served_and_referenced(self):
        st, hdr, body = self.get("/files.js")
        self.assertEqual(st, 200)
        self.assertIn("text/javascript", hdr["Content-Type"])
        html = self.get("/")[2]
        self.assertIn(b'src="/files.js"', html)
        self.assertNotIn(b"Coming soon", html)

    def test_no_colour_literals_or_fonts_in_file_browser_code(self):
        js = (ROOT / "hands" / "static" / "files.js").read_text()
        for pat in (r"#[0-9a-fA-F]{3,8}\b", r"\brgba?\(", r"\bhsla?\(", r"font-family", r"\bfont\s*[:=]", r"\.style\.(color|background|font)"):
            self.assertIsNone(re.search(pat, js), pat)
        html = (ROOT / "hands" / "static" / "index.html").read_text()
        self.assertNotIn("style=", html)


if __name__ == "__main__":
    unittest.main()
