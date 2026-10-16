"""Planner: idea -> researched spec (steps.json, docs, acceptance tests) -> PR to <slug>-specs -> approve -> build.

  hands <p> plan        (optional feedback on stdin)  run the planner; opens/updates PR hands/plan
  hands <p> approve                                    merge that exact PR head, refresh the spec, start the build

Trust: the planner session is untrusted like any agent (it reads web search results). Its output is
exported through a --network none container, size/type-checked, limited to the spec output contract,
validated by the runner's own spec loader, and only reaches main through the human's Approve click,
which merges exactly the reviewed commit (sha-pinned). The GitHub token is read by this process only.
"""
from __future__ import annotations

import json
import os
import re
import time
from base64 import b64encode
from pathlib import Path

from . import accview, claude, docker, gitops, workspace
from . import spec as specmod
from .config import Config
from .docker import Mount, Run
from .state import Lock

BRANCH = "hands/plan"
MAX_ROUNDS = 3                    # first attempt + 2 fix rounds after validation errors
MAX_STEPS = 60
HOST = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
ALLOWED = re.compile(r"^(?:steps\.json|PLAN\.md|allowed-domains|docs/[A-Za-z0-9._/-]+\.md|acceptance/[A-Za-z0-9._/@+-]+)$")


class PlanError(Exception):
    pass


def pdir(cfg: Config) -> Path:
    return cfg.state_dir / "plan"


def read_state(cfg: Config) -> dict:
    try:
        return json.loads((pdir(cfg) / "plan.json").read_text())
    except (OSError, ValueError):
        return {"state": "none"}


def write_state(cfg: Config, **kw) -> dict:
    d = pdir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    st = {**read_state(cfg), **kw, "updated": int(time.time())}
    import threading
    tmp = d / f".plan.json.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, d / "plan.json")
    return st


class Log:
    def __init__(self, cfg: Config):
        pdir(cfg).mkdir(parents=True, exist_ok=True)
        self.path = pdir(cfg) / "plan.log"
        from .notify import Notifier
        self.notify = Notifier(cfg.telegram_env, cfg.name, log=lambda m: None)

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(self.path, "a") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- checks on planner output
def changed_files(spec_dir: Path, candidate: Path, names: list[str]) -> list[str]:
    out = []
    for n in names:
        base = spec_dir / n
        new = candidate / n
        if base.is_file() and not base.is_symlink() and base.read_bytes() == new.read_bytes():
            continue
        out.append(n)
    return sorted(out)


def check_output(cfg: Config, candidate: Path, changed: list[str]) -> list[str]:
    """Problems with the candidate spec (empty = good)."""
    errs = [f"{p}: not part of the planner output contract (allowed: steps.json, PLAN.md, allowed-domains, "
            f"docs/*.md, acceptance/**)" for p in changed if not ALLOWED.match(p) or ".." in p.split("/")]
    for need in ("steps.json", "PLAN.md", "docs/design.md", "docs/plan.md", "docs/conventions.md"):
        if not (candidate / need).is_file():
            errs.append(f"{need} missing")
    if errs:
        return errs
    try:
        sp = specmod.load(candidate, "candidate")
    except specmod.SpecError as e:
        return [f"steps.json invalid: {e}"]
    printable = re.compile(r"^[\x20-\x7e]*$")
    for label, vals in (("setup", sp.setup), ("regression", sp.regression), ("e2e_command", [sp.e2e_command]),
                        ("allowed_paths", sp.allowed_paths), ("allow_text_paths", sp.allow_text_paths),
                        *[(f"{s.id}.acceptance", s.acceptance) for s in sp.steps],
                        *[(f"{s.id}.acceptance_files", s.acceptance_files) for s in sp.steps]):
        for v in vals:
            if not printable.match(v) or len(v) > 400:
                errs.append(f"{label}: {v[:80]!r} must be plain printable ASCII, at most 400 characters")
    errs += check_playwright(candidate)
    if not errs:
        errs += check_pins(sp, cfg.min_release_age_days)
    if len(sp.steps) > MAX_STEPS:
        errs.append(f"{len(sp.steps)} steps; keep it under {MAX_STEPS} (plan a first release)")
    if not any(s.type == "code" for s in sp.steps):
        errs.append("no code steps")
    if "docs/conventions.md" not in sp.always_refs:
        errs.append('always_refs must include "docs/conventions.md"')
    ad = candidate / "allowed-domains"
    if ad.is_file():
        for ln in ad.read_text(errors="replace").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and not HOST.match(ln):
                errs.append(f"allowed-domains: {ln!r} is not an exact lowercase hostname")
    shared, by_step = accview.views(sp)
    for f in sorted(shared):
        if re.search(r"\.(test|spec)\.[A-Za-z]+$", f) and not any(f in str(c) for c in [*sp.setup, *sp.regression, sp.e2e_command]):
            errs.append(f"acceptance/{f} is not referenced by any step (every test must belong to a step)")
    return errs


