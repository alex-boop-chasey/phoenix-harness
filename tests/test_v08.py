"""0.5 step 08: project variables in build containers, secret gates and redaction. Fake docker, no Claude."""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import projectenv, workspace  # noqa: E402
from hands.workspace import Change  # noqa: E402
from test_runner import EVAL_PASS, GOOD1, STEP1, STEP2, Env, git  # noqa: E402

SECRET = "sk_live_4f8a9b2c7d1e_SECRET"
HOOK = "whsec_0011223344_HOOK"
PUBLIC = "https://pub.example.test"


def with_env(steps, **kw):
    """Env whose project has STRIPE_KEY/WEBHOOK (secret) and SITE_URL (public) in ~/.config/hands-style secrets dir."""
    e = Env(steps, **kw)
    d = e.tmp / "cfg" / "secrets" / "t"
    st = projectenv.Store(d)
    st.set([{"key": "STRIPE_KEY", "value": SECRET, "public": False},
            {"key": "WEBHOOK", "value": HOOK, "public": False},
            {"key": "SITE_URL", "value": PUBLIC, "public": True},
            {"key": "SHORT", "value": "abc", "public": False}])
    e.cfg.project_env = d / "project.env"
    return e


def docker_runs(e: Env) -> list[list[str]]:
    return [json.loads(x) for x in (e.tmp / "docker.jsonl").read_text().splitlines() if '"run"' in x]


def role_of(argv: list[str]) -> str:
    return next((a.split("=", 1)[1] for a in argv if a.startswith("HANDS_ROLE=")), "")


def all_runner_output(e: Env) -> dict[str, bytes]:
    out = {}
    for p in sorted((e.cfg.state_dir).rglob("*")):
        if p.is_file() and not any(x in str(p) for x in ("/work/", "/verify/", "/acc-view/")):
            out[str(p.relative_to(e.cfg.state_dir))] = p.read_bytes()
    return out


class TestContainers(unittest.TestCase):
    def test_mount_and_env_file_per_role(self):
        e = with_env([STEP1])
        e.queue(GOOD1, EVAL_PASS)
        self.assertEqual(e.runner().run(), 0)
        seen = {}
        for argv in docker_runs(e):
            role = role_of(argv)
            env_files = [argv[i + 1] for i, a in enumerate(argv) if a == "--env-file"]
            mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
            seen.setdefault(role, []).append((env_files, mounts))
        pe = str(e.cfg.project_env)
        for role in ("coder", "accept", "eval"):
            self.assertIn(role, seen)
            for env_files, mounts in seen[role]:
                self.assertIn(pe, env_files, role)
                self.assertIn(f"type=bind,src={pe},dst=/run/hands/project.env,readonly", mounts, role)
        for role in ("export", "probe"):
            for env_files, mounts in seen.get(role, []):
                self.assertNotIn(pe, env_files, role)
                self.assertFalse(any("/run/hands/project.env" in m for m in mounts), role)
        # the Claude token goes to coder and eval only, never to acceptance
        tok = str(e.cfg.claude_env)
        self.assertTrue(all(tok in ef for ef, _ in seen["coder"] + seen["eval"]))
        self.assertTrue(all(tok not in ef for ef, _ in seen["accept"]))

    def test_prompts_list_names_and_visibility_never_values(self):
        e = with_env([STEP1])
        e.queue(GOOD1, EVAL_PASS)
        self.assertEqual(e.runner().run(), 0)
        recs = {r["role"]: r["prompt"] for r in e.prompt_records()}
        for role in ("coder", "eval"):
            p = recs[role]
            self.assertIn("`STRIPE_KEY` (secret)", p)
            self.assertIn("`SITE_URL` (public)", p)
            self.assertIn("/run/hands/project.env", p)
            for v in (SECRET, HOOK):
                self.assertNotIn(v, p)
        self.assertIn("`STRIPE_KEY` (secret)", (e.cfg.state_dir / "attempts" / "S01-1" / "prompt.md").read_text())

    def test_planner_prompt_names_only(self):
        from hands import planner
        e = with_env([STEP1])
        e.cfg.spec_dir.parent.joinpath("description.txt").write_text("x")
        p = planner.prompt(e.cfg, "An idea", "", [], False)
        self.assertIn("`STRIPE_KEY` (secret)", p)
        self.assertIn("`SITE_URL` (public)", p)
        self.assertNotIn(SECRET, p)
        self.assertNotIn(PUBLIC, p)          # the planner gets no values at all, public or not
        e.cfg.project_env = None
        self.assertNotIn("STRIPE_KEY", planner.prompt(e.cfg, "An idea", "", [], False))

    def test_preflight_requires_private_env_file(self):
        e = with_env([STEP1])
        os.chmod(e.cfg.project_env, 0o644)
        with self.assertRaises(SystemExit) as cm:
            e.runner().preflight()
        self.assertIn("0600", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))


