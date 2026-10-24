"""hands.designfiles: checks, SVG/ZIP/video rules and the refs/design store."""
import io
import json
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from hands import designfiles as df

REPO = Path(__file__).resolve().parents[2]
FX = REPO / "acceptance" / "fixtures" / "c1-media"
SVG = '<svg xmlns="http://www.w3.org/2000/svg">%s</svg>'


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.dir = Path(self._t.name)

    def put(self, name, data):
        p = self.dir / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p


class TestNames(Base):
    def test_guess_kind_table(self):
        for name, kind in {"DESIGN.md": "design-md", "t.json": "style-pack", "logo.svg": "brand", "i.svg": "icon-pack",
                           "tone.txt": "copy-tone", "features.md": "feature-list", "dont.md": "avoid", "a.zip": "icon-pack",
                           "a.webm": "video", "a.woff2": "font", "a.jpeg": "screenshot"}.items():
            self.assertEqual(df.guess_kind(name), kind, name)

    def test_safe_name(self):
        self.assertEqual(df.safe_name("../../etc/passwd.md"), "passwd.md")
        self.assertEqual(df.safe_name("My Logo (final).PNG"), "My-Logo-final.png")
        self.assertEqual(df.safe_name(".hidden.md"), "hidden.md")
        self.assertRegex(df.safe_name("‮gnp.md"), r"^[A-Za-z0-9._-]+$")
        long = df.safe_name("x" * 300 + ".md")
        self.assertTrue(len(long) <= 80 and long.endswith(".md"))


class TestCheck(Base):
    def test_caps_boundary(self):
        self.assertTrue(df.check(self.put("a.md", b"a" * 1048576), "a.md")["ok"])
        with self.assertRaisesRegex(df.Refused, "too large"):
            df.check(self.put("b.md", b"a" * 1048577), "b.md")

    def test_type_hints(self):
        with self.assertRaisesRegex(df.Refused, r"HEIC: share the photo as JPEG"):
            df.check(self.put("a.heic", b"x"), "a.heic")
        with self.assertRaisesRegex(df.Refused, r"MOV: export as MP4"):
            df.check(self.put("a.mov", b"x"), "a.mov")

    def test_magic_mismatch(self):
        with self.assertRaisesRegex(df.Refused, "does not match its extension"):
            df.check(FX / "clean.jpg", "x.png")

    def test_secret_patterns_and_values(self):
        with self.assertRaisesRegex(df.Refused, r"secret found"):
            df.check(self.put("a.md", "k: ghp_" + "a" * 36), "a.md")
        with self.assertRaises(df.Refused) as cm:
            df.check(self.put("a.css", "x: phx-value-123456789"), "a.css", secrets={"K": "phx-value-123456789"})
        self.assertNotIn("phx-value-123456789", str(cm.exception))

    def test_svg_refusals(self):
        for body in (SVG % "<script>1</script>", '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>',
                     SVG % "<foreignObject/>", SVG % '<image href="https://e.x/a.png"/>',
                     SVG % "<style>@import url(https://e.x/a.css);</style>", SVG % '<a href="javascript:1"/>',
                     '<!DOCTYPE svg [<!ENTITY a "b">]>' + SVG % "&a;"):
            with self.assertRaisesRegex(df.Refused, "^svg: "):
                df.check(self.put("a.svg", body), "a.svg")

    def test_svg_reserialise_drops_comments_title(self):
        out = df.sanitize_svg((SVG % '<!-- c --><title>t</title><path d="M0 0"/>').encode()).decode()
        self.assertIn("<path", out)
        self.assertNotIn("<!--", out)
        self.assertNotIn("title", out)

    def test_zip_rules(self):
        def zip_of(members):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data, attr in members:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = attr
                    info.compress_type = zipfile.ZIP_DEFLATED
                    z.writestr(info, data)
            return self.put("p.zip", buf.getvalue())
        for members in ([("../e.md", "x", 0)], [("/a.md", "x", 0)], [("l.svg", "x", 0o120777 << 16)],
                        [("a.sh", "x", 0)], [("n.zip", "PK", 0)], [("z.md", "0" * 200000, 0)],
                        [(f"{i}.md", "x", 0) for i in range(201)]):
            with self.assertRaisesRegex(df.Refused, "^zip: "):
                df.check(zip_of(members), "p.zip")
        self.assertTrue(df.check(zip_of([("a.md", "# hi", 0)]), "p.zip")["ok"])