NPM_REGISTRY = "https://registry.npmjs.org"


def registry() -> str:
    v = os.environ.get("HANDS_NPM_REGISTRY")
    return v if (v and os.environ.get("HANDS_ALLOW_TEST_API") == "1") else NPM_REGISTRY


def publish_time(name: str, version: str) -> float | None:
    """Epoch seconds when name@version was published, from the npm registry (None if not found)."""
    import urllib.error
    import urllib.parse
    import urllib.request
    from datetime import datetime
    url = f"{registry()}/{urllib.parse.quote(name, safe='@')}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read(60_000_000))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise PlanError(f"npm registry: HTTP {e.code} for {name}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise PlanError(f"npm registry unreachable while checking {name}: {type(e).__name__}")
    t = (data.get("time") or {}).get(version) if isinstance(data, dict) else None
    if not isinstance(t, str):
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def check_pins(sp, min_days: float) -> list[str]:
    errs = []
    if not sp.pins:
        return ['steps.json: "pins" is required: every direct npm dependency the plan installs, '
                'name -> exact version (each published at least %g days ago)' % min_days]
    now = time.time()
    for name, ver in sorted(sp.pins.items()):
        t = publish_time(name, ver)
        if t is None:
            errs.append(f"pins: {name}@{ver} does not exist on npm")
        elif now - t < min_days * 86400:
            age = (now - t) / 86400
            errs.append(f"pins: {name}@{ver} was published {age:.1f} days ago; pin a version at least {min_days:g} "
                        f"days old (pnpm refuses brand-new releases)")
    return errs


PW_CONFIG = re.compile(r"(^|/)playwright[^/]*\.config\.[cm]?[jt]s$")


def check_playwright(candidate: Path) -> list[str]:
    """acceptance/ is mounted read-only in every build container: Playwright must write elsewhere."""
    errs = []
    acc = candidate / "acceptance"
    if not acc.is_dir():
        return errs
    for f in sorted(acc.rglob("*")):
        rel = f.relative_to(candidate).as_posix()
        if not f.is_file() or not PW_CONFIG.search(rel):
            continue
        text = f.read_text(errors="replace")
        m = re.search(r"outputDir\s*:\s*([^,\n]+)", text)
        if not m or not re.search(r"tmpdir\(\)|['\"]/tmp/", m.group(1)):
            errs.append(f"{rel}: set outputDir under os.tmpdir() (e.g. path.join(os.tmpdir(), 'pw-results')); "
                        f"acceptance/ is read-only in the build containers")
        if re.search(r"['\"](html|blob|json|junit)['\"]", text):
            errs.append(f"{rel}: use only the list/line/dot reporters (others write files into acceptance/)")
    return errs


def domains(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(errors="replace").splitlines() if ln.strip() and not ln.startswith("#")]


# ---------------------------------------------------------------- session
def prompt(cfg: Config, idea: str, feedback: str, errors: list[str], revising: bool) -> str:
    parts = [f"# Plan project `{cfg.name}`", "## The idea (from the human, verbatim)\n" + idea.strip()]
    if revising:
        parts.append("## Revise the existing plan\nYour previous plan is already in /workspace. Change what the "
                     "feedback and errors below require; keep the rest unless it conflicts.")
    if feedback.strip():
        parts.append("## Reviewer feedback on the previous plan (from the human)\n" + feedback.strip())
    if errors:
        parts.append("## The runner rejected the output; fix all of these\n" + "\n".join(f"- {e}" for e in errors[:40]))
    parts.append(f"Write the output files in /workspace now. Project slug for steps.json: `{cfg.name}`.")
    return "\n\n".join(parts) + "\n"


