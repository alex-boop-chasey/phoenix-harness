"""The outer loop: one spec step per fresh coder session, checked by the runner and an independent evaluator.

Trust model: everything the agent wrote, and everything agent-written code can touch while it runs
(workspace, its .git, caches, files in mounted dirs, its own process), is untrusted. The runner trusts
only: container exit codes, files it writes itself on the host, and git objects in its own clones.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import time
from pathlib import Path

from . import accview, activity, claude, docker, gitops, hooks, merge, projectenv, prompts, rounds, usage, workspace
from . import spec as specmod
from .config import Config
from .docker import Mount, Run
from .notify import Notifier
from .state import Lock, State

INFRA = ("rate_limit", "auth", "model_access", "overloaded")
MAX_UNCOUNTED = 10        # infra retries per step before escalating (rate-limit waits excepted)


class Stop(Exception):
    """End the loop (escalation, STOP file, caps, done). Message already logged."""


def _term(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


GENERIC = {"ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL", "ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL", "ELIFECYCLE"}


def failure_signature(reasons: list[str]) -> str:
    """Fingerprint of a check failure, for 'same failure twice in a row -> stop and ask'.
    Only SPECIFIC markers count (a file:line compile error, a named failing test, a specific pnpm error
    code); generic wrappers and timings are ignored. Empty -> never escalate early on this."""
    if not reasons or not reasons[0].startswith(("acceptance command failed", "regression: acceptance")):
        return ""
    import re
    tail = "\n".join(reasons[1:])
    marks = set()
    marks |= {m for m in re.findall(r"ERR_[A-Z0-9_]+", tail) if m not in GENERIC}
    marks |= set(re.findall(r"[\w./@-]+\.\w+[(:]\d+[,:]\d+\)?:? -? ?error (?:TS|ts\()\d+\)?[^\n]{0,160}", tail))
    marks |= set(re.findall(r"error TS\d+:[^\n]{0,160}", tail))
    marks |= {re.sub(r"\s*\(\d+(?:\.\d+)?m?s\)", "", m) for m in re.findall(r"(?:✘|›)[^\n]{3,200}", tail)}
    marks |= set(re.findall(r"FAIL\s+[\w./@-]+\.\w+[^\n]{0,160}", tail))
    marks = {re.sub(r"\d+(?:\.\d+)?\s?m?s\b", "", m).strip() for m in marks}
    marks.discard("")
    if not marks:
        return ""
    return reasons[0] + "\n" + "\n".join(sorted(marks))


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.pnpm_volume:
            cfg.pnpm_volume = f"hands-{cfg.name}-coder-store"
        self.state = State(cfg.state_dir)
        self.notify = Notifier(cfg.telegram_env, cfg.name, log=self.state.log)
        self.sessions = 0
        self.cost = 0.0
        self.transient = 0
        self.uncounted: dict[str, int] = {}
        self.sleep = time.sleep          # patched in tests
        self.clock = time.time
        self.spec: specmod.Spec | None = None
        self.acc_view: Path | None = None     # per-step copy of the visible acceptance files
        self.spec_problem = ""
        self.penv: projectenv.Store | None = None
        self.redactor = projectenv.Redactor({})
        self.load_project_env()

    # ------------------------------------------------------------------ helpers
    @property
    def stop_file(self) -> Path:
        return self.cfg.state_dir / "STOP"

    @property
    def branch(self) -> str:
        """The app branch the runner builds and pushes: the open change round's, else cfg.branch."""
        return rounds.active_branch(self.state.data, self.cfg.branch)

    @property
    def round_id(self) -> int:
        r = rounds.record(self.state.data)
        return int(r["id"]) if r and r.get("status") == "open" else 0

    def regression_steps(self) -> list[specmod.Step]:
        """In a change round every passed step's acceptance runs again for each new step (regression rule).
        Round 0 (fresh plan) keeps the spec's own `regression` list only."""
        if not self.spec or not self.round_id:
            return []
        return [s for s in self.spec.steps if self.state.passed(s.id) and s.type == "code"]

    def load_project_env(self) -> None:
        """(Re)read project.env: the dashboard may change it between steps. Secret values feed the redactor,
        which is applied to everything the runner logs, stores or sends."""
        self.penv = None
        p = self.cfg.project_env
        if p and p.is_file():
            try:
                self.penv = projectenv.Store(p.parent, p.name)
            except projectenv.EnvError as e:
                self.state.log(f"WARN: project_env unreadable: {e}")
        self.redactor = projectenv.Redactor(self.penv.secrets() if self.penv else {})

    def env_names(self) -> list[tuple[str, bool]] | None:
        return self.penv.names() if self.penv else None

    def send(self, msg: str) -> bool:
        return self.notify.send(self.redactor.text(msg))

    def redact_files(self, *paths: Path) -> None:
        for p in paths:
            try:
                self.redactor.file(p)
            except OSError as e:
                self.state.log(f"WARN: could not redact {p.name}: {e}")

    @property
    def model(self) -> str:
        return self.state.data.get("model_override") or self.cfg.coder_model

    @property
    def eval_model(self) -> str:
        return self.state.data.get("model_override") or self.cfg.eval_model

    def log(self, msg: str) -> None:
        self.state.log(self.redactor.text(msg))

    def escalate(self, reason: str) -> None:
        reason = self.redactor.text(reason)
        self.state.set_status("escalation", reason)
        self.log(f"ESCALATION: {reason}")
        self.send(f"ESCALATION: {reason[:1500]}\nFix, then: hands {self.cfg.name} ack")
        raise Stop(reason)

    def nap(self, seconds: float) -> None:
        """Sleep in slices so a STOP file is honoured promptly."""
        end = self.clock() + seconds
        while True:
            if self.stop_file.exists():
                self.state.set_status("stopped", "STOP file")
                self.log("STOP file found while waiting")
                raise Stop("STOP")
            left = end - self.clock()
            if left <= 0:
                return
            self.sleep(min(30, left))

    def common_env(self, role: str) -> dict[str, str]:
        env = {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
            "npm_config_store_dir": "/pnpm-store",
        }
        # the coder's cache is persistent (speed); acceptance/eval get a fresh volume per attempt,
        # and corepack's pnpm binary always lives in the container's own home, never in a shared volume
        env["COREPACK_HOME"] = "/home/node/.cache/corepack"
        if role in ("coder", "accept", "eval"):
            env["CI"] = "1"           # same package-manager policies in the coder's session as in the checks
        return env

    def mounts(self, work: Path, *, store: str, with_skills: bool = False) -> list[Mount]:
        assert self.spec
        m = docker.base_mounts(self.cfg, self.spec.root)
        m.append(Mount(self.cfg.runner_home / "prompts", "/opt/hands/prompts"))
        m.append(Mount(work, "/workspace", ro=False))
        if self.acc_view is not None and self.acc_view.is_dir():
            m.append(Mount(self.acc_view, "/workspace/acceptance"))
        m.append(Mount(store, "/pnpm-store", ro=False))
        if with_skills and self.cfg.skills_dir and self.cfg.skills_dir.is_dir():
            m.append(Mount(self.cfg.skills_dir, "/home/node/.claude/skills"))
        if self.cfg.project_env and self.cfg.project_env.is_file():       # coder, acceptance, evaluator
            m.append(Mount(self.cfg.project_env, projectenv.CONTAINER_ENV_PATH))
        return m

    def hook_args(self, role: str) -> list[str]:
        """Read-only mounts for the role's Claude Code hooks: the harness hooks/ directory and a settings
        file written into project state (the agent cannot change hook config). [] for roles without hooks,
        and, with a warning, when the installed runner has no hooks/ directory."""
        if not hooks.settings_for(role):
            return []
        hooks_dir = hooks.hooks_dir()
        if not hooks_dir.is_dir():
            self.log(f"{role} runs without the format hook: {hooks_dir} is missing")
            return []
        settings = hooks.write_settings(role, self.cfg.state_dir / hooks.SETTINGS_FILE_NAME)
        return hooks.mount_args(role, hooks_dir, settings)

    def env_files(self, *, claude_token: bool) -> list[Path]:
        files = [self.cfg.claude_env] if claude_token else []
        if self.cfg.project_env:
            files.append(self.cfg.project_env)
        return files

    def claude_cmd(self, *, model: str, system_file: str, max_turns: int, extra: list[str]) -> list[str]:
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
               "--max-turns", str(max_turns), "--dangerously-skip-permissions",
               "--no-session-persistence",
               "--append-system-prompt-file", f"/opt/hands/prompts/{system_file}"]
        if self.cfg.fallback_model and self.cfg.fallback_model != model:
            cmd += ["--fallback-model", self.cfg.fallback_model]
        return cmd + extra

    def record_session(self, role: str, step: str, attempt: int, res: claude.Result, dur: float) -> None:
        self.sessions += 1
        self.cost += res.cost
        self.state.data["total_sessions"] = self.state.data.get("total_sessions", 0) + 1
        self.state.data["total_cost_usd"] = round(self.state.data.get("total_cost_usd", 0.0) + res.cost, 4)
        self.state.save()
        self.state.append_jsonl("metrics.jsonl", {
            "role": role, "step": step, "attempt": attempt, "kind": res.kind, "rc": res.rc,
            "turns": res.turns, "cost_usd": res.cost, "models": res.models, "duration_s": round(dur),
            "spec_commit": self.spec.commit if self.spec else "",
        })
        self.log(f"{role} {step}#{attempt}: kind={res.kind} rc={res.rc} turns={res.turns} cost={res.cost:.3f} "
                 f"models={','.join(res.models)} dur={dur:.0f}s")

    def free_gb(self) -> float:
        return shutil.disk_usage(self.cfg.state_dir).free / 1e9

    # ------------------------------------------------------------------ preflight
    def preflight(self) -> None:
        c = self.cfg
        problems = []
        if not docker.image_exists(c):
            problems.append(f"docker image {c.image} not found")
        if not c.claude_env.is_file():
            problems.append(f"{c.claude_env} missing")
        else:
            if c.claude_env.stat().st_mode & 0o077:
                problems.append(f"{c.claude_env} must be chmod 600")
            for line in c.claude_env.read_text().splitlines():
                if "=" in line and line.split("=", 1)[1][:1] in "\"'":
                    problems.append(f"{c.claude_env}: remove quotes around values (docker --env-file keeps them)")
        for d in (c.spec_dir, c.app_dir):
            if not (d / ".git").exists():
                problems.append(f"{d} is not a git clone")
        if c.project_env and not c.project_env.is_file():
            problems.append(f"project_env {c.project_env} missing")
        elif c.project_env and (c.project_env.stat().st_mode & 0o077 or c.project_env.is_symlink()):
            problems.append(f"project_env {c.project_env} must be a regular file with mode 0600")
        elif c.project_env:
            try:
                for prob in projectenv.Store(c.project_env.parent, c.project_env.name).problems:
                    if "reserved" in prob:          # e.g. GH_TOKEN=... written by hand: never ship it to containers
                        problems.append(f"project_env {c.project_env}: {prob}")
            except projectenv.EnvError as e:
                problems.append(f"project_env {c.project_env}: {e}")
        if os.getuid() == 0 and not os.environ.get("HANDS_TEST_ALLOW_ROOT"):
            problems.append("run the runner as builder, not root")
        c.state_dir.mkdir(parents=True, exist_ok=True)
        if self.free_gb() < c.min_free_gb:
            problems.append(f"only {self.free_gb():.1f} GB free (min_free_gb={c.min_free_gb}); try: docker system prune")
        if problems:
            raise SystemExit("preflight failed:\n  " + "\n  ".join(problems))

    # ------------------------------------------------------------------ main loop
    def run(self, max_steps: int | None = None) -> int:
        try:
            self.preflight()
        except SystemExit as e:
            # recorded so the watchdog does not restart into the same failure forever
            # docker not up yet (reboot) is transient: exit and let the watchdog retry (it caps restarts)
            if docker.reachable(self.cfg) and not Lock.held(self.cfg.state_dir / "runner.lock"):
                self.state.set_status("escalation", str(e)[:1000])
                self.send(f"ESCALATION: {str(e)[:1000]}\nFix, then: hands {self.cfg.name} ack")
            raise
        old = {s: signal.signal(s, _term) for s in (signal.SIGTERM, signal.SIGHUP)}
        try:
            with Lock(self.cfg.state_dir / "runner.lock"):
                return self._run(max_steps)
        finally:
            for s, h in old.items():
                signal.signal(s, h)

    def _run(self, max_steps: int | None) -> int:
        if self.stop_file.exists():
            self.log(f"STOP file present ({self.stop_file}); remove it to run")
            return 1
        if self.state.status == "escalation":
            self.log(f"status is escalation: {self.state.data.get('reason')}\nresolve, then: hands {self.cfg.name} ack")
            return 2
        done_steps = 0
        try:
            n = docker.cleanup_orphans(self.cfg)
            if n:
                self.log(f"removed {n} orphaned container(s)")
            docker.cleanup_volumes(self.cfg)
            self.recover()
            self.state.set_status("running", "")
            self.send(f"runner started (coder {self.model}, image {self.cfg.image})")
            while True:
                if self.stop_file.exists():
                    self.state.set_status("stopped", "STOP file")
                    self.log("STOP file present; stopping")
                    self.send("stopped: STOP file")
                    return 0
                if self.sessions >= self.cfg.max_sessions:
                    self.state.set_status("stopped", f"session cap {self.cfg.max_sessions}")
                    self.log("session cap reached")
                    self.send(f"stopped: session cap {self.cfg.max_sessions} reached")
                    return 0
                if self.cfg.cost_cap_usd and self.cost >= self.cfg.cost_cap_usd:
                    self.state.set_status("stopped", "cost cap")
                    self.send(f"stopped: cost cap ${self.cfg.cost_cap_usd} reached (estimate)")
                    return 0
                if self.free_gb() < self.cfg.min_free_gb:
                    self.escalate(f"disk low: {self.free_gb():.1f} GB free; run docker system prune / clear {self.cfg.state_dir}/attempts")
                step = self.next_step()
                if step is None:
                    self.state.set_status("done", "all steps passed")
                    self.log("DONE: all steps passed")
                    self.send("DONE: all steps passed")
                    return 0
                if max_steps is not None and done_steps >= max_steps:
                    self.state.set_status("idle", f"--steps {max_steps} reached")
                    return 0
                if self.attempt(step):
                    done_steps += 1
        except Stop:
            return 2 if self.state.status == "escalation" else 0
        except KeyboardInterrupt as e:
            self.state.set_status("stopped", f"interrupted ({e})")
            self.log(f"interrupted ({e}); the next run recovers the in-flight attempt")
            self.send(f"stopped: interrupted ({e})")
            return 130
        except Exception as e:
            self.state.set_status("escalation", f"runner error: {type(e).__name__}: {e}")
            self.log(f"RUNNER ERROR: {type(e).__name__}: {e}")
            self.send(f"RUNNER ERROR: {type(e).__name__}: {str(e)[:800]}")
            raise

    def recover(self) -> None:
        """A previous run died mid-attempt: drop its unaccepted local commit/changes before anything can push them."""
        cur = self.state.data.get("current")
        if not cur:
            return
        app = self.cfg.app_dir
        h = gitops.head(app)
        if h != cur["base"]:
            gitops.git(app, "tag", "-f", f"hands/interrupted/{cur['step']}-{cur['attempt']}-{int(time.time())}", h, check=False)
        gitops.hard_reset(app, cur["base"])
        for d in ("work", "verify"):
            workspace.rmtree(self.cfg.state_dir / d)
        adir = self.cfg.state_dir / "attempts" / f"{cur['step']}-{cur['attempt']}"
        if adir.is_dir() and not adir.is_symlink():       # the session died before its files were redacted
            self.redact_files(*(p for p in adir.iterdir() if p.is_file() and not p.is_symlink()))
        self.state.data.pop("current", None)
        self.state.save()
        self.log(f"recovered from interrupted {cur['step']} attempt {cur['attempt']} (phase {cur.get('phase')}); reset to {cur['base'][:9]}")

    def _net(self, what: str, fn):
        """Network git operation with retries; escalates after 3 failures."""
        for n in range(1, 4):
            try:
                return fn()
            except gitops.GitError as e:
                self.log(f"{what} failed ({e}); retry {n}/3")
                if n == 3:
                    self.escalate(f"{what} failed 3 times: {e}")
                self.nap(60 * n)

    def check_passed_records(self) -> None:
        """Passed step entries and their owned acceptance files are immutable: re-check every record with a hash
        against the spec just loaded (a hand-merged spec PR cannot bypass the amend validator this way)."""
        assert self.spec
        known = {s.id for s in self.spec.steps}
        bad = []
        for sid, s in sorted(self.state.data["steps"].items()):
            if s.get("status") != "passed" or (sid not in known and not s.get("spec_hash")):
                continue                        # records without a hash (built before 0.5.7) only get the WARN below
            why = rounds.check_record(self.spec.root, sid, s)
            if why:
                bad.append(why)
        if bad:
            self.escalate("passed step(s) changed in the spec since they passed (amendments only append; "
                          "restore them, or reset-step to rebuild): " + " | ".join(bad[:5]))

    def open_round(self, rnd: int) -> None:
        """Branch change/C<rnd> from origin/<cfg.branch> and tag hands/pre-C<rnd> at that base, both pushed, then
        the record. Every part is idempotent, so a crash in the middle just re-runs it next time."""
        c, app = self.cfg, self.cfg.app_dir
        name, tag = rounds.branch_name(rnd), rounds.pre_tag(rnd)
        cur = rounds.record(self.state.data)
        if cur and cur.get("status") == "open":
            self.escalate(f"round C{cur['id']} is built on {cur.get('branch')} but not merged; merge it before "
                          f"round C{rnd} can build")

        def op() -> str:
            if c.push:
                gitops.git(app, "fetch", "-q", "origin", c.branch)
                base = gitops.git(app, "rev-parse", "FETCH_HEAD").strip()
                remote_branch = gitops.remote_sha(app, f"refs/heads/{name}")
                remote_tag = gitops.remote_sha(app, f"refs/tags/{tag}")
            else:
                base = gitops.git(app, "rev-parse", c.branch).strip()
                remote_branch = remote_tag = ""
            if remote_tag and remote_tag != base:
                raise rounds.RoundError(f"tag {tag} already exists on origin at {remote_tag[:9]}, not at the base "
                                        f"{base[:9]}; a round C{rnd} was opened before (never moved by the runner)")
            if remote_branch:
                gitops.git(app, "fetch", "-q", "origin", name)
                gitops.git(app, "checkout", "-q", "-B", name, "FETCH_HEAD")
            else:
                gitops.git(app, "checkout", "-q", "-B", name, base)
            local_tag = gitops.git(app, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False).strip()
            if local_tag and local_tag != base:
                raise rounds.RoundError(f"tag {tag} exists locally at {local_tag[:9]}, not at the base {base[:9]}")
            if not local_tag:
                gitops.git(app, "tag", tag, base)
            if c.push:
                if not remote_tag:
                    gitops.push_ref(app, f"refs/tags/{tag}", f"refs/tags/{tag}")
                if not remote_branch:
                    gitops.push_ref(app, name, f"refs/heads/{name}")
            return base

        if c.push:
            self.sync_branch(c.branch)          # accepted round-0 commits whose push failed go to origin first
        try:
            base = self._net(f"round C{rnd} open", op)
        except rounds.RoundError as e:
            self.escalate(str(e))
        self.state.data["round"] = {"id": rnd, "branch": name, "base_sha": base, "pre_tag": tag,
                                    "spec_commit": self.spec.commit if self.spec else "",
                                    "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "open",
                                    **self.round_request(rnd)}
        self.state.save()
        merge.append_event(self.state, "opened", self.state.data["round"])
        self.log(f"opened round C{rnd}: branch {name} from {base[:9]}, tag {tag}" + ("" if c.push else " (push disabled)"))
        self.send(f"round C{rnd} opened on {name} (base {base[:9]}, tag {tag})")

    def round_request(self, rnd: int) -> dict:
        """The amendment's description and PR for the round record, from the planner's plan.json (round n of
        amend mode); tolerant: a hand-written spec has none."""
        try:
            pj = json.loads((self.cfg.state_dir / "plan" / "plan.json").read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(pj, dict) or pj.get("mode") != "amend" or pj.get("round") != rnd:
            return {}
        out = {}
        if isinstance(pj.get("request"), str) and pj["request"].strip():
            out["request"] = pj["request"].strip()[:400]
        if isinstance(pj.get("pr"), int):
            out["pr"] = pj["pr"]
        if isinstance(pj.get("pr_url"), str):
            out["pr_url"] = pj["pr_url"][:300]
        return out

    def sync_branch(self, branch: str) -> None:
        """Make the app clone consistent with origin/<branch> (push accepted commits, fast-forward, escalate on
        divergence); retried, escalates when the push keeps failing."""
        c = self.cfg
        for tries in range(1, 4):
            st, detail = self._net("app sync", lambda: gitops.sync_app(c.app_dir, branch, c.push))
            if st in ("ok", "pushed"):
                if st == "pushed":
                    self.log(f"pushed previously accepted commits {detail}".strip())
                return
            if st == "diverged":
                self.escalate(f"app repo {detail}; reconcile by hand (the runner never force-pushes)")
            self.log(f"push failed ({detail}); retry {tries}/3")
            if tries == 3:
                self.escalate(f"cannot push accepted work to origin: {detail}")
            self.nap(60 * tries)

    def next_step(self) -> specmod.Step | None:
        c = self.cfg
        commit = self._net("spec fetch", lambda: gitops.refresh_spec(c.spec_dir, c.spec_branch))
        try:
            self.spec = specmod.load(c.spec_dir, commit)
        except specmod.SpecError as e:
            self.escalate(f"spec invalid at {commit[:9]}: {e}")
        self.check_passed_records()
        pending = next((s for s in self.spec.steps if not self.state.passed(s.id)), None)
        if pending is not None:
            want = rounds.round_of(pending.id)
            cur = rounds.record(self.state.data)
            if want > (int(cur["id"]) if cur else 0):
                self.open_round(want)
        self.sync_branch(self.branch)
        assert self.spec
        known = {s.id for s in self.spec.steps}
        for sid, s in self.state.data["steps"].items():
            if s.get("status") == "passed" and sid not in known:
                self.log(f"WARN: state has passed step {sid} that is no longer in the spec")
        for s in self.spec.steps:
            if not self.state.passed(s.id):
                if s.type == "human":
                    self.escalate(f"HUMAN STEP {s.id}: {s.title}\n{s.goal}\nWhen done: hands {c.name} mark-done {s.id}")
                return s
        return None

    def attempt_dir(self, sid: str, attempt: int) -> Path:
        adir = self.cfg.state_dir / "attempts" / f"{sid}-{attempt}"
        if adir.exists() and any(adir.iterdir()):
            # an earlier uncounted try (infra error, escalation) used this number: keep its logs
            adir.rename(adir.with_name(f"{adir.name}.{int(time.time())}"))
        adir.mkdir(parents=True, exist_ok=True)
        return adir

    # ------------------------------------------------------------------ one attempt
    def attempt(self, step: specmod.Step) -> bool:
        """Returns True if the step passed."""
        c, spec = self.cfg, self.spec
        assert spec
        st = self.state.step(step.id)
        attempt = st["attempts"] + 1
        self.spec_problem = ""
        max_attempts = step.max_attempts or c.default_max_attempts
        adir = self.attempt_dir(step.id, attempt)
        base = gitops.head(c.app_dir)
        work = c.state_dir / "work"
        self.state.data["current"] = {"step": step.id, "attempt": attempt, "base": base, "phase": "coder"}
        self.state.save()
        self.load_project_env()
        self.log(f"=== {step.id} attempt {attempt}/{max_attempts}: {step.title} (base {base[:9]}, spec {spec.commit[:9]})")

        # ---- acceptance view: shared + passed steps' + this step's files only
        passed = {sid for sid in self.state.data["steps"] if self.state.passed(sid)}
        self.acc_view = c.state_dir / "acc-view"
        acc_files = accview.build(spec, step, passed, self.acc_view)
        (adir / "acceptance-files.txt").write_text("".join(f"acceptance/{f}\n" for f in acc_files))

        # ---- coder session
        workspace.make_work(c.app_dir, work)
        progress = self.state.read_jsonl("progress.jsonl")[-c.progress_notes:]
        prev_diff = ""
        prev = c.state_dir / "attempts" / f"{step.id}-{attempt - 1}" / "diff.patch"
        if attempt > 1 and prev.exists():
            prev_diff = prompts.truncate(prev.read_text(errors="replace"), 20000)
        (adir / "prompt.md").write_text(prompts.coder(
            spec, step, attempt=attempt, max_attempts=max_attempts, progress=progress,
            git_log=gitops.log_oneline(c.app_dir), feedback=st.get("reasons", []), prev_diff=prev_diff,
            acc_files=acc_files, env_vars=self.env_names(), imported=c.imported,
            regression_steps=self.regression_steps(), round_id=self.round_id))
        agents = json.loads((c.runner_home / "prompts" / "agents.json").read_text())
        cmd = self.claude_cmd(model=self.model, system_file="coder.md", max_turns=c.max_turns,
                              extra=["--agents", json.dumps(agents), "--disallowedTools", "WebFetch"])
        t0 = time.time()
        (adir / "coder.json").write_bytes(b"")
        with activity.Tail(adir / "coder.json", self.on_activity):
            rc = docker.run(c, Run(
                name=docker.unique_name(c, "coder", f"{step.id}-{attempt}"), role="coder", cmd=cmd,
                mounts=self.mounts(work, store=c.pnpm_volume, with_skills=True), env=self.common_env("coder"),
                env_files=self.env_files(claude_token=True), extra_args=self.hook_args("coder"),
                stdin=adir / "prompt.md", stdout=adir / "coder.json", stderr=adir / "coder.stderr",
                timeout_s=c.session_timeout_min * 60, append_stdout=True))
        self.redact_files(adir / "coder.json", adir / "coder.stderr")     # before anything parses or serves them
        res = claude.parse(adir / "coder.json", rc, adir / "coder.stderr")
        self.record_session("coder", step.id, attempt, res, time.time() - t0)

        if res.kind in INFRA:
            workspace.rmtree(work)
            self.state.data.pop("current", None)
            self.state.save()
            self.count_uncounted(step.id, res)
            self.handle_infra(res)
            return False          # not counted as an attempt
        self.transient = 0
        summary = claude.parse_summary(res.text)
        spec_problem = claude.parse_tagged(res.text, "SPEC_PROBLEM")
        self.spec_problem = spec_problem
        if spec_problem:
            (adir / "spec-problem.txt").write_text(spec_problem + "\n")
            self.log(f"coder reports a SPEC_PROBLEM: {spec_problem[:300]}")
        if res.kind != "ok":
            self.log(f"coder ended with {res.kind}; checking whatever it left in the workspace")

        # ---- export + allowlist
        self.set_phase("check")
        out = adir / "export"
        workspace.rmtree(out)
        out.mkdir()
        rc = docker.run(c, Run(
            name=docker.unique_name(c, "export", f"{step.id}-{attempt}"), role="export",
            cmd=["bash", "-c", workspace.EXPORT_SCRIPT], network=False,
            mounts=[*docker.base_mounts(c, spec.root), Mount(work, "/workspace", ro=True), Mount(out, "/out", ro=False)],
            stderr=adir / "export.stderr", timeout_s=600))
        workspace.rmtree(work)          # nothing below reads the agent's workspace again
        if rc != 0:
            return self.fail(step, attempt, max_attempts, base, adir,
                             [f"could not export the workspace (rc={rc}); is /workspace/.git intact?"], committed=False)
        try:
            workspace.apply_tree(c.app_dir, out / "tree.tar")
        except workspace.ExportError as e:
            gitops.hard_reset(c.app_dir, base)
            return self.fail(step, attempt, max_attempts, base, adir, [str(e)], committed=False)
        finally:
            workspace.rmtree(out)
        changes = workspace.staged_changes(c.app_dir, base)
        (adir / "changes.txt").write_text("\n".join(f"{x.status} {x.mode} {x.path}" for x in changes) + "\n")
        if not changes:
            gitops.hard_reset(c.app_dir, base)
            return self.fail(step, attempt, max_attempts, base, adir,
                             [f"no file changes were made (coder ended: {res.kind})"], committed=False)
        bad = workspace.check_allowlist(changes, spec.allowed_paths, spec.allow_text_paths)
        if bad:
            (adir / "diff.patch").write_text(self.redactor.text(gitops.diff(c.app_dir, "--cached", base)))
            gitops.hard_reset(c.app_dir, base)
            return self.fail(step, attempt, max_attempts, base, adir,
                             ["changes outside the allowed paths (whole attempt rejected):", *bad[:40]], committed=False)
        leaks = workspace.scan_secrets(c.app_dir, changes, self.redactor)
        if leaks:
            (adir / "diff.patch").write_text(self.redactor.text(gitops.diff(c.app_dir, "--cached", base)))
            gitops.hard_reset(c.app_dir, base)
            return self.fail(step, attempt, max_attempts, base, adir,
                             ["secret values found in the changes (whole attempt rejected); read them from the "
                              "environment or /run/hands/project.env at run time, never write them into files:",
                              *leaks[:40]], committed=False)
        msg = (f"{step.id}: {step.title}\n\n{summary[:1500]}\n\n"
               f"Spec-Commit: {spec.commit}\nHands-Attempt: {attempt}\nHands-Model: {','.join(res.models) or self.model}\n")
        gitops.git(c.app_dir, "-c", "user.name=hands-runner", "-c", "user.email=runner@hands.local",
                   "commit", "-q", "--no-verify", "-F", "-", input=msg.encode())
        commit = gitops.head(c.app_dir)
        (adir / "diff.patch").write_text(self.redactor.text(gitops.diff(c.app_dir, base, commit)))

        # ---- acceptance: runner-run, fresh clone, fresh package store, one container per command
        self.set_phase("acceptance")
        report, failure = self.run_acceptance(step, attempt, commit, adir)
        if failure:
            return self.fail(step, attempt, max_attempts, base, adir, failure, committed=True)

        # ---- independent evaluator: another fresh clone + store (acceptance ran agent code in the last one)
        self.set_phase("evaluator")
        verdict = self.run_evaluator(step, attempt, adir, base, commit, report, summary)
        if not verdict["pass"]:
            reasons = ["independent review failed:", *[str(r) for r in verdict.get("reasons", [])][:30]]
            return self.fail(step, attempt, max_attempts, base, adir, reasons, committed=True)

        # ---- accept: record first, then push (an unpushed accepted commit is pushed by sync_app next round)
        st.update({"status": "passed", "attempts": attempt, "commit": commit, "reasons": [],
                   "spec_commit": spec.commit, "passed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "round": self.round_id, "branch": self.branch, **rounds.make_record(spec.root, step.id)})
        self.state.data.pop("current", None)
        self.state.save()
        self.uncounted.pop(step.id, None)
        pushed = "push disabled"
        if c.push:
            ok, err = gitops.push_branch(c.app_dir, self.branch)
            pushed = "pushed" if ok else f"push failed ({err[:200]}); will retry next step"
        self.state.append_jsonl("progress.jsonl", {
            "step": step.id, "title": step.title, "commit": commit, "attempts": attempt,
            "summary": summary[:1500], "risks": verdict.get("risks", [])[:10]})
        self.log(f"PASSED {step.id} @ {commit[:9]} ({pushed})")
        risks = verdict.get("risks") or []
        self.send(f"PASSED {step.id} {step.title} (attempt {attempt}, {commit[:9]}, {pushed})"
                         + (f"\nrisks: {'; '.join(map(str, risks[:3]))}" if risks else ""))
        return True

    def set_phase(self, phase: str) -> None:
        self.state.data["current"]["phase"] = phase
        self.state.save()
        (self.cfg.state_dir / "activity.json").unlink(missing_ok=True)

    def on_activity(self, item: dict) -> None:
        """Live 'what is the session doing' for the dashboard. Runs in the tail thread, so it writes its
        own small file (never state.json). Throttled; plain text only."""
        now = time.time()
        if now - getattr(self, "_act_at", 0) < 3 and item.get("kind") not in ("write", "agent", "skill"):
            return
        self._act_at = now
        p = self.cfg.state_dir / "activity.json"
        tmp = p.with_name(".activity.json.tmp")
        try:
            tmp.write_text(json.dumps({"text": self.redactor.text(item.get("text", ""))[:160], "at": item.get("at")}))
            os.replace(tmp, p)
        except OSError:
            pass

    def fresh_clone(self, name: str, commit: str) -> Path:
        d = self.cfg.state_dir / name
        workspace.rmtree(d)
        gitops.git(self.cfg.state_dir, "clone", "-q", "--no-hardlinks", "--no-checkout", str(self.cfg.app_dir), name)
        gitops.git(d, "checkout", "-q", "--detach", commit)
        gitops.git(d, "remote", "remove", "origin", check=False)
        excl = d / ".git" / "info" / "exclude"
        excl.parent.mkdir(parents=True, exist_ok=True)
        with open(excl, "a") as f:
            f.write("\n/acceptance/\n")
        return d

    def run_acceptance(self, step, attempt, commit: str, adir: Path) -> tuple[list[dict], list[str] | None]:
        """Each command runs in its own container; the result is that container's exit code.
        Output goes to a host file via the container's stdout, so nothing in the container can forge it."""
        c, spec = self.cfg, self.spec
        plan = prompts.acceptance_plan(spec, step, self.regression_steps())
        verify = self.fresh_clone("verify", commit)
        store = docker.volume_create(c, f"hands-{c.name}-accept-{secrets.token_hex(4)}")
        report: list[dict] = []
        try:
            for i, (cmd, owner) in enumerate(plan):
                logf = adir / f"accept-{i}.log"
                logf.write_text(f"### {cmd}\n")
                t0 = time.time()
                rc = docker.run(c, Run(
                    name=docker.unique_name(c, "accept", f"{step.id}-{attempt}-{i}"), role="accept",
                    cmd=["bash", "-c", cmd], mounts=self.mounts(verify, store=store),
                    env=self.common_env("accept"), env_files=self.env_files(claude_token=False),
                    stdout=logf, stderr=logf, timeout_s=c.accept_timeout_min * 60, append_stdout=True))
                self.redact_files(logf)
                report.append({"cmd": cmd, "rc": rc, "for": owner})
                self.log(f"acceptance {step.id}#{attempt} [{i}] rc={rc} {time.time() - t0:.0f}s: {cmd}")
                if rc != 0:
                    tail = "\n".join(logf.read_text(errors="replace").splitlines()[-80:])
                    why = "timed out" if rc == 124 else f"exit {rc}"
                    what = (f"regression: acceptance of passed step {owner} failed" if owner and owner != step.id
                            else "acceptance command failed")
                    return report, [f"{what} ({why}): `{cmd}`", f"last output:\n{tail}"]
            return report, None
        finally:
            docker.volume_rm(c, store)
            workspace.rmtree(verify)

    def run_evaluator(self, step, attempt, adir, base, commit, report, summary) -> dict:
        c, spec = self.cfg, self.spec
        diff = prompts.truncate(self.redactor.text(gitops.diff(c.app_dir, base, commit)), c.diff_limit_chars)
        stat = gitops.git(c.app_dir, "diff", "--stat", "--no-ext-diff", base, commit, check=False)
        nonce = secrets.token_hex(8)
        (adir / "eval-prompt.md").write_text(prompts.evaluator(
            spec, step, diff=diff, diffstat=stat, accept_report=report, coder_summary=summary, nonce=nonce,
            env_vars=self.env_names()))
        tries = 0
        while tries < 3:
            verify = self.fresh_clone("verify", commit)
            store = docker.volume_create(c, f"hands-{c.name}-eval-{secrets.token_hex(4)}")
            n = tries + 1
            cmd = self.claude_cmd(model=self.eval_model, system_file="evaluator.md", max_turns=c.eval_max_turns,
                                  extra=["--disallowedTools", "Write,Edit,NotebookEdit,Agent,WebFetch"])
            t0 = time.time()
            try:
                (adir / f"eval-{n}.json").write_bytes(b"")
                with activity.Tail(adir / f"eval-{n}.json", self.on_activity):
                    rc = docker.run(c, Run(
                        name=docker.unique_name(c, "eval", f"{step.id}-{attempt}-{n}"), role="eval", cmd=cmd,
                        mounts=self.mounts(verify, store=store), env=self.common_env("eval"),
                        env_files=self.env_files(claude_token=True), stdin=adir / "eval-prompt.md",
                        stdout=adir / f"eval-{n}.json", stderr=adir / f"eval-{n}.stderr",
                        timeout_s=c.eval_timeout_min * 60, append_stdout=True))
            finally:
                docker.volume_rm(c, store)
                workspace.rmtree(verify)
            self.redact_files(adir / f"eval-{n}.json", adir / f"eval-{n}.stderr")
            res = claude.parse(adir / f"eval-{n}.json", rc, adir / f"eval-{n}.stderr")
            self.record_session("eval", step.id, attempt, res, time.time() - t0)
            if res.kind in INFRA:
                self.count_uncounted(step.id, res)
                self.handle_infra(res)      # waits / switches model; does not use up a try
                continue
            tries += 1
            # only a clean, successful session's own final result counts; never stderr
            v = claude.parse_verdict(str(res.raw.get("result", ""))) if res.ok else None
            if v is not None and v.get("nonce") != nonce:
                self.log(f"evaluator verdict nonce mismatch; ignoring verdict (try {n})")
                v = None
            if v is not None:
                (adir / "verdict.json").write_text(json.dumps(v, indent=2))
                self.log(f"evaluator {step.id}#{attempt}: pass={v['pass']} reasons={len(v.get('reasons', []))}")
                return v
            self.log(f"evaluator gave no valid VERDICT (kind={res.kind}); try {n}/3")
        tag = f"hands/unreviewed/{step.id}-{attempt}-{int(time.time())}"
        gitops.git(c.app_dir, "tag", "-f", tag, commit, check=False)
        gitops.hard_reset(c.app_dir, base)
        self.state.data.pop("current", None)
        self.state.save()
        self.escalate(f"evaluator failed to produce a verdict 3 times for {step.id} attempt {attempt}; "
                      f"candidate kept at tag {tag}; logs in {adir}")
        return {"pass": False}

    # ------------------------------------------------------------------ outcomes
    def fail(self, step, attempt, max_attempts, base, adir: Path, reasons: list[str], *, committed: bool) -> bool:
        c = self.cfg
        reasons = [self.redactor.text(r) for r in reasons]
        prev_sig = failure_signature(self.state.step(step.id).get("reasons") or []) if attempt > 1 else ""
        if committed:
            gitops.git(c.app_dir, "tag", "-f", f"hands/failed/{step.id}-{attempt}", "HEAD", check=False)
        gitops.hard_reset(c.app_dir, base)
        workspace.rmtree(c.state_dir / "work")
        workspace.rmtree(c.state_dir / "verify")
        st = self.state.step(step.id)
        st.update({"status": "failed", "attempts": attempt, "reasons": reasons})
        self.state.data.pop("current", None)
        self.state.save()
        (adir / "reasons.txt").write_text("\n".join(reasons) + "\n")
        self.state.append_jsonl("attempts.jsonl", {"step": step.id, "attempt": attempt, "reasons": reasons[:10]})
        first = reasons[0] if reasons else "?"
        self.log(f"FAILED {step.id} attempt {attempt}/{max_attempts}: {first}")
        if attempt >= max_attempts:
            self.escalate(f"{step.id} failed {attempt} attempts. Last: {' | '.join(reasons[:3])[:900]}\nLogs: {adir}")
        if getattr(self, "spec_problem", ""):
            self.escalate(f"{step.id} attempt {attempt} failed and the coder reports a problem in the spec/tests it "
                          f"cannot fix:\n{self.spec_problem[:900]}\nFix the spec repo (PR), then: hands {c.name} ack\nLogs: {adir}")
        sig = failure_signature(reasons)
        if prev_sig and sig and sig == prev_sig:
            self.escalate(f"{step.id} failed twice in a row with the same error, so another attempt is unlikely to "
                          f"help:\n{' | '.join(reasons[:2])[:900]}\nLogs: {adir}")
        self.send(f"{step.id} attempt {attempt}/{max_attempts} rejected: {first[:300]}")
        return False

    def count_uncounted(self, sid: str, res: claude.Result) -> None:
        if res.kind == "rate_limit":
            return                      # genuine waits are bounded by rate_limit_max_wait_s instead
        n = self.uncounted[sid] = self.uncounted.get(sid, 0) + 1
        if n > MAX_UNCOUNTED:
            self.escalate(f"{sid}: {n} infrastructure failures in a row (last: {res.kind}: {res.text[:200]})")

    def handle_infra(self, res: claude.Result) -> None:
        c = self.cfg
        if res.kind == "auth":
            self.escalate(f"Claude auth failed ({res.text[:200]}). Renew the token: claude setup-token, then update {c.claude_env}")
        if res.kind == "model_access":
            if self.state.data.get("model_override") != c.fallback_model and c.fallback_model:
                self.state.data["model_override"] = c.fallback_model
                self.state.save()
                self.log(f"model access error ({res.text[:200]}); switching to fallback {c.fallback_model}")
                self.send(f"model unavailable; switched to {c.fallback_model}. Clear with: hands {c.name} ack --clear-model")
                return
            self.escalate(f"model access error even on fallback: {res.text[:300]}")
        if res.kind == "overloaded":
            self.transient += 1
            if self.transient > 6:
                self.escalate(f"API errors persisted for 6 retries: {res.text[:300]}")
            wait = min(1800, 120 * self.transient)
            self.log(f"API overloaded/error; retry in {wait}s ({self.transient}/6)")
            self.nap(wait)
            return
        if res.kind == "rate_limit":
            self.wait_rate_limit(res.text)

    def wait_rate_limit(self, text: str) -> None:
        c = self.cfg
        started = self.clock()
        self.state.set_status("paused_rate_limit", text[:300])
        resets = claude.parse_resets_at(text, tz=c.tz, now=self.clock())
        self.send(f"usage limit hit; pausing (resets {time.strftime('%H:%M %Z', time.localtime(resets)) if resets else 'unknown'})")
        while True:
            if self.clock() - started > c.rate_limit_max_wait_s:
                self.escalate(f"usage limit did not clear within {c.rate_limit_max_wait_s // 3600}h")
            now = self.clock()
            if resets and now < resets <= now + 8 * 3600:
                self.log(f"sleeping until limit reset at {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(resets))}")
                self.nap(resets - now + 90)
            else:
                self.log(f"probing again in {c.rate_limit_probe_s}s")
                self.nap(c.rate_limit_probe_s)
            res = self.probe()
            if res.ok:
                self.state.set_status("running", "")
                self.log("usage limit cleared; resuming")
                self.send("usage limit cleared; resuming")
                return
            if res.kind == "auth":
                self.handle_infra(res)
            resets = claude.parse_resets_at(res.text, tz=c.tz, now=self.clock())

    def probe(self, model: str | None = None) -> claude.Result:
        c = self.cfg
        pdir = c.state_dir / "probe"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "prompt.txt").write_text("Reply with exactly: OK\n")
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model or self.model, "--max-turns", "1",
               "--no-session-persistence"]
        (pdir / "err.log").write_text("")
        rc = docker.run(c, Run(
            name=docker.unique_name(c, "probe", "x"), role="probe", cmd=cmd,
            mounts=docker.base_mounts(c, self.spec.root if self.spec else c.spec_dir),
            env=self.common_env("probe"), env_files=[c.claude_env], stdin=pdir / "prompt.txt",
            stdout=pdir / "out.json", stderr=pdir / "err.log", timeout_s=600))
        usage.record_from_stream(pdir / "out.json")
        return claude.parse(pdir / "out.json", rc, pdir / "err.log")
