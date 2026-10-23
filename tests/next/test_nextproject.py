"""Phase 2 wiring: stylerefs.process, hands.nextproject, the CLI gate/refs/create commands, the planning chat
(launch, approve, Session manager refusal) and the dashboard helpers. Containers are faked at hands.docker."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
from pathlib import Path

from hands import config as cfgmod
from hands import docker, nextproject, nextstate, planchat, sessions, setup, stylerefs
from hands.config import Config
from hands.state import Lock, State

REPO = Path(__file__).resolve().parents[2]
STYLE = {"schema": 1, "palette": [{"name": "accent", "hex": "#ff5722", "role": "accent"}],
         "fonts": [{"family": "Georgia", "role": "body", "weights": [400]}],
         "typeScale": [{"name": "body", "px": 20, "lineHeight": 1.5}], "spacingScale": [8, 16], "radii": [12],
         "shadows": [], "layout": {"maxWidth": 1200}, "motion": {"animations": [], "libraries": []}, "notes": []}
HEADINGS = ["## Sources", "## Layout and structure", "## Colour", "## Typography", "## Spacing and shape",
            "## Graphics and icons", "## Motion", "## Frames", "## How to apply"]
RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 2,
          "total_cost_usd": 0.01, "session_id": "s"}
ANALYST_VALUE = "analyst-test-value"
BUILD_VALUE = "build-test-value"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = fn(*args, **kw)
    return rc, out.getvalue()


class Project(unittest.TestCase):
    """A project with real git repos and state; docker.run / docker.run_argv replaced by fakes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-nextproject-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        spec, app, state = self.tmp / "spec", self.tmp / "app", self.tmp / "state"
        for d in (spec, app):
            d.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
            git(d, "config", "user.name", "t")
            git(d, "config", "user.email", "t@t")
            (d / "README.md").write_text("# x\n")
            git(d, "add", "-A")
            git(d, "commit", "-qm", "init")
        state.mkdir()
        self.penv = self.tmp / "project.env"
        self.penv.write_text(f"ANALYST_ANTHROPIC_API_KEY={ANALYST_VALUE}\nOTHER=1\n")
        self.claude_env = self.tmp / "claude.env"
        self.claude_env.write_text(f"ANTHROPIC_API_KEY={BUILD_VALUE}\n")
        self.cfg = Config(name="proj", spec_dir=spec, app_dir=app, state_dir=state, image="hands:1", push=False,
                          project_env=self.penv, claude_env=self.claude_env, telegram_env=None,
                          skills_dir=self.tmp / "skills", docker_bin="true")
        self.capdir = self.tmp / "capture"
        (self.capdir / "node_modules").mkdir(parents=True)
        (self.capdir / "capture.mjs").write_text("// fake\n")
        self.captures: list = []
        self.runs: list = []
        self.seen: list[str] = []
        self.capture_rc = 0
        self.analyst_style = STYLE
        for name, fake in (("run", self.fake_run), ("run_argv", self.fake_run_argv)):
            orig = getattr(docker, name)
            setattr(docker, name, fake)
            self.addCleanup(setattr, docker, name, orig)
        orig_process = stylerefs.process
        stylerefs.process = lambda project, index, capture_dir=None: orig_process(project, index, capture_dir or self.capdir)
        self.addCleanup(setattr, stylerefs, "process", orig_process)

    def ref_status(self, index: int) -> str:
        return nextstate.ref(nextstate.load(nextproject.file(self.cfg)), index)["status"]

    def fake_run_argv(self, cfg, argv, name, role, stdout=None, stderr=None, timeout_s=0):
        full = docker.prebuilt_argv(cfg, argv, name, role)
        self.captures.append(full)
        index = int(name.split("-capture-")[1][:2])
        self.seen.append(f"{index:02d} {self.ref_status(index)}")
        out = Path(next(a.split(":")[0] for a in argv if a.endswith(":/out:rw")))
        if self.capture_rc == 3:
            (out / "capture.json").write_text(json.dumps({"schema": 1, "error": "net::ERR_NAME_NOT_RESOLVED"}))
            return 3
        doc = json.loads((REPO / "capture" / "schema.example.json").read_text())
        (out / "capture.json").write_text(json.dumps(doc))
        (out / "frames" / "desktop").mkdir(parents=True)
        (out / "frames" / "desktop" / "step-00.png").write_bytes(b"\x89PNG fake")
        (out / "frames" / "desktop" / "page.html").write_text("<p>site text</p>")
        os.symlink("/etc/passwd", out / "frames" / "desktop" / "step-01.png")
        return self.capture_rc

    def fake_run(self, cfg, r) -> int:
        docker.build_argv(cfg, r)                    # the real builder validates every mount
        self.runs.append(r)
        assert r.role == "analyst", r.role
        index = int(r.name.split("-analyst-")[1][:2])
        self.seen.append(f"{index:02d} {self.ref_status(index)}")
        self.env_text = [Path(f).read_text() for f in r.env_files]
        mounts = {m.dst: m for m in r.mounts}
        out = Path(mounts["/out"].src)
        cap = Path(mounts["/capture"].src)
        self.clean_files = sorted(str(p.relative_to(cap)) for p in cap.rglob("*") if p.is_file() or p.is_symlink())
        host = self.cfg.name
        for ref in nextstate.load(nextproject.file(self.cfg))["style_refs"]:
            if ref["index"] == index:
                host = ref["url"].split("/")[2]
        (out / "report.md").write_text("\n".join([f"# Style reference {index:02d}: {host}", ""] + [f"{h}\nx" for h in HEADINGS]))
        (out / "style.json").write_text(json.dumps(self.analyst_style))
        (out / "frames").mkdir()
        (out / "frames" / "step-00.png").write_bytes(b"\x89PNG fake")
        r.stdout.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {}}]}}) + "\n" + json.dumps(RESULT) + "\n")
        return 0