def run_session(cfg: Config, work: Path, prompt_file: Path, out: Path, err: Path, log: Log) -> claude.Result:
    model = cfg.planner_model or cfg.coder_model
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
           "--max-turns", str(cfg.planner_max_turns),
           "--dangerously-skip-permissions", "--no-session-persistence",
           "--append-system-prompt-file", "/opt/hands/prompts/planner.md"]
    if cfg.fallback_model and cfg.fallback_model != model:
        cmd += ["--fallback-model", cfg.fallback_model]
    env = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_WEB_FETCH": "1",
           "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
    mounts = [*docker.base_mounts(cfg, cfg.spec_dir), Mount(cfg.runner_home / "prompts", "/opt/hands/prompts"),
              Mount(work, "/workspace", ro=False)]
    skills = cfg.runner_home / "planner-skills"
    if skills.is_dir():
        mounts.append(Mount(skills, "/home/node/.claude/skills"))
    disallowed = "WebFetch" if cfg.planner_web_search else "WebFetch,WebSearch"
    cmd += ["--disallowedTools", disallowed]
    log(f"planner session ({model}, web search {'on' if cfg.planner_web_search else 'off'}) started")
    t0 = time.time()
    out.write_bytes(b"")
    seen: set[str] = set()
    last = [0.0]
    live: dict = {"activity": {"text": "planner session started", "at": int(time.time())}, "files_written": []}
    pdir(cfg).mkdir(parents=True, exist_ok=True)
    (pdir(cfg) / "plan-activity.json").write_text(json.dumps(live))

    def on_item(item: dict) -> None:
        f = item.get("file", "")
        if item["kind"] in ("write", "search", "agent", "skill") and (item["kind"] != "write" or f not in seen):
            seen.add(f)
            log(item["text"])
        if item["kind"] in ("write", "search", "agent", "skill") or time.time() - last[0] > 5:
            last[0] = time.time()
            act = {"text": item["text"], "at": item["at"]}
            if f:
                act["file"] = f
                act["phase"] = phase_for(f)
            # tail thread: its own file, never plan.json (the main thread owns that)
            prev = live.get("activity") or {}
            if "file" not in act and prev.get("file"):
                act["file"], act["phase"] = prev["file"], prev.get("phase", "")
            live["activity"] = act
            if f and item["kind"] == "write":
                live["files_written"] = sorted(set(live.get("files_written") or []) | {f})
            p = pdir(cfg) / "plan-activity.json"
            tmp = p.with_name(".plan-activity.json.tmp")
            try:
                tmp.write_text(json.dumps(live))
                os.replace(tmp, p)
            except OSError:
                pass

    from . import activity
    with activity.Tail(out, on_item):
        rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "planner", "plan"), role="planner", cmd=cmd,
                                 mounts=mounts, env=env, env_files=[cfg.claude_env], stdin=prompt_file, stdout=out,
                                 stderr=err, timeout_s=cfg.planner_timeout_min * 60, append_stdout=True))
    res = claude.parse(out, rc, err)
    log(f"planner session ended: kind={res.kind} turns={res.turns} cost~${res.cost:.2f} {time.time() - t0:.0f}s")
    return res


PHASES = [("docs/design.md", "Design (brainstorming)"), ("docs/plan.md", "Task plan (writing-plans)"),
          ("docs/conventions.md", "Conventions"), ("acceptance/", "Acceptance tests"), ("steps.json", "Machine spec"),
          ("PLAN.md", "Review summary"), ("allowed-domains", "Network domains")]


def phase_for(path: str) -> str:
    for pre, label in PHASES:
        if path == pre or path.startswith(pre):
            return label
    return "Other files" if path.startswith("docs/") else "Working"


