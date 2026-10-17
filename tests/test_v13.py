"""0.5 step 13: File browser backend (hands/files.py + dashboard routes). Pure filesystem, no docker."""
import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import dashboard, files  # noqa: E402
from test_v04 import Dash  # noqa: E402


class Base(unittest.TestCase):
    """A fake home with two allowed roots (work, other), the secret folders of the real layout, and a
    few traps: symlinks out of the root, a symlink loop, a symlink into ~/.config/hands."""

    def setUp(self):
        self.d = Dash()
        self.home = Path(tempfile.mkdtemp(prefix="hands-fh-")).resolve()
        self.old_home = files.HOME
        files.HOME = self.home
        self.work = self.home / "work"
        self.other = self.home / "other"
        self.outside = Path(tempfile.mkdtemp(prefix="hands-out-")).resolve()
        for d in (self.work / "sub" / "deep", self.work / ".git" / "objects", self.other,
                  self.home / ".config" / "hands", self.home / ".ssh", self.home / ".config" / "gh",
                  self.home / ".claude", self.home / "secretish"):
            d.mkdir(parents=True)
        (self.work / "a.txt").write_text("hello\nworld\n")
        (self.work / "sub" / "b.md").write_text("# b\n")
        (self.work / "sub" / "deep" / "c.bin").write_bytes(b"\x00\x01binary")
        (self.work / ".git" / "config").write_text("[core]\n")
        (self.home / ".config" / "hands" / "claude.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=secret\n")
        (self.home / ".ssh" / "id_ed25519").write_text("PRIVATE\n")
        (self.home / ".git-credentials").write_text("https://x:y@github.com\n")
        (self.home / ".claude" / ".credentials.json").write_text("{}")
        (self.outside / "o.txt").write_text("outside\n")
        os.symlink(self.home / ".config" / "hands", self.work / "to-hands")
        os.symlink(self.home / ".ssh" / "id_ed25519", self.work / "to-key")
        os.symlink(self.outside, self.work / "to-outside")
        os.symlink(self.work / "loop2", self.work / "loop1")
        os.symlink(self.work / "loop1", self.work / "loop2")
        os.symlink(self.work / "sub", self.work / "to-sub")         # inside the root: allowed
        os.symlink(self.other, self.work / "to-other")             # another allowed root: allowed
        files.save_settings({"roots": [str(self.work), str(self.other)], "git_writes": False})

    def tearDown(self):
        files.HOME = self.old_home
        self.d.close()

    def get(self, path, **kw):
        return self.d.req("GET", path, **kw)

    def post(self, path, body=None, raw=None, headers=None, **kw):
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.d.port}", "Cookie": f"{dashboard.COOKIE}={self.d.sid}",
             "Origin": f"http://127.0.0.1:{self.d.port}", "X-Hands": "1"}
        h.update(kw.get("h", {}))
        h.update(headers or {})
        data = raw if raw is not None else json.dumps(body or {}).encode()
        c.request("POST", path, headers=h, body=data)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, dict(r.getheaders()), out

    def j(self, res):
        st, _, body = res
        return st, json.loads(body)

    def q(self, **kw):
        import urllib.parse
        return urllib.parse.urlencode(kw)