class Pipeline(Project):
    def test_process_captures_analyses_validates_and_freezes(self):
        nextproject.init(self.cfg, ["https://a.example/page"], 1, [])
        self.assertEqual(quiet(stylerefs.process, self.cfg, 1)[0], "frozen")
        self.assertEqual(self.seen, ["01 capturing", "01 analysing"])
        self.assertEqual(self.ref_status(1), "frozen")
        dest = self.cfg.spec_dir / "refs" / "style-refs" / "01"
        self.assertTrue((dest / "report.md").is_file() and (dest / "style.json").is_file())
        self.assertEqual(git(self.cfg.spec_dir, "log", "-1", "--format=%s"), "style refs: 01 a.example")
        cap = self.captures[0]
        self.assertEqual(cap[:2], ["true", "run"])
        self.assertIn("hands.role=capture", cap)
        self.assertNotIn("--env-file", cap)
        self.assertFalse(any("/run/hands" in a or str(self.penv) in a for a in cap))
        self.assertEqual(cap[cap.index("--network") + 1], "bridge")
        r = self.runs[0]
        self.assertTrue(r.network)
        self.assertEqual(self.env_text, [f"ANTHROPIC_API_KEY={ANALYST_VALUE}\n"])
        self.assertFalse(Path(r.env_files[0]).exists(), "the analyst env file is deleted after the run")
        self.assertNotIn(self.penv, [m.src for m in r.mounts])
        self.assertTrue({m.dst: m.ro for m in r.mounts}["/capture"])
        self.assertEqual(self.clean_files, ["capture.json", "frames/desktop/step-00.png"])
        tools = json.loads((stylerefs.work_dir(self.cfg, 1) / "tools.json").read_text())
        self.assertEqual((tools["role"], tools["tools"]), ("analyst", {"Read": 1}))

    def test_process_failures_are_recorded(self):
        nextproject.init(self.cfg, ["https://a.example/", "https://b.example/", "https://c.example/"], 1, [])
        self.analyst_style = {"schema": 2}
        self.assertEqual(quiet(stylerefs.process, self.cfg, 1)[0], "failed")
        ref = nextstate.ref(nextstate.load(nextproject.file(self.cfg)), 1)
        self.assertTrue(ref["reason"].startswith("invalid analyst output: schema: must be 1"), ref["reason"])
        self.assertFalse((self.cfg.spec_dir / "refs").exists())
        self.capture_rc = 3
        quiet(stylerefs.process, self.cfg, 2)
        self.assertEqual(nextstate.gate(nextstate.load(nextproject.file(self.cfg)))[1], "style ref 02 failed: navigation failed")
        self.capture_rc = 0
        self.penv.write_text("OTHER=1\n")
        quiet(stylerefs.process, self.cfg, 3)
        self.assertEqual(nextstate.ref(nextstate.load(nextproject.file(self.cfg)), 3)["reason"], "analyst token missing")
        self.assertEqual(len(self.runs), 1, "no analyst container without its token")
        shutil.rmtree(self.capdir / "node_modules")
        quiet(stylerefs.process, self.cfg, 3)
        self.assertIn("capture dependencies missing", nextstate.ref(nextstate.load(nextproject.file(self.cfg)), 3)["reason"])


