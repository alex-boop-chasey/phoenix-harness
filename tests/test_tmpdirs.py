"""The suite leaves no temp directories behind (tests/tmpdirs.py)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402


class TestTmpDirs(unittest.TestCase):
    def test_dirs_are_removed_after_each_test_even_on_failure(self):
        made = []

        class Inner(unittest.TestCase):
            def test_fail(self):
                made.append(tempfile.mkdtemp(prefix="hands-inner-"))
                (Path(made[-1]) / "f").write_text("x")
                self.fail("on purpose")

            def test_pass(self):
                made.append(tempfile.mkdtemp(prefix="hands-inner-"))
                fd, f = tempfile.mkstemp(prefix="hands-inner-")
                os.close(fd)
                made.append(f)

        res = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(res)
        self.assertEqual(len(res.failures), 1)
        self.assertEqual(len(made), 3)
        for p in made:
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(tmpdirs.pending(), [])

    def test_nothing_left_by_earlier_tests(self):
        # every test's temp dirs and files are swept when it ends, so whichever tests ran before this
        # one in this process, nothing made since the process started may remain in the temp root
        root = Path(tempfile.gettempdir())
        left = sorted(p.name for p in root.iterdir()
                      if p.name.startswith(("hands-", "tmp")) and p.lstat().st_mtime >= tmpdirs.START)
        self.assertEqual(left, [])
        self.assertEqual(tmpdirs.pending(), [])


if __name__ == "__main__":
    unittest.main()
