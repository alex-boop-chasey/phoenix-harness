"""The resolver in the runner: cause codes, the resolver session, the four actions, caps, the off switch and the
hint in the next coder prompt. Uses tests/fake_docker.py scenarios; no docker, no network."""
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import resolver  # noqa: E402
from test_runner import EVAL_PASS, GOOD1, STEP1, Env  # noqa: E402

ONE = {**STEP1, "max_attempts": 1}
BAD = {"role": "coder", "files": {"src/x.js": "x\n"}, "text": "tried\nSUMMARY: wrong file"}
OUTSIDE = {"role": "coder", "files": {"evil/x.js": "x\n"}, "text": "tried\nSUMMARY: wrote outside"}
HINT = {"action": "hint_and_retry", "reason": "wrong file", "hint": "Create src/a.js, not src/x.js."}
ALEX = {"action": "escalate_to_alex", "reason": "needs a human", "note": "Check the registry login."}
START = 1_800_000_000


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.sent = []

    def env(self, steps=(ONE,), on=True, **kw):
        e = Env(list(steps), **kw)
        e.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        if on:
            resolver.toggle(e.cfg.state_dir, True, False, "test")
        return e

    def runner(self, e):
        r = e.runner()
        r.notify.send = lambda m: self.sent.append(m) or True
        return r

    def resolver_runs(self, e):
        return [x for x in e.prompt_records() if x["role"] == "resolver"]

    def coder_prompts(self, e):
        return [x["prompt"] for x in e.prompt_records() if x["role"] == "coder"]


