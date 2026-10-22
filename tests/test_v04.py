"""0.4.0: per-step acceptance view, watchdog all, dashboard. No docker or Claude needed."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401  (removes every mkdtemp dir after each test)
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import accview, config as cfgmod, dashboard, spec as specmod  # noqa: E402
from test_runner import EVAL_PASS, GOOD1, GOOD2, STEP1, STEP2, Env  # noqa: E402


def mkspec(steps, files):
    d = Path(tempfile.mkdtemp(prefix="hands-spec-"))
    for rel, body in files.items():
        (d / "acceptance" / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / "acceptance" / rel).write_text(body)
    (d / "steps.json").write_text(json.dumps({"version": 1, "allowed_paths": ["src/**"], "steps": steps}))
    return specmod.load(d)


class TestAccView(unittest.TestCase):
    STEPS = [
        {"id": "S01", "title": "g", "goal": "g", "acceptance": ["node --test acceptance/greet.test.mjs"]},
        {"id": "S02", "title": "s", "goal": "s",
         "acceptance": ["node --test acceptance/greet.test.mjs 'acceptance/shout.test.mjs'"]},
        {"id": "S03", "title": "e", "goal": "e", "acceptance": ["bash acceptance/s3/"], "acceptance_files": ["fixtures/s3.json"]},
        {"id": "S04", "title": "x", "goal": "x", "acceptance": ["node --test acceptance/s4/*.test.mjs"]},
    ]
    FILES = {"greet.test.mjs": "", "shout.test.mjs": "", "helpers/util.mjs": "", "s3/run.sh": "", "s3/more/x.sh": "",
             "fixtures/s3.json": "", "s4/a.test.mjs": "", "s4/b.test.mjs": "", "s4/data.json": ""}

    def test_visibility(self):
        sp = mkspec(self.STEPS, self.FILES)
        v1 = accview.visible(sp, sp.step("S01"), set())
        self.assertIn("greet.test.mjs", v1)
        self.assertIn("helpers/util.mjs", v1)                # unclaimed = shared
        self.assertIn("s4/data.json", v1)                    # not matched by S04's glob = shared
        for hidden in ("shout.test.mjs", "s3/run.sh", "s3/more/x.sh", "fixtures/s3.json", "s4/a.test.mjs"):
            self.assertNotIn(hidden, v1)
        v2 = accview.visible(sp, sp.step("S02"), {"S01"})
        self.assertIn("shout.test.mjs", v2)
        self.assertNotIn("s3/run.sh", v2)
        v3 = accview.visible(sp, sp.step("S03"), {"S01", "S02"})
        self.assertTrue({"s3/run.sh", "s3/more/x.sh", "fixtures/s3.json", "shout.test.mjs"} <= set(v3))
        self.assertNotIn("s4/a.test.mjs", v3)
        v4 = accview.visible(sp, sp.step("S04"), {"S01", "S02", "S03"})
        self.assertEqual(set(v4), set(self.FILES))

    def test_build_copies_regular_files_only(self):
        sp = mkspec(self.STEPS, self.FILES)
        secret = Path(tempfile.mkdtemp()) / "secret"
        secret.write_text("TOKEN")
        os.symlink(secret, sp.root / "acceptance" / "leak.txt")
        dest = Path(tempfile.mkdtemp()) / "view"
        names = accview.build(sp, sp.step("S01"), set(), dest)
        self.assertNotIn("leak.txt", names)
        self.assertFalse((dest / "leak.txt").exists())
        self.assertTrue((dest / "greet.test.mjs").is_file())
        self.assertFalse((dest / "shout.test.mjs").exists())

    def test_acceptance_files_validated(self):
        with self.assertRaises(specmod.SpecError):
            mkspec([{"id": "A", "title": "t", "goal": "g", "acceptance": ["x"], "acceptance_files": ["nope.js"]}], {})
        with self.assertRaises(specmod.SpecError):
            mkspec([{"id": "A", "title": "t", "goal": "g", "acceptance": ["x"], "acceptance_files": ["../steps.json"]}],
                   {"a": ""})

    def test_runner_hides_later_steps_tests(self):
        s1 = {**STEP1}
        s2 = {**STEP2, "acceptance": ["test -f src/b.js", "bash acceptance/s2.sh"]}
        e = Env([s1, s2], extra_acceptance={"s2.sh": "test -f src/b.js\n"})
        e.queue(GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        att = e.cfg.state_dir / "attempts"
        self.assertEqual((att / "S01-1" / "acceptance-files.txt").read_text().split(), ["acceptance/check.sh"])
        self.assertEqual(sorted((att / "S02-1" / "acceptance-files.txt").read_text().split()),
                         ["acceptance/check.sh", "acceptance/s2.sh"])
        coder = [x for x in e.prompt_records() if x["role"] == "coder"]
        self.assertNotIn("s2.sh", coder[0]["prompt"])
        self.assertIn("acceptance/s2.sh", coder[1]["prompt"])
        # the coder container's /workspace/acceptance mount is the runner's view, not the spec repo
        runs = [json.loads(x) for x in (e.tmp / "docker.jsonl").read_text().splitlines()]
        acc = [a for argv in runs if "HANDS_ROLE=coder" in argv for a in argv if "dst=/workspace/acceptance" in a]
        self.assertTrue(acc and all("acc-view" in a and a.endswith(",readonly") for a in acc), acc)


class TestCli(unittest.TestCase):
    def test_all_only_with_watchdog(self):
        from hands.__main__ import main
        self.assertEqual(main(["all", "status"]), 2)

    def test_hands_exe_is_absolute_or_module(self):
        from hands.__main__ import hands_exe
        exe = hands_exe()
        self.assertTrue(exe.startswith("/") or exe.startswith("'/") or exe.startswith("env PYTHONPATH="), exe)


class Dash:
    def __init__(self):
        self.home = Path(tempfile.mkdtemp(prefix="hands-dash-"))
        self.old = cfgmod.CONFIG_HOME
        cfgmod.CONFIG_HOME = self.home
        os.environ["HANDS_CONFIG_HOME"] = str(self.home)
        os.environ["HANDS_DASH_INPROC_CLI"] = "1"
        self.srv = dashboard.Server(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        st, hdr, _ = self.req("GET", f"/login?c={dashboard.mint_login_code()}", cookie=False)
        assert st == 303, st
        self.sid = hdr["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        cfgmod.CONFIG_HOME = self.old
        os.environ.pop("HANDS_CONFIG_HOME", None)

    def req(self, method, path, *, host=None, cookie=True, origin=None, xh=True, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        if cookie:
            h["Cookie"] = f"other=1; {dashboard.COOKIE}={self.sid}"
        if origin is not None:
            h["Origin"] = origin
        if xh and method == "POST":
            h["X-Hands"] = "1"
        c.request(method, path, headers=h, body=b"" if method == "POST" else None)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.e = Env([STEP1, STEP2])
        self.e.queue({"role": "coder", "files": {"src/z.js": "x\n"}}, GOOD1, EVAL_PASS, GOOD2, EVAL_PASS)
        self.assertEqual(self.e.runner().run(), 0)
        self.d = Dash()
        (self.d.home / "projects").mkdir()
        c = self.e.cfg
        (self.d.home / "projects" / "t.toml").write_text(
            f'spec_dir = "{c.spec_dir}"\napp_dir = "{c.app_dir}"\nstate_dir = "{c.state_dir}"\nimage = "hands:test"\n'
            f'claude_env = "{c.claude_env}"\n')
        (self.d.home / "projects" / "broken.toml").write_text("nonsense = 1\n")

    def tearDown(self):
        self.d.close()

    def test_login_code_single_use_private_and_expiring(self):
        code = dashboard.mint_login_code()
        self.assertEqual(os.stat(dashboard.login_path()).st_mode & 0o777, 0o600)
        self.assertEqual(self.d.req("GET", "/login?c=wrong", cookie=False)[0], 403)
        st, hdr, _ = self.d.req("GET", f"/login?c={code}", cookie=False)
        self.assertEqual(st, 303)
        self.assertIn("HttpOnly", hdr["Set-Cookie"])
        self.assertIn("SameSite=Strict", hdr["Set-Cookie"])
        self.assertNotIn(code, hdr["Set-Cookie"])
        self.assertEqual(self.d.req("GET", f"/login?c={code}", cookie=False)[0], 403)      # used
        code = dashboard.mint_login_code()
        os.chmod(dashboard.login_path(), 0o644)
        self.assertEqual(self.d.req("GET", f"/login?c={code}", cookie=False)[0], 403)      # loose perms
        os.unlink(dashboard.login_path())
        p = dashboard.login_path()
        p.write_text(f"abc {int(__import__('time').time()) - 1}\n")
        p.chmod(0o600)
        self.assertEqual(self.d.req("GET", "/login?c=abc", cookie=False)[0], 403)          # expired

    def test_auth_and_host(self):
        self.assertEqual(self.d.req("GET", "/api/projects", cookie=False)[0], 401)
        self.assertEqual(self.d.req("GET", "/api/projects", host="evil.example:8090")[0], 403)
        self.assertEqual(self.d.req("GET", "/api/projects", host=f"localhost:{self.d.port}")[0], 403)
        self.d.sid = "forged"
        self.assertEqual(self.d.req("GET", "/api/projects")[0], 401)
        self.d.sid = self.d.srv.new_session()
        st, hdr, body = self.d.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("script-src 'self'", hdr["Content-Security-Policy"])
        self.assertIn(b"/app.js", body)

    def test_api_reads(self):
        st, _, body = self.d.req("GET", "/api/projects")
        rows = {r["name"]: r for r in json.loads(body)}
        self.assertEqual(rows["t"]["status"], "done")
        self.assertIn("error", rows["broken"])
        st, _, body = self.d.req("GET", "/api/p/t")
        d = json.loads(body)
        self.assertEqual([s["status"] for s in d["steps"]], ["passed", "passed"])
        self.assertEqual(d["steps"][0]["attempts"], 2)
        st, _, body = self.d.req("GET", "/api/p/t/log?lines=5")
        self.assertEqual(len(json.loads(body)["lines"]), 5)
        st, _, body = self.d.req("GET", "/api/p/t/attempts")
        names = [a["name"] for a in json.loads(body)]
        self.assertIn("S01-1", names)
        st, _, body = self.d.req("GET", "/api/p/t/attempts/S01-1/reasons.txt")
        self.assertEqual(st, 200)
        self.assertIn(b"acceptance command failed", body)

    def test_no_traversal_or_symlinks(self):
        for p in ("/api/p/t/attempts/S01-1/..%2f..%2fstate.json", "/api/p/t/attempts/..%2fS01-1/prompt.md",
                  "/api/p/t/attempts/S01-1/state.json", "/api/p/..%2f..%2fetc/log", "/api/p/nope",
                  "/api/p/t/attempts/S01-1/%2e%2e"):
            self.assertEqual(self.d.req("GET", p)[0], 404, p)
        secret = Path(tempfile.mkdtemp()) / "s"
        secret.write_text("SECRET")
        adir = self.e.cfg.state_dir / "attempts" / "S01-1"
        (adir / "verdict.json").unlink(missing_ok=True)
        os.symlink(secret, adir / "verdict.json")
        st, _, body = self.d.req("GET", "/api/p/t/attempts/S01-1/verdict.json")
        self.assertNotIn(b"SECRET", body)
        self.assertNotEqual(st, 200)

    def test_post_requires_origin_and_header(self):
        stop = self.e.cfg.state_dir / "STOP"
        self.assertEqual(self.d.req("POST", "/api/p/t/action/stop")[0], 403)
        self.assertEqual(self.d.req("POST", "/api/p/t/action/stop", origin="http://evil.example")[0], 403)
        self.assertEqual(self.d.req("POST", "/api/p/t/action/stop", origin=f"http://127.0.0.1:{self.d.port}", xh=False)[0], 403)
        self.assertFalse(stop.exists())
        st, _, body = self.d.req("POST", "/api/p/t/action/stop", origin=f"http://127.0.0.1:{self.d.port}")
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["rc"], 0)
        self.assertTrue(stop.exists())
        self.assertEqual(self.d.req("POST", "/api/p/t/action/rm-rf", origin=f"http://127.0.0.1:{self.d.port}")[0], 404)

    def test_negative_length_rejected(self):
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        c.putrequest("POST", "/api/p/t/action/stop", skip_host=True)
        for k, v in (("Host", f"127.0.0.1:{self.d.port}"), ("Cookie", f"{dashboard.COOKIE}={self.d.sid}"),
                     ("Origin", f"http://127.0.0.1:{self.d.port}"), ("X-Hands", "1"), ("Content-Length", "-1")):
            c.putheader(k, v)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)

    def test_head_login_does_not_consume_code(self):
        code = dashboard.mint_login_code()
        self.assertEqual(self.d.req("HEAD", f"/login?c={code}", cookie=False)[0], 405)
        self.assertEqual(self.d.req("GET", f"/login?c={code}", cookie=False)[0], 303)


if __name__ == "__main__":
    unittest.main()


class TestReviewFixes(unittest.TestCase):
    def test_strict_parser_rejects_ambiguous_commands(self):
        for cmd in ("node --test acceptance/{a,b}.mjs", "cd acceptance/ && x", 'node acceptance/"x.mjs"',
                    "cd acceptance && node s3.test.mjs", "node acceptance/$X", "x ./acceptance", "node acceptance/../x"):
            with self.assertRaises(specmod.SpecError, msg=cmd):
                mkspec([{"id": "A", "title": "t", "goal": "g", "acceptance": [cmd]}], {"a.test.mjs": ""})

    def test_workspace_prefix_claims(self):
        sp = mkspec([{"id": "A", "title": "t", "goal": "g", "acceptance": ["true"]},
                     {"id": "B", "title": "t", "goal": "g", "acceptance": ["node /workspace/acceptance/b.test.mjs"]}],
                    {"b.test.mjs": ""})
        self.assertEqual(accview.visible(sp, sp.step("A"), set()), [])

    def test_regression_files_always_shared(self):
        d = Path(tempfile.mkdtemp(prefix="hands-spec-"))
        (d / "acceptance").mkdir()
        for f in ("pw.config.ts", "s3.test.mjs"):
            (d / "acceptance" / f).write_text("")
        (d / "steps.json").write_text(json.dumps({
            "version": 1, "allowed_paths": ["src/**"], "regression": ["node acceptance/pw.config.ts"],
            "steps": [{"id": "S1", "title": "t", "goal": "g", "acceptance": ["true"]},
                      {"id": "S3", "title": "t", "goal": "g",
                       "acceptance": ["node acceptance/pw.config.ts acceptance/s3.test.mjs"]}]}))
        sp = specmod.load(d)
        self.assertEqual(accview.visible(sp, sp.step("S1"), set()), ["pw.config.ts"])

    def test_symlink_in_acceptance_rejected(self):
        d = Path(tempfile.mkdtemp(prefix="hands-spec-"))
        (d / "acceptance").mkdir()
        (d / "acceptance" / "a.mjs").write_text("")
        os.symlink("a.mjs", d / "acceptance" / "b.mjs")
        (d / "steps.json").write_text(json.dumps({"version": 1, "allowed_paths": ["src/**"],
                                                  "steps": [{"id": "A", "title": "t", "goal": "g", "acceptance": ["true"]}]}))
        with self.assertRaises(specmod.SpecError):
            specmod.load(d)

    def test_preflight_failure_escalates(self):
        e = Env([STEP1], image="hands:test")
        e.cfg.claude_env.unlink()
        r = e.runner()
        with self.assertRaises(SystemExit):
            r.run()
        self.assertEqual(r.state.status, "escalation")


class TestWatchdog(unittest.TestCase):
    def _proj(self):
        e = Env([STEP1])
        home = Path(tempfile.mkdtemp(prefix="hands-cfg-"))
        (home / "projects").mkdir()
        c = e.cfg
        (home / "projects" / "w.toml").write_text(
            f'spec_dir = "{c.spec_dir}"\napp_dir = "{c.app_dir}"\nstate_dir = "{c.state_dir}"\nimage = "hands:test"\n'
            f'claude_env = "{c.claude_env}"\ntelegram_env = "{home}/none.env"\n')
        old = cfgmod.CONFIG_HOME
        cfgmod.CONFIG_HOME = home
        self.addCleanup(setattr, cfgmod, "CONFIG_HOME", old)
        return e

    def test_user_interrupt_not_restarted(self):
        from hands.__main__ import main
        e = self._proj()
        r = e.runner()
        for reason in ("interrupted ()", "interrupted (signal 1)"):
            r.state.set_status("stopped", reason)
            self.assertEqual(main(["w", "watchdog"]), 0)
            self.assertFalse((e.cfg.state_dir / "watchdog.json").exists())

    def test_restart_limit_escalates(self):
        import time as _t
        from hands.__main__ import main
        e = self._proj()
        r = e.runner()
        r.state.set_status("running", "")
        (e.cfg.state_dir / "watchdog.json").write_text(json.dumps({"restarts": [_t.time() - 60] * 3}))
        self.assertEqual(main(["w", "watchdog"]), 1)
        self.assertEqual(json.loads((e.cfg.state_dir / "state.json").read_text())["status"], "escalation")


class TestReviewRound2(unittest.TestCase):
    def test_spec_wide_glob_does_not_leak(self):
        d = Path(tempfile.mkdtemp(prefix="hands-spec-"))
        (d / "acceptance").mkdir()
        for f in ("s1.test.mjs", "s2.test.mjs", "helper.mjs"):
            (d / "acceptance" / f).write_text("")
        (d / "steps.json").write_text(json.dumps({
            "version": 1, "allowed_paths": ["src/**"], "regression": ["node --test 'acceptance/*.test.mjs'"],
            "steps": [{"id": "S1", "title": "t", "goal": "g", "acceptance": ["node --test acceptance/s1.test.mjs"]},
                      {"id": "S2", "title": "t", "goal": "g", "acceptance": ["node --test acceptance/s2.test.mjs"]}]}))
        sp = specmod.load(d)
        self.assertEqual(accview.visible(sp, sp.step("S1"), set()), ["helper.mjs", "s1.test.mjs"])

    def test_more_misses_rejected(self):
        for cmd in ("node $PWD/acceptance/s3.test.mjs", "node /workspace//acceptance/x.mjs", 'cd "acceptance" && x',
                    "x --dir=acceptance", "ACC=acceptance; node $ACC/x", "node accept*/s3.test.mjs".replace("accept*", "acceptance*")):
            with self.assertRaises(specmod.SpecError, msg=cmd):
                accview.tokens(cmd, "t")

    def test_reasonable_commands_accepted(self):
        cases = {"node acceptance/gen.mjs>out.txt": ["gen.mjs"],
                 "npx playwright test acceptance/e2e/login.spec.ts:12": ["e2e/login.spec.ts"],
                 "node --test acceptance/a.test.mjs,acceptance/b.test.mjs": ["a.test.mjs", "b.test.mjs"],
                 "node <acceptance/in.txt": ["in.txt"],
                 "pnpm exec playwright test acceptance/e2e/login.spec.ts --config=acceptance/pw.config.ts":
                     ["e2e/login.spec.ts", "pw.config.ts"]}
        for cmd, want in cases.items():
            self.assertEqual(accview.tokens(cmd, "t"), want, cmd)

    def test_dot_acceptance_files_rejected(self):
        with self.assertRaises(specmod.SpecError):
            mkspec([{"id": "A", "title": "t", "goal": "g", "acceptance": ["true"], "acceptance_files": ["."]}], {"a": ""})

    def test_docker_down_preflight_does_not_escalate(self):
        e = Env([STEP1])
        e.cfg.claude_env.unlink()
        down = e.tmp / "docker-down"
        down.write_text("#!/bin/sh\nexit 1\n")
        down.chmod(0o755)
        e.cfg.docker_bin = str(down)
        r = e.runner()
        with self.assertRaises(SystemExit):
            r.run()
        self.assertNotEqual(r.state.status, "escalation")
