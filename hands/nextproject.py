"""Phase 2 wiring for one project (a Config): next.json (hands.nextstate), the style-ref pipeline, the setup
choices mirrored into state.json (hands.setup), the planning gate and the planning chat (hands.planchat).

The CLI (`hands <slug> create|plan|start|setup|refs`) and the dashboard API call these; containers only ever
start through hands.docker (style refs, setup) or the Session manager's PTY backend (the planning chat).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import nextstate, setup
from .gitops import GitError
from .state import Lock, State


def file(cfg) -> Path:
    return Path(cfg.state_dir) / nextstate.FILE


def refs_lock(cfg) -> Path:
    return Path(cfg.state_dir) / "style-refs.lock"


def _mirror_setup(cfg, framework: int, libs: list[str], status: str) -> None:
    """state.json's `setup` record (what `hands <slug> setup` reads) follows the choices; setup.run mirrors back."""
    st = State(cfg.state_dir)
    setup._set_setup(st, setup.SetupOptions(framework=framework, libs=list(libs)), status)


def init(cfg, style_refs: list[str], framework: int, libs: list[str]) -> dict:
    """A new project's choices: next.json written (refs queued), state.json's setup record set."""
    state = nextstate.init(file(cfg), style_refs, framework, libs)
    s = state["setup"]
    _mirror_setup(cfg, s["framework"], s["libs"], s["status"])
    return nextstate.load(file(cfg))


def ensure(cfg) -> dict:
    """next.json for a project created before it existed: no refs, setup taken from state.json."""
    path = file(cfg)
    if not path.exists():
        cur = State(cfg.state_dir).data.get("setup")
        cur = cur if isinstance(cur, dict) else {}
        opts = setup.options_of(State(cfg.state_dir).data)
        state = nextstate.empty()
        state["setup"] = {"framework": opts.framework, "libs": list(opts.libs),
                          "status": cur.get("status") if cur.get("status") in setup.SETUP_STATUSES else "none",
                          "record": cur.get("record")}
        nextstate.save(path, state)
    return nextstate.load(path)


def request(cfg, style_refs: list[str], framework: int | None = None, libs: list[str] | None = None) -> list[int]:
    """A change request: new style ref URLs (queued; returns their indexes) and/or new setup choices.
    Everything is validated before anything is written."""
    state = ensure(cfg)
    urls = nextstate.check_urls(list(style_refs or []))
    known = {r["url"] for r in state["style_refs"]}
    dup = [u for u in urls if u in known]
    if dup:
        raise nextstate.InvalidInput(f"duplicate style ref url: {dup[0]}")
    if len(state["style_refs"]) + len(urls) > nextstate.MAX_REFS:
        raise nextstate.TooManyRefs()
    opts = None
    if framework is not None or libs is not None:
        s = state["setup"]
        opts = nextstate.check_options(s.get("framework", 1) if framework is None else framework,
                                       s.get("libs", []) if libs is None else libs)
    indexes = [nextstate.add_ref(file(cfg), u) for u in urls]
    s = state["setup"]
    if opts is not None and (opts.framework, opts.libs) != (s.get("framework"), s.get("libs")):
        _mirror_setup(cfg, opts.framework, opts.libs, nextstate.setup_status_for(opts.framework, opts.libs))
    return indexes


def setup_pending(cfg) -> bool:
    return nextstate.load(file(cfg))["setup"]["status"] == "pending"


def gate_reasons(cfg) -> list[str]:
    """Why planning or the build may not start: next.json's gate plus state.json's setup status (projects
    without next.json still have the S04 setup gate)."""
    reasons = nextstate.gate(nextstate.load(file(cfg)))
    blocked = setup.blocking_status(State(cfg.state_dir).data)
    if blocked and not any(r.startswith("setup ") for r in reasons):
        reasons.append(f"setup {blocked}")
    return reasons


def busy(cfg) -> str:
    """Why the style-ref pipeline must not run now ('' when it may)."""
    from . import planner
    if Lock.held(Path(cfg.state_dir) / "runner.lock"):
        return "the build (or the scaffold) is running; stop it first"
    if Lock.held(planner.pdir(cfg) / "plan.lock"):
        return "the planner is running"
    if Lock.held(refs_lock(cfg)):
        return "style refs are being processed"
    return ""


def process_refs(cfg, indexes: list[int]) -> int:
    """Run the pipeline for each index (under the project's style-refs lock); returns how many failed."""
    from . import stylerefs
    refs_lock(cfg).parent.mkdir(parents=True, exist_ok=True)
    failed = 0
    with Lock(refs_lock(cfg)):
        for i in indexes:
            if stylerefs.process(cfg, i) != "frozen":
                failed += 1
    return failed