def export(cfg: Config, work: Path, dest: Path, tmp: Path) -> list[str]:
    workspace.rmtree(tmp)
    tmp.mkdir(parents=True)
    rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "export", "plan"), role="export",
                             cmd=["bash", "-c", workspace.EXPORT_SCRIPT_ALL], network=False,
                             mounts=[*docker.base_mounts(cfg, cfg.spec_dir), Mount(work, "/workspace", ro=True),
                                     Mount(tmp, "/out", ro=False)],
                             stderr=tmp.parent / "export.stderr", timeout_s=600))
    try:
        if rc != 0:
            raise PlanError(f"export failed (rc={rc})")
        return workspace.extract_tree(tmp / "tree.tar", dest)
    except workspace.ExportError as e:
        raise PlanError(f"planner output rejected: {e}")
    finally:
        workspace.rmtree(tmp)


def make_work(cfg: Config, work: Path, overlay: Path | None) -> None:
    workspace.rmtree(work)
    gitops.git(work.parent, "clone", "-q", "--no-hardlinks", str(cfg.spec_dir), work.name)
    gitops.git(work, "remote", "remove", "origin", check=False)
    if overlay and overlay.is_dir():
        for root, dirs, files in os.walk(overlay, followlinks=False):
            for f in files:
                src = Path(root) / f
                rel = src.relative_to(overlay)
                if rel.parts[0] == ".git" or src.is_symlink() or not ALLOWED.match(rel.as_posix()):
                    continue            # only contract files carry over; anything else was already reported
                dst = work / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())


# ---------------------------------------------------------------- GitHub
def github():
    from . import newproject
    return newproject.GitHub(newproject.token())


def open_pr(cfg: Config, candidate: Path, changed: list[str], title: str, body: str, log: Log) -> dict:
    if not (cfg.managed and cfg.github_org and cfg.repo_specs):
        raise PlanError("project is not managed by hands (no github_org/repo_specs in its config)")
    gh = github()
    org, repo = cfg.github_org, cfg.repo_specs
    _, ref = gh.call("GET", f"/repos/{org}/{repo}/git/ref/heads/{cfg.branch}")
    base = ref["object"]["sha"]
    local = gitops.head(cfg.spec_dir)
    if base != local:
        raise PlanError(f"spec repo main moved during planning ({local[:9]} -> {base[:9]}); plan again")
    _, bc = gh.call("GET", f"/repos/{org}/{repo}/git/commits/{base}")
    entries = []
    for path in changed:
        data = (candidate / path).read_bytes()
        _, blob = gh.call("POST", f"/repos/{org}/{repo}/git/blobs",
                          {"content": b64encode(data).decode(), "encoding": "base64"})
        mode = "100755" if os.stat(candidate / path).st_mode & 0o100 else "100644"
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    _, tree = gh.call("POST", f"/repos/{org}/{repo}/git/trees", {"base_tree": bc["tree"]["sha"], "tree": entries})
    _, commit = gh.call("POST", f"/repos/{org}/{repo}/git/commits",
                        {"message": f"{title}\n\nGenerated by the hands planner.", "tree": tree["sha"], "parents": [base]})
    head = commit["sha"]
    code, _ = gh.call("GET", f"/repos/{org}/{repo}/git/ref/heads/{BRANCH}", ok=(200, 404))
    if code == 200:
        gh.call("PATCH", f"/repos/{org}/{repo}/git/refs/heads/{BRANCH}", {"sha": head, "force": True})
    else:
        gh.call("POST", f"/repos/{org}/{repo}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": head})
    _, prs = gh.call("GET", f"/repos/{org}/{repo}/pulls?state=open&head={org}:{BRANCH}")
    items = prs.get("items") or []
    if items:
        pr = items[0]
        gh.call("PATCH", f"/repos/{org}/{repo}/pulls/{pr['number']}", {"title": title, "body": body})
    else:
        _, pr = gh.call("POST", f"/repos/{org}/{repo}/pulls",
                        {"title": title, "head": BRANCH, "base": cfg.branch, "body": body})
    log(f"PR #{pr['number']} {pr.get('html_url', '')} @ {head[:9]}")
    return {"pr": pr["number"], "pr_url": pr.get("html_url", ""), "head_sha": head, "base_sha": base}