# ---------------------------------------------------------------- settings
class TestSettings(Base):
    def test_defaults_and_file(self):
        p = files.settings_path()
        p.unlink()
        s = files.settings()
        self.assertEqual(s["roots"], [str(self.home / n) for n in ("harness", "projects", "ops", "claude-harness")])
        self.assertFalse(s["git_writes"])
        files.save_settings({"roots": [str(self.work)], "git_writes": True})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(files.settings(), {"roots": [str(self.work)], "git_writes": True})
        # ~ is expanded and resolved; duplicates collapse
        files.save_settings({"roots": ["~/work", str(self.work), "~/other"], "git_writes": False})
        self.assertEqual(files.settings()["roots"], [str(self.work), str(self.other)])

    def test_rejects_bad_roots(self):
        bad = ["/etc", str(self.home), "~/.config/hands", "~/.ssh", "~/.claude", "~/work/a.txt", "~/missing",
               "relative", "~/work\0x", str(self.work / "to-outside"), str(self.work / "to-hands")]
        for r in bad:
            with self.subTest(r=r), self.assertRaises(ValueError):
                files.save_settings({"roots": [r], "git_writes": False})
        with self.assertRaises(ValueError):
            files.save_settings({"roots": [], "git_writes": False})
        with self.assertRaises(ValueError):
            files.save_settings({"roots": [str(self.work)], "git_writes": "yes"})
        with self.assertRaises(ValueError):
            files.save_settings({"roots": [str(self.work)] * 1, "git_writes": False, "extra": 1})
        self.assertEqual(files.settings()["roots"], [str(self.work), str(self.other)])    # unchanged

    def test_roots_api(self):
        st, body = self.j(self.get("/api/files/roots"))
        self.assertEqual(st, 200)
        self.assertEqual([r["path"] for r in body["roots"]], [str(self.work), str(self.other)])
        self.assertEqual([r["name"] for r in body["roots"]], ["work", "other"])
        self.assertTrue(all(r["exists"] for r in body["roots"]))
        self.assertFalse(body["git_writes"])
        self.assertIn("max_upload", body["limits"])
        st, body = self.j(self.post("/api/files/settings", {"roots": [str(self.other)], "git_writes": True}))
        self.assertEqual(st, 200)
        self.assertEqual([r["path"] for r in body["roots"]], [str(self.other)])
        self.assertTrue(body["git_writes"])
        st, body = self.j(self.post("/api/files/settings", {"roots": ["/etc"], "git_writes": True}))
        self.assertEqual(st, 400)
        # a root that disappeared is still listed, flagged
        files.save_settings({"roots": [str(self.work), str(self.other)], "git_writes": False})
        os.rmdir(self.other)
        st, body = self.j(self.get("/api/files/roots"))
        self.assertEqual([(r["name"], r["exists"]) for r in body["roots"]], [("work", True), ("other", False)])
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.other), path="")))
        self.assertEqual(st, 404)