class Cli(Project):
    def main(self, *argv: str) -> tuple[int, str]:
        from hands import __main__ as cli
        orig = cli.load
        cli.load = lambda name: self.cfg
        try:
            return quiet(cli.main, ["proj", *argv])
        finally:
            cli.load = orig

    def test_start_refuses_with_gate_reasons(self):
        nextproject.init(self.cfg, ["https://a.example/", "https://b.example/"], 2, [])
        nextstate.set_ref(nextproject.file(self.cfg), 1, "failed", "analyst token missing")
        rc, out = self.main("start")
        self.assertEqual(rc, 1)
        for line in ("style ref 01 failed: analyst token missing", "style ref 02 queued", "setup pending"):
            self.assertIn(line, out)
        self.assertEqual(State(self.cfg.state_dir).data["setup"]["status"], "pending", "state.json mirrors the choice")

    def test_gate_covers_setup_in_state_json_without_next_json(self):
        st = State(self.cfg.state_dir)
        st.data["setup"] = {"framework": 4, "libs": [], "status": "failed"}
        st.save()
        self.assertEqual(nextproject.gate_reasons(self.cfg), ["setup failed"])
        rc, out = self.main("start")
        self.assertEqual(rc, 1)
        self.assertIn("setup failed", out)

    def test_refs_retry_and_remove(self):
        nextproject.init(self.cfg, ["https://a.example/", "https://b.example/"], 1, [])
        nextstate.set_ref(nextproject.file(self.cfg), 1, "failed", "capture failed (exit 1)")
        rc, out = self.main("refs", "retry", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn("style ref 01 frozen", out)
        self.assertTrue((self.cfg.spec_dir / "refs" / "style-refs" / "01").is_dir())
        self.assertEqual(self.main("refs", "retry", "7")[0], 1)
        self.assertEqual(self.main("refs", "boom", "1")[0], 2)
        self.assertEqual(self.main("refs", "retry", "x")[0], 2)
        rc, out = self.main("refs", "remove", "1")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.cfg.spec_dir / "refs" / "style-refs" / "01").exists())
        self.assertEqual(git(self.cfg.spec_dir, "log", "-1", "--format=%s"), "style refs: remove 01")
        self.assertEqual([r["index"] for r in nextstate.load(nextproject.file(self.cfg))["style_refs"]], [2])
        rc, out = self.main("refs", "add", "https://c.example/")
        self.assertEqual(rc, 0, out)
        self.assertEqual([(r["index"], r["status"]) for r in nextstate.load(nextproject.file(self.cfg))["style_refs"]],
                         [(2, "queued"), (3, "frozen")])
        self.assertEqual(self.main("refs", "add", "javascript:1")[0], 2)
        with Lock(self.cfg.state_dir / "runner.lock"):
            rc, out = self.main("refs", "retry", "2")
        self.assertEqual(rc, 1)
        self.assertIn("running", out)

    def test_after_create_processes_refs_then_runs_setup(self):
        from hands import __main__ as cli
        calls = []
        orig = setup.run
        setup.run = lambda cfg, **kw: calls.append(nextstate.load(nextproject.file(cfg))["style_refs"][0]["status"]) or 0
        self.addCleanup(setattr, setup, "run", orig)
        rc, out = quiet(cli.after_create, self.cfg, ["https://a.example/"], 4, ["gsap"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, ["frozen"], "the scaffold runs after the refs were processed")
        self.assertEqual(State(self.cfg.state_dir).data["setup"]["libs"], ["gsap"])

    def test_change_request_adds_refs_and_new_setup_choices(self):
        from hands import __main__ as cli
        rc, _ = quiet(cli.change_request, self.cfg, ["https://a.example/"], 2, None)     # project from before next.json
        self.assertEqual(rc, 0)
        s = nextstate.load(nextproject.file(self.cfg))
        self.assertEqual([(r["index"], r["status"]) for r in s["style_refs"]], [(1, "frozen")])
        self.assertEqual((s["setup"]["framework"], s["setup"]["status"]), (2, "pending"))
        self.assertEqual(State(self.cfg.state_dir).data["setup"]["status"], "pending")
        self.assertEqual(quiet(cli.change_request, self.cfg, ["https://a.example"], None, None)[0], 2)   # duplicate
        self.assertEqual(quiet(cli.change_request, self.cfg, [], 7, None)[0], 2)

    def test_planner_refuses_while_the_gate_is_blocked(self):
        from hands import planner
        nextproject.init(self.cfg, ["https://a.example/"], 1, [])
        rc, out = quiet(planner.plan, self.cfg)
        self.assertEqual(rc, 1)
        self.assertIn("planning may not start yet: style ref 01 queued", out)

    def test_setup_run_mirrors_into_next_json(self):
        nextproject.init(self.cfg, [], 1, ["gsap"])
        self.assertEqual(quiet(setup.run, self.cfg)[0], 3)               # libraries need a framework
        self.assertEqual(nextstate.load(nextproject.file(self.cfg))["setup"]["status"], "failed")
        self.assertEqual(nextproject.gate_reasons(self.cfg), ["setup failed"])


class PlanChatBase(Project):
    def setUp(self):
        super().setUp()
        self.launched = []
        orig = sessions.start_plan_chat
        sessions.start_plan_chat = lambda sid, project, cwd, argv, on_exit=None: self.launched.append(
            (sid, project, cwd, argv, on_exit)) or {"id": sid}
        self.addCleanup(setattr, sessions, "start_plan_chat", orig)


class PlanChat(PlanChatBase):
    def test_start_then_resume_in_the_firewalled_container(self):
        nextproject.init(self.cfg, [], 1, [])
        res = nextproject.plan_chat_start(self.cfg)
        sid, project, cwd, argv, on_exit = self.launched[0]
        self.assertEqual((res["session_id"], res["status"], project), (sid, "open", "proj"))
        self.assertEqual(argv[:2], ["true", "run"])
        self.assertIn("HANDS_FIREWALL=1", argv)
        self.assertIn("NET_ADMIN", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], sid)
        env_file = Path(argv[argv.index("--env-file") + 1])
        self.assertEqual(env_file.read_text(), f"ANTHROPIC_API_KEY={BUILD_VALUE}\n")
        self.assertFalse(any(str(self.penv) in a for a in argv), "no project.env")
        self.assertIn(f"{self.cfg.spec_dir}:/spec:ro", argv)
        self.assertNotIn("/app", " ".join(argv), "a new project has no app mount")
        on_exit()
        self.assertFalse(env_file.exists(), "the env file goes when the terminal ends")
        p = nextstate.load(nextproject.file(self.cfg))["plan_session"]
        self.assertEqual((p["session_id"], p["status"]), (sid, "open"))
        nextproject.plan_chat_start(self.cfg)
        argv2 = self.launched[1][3]
        self.assertEqual(argv2[argv2.index("--resume") + 1], sid)
        self.launched[1][4]()

    def test_the_chat_mounts_its_own_allowed_domains_file_and_deletes_it_on_exit(self):
        nextproject.init(self.cfg, [], 1, [])
        nextproject.plan_chat_start(self.cfg)
        argv, on_exit = self.launched[0][3], self.launched[0][4]
        mounts = [a for a in argv if a.endswith(f":{planchat.DOMAINS_MOUNT}:ro")]
        self.assertEqual(len(mounts), 1, argv)
        domains_file = Path(mounts[0].split(":")[0])
        self.assertEqual(domains_file.read_text(), "platform.claude.com\n")
        env_file = Path(argv[argv.index("--env-file") + 1])
        on_exit()
        self.assertFalse(domains_file.exists(), "the allowed-domains file goes when the terminal ends")
        self.assertFalse(env_file.exists())

    def test_the_build_wide_oauth_token_starts_the_chat(self):
        """claude.env holds CLAUDE_CODE_OAUTH_TOKEN on this host, not ANTHROPIC_API_KEY."""
        self.claude_env.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\n")
        nextproject.plan_chat_start(self.cfg)
        argv = self.launched[0][3]
        env_file = Path(argv[argv.index("--env-file") + 1])
        self.assertEqual(env_file.read_text(), "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\n")
        self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)
        self.launched[0][4]()
        self.assertFalse(env_file.exists(), "the env file goes when the terminal ends")

    def test_token_missing_names_both_keys(self):
        self.claude_env.write_text("GH_TOKEN=x\n")
        self.penv.write_text("ANALYST_ANTHROPIC_API_KEY=k1\nOTHER=1\n")
        with self.assertRaisesRegex(ValueError, "CLAUDE_CODE_OAUTH_TOKEN") as cm:
            nextproject.plan_chat_start(self.cfg)
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))
        self.assertEqual(self.launched, [])

    def test_token_file_finds_either_key_in_the_project_variables(self):
        self.claude_env.write_text("GH_TOKEN=x\n")
        for text in ("ANTHROPIC_API_KEY=abc\n", "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-x\n"):
            with self.subTest(text=text):
                self.penv.write_text(text)
                self.assertEqual(nextproject.token_file(self.cfg), self.penv)
        self.penv.write_text("OTHER=1\n")
        self.assertEqual(nextproject.token_file(self.cfg), self.claude_env)

    def test_the_chat_mounts_the_planner_skills_so_brainstorming_is_available(self):
        """The primer tells the chat to use the brainstorming skill; image/skills does not carry it, the
        planner's skill set does (hands.planner mounts the same folder)."""
        skills = REPO / "planner-skills"
        self.assertTrue((skills / "brainstorming").is_dir(), "planner-skills/brainstorming is the mounted folder")
        nextproject.plan_chat_start(self.cfg)
        argv = self.launched[0][3]
        mount = f"{skills}:/home/node/.claude/skills:ro"
        self.assertIn(mount, argv)
        self.assertEqual(argv[argv.index(mount) - 1], "-v")
        self.assertEqual(nextproject.chat_skills_dir(self.cfg), skills)

    def test_the_primer_names_the_mounted_skill_folder(self):
        folders = {d.name for d in nextproject.chat_skills_dir(self.cfg).iterdir() if d.is_dir()}
        text = planchat.primer(nextproject.plan_context(self.cfg, nextproject.ensure(self.cfg)))
        self.assertIn("brainstorming", folders)
        self.assertIn("brainstorming skill", text)

    def _stub(self, runner_home: Path, skills_dir: Path | None):
        return types.SimpleNamespace(runner_home=runner_home, skills_dir=skills_dir, state_dir=self.cfg.state_dir)

    def test_the_chat_falls_back_to_the_project_skills_dir_without_planner_skills(self):
        bare = self.tmp / "bare-runner"
        bare.mkdir()
        self.cfg.skills_dir.mkdir()
        self.assertEqual(nextproject.chat_skills_dir(self._stub(bare, self.cfg.skills_dir)), self.cfg.skills_dir)

    def test_the_chat_mounts_an_empty_folder_when_no_skills_exist(self):
        bare = self.tmp / "bare-runner"
        bare.mkdir()
        for skills_dir in (None, self.tmp / "missing-skills"):
            with self.subTest(skills_dir=skills_dir):
                self.assertEqual(nextproject.chat_skills_dir(self._stub(bare, skills_dir)),
                                 self.cfg.state_dir / "plan-chat" / "no-skills")

    def test_brief_ready_and_hash_pinned_approve(self):
        nextproject.plan_chat_start(self.cfg)
        self.launched[0][4]()
        with self.assertRaises(ValueError):
            nextproject.plan_chat_approve(self.cfg, "a" * 64)               # no brief yet
        brief = nextproject.brief_dir(self.cfg)
        (brief / "brief.md").write_text("# Brief: proj\n")
        (brief / "coverage.md").write_text("| r | w |\n")
        view = nextproject.refresh_plan_session(self.cfg)
        self.assertEqual(view["status"], "brief_ready")
        sha = view["brief_sha256"]
        self.assertEqual(nextstate.load(nextproject.file(self.cfg))["plan_session"]["brief_sha256"], sha)
        with self.assertRaises(ValueError):
            nextproject.plan_chat_approve(self.cfg, "b" * 64)
        res = nextproject.plan_chat_approve(self.cfg, sha)
        self.assertEqual((res["sha256"], res["brief_path"]), (sha, "brief.md"))
        self.assertTrue((self.cfg.spec_dir / "brief.md").is_file())
        self.assertEqual(nextproject.plan_session_view(self.cfg)["status"], "approved")
        self.assertEqual(nextproject.change_round(self.cfg), 1)
        (brief / "brief.md").write_text("# Brief: proj\nchanged\n")
        self.assertEqual(nextproject.plan_session_view(self.cfg)["status"], "brief_ready")


