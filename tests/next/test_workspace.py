"""hands.workspace.rmtree works on the harness's Python 3.11 as well as 3.12 (shutil's onexc is 3.12+)."""
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from hands import workspace


class RmTree(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-ws-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_removes_tree_with_read_only_directories(self):
        tree = self.tmp / "work"
        locked = tree / "a" / "b"
        locked.mkdir(parents=True)
        (locked / "f.txt").write_text("x")
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        os.chmod(locked.parent, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: [os.chmod(d, 0o700) for d in (locked.parent, locked) if d.exists()])
        workspace.rmtree(tree)
        self.assertFalse(tree.exists())

    def test_missing_path_is_fine(self):
        workspace.rmtree(self.tmp / "nope")


if __name__ == "__main__":
    unittest.main()