class Actions(Base):
    def test_off_means_no_resolver_and_todays_escalation(self):
        e = self.env(on=False)
        e.queue(BAD)
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(r.state.status, "escalation")
        self.assertEqual(self.resolver_runs(e), [])
        esc = [m for m in self.sent if m.startswith("ESCALATION:")]
        self.assertEqual(len(esc), 1)
        self.assertTrue(esc[0].endswith("\nFix, then: hands t ack"), esc[0])
        self.assertEqual(r.state.data["escalation"]["cause"], "attempts_exhausted")

    def test_hint_and_retry_reaches_the_next_coder_prompt_once(self):
        e = self.env(steps=(ONE, {**STEP1, "id": "S02", "title": "two", "max_attempts": 1, "depends_on": ["S01"],
                                  "acceptance": ["test -f src/b.js"]}))
        e.queue(BAD, {"role": "resolver", "decision": HINT}, GOOD1, EVAL_PASS,
                {"role": "coder", "files": {"src/b.js": "b\n"}}, EVAL_PASS)
        r = self.runner(e)
        self.assertEqual(r.run(), 0)
        prompts = self.coder_prompts(e)
        self.assertEqual(len(prompts), 3)
        p = prompts[1]
        at = p.index("## Resolver hint (advisory)")
        self.assertGreater(p.index("Create src/a.js, not src/x.js.", at), at)
        self.assertGreater(p.rindex("<untrusted-data", 0, p.index("Create src/a.js", at)), at)
        self.assertNotIn("Resolver hint", prompts[0])
        self.assertNotIn("Resolver hint", prompts[2])
        self.assertNotIn("resolver_hint", json.dumps(r.state.data["steps"]))
        run = self.resolver_runs(e)[0]
        cmd = " ".join(run["cmd"])
        self.assertIn("--disallowedTools Write,Edit,NotebookEdit,Agent,WebFetch,WebSearch", cmd)
        self.assertIn("--max-turns 60", cmd)
        self.assertEqual(run["env"]["HANDS_ROLE"], "resolver")
        self.assertEqual(sorted(run["mounts"]) .count("/app"), 1)
        for dst in ("/spec", "/app", "/attempt", "/opt/hands/prompts"):
            self.assertIn(dst, run["readonly"])
        self.assertNotIn("/etc/hands/allowed-domains", run["mounts"])
        self.assertNotIn("/run/hands/project.env", run["mounts"])
        self.assertNotIn("/workspace", run["mounts"])
        self.assertIn("Cause code: attempts_exhausted", run["prompt"])
        sd = e.cfg.state_dir
        rdir = next(sd.glob("attempts/*/resolver-1"))
        for name in ("briefing.md", "briefing.sha256", "session.json", "decision.json"):
            self.assertTrue((rdir / name).is_file(), name)
        dec = json.loads((rdir / "decision.json").read_text())
        self.assertTrue(dec["valid"])
        self.assertTrue(dec["executed"])
        self.assertEqual(dec["action"], "hint_and_retry")
        self.assertEqual(dec["model"], "claude-fable-5-1")
        self.assertIn("cost_usd", dec)
        line = json.loads((sd / "resolver.jsonl").read_text().splitlines()[0])
        self.assertEqual((line["step"], line["cause"], line["action"], line["executed"]),
                         ("S01", "attempts_exhausted", "hint_and_retry", True))
        self.assertEqual(r.state.data["total_sessions"], 6)
        self.assertTrue(any("hint_and_retry" in m and "RESOLVER" in m for m in self.sent), self.sent)

    def test_secret_in_briefing_and_transcript_is_redacted(self):
        e = self.env()
        pe = e.tmp / "project.env"
        pe.write_text("API_TOKEN=s3cr3t-value-123456\n")
        pe.chmod(0o600)
        e.cfg.project_env = pe
        e.queue({"role": "coder", "files": {"src/x.js": "x\n"}, "text": "tried s3cr3t-value-123456"}, {
            "role": "resolver", "decision": ALEX, "text": "see s3cr3t-value-123456\nDECISION: {}"})
        r = self.runner(e)
        r.run()
        rdir = next(e.cfg.state_dir.glob("attempts/*/resolver-1"))
        for name in ("briefing.md", "session.json"):
            self.assertNotIn("s3cr3t-value-123456", (rdir / name).read_text(), name)
        self.assertNotIn("s3cr3t-value-123456", "".join(self.sent))

    def test_schedule_retry_pauses_then_resumes(self):
        e = self.env()
        e.queue(BAD, {"role": "resolver", "decision": {"action": "schedule_retry", "reason": "limit",
                                                        "retry_at": iso(START + 7200)}}, GOOD1, EVAL_PASS)
        r = self.runner(e)
        seen = []
        orig = r.sleep
        r.sleep = lambda s: (seen.append(r.state.status), orig(s))
        self.assertEqual(r.run(), 0)
        self.assertIn("paused_resolver", seen)
        self.assertGreaterEqual(r.clock() - START, 7200)
        self.assertEqual(r.state.status, "done")
        self.assertIn("Attempt 1 of 1", self.coder_prompts(e)[1])

    def test_stop_during_the_nap_keeps_the_schedule_across_a_restart(self):
        e = self.env()
        e.queue(BAD, {"role": "resolver", "decision": {"action": "schedule_retry", "reason": "limit",
                                                        "retry_at": iso(START + 7200)}}, GOOD1, EVAL_PASS)
        r = self.runner(e)
        r.sleep = lambda s: r.stop_file.touch()
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state.status, "paused_resolver")
        self.assertEqual(len(self.coder_prompts(e)), 1)
        r.stop_file.unlink()
        r2 = self.runner(e)
        t0 = r2.clock()
        self.assertEqual(r2.run(), 0)
        self.assertGreaterEqual(r2.clock() - t0, 7000)
        self.assertEqual(r2.state.status, "done")
        self.assertEqual(len(self.coder_prompts(e)), 2)

    def test_turning_the_resolver_off_cancels_a_scheduled_retry(self):
        e = self.env()
        e.queue(BAD, {"role": "resolver", "decision": {"action": "schedule_retry", "reason": "limit",
                                                        "retry_at": iso(START + 7200)}}, GOOD1, EVAL_PASS)
        r = self.runner(e)
        r.sleep = lambda s: resolver.toggle(e.cfg.state_dir, False, False, "test")
        self.assertEqual(r.run(), 2)
        self.assertEqual(r.state.status, "escalation")
        self.assertIn("resolver turned off; scheduled retry cancelled", r.state.data["reason"])
        self.assertEqual(r.state.data["reason"].count("scheduled retry cancelled"), 1)
        self.assertEqual(len(self.coder_prompts(e)), 1)
        self.assertTrue(any("scheduled retry cancelled" in m for m in self.sent), self.sent)

    def test_restart_after_crash_keeps_the_attempt_counts(self):
        e = self.env(steps=({**STEP1, "max_attempts": 3},))
        e.queue({"role": "resolver", "decision": {"action": "restart_after_crash", "reason": "flaky"}},
                GOOD1, EVAL_PASS)
        r = self.runner(e)
        r.state.step("S01").update({"attempts": 1, "status": "failed", "reasons": ["earlier"]})
        orig = r.attempt
        calls = []

        def attempt(step):
            if not calls:
                calls.append(1)
                raise RuntimeError("boom")
            return orig(step)
        r.attempt = attempt
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state.data["steps"]["S01"]["attempts"], 2)
        self.assertIn("Attempt 2 of 3", self.coder_prompts(e)[0])
        self.assertEqual(json.loads((e.cfg.state_dir / "resolver.jsonl").read_text().splitlines()[0])["cause"],
                         "runner_error")

    def test_runner_error_with_the_resolver_off_behaves_as_before(self):
        e = self.env(on=False)
        r = self.runner(e)
        r.attempt = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            r.run()
        self.assertEqual(r.state.status, "escalation")
        self.assertEqual(r.state.data["reason"], "runner error: RuntimeError: boom")
        self.assertTrue(any(m.startswith("RUNNER ERROR: RuntimeError: boom") for m in self.sent))

    def test_escalate_to_alex_stops_with_the_note(self):
        e = self.env()
        e.queue(BAD, {"role": "resolver", "decision": {**ALEX, "root_command": "sudo systemctl restart docker"}})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(r.state.status, "escalation")
        self.assertIn("Check the registry login.", r.state.data["reason"])
        self.assertTrue(any("Check the registry login." in m and m.startswith("ESCALATION:") for m in self.sent))
        self.assertEqual(r.state.data["resolver_root_command"], "sudo systemctl restart docker")

    def test_forged_nonce_and_invalid_decisions_escalate(self):
        for item in ({"nonce": "0000", "decision": HINT}, {"decision": {"action": "format_disk", "reason": "x"}},
                     {"text": "no decision here"}, {"decision": {**HINT, "hint": "x" * 2001}}):
            e = self.env()
            e.queue(BAD, {"role": "resolver", **item})
            r = self.runner(e)
            self.assertEqual(r.run(), 2, item)
            self.assertEqual(r.state.status, "escalation")
            self.assertNotIn("resolver_hint", json.dumps(r.state.data["steps"]))
            rdir = next(e.cfg.state_dir.glob("attempts/*/resolver-1"))
            dec = json.loads((rdir / "decision.json").read_text())
            self.assertFalse(dec["valid"])
            self.assertFalse(dec["executed"])
            self.assertEqual(dec["action"], "escalate_to_alex")


