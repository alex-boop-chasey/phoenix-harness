"""Session manager: the working folder is marked trusted in Claude's config before a session starts, so the
workspace trust dialog ("Do you trust the files in this folder?") never has to be answered in a terminal."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tmpdirs  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hands import sessions  # noqa: E402


class TrustFolder(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = self.home / ".claude.json"
        self.folder = str(self.home / "work")

    def read(self):
        return json.loads(self.cfg.read_text())

    def test_missing_file_is_created_private(self):
        self.assertTrue(sessions.trust_folder(self.folder, self.cfg))
        self.assertEqual(self.read(), {"projects": {self.folder: {"hasTrustDialogAccepted": True}}})
        self.assertEqual(stat.S_IMODE(self.cfg.stat().st_mode), 0o600)

    def test_only_that_key_of_that_folder_changes(self):
        before = {"numStartups": 7, "oauthAccount": {"x": 1},
                  "projects": {"/other": {"hasTrustDialogAccepted": False, "allowedTools": ["a"]},
                               self.folder: {"hasTrustDialogAccepted": False, "allowedTools": ["b"], "history": [1]}}}
        self.cfg.write_text(json.dumps(before))
        os.chmod(self.cfg, 0o644)
        self.assertTrue(sessions.trust_folder(self.folder, self.cfg))
        after = self.read()
        before["projects"][self.folder]["hasTrustDialogAccepted"] = True
        self.assertEqual(after, before)
        self.assertEqual(stat.S_IMODE(self.cfg.stat().st_mode), 0o644)       # mode kept as it was
        self.assertEqual(list(after["projects"]), ["/other", self.folder])     # order kept
        # already trusted: nothing rewritten
        mtime = self.cfg.stat().st_mtime_ns
        self.assertTrue(sessions.trust_folder(self.folder, self.cfg))
        self.assertEqual(self.cfg.stat().st_mtime_ns, mtime)

    def test_broken_config_is_left_alone(self):
        for raw in ("{not json", "[]", json.dumps({"projects": []}), json.dumps({"projects": {self.folder: 3}})):
            self.cfg.write_text(raw)
            self.assertFalse(sessions.trust_folder(self.folder, self.cfg), raw)
            self.assertEqual(self.cfg.read_text(), raw)
        self.assertEqual([p.name for p in self.home.iterdir()], [".claude.json"])   # no temp file left

    def test_config_path_follows_claude_config_dir(self):
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        try:
            os.environ["CLAUDE_CONFIG_DIR"] = str(self.home / "cfgdir")
            self.assertEqual(sessions.claude_config_file(), self.home / "cfgdir" / ".claude.json")
            os.environ.pop("CLAUDE_CONFIG_DIR")
            self.assertEqual(sessions.claude_config_file(), Path.home() / ".claude.json")
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old

    def test_start_trusts_before_launch(self):
        calls = []
        m = sessions.PtyBackend()
        m.launch = lambda sid, argv, env, cwd: calls.append(("launch", cwd))
        old = sessions.trust_folder, sessions.claude_exe
        sessions.trust_folder = lambda cwd, path=None: calls.append(("trust", cwd)) or True
        sessions.claude_exe = lambda: "/bin/true"
        try:
            m.start({"id": "0d8d2a3e-5f3a-4a2b-9c1d-0123456789ab", "cwd": self.folder}, False)
        finally:
            sessions.trust_folder, sessions.claude_exe = old
        self.assertEqual(calls, [("trust", self.folder), ("launch", self.folder)])


if __name__ == "__main__":
    unittest.main()
