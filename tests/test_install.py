"""install.sh must never create a project config (it used to seed ~/.config/hands/projects/ra.toml).

Runs the real script in a throwaway HOME with stub python3/docker/crontab/git/tmux on PATH (the stubs
accept anything and exit 0), so the copy, launcher and cron steps run for real but nothing is
installed, tested or started on the host.
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401

STUB = "#!/bin/sh\nexit 0\n"
CRONTAB = "#!/bin/sh\n# fake crontab: - stores stdin, -l prints it\nF=\"$HOME/crontab.txt\"\ncase \"$1\" in -) cat > \"$F\";; -l) [ -f \"$F\" ] && cat \"$F\";; esac\nexit 0\n"


@unittest.skipUnless((ROOT / "install.sh").is_file(),
                     "install.sh is not next to tests/ (installed copy has no install.sh); run from the source tree")
class TestInstallScript(unittest.TestCase):
    def run_install(self, python3=STUB):
        home = Path(tempfile.mkdtemp(prefix="hands-install-"))
        bins = home / "stubs"
        bins.mkdir()
        for name in ("python3", "docker", "crontab", "git", "tmux", "sleep"):
            p = bins / name
            p.write_text(CRONTAB if name == "crontab" else python3 if name == "python3" else STUB)
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        env = {
            "HOME": str(home),
            "PATH": f"{bins}:/usr/bin:/bin",
            "TMPDIR": str(home),
            "LANG": "C.UTF-8",
        }
        r = subprocess.run(["bash", str(ROOT / "install.sh")], cwd=str(home), env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        return home, r

    def test_install_writes_no_project_config(self):
        home, r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("installed runner", r.stdout)
        projects = home / ".config" / "hands" / "projects"
        written = sorted(str(p.relative_to(projects)) for p in projects.rglob("*")) if projects.exists() else []
        self.assertEqual(written, [], f"install.sh wrote under ~/.config/hands/projects/: {written}")
        self.assertNotIn("ra.toml", r.stdout + r.stderr)
        # what it may seed: the New project defaults, and nothing else under ~/.config/hands
        cfg = home / ".config" / "hands"
        self.assertEqual(sorted(p.name for p in cfg.iterdir()), ["new-project.toml", "projects"])
        self.assertTrue((home / ".local" / "bin" / "hands").exists())
        self.assertFalse((home / "projects").exists())

    def test_failing_self_test_aborts_before_copying(self):
        failing = ("#!/bin/sh\ncase \"$*\" in *unittest*) echo 'FAIL: test_x (test_y.T.test_x)';"
                   " echo 'FAILED (failures=1)'; exit 1;; esac\nexit 0\n")
        home, r = self.run_install(python3=failing)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("FAIL: test_x", r.stdout)
        self.assertIn("self-test FAILED: nothing installed", r.stdout)
        self.assertNotIn("installed runner", r.stdout)
        self.assertFalse((home / ".local").exists(), "install.sh copied or wrote files despite a failing self-test")
        self.assertFalse((home / ".config").exists())


if __name__ == "__main__":
    unittest.main()