# ---------------------------------------------------------------- the resolver (refusal table)
class TestResolve(Base):
    def test_allowed(self):
        self.assertEqual(files.resolve(str(self.work), ""), self.work)
        self.assertEqual(files.resolve(str(self.work), "sub/deep"), self.work / "sub" / "deep")
        self.assertEqual(files.resolve(str(self.work), "to-sub/b.md"), self.work / "sub" / "b.md")
        self.assertEqual(files.resolve(str(self.work), "to-other"), self.other)
        self.assertEqual(files.resolve(str(self.work), "sub/./deep"), self.work / "sub" / "deep")
        self.assertEqual(files.resolve(str(self.work), "sub/"), self.work / "sub")

    def test_refusals(self):
        table = {
            "../a.txt": ValueError, "sub/../../other": ValueError, "..": ValueError,
            "%2e%2e/a.txt": LookupError,                       # not decoded twice: a literal name that does not exist
            "/etc/passwd": ValueError, str(self.work / "a.txt"): ValueError,
            "to-hands": PermissionError, "to-hands/claude.env": PermissionError,
            "to-key": PermissionError, "to-outside": PermissionError, "to-outside/o.txt": PermissionError,
            "loop1": LookupError, "loop1/x": LookupError,
            "a\0.txt": ValueError, "x" * 256: ValueError, "sub/" + "y" * 300: ValueError, "/".join(["s"] * 3000): ValueError,
            "missing/file": LookupError,
        }
        for rel, exc in table.items():
            with self.subTest(rel=rel[:40]), self.assertRaises(exc):
                files.resolve(str(self.work), rel)
        for root in ("/etc", str(self.home), str(self.home / ".ssh"), "", str(self.work) + "/", "~/work",
                     str(self.home / "secretish"), str(self.work / "sub")):
            with self.subTest(root=root), self.assertRaises(LookupError):
                files.resolve(root, "")
        with self.assertRaises(ValueError):
            files.resolve(str(self.work), 5)
        with self.assertRaises(ValueError):
            files.resolve(5, "")

    def test_root_inside_denied_is_refused_even_if_configured(self):
        """A root whose real location is a secret folder (e.g. settings edited by hand) never serves anything."""
        s = files.settings_path()
        s.write_text(json.dumps({"version": 1, "roots": [str(self.home / ".config" / "hands"), str(self.work)], "git_writes": False}))
        self.assertEqual(files.settings()["roots"], [str(self.work)])
        with self.assertRaises(LookupError):
            files.resolve(str(self.home / ".config" / "hands"), "")
        st, body = self.j(self.get("/api/files/roots"))
        self.assertEqual([r["path"] for r in body["roots"]], [str(self.work)])

    def test_new_path_for_writes(self):
        parent, name = files.resolve_new(str(self.work), "sub/new.txt")
        self.assertEqual((parent, name), (self.work / "sub", "new.txt"))
        with self.assertRaises(LookupError):
            files.resolve_new(str(self.work), "nope/new.txt")            # parent must exist
        with self.assertRaises(PermissionError):
            files.resolve_new(str(self.work), "to-hands/new.txt")
        with self.assertRaises(PermissionError):
            files.resolve_new(str(self.work), "to-outside/new.txt")
        with self.assertRaises(ValueError):
            files.resolve_new(str(self.work), "")                         # the root itself is not a new path
        with self.assertRaises(ValueError):
            files.resolve_new(str(self.work), "sub/.")
        with self.assertRaises(PermissionError):
            files.resolve_new(str(self.work), ".git/hooks")              # .git writes off by default
        with self.assertRaises(ValueError):
            files.resolve_new(str(self.work), "to-sub/../.git/x")        # .. still refused
        files.save_settings({"roots": [str(self.work), str(self.other)], "git_writes": True})
        self.assertEqual(files.resolve_new(str(self.work), ".git/hooks"), (self.work / ".git", "hooks"))
        # the final component may not be a symlink (writes through a link are refused, even inside the root)
        with self.assertRaises(PermissionError):
            files.resolve_new(str(self.work), "to-sub")