def retry_ref(cfg, index: int) -> None:
    """Back to queued (the pipeline runs it next); UnknownRef when there is no such ref."""
    nextstate.ref(nextstate.load(file(cfg)), index)
    nextstate.set_ref(file(cfg), index, "queued")


def remove_ref(cfg, index: int) -> None:
    """Drop ref `index`: refs/style-refs/<nn> leaves the spec repo (committed), then the entry leaves next.json."""
    from . import stylerefs
    nextstate.ref(nextstate.load(file(cfg)), index)
    stylerefs.remove(Path(cfg.spec_dir), index)
    shutil.rmtree(stylerefs.work_dir(cfg, index), ignore_errors=True)
    nextstate.remove_ref(file(cfg), index)


def info(cfg) -> dict:
    """The project's Phase 2 state for the dashboard: style refs, setup, planning chat and the gate."""
    try:
        state = nextstate.load(file(cfg))
    except nextstate.InvalidInput as e:
        return {"error": str(e)[:300], "style_refs": [], "setup": None, "plan_session": None, "gate": [str(e)[:300]]}
    s = state["setup"]
    return {"style_refs": [{k: r.get(k) for k in ("index", "url", "added", "status", "reason")} for r in state["style_refs"]],
            "setup": {"framework": s.get("framework"), "libs": s.get("libs"), "status": s.get("status"),
                      "label": setup.FRAMEWORK_LABELS.get(s.get("framework"), "")},
            "plan_session": plan_session_view(cfg, state), "gate": gate_reasons(cfg)}


# ------------------------------------------------------------------------------------------ planning chat

def chat_dir(cfg) -> Path:
    return Path(cfg.state_dir) / "plan-chat"


def brief_dir(cfg) -> Path:
    return chat_dir(cfg) / "brief"


def chat_skills_dir(cfg) -> Path:
    """The skills mounted read-only in the planning chat. The planner's set (brainstorming, writing-plans) when
    the runner ships it -- the primer tells the chat to use the brainstorming skill, and hands.planner mounts the
    same folder -- else the project's skills_dir, else an empty folder (the mount is always present)."""
    planner_skills = Path(cfg.runner_home) / "planner-skills"
    if planner_skills.is_dir():
        return planner_skills
    if cfg.skills_dir and Path(cfg.skills_dir).is_dir():
        return Path(cfg.skills_dir)
    return chat_dir(cfg) / "no-skills"


def _brief_sha(cfg) -> str | None:
    from . import planchat
    try:
        return planchat.brief_hash(brief_dir(cfg))
    except planchat.BriefIncomplete:
        return None


def plan_session_view(cfg, state: dict | None = None) -> dict:
    """plan_session with the status refreshed from the brief dir: brief_ready once brief.md and coverage.md
    exist; approved only while the approved hash is still the brief's hash."""
    from . import planchat
    state = state or nextstate.load(file(cfg))
    p = dict(state["plan_session"])
    if not p.get("session_id"):
        return p
    current = _brief_sha(cfg)
    if p.get("status") == "approved" and p.get("brief_sha256") and p["brief_sha256"] == current:
        return p
    p["status"] = planchat.status(brief_dir(cfg))
    p["brief_sha256"] = current if p["status"] == "brief_ready" else None
    return p


def refresh_plan_session(cfg) -> dict:
    """Store the refreshed plan_session (status/hash) in next.json; returns it."""
    p = plan_session_view(cfg)
    stored = nextstate.load(file(cfg))["plan_session"]
    if (p.get("status"), p.get("brief_sha256")) != (stored.get("status"), stored.get("brief_sha256")):
        path = file(cfg)
        state = nextstate.load(path)
        state["plan_session"].update({"status": p["status"], "brief_sha256": p.get("brief_sha256"),
                                      "updated": nextstate.now_iso()})
        nextstate.save(path, state)
    return p


def plan_context(cfg, state: dict):
    from . import planchat
    existing = bool(cfg.imported) or (Path(cfg.spec_dir) / "steps.json").is_file()
    try:
        description = (Path(cfg.spec_dir).parent / "description.txt").read_text(errors="replace")[:4000]
    except OSError:
        description = ""
    refs = [{"url": r["url"], "report_path": f"{planchat.SPEC_MOUNT}/refs/style-refs/{r['index']:02d}/report.md"}
            for r in state["style_refs"] if r["status"] == "frozen"]
    s = state["setup"]
    setup_ctx = None
    if s.get("framework") != 1 or s.get("libs"):
        setup_ctx = {"framework": s.get("framework"), "libs": list(s.get("libs") or []), "record": s.get("record")}
    return planchat.PlanContext(slug=cfg.name, kind="existing" if existing else "new", description=description,
                                style_refs=refs, setup=setup_ctx, spec_dir=planchat.SPEC_MOUNT,
                                app_dir=planchat.APP_MOUNT if existing else None)