class Gates(Base):
    def test_never_auto_cause_runs_no_session(self):
        e = self.env()
        e.queue(OUTSIDE, {"role": "resolver", "decision": HINT})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(self.resolver_runs(e), [])
        self.assertEqual(r.state.data["escalation"]["cause"], "allowlist")
        self.assertTrue(any(m.startswith("ESCALATION:") for m in self.sent))

    def test_secret_leak_and_nonce_mismatch_are_inferred(self):
        e = self.env()
        pe = e.tmp / "project.env"
        pe.write_text("API_TOKEN=s3cr3t-value-123456\n")
        pe.chmod(0o600)
        e.cfg.project_env = pe
        e.queue({"role": "coder", "files": {"src/a.js": "const k = 's3cr3t-value-123456';\n"}})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(r.state.data["escalation"]["cause"], "secret_leak")
        e = self.env()
        e.queue(GOOD1, {"role": "eval", "text": 'VERDICT: {"pass": true, "reasons": [], "checked": [], "risks": []}',
                        "no_nonce": True}, {"role": "eval", "text": "x"}, {"role": "eval", "text": "x"})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(r.state.data["escalation"]["cause"], "verdict_nonce")
        self.assertEqual(self.resolver_runs(e), [])

    def test_self_project_needs_the_self_switch(self):
        e = self.env(is_self=True)
        e.queue(BAD)
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(self.resolver_runs(e), [])

    def test_two_decisions_per_escalation_then_alex(self):
        e = self.env()
        e.queue(BAD, {"role": "resolver", "decision": HINT}, BAD, {"role": "resolver", "decision": HINT}, BAD,
                {"role": "resolver", "decision": HINT})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(len(self.resolver_runs(e)), 2)
        self.assertEqual(r.state.status, "escalation")

    def test_daily_cap(self):
        e = self.env()
        resolver.set_cap(e.cfg.state_dir, 1)
        e.queue(BAD, {"role": "resolver", "decision": HINT}, BAD, {"role": "resolver", "decision": HINT})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(len(self.resolver_runs(e)), 1)

    def test_resolver_sessions_count_toward_the_session_cap(self):
        e = self.env(max_sessions=2)
        e.queue(BAD, {"role": "resolver", "decision": HINT}, GOOD1, EVAL_PASS)
        r = self.runner(e)
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state.status, "stopped")
        self.assertEqual(r.sessions, 2)
        e = self.env(max_sessions=1)
        e.queue(BAD, {"role": "resolver", "decision": HINT})
        r = self.runner(e)
        self.assertEqual(r.run(), 2)
        self.assertEqual(self.resolver_runs(e), [])

    def test_resolver_cost_counts_toward_the_cost_cap(self):
        e = self.env(cost_cap_usd=0.25)
        e.queue(BAD, {"role": "resolver", "decision": HINT, "cost": 0.5}, GOOD1, EVAL_PASS)
        r = self.runner(e)
        self.assertEqual(r.run(), 0)
        self.assertEqual(r.state.status, "stopped")
        self.assertEqual(r.state.data["reason"], "cost cap")


