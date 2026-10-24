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
  reset-step STEP        set a step back to pending with 0 attempts (not after its round was merged)
  merge                  squash-merge the finished change round into main, sha-pinned (--branch-sha, --main-sha)
  discard                drop the open round's build (work kept as a tag); its steps go back to pending
  watchdog               (cron) restart the loop in tmux if it died; never after escalation/done/STOP
  create                 new project from nothing: repos, protection, deploy keys, clones, config
                         (description on stdin; the dashboard's New project form runs this)
    --from-repo O/R      build on an existing repo instead of creating <slug>-app: adds a write deploy
                         key to it (and the protect-main ruleset only with --ruleset); --branch B picks
                         the branch to build on (default: the repo's default branch)
    --style-ref URL      (repeatable) style reference URLs, captured, analysed and frozen into the spec repo
                         right after the clones exist; --framework N --libs a,b choose the scaffold, which
                         runs next (state: <state_dir>/next.json); planning waits for both (the gate)
    --site-inputs FILE   (create, plan --amend) the copy fields as JSON (audience, offer, proof, tone_notes,
                         banned_words, directions); kept in next.json and put into the spec outbox
  destroy --confirm P    delete a hands-created project: GitHub repos, keys, folders, config
                         (an imported repo is never deleted: only its hands deploy key is removed)
  plan                   run the planner (feedback on stdin): idea -> spec -> PR hands/plan
                         (--amend with --style-ref/--framework/--libs: a change request; new refs are processed
                         first). Refused while the gate is blocked (style refs not frozen, setup not done)
  approve                merge the reviewed plan PR (sha-pinned), refresh the spec, start the build
  setup                  scaffold the app repo with the project's framework/libs choices inside the firewalled
                         container (--framework N --libs a,b store new choices first); commits and pushes
                         `scaffold: ...` only when build, lint and typecheck pass; writes refs/setup.json
  design-files add FILE... [--kind K] [--note T] | list | remove ID | edit ID [--kind K] [--note T]
                         | group ID ID... [--note T] | process UPLOAD_ID   design inputs (the spec outbox is the store)
  refs add URL [URL...]  add style reference URLs (change request) and run their pipeline
  refs retry N [N...]    run the style-ref pipeline again for ref N (capture, analyst, validate, freeze)
  refs remove N [N...]   drop style ref N (next.json, and refs/style-refs/NN from the spec repo)
  start refuses while the gate is blocked: a style ref not frozen, or setup pending/running/failed
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

from . import accview, claude, docker, gitops, merge, prompts, rounds
from . import spec as specmod
from . import config as cfgmod
from .config import RUNNER_HOME, load
from .docker import Mount, Run
from .loop import Runner
from .state import Lock, State


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)       # session streams can contain tool output incl. secrets: owner-only files
    ap = argparse.ArgumentParser(prog="hands", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("command", choices=["run", "start", "status", "check", "validate", "prompt", "stop", "ack",
                                        "mark-done", "reset-step", "watchdog", "create", "destroy", "plan", "approve", "usage",
                                        "merge", "discard", "setup", "refs", "design-files"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("args", nargs="*", help="refs add|retry|remove: the URLs or ref indexes")
    ap.add_argument("--steps", type=int)
    ap.add_argument("--clear-model", action="store_true")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--then-plan", action="store_true")
    ap.add_argument("--amend", action="store_true", help="plan: amend the approved plan (change request on stdin)")
    ap.add_argument("--ack-shared", action="store_true", help="approve: accept the amendment's shared-settings changes")
    ap.add_argument("--ack-domains", action="store_true", help="approve: accept the plan's new network domains")
    ap.add_argument("--ack-design", action="store_true", help="approve: accept the PR's new or changed design files")
    ap.add_argument("--ack-tests", action="store_true", help="approve: accept the amendment's changed/removed tests")
    ap.add_argument("--from-repo", default="", metavar="OWNER/REPO")
    ap.add_argument("--branch", default="")
    ap.add_argument("--ruleset", action="store_true")
    ap.add_argument("--branch-sha", default="", help="merge/discard: the change/C<n> head the page showed")
    ap.add_argument("--main-sha", default="", help="merge: the main head the page showed")
    ap.add_argument("--framework", type=int, help="setup: 1 None, 2 Astro+Tailwind, 3 +AI kit, 4 Next.js+Tailwind, 5 +AI kit")
    ap.add_argument("--libs", default=None, help="setup: comma-separated library ids (shadcn, gsap); '' for none")
    ap.add_argument("--style-ref", action="append", default=[], metavar="URL",
                    help="create / plan --amend: a style reference URL (repeatable)")
    ap.add_argument("--site-inputs", default=None, metavar="FILE",
                    help="create / plan --amend: JSON file with the copy fields (audience, offer, proof, ...)")
    ap.add_argument("--kind", default=None, help="design-files add|edit: the file kind")
    ap.add_argument("--note", default=None, help="design-files add|edit|group: the note (max 500 characters)")
    a = ap.parse_args(argv)
    libs = None if a.libs is None else [x.strip() for x in a.libs.split(",") if x.strip()]
    site_inputs = None
    if a.site_inputs and a.command in ("create", "plan"):
        from . import sitecopy
        try:
            site_inputs = sitecopy.read(Path(a.site_inputs))
        except sitecopy.InvalidInputs as e:
            print(f"FAILED: site inputs: {e}")
            return 2
        finally:
            Path(a.site_inputs).unlink(missing_ok=True)      # a one-shot hand-over from the dashboard
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
                from . import nextstate
                try:                                   # refuse bad choices before any repo exists
                    nextstate.validate(list(a.style_ref), 1 if a.framework is None else a.framework, libs or [])
                except nextstate.InvalidInput as e:
                    print(f"FAILED: {e}")
                    return 2
                rc = newproject.main_create(a.project, a.from_repo, a.branch, a.ruleset)
                if rc != 0:
                    return rc
                cfg = load(a.project)
                rc = after_create(cfg, list(a.style_ref), 1 if a.framework is None else a.framework, libs or [],
                                  site_inputs)
                if a.then_plan:
                    from . import planner
                    print("starting the planner (log: dashboard Plan section)", flush=True)
                    return planner.plan(cfg) or rc
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
            if a.style_ref or a.framework is not None or libs is not None or site_inputs is not None:
                rc = change_request(cfg, list(a.style_ref), a.framework, libs, site_inputs)
                if rc != 0:
                    return rc
            return planner.plan(cfg, fb, amend=a.amend)
        return planner.approve(cfg, a.arg or "", ack_shared=a.ack_shared, ack_domains=a.ack_domains, ack_tests=a.ack_tests, ack_design=a.ack_design)
    if a.command in ("merge", "discard"):
        return round_cmd(cfg, a.command, a.branch_sha, a.main_sha)
    if a.command == "setup":
        from . import setup
        return setup.run(cfg, framework=a.framework, libs=libs)
    if a.command == "design-files":
        from . import uploads
        return uploads.cli(cfg, a.arg or "", a.args, a.kind, a.note)
    if a.command == "refs":
        return refs_cmd(cfg, a.arg or "", a.args)
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
        from . import nextproject, nextstate
        try:
            reasons = nextproject.gate_reasons(cfg)
        except nextstate.InvalidInput as e:
            reasons = [str(e)]
        if reasons:
            print("start refused: planning may not start yet")
            for why in reasons:
                print(f"  {why}")
            print(f"fix: hands {cfg.name} refs retry N | refs remove N | setup")
            return 1
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
        rr = rounds.record(d)
        if rr and rr.get("status") == "merged":
            print(f"round C{rr['id']}: merged into {cfg.branch} at {str(rr.get('merged_sha', ''))[:9]} "
                  f"(tags {rr.get('pre_tag')} = before, {rr.get('merged_tag')} = unsquashed work)")
        elif rr:
            t = merge.tap_state(cfg, d, merge.step_ids(cfg))
            print(f"round C{rr['id']}: {rr.get('status')} on {rr.get('branch')} (base {str(rr.get('base_sha', ''))[:9]}, "
                  f"tag {rr.get('pre_tag')}; {t['passed']}/{t['total']} passed, "
                  f"{'merge ready' if t['ready'] else t['why'] or 'not ready'})")
        print(f"totals: {d.get('total_sessions', 0)} sessions, ~${d.get('total_cost_usd', 0)} (API-equivalent estimate)")
        from . import nextproject, nextstate
        try:
            print(nextstate.describe(nextstate.load(nextproject.file(cfg))))
        except nextstate.InvalidInput as e:
            print(f"(next.json unreadable: {e})")
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
                            acc_files=accview.visible(sp, step, passed), regression_steps=r.regression_steps(),
                            round_id=r.round_id))
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
        if cmd == "reset-step" and x.get("status") == "passed":
            why = merge.reset_refusal(cfg, st.data, a.arg, x)
            if why:
                print(why)
                return 1
        if cmd == "mark-done":
            try:
                spec_commit = gitops.head(cfg.spec_dir)      # so a later `plan --amend` can compare this step
            except gitops.GitError:
                spec_commit = ""
            # in a change round the step's commit is the branch head the human left (fetched from origin), so the
            # merge tap pins to it; outside a round there is nothing to pin: "human"
            head = merge.branch_head(cfg, st.data) if rounds.is_open(st.data) else ""
            if rounds.is_open(st.data) and not head:
                print(f"cannot read the head of {rounds.record(st.data).get('branch')} on origin; try again when origin is reachable")
                return 1
            x.update({"status": "passed", "commit": head or "human", "by": "human", "spec_commit": spec_commit,
                      "passed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "round": r.round_id, "branch": r.branch, **rounds.make_record(cfg.spec_dir, a.arg)})
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


def after_create(cfg, style_refs: list[str], framework: int, libs: list[str], site_inputs: dict | None = None) -> int:
    """Right after the clones exist: record the choices, run the style-ref pipeline for every URL, then the
    scaffold when a framework was chosen. 1 when a ref or the scaffold failed (the project exists either way;
    `refs retry N` / `refs remove N` / `setup` fix it, and the gate holds planning until then)."""
    from . import nextproject, setup, uploads
    state = nextproject.init(cfg, style_refs, framework, libs)
    rc = 0
    if site_inputs is not None:
        nextproject.set_site_inputs(cfg, site_inputs)
    for res in uploads.process_pending(cfg, uploads.creating_inbox(cfg.name)):
        if res["status"] != "ready":
            print(f"design file {res['id']}: {res['status']} ({res['reason']})", flush=True)
        if res.get("group_error"):
            print(f"design files: style pack not grouped ({res['group_error']})", flush=True)
    indexes = [r["index"] for r in state["style_refs"]]
    if indexes and _process(cfg, indexes):
        rc = 1
    if state["setup"]["status"] == "pending":
        print(f"setup: scaffolding {setup.FRAMEWORK_LABELS[framework]}" + (f" + {', '.join(libs)}" if libs else ""),
              flush=True)
        if setup.run(cfg) != 0:
            rc = 1
    return rc


def change_request(cfg, style_refs: list[str], framework: int | None, libs: list[str] | None,
                   site_inputs: dict | None = None) -> int:
    """New style refs, setup choices and/or copy fields for an existing project; the new refs are processed at once."""
    from . import nextproject, nextstate
    why = nextproject.busy(cfg) if style_refs else ""
    if why:
        print(f"refused: {why}")
        return 1
    try:
        indexes = nextproject.request(cfg, style_refs, framework, libs)
    except nextstate.InvalidInput as e:
        print(f"FAILED: {e}")
        return 2
    if site_inputs is not None:
        nextproject.set_site_inputs(cfg, site_inputs)
    if indexes:
        _process(cfg, indexes)
    if framework is not None or libs is not None:
        print(f"setup: {f'pending; run: hands {cfg.name} setup' if nextproject.setup_pending(cfg) else 'nothing to scaffold'}")
    return 0


def _process(cfg, indexes: list[int]) -> int:
    """Run the pipeline for these refs and print each outcome; returns how many failed."""
    from . import nextproject, nextstate
    print(f"style refs: processing {', '.join(f'{i:02d}' for i in indexes)} (log: {cfg.state_dir}/runner.log)", flush=True)
    failed = nextproject.process_refs(cfg, indexes)
    for r in nextstate.load(nextproject.file(cfg))["style_refs"]:
        if r["index"] in indexes:
            print(f"style ref {r['index']:02d} {r['status']}" + (f": {r['reason']}" if r.get("reason") else ""))
    return failed


def refs_cmd(cfg, verb: str, args: list[str]) -> int:
    """`refs add URL...` (change request), `refs retry N...` (back to queued, pipeline again), `refs remove N...`."""
    from . import nextproject, nextstate, stylerefs
    if verb not in ("add", "retry", "remove") or not args:
        print("usage: hands <project> refs add URL [URL...] | refs retry N [N...] | refs remove N [N...]")
        return 2
    why = nextproject.busy(cfg)
    if why:
        print(f"refused: {why}")
        return 1
    if verb == "add":
        return change_request(cfg, args, None, None)
    if not all(re.fullmatch(r"[0-9]{1,2}", x) for x in args):
        print("ref indexes must be integers from 1 to 99")
        return 2
    indexes = [int(x) for x in args]
    try:
        for i in indexes:
            nextstate.ref(nextstate.load(nextproject.file(cfg)), i)
        if verb == "remove":
            for i in indexes:
                nextproject.remove_ref(cfg, i)
                print(f"style ref {i:02d} removed")
            return 0
        for i in indexes:
            nextproject.retry_ref(cfg, i)
    except (nextstate.InvalidInput, stylerefs.InvalidInput) as e:     # unknown ref; FreezeError (git) on remove
        print(f"FAILED: {e}")
        return 1
    return 1 if _process(cfg, indexes) else 0


def round_cmd(cfg, cmd: str, branch_sha: str, main_sha: str) -> int:
    """`merge` / `discard` (the dashboard taps, and the same thing by hand). Shas are required: this is the pinned
    path, there is no "merge whatever is there". A self project is merged by Install update instead."""
    from .notify import Notifier
    st = State(cfg.state_dir)
    notifier = Notifier(cfg.telegram_env, cfg.name, log=st.log)
    if Lock.held(cfg.state_dir / "plan" / "plan.lock"):
        print(f"{cmd} refused: the planner is running")
        return 1
    if not branch_sha or (cmd == "merge" and not main_sha):
        print(f"{cmd}: --branch-sha{' and --main-sha' if cmd == 'merge' else ''} required (the heads shown by "
              f"`hands {cfg.name} status` or the dashboard)")
        return 2
    try:
        if cmd == "merge":
            if cfg.is_self:
                raise merge.MergeError(merge.SELF_WHY)
            if rounds.record(st.data) and rounds.record(st.data).get("status") != "merging":   # the tap checked already
                merge.check_tap(cfg, st.data, merge.step_ids(cfg))
            rec = merge.merge_round(cfg, st, st.log, notifier.send, branch_sha, main_sha)
            print(f"merged round C{rec['id']} into {cfg.branch} at {rec['merged_sha'][:9]}; rollback by hand: "
                  f"git reset --hard {rec['pre_tag']} (before) / unsquashed work at {rec['merged_tag']}")
        else:
            merge.discard_round(cfg, st, st.log, notifier.send, branch_sha)
            print("round discarded: its steps are pending again (the change request stays in the spec); "
                  f"`hands {cfg.name} start` rebuilds them in a fresh round")
    except merge.MergeError as e:
        print(f"{cmd} refused: {e}")
        return 1
    return 0


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


# Runs as the agent uid inside a probe container (bash). Every line is a marker read by firewall_checks;
# the last line is the uid. Raw IPs: 1.1.1.1 (any outside host), 140.82.112.3 (github.com, outside the
# allowlist unless a project lists it), the default gateway (= the host).
FIREWALL_PROBE = (
    "curl -s --connect-timeout 5 https://example.com >/dev/null && echo LEAK || echo blocked; "
    "curl -s --connect-timeout 5 http://1.1.1.1/ >/dev/null && echo LEAK-IP || echo ip-blocked; "
    "curl -s -o /dev/null -w 'anthropic=%{http_code}\\n' https://api.anthropic.com; "
    "iptables -F 2>/dev/null && echo FLUSHED || echo cannot-flush; "
    "sudo -n true 2>/dev/null && echo SUDO || echo no-sudo; "
    "curl -s -6 --connect-timeout 5 https://example.com >/dev/null && echo LEAK6 || echo v6-blocked; "
    "getent hosts api.anthropic.com >/dev/null && echo dns-allowed-ok || echo DNS-ALLOWED-FAILED; "
    "getent hosts probe-$RANDOM$RANDOM.example.com >/dev/null && echo DNS-RANDOM-RESOLVED || echo dns-random-blocked; "
    "for ns in 1.1.1.1 8.8.8.8; do for t in '' '+tcp'; do "
    # dig prints ';; communications error' on STDOUT, so judge by its exit code and real answer lines only
    "dig $t +short +time=3 +tries=1 @$ns example.com 2>/dev/null | grep -v '^;' | grep -q . "
    "&& echo DNS-OPEN-$ns${t:-+udp} || echo dns-direct-blocked-$ns${t:-+udp}; done; done; "
    "pgrep -x dnsmasq >/dev/null && echo dnsmasq-running || echo DNSMASQ-MISSING; "
    "pkill -x dnsmasq 2>/dev/null; pkill -9 -x dnsmasq 2>/dev/null; sleep 1; "
    "pgrep -x dnsmasq >/dev/null && echo dnsmasq-survived || echo DNSMASQ-KILLED; "
    "touch /run/hands-dns/dnsmasq.conf 2>/dev/null && echo DNSCONF-WRITABLE || echo dnsconf-readonly; "
    "timeout 5 bash -c 'exec 3<>/dev/tcp/140.82.112.3/22' 2>/dev/null && echo LEAK-SSH || echo ssh-blocked; "
    "timeout 5 bash -c 'exec 3<>/dev/tcp/140.82.112.3/443' 2>/dev/null && echo LEAK-GHIP || echo ghip-blocked; "
    "getent hosts api.github.com >/dev/null && echo GH-DNS-RESOLVED || echo gh-dns-blocked; "
    "curl -s -o /dev/null -w 'github=%{http_code}\\n' --connect-timeout 5 https://api.github.com/zen; "
    "GW=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}'); "
    "[ -n \"$GW\" ] && timeout 5 bash -c \"exec 3<>/dev/tcp/$GW/22\" 2>/dev/null && echo LEAK-HOST || echo host-blocked; "
    "id -u"
)


def firewall_checks(out: str, allowed: set[str] = frozenset()) -> list[tuple[bool, str]]:
    """Turn the probe's markers into (passed, label) pairs. `allowed` = the project's allowed-domains:
    GitHub probes are skipped (reported as passed with a note) when the project allowlists it."""
    lines = out.split()
    has = lines.__contains__
    checks = [
        (has("blocked") and not has("LEAK"), "egress blocked for non-allowlisted sites"),
        (has("ip-blocked") and not has("LEAK-IP"), "egress blocked to a raw outside IP (1.1.1.1:80)"),
        (any(l.startswith("anthropic=") and l != "anthropic=000" for l in lines), "api.anthropic.com reachable"),
        (has("cannot-flush") and has("no-sudo"), "agent cannot change firewall or use sudo"),
        (has("v6-blocked") and not has("LEAK6"), "no IPv6 egress"),
        (has("dns-allowed-ok"), "DNS: allowlisted name resolves"),
        (has("dns-random-blocked"), "DNS: non-allowlisted name does not resolve"),
        (not any(l.startswith("DNS-OPEN-") for l in lines)
         and sum(l.startswith("dns-direct-blocked-") for l in lines) == 4,
         "DNS: direct queries to 1.1.1.1/8.8.8.8 (udp+tcp) blocked"),
        (has("dnsmasq-running") and has("dnsmasq-survived") and has("dnsconf-readonly"),
         "DNS: agent cannot kill or reconfigure dnsmasq"),
        (has("ssh-blocked") and not has("LEAK-SSH"), "egress blocked on tcp/22 to an outside IP"),
        (has("host-blocked") and not has("LEAK-HOST"), "egress blocked to the host (default gateway)"),
    ]
    gh = {"github.com", "api.github.com"} & allowed
    if gh:
        checks.append((True, f"GitHub probes skipped: project allowlists {', '.join(sorted(gh))}"))
    else:
        checks.append((has("ghip-blocked") and not has("LEAK-GHIP"), "egress blocked to a GitHub IP (not allowlisted)"))
        checks.append((has("gh-dns-blocked") and has("github=000"), "api.github.com unreachable (not allowlisted)"))
    return checks


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
    from . import vendored
    found = vendored.scan(cfg.runner_home)
    for kind in ("kits", "skills"):
        for item in found[kind]:
            if item["ok"]:
                line(True, f"vendored {kind[:-1]} {item['name']} {item.get('version', '')}".rstrip())
            else:
                print(f"[warn] vendored {kind[:-1]} {item['name']} skipped (never mounted): {'; '.join(item['problems'])}")
    try:
        commit = gitops.refresh_spec(cfg.spec_dir, cfg.spec_branch)
        r.spec = specmod.load(cfg.spec_dir, commit)
        line(True, f"spec valid at {commit[:9]} ({len(r.spec.steps)} steps)")
    except Exception as e:
        line(False, f"spec: {e}")
        return 1
    r.recover()                         # never leave an unaccepted candidate where a push could pick it up
    try:
        line(True, f"app repo vs origin: {gitops.ahead_behind(cfg.app_dir, r.branch)} (report only, nothing pushed)")
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
    # firewall: blocked + allowed, DNS filtered, and the agent cannot undo any of it
    fw = docker.run(cfg, Run(
        name=docker.unique_name(cfg, "probe", "fw"), role="probe",
        cmd=["bash", "-c", FIREWALL_PROBE],
        mounts=docker.base_mounts(cfg, r.spec.root), stdout=pdir / "fw.txt", stderr=pdir / "fw.err", timeout_s=300))
    out = (pdir / "fw.txt").read_text() if (pdir / "fw.txt").exists() else ""
    from . import planner
    for want, label in firewall_checks(out, set(planner.domains(r.spec.root / "allowed-domains"))):
        line(fw == 0 and want, label)
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