class TestAllowlist(unittest.TestCase):
    def test_env_files_always_denied_except_examples(self):
        allowed = ["**"]
        denied = [".env", ".ENV", ".env.local", ".env.production", ".Env.Development.Local", ".dev.vars", ".DEV.VARS",
                  "apps/web/.env", "packages/api/.env.test", "functions/.dev.vars"]
        for p in denied:
            bad = workspace.check_allowlist([Change("A", p, "100644")], allowed)
            self.assertEqual(len(bad), 1, p)
            self.assertIn("environment file", bad[0])
        for p in (".env.example", ".env.sample", "apps/web/.ENV.EXAMPLE", "src/env.ts", ".envrc", "env/.keep", "x.env"):
            self.assertEqual(workspace.check_allowlist([Change("A", p, "100644")], allowed), [], p)
        # deleting one is fine (cleaning up a leak)
        self.assertEqual(workspace.check_allowlist([Change("D", ".env", "000000")], allowed), [])


class TestSecretScan(unittest.TestCase):
    def test_attempt_with_a_secret_value_is_rejected_naming_the_key(self):
        e = with_env([STEP1, STEP2])
        e.queue({"role": "coder", "files": {"src/a.js": f"export const k = '{SECRET}';\n", "src/ok.js": "1\n"},
                 "text": "ok\nSUMMARY: wired stripe"},
                GOOD1, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(1), 0)             # first attempt rejected, second accepted
        st = r.state.data["steps"]["S01"]
        self.assertEqual(st["status"], "passed")
        self.assertEqual(st["attempts"], 2)
        reasons = (e.cfg.state_dir / "attempts" / "S01-1" / "reasons.txt").read_text()
        self.assertIn("src/a.js", reasons)
        self.assertIn("STRIPE_KEY", reasons)
        self.assertNotIn("src/ok.js", reasons)
        self.assertNotIn(SECRET, reasons)
        self.assertNotIn("SHORT", reasons)        # values under 8 characters are not scanned
        self.assertNotIn(SECRET, e.origin_log())
        for name, data in all_runner_output(e).items():
            self.assertNotIn(SECRET.encode(), data, name)
        # the rejected diff is kept for the next prompt, redacted
        diff = (e.cfg.state_dir / "attempts" / "S01-1" / "diff.patch").read_text()
        self.assertIn("[REDACTED:STRIPE_KEY]", diff)
        self.assertIn("[REDACTED:STRIPE_KEY]", (e.cfg.state_dir / "attempts" / "S01-2" / "prompt.md").read_text())

    def test_public_values_and_short_secrets_pass(self):
        e = with_env([STEP1])
        e.queue({"role": "coder", "files": {"src/a.js": f"export const u = '{PUBLIC}'; const s = 'abc';\n"},
                 "text": "ok\nSUMMARY: fine"}, EVAL_PASS)
        self.assertEqual(e.runner().run(), 0)

    def test_scan_helper(self):
        red = projectenv.Redactor({"K": "longsecretvalue", "S": "short", "Q": 'with "quote"'})
        self.assertEqual(red.found(b"xx longsecretvalue yy"), ["K"])
        self.assertEqual(red.found(b'json: with \\"quote\\" escaped'), ["Q"])     # JSON-escaped form too
        self.assertEqual(red.found(b"short"), [])
        self.assertEqual(red.text("a longsecretvalue b"), "a [REDACTED:K] b")
        self.assertEqual(projectenv.Redactor({}).text("plain"), "plain")


