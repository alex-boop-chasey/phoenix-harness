"""hands.setup: version picking, package lists, setup plans, scaffold verification, record, egress hosts."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hands import setup
from hands.setup import (FRAMEWORK_LABELS, LIB_IDS, AiKit, AiKitNotConfigured, InvalidOptions, LibsNeedFramework,
                         NoEligibleVersion, SetupOptions, egress_hosts, packages, pick_version, plan, record, verify)

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
VERSIONS = {p: "9.9.9" for p in ("astro", "tailwindcss", "@tailwindcss/vite", "@astrojs/check", "typescript", "prettier",
                                 "prettier-plugin-astro", "eslint", "eslint-plugin-astro", "@eslint/js", "typescript-eslint",
                                 "create-next-app", "shadcn", "gsap", "@gsap/react")}
VERSIONS["create-astro"] = "4.12.0"
KIT = AiKit(repo="https://example.com/kit.git", ref="v1.2.3", install_command="bash /tmp/aikit/install.sh")


def _pkg(d: Path, lint_exit: int) -> None:
    (d / "package.json").write_text(json.dumps({
        "name": "x", "private": True,
        "scripts": {"build": "node -e \"process.exit(0)\"",
                    "lint": f"node -e \"console.error('lint broke');process.exit({lint_exit})\"",
                    "typecheck": "node -e \"process.exit(0)\""}}))


class PickVersion(unittest.TestCase):
    def test_pick_version_ignores_meta_keys_and_prereleases(self):
        times = {"created": "2020-01-01T00:00:00.000Z", "modified": "2026-10-21T00:00:00.000Z",
                 "4.9.9": "2026-09-01T00:00:00.000Z", "5.0.0": "2026-10-01T00:00:00.000Z",
                 "5.2.0-beta.1": "2026-10-10T00:00:00.000Z", "5.1.0": "2026-10-20T00:00:00.000Z"}
        self.assertEqual(pick_version(times, NOW), "5.0.0")
        self.assertEqual(pick_version(times, datetime(2026, 10, 24, tzinfo=timezone.utc)), "5.1.0")
        self.assertEqual(pick_version(times, NOW, min_age_days=0), "5.1.0")

    def test_pick_version_compares_numerically_and_needs_tz(self):
        times = {"1.9.0": "2020-01-01T00:00:00.000Z", "1.10.0": "2020-01-02T00:00:00.000Z"}
        self.assertEqual(pick_version(times, NOW), "1.10.0")
        self.assertEqual(pick_version(times, datetime(2026, 10, 22)), "1.10.0")   # naive now = UTC

    def test_pick_version_max_major(self):
        times = {"6.0.2": "2026-01-01T00:00:00.000Z", "7.0.1": "2026-02-01T00:00:00.000Z", "6.1.0": "2026-03-01T00:00:00.000Z"}
        self.assertEqual(pick_version(times, NOW), "7.0.1")
        self.assertEqual(pick_version(times, NOW, max_major=6), "6.1.0")
        with self.assertRaises(NoEligibleVersion):
            pick_version(times, NOW, max_major=5)
        self.assertEqual(setup.VERSION_CAPS["typescript"], 6)

    def test_pick_version_none_eligible(self):
        with self.assertRaises(NoEligibleVersion):
            pick_version({"created": "2026-10-20T00:00:00.000Z", "1.0.0": "2026-10-21T00:00:00.000Z"}, NOW)
        with self.assertRaises(NoEligibleVersion):
            pick_version({"1.0.0-rc.1": "2020-01-01T00:00:00.000Z", "bad": "2020-01-01T00:00:00.000Z"}, NOW)
        with self.assertRaises(NoEligibleVersion):
            pick_version({"1.0.0": "not a date"}, NOW)


class Options(unittest.TestCase):
    def test_labels_and_libs(self):
        self.assertEqual(FRAMEWORK_LABELS[1], "None")
        self.assertEqual(FRAMEWORK_LABELS[5], "Next.js + Tailwind + AI kit")
        self.assertEqual(LIB_IDS, ("shadcn", "gsap"))

    def test_options_from_dict_validates(self):
        o = SetupOptions.from_dict({"framework": 2, "libs": ["gsap"]})
        self.assertEqual((o.framework, o.libs), (2, ["gsap"]))
        self.assertEqual(SetupOptions.from_dict({}).framework, 1)
        for bad in ({"framework": 9}, {"framework": "2"}, {"framework": 2, "libs": ["nope"]}, {"framework": 2, "libs": "gsap"}):
            with self.assertRaises(InvalidOptions):
                SetupOptions.from_dict(bad)

    def test_packages(self):
        a = packages(SetupOptions(2, ["shadcn", "gsap"]))
        for p in ("create-astro", "astro", "tailwindcss", "@tailwindcss/vite", "@astrojs/check", "typescript", "prettier",
                  "prettier-plugin-astro", "eslint", "eslint-plugin-astro", "@eslint/js", "typescript-eslint", "shadcn", "gsap"):
            self.assertIn(p, a)
        self.assertNotIn("@gsap/react", a)
        n = packages(SetupOptions(4, ["shadcn"]))
        self.assertEqual(n, ["create-next-app", "prettier", "shadcn"])
        self.assertEqual(packages(SetupOptions(5, ["gsap"])), ["create-next-app", "prettier", "gsap", "@gsap/react"])
        self.assertEqual(packages(SetupOptions(1, [])), [])


class Plan(unittest.TestCase):
    def test_plan_astro_with_gsap(self):
        p = plan(SetupOptions(2, ["gsap"]), VERSIONS, None)
        cmds = p.commands
        self.assertTrue(any("create-astro@4.12.0" in c for c in cmds))
        self.assertTrue(any("tailwindcss@9.9.9" in c and "@tailwindcss/vite@9.9.9" in c for c in cmds))
        self.assertTrue(any("gsap@9.9.9" in c for c in cmds))
        self.assertFalse(any("shadcn" in c or "git clone" in c or "@gsap/react" in c for c in cmds))
        last = cmds[-1]
        for s in ("pnpm run build", "pnpm run lint", "pnpm run typecheck"):
            self.assertIn(s, last)
        self.assertTrue(any("format:check" in c and "pnpm@12.11.2" in c for c in cmds))
        self.assertTrue(any("@import \"tailwindcss\"" in c for c in cmds))
        self.assertEqual(p.versions["create-astro"], "4.12.0")
        self.assertNotIn("create-next-app", p.versions)
        self.assertEqual(p.framework, 2)
        self.assertEqual(p.libs, ["gsap"])
        self.assertEqual(p.packages, packages(SetupOptions(2, ["gsap"])))

    def test_plan_next_with_shadcn(self):
        p = plan(SetupOptions(4, ["shadcn"]), VERSIONS, None)
        self.assertTrue(any("create-next-app@9.9.9" in c for c in p.commands))
        self.assertTrue(any("shadcn@9.9.9" in c and "init" in c for c in p.commands))
        self.assertFalse(any("astro" in c for c in p.commands))
        self.assertTrue(any("tsc --noEmit" in c for c in p.commands))
        self.assertTrue(any("--no-agents-md" in c for c in p.commands))
        self.assertTrue(any("src/app/layout.tsx" in c for c in p.commands))
        self.assertFalse(any("next/font" in c for c in p.commands))
        self.assertIn("pnpm run typecheck", p.commands[-1])

    def test_plan_ai_kit_required(self):
        with self.assertRaises(AiKitNotConfigured):
            plan(SetupOptions(3, []), VERSIONS, None)
        with self.assertRaises(AiKitNotConfigured):
            plan(SetupOptions(5, []), VERSIONS, None)
        p = plan(SetupOptions(3, []), VERSIONS, KIT)
        self.assertIn("git clone --depth 1 --branch v1.2.3 https://example.com/kit.git /tmp/aikit", p.commands)
        self.assertIn("bash /tmp/aikit/install.sh", p.commands)
        self.assertLess(p.commands.index("git clone --depth 1 --branch v1.2.3 https://example.com/kit.git /tmp/aikit"),
                        p.commands.index("bash /tmp/aikit/install.sh"))
        self.assertFalse(any("git clone" in c for c in plan(SetupOptions(2, []), VERSIONS, KIT).commands))

    def test_plan_none(self):
        p = plan(SetupOptions(1, []), VERSIONS, None)
        self.assertEqual(p.commands, [])
        self.assertEqual(p.versions, {})
        with self.assertRaises(LibsNeedFramework):
            plan(SetupOptions(1, ["gsap"]), VERSIONS, None)

    def test_plan_needs_every_version(self):
        with self.assertRaises(KeyError):
            plan(SetupOptions(2, []), {"create-astro": "4.12.0"}, None)

    def test_plan_rejects_shell_metacharacters_in_ai_kit(self):
        with self.assertRaises(InvalidOptions):
            plan(SetupOptions(3, []), VERSIONS, AiKit(repo="https://x.io/k.git; rm -rf /", ref="v1", install_command="true"))
        with self.assertRaises(InvalidOptions):
            plan(SetupOptions(3, []), VERSIONS, AiKit(repo="https://x.io/k.git", ref="v1 $(x)", install_command="true"))

    def test_plan_rejects_bad_versions(self):
        with self.assertRaises(InvalidOptions):
            plan(SetupOptions(2, []), {**VERSIONS, "astro": "9.9.9; echo"}, None)


class Verify(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-setup-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_verify_runs_three_scripts(self):
        _pkg(self.tmp, 1)
        v = verify(self.tmp)
        self.assertFalse(v.ok)
        self.assertEqual([s["name"] for s in v.steps], ["build", "lint", "typecheck"])
        self.assertEqual(v.steps[1]["name"], "lint")
        self.assertEqual(v.steps[1]["exit"], 1)
        self.assertIn("lint broke", v.steps[1]["log"])
        self.assertEqual(v.steps[0]["exit"], 0)
        _pkg(self.tmp, 0)
        self.assertTrue(verify(self.tmp).ok)

    def test_verify_with_a_custom_runner(self):
        calls = []

        def run_step(name, argv, cwd):
            calls.append((name, argv, cwd))
            return (0 if name != "typecheck" else 2), f"log of {name}"

        v = verify(self.tmp, run_step=run_step)
        self.assertFalse(v.ok)
        self.assertEqual([c[0] for c in calls], ["build", "lint", "typecheck"])
        self.assertEqual(calls[0][1], ["pnpm", "run", "build"])
        self.assertEqual(v.steps[2], {"name": "typecheck", "exit": 2, "log": "log of typecheck"})
        self.assertEqual(v.to_dict(), {"ok": False, "steps": v.steps})


class Record(unittest.TestCase):
    def test_record(self):
        p = plan(SetupOptions(2, ["gsap"]), VERSIONS, None)
        v = setup.VerifyResult(ok=True, steps=[{"name": "build", "exit": 0, "log": ""}])
        rec = record(p, v, NOW)
        self.assertEqual(rec["schema"], 1)
        self.assertEqual(rec["framework"], 2)
        self.assertEqual(rec["libs"], ["gsap"])
        self.assertEqual(rec["versions"]["gsap"], "9.9.9")
        self.assertEqual(rec["commands"], p.commands)
        self.assertEqual(rec["verified_at"], "2026-10-22T00:00:00Z")
        self.assertTrue(rec["ok"])
        self.assertEqual(set(rec), {"schema", "framework", "libs", "versions", "commands", "verified_at", "ok"})
        self.assertFalse(record(p, setup.VerifyResult(ok=False, steps=[]), NOW)["ok"])


class Egress(unittest.TestCase):
    def test_egress_hosts(self):
        self.assertEqual(egress_hosts(SetupOptions(1, []), None), ["registry.npmjs.org"])
        self.assertEqual(egress_hosts(SetupOptions(4, []), None), ["registry.npmjs.org"])
        self.assertEqual(egress_hosts(SetupOptions(4, ["shadcn"]), None), ["registry.npmjs.org", "ui.shadcn.com"])
        self.assertEqual(egress_hosts(SetupOptions(4, ["gsap"]), None), ["registry.npmjs.org"])
        astro = egress_hosts(SetupOptions(2, []), None)
        self.assertEqual(astro[0], "registry.npmjs.org")
        for h in ("github.com", "api.github.com", "codeload.github.com"):       # create-astro's template download
            self.assertIn(h, astro)
        self.assertEqual(egress_hosts(SetupOptions(5, []), KIT), ["registry.npmjs.org", "example.com"])
        self.assertEqual(egress_hosts(SetupOptions(5, []), AiKit("git@git.example.org:team/kit.git", "v1", "true")),
                         ["registry.npmjs.org", "git.example.org"])
        self.assertEqual(egress_hosts(SetupOptions(4, []), KIT), ["registry.npmjs.org"])    # kit host only for 3/5
        self.assertEqual(egress_hosts(SetupOptions(5, []), None), ["registry.npmjs.org"])


class StateHelpers(unittest.TestCase):
    def test_options_of_and_blocking_status(self):
        self.assertEqual(setup.options_of({}).framework, 1)
        self.assertEqual(setup.options_of({"setup": {"framework": 4, "libs": ["gsap"]}}).libs, ["gsap"])
        self.assertEqual(setup.blocking_status({}), "")
        self.assertEqual(setup.blocking_status({"setup": {"status": "done"}}), "")
        for s in ("pending", "running", "failed"):
            self.assertEqual(setup.blocking_status({"setup": {"status": s}}), s)

    def test_commit_subject(self):
        self.assertEqual(setup.commit_subject(SetupOptions(2, [])), "scaffold: Astro + Tailwind")
        self.assertEqual(setup.commit_subject(SetupOptions(4, ["shadcn", "gsap"])), "scaffold: Next.js + Tailwind + shadcn, gsap")

    def test_ai_kit_from_config(self):
        self.assertIsNone(setup.ai_kit_from({}))
        self.assertIsNone(setup.ai_kit_from({"repo": "x"}))
        k = setup.ai_kit_from({"repo": "https://x.io/k.git", "ref": "v1", "install_command": "bash /tmp/aikit/i.sh"})
        self.assertEqual(k, AiKit("https://x.io/k.git", "v1", "bash /tmp/aikit/i.sh"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-setup-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "hands.setup", *args], cwd=REPO, capture_output=True, text=True,
                              env={**os.environ, "CI": "1"}, timeout=120)

    def write(self, name: str, data) -> str:
        p = self.tmp / name
        p.write_text(json.dumps(data))
        return str(p)

    def test_pick(self):
        times = self.write("times.json", {"created": "2020-01-01T00:00:00.000Z", "5.0.0": "2026-10-01T00:00:00.000Z",
                                          "5.1.0": "2026-10-20T00:00:00.000Z"})
        r = self.run_cli("pick", "--times", times, "--now", "2026-10-22T00:00:00Z")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "5.0.0"))
        r = self.run_cli("pick", "--times", times, "--now", "2026-10-22T00:00:00Z", "--min-age-days", "1")
        self.assertEqual(r.stdout.strip(), "5.1.0")
        r = self.run_cli("pick", "--times", times, "--now", "2026-10-22T00:00:00Z", "--min-age-days", "1", "--max-major", "4")
        self.assertEqual(r.returncode, 2)
        r = self.run_cli("pick", "--times", self.write("none.json", {"1.0.0": "2026-10-21T00:00:00.000Z"}),
                         "--now", "2026-10-22T00:00:00Z")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no eligible version", r.stderr)

    def test_plan_and_errors(self):
        versions = self.write("versions.json", VERSIONS)
        r = self.run_cli("plan", "--options", self.write("o.json", {"framework": 3, "libs": []}), "--versions", versions, "--json")
        self.assertEqual(r.returncode, 3)
        self.assertIn("AI kit not configured", r.stderr)
        r = self.run_cli("plan", "--options", self.write("o2.json", {"framework": 1, "libs": ["gsap"]}), "--versions", versions)
        self.assertEqual(r.returncode, 3)
        self.assertIn("libraries need a framework", r.stderr)
        r = self.run_cli("plan", "--options", self.write("o3.json", {"framework": 7, "libs": []}), "--versions", versions)
        self.assertEqual(r.returncode, 1)
        kit = self.write("kit.json", {"repo": "https://example.com/kit.git", "ref": "v1.2.3", "install_command": "bash /tmp/aikit/install.sh"})
        r = self.run_cli("plan", "--options", self.write("o4.json", {"framework": 5, "libs": ["gsap"]}), "--versions", versions,
                         "--ai-kit", kit, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = json.loads(r.stdout)
        self.assertEqual(set(p), {"commands", "versions", "packages", "framework", "libs"})
        self.assertIn("git clone --depth 1 --branch v1.2.3 https://example.com/kit.git /tmp/aikit", p["commands"])
        r = self.run_cli("packages", "--options", self.write("o5.json", {"framework": 4, "libs": []}), "--json")
        self.assertEqual(json.loads(r.stdout), ["create-next-app", "prettier"])
        r = self.run_cli("packages", "--options", self.write("o5.json", {"framework": 4, "libs": []}))
        self.assertEqual(r.stdout.split(), ["create-next-app", "prettier"])

    def test_verify_and_record(self):
        _pkg(self.tmp, 1)
        r = self.run_cli("verify", str(self.tmp), "--json")
        self.assertEqual(r.returncode, 1)
        v = json.loads(r.stdout)
        self.assertFalse(v["ok"])
        self.assertEqual(v["steps"][1]["exit"], 1)
        r = self.run_cli("verify", str(self.tmp))
        self.assertEqual(r.returncode, 1)
        self.assertIn("lint", r.stdout)
        p = plan(SetupOptions(4, []), VERSIONS, None)
        r = self.run_cli("record", "--plan", self.write("plan.json", p.to_dict()), "--verify", self.write("v.json", v), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads(r.stdout)
        self.assertEqual((rec["schema"], rec["framework"], rec["ok"]), (1, 4, False))
        self.assertRegex(rec["verified_at"], r"^\d{4}-\d{2}-\d{2}T")


if __name__ == "__main__":
    unittest.main()


class RunOrchestration(unittest.TestCase):
    """`run` with the launcher replaced: containers are simulated on the host, git and state are real."""

    def setUp(self):
        from hands import docker
        from hands.config import Config
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-setup-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        spec, app, state = self.tmp / "spec", self.tmp / "app", self.tmp / "state"
        for d, files in ((spec, {"steps.json": "{}\n"}), (app, {"README.md": "# app\n", ".gitignore": "node_modules/\n"})):
            d.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
            for name, text in files.items():
                (d / name).write_text(text)
            subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)
        self.cfg = Config(name="proj", spec_dir=spec, app_dir=app, state_dir=state, image="hands:1", push=False)
        self.runs: list = []
        self.lint_exit = 0
        self.docker = docker
        for name, fake in (("run", self.fake_run), ("volume_create", lambda cfg, n: n), ("volume_rm", lambda cfg, n: None)):
            orig = getattr(docker, name)
            setattr(docker, name, fake)
            self.addCleanup(setattr, docker, name, orig)

    @staticmethod
    def _mount(r, dst: str) -> Path:
        return Path(next(m.src for m in r.mounts if m.dst == dst))

    def fake_run(self, cfg, r) -> int:
        self.docker.build_argv(cfg, r)                   # the real argv builder validates every mount
        self.runs.append(r)
        script = r.cmd[2]
        if "npm view" in script:
            out = self._mount(r, "/out")
            pkgs = [ln.split()[2].strip("'") for ln in script.splitlines() if ln.startswith("npm view")]
            for i, _ in enumerate(pkgs):
                (out / f"{i}.json").write_text(json.dumps({"created": "2020-01-01T00:00:00.000Z",
                                                           "1.2.3": "2026-01-01T00:00:00.000Z",
                                                           "9.0.0": "2026-10-21T00:00:00.000Z"}))
            return 0
        if script.startswith("set -euo pipefail\ncd /workspace"):
            work = self._mount(r, "/workspace")
            (work / "package.json").write_text('{"name": "app", "scripts": {"build": "true", "lint": "true", "typecheck": "true"}}\n')
            (work / "src").mkdir()
            (work / "src" / "index.ts").write_text("export const x = 1;\n")
            (work / "node_modules").mkdir()
            (work / "node_modules" / "junk.js").write_text("ignored\n")
            return 0
        if "exec pnpm run" in script:
            name = script.split()[-1]
            r.stdout.write_text(f"ran {name}\n")
            return self.lint_exit if name == "lint" else 0
        if script == self.workspace.EXPORT_SCRIPT:
            import tarfile
            work, out = self._mount(r, "/workspace"), self._mount(r, "/out")
            with tarfile.open(out / "tree.tar", "w") as tf:
                for p in sorted(work.rglob("*")):
                    rel = p.relative_to(work)
                    if p.is_file() and rel.parts[0] not in (".git", "node_modules"):
                        tf.add(p, arcname=str(rel))
            return 0
        raise AssertionError(f"unexpected container: {r.name} {script[:60]!r}")

    @property
    def workspace(self):
        from hands import workspace
        return workspace

    def state(self):
        from hands.state import State
        return State(self.cfg.state_dir)

    def run_setup(self, **kw) -> int:
        """setup.run with the runner's log lines (printed as well as written to runner.log) kept out of the test output."""
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            return setup.run(self.cfg, **kw)

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def test_run_scaffolds_commits_and_records(self):
        st = self.state()
        st.data["setup"] = {"framework": 4, "libs": ["gsap"], "status": "pending"}
        st.save()
        self.assertEqual(self.run_setup(), 0)
        self.assertEqual(self.git(self.cfg.app_dir, "log", "-1", "--format=%s"), "scaffold: Next.js + Tailwind + gsap")
        self.assertIn("create-next-app@1.2.3", self.git(self.cfg.app_dir, "log", "-1", "--format=%b"))
        self.assertEqual(self.git(self.cfg.app_dir, "status", "--porcelain"), "")
        self.assertTrue((self.cfg.app_dir / "src" / "index.ts").is_file())
        self.assertFalse((self.cfg.app_dir / "node_modules").exists())
        rec = json.loads((self.cfg.spec_dir / "refs" / "setup.json").read_text())
        self.assertEqual((rec["schema"], rec["framework"], rec["libs"], rec["ok"]), (1, 4, ["gsap"], True))
        self.assertEqual(rec["versions"], {"create-next-app": "1.2.3", "prettier": "1.2.3", "gsap": "1.2.3", "@gsap/react": "1.2.3"})
        self.assertEqual(self.git(self.cfg.spec_dir, "log", "-1", "--format=%s"), "setup: scaffold")
        s = self.state().data["setup"]
        self.assertEqual((s["status"], s["framework"], s["libs"]), ("done", 4, ["gsap"]))
        self.assertEqual(s["record"], rec)
        roles = [(r.role, r.network) for r in self.runs]
        self.assertEqual(roles, [("setup", True), ("setup", True), ("setup", True), ("setup", True), ("setup", True),
                                 ("export", False)])
        for r in self.runs[:5]:
            self.assertFalse(r.env_files, "the setup container gets no token and no project env")
            self.assertEqual(r.env["CI"], "1")
        self.assertEqual([r.cmd[2].split()[-1] for r in self.runs[2:5]], ["build", "lint", "typecheck"])
        self.assertEqual(self._mount(self.runs[1], "/etc/hands/allowed-domains").read_text(), "registry.npmjs.org\n")
        self.assertFalse((self.cfg.state_dir / "setup" / "work").exists())
        self.assertTrue((self.cfg.state_dir / "setup" / "verify-lint.log").is_file())

    def test_run_failed_verify_commits_nothing(self):
        st = self.state()
        st.data["setup"] = {"framework": 2, "libs": [], "status": "pending"}
        st.save()
        self.lint_exit = 1
        base = self.git(self.cfg.app_dir, "rev-parse", "HEAD")
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(self.git(self.cfg.app_dir, "rev-parse", "HEAD"), base)
        self.assertEqual(self.git(self.cfg.app_dir, "status", "--porcelain"), "")
        self.assertFalse((self.cfg.spec_dir / "refs" / "setup.json").exists())
        s = self.state().data["setup"]
        self.assertEqual(s["status"], "failed")
        self.assertIn("lint exit 1", s["error"])
        self.assertFalse(s["record"]["ok"])
        self.assertEqual(setup.blocking_status(self.state().data), "failed")
        self.assertEqual([r.role for r in self.runs], ["setup"] * 5)

    def test_run_none_and_errors(self):
        self.assertEqual(self.run_setup(), 0)
        self.assertEqual(self.state().data["setup"]["status"], "none")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.run_setup(framework=1, libs=["gsap"]), 3)
        self.assertEqual(self.state().data["setup"]["status"], "failed")
        self.assertEqual(self.run_setup(framework=3), 3)          # no AI kit configured
        self.assertEqual(self.state().data["setup"]["error"], "AI kit not configured")
        self.assertEqual(self.run_setup(framework=4, libs=["nope"]), 1)
        self.assertEqual(self.runs, [])

    def test_run_with_ai_kit_from_project_config(self):
        self.cfg.ai_kit = {"repo": "https://kits.example.org/kit.git", "ref": "v2", "install_command": "bash /tmp/aikit/install.sh"}
        self.assertEqual(self.run_setup(framework=5, libs=[]), 0)
        self.assertEqual(self.git(self.cfg.app_dir, "log", "-1", "--format=%s"), "scaffold: Next.js + Tailwind + AI kit")
        rec = self.state().data["setup"]["record"]
        self.assertIn("git clone --depth 1 --branch v2 https://kits.example.org/kit.git /tmp/aikit", rec["commands"])
        self.assertEqual(self._mount(self.runs[0], "/etc/hands/allowed-domains").read_text(),
                         "registry.npmjs.org\nkits.example.org\n")

    def test_run_refuses_while_the_runner_is_alive(self):
        from hands.state import Lock
        self.cfg.state_dir.mkdir()
        with Lock(self.cfg.state_dir / "runner.lock"):
            self.assertEqual(self.run_setup(framework=4), 1)
        self.assertEqual(self.runs, [])