# ---------------------------------------------------------------- commands
def plan(cfg: Config, feedback: str = "") -> int:
    log = Log(cfg)
    d = pdir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    try:
        lk = Lock(d / "plan.lock").__enter__()
    except SystemExit:
        log("FAILED: the planner is already running")
        return 1
    try:
        return _plan(cfg, feedback, log)
    except Exception as e:
        msg = str(e) if isinstance(e, (PlanError,)) else f"{type(e).__name__}: {e}"
        log(f"FAILED: {msg}")
        log.notify.send(f"PLAN FAILED: {msg[:800]}")
        cand, prev, reviewed = pdir(cfg) / "candidate", pdir(cfg) / "previous", pdir(cfg) / "reviewed"
        workspace.rmtree(pdir(cfg) / "work")
        workspace.rmtree(prev)
        st = read_state(cfg)
        if reviewed.is_dir() and st.get("head_sha"):
            workspace.rmtree(cand)                  # never show unreviewed, invalid output as "the plan"
            os.replace(reviewed, cand)
            write_state(cfg, state="ready", error=f"re-plan failed, still showing the previous plan: {msg[:1800]}")
        else:
            workspace.rmtree(cand)
            write_state(cfg, state="failed", error=msg[:2000])
        return 1
    finally:
        lk.__exit__(None, None, None)


def _plan(cfg: Config, feedback: str, log: Log) -> int:
    if Lock.held(cfg.state_dir / "runner.lock"):
        raise PlanError("the build is running; stop it before planning")
    prev = read_state(cfg)
    if prev.get("state") == "approved":
        raise PlanError("this project's plan was already approved; re-planning an approved project is not supported yet")
    idea_file = cfg.spec_dir.parent / "description.txt"
    if not idea_file.is_file():
        raise PlanError(f"{idea_file} missing (the idea)")
    idea = idea_file.read_text()
    log("=== planning" + (" (with feedback)" if feedback.strip() else ""))
    commit = gitops.refresh_spec(cfg.spec_dir, cfg.branch)
    if (cfg.spec_dir / "steps.json").exists():
        raise PlanError("the spec repo already has steps.json on main; the planner only writes the first plan")
    write_state(cfg, state="planning", error="", started=int(time.time()), base_sha=commit)
    candidate = pdir(cfg) / "candidate"
    revising = candidate.is_dir() and (candidate / "steps.json").is_file()
    overlay = pdir(cfg) / "previous"
    reviewed = pdir(cfg) / "reviewed"
    workspace.rmtree(overlay)
    workspace.rmtree(reviewed)
    if revising:
        if prev.get("state") == "ready":            # keep the reviewable plan until a new one is ready
            import shutil
            shutil.copytree(candidate, reviewed, symlinks=True)
        os.replace(candidate, overlay)
    errors: list[str] = []
    work = pdir(cfg) / "work"
    for rnd in range(1, MAX_ROUNDS + 1):
        make_work(cfg, work, overlay if (revising or errors) else None)
        pf = pdir(cfg) / f"prompt-{rnd}.md"
        pf.write_text(prompt(cfg, idea, feedback, errors, revising or bool(errors)))
        res = run_session(cfg, work, pf, pdir(cfg) / f"session-{rnd}.json", pdir(cfg) / f"session-{rnd}.stderr", log)
        if res.kind in ("rate_limit", "auth", "model_access", "overloaded"):
            raise PlanError(f"planner session failed ({res.kind}): {res.text[:300]}")
        if "PLAN_FAILED:" in res.text:
            raise PlanError("planner gave up: " + res.text.split("PLAN_FAILED:", 1)[1].strip()[:500])
        names = export(cfg, work, candidate, pdir(cfg) / "export")
        workspace.rmtree(work)
        changed = changed_files(cfg.spec_dir, candidate, names)
        errors = check_output(cfg, candidate, changed)
        if not errors:
            break
        log(f"round {rnd}: {len(errors)} problem(s): " + " | ".join(errors[:5]))
        if rnd == MAX_ROUNDS:
            raise PlanError(f"plan still invalid after {MAX_ROUNDS} rounds: " + " | ".join(errors[:8]))
        workspace.rmtree(overlay)
        os.replace(candidate, overlay)
    workspace.rmtree(overlay)
    workspace.rmtree(reviewed)
    sp = specmod.load(candidate, "candidate")
    summary = res.text.split("PLANNED:", 1)[1].strip()[:300] if "PLANNED:" in res.text else f"{len(sp.steps)} steps"
    plan_md = (candidate / "PLAN.md").read_text(errors="replace")
    new_domains = domains(candidate / "allowed-domains")
    body = (f"**Plan for `{cfg.name}`** — {summary}\n\n"
            + (f"**Adds network access for:** {', '.join(new_domains)}\n\n" if new_domains else "")
            + "Review in the hands dashboard and press **Approve & build** (merges exactly this commit).\n\n---\n\n"
            + plan_md[:55000])
    pr = open_pr(cfg, candidate, changed, f"Plan: {cfg.name}", body, log)
    write_state(cfg, state="ready", error="", summary=summary, files=changed, steps=len(sp.steps),
                domains=new_domains, **pr)
    log(f"READY: {summary}. Review and approve in the dashboard.")
    log.notify.send(f"PLAN READY: {summary[:300]}" + (f"\nNew network domains: {', '.join(new_domains)}" if new_domains else "")
                    + "\nReview and approve in the dashboard.")
    return 0