class TestRedaction(unittest.TestCase):
    def test_every_output_path_is_redacted(self):
        steps = [{**STEP1, "acceptance": ["bash acceptance/leak.sh"]}]
        e = with_env(steps, extra_acceptance={"leak.sh": "test -f src/ok.js || { echo token=%s; echo hook=%s; exit 1; }\n" % (SECRET, HOOK)})
        stream = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": f"curl -H 'Authorization: {SECRET}' x"}}]}}]
        e.queue({"role": "coder", "files": {"src/a.js": "1\n"}, "stream": stream,
                 "text": f"used {SECRET} and {HOOK}\nSUMMARY: wired {SECRET}"},
                {"role": "coder", "files": {"src/ok.js": "2\n"}, "text": "ok\nSUMMARY: fine"}, EVAL_PASS)
        r = e.runner()
        sent = []
        r.notify.send = lambda msg: sent.append(msg) or True
        acts = []                                 # what the dashboard's live line gets: activity.json after each item
        orig = r.on_activity
        act_file = e.cfg.state_dir / "activity.json"
        r.on_activity = lambda item: (orig(item), acts.append({"text": act_file.read_text() if act_file.exists() else ""}))
        rc = r.run(1)
        self.assertEqual(rc, 0, r.state.data)
        self.assertEqual(r.state.data["steps"]["S01"]["attempts"], 2)     # the first try failed acceptance (leaky log)
        self.assertEqual(r.state.data["steps"]["S01"]["status"], "passed")
        out = all_runner_output(e)
        for name, data in out.items():
            self.assertNotIn(SECRET.encode(), data, name)
            self.assertNotIn(HOOK.encode(), data, name)
        for name in ("attempts/S01-1/coder.json", "attempts/S01-1/accept-0.log", "attempts/S01-1/reasons.txt",
                     "attempts.jsonl"):
            self.assertIn(b"[REDACTED:STRIPE_KEY]", out[name], name)
        self.assertIn(b"[REDACTED:WEBHOOK]", out["attempts/S01-1/accept-0.log"])
        self.assertTrue(sent and all(SECRET not in m and HOOK not in m for m in sent), sent)
        r.send(f"escalation text quoting {SECRET}")                       # every Telegram message goes through here
        self.assertEqual(sent[-1], "escalation text quoting [REDACTED:STRIPE_KEY]")
        self.assertTrue(acts and all(SECRET not in a["text"] for a in acts), acts)
        self.assertTrue(any("[REDACTED:STRIPE_KEY]" in a["text"] for a in acts), acts)
        self.assertNotIn(SECRET, e.origin_log())

    def test_progress_and_verdict_redacted(self):
        e = with_env([STEP1])
        e.queue({"role": "coder", "files": {"src/a.js": "1\n"}, "text": f"ok\nSUMMARY: set {SECRET} in env"},
                {"role": "eval", "text": 'VERDICT: {"pass": true, "reasons": [], "checked": ["saw %s"], "risks": ["%s visible"]}' % (SECRET, HOOK)})
        self.assertEqual(e.runner().run(), 0)
        out = all_runner_output(e)
        for name, data in out.items():
            self.assertNotIn(SECRET.encode(), data, name)
            self.assertNotIn(HOOK.encode(), data, name)
        self.assertIn(b"[REDACTED:STRIPE_KEY]", out["progress.jsonl"])
        self.assertIn(b"[REDACTED:WEBHOOK]", out["attempts/S01-1/verdict.json"])
        body = git(e.tmp / "app.git", "log", "--format=%B", "main")
        self.assertIn("[REDACTED:STRIPE_KEY]", body)          # commit message carries the summary
        self.assertNotIn(SECRET, body)


class TestCheck(unittest.TestCase):
    def test_check_env_reports_and_fails(self):
        from hands.__main__ import check_env
        e = with_env([STEP1])
        lines = []
        ok = check_env(e.cfg, lambda good, msg: lines.append((good, msg)))
        self.assertTrue(ok, lines)
        self.assertTrue(any("3 secret" in m and "1 public" in m for _, m in lines), lines)
        self.assertTrue(all(SECRET not in m for _, m in lines))
        os.chmod(e.cfg.project_env, 0o640)
        self.assertFalse(check_env(e.cfg, lambda good, msg: lines.append((good, msg))))
        self.assertTrue(any(not good and "0600" in m for good, m in lines), lines)
        os.chmod(e.cfg.project_env, 0o600)
        with open(e.cfg.project_env, "a") as f:
            f.write("lower=case\n")
        lines.clear()
        self.assertFalse(check_env(e.cfg, lambda good, msg: lines.append((good, msg))))
        self.assertTrue(any(not good and "line" in m for good, m in lines), lines)
        e.cfg.project_env = None
        lines.clear()
        self.assertTrue(check_env(e.cfg, lambda good, msg: lines.append((good, msg))))


if __name__ == "__main__":
    unittest.main()
