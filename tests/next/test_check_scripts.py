"""tools/setup.sh and tools/check.sh: the runner's two entry points (docs/conventions.md, Scripts).

check.sh is exercised on a copy of the repository that has this very test file removed, so a run
never recurses into itself; the copy is made with tempfile.mkdtemp() and removed afterwards.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SELF = Path(__file__).name
IGNORED = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache"}
FAILING_TEST = (
    "import unittest\n\n"
    "class ZZ(unittest.TestCase):\n"
    "    def test_zz_fail(self):\n"
    "        self.assertEqual(1, 2)\n"
)


def run(script: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "CI": "1"}
    return subprocess.run(["bash", f"tools/{script}"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


class CheckScripts(unittest.TestCase):
    def copy_repo(self) -> Path:
        """A copy of the repository without .git, node_modules and this test (no recursion)."""
        tmp = Path(tempfile.mkdtemp(prefix="hands-check-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dest = tmp / "repo"
        shutil.copytree(REPO, dest, symlinks=True,
                        ignore=lambda d, names: [n for n in names if n in IGNORED or n == SELF])
        self.assertFalse((dest / "tests" / "next" / SELF).exists())
        return dest

    def test_scripts_are_bash_scripts(self):
        for name in ("setup.sh", "check.sh"):
            head = (REPO / "tools" / name).read_text().splitlines()[0]
            self.assertRegex(head, r"^#!.*bash", name)

    def test_check_sh_passes_on_clean_tree(self):
        r = run("check.sh", self.copy_repo())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr
        self.assertIn("tests/next", out)
        for stage in ("compileall", "legacy pytest", "js tests"):
            self.assertIn(stage, out)

    def test_check_sh_fails_when_a_new_test_fails(self):
        repo = self.copy_repo()
        (repo / "tests" / "next" / "test_zz_fail.py").write_text(FAILING_TEST)
        r = run("check.sh", repo)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_zz_fail", r.stdout + r.stderr)

    def test_setup_sh_is_idempotent(self):
        repo = self.copy_repo()
        for i in (1, 2):
            r = run("setup.sh", repo)
            self.assertEqual(r.returncode, 0, f"run {i}: {r.stdout}{r.stderr}")


if __name__ == "__main__":
    unittest.main()