class Cli(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(tempfile.mkdtemp(prefix="hands-res-home-"))
        p = mock.patch("hands.config.CONFIG_HOME", self.home)
        p.start()
        self.addCleanup(p.stop)

    def project(self, e):
        (self.home / "projects").mkdir()
        c = e.cfg
        (self.home / "projects" / "t.toml").write_text(
            f'spec_dir = "{c.spec_dir}"\napp_dir = "{c.app_dir}"\nstate_dir = "{c.state_dir}"\nimage = "hands:test"\n'
            'telegram_env = ""\n')

    def main(self, *args):
        import io
        from contextlib import redirect_stdout
        from hands import __main__ as cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["t", *args])
        return rc, buf.getvalue()

    def test_prompt_command_shows_a_stored_hint(self):
        e = self.env(on=False)
        self.project(e)
        e.cfg.state_dir.joinpath("state.json").write_text(json.dumps(
            {"status": "running", "steps": {"S01": {"status": "pending", "attempts": 0, "reasons": [],
                                                     "resolver_hint": "Use the slug helper."}}}))
        rc, out = self.main("prompt", "S01")
        self.assertEqual(rc, 0)
        self.assertIn("## Resolver hint (advisory)", out)
        self.assertIn("Use the slug helper.", out)

    def test_ack_clears_resolver_state(self):
        e = self.env(on=False)
        self.project(e)
        e.cfg.state_dir.joinpath("state.json").write_text(json.dumps(
            {"status": "escalation", "reason": "x", "escalation": {"cause": "same_failure"},
             "resolver_escalation": {"step": "S01", "cause": "same_failure", "decisions": 2},
             "resolver_note": "n", "steps": {"S01": {"status": "failed", "attempts": 1, "resolver_hint": "h"}}}))
        rc, _ = self.main("ack")
        self.assertEqual(rc, 0)
        d = json.loads(e.cfg.state_dir.joinpath("state.json").read_text())
        self.assertEqual(d["status"], "idle")
        for k in ("escalation", "resolver_escalation", "resolver_note"):
            self.assertNotIn(k, d)
        self.assertNotIn("resolver_hint", d["steps"]["S01"])
        self.assertEqual(d["steps"]["S01"]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