def token_file(cfg) -> Path:
    """The file holding the project's build token (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN): the build
    token file, else the project's variables."""
    from . import planchat
    for f in (cfg.claude_env, cfg.project_env):
        if f and planchat.token_in(Path(f)) is not None:
            return Path(f)
    return Path(cfg.claude_env)


def _cleanup(docker_bin: str, name: str, temp_files: list[Path]):
    """After the chat's terminal ends: the per-run files (the one-variable env file and the allowed-domains
    file) go, and so does a container left behind."""
    def run() -> None:
        for f in temp_files:
            f.unlink(missing_ok=True)
        subprocess.run([docker_bin, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return run


def plan_chat_start(cfg) -> dict:
    """Start (first time: new session id + primer) or resume (`claude --resume <id>`) the project's planning chat
    as a hidden Session manager session running in the firewalled container; returns {session_id, status, live}.
    A chat that is already running is left alone (the caller attaches to it)."""
    from . import docker, planchat, sessions
    state = ensure(cfg)
    ps = planchat.PlanSession.from_json(state["plan_session"])
    if ps.session_id and sessions.BACKEND.alive(ps.session_id):
        return {"session_id": ps.session_id, "status": plan_session_view(cfg)["status"], "live": True}
    d = chat_dir(cfg)
    skills = chat_skills_dir(cfg)
    for p in (brief_dir(cfg), d / "home", d / "env", skills):
        Path(p).mkdir(parents=True, exist_ok=True)
    ctx = plan_context(cfg, state)
    paths = planchat.Paths(spec_dir=Path(cfg.spec_dir), app_dir=Path(cfg.app_dir) if ctx.app_dir else None,
                           brief_dir=brief_dir(cfg), home_dir=d / "home", skills_dir=Path(skills), env_dir=d / "env",
                           secrets_file=token_file(cfg))
    try:
        spec = planchat.launch_spec(ctx, ps, paths, docker.firewall_args(cfg), image=cfg.image)
    except planchat.TokenMissing:
        raise ValueError(f"no build token: {' or '.join(planchat.TOKEN_KEYS)} in the build token file or the "
                         "project's variables")
    argv = [cfg.docker_bin, *spec["argv"][1:]]
    temp_files = [Path(f) for f in spec["temp_files"]]
    name = argv[argv.index("--name") + 1]
    sid = spec["session_id"]
    try:
        sessions.start_plan_chat(sid, cfg.name, brief_dir(cfg), argv,
                                 on_exit=_cleanup(cfg.docker_bin, name, temp_files))
    except BaseException:
        for f in temp_files:
            f.unlink(missing_ok=True)
        raise
    status = ps.status if ps.status != "none" else "open"
    nextstate.set_plan_session(file(cfg), status, session_id=sid)
    return {"session_id": sid, "status": refresh_plan_session(cfg)["status"], "live": True}


def change_round(cfg) -> int | None:
    """None for the first brief; afterwards the next change number (briefs/change-<n>.md)."""
    spec = Path(cfg.spec_dir)
    if not (spec / "brief.md").is_file():
        return None
    nums = [int(m.group(1)) for p in (spec / "briefs").glob("change-*.md")
            if (m := re.fullmatch(r"change-(\d{1,4})\.md", p.name))]
    return max(nums, default=0) + 1


def plan_chat_approve(cfg, sha: str) -> dict:
    """Hash-pinned approval: the brief the page showed goes into the spec repo (planchat.approve)."""
    from . import planchat
    state = nextstate.load(file(cfg))
    if not state["plan_session"].get("session_id"):
        raise ValueError("no planning chat for this project")
    if not isinstance(sha, str) or not nextstate.SHA_RE.match(sha.strip().lower()):
        raise ValueError("sha256: 64 hex characters")
    try:
        res = planchat.approve(brief_dir(cfg), Path(cfg.spec_dir), sha.strip().lower(), change_round(cfg))
    except planchat.HashMismatch as e:
        raise ValueError(str(e))
    except planchat.BriefIncomplete as e:
        raise ValueError(f"brief not ready: {e}")
    except GitError as e:
        raise ValueError(f"spec repo commit failed: {str(e)[:300]}")
    nextstate.set_plan_session(file(cfg), "approved", sha=res["sha256"])
    return res
