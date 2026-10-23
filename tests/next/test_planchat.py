"""hands.planchat: primer, sandboxed launch spec, brief hash/status, hash-pinned approval."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hands import planchat
from hands.planchat import BriefIncomplete, HashMismatch, Paths, PlanContext, PlanSession, TokenMissing

REPO = Path(__file__).resolve().parents[2]
UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

CTX_EXISTING = {
    "slug": "demo", "kind": "existing", "description": "Existing shop app; wants a loyalty programme.",
    "style_refs": [{"url": "https://example.com/", "report_path": "/spec/refs/style-refs/01/report.md"}],
    "setup": {"framework": 2, "libs": ["gsap"], "record": None},
    "spec_dir": "/spec", "app_dir": "/app", "brief_dir": "/brief", "skills_dir": "/home/node/.claude/skills",
}
CTX_NEW = dict(CTX_EXISTING, kind="new", description="A bakery site with online ordering.", app_dir=None)

BRIEF = "\n".join([
    "# Brief: demo", "", "## Idea", "A bakery site.", "", "## Must have", "- Menu", "", "## Nice to have", "- News", "",
    "## Out of scope", "- Delivery", "", "## Style and motion", "Warm.", "", "## Setup", "Astro + Tailwind.", "",
    "## Open decisions", "- Payments", ""])
COVERAGE = "| Requirement | Where |\n|---|---|\n| Menu | Must have |\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class TmpDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-planchat-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def mkdir(self, name: str) -> Path:
        d = self.tmp / name
        d.mkdir(parents=True)
        return d

    def brief_dir(self, coverage: bool = True) -> Path:
        d = self.mkdir("brief")
        (d / "brief.md").write_text(BRIEF, encoding="utf-8")
        if coverage:
            (d / "coverage.md").write_text(COVERAGE, encoding="utf-8")
        return d

    def spec_repo(self) -> Path:
        spec = self.mkdir("spec")
        git(spec, "init", "-q")
        git(spec, "config", "user.email", "t@example.com")
        git(spec, "config", "user.name", "tester")
        (spec / "README.md").write_text("spec\n")
        git(spec, "add", ".")
        git(spec, "commit", "-q", "-m", "init")
        return spec

    def paths(self, secrets_text: str = "ANTHROPIC_API_KEY=abc\nGITHUB_TOKEN=ghp_x\nOTHER=zzz\n") -> Paths:
        secrets = self.tmp / "secrets.env"
        secrets.write_text(secrets_text)
        return Paths(spec_dir=self.mkdir("spec"), app_dir=self.mkdir("app"), brief_dir=self.mkdir("brief"),
                     home_dir=self.mkdir("home"), skills_dir=self.mkdir("skills"), env_dir=self.mkdir("env"),
                     secrets_file=secrets)


class Primer(unittest.TestCase):
    def test_primer_mentions_context_and_rules(self):
        text = planchat.primer(PlanContext.from_json(CTX_EXISTING))
        for phrase in ["demo", "Existing shop app; wants a loyalty programme.", "https://example.com/",
                       "/spec/refs/style-refs/01/report.md", "Astro + Tailwind", "gsap", "/spec", "/app",
                       "planning interviewer", "brainstorming", "full Q&A mode", "read-only",
                       "never write the spec repo", "never run git push", "/brief/brief.md", "/brief/coverage.md",
                       "# Brief: demo", *planchat.BRIEF_HEADINGS]:
            self.assertIn(phrase, text)

    def test_primer_for_a_new_project_never_mentions_app(self):
        text = planchat.primer(PlanContext.from_json(CTX_NEW))
        self.assertNotIn("/app", text)
        self.assertIn("/spec", text)
        self.assertIn("A bakery site with online ordering.", text)

    def test_primer_without_spec_setup_or_refs(self):
        ctx = PlanContext.from_json(dict(CTX_NEW, spec_dir=None, setup=None, style_refs=[], description=""))
        text = planchat.primer(ctx)
        self.assertNotIn("/spec", text)
        self.assertNotIn("/app", text)
        self.assertIn("/brief/brief.md", text)
        self.assertIn("planning interviewer", text)

    def test_from_json_validates(self):
        for bad in [dict(CTX_NEW, kind="other"), dict(CTX_NEW, slug=""), dict(CTX_NEW, style_refs="x"),
                    dict(CTX_NEW, setup={"framework": 9}), "nope"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    PlanContext.from_json(bad)
        ctx = PlanContext.from_json({"slug": "demo", "kind": "new"})
        self.assertEqual(ctx.brief_dir, "/brief")
        self.assertIsNone(ctx.setup)
        self.assertEqual(ctx.style_refs, [])


class LaunchSpec(TmpDirs):
    def test_launch_spec_mounts_and_single_env_var(self):
        p = self.paths()
        spec = planchat.launch_spec(PlanContext.from_json(CTX_EXISTING), PlanSession(None, "none", None), p,
                                    ["--network", "hands-fw", "--dns", "10.0.0.2"])
        argv = spec["argv"]
        self.assertEqual(argv[:2], ["docker", "run"])
        i = argv.index("--network")
        self.assertEqual(argv[i:i + 4], ["--network", "hands-fw", "--dns", "10.0.0.2"])
        for mount in [f"{p.spec_dir}:/spec:ro", f"{p.app_dir}:/app:ro", f"{p.brief_dir}:/brief:rw",
                      f"{p.home_dir}:/home/node/.claude:rw", f"{p.skills_dir}:/home/node/.claude/skills:ro"]:
            self.assertIn(mount, argv)
            self.assertEqual(argv[argv.index(mount) - 1], "-v")
        env_file = Path(argv[argv.index("--env-file") + 1])
        self.assertEqual(env_file.parent, p.env_dir)
        self.assertEqual(env_file.read_text(), "ANTHROPIC_API_KEY=abc\n")
        self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)
        text = json.dumps(spec)
        for forbidden in ["ghp_x", "zzz", "project.env", "/run/hands", "GITHUB"]:
            self.assertNotIn(forbidden, text)
        self.assertEqual(spec["session"], {"project": "demo", "role": "plan-chat", "hidden": True})
        self.assertEqual(json.loads(text), spec)

    def test_first_start_uses_session_id_and_resume_after(self):
        p = self.paths()
        ctx = PlanContext.from_json(CTX_NEW)
        first = planchat.launch_spec(ctx, PlanSession(None, "none", None), p, ["--network", "hands-fw"])
        argv = first["argv"]
        c = argv.index("claude")
        self.assertEqual(argv[c + 1], "--session-id")
        self.assertRegex(argv[c + 2], UUID4_RE)
        self.assertEqual(first["session_id"], argv[c + 2])
        self.assertEqual(argv[-1], planchat.primer(ctx))
        self.assertNotIn(f"{p.app_dir}:/app:ro", argv)

        sid = "7d7e5f1a-1234-4abc-9def-0123456789ab"
        again = planchat.launch_spec(ctx, PlanSession(sid, "open", None), p, ["--network", "hands-fw"])
        argv = again["argv"]
        c = argv.index("claude")
        self.assertEqual(argv[c:], ["claude", "--resume", sid])
        self.assertNotIn("--session-id", argv)
        self.assertEqual(again["session_id"], sid)

    def test_launch_spec_rejects_a_bad_stored_session_id(self):
        p = self.paths()
        with self.assertRaises(ValueError):
            planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession("../x", "open", None), p, [])

    def test_launch_spec_without_token_raises_and_writes_nothing(self):
        p = self.paths()
        for text in ["GITHUB_TOKEN=ghp_x\n", "ANTHROPIC_API_KEY=\n", "ANTHROPIC_API_KEY=   \n", ""]:
            with self.subTest(text=text):
                p.secrets_file.write_text(text)
                with self.assertRaisesRegex(TokenMissing, "ANTHROPIC_API_KEY"):
                    planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession(None, "none", None), p, [])
                self.assertEqual(os.listdir(p.env_dir), [])
        p.secrets_file.unlink()
        with self.assertRaises(TokenMissing):
            planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession(None, "none", None), p, [])

    def test_launch_spec_accepts_the_build_wide_oauth_token(self):
        """claude.env holds CLAUDE_CODE_OAUTH_TOKEN on this host; the chat must start from that too."""
        p = self.paths("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\nGITHUB_TOKEN=ghp_x\n")
        spec = planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession(None, "none", None), p, [])
        argv = spec["argv"]
        env_file = Path(argv[argv.index("--env-file") + 1])
        self.assertEqual(env_file.read_text(), "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\n")
        self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)
        self.assertEqual(env_file.parent, p.env_dir)
        self.assertNotIn("ghp_x", json.dumps(spec))
        self.assertEqual(argv.count("--env-file"), 1)

    def test_launch_spec_prefers_the_api_key_when_both_are_present(self):
        p = self.paths("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\nANTHROPIC_API_KEY=abc\n")
        spec = planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession(None, "none", None), p, [])
        argv = spec["argv"]
        env_file = Path(argv[argv.index("--env-file") + 1])
        self.assertEqual(env_file.read_text(), "ANTHROPIC_API_KEY=abc\n")

    def test_token_missing_names_both_keys(self):
        p = self.paths("GITHUB_TOKEN=ghp_x\n")
        with self.assertRaisesRegex(TokenMissing, "CLAUDE_CODE_OAUTH_TOKEN"):
            planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession(None, "none", None), p, [])
        self.assertEqual(os.listdir(p.env_dir), [])

    def test_build_token_refuses_blank_nul_and_unknown_keys(self):
        p = self.paths()
        for text in ["", "GITHUB_TOKEN=ghp_x\n", "ANTHROPIC_API_KEY=\n", "ANTHROPIC_API_KEY=   \n",
                     "CLAUDE_CODE_OAUTH_TOKEN=\n", "CLAUDE_CODE_OAUTH_TOKEN=  \n",
                     "ANTHROPIC_API_KEY=a\0b\n", "CLAUDE_CODE_OAUTH_TOKEN=a\0b\n",
                     "ANTHROPIC_API_KEY=\0\n"]:
            with self.subTest(text=text):
                p.secrets_file.write_text(text)
                with self.assertRaises(TokenMissing):
                    planchat.build_token(p.secrets_file)

    def test_build_token_returns_the_key_it_found(self):
        p = self.paths()
        for text, want in [("ANTHROPIC_API_KEY= abc \n", ("ANTHROPIC_API_KEY", "abc")),
                           ("CLAUDE_CODE_OAUTH_TOKEN=tok\n", ("CLAUDE_CODE_OAUTH_TOKEN", "tok"))]:
            with self.subTest(text=text):
                p.secrets_file.write_text(text)
                self.assertEqual(planchat.build_token(p.secrets_file), want)

    def test_a_trailing_junk_line_does_not_extend_the_token(self):
        """read_secrets takes KEY=value lines only, so the env file stays a single variable."""
        p = self.paths("ANTHROPIC_API_KEY=abc\nnot-a-key-line\n")
        spec = planchat.launch_spec(PlanContext.from_json(CTX_NEW), PlanSession(None, "none", None), p, [])
        argv = spec["argv"]
        env_file = Path(argv[argv.index("--env-file") + 1])
        self.assertEqual(env_file.read_text(), "ANTHROPIC_API_KEY=abc\n")

    def test_launch_spec_new_project_has_no_app_mount(self):
        p = Paths(spec_dir=None, app_dir=None, brief_dir=self.mkdir("brief"), home_dir=self.mkdir("home"),
                  skills_dir=self.mkdir("skills"), env_dir=self.mkdir("env"), secrets_file=self.tmp / "s.env")
        p.secrets_file.write_text("ANTHROPIC_API_KEY=abc\n")
        spec = planchat.launch_spec(PlanContext.from_json(dict(CTX_NEW, spec_dir=None)), PlanSession(None, "none", None), p, [])
        joined = "\n".join(spec["argv"])
        self.assertNotIn(":/app:", joined)
        self.assertNotIn(":/spec:", joined)
        self.assertIn(f"{p.brief_dir}:/brief:rw", spec["argv"])

    def test_json_round_trip_of_inputs(self):
        p = self.paths()
        paths = Paths.from_json(json.loads(json.dumps({k: (str(v) if v else None) for k, v in p.__dict__.items()})))
        self.assertEqual(paths.secrets_file, p.secrets_file)
        self.assertEqual(paths.app_dir, p.app_dir)
        state = PlanSession.from_json({"session_id": None, "status": "none", "brief_sha256": None})
        self.assertEqual(state, PlanSession(None, "none", None))
        self.assertEqual(PlanSession.from_json({}), PlanSession(None, "none", None))
        with self.assertRaises(ValueError):
            PlanSession.from_json({"status": "bogus"})


class HashAndStatus(TmpDirs):
    def test_brief_hash_is_sha256_of_brief_md(self):
        d = self.brief_dir()
        self.assertEqual(planchat.brief_hash(d), hashlib.sha256(BRIEF.encode()).hexdigest())

    def test_brief_hash_missing_file(self):
        with self.assertRaises(BriefIncomplete):
            planchat.brief_hash(self.mkdir("empty"))

    def test_status(self):
        self.assertEqual(planchat.status(self.brief_dir()), "brief_ready")
        self.assertEqual(planchat.status(self.mkdir("empty")), "open")
        self.assertEqual(planchat.status(self.tmp / "nope"), "open")
        only_brief = self.mkdir("half")
        (only_brief / "brief.md").write_text(BRIEF)
        self.assertEqual(planchat.status(only_brief), "open")

    def test_plan_session_record(self):
        ready, empty = self.brief_dir(), self.mkdir("empty")
        self.assertEqual(planchat.plan_session_record(None, empty),
                         {"session_id": None, "status": "none", "brief_sha256": None, "updated": ""})
        self.assertEqual(planchat.plan_session_record("id", empty, updated="t")["status"], "open")
        self.assertEqual(planchat.plan_session_record("id", ready)["status"], "brief_ready")
        rec = planchat.plan_session_record("id", ready, brief_sha256="0" * 64, updated="t")
        self.assertEqual(rec, {"session_id": "id", "status": "approved", "brief_sha256": "0" * 64, "updated": "t"})
        self.assertEqual(PlanSession.from_json(rec), PlanSession("id", "approved", "0" * 64))


class Approve(TmpDirs):
    def test_approve_requires_matching_hash(self):
        spec, brief = self.spec_repo(), self.brief_dir()
        with self.assertRaisesRegex(HashMismatch, "hash mismatch"):
            planchat.approve(brief, spec, "deadbeef" * 8)
        self.assertFalse((spec / "brief.md").exists())
        self.assertEqual(git(spec, "log", "--oneline").count("\n"), 0)

        sha = planchat.brief_hash(brief)
        result = planchat.approve(brief, spec, sha)
        self.assertEqual((spec / "brief.md").read_text(), BRIEF)
        self.assertEqual((spec / "refs" / "coverage.md").read_text(), COVERAGE)
        self.assertEqual((spec / "brief.sha256").read_text(), sha + "\n")
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), f"brief: approved {sha[:12]}")
        self.assertEqual(git(spec, "status", "--porcelain"), "")
        self.assertEqual(result["sha256"], sha)
        self.assertEqual(result["brief_path"], "brief.md")
        self.assertEqual(result["coverage_path"], "refs/coverage.md")
        self.assertEqual(result["commit"], git(spec, "rev-parse", "HEAD"))
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_approve_change_round_paths(self):
        spec, brief = self.spec_repo(), self.brief_dir()
        sha = planchat.brief_hash(brief)
        result = planchat.approve(brief, spec, sha, round=2)
        self.assertEqual((spec / "briefs" / "change-2.md").read_text(), BRIEF)
        self.assertEqual((spec / "refs" / "coverage-change-2.md").read_text(), COVERAGE)
        self.assertEqual((spec / "brief.sha256").read_text(), sha + "\n")
        self.assertEqual(result["brief_path"], "briefs/change-2.md")
        self.assertEqual(result["coverage_path"], "refs/coverage-change-2.md")
        self.assertEqual(git(spec, "status", "--porcelain"), "")
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), f"brief: approved {sha[:12]}")
        for bad in [0, -1, True, "2"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    planchat.approve(brief, spec, sha, round=bad)

    def test_approve_refuses_missing_coverage(self):
        spec, brief = self.spec_repo(), self.brief_dir(coverage=False)
        with self.assertRaisesRegex(BriefIncomplete, "coverage.md missing"):
            planchat.approve(brief, spec, planchat.brief_hash(brief))
        self.assertFalse((spec / "brief.md").exists())
        with self.assertRaisesRegex(BriefIncomplete, "brief.md missing"):
            planchat.approve(self.mkdir("empty"), spec, "0" * 64)


class Cli(TmpDirs):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "hands.planchat", *args], capture_output=True, text=True, cwd=REPO)

    def test_primer_and_status_and_hash(self):
        ctx = self.tmp / "ctx.json"
        ctx.write_text(json.dumps(CTX_NEW))
        r = self.run_cli("primer", "--ctx", str(ctx))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("planning interviewer", r.stdout)
        self.assertNotIn("/app", r.stdout)
        brief = self.brief_dir()
        self.assertEqual(self.run_cli("hash", "--brief-dir", str(brief)).stdout.strip(), planchat.brief_hash(brief))
        self.assertEqual(self.run_cli("status", "--brief-dir", str(brief)).stdout.strip(), "brief_ready")
        self.assertEqual(self.run_cli("status", "--brief-dir", str(self.mkdir("empty"))).stdout.strip(), "open")
        r = self.run_cli("hash", "--brief-dir", str(self.tmp / "none"))
        self.assertEqual(r.returncode, 1)

    def test_launch_spec_json(self):
        p = self.paths()
        ctx, state, paths = self.tmp / "ctx.json", self.tmp / "state.json", self.tmp / "paths.json"
        ctx.write_text(json.dumps(CTX_EXISTING))
        state.write_text(json.dumps({"session_id": None, "status": "none", "brief_sha256": None}))
        paths.write_text(json.dumps({k: (str(v) if v else None) for k, v in p.__dict__.items()}))
        r = self.run_cli("launch-spec", "--ctx", str(ctx), "--state", str(state), "--paths", str(paths),
                         "--network-args", '["--network","hands-fw"]', "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        spec = json.loads(r.stdout)
        self.assertEqual(spec["session"]["role"], "plan-chat")
        self.assertIn("--session-id", spec["argv"])
        self.assertNotIn("ghp_x", r.stdout)
        r = self.run_cli("launch-spec", "--ctx", str(ctx), "--state", str(state), "--paths", str(paths),
                         "--network-args", "not json", "--json")
        self.assertEqual(r.returncode, 1)
        p.secrets_file.write_text("GITHUB_TOKEN=ghp_x\n")
        r = self.run_cli("launch-spec", "--ctx", str(ctx), "--state", str(state), "--paths", str(paths),
                         "--network-args", "[]", "--json")
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("ghp_x", r.stdout + r.stderr)

    def test_approve_exit_codes(self):
        spec, brief = self.spec_repo(), self.brief_dir()
        r = self.run_cli("approve", "--brief-dir", str(brief), "--spec-dir", str(spec), "--sha", "0" * 64)
        self.assertEqual(r.returncode, 5)
        self.assertIn("hash mismatch", r.stderr)
        half = self.mkdir("half")
        (half / "brief.md").write_text(BRIEF)
        r = self.run_cli("approve", "--brief-dir", str(half), "--spec-dir", str(spec), "--sha", planchat.brief_hash(half))
        self.assertEqual(r.returncode, 5)
        self.assertIn("coverage.md missing", r.stderr)
        sha = planchat.brief_hash(brief)
        r = self.run_cli("approve", "--brief-dir", str(brief), "--spec-dir", str(spec), "--sha", sha, "--round", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((spec / "briefs" / "change-3.md").exists())
        self.assertIn(sha[:12], r.stdout)


if __name__ == "__main__":
    unittest.main()
