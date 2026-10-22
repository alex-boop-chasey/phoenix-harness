"""hands.hooks: the coder's Claude Code settings fragment (format/lint hook) and its read-only mounts."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hands import docker, hooks
from hands.config import Config
from hands.docker import Run

REPO = Path(__file__).resolve().parents[2]
EXPECTED = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": "node /hands-hooks/format-on-write.mjs", "timeout": 120}],
            }
        ]
    }
}


class Settings(unittest.TestCase):
    def test_coder_settings_shape(self):
        s = hooks.coder_settings()
        self.assertEqual(s, EXPECTED)
        (entry,) = s["hooks"]["PostToolUse"]
        self.assertEqual(entry["matcher"], "Edit|Write")
        (h,) = entry["hooks"]
        self.assertEqual(h["command"], hooks.HOOK_COMMAND)
        self.assertEqual(h["timeout"], 120)
        self.assertEqual(hooks.settings_for("coder"), EXPECTED)

    def test_coder_settings_is_a_fresh_copy(self):
        hooks.coder_settings()["hooks"]["PostToolUse"].clear()
        self.assertEqual(hooks.coder_settings(), EXPECTED)

    def test_other_roles_get_no_hooks(self):
        for role in ("evaluator", "eval", "planner", "analyst", "accept", "export", "probe", ""):
            self.assertEqual(hooks.settings_for(role), {}, role)

    def test_mount_args_coder_only(self):
        args = hooks.mount_args("coder", Path("/x/hooks"), Path("/y/settings-coder.json"))
        self.assertEqual(args, ["-v", "/x/hooks:/hands-hooks:ro",
                                "-v", "/y/settings-coder.json:/home/node/.claude/settings.json:ro"])
        for role in ("evaluator", "eval", "planner", "analyst", "accept"):
            self.assertEqual(hooks.mount_args(role, Path("/x/hooks"), Path("/y/s.json")), [], role)

    def test_mount_args_accept_strings(self):
        self.assertEqual(hooks.mount_args("coder", "/x/hooks", "/y/s.json")[1], "/x/hooks:/hands-hooks:ro")

    def test_mount_args_reject_colon_and_comma(self):
        for bad in ("/x/a:b", "/x/a,b"):
            with self.assertRaises(ValueError):
                hooks.mount_args("coder", Path(bad), Path("/y/s.json"))
            with self.assertRaises(ValueError):
                hooks.mount_args("coder", Path("/x/hooks"), Path(bad))

    def test_write_settings(self):
        tmp = Path(tempfile.mkdtemp(prefix="hands-hooks-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        dest = tmp / "state" / "settings-coder.json"
        self.assertEqual(hooks.write_settings("coder", dest), dest)
        self.assertEqual(json.loads(dest.read_text()), EXPECTED)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), oct(0o644))
        other = hooks.write_settings("planner", tmp / "settings-planner.json")
        self.assertEqual(json.loads(other.read_text()), {})
        # rewriting replaces the content atomically (no leftover temp files)
        dest.write_text("garbage")
        hooks.write_settings("coder", dest)
        self.assertEqual(json.loads(dest.read_text()), EXPECTED)
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["settings-coder.json"])

    def test_hooks_dir_is_the_checkout_hooks_directory(self):
        d = hooks.hooks_dir()
        self.assertEqual(d, REPO / "hooks")
        self.assertTrue((d / "format-on-write.mjs").is_file())


class Cli(unittest.TestCase):
    def py(self, *args):
        return subprocess.run([sys.executable, "-m", "hands.hooks", *args], cwd=REPO,
                              capture_output=True, text=True, timeout=60)

    def test_render(self):
        r = self.py("render", "--role", "coder", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), EXPECTED)
        r = self.py("render", "--role", "planner", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {})
        r = self.py("render", "--role", "coder")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), EXPECTED)

    def test_mount_args(self):
        r = self.py("mount-args", "--role", "coder", "--hooks-dir", "/x/hooks",
                    "--settings-file", "/y/settings-coder.json", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), ["-v", "/x/hooks:/hands-hooks:ro",
                                                "-v", "/y/settings-coder.json:/home/node/.claude/settings.json:ro"])
        r = self.py("mount-args", "--role", "eval", "--hooks-dir", "/x/hooks", "--settings-file", "/y/s.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [])

    def test_bad_input_exits_2(self):
        self.assertEqual(self.py("mount-args", "--role", "coder", "--hooks-dir", "/x:y",
                                 "--settings-file", "/y/s.json").returncode, 2)
        self.assertNotEqual(self.py("render").returncode, 0)
        self.assertNotEqual(self.py().returncode, 0)

    def test_imports_cleanly_on_its_own(self):
        r = subprocess.run([sys.executable, "-c", "import hands.hooks"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class LauncherArgs(unittest.TestCase):
    def test_extra_args_land_before_the_image(self):
        tmp = Path(tempfile.mkdtemp(prefix="hands-hooks-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cfg = Config(name="t", spec_dir=tmp, app_dir=tmp, state_dir=tmp, image="hands:test", skills_dir=None)
        extra = hooks.mount_args("coder", Path("/x/hooks"), Path("/y/settings-coder.json"))
        argv = docker.build_argv(cfg, Run(name="n", role="coder", cmd=["claude"], extra_args=extra))
        i = argv.index("hands:test")
        self.assertEqual(argv[i - 4:i], extra)
        self.assertEqual(argv[i + 1:], ["claude"])
        plain = docker.build_argv(cfg, Run(name="n", role="eval", cmd=["claude"]))
        self.assertNotIn("-v", plain)


if __name__ == "__main__":
    unittest.main()