class TestVideo(Base):
    def test_mp4_udta_zeroed_same_length(self):
        def box(t, p):
            return struct.pack(">I", 8 + len(p)) + t + p
        mp4 = (box(b"ftyp", b"isom\0\0\0\1isom") + box(b"moov", box(b"mvhd", bytes(20)) + box(b"udta", b"PHX-GPS-SECRET"))
               + box(b"mdat", b"\7" * 16))
        dst = self.dir / "o.mp4"
        df.strip_video(self.put("a.mp4", mp4), dst)
        out = dst.read_bytes()
        self.assertEqual(len(out), len(mp4))
        self.assertNotIn(b"PHX-GPS-SECRET", out)
        self.assertNotIn(b"udta", out)

    def test_webm_tags_voided(self):
        def el(i, p):
            return i + b"\x01" + bytes(6) + bytes([len(p)]) + p
        webm = el(b"\x1a\x45\xdf\xa3", b"\x42\x82\x84webm") + b"\x18\x53\x80\x67\x01" + b"\xff" * 7 \
            + el(b"\x12\x54\xc3\x67", b"PHX-GPS-SECRET") + el(b"\x1f\x43\xb6\x75", b"\x09" * 8)
        dst = self.dir / "o.webm"
        df.strip_video(self.put("a.webm", webm), dst)
        out = dst.read_bytes()
        self.assertEqual(len(out), len(webm))
        self.assertNotIn(b"PHX-GPS-SECRET", out)
        self.assertIn(b"\x09" * 8, out)


class TestPngAndStore(Base):
    def test_clean_png_drops_text_chunks(self):
        out = df.clean_png((FX / "photo-meta.png").read_bytes())
        self.assertNotIn(b"tEXt", out)
        self.assertNotIn(b"PHX-GPS-SECRET", out)
        with self.assertRaises(df.Refused):
            df.clean_png((FX / "clean.jpg").read_bytes())

    def stage_image(self, src, name, clean=None):
        stage = self.dir / ("stage-" + name)
        df.prepare(FX / src, name, "screenshot", "n", stage)
        if clean:
            (stage / "clean" / "thumbs").mkdir(parents=True)
            (stage / "clean" / name).write_bytes((FX / clean).read_bytes())
            (stage / "clean" / "thumbs" / (name + ".png")).write_bytes((FX / "clean.png").read_bytes())
        return stage

    def test_ingest_requires_clean_raster(self):
        store = self.dir / "store"
        with self.assertRaisesRegex(df.Refused, "not sanitised"):
            df.ingest(store, self.stage_image("photo-exif.jpg", "a.jpg"))
        self.assertFalse((store / "refs" / "index.json").exists())

    def test_ingest_refuses_metadata_left(self):
        store = self.dir / "store"
        with self.assertRaisesRegex(df.Refused, "metadata left in b.jpg"):
            df.ingest(store, self.stage_image("photo-exif.jpg", "b.jpg", "photo-exif.jpg"))
        self.assertFalse((store / "refs" / "design").exists())

    def test_ids_never_reused_and_group_text_only(self):
        store = self.dir / "store"

        def add(name, body, kind="style-pack"):
            src = self.put(name, body)
            stage = self.dir / ("s-" + name + str(len(list(self.dir.iterdir()))))
            df.prepare(src, name, kind, "", stage)
            return df.ingest(store, stage)["id"]
        a, b = add("t.json", "{}"), add("t.css", "a{}")
        self.assertEqual((a, b), ("d001", "d002"))
        df.remove(store, b)
        self.assertEqual(add("t.css", "a{}"), "d003")
        g = df.group(store, [a, "d003"], "pack")
        self.assertEqual(g["id"], "d004")
        self.assertEqual([e["id"] for e in df.load_index(store)["files"]], ["d004"])
        with self.assertRaises(df.Refused):
            df.edit(store, "d004", kind="bogus")
