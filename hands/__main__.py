"""hands <project> <command>

  run [--steps N]        run the build loop (normally inside tmux via `start`)
  start                  start the loop detached in tmux session hands-<project>
  status                 show state, current step, recent sessions
  check                  preflight + live probes (firewall, auth, model, exact coder flags, telegram)
  validate               validate the spec repo (steps.json, refs, e2e files)
  prompt [STEP]          print the coder prompt the next (or given) step would get; no session
  stop                   ask the loop to stop at the next safe point
  ack [--clear-model]    clear an escalation (resets the escalated step's attempt count)
  mark-done STEP         mark a human step done
  reset-step STEP        set a step back to pending with 0 attempts
  watchdog               (cron) restart the loop in tmux if it died; never after escalation/done/STOP
  create                 new project from nothing: repos, protection, deploy keys, clones, config
                         (description on stdin; the dashboard's New project form runs this)
    --from-repo O/R      build on an existing repo instead of creating <slug>-app: adds a write deploy
                         key to it (and the protect-main ruleset only with --ruleset); --branch B picks
                         the branch to build on (default: the repo's default branch)
  destroy --confirm P    delete a hands-created project: GitHub repos, keys, folders, config
                         (an imported repo is never deleted: only its hands deploy key is removed)
  plan                   run the planner (feedback on stdin): idea -> spec -> PR hands/plan
  approve                merge the reviewed plan PR (sha-pinned), refresh the spec, start the build
hands all watchdog       (cron) watchdog for every project, and keep the dashboard running
hands all usage          refresh plan usage (one tiny Haiku reply) -> ~/.config/hands/usage.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import accview, claude, docker, gitops, prompts
from . import spec as specmod
from . import config as cfgmod
from .config import RUNNER_HOME, load
from .docker import Mount, Run
from .loop import Runner
from .state import Lock


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)       # session streams can contain tool output incl. secrets: owner-only files
    ap = argparse.ArgumentParser(prog="hands", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("command", choices=["run", "start", "status", "check", "validate", "prompt", "stop", "ack",
                                        "mark-done", "reset-step", "watchdog", "create", "destroy", "plan", "approve", "usage"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--steps", type=int)
    ap.add_argument("--clear-model", action="store_true")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--then-plan", action="store_true")
    ap.add_argument("--from-repo", default="", metavar="OWNER/REPO")
    ap.add_argument("--branch", default="")
    ap.add_argument("--ruleset", action="store_true")
    a = ap.parse_args(argv)
    if a.project == "all":
        if a.command == "usage":
            from . import usage
            return usage.refresh()
        if a.command != "watchdog":
            print("'all' is only valid with watchdog or usage")
            return 2
        return watchdog_all()
    if a.command in ("create", "destroy"):
        from . import newproject
        try:
            if a.command == "create":
                rc = newproject.main_create(a.project, a.from_repo, a.branch, a.ruleset)
                if rc == 0 and a.then_plan:
                    from . import planner
                    print("starting the planner (log: dashboard Plan section)", flush=True)
                    return planner.plan(load(a.project))
                return rc
            return newproject.destroy(a.project, a.confirm)
        except newproject.CreateError as e:
            print(f"FAILED: {e}")
            return 1
    cfg = load(a.project)
    if a.command in ("plan", "approve"):
        from . import planner
        if a.command == "plan":
            fb = sys.stdin.read() if not sys.stdin.isatty() else ""
            return planner.plan(cfg, fb)
        return planner.approve(cfg, a.arg or "")
    r = Runner(cfg)
    st = r.state
    cmd = a.command

    if cmd == "run":
        return r.run(max_steps=a.steps)

    restart_record = None
    if cmd == "watchdog":
        if Lock.held(cfg.state_dir / "runner.lock") or r.stop_file.exists():
            return 0
        reason = st.data.get("reason") or ""
        # restart only after a crash/kill/shutdown (SIGTERM); Ctrl-C (SIGINT) and tmux kill (SIGHUP) are user stops
        if st.status in ("running", "paused_rate_limit") or (st.status == "stopped" and "signal 15" in reason):
            wf = cfg.state_dir / "watchdog.json"
            try:
                hist = [t for t in json.loads(wf.read_text()).get("restarts", []) if time.time() - t < 3600]
            except (OSError, ValueError):
                hist = []
            if len(hist) >= 3:
                st.set_status("escalation", "watchdog: runner died 3 times within an hour; see runner.log")
                st.log("ESCALATION: watchdog restart limit reached")
                r.notify.send(f"ESCALATION: runner keeps dying (3 restarts in 1h). Check runner.log, then: hands {cfg.name} ack")
                return 1
            restart_record = (wf, hist)
            st.log(f"watchdog: runner not alive (status {st.status}); restarting")
            r.notify.send(f"watchdog: runner was not running (status {st.status}); restarting")
            cmd = "start"
        else:
            return 0

    if cmd == "start":
        sess = f"hands-{cfg.name}"
        if subprocess.run(["tmux", "has-session", "-t", f"={sess}"], capture_output=True).returncode == 0:
            print(f"tmux session {sess} already running: tmux attach -t {sess}")
            return 1
        subprocess.run(["tmux", "new-session", "-d", "-s", sess, f"{hands_exe()} {shlex.quote(cfg.name)} run"],
                       check=True, env=_no_tmux_env())
        if restart_record:
            wf, hist = restart_record
            wf.write_text(json.dumps({"restarts": hist + [time.time()]}))
        print(f"started. watch: tmux attach -t {sess}   (detach: Ctrl-b d)   log: {cfg.state_dir}/runner.log")
        return 0

    if cmd == "status":
        d = st.data
        print(f"status: {d.get('status')}  {d.get('reason') or ''}".rstrip())
        print(f"runner alive: {Lock.held(cfg.state_dir / 'runner.lock')}   STOP file: {r.stop_file.exists()}")
        if d.get("model_override"):
            print(f"model override: {d['model_override']}")
        if d.get("current"):
            print(f"current: {d['current']}")
        print(f"totals: {d.get('total_sessions', 0)} sessions, ~${d.get('total_cost_usd', 0)} (API-equivalent estimate)")
        try:
            sp = specmod.load(cfg.spec_dir, gitops.head(cfg.spec_dir))
            for s in sp.steps:
                x = d["steps"].get(s.id, {})
                print(f"  {x.get('status', 'pending'):8} {s.id:10} att={x.get('attempts', 0)} {s.title}")
        except Exception as e:
            print(f"(spec not loadable: {e})")
        for m in st.read_jsonl("metrics.jsonl")[-5:]:
            print(f"  {m['ts']} {m['role']:5} {m['step']}#{m['attempt']} {m['kind']} turns={m['turns']} ${m['cost_usd']:.2f} {m['duration_s']}s")
        return 0

    if cmd == "validate":
        try:
            sp = specmod.load(cfg.spec_dir, gitops.head(cfg.spec_dir))
        except specmod.SpecError as e:
            print(f"INVALID: {e}")
            return 1
        print(f"OK: {len(sp.steps)} steps, allowed_paths={sp.allowed_paths}")
        shared, by_step = accview.views(sp)
        print(f"acceptance shared (every step sees these): {', '.join(sorted(shared)) or '(none)'}")
        for s in sp.steps:
            print(f"  {s.id} owns: {', '.join(sorted(by_step[s.id])) or '(none)'}")
        named = set()
        for p in accview.shared_patterns(sp):
            named |= accview._match(p, sorted(shared))
        odd = sorted(shared - named)
        if odd:
            print(f"WARN: files no command names (visible to every step; if one belongs to a step, list it in that "
                  f"step's acceptance_files): {', '.join(odd)}")
        if cfg.imported:
            from . import codesummary
            for w in codesummary.coverage_warnings(sp.allowed_paths, cfg.app_dir):
                print(f"WARN: {w} (the builders cannot change those files)")
        return 0

    if cmd == "prompt":
        sp = specmod.load(cfg.spec_dir, gitops.head(cfg.spec_dir))
        step = sp.step(a.arg) if a.arg else next((s for s in sp.steps if not st.passed(s.id)), None)
        if not step:
            print("all steps passed")
            return 0
        x = st.step(step.id)
        passed = {sid for sid in st.data["steps"] if st.passed(sid)}
        print(prompts.coder(sp, step, attempt=x["attempts"] + 1, max_attempts=step.max_attempts or cfg.default_max_attempts,
                            progress=st.read_jsonl("progress.jsonl")[-cfg.progress_notes:],
                            git_log=gitops.log_oneline(cfg.app_dir), feedback=x.get("reasons", []), prev_diff="",
                            acc_files=accview.visible(sp, step, passed)))
        return 0

    if cmd == "stop":
        r.stop_file.touch()
        print(f"STOP requested ({r.stop_file}); the loop stops at the next safe point. Remove the file to run again.")
        return 0

    if cmd == "ack":
        if Lock.held(cfg.state_dir / "runner.lock"):
            print("runner is running; stop it first (hands %s stop)" % cfg.name)
            return 1
        if st.status != "escalation" and not a.clear_model:
            print(f"status is {st.status}; nothing to acknowledge")
            return 0
        for sid, x in st.data["steps"].items():
            if x.get("status") == "failed":
                x["attempts"] = 0
        if a.clear_model:
            st.data.pop("model_override", None)
        (cfg.state_dir / "watchdog.json").unlink(missing_ok=True)
        st.set_status("idle", "acknowledged")
        print("acknowledged. start again with: hands", cfg.name, "start")
        return 0

    if cmd in ("mark-done", "reset-step"):
        if not a.arg:
            print("step id required")
            return 2
        if Lock.held(cfg.state_dir / "runner.lock"):
            print("runner is running; stop it first")
            return 1
        x = st.step(a.arg)
        if cmd == "mark-done":
            x.update({"status": "passed", "commit": "human", "passed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            st.append_jsonl("progress.jsonl", {"step": a.arg, "title": "(human step)", "commit": "", "summary": "done by a human"})
        else:
            st.data["steps"][a.arg] = {"status": "pending", "attempts": 0, "reasons": []}
        if st.status == "escalation":
            st.set_status("idle", f"{cmd} {a.arg}")
        st.save()
        print(f"{a.arg}: {st.data['steps'][a.arg]['status']}")
        return 0

    if cmd == "check":
        if Lock.held(cfg.state_dir / "runner.lock"):
            print("runner is running; check only while it is stopped")
            return 1
        with Lock(cfg.state_dir / "runner.lock"):
            return check(r)
    return 2


def hands_exe() -> str:
    """Absolute command for tmux/cron (cron's PATH has no ~/.local/bin)."""
    p = Path.home() / ".local" / "bin" / "hands"
    if p.is_file() and os.access(p, os.X_OK):
        return shlex.quote(str(p))
    w = shutil.which("hands")
    if w:
        return shlex.quote(w)
    return f"env PYTHONPATH={shlex.quote(str(RUNNER_HOME))} {shlex.quote(sys.executable)} -m hands"


def _no_tmux_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def project_names() -> list[str]:
    d = cfgmod.CONFIG_HOME / "projects"
    return sorted(p.stem for p in d.glob("*.toml") if p.stem != "all") if d.is_dir() else []


def watchdog_all() -> int:
    """For cron: run every project's watchdog, then make sure the dashboard is up."""
    rc = 0
    for name in project_names():
        try:
            rc |= main([name, "watchdog"]) or 0
        except SystemExit as e:
            print(f"{name}: {e}")
            rc = 1
        except Exception as e:
            print(f"{name}: {type(e).__name__}: {e}")
            rc = 1
    from . import dashboard
    try:
        dashboard.ensure_running()
    except Exception as e:
        print(f"dashboard: {type(e).__name__}: {e}")
        rc = 1
    return rc


def check_env(cfg, line) -> bool:
    """Project variables: counts by visibility; fails on a loose file mode or lines the dashboard cannot manage."""
    from . import projectenv
    if not cfg.project_env:
        print("[--] project_env not set: no project variables go to the build containers")
        return True
    p = cfg.project_env
    if not p.is_file():
        line(False, f"project_env {p} missing")
        return False
    ok = True
    mode = p.stat().st_mode & 0o777
    if mode != 0o600 or p.is_symlink():
        line(False, f"project_env {p} must be a regular file with mode 0600 (is {mode:o})")
        ok = False
    try:
        st = projectenv.Store(p.parent, p.name)
    except projectenv.EnvError as e:
        line(False, f"project_env: {e}")
        return False
    n_secret = sum(1 for _, pub in st.names() if not pub)
    n_public = len(st.values) - n_secret
    line(True, f"project variables: {n_secret} secret, {n_public} public ({p})")
    for prob in st.problems:
        line(False, f"project_env {prob}")
        ok = False
    return ok


def check(r: Runner) -> int:
    cfg = r.cfg
    ok = True

    def line(good: bool, msg: str):
        nonlocal ok
        ok &= good
        print(f"[{'ok' if good else 'FAIL'}] {msg}")

    try:
        r.preflight()
        line(True, "preflight (image, token file, repos)")
    except SystemExit as e:
        line(False, str(e))
        return 1
    check_env(cfg, line)
    try:
        commit = gitops.refresh_spec(cfg.spec_dir, cfg.spec_branch)
        r.spec = specmod.load(cfg.spec_dir, commit)
        line(True, f"spec valid at {commit[:9]} ({len(r.spec.steps)} steps)")
    except Exception as e:
        line(False, f"spec: {e}")
        return 1
    r.recover()                         # never leave an unaccepted candidate where a push could pick it up
    try:
        line(True, f"app repo vs origin: {gitops.ahead_behind(cfg.app_dir, cfg.branch)} (report only, nothing pushed)")
    except gitops.GitError as e:
        line(False, f"app repo fetch: {e}")
    for model in dict.fromkeys([cfg.coder_model, cfg.eval_model, cfg.fallback_model]):
        if not model:
            continue
        res = r.probe(model)
        line(res.ok and "OK" in res.text, f"probe {model}: kind={res.kind} models={res.models} {res.text[:60]!r}")
    # exact coder flag set, 1 turn, real workspace mount layout
    pdir = cfg.state_dir / "probe"
    work = cfg.state_dir / "probe-work"
    from . import workspace
    workspace.make_work(cfg.app_dir, work)
    (pdir / "flags.txt").write_text(
        "Reply with exactly one line: SKILLS=<comma-separated names of skills you have> AGENTS=<comma-separated subagent names you can use>\n")
    agents = json.loads((cfg.runner_home / "prompts" / "agents.json").read_text())
    cmd = r.claude_cmd(model=r.model, system_file="coder.md", max_turns=2,
                       extra=["--agents", json.dumps(agents), "--disallowedTools", "WebFetch"])
    rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "probe", "flags"), role="probe", cmd=cmd,
                             mounts=r.mounts(work, store=cfg.pnpm_volume, with_skills=True), env=r.common_env("coder"),
                             env_files=r.env_files(claude_token=True),
                             stdin=pdir / "flags.txt", stdout=pdir / "flags.json", stderr=pdir / "flags.err", timeout_s=900))
    res = claude.parse(pdir / "flags.json", rc, pdir / "flags.err")
    txt = res.text
    line(res.ok, f"coder flags accepted (kind={res.kind})")
    for need in ("test-driven-development", "systematic-debugging", "verification-before-completion"):
        line(need in txt, f"skill visible: {need}")
    for need in ("explorer", "test-runner"):
        line(need in txt, f"subagent visible: {need}")
    if (cfg.runner_home / "planner-skills").is_dir():
        from . import planner
        pw = cfg.state_dir / "probe-planner"
        workspace.rmtree(pw)
        pw.mkdir(parents=True)
        (pdir / "planner.txt").write_text(
            "Do not write files. Reply with exactly one line: SKILLS=<comma-separated names of skills you have> "
            "TOOLS=<comma-separated names of your tools>\n")
        pres = planner.run_session(cfg, pw, pdir / "planner.txt", pdir / "planner.json", pdir / "planner.err",
                                   lambda m: None)
        line(pres.ok, f"planner flags accepted (kind={pres.kind})")
        for need in ("brainstorming", "writing-plans"):
            line(need in pres.text, f"planner skill visible: {need}")
        line("WebFetch" not in pres.text.split("TOOLS=", 1)[-1], "planner has no WebFetch")
        workspace.rmtree(pw)
    # firewall: blocked + allowed, and the agent cannot undo it
    fw = docker.run(cfg, Run(
        name=docker.unique_name(cfg, "probe", "fw"), role="probe",
        cmd=["bash", "-c",
             "curl -s --connect-timeout 5 https://example.com >/dev/null && echo LEAK || echo blocked; "
             "curl -s -o /dev/null -w 'anthropic=%{http_code}\\n' https://api.anthropic.com; "
             "iptables -F 2>/dev/null && echo FLUSHED || echo cannot-flush; "
             "sudo -n true 2>/dev/null && echo SUDO || echo no-sudo; "
             "curl -s -6 --connect-timeout 5 https://example.com >/dev/null && echo LEAK6 || echo v6-blocked; "
             "dig +short +time=3 +tries=1 @1.1.1.1 example.com | grep -q . && echo DNS-OPEN || echo dns-1111-blocked; "
             "id -u"],
        mounts=docker.base_mounts(cfg, r.spec.root), stdout=pdir / "fw.txt", stderr=pdir / "fw.err", timeout_s=300))
    out = (pdir / "fw.txt").read_text() if (pdir / "fw.txt").exists() else ""
    line(fw == 0 and "blocked" in out and "LEAK" not in out, "egress blocked for non-allowlisted sites")
    line("anthropic=000" not in out and "anthropic=" in out, "api.anthropic.com reachable")
    line("cannot-flush" in out and "no-sudo" in out, "agent cannot change firewall or use sudo")
    line("LEAK6" not in out, "no IPv6 egress")
    if "DNS-OPEN" in out:
        print("[warn] DNS to outside resolvers is open (reference firewall allows all port 53): DNS tunnelling possible")
    line(out.strip().splitlines()[-1:] != ["0"], f"agent uid is not root ({out.strip().splitlines()[-1:] or '?'})")
    workspace.rmtree(work)
    if r.notify.enabled:
        line(r.notify.send("check: telegram works"), "telegram message sent")
    else:
        print(f"[--] telegram not configured ({cfg.telegram_env})")
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