class Http(PlanChatBase):
    """The Phase 2 routes through a real dashboard server (auth, origin and X-Hands checks included)."""

    def setUp(self):
        super().setUp()
        from hands import dashboard
        self.dashboard = dashboard
        home = self.tmp / "cfg"
        (home / "projects").mkdir(parents=True)
        orig = cfgmod.CONFIG_HOME
        cfgmod.CONFIG_HOME = home
        self.addCleanup(setattr, cfgmod, "CONFIG_HOME", orig)
        c = self.cfg
        (home / "projects" / "proj.toml").write_text(
            f'spec_dir = "{c.spec_dir}"\napp_dir = "{c.app_dir}"\nstate_dir = "{c.state_dir}"\nimage = "hands:1"\n'
            f'claude_env = "{c.claude_env}"\nproject_env = "{c.project_env}"\ndocker_bin = "true"\nmanaged = true\n')
        self.spawned = []
        orig_spawn = dashboard.spawn_cli
        dashboard.spawn_cli = lambda log, argv, stdin_text="": self.spawned.append(argv)
        self.addCleanup(setattr, dashboard, "spawn_cli", orig_spawn)
        self.srv = dashboard.Server(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        st, hdr, _ = self.req("GET", f"/login?c={dashboard.mint_login_code()}", auth=False)
        self.assertEqual(st, 303)
        self.cookie = hdr["Set-Cookie"].split(";")[0]

    def req(self, method, path, body=None, auth=True, xh=True):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}"}
        if auth:
            h["Cookie"] = self.cookie
        if xh:
            h["X-Hands"] = "1"
        c.request(method, path, headers=h, body=json.dumps(body).encode() if body is not None else b"")
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), raw

    def call(self, method, path, body=None, **kw):
        st, _, raw = self.req(method, path, body, **kw)
        return st, json.loads(raw or b"{}")

    def test_routes(self):
        nextproject.init(self.cfg, ["https://a.example/"], 1, [])
        base = "/api/projects/proj"
        st, r = self.call("POST", f"{base}/style-refs", {"urls": ["https://b.example"]})
        self.assertEqual((st, r["urls"]), (200, ["https://b.example/"]))
        self.assertEqual(self.spawned[-1], ["proj", "refs", "add", "https://b.example/"])
        self.assertEqual(self.call("POST", f"{base}/style-refs", {"urls": ["https://a.example"]})[0], 400)
        self.assertEqual(self.call("POST", f"{base}/style-refs", {"urls": ["javascript:1"]})[0], 400)
        self.assertEqual(self.call("POST", f"{base}/style-refs/1/retry")[0], 200)
        self.assertEqual(self.spawned[-1], ["proj", "refs", "retry", "1"])
        self.assertEqual(self.call("POST", f"{base}/style-refs/5/retry")[0], 404)
        self.assertEqual(self.call("DELETE", f"{base}/style-refs/1", xh=False)[0], 403)
        self.assertEqual(self.call("DELETE", f"{base}/style-refs/1", auth=False)[0], 401)
        self.assertEqual(self.call("DELETE", f"{base}/style-refs/1")[0], 200)
        self.assertEqual(self.spawned[-1], ["proj", "refs", "remove", "1"])
        self.assertEqual(self.call("POST", f"{base}/setup/retry")[0], 200)
        self.assertEqual(self.spawned[-1], ["proj", "setup"])
        with Lock(self.cfg.state_dir / "runner.lock"):
            self.assertEqual(self.call("POST", f"{base}/setup/retry")[0], 409)
        self.assertEqual(self.call("POST", "/api/projects/nope/setup/retry")[0], 404)
        self.assertEqual(len(self.spawned), 4)
        st, r = self.call("POST", f"{base}/plan-chat")
        self.assertEqual((st, r["session_id"], r["status"]), (200, self.launched[0][0], "open"))
        self.launched[0][4]()
        st, r = self.call("POST", f"{base}/plan-chat/approve", {"sha256": "a" * 64})
        self.assertEqual(st, 400)
        self.assertIn("brief", r["error"])
        st, r = self.call("GET", "/api/p/proj")
        self.assertEqual(st, 200)
        self.assertEqual(r["next"]["gate"], ["style ref 01 queued"])
        self.assertEqual(r["next"]["plan_session"]["status"], "open")

    def test_create_and_amend_pass_the_choices_to_the_cli(self):
        self.assertEqual(self.dashboard.next_flags({"style_refs": [], "framework": 2}), ["--framework", "2"])
        self.assertEqual(self.call("POST", "/api/new", {"slug": "other", "description": "x", "style_refs": ["ftp://x"]})[0], 400)
        self.assertEqual(self.call("POST", "/api/p/proj/action/amend", {"feedback": "more", "libs": ["jquery"]})[0], 400)
        self.assertEqual(self.spawned, [])