# ---------------------------------------------------------------- operations over HTTP
class TestOps(Base):
    def test_auth_and_csrf_like_the_dashboard(self):
        self.assertEqual(self.get("/api/files/roots", cookie=False)[0], 401)
        self.assertEqual(self.get("/api/files/roots", host="localhost:1")[0], 403)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "n"},
                                   headers={"Origin": "http://evil"})[0], 403)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "n"}, headers={"X-Hands": ""})[0], 403)
        self.assertEqual(self.post("/api/files/upload?" + self.q(root=str(self.work), path="n"), raw=b"x",
                                   headers={"Origin": "http://evil"})[0], 403)
        self.assertFalse((self.work / "n").exists())

    def test_list(self):
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="")))
        self.assertEqual(st, 200)
        self.assertEqual(body["root"], str(self.work))
        self.assertEqual(body["path"], "")
        names = [e["name"] for e in body["entries"]]
        self.assertEqual(names[:2], [".git", "sub"])                     # folders first, sorted
        self.assertIn("a.txt", names)
        e = {x["name"]: x for x in body["entries"]}
        self.assertEqual(e["a.txt"]["type"], "file")
        self.assertEqual(e["a.txt"]["size"], 12)
        self.assertEqual(e["a.txt"]["mode"], f"{os.stat(self.work / 'a.txt').st_mode & 0o7777:04o}")
        self.assertIsInstance(e["a.txt"]["modified"], int)
        self.assertEqual(e["sub"]["type"], "dir")
        self.assertEqual(e["to-sub"]["type"], "link")
        self.assertTrue(e["to-sub"]["ok"])
        self.assertEqual(e["to-sub"]["target_type"], "dir")
        self.assertFalse(e["to-hands"]["ok"])                            # link out of bounds: shown, unusable
        self.assertFalse(e["to-outside"]["ok"])
        self.assertFalse(e["loop1"]["ok"])
        self.assertFalse(body["truncated"])
        self.assertFalse(body["git"])
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="sub/deep")))
        self.assertEqual([x["name"] for x in body["entries"]], ["c.bin"])
        self.assertEqual(body["path"], "sub/deep")
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="to-sub")))
        self.assertEqual(st, 200)
        self.assertEqual(body["path"], "sub")                           # canonical path under the root
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path=".git")))
        self.assertTrue(body["git"])
        self.assertEqual(self.get("/api/files/list?" + self.q(root=str(self.work), path="a.txt"))[0], 400)
        self.assertEqual(self.get("/api/files/list?" + self.q(root=str(self.work), path="to-hands"))[0], 403)
        self.assertEqual(self.get("/api/files/list?" + self.q(root=str(self.work), path="../other"))[0], 400)
        self.assertEqual(self.get("/api/files/list?" + self.q(root=str(self.work), path="%2e%2e/x"))[0], 404)
        self.assertEqual(self.get("/api/files/list?" + self.q(root=str(self.home), path=""))[0], 404)
        self.assertEqual(self.get("/api/files/list?" + self.q(root=str(self.work), path="a\0b"))[0], 400)
        # a link to another allowed root resolves to that root
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.work), path="to-other")))
        self.assertEqual((body["root"], body["path"]), (str(self.other), ""))

    def test_list_truncated(self):
        d = self.other / "many"
        d.mkdir()
        for i in range(files.MAX_LIST + 5):
            (d / f"f{i:05d}").write_text("")
        st, body = self.j(self.get("/api/files/list?" + self.q(root=str(self.other), path="many")))
        self.assertEqual(len(body["entries"]), files.MAX_LIST)
        self.assertTrue(body["truncated"])

    def test_read(self):
        st, body = self.j(self.get("/api/files/read?" + self.q(root=str(self.work), path="a.txt")))
        self.assertEqual(st, 200)
        self.assertEqual(body, {"root": str(self.work), "path": "a.txt", "text": "hello\nworld\n", "size": 12,
                               "truncated": False, "binary": False})
        st, body = self.j(self.get("/api/files/read?" + self.q(root=str(self.work), path="sub/deep/c.bin")))
        self.assertEqual(st, 200)
        self.assertEqual(body["binary"], True)
        self.assertNotIn("text", body)
        big = self.work / "big.txt"
        big.write_bytes(b"x" * (files.MAX_TEXT + 10))
        st, body = self.j(self.get("/api/files/read?" + self.q(root=str(self.work), path="big.txt")))
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["text"]), files.MAX_TEXT)
        self.assertEqual(body["size"], files.MAX_TEXT + 10)
        self.assertEqual(self.get("/api/files/read?" + self.q(root=str(self.work), path="sub"))[0], 400)
        self.assertEqual(self.get("/api/files/read?" + self.q(root=str(self.work), path="to-key"))[0], 403)
        self.assertEqual(self.get("/api/files/read?" + self.q(root=str(self.work), path="to-outside/o.txt"))[0], 403)
        self.assertEqual(self.get("/api/files/read?" + self.q(root=str(self.work), path="nope"))[0], 404)
        self.assertEqual(self.get("/api/files/read?" + self.q(root=str(self.work), path="../.ssh/id_ed25519"))[0], 400)
        # UTF-8 with invalid bytes counts as binary; BOM text is text
        (self.work / "bad.txt").write_bytes(b"caf\xe9 latin1\n")
        self.assertTrue(self.j(self.get("/api/files/read?" + self.q(root=str(self.work), path="bad.txt")))[1]["binary"])

    def test_download_streams_with_headers(self):
        data = os.urandom(300_000)
        (self.work / "blob.dat").write_bytes(data)
        st, hdr, body = self.get("/api/files/download?" + self.q(root=str(self.work), path="blob.dat"))
        self.assertEqual(st, 200)
        self.assertEqual(body, data)
        self.assertEqual(hdr["Content-Type"], "application/octet-stream")
        self.assertEqual(hdr["Content-Length"], str(len(data)))
        self.assertEqual(hdr["Content-Disposition"], 'attachment; filename="blob.dat"')
        self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")
        (self.work / 'we "ird\n;.txt').write_text("x")
        st, hdr, _ = self.get("/api/files/download?" + self.q(root=str(self.work), path='we "ird\n;.txt'))
        self.assertEqual(st, 200)
        self.assertNotIn("\n", hdr["Content-Disposition"])
        self.assertTrue(hdr["Content-Disposition"].startswith("attachment; filename*=UTF-8''"))
        self.assertEqual(self.get("/api/files/download?" + self.q(root=str(self.work), path="sub"))[0], 400)
        self.assertEqual(self.get("/api/files/download?" + self.q(root=str(self.work), path="to-key"))[0], 403)
        self.assertEqual(self.get("/api/files/download?" + self.q(root=str(self.work), path="nope"))[0], 404)

    def test_mkdir(self):
        st, body = self.j(self.post("/api/files/mkdir", {"root": str(self.work), "path": "sub/newdir"}))
        self.assertEqual(st, 200)
        self.assertEqual(body, {"ok": True, "root": str(self.work), "path": "sub/newdir"})
        self.assertTrue((self.work / "sub" / "newdir").is_dir())
        self.assertEqual((self.work / "sub" / "newdir").stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "sub/newdir"})[0], 409)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "a.txt"})[0], 409)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "nope/x"})[0], 404)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "to-hands/x"})[0], 403)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": ".git/x"})[0], 403)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "../x"})[0], 400)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": "/tmp/x"})[0], 400)
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work)})[0], 400)
        self.assertEqual(self.post("/api/files/mkdir", raw=b"nonsense")[0], 400)
        self.assertFalse((self.work / ".git" / "x").exists())
        self.assertFalse((self.home / ".config" / "hands" / "x").exists())

    def test_rename_and_move(self):
        st, body = self.j(self.post("/api/files/rename", {"root": str(self.work), "path": "a.txt", "to": "sub/a2.txt"}))
        self.assertEqual(st, 200)
        self.assertEqual(body, {"ok": True, "root": str(self.work), "path": "sub/a2.txt"})
        self.assertEqual((self.work / "sub" / "a2.txt").read_text(), "hello\nworld\n")
        self.assertFalse((self.work / "a.txt").exists())
        # into another allowed root
        st, body = self.j(self.post("/api/files/rename", {"root": str(self.work), "path": "sub/a2.txt",
                                                          "to_root": str(self.other), "to": "moved.txt"}))
        self.assertEqual(st, 200)
        self.assertEqual(body["root"], str(self.other))
        self.assertTrue((self.other / "moved.txt").is_file())
        # a folder, including its contents
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "sub", "to": "sub2"})[0], 200)
        self.assertTrue((self.work / "sub2" / "deep" / "c.bin").is_file())
        # refusals: existing target, the root itself, into itself, outside, .git, through a bad link
        (self.work / "x.txt").write_text("x")
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "x.txt", "to": "sub2/b.md"})[0], 409)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "", "to": "r"})[0], 400)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "sub2", "to": "sub2/deep/s"})[0], 400)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "x.txt", "to": "../x.txt"})[0], 400)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "x.txt", "to": "to-outside/x"})[0], 403)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "x.txt", "to": ".git/x"})[0], 403)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": ".git/config", "to": "c"})[0], 403)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "to-hands", "to": "h"})[0], 403)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "x.txt",
                                                         "to_root": str(self.home), "to": "x"})[0], 404)
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "nope", "to": "n"})[0], 404)
        self.assertTrue((self.work / "x.txt").is_file())
        self.assertTrue((self.home / ".config" / "hands").is_dir())
        self.assertTrue((self.work / "to-hands").is_symlink())
        # the link itself (not its target) can be renamed when it points inside the roots
        self.assertEqual(self.post("/api/files/rename", {"root": str(self.work), "path": "to-sub", "to": "lnk"})[0], 200)
        self.assertTrue((self.work / "lnk").is_symlink())

    def test_delete(self):
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "sub/deep/c.bin"})[0], 200)
        self.assertFalse((self.work / "sub" / "deep" / "c.bin").exists())
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "sub/deep"})[0], 200)     # empty folder
        self.assertFalse((self.work / "sub" / "deep").exists())
        st, body = self.j(self.post("/api/files/delete", {"root": str(self.work), "path": "sub"}))
        self.assertEqual(st, 409)                                                                            # not empty
        self.assertIn("not empty", body["error"])
        self.assertTrue((self.work / "sub" / "b.md").is_file())
        (self.work / "sub" / "lnk").symlink_to(self.home / ".ssh")
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "sub", "recursive": True})[0], 200)
        self.assertFalse((self.work / "sub").exists())
        self.assertTrue((self.home / ".ssh" / "id_ed25519").is_file())    # the link was removed, not followed
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "sub", "recursive": "yes"})[0], 400)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": ""})[0], 400)          # never the root
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "", "recursive": True})[0], 400)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "to-hands"})[0], 403)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "to-key"})[0], 403)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": ".git/config"})[0], 403)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": ".git", "recursive": True})[0], 403)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "nope"})[0], 404)
        self.assertTrue(self.work.is_dir() and (self.work / ".git" / "config").is_file())
        self.assertTrue((self.work / "to-hands").is_symlink() and (self.home / ".config" / "hands" / "claude.env").is_file())
        # a link inside the roots is deleted as a link
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "to-other"})[0], 200)
        self.assertTrue(self.other.is_dir())
        # deleting a link to another root as "recursive" still only removes the link
        os.symlink(self.other, self.work / "to-other")
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": "to-other", "recursive": True})[0], 200)
        self.assertTrue(self.other.is_dir())

    def test_upload(self):
        data = os.urandom(200_000) + b"\n"
        st, body = self.j(self.post("/api/files/upload?" + self.q(root=str(self.work), path="sub/up.bin"), raw=data,
                                    headers={"Content-Type": "application/octet-stream"}))
        self.assertEqual(st, 200)
        self.assertEqual(body, {"ok": True, "root": str(self.work), "path": "sub/up.bin", "size": len(data)})
        self.assertEqual((self.work / "sub" / "up.bin").read_bytes(), data)
        self.assertEqual((self.work / "sub" / "up.bin").stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in (self.work / "sub").iterdir()), ["b.md", "deep", "up.bin"])   # no temp left
        # no overwrite without the flag
        self.assertEqual(self.post("/api/files/upload?" + self.q(root=str(self.work), path="sub/up.bin"), raw=b"new")[0], 409)
        self.assertEqual((self.work / "sub" / "up.bin").read_bytes(), data)
        st, body = self.j(self.post("/api/files/upload?" + self.q(root=str(self.work), path="sub/up.bin", overwrite=1), raw=b"new"))
        self.assertEqual(st, 200)
        self.assertEqual((self.work / "sub" / "up.bin").read_bytes(), b"new")
        # over the cap: refused before reading the body
        st, body = self.j(self.post("/api/files/upload?" + self.q(root=str(self.work), path="huge"), raw=b"",
                                    headers={"Content-Length": str(files.MAX_UPLOAD + 1)}))
        self.assertEqual(st, 413)
        self.assertFalse((self.work / "huge").exists())
        # chunked bodies are refused (the length must be known up front)
        st, body = self.j(self.post("/api/files/upload?" + self.q(root=str(self.work), path="chunky"), raw=b"",
                                    headers={"Transfer-Encoding": "chunked"}))
        self.assertEqual(st, 411)
        self.assertFalse((self.work / "chunky").exists())
        # refusals, none of which creates anything
        for path, code in (("to-hands/x", 403), (".git/x", 403), ("../x", 400), ("nope/x", 404), ("sub", 409), ("", 400),
                            ("to-outside/x", 403), ("to-sub", 403)):
            with self.subTest(path=path):
                self.assertEqual(self.post("/api/files/upload?" + self.q(root=str(self.work), path=path), raw=b"x")[0], code)
        self.assertEqual(self.post("/api/files/upload?" + self.q(root=str(self.home), path="x"), raw=b"x")[0], 404)
        self.assertFalse((self.home / ".config" / "hands" / "x").exists())
        self.assertFalse((self.outside / "x").exists())
        self.assertFalse((self.work / ".git" / "x").exists())
        self.assertEqual(sorted(p.name for p in self.work.iterdir() if p.name.startswith(".")), [".git"])
        # a short body (client died) leaves nothing behind
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        c.putrequest("POST", "/api/files/upload?" + self.q(root=str(self.work), path="short.bin"))
        for k, v in {"Host": f"127.0.0.1:{self.d.port}", "Cookie": f"{dashboard.COOKIE}={self.d.sid}",
                     "Origin": f"http://127.0.0.1:{self.d.port}", "X-Hands": "1", "Content-Length": "1000"}.items():
            c.putheader(k, v)
        c.endheaders()
        c.send(b"x" * 10)
        c.sock.close()
        import time
        time.sleep(0.5)
        self.assertFalse((self.work / "short.bin").exists())
        self.assertEqual(sorted(p.name for p in self.work.iterdir() if "upload" in p.name), [])
        # an empty file is fine
        st, body = self.j(self.post("/api/files/upload?" + self.q(root=str(self.work), path="empty"), raw=b""))
        self.assertEqual(st, 200)
        self.assertEqual((self.work / "empty").stat().st_size, 0)

    def test_search(self):
        (self.work / "sub" / "Notes.TXT").write_text("")
        (self.work / ".git" / "a.txt.pack").write_text("")
        (self.work / "node_modules").mkdir()
        (self.work / "node_modules" / "a.txt").write_text("")
        st, body = self.j(self.get("/api/files/search?" + self.q(root=str(self.work), q="txt")))
        self.assertEqual(st, 200)
        self.assertEqual([h["path"] for h in body["hits"]], ["a.txt", "sub/Notes.TXT"])     # .git, node_modules, links skipped
        self.assertEqual(body["hits"][0]["type"], "file")
        self.assertFalse(body["truncated"])
        st, body = self.j(self.get("/api/files/search?" + self.q(root=str(self.work), q="DEEP")))
        self.assertEqual([h["path"] for h in body["hits"]], ["sub/deep"])
        # no result through links, none from secrets
        st, body = self.j(self.get("/api/files/search?" + self.q(root=str(self.work), q="claude.env")))
        self.assertEqual(body["hits"], [])
        st, body = self.j(self.get("/api/files/search?" + self.q(root=str(self.work), q="id_ed")))
        self.assertEqual(body["hits"], [])
        self.assertEqual(self.get("/api/files/search?" + self.q(root=str(self.work), q=""))[0], 400)
        self.assertEqual(self.get("/api/files/search?" + self.q(root=str(self.work), q="x" * 300))[0], 400)
        self.assertEqual(self.get("/api/files/search?" + self.q(root=str(self.home), q="a"))[0], 404)
        # bounded: a big tree yields at most MAX_HITS hits and is flagged
        d = self.other / "lots"
        d.mkdir()
        for i in range(files.MAX_HITS + 20):
            (d / f"hit{i}.txt").write_text("")
        st, body = self.j(self.get("/api/files/search?" + self.q(root=str(self.other), q="hit")))
        self.assertEqual(len(body["hits"]), files.MAX_HITS)
        self.assertTrue(body["truncated"])

    def test_git_writes_setting(self):
        files.save_settings({"roots": [str(self.work), str(self.other)], "git_writes": True})
        self.assertEqual(self.post("/api/files/mkdir", {"root": str(self.work), "path": ".git/hooks"})[0], 200)
        self.assertEqual(self.post("/api/files/delete", {"root": str(self.work), "path": ".git/hooks"})[0], 200)
        self.assertEqual(self.post("/api/files/upload?" + self.q(root=str(self.work), path=".git/x"), raw=b"x")[0], 200)
        # reads of .git were always allowed
        files.save_settings({"roots": [str(self.work), str(self.other)], "git_writes": False})
        self.assertEqual(self.get("/api/files/read?" + self.q(root=str(self.work), path=".git/config"))[0], 200)


if __name__ == "__main__":
    unittest.main()