def approve(cfg: Config, expected_sha: str = "") -> int:
    """Merge exactly the reviewed plan commit. expected_sha = the head the human was shown (dashboard)."""
    log = Log(cfg)
    d = pdir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    try:
        lk = Lock(d / "plan.lock").__enter__()
    except SystemExit:
        log("FAILED: the planner is running")
        return 1
    merged = False
    try:
        st = read_state(cfg)
        if st.get("state") != "ready" or not st.get("pr") or not st.get("head_sha"):
            raise PlanError(f"nothing to approve (plan state: {st.get('state')})")
        if expected_sha and expected_sha != st["head_sha"]:
            raise PlanError("the plan changed since you looked at it; review the new version, then approve again")
        if Lock.held(cfg.state_dir / "runner.lock"):
            raise PlanError("the build is already running")
        gh = github()
        org, repo = cfg.github_org, cfg.repo_specs
        _, pr = gh.call("GET", f"/repos/{org}/{repo}/pulls/{st['pr']}")
        if pr.get("state") != "open":
            raise PlanError(f"PR #{st['pr']} is {pr.get('state')}")
        if (pr.get("head") or {}).get("sha") != st["head_sha"]:
            raise PlanError("the PR changed after it was reviewed (head commit differs); plan again or review on GitHub")
        _, ref = gh.call("GET", f"/repos/{org}/{repo}/git/ref/heads/{cfg.branch}")
        if ref["object"]["sha"] != st.get("base_sha"):
            raise PlanError(f"{cfg.branch} moved since planning, so merging would combine unreviewed changes; re-plan")
        _, hc = gh.call("GET", f"/repos/{org}/{repo}/git/commits/{st['head_sha']}")
        code, res = gh.call("PUT", f"/repos/{org}/{repo}/pulls/{st['pr']}/merge",
                            {"merge_method": "squash", "sha": st["head_sha"], "commit_title": f"Plan: {cfg.name}"},
                            ok=(200, 405, 409, 422))
        if code != 200:
            raise PlanError(f"GitHub refused the merge (HTTP {code}: {res.get('message', '')}). "
                            f"Approve/merge PR #{st['pr']} on GitHub, then press Start.")
        merged = True
        write_state(cfg, state="approved", error="", merged_sha=res.get("sha", ""))
        log(f"merged PR #{st['pr']} ({res.get('sha', '')[:9]})")
        commit = gitops.refresh_spec(cfg.spec_dir, cfg.branch)
        _, mc = gh.call("GET", f"/repos/{org}/{repo}/git/commits/{commit}")
        if (mc.get("tree") or {}).get("sha") != (hc.get("tree") or {}).get("sha"):
            raise PlanError(f"{cfg.branch} after the merge differs from the reviewed plan; check the spec repo "
                            f"before pressing Start")
        specmod.load(cfg.spec_dir, commit)
        write_state(cfg, merged_sha=commit)
    except Exception as e:
        msg = str(e) if isinstance(e, PlanError) else f"{type(e).__name__}: {e}"
        log(f"FAILED: {msg}")
        write_state(cfg, error=msg[:2000])
        return 1
    finally:
        lk.__exit__(None, None, None)
    log("starting the build")
    return START(cfg)


def _start(cfg: Config) -> int:
    from .__main__ import main as cli
    return cli([cfg.name, "start"])


START = _start          # tests replace this (no tmux)