class SessionManagerRefusesPlanChat(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-planchat-sess-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        orig = cfgmod.CONFIG_HOME
        cfgmod.CONFIG_HOME = self.tmp / "cfg"
        self.addCleanup(setattr, cfgmod, "CONFIG_HOME", orig)

    def test_resume_never_runs_a_plan_chat_as_a_host_claude(self):
        sid = sessions.new_id()
        rec = sessions.Store().create_plan_chat(sid, "proj", self.tmp)
        with self.assertRaises(sessions.PlanChatSession) as cm:
            sessions.resume(sid)
        self.assertEqual(cm.exception.project, "proj")
        with self.assertRaises(sessions.PlanChatSession):
            sessions.BACKEND.start(rec, resume=True)
        self.assertFalse(sessions.BACKEND.alive(sid))
        row = sessions.Store().row(rec)
        self.assertEqual((row["role"], row["project"], row["hidden"]), ("plan-chat", "proj", True))

    def test_start_plan_chat_runs_the_given_argv_and_cleans_up(self):
        sid = sessions.new_id()
        done = threading.Event()
        row = sessions.start_plan_chat(sid, "proj", self.tmp, ["/bin/sh", "-c", "exit 0"], on_exit=done.set)
        self.assertTrue(row["hidden"])
        self.assertTrue(done.wait(10), "on_exit runs when the terminal ends")
        with self.assertRaises(sessions.Conflict):
            plain = sessions.Store().create(self.tmp)
            sessions.start_plan_chat(plain["id"], "proj", self.tmp, ["/bin/true"])

    def test_dashboard_resume_routes_to_the_project(self):
        from hands import dashboard
        sid = sessions.new_id()
        rec = sessions.Store().create_plan_chat(sid, "proj", self.tmp)
        calls = []
        orig = dashboard.plan_chat
        dashboard.plan_chat = lambda name: calls.append(name) or {"session_id": sid}
        self.addCleanup(setattr, dashboard, "plan_chat", orig)
        self.assertEqual(dashboard.plan_chat_resume(rec)["id"], sid)
        self.assertEqual(calls, ["proj"])
        with self.assertRaises(ValueError):
            dashboard.plan_chat_resume(rec, telegram=True)


class DashboardCreate(unittest.TestCase):
    """start_create: "Create project" runs the planner after create; "Create and plan with Claude"
    (plan_with_claude) does not -- the dashboard opens the planning chat instead."""

    def setUp(self):
        from hands import dashboard
        self.dashboard = dashboard
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-dash-create-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        orig = cfgmod.CONFIG_HOME
        cfgmod.CONFIG_HOME = self.tmp / "cfg"
        self.addCleanup(setattr, cfgmod, "CONFIG_HOME", orig)
        self.argvs = []
        test = self

        class FakePopen:
            def __init__(self, argv, **kw):
                test.argvs.append(argv)
                self.stdin = io.StringIO()

            def wait(self):
                return 0

        orig_popen = dashboard.subprocess.Popen
        dashboard.subprocess.Popen = FakePopen
        self.addCleanup(setattr, dashboard.subprocess, "Popen", orig_popen)

    def test_plan_with_claude_skips_the_planner(self):
        req = {"style_refs": ["https://a.example"], "framework": 2, "libs": ["gsap"]}
        self.dashboard.start_create("one", "an idea", req=req)
        self.dashboard.start_create("two", "an idea", req={**req, "plan_with_claude": True})
        self.dashboard.start_create("three", "an idea", req={**req, "plan_with_claude": "yes"})
        tail = ["--style-ref", "https://a.example/", "--framework", "2", "--libs", "gsap"]
        self.assertEqual(self.argvs[0][-9:], ["one", "create", "--then-plan", *tail])
        self.assertEqual(self.argvs[1][-8:], ["two", "create", *tail])
        self.assertNotIn("--then-plan", self.argvs[1])
        self.assertIn("--then-plan", self.argvs[2])


class DashboardHelpers(unittest.TestCase):
    def test_next_flags(self):
        from hands import dashboard
        self.assertEqual(dashboard.next_flags({}), [])
        self.assertEqual(dashboard.next_flags({"style_refs": ["https://a.example"], "framework": "4", "libs": ["gsap", "shadcn"]}),
                         ["--style-ref", "https://a.example/", "--framework", "4", "--libs", "shadcn,gsap"])
        for bad in ({"style_refs": ["javascript:1"]}, {"framework": 9}, {"libs": ["jquery"]}, {"style_refs": "x"}):
            with self.assertRaises(ValueError):
                dashboard.next_flags(bad)

    def test_ref_index(self):
        from hands import dashboard
        self.assertEqual(dashboard._ref_index("7"), 7)
        for bad in ("0", "100", "x", "-1", "01"):
            with self.assertRaises(LookupError):
                dashboard._ref_index(bad)


if __name__ == "__main__":
    unittest.main()
