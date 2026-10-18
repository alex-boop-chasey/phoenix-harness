"""hands dashboard: a local web UI over the runner's state.

  hands-dash run [--port 8090]   serve in the foreground
  hands-dash start               serve in tmux session hands-dash (also done by `hands all watchdog`)
  hands-dash stop                stop it
  hands-dash url                 print the one-time login URL (open it through the SSH tunnel)

Security model (the dashboard can start builds, so it is treated as a control plane):
- binds 127.0.0.1 only; reach it with  ssh -L 8090:127.0.0.1:8090 builder
- login: `hands-dash url` (needs a shell as builder) writes a single-use code, valid 5 minutes, to
  ~/.config/hands/dashboard-login (0600); /login?c=<code> exchanges it for a random session id
  (in memory only, 12 h) in an HttpOnly SameSite=Strict cookie. No long-lived secret ever leaves the VPS.
- Host must be exactly 127.0.0.1[:port] (blocks DNS rebinding, and keeps the cookie away from dev
  servers on "localhost"); POSTs also need a matching Origin and an X-Hands header (blocks CSRF)
- bounded threads, socket timeouts, bounded reads; a kept-alive connection may wait KEEPALIVE_IDLE seconds
  for its next request (longer than any browser keeps an idle connection, so the browser always closes
  first and never sends a POST into a socket we just closed: Safari reports that as "Load failed")
- every request and connection error is logged to ~/.config/hands/dashboard.log (path only, no query)
- strict CSP, no inline script; everything an agent could have influenced (logs, prompts, diffs,
  verdicts) is rendered as text, never HTML
- read-only over state files except: STOP file, and `hands <p> start|ack` run as subprocesses;
  it owns ~/.config/hands/ui.json (closed tabs), sessions/, secrets/<slug>/
- it never holds the GitHub token and never mounts anything
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as cfgmod
from . import gitops
from . import sessions
from . import spec as specmod
from . import ws
from .state import Lock

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8090
SESSION = "hands_dashboard"     # "_" cannot occur in a project slug, so no clash with hands-<slug>
COOKIE = "hands_dash"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
ATTEMPT = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,40}-\d{1,4}(?:\.\d{1,12})?$")
ATTEMPT_FILE = re.compile(
    r"^(?:prompt\.md|eval-prompt\.md|reasons\.txt|verdict\.json|changes\.txt|diff\.patch|acceptance-files\.txt"
    r"|accept-\d{1,3}\.log|coder\.stderr|export\.stderr|eval-\d\.stderr|spec-problem\.txt)$")
# coder.json / eval-N.json are full session streams (every tool result, possibly secrets): not served.
# Everything served from attempts/ and runner.log is passed through the project's Redactor again at read
# time (redactor_for), because the runner redacts attempt files only after each session ends.
LOCAL_HOST = re.compile(r"^127\.0\.0\.1(?::\d{1,5})?$")
SESSION_TTL = 12 * 3600
LOGIN_TTL = 300
MAX_CONN = 32
KEEPALIVE_IDLE = 120        # seconds a kept-alive connection may wait for its next request (step 17)
LOG_MAX = 2_000_000         # dashboard.log is rotated to dashboard.log.1 at this size
MAX_FILE = 2_000_000
MAX_LOG_BYTES = 1_000_000
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/sessions.js": ("sessions.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                # xterm.js 5.5.0 + fit addon, vendored (no CDN; CSP is script-src 'self')
                "/vendor/xterm.min.js": ("vendor/xterm.min.js", "text/javascript; charset=utf-8"),
                "/vendor/addon-fit.min.js": ("vendor/addon-fit.min.js", "text/javascript; charset=utf-8"),
                "/vendor/xterm.min.css": ("vendor/xterm.min.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def config_home() -> Path:
    return cfgmod.CONFIG_HOME


def login_path() -> Path:
    return config_home() / "dashboard-login"


def log_path() -> Path:
    return config_home() / "dashboard.log"


_LOG_LOCK = threading.Lock()


def log_line(text: str) -> None:
    """Append one UTC-stamped line to dashboard.log (0600, never through a symlink, rotated once at LOG_MAX).
    Logging must never break a request: every failure here is swallowed."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {text}\n"
    try:
        with _LOG_LOCK:
            p = log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                if os.lstat(p).st_size > LOG_MAX:
                    os.replace(p, p.with_suffix(".log.1"))
            except OSError:
                pass
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, line.encode("utf-8", errors="replace"))
            finally:
                os.close(fd)
    except OSError:
        pass


def mint_login_code() -> str:
    """Single-use login code for the running server (written 0600; replaces any previous code)."""
    p = login_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink() or p.exists():
        p.unlink()
    code = secrets.token_urlsafe(24)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{code} {int(time.time()) + LOGIN_TTL}\n")
    return code


def redeem_login_code(code: str) -> bool:
    p = login_path()
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
            return False
        parts = os.read(fd, 200).decode(errors="replace").split()
    finally:
        os.close(fd)
    if len(parts) != 2 or not parts[1].isdigit():
        return False
    good = hmac.compare_digest(code.encode(), parts[0].encode()) and time.time() < int(parts[1])
    if good:
        try:
            p.unlink()          # single use
        except OSError:
            return False
    return good


# ---------------------------------------------------------------- safe reads (state dirs are runner-owned,
# but attempt logs contain agent-influenced text; still never follow symlinks, never read unbounded)
def read_file(path: Path, limit: int = MAX_FILE, tail: bool = False) -> tuple[str, bool]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        size = st.st_size
        if tail and size > limit:
            os.lseek(fd, size - limit, os.SEEK_SET)
        data = b""
        while len(data) < limit:
            chunk = os.read(fd, min(65536, limit - len(data)))
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", errors="replace"), size > limit
    finally:
        os.close(fd)


def read_json(path: Path, default):
    try:
        return json.loads(read_file(path)[0])
    except (OSError, ValueError):
        return default


def read_jsonl(path: Path, last: int) -> list[dict]:
    try:
        text, _ = read_file(path, MAX_LOG_BYTES, tail=True)
    except OSError:
        return []
    out = []
    for line in text.splitlines()[-last:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------- data
def project_names() -> list[str]:
    d = config_home() / "projects"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml") if NAME.match(p.stem) and p.stem != "all")


def load_cfg(name: str) -> cfgmod.Config:
    if not NAME.match(name) or name not in project_names():
        raise LookupError(name)
    try:
        return cfgmod.load(name)
    except SystemExit as e:
        raise ValueError(str(e)) from e


def summary(name: str) -> dict:
    try:
        cfg = load_cfg(name)
    except ValueError as e:
        return {"name": name, "error": str(e)}
    st = read_json(cfg.state_dir / "state.json", {})
    steps = st.get("steps") or {}
    status = st.get("status", "idle")
    if Lock.held(cfg.state_dir / "plan" / "plan.lock") and not Lock.held(cfg.state_dir / "runner.lock"):
        status = "planning"
    return {"name": name, "status": status, "reason": st.get("reason", ""),
            "alive": Lock.held(cfg.state_dir / "runner.lock"), "stop": (cfg.state_dir / "STOP").exists(),
            "passed": sum(1 for x in steps.values() if x.get("status") == "passed"),
            "current": st.get("current"), "updated_at": st.get("updated_at"),
            "activity": read_json(cfg.state_dir / "activity.json", None) if st.get("current") else None,
            "hidden": name in hidden_projects()}


# ---------------------------------------------------------------- UI state (closed tabs): ~/.config/hands/ui.json
# {"hidden": [name, ...]}. Purely cosmetic: hiding never touches the project's folder, repos, state or config,
# and the runner never reads it.
def ui_path() -> Path:
    return config_home() / "ui.json"


def hidden_projects() -> set[str]:
    d = read_json(ui_path(), {})
    names = d.get("hidden") if isinstance(d, dict) else None
    return {n for n in (names or []) if isinstance(n, str) and NAME.match(n)}


def set_hidden(name: str, hidden: bool) -> dict:
    if not NAME.match(name) or name not in project_names():
        raise LookupError(name)
    with _UI_MUTEX:
        names = hidden_projects()
        (names.add if hidden else names.discard)(name)
        p = ui_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"hidden": sorted(names)}, f, indent=1)
                f.write("\n")
            os.replace(tmp, p)        # atomic: readers see the old or the new file, never a partial one
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return {"ok": True, "name": name, "hidden": hidden}


_UI_MUTEX = threading.Lock()


# ---------------------------------------------------------------- environment variables (hands/projectenv.py)
MAX_BODY = 10000
MAX_ENV_BODY = 65536        # /api/p/<name>/env may carry several 8 kB values


def env_store(name: str):
    """The Store for an existing project, or the pending store for a project whose create is running."""
    from . import newproject, projectenv
    if NAME.match(name) and name in project_names():
        cfg = load_cfg(name)
        p = cfg.project_env or (newproject.secrets_root(name) / projectenv.ENV_FILE)
        for repo in (cfg.app_dir, cfg.spec_dir):
            if repo in p.parents:
                raise ValueError(f"project_env {p} is inside a repo; point it under ~/.config/hands/secrets/")
        return projectenv.Store(p.parent, p.name), {"file": str(p), "active": bool(cfg.project_env), "pending": False}
    try:
        newproject.validate_slug(name)
    except newproject.CreateError:
        raise LookupError(name)
    info = create_info(name)
    if info and info["state"] == "running":
        d = projectenv.pending_dir(name)
        return projectenv.Store(d), {"file": str(d / projectenv.ENV_FILE), "active": False, "pending": True}
    raise LookupError(name)


def env_listing(name: str) -> dict:
    with _UI_MUTEX:
        st, info = env_store(name)
        return {"vars": st.listing(), "problems": st.problems, **info}


def env_update(name: str, req) -> dict:
    """Body: {"set": [{"key","value"?,"public"?}, ...], "delete": [key, ...]} — at least one of them."""
    if not isinstance(req, dict):
        raise ValueError("bad json")
    items, dels = req.get("set"), req.get("delete")
    if items is None and dels is None:
        raise ValueError("nothing to do: give 'set' and/or 'delete'")
    if items is not None and not isinstance(items, list):
        raise ValueError("set must be a list")
    if dels is not None and (not isinstance(dels, list) or not all(isinstance(k, str) for k in dels)):
        raise ValueError("delete must be a list of keys")
    with _UI_MUTEX:
        st, info = env_store(name)
        if items:
            st.set(items)
        for k in dels or []:
            st.delete(k)
        return {"vars": st.listing(), "problems": st.problems, **info}


def detail(name: str) -> dict:
    cfg = load_cfg(name)
    sd = cfg.state_dir
    st = read_json(sd / "state.json", {})
    out = summary(name)
    out.update({
        "model_override": st.get("model_override"),
        "total_sessions": st.get("total_sessions", 0), "total_cost_usd": st.get("total_cost_usd", 0),
        "config": {"coder_model": cfg.coder_model, "eval_model": cfg.eval_model, "fallback_model": cfg.fallback_model,
                   "image": cfg.image, "max_sessions": cfg.max_sessions, "cost_cap_usd": cfg.cost_cap_usd,
                   "push": cfg.push, "branch": cfg.branch},
        "metrics": read_jsonl(sd / "metrics.jsonl", 30),
        "progress": read_jsonl(sd / "progress.jsonl", 30),
    })
    sst = st.get("steps") or {}
    steps, spec_err, spec_commit = [], None, ""
    try:
        spec_commit = gitops.head(cfg.spec_dir)
        sp = specmod.load(cfg.spec_dir, spec_commit)
        for s in sp.steps:
            x = sst.get(s.id, {})
            steps.append({"id": s.id, "title": s.title, "type": s.type, "status": x.get("status", "pending"),
                          "attempts": x.get("attempts", 0), "commit": x.get("commit", ""),
                          "reasons": (x.get("reasons") or [])[:12]})
    except Exception as e:      # spec mid-refresh or invalid: still show state
        spec_err = f"{type(e).__name__}: {e}"
        for sid, x in sst.items():
            steps.append({"id": sid, "title": "", "type": "", "status": x.get("status", "pending"),
                          "attempts": x.get("attempts", 0), "commit": x.get("commit", ""),
                          "reasons": (x.get("reasons") or [])[:12]})
    out.update({"steps": steps, "spec_error": spec_err, "spec_commit": spec_commit,
                "managed": cfg.managed, "setup": setup_info(cfg), "create": create_info(name), "plan": plan_info(cfg)})
    return out


def _count_lines(path: Path, pred) -> int:
    try:
        text, _ = read_file(path, 200_000)
    except OSError:
        return 0
    return sum(1 for ln in text.splitlines() if pred(ln.strip()))


def setup_info(cfg) -> dict:
    """What a human still has to do before/while building (all derived from files, never from agent output)."""
    env_keys = 0
    if cfg.project_env:
        env_keys = _count_lines(cfg.project_env, lambda l: l and not l.startswith("#") and "=" in l)
    desc = ""
    d = cfg.spec_dir.parent / "description.txt"
    if cfg.managed and d.is_file():
        try:
            desc = read_file(d, 5000)[0].strip()
        except OSError:
            pass
    return {"spec_ready": (cfg.spec_dir / "steps.json").is_file(),
            "project_env": str(cfg.project_env or ""), "project_env_keys": env_keys,
            "allowed_domains": _count_lines(cfg.spec_dir / "allowed-domains", lambda l: l and not l.startswith("#")),
            "repo_specs": f"{cfg.github_org}/{cfg.repo_specs}" if cfg.repo_specs else "",
            "repo_app": (cfg.repo_app if "/" in cfg.repo_app else f"{cfg.github_org}/{cfg.repo_app}") if cfg.repo_app else "",
            "imported": bool(cfg.imported),
            "description": desc}


PLAN_PATH = re.compile(r"^(?:steps\.json|PLAN\.md|allowed-domains|docs/[A-Za-z0-9._/-]+|acceptance/[A-Za-z0-9._/@+-]+)$")


def plan_info(cfg) -> dict:
    from . import planner
    d = planner.pdir(cfg)
    st = read_json(d / "plan.json", {"state": "none"})
    if not isinstance(st, dict):
        st = {"state": "none"}
    running = Lock.held(d / "plan.lock")
    if st.get("state") == "planning" and not running and time.time() - int(st.get("updated") or 0) > 20:
        st = {**st, "state": "failed", "error": st.get("error") or "the planner process ended unexpectedly; see the log"}
    out = {k: st.get(k) for k in ("state", "error", "pr", "pr_url", "head_sha", "summary", "files", "domains", "steps",
                                  "activity", "files_written", "started", "warnings")}
    out["running"] = running
    act = read_json(d / "plan-activity.json", None)
    if isinstance(act, dict) and st.get("state") == "planning":
        out["activity"], out["files_written"] = act.get("activity"), act.get("files_written") or []
    try:
        out["log"] = read_file(d / "plan.log", 300_000, tail=True)[0].splitlines()[-150:]
    except OSError:
        out["log"] = []
    cand = d / "candidate"
    out["plan_md"] = ""
    out["step_list"] = []
    if cand.is_dir() and not cand.is_symlink():
        try:
            out["plan_md"] = read_file(cand / "PLAN.md", 200_000)[0]
        except OSError:
            pass
        sj = read_json(cand / "steps.json", None)
        if isinstance(sj, dict):
            for x in (sj.get("steps") or [])[:100]:
                if isinstance(x, dict):
                    out["step_list"].append({k: x.get(k) for k in ("id", "title", "type", "goal", "acceptance", "e2e", "depends_on",
                                                                   "refs", "notes", "acceptance_files", "max_attempts")})
            out["shared"] = {k: sj.get(k) for k in ("allowed_paths", "allow_text_paths", "setup", "regression", "e2e_command",
                                                    "always_refs")}
    return out


def plan_file(name: str, rel: str) -> tuple[str, bool]:
    from . import planner
    cfg = load_cfg(name)
    if not PLAN_PATH.match(rel) or ".." in rel.split("/"):
        raise LookupError(rel)
    cand = planner.pdir(cfg) / "candidate"
    p = cand
    for part in rel.split("/"):
        p = p / part
        if p.is_symlink():
            raise LookupError(rel)
    return read_file(p)


def spawn_cli(log: Path, argv_tail: list[str], stdin_text: str = "") -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.is_symlink():
        log.unlink()
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = str(cfgmod.RUNNER_HOME)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as out:
        p = subprocess.Popen(hands_argv(*argv_tail), stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True, text=True)
        p.stdin.write(stdin_text)
        p.stdin.close()
    threading.Thread(target=p.wait, daemon=True).start()


def plan_action(name: str, action: str, feedback: str, head_sha: str = "") -> dict:
    from . import planner
    cfg = load_cfg(name)
    if not cfg.managed:
        raise ValueError("planning is only for projects created by hands")
    with _CREATE_MUTEX:
        d = planner.pdir(cfg)
        if Lock.held(d / "plan.lock"):
            raise ValueError("the planner is already running")
        if Lock.held(cfg.state_dir / "runner.lock"):
            raise ValueError("the build is running")
        st = read_json(d / "plan.json", {})
        if action == "approve" and (st.get("state") != "ready"):
            raise ValueError("there is no plan ready to approve")
        if action == "approve" and (not re.fullmatch(r"[0-9a-f]{40}", head_sha or "") or head_sha != st.get("head_sha")):
            raise ValueError("the plan changed since this page showed it; review the new version, then approve")
        if action == "plan" and st.get("state") == "approved":
            raise ValueError("the plan was already approved")
        if len(feedback) > 8000:
            raise ValueError("feedback too long (8000 characters max)")
        if action == "plan":
            planner.write_state(cfg, state="planning", error="")     # visible at once; the child takes the lock
        spawn_cli(d / "plan.log", [name, action] + ([head_sha] if action == "approve" else []),
                  feedback if action == "plan" else "")
    return {"rc": 0, "output": f"{action} started"}


def create_info(slug: str) -> dict | None:
    from . import newproject
    st = read_json(newproject.status_path(slug), None)
    if not isinstance(st, dict):
        return None
    if (st.get("state") == "running" and not Lock.held(newproject.lock_path(slug))
            and time.time() - int(st.get("at") or 0) > 20):
        st = {**st, "state": "failed", "error": "the create process ended unexpectedly; see the log"}
    try:
        text, _ = read_file(newproject.creating_dir() / f"{slug}.log", 200_000, tail=True)
    except OSError:
        text = ""
    return {"state": st.get("state"), "error": st.get("error", ""), "lines": text.splitlines()[-200:]}


def start_create(slug: str, description: str, from_repo: str = "", branch: str = "", ruleset: bool = False) -> dict:
    from . import newproject
    try:
        newproject.validate_slug(slug)
        from_repo = (from_repo or "").strip()
        branch = (branch or "").strip()
        if from_repo:
            from_repo = "/".join(newproject.parse_repo(from_repo))
        if branch:
            if not from_repo:
                raise newproject.CreateError("branch is only used with an existing repo")
            newproject.validate_branch(branch)
    except newproject.CreateError as e:
        raise ValueError(str(e))
    description = (description or "").strip()
    if not description or len(description) > 4000:
        raise ValueError("description: 1-4000 characters")
    extra = []
    if from_repo:
        extra += ["--from-repo", from_repo]
        if branch:
            extra += ["--branch", branch]
        if ruleset:
            extra.append("--ruleset")
    with _CREATE_MUTEX:
        if (config_home() / "projects" / f"{slug}.toml").exists():
            raise ValueError(f"project {slug} already exists")
        info = create_info(slug)
        if Lock.held(newproject.lock_path(slug)) or (info and info["state"] == "running"):
            raise ValueError(f"{slug} is already being created")
        d = newproject.creating_dir()
        d.mkdir(parents=True, exist_ok=True)
        log = d / f"{slug}.log"
        if log.is_symlink():
            log.unlink()
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["PYTHONPATH"] = str(cfgmod.RUNNER_HOME)
        from . import projectenv
        projectenv.discard_pending(slug)             # stale variables from an earlier, failed attempt
        newproject.write_status(slug, "running")     # visible at once; the child rewrites it
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as out:
            out.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} create {slug}\n")
            out.flush()
            try:
                p = subprocess.Popen(hands_argv(slug, "create", "--then-plan", *extra), stdin=subprocess.PIPE, stdout=out,
                                     stderr=subprocess.STDOUT, env=env, start_new_session=True, text=True)
                p.stdin.write(description)
                p.stdin.close()
            except OSError as e:
                newproject.write_status(slug, "failed", f"could not start: {e}")
                raise ValueError(f"could not start create: {e}")
    threading.Thread(target=p.wait, daemon=True).start()     # reap it
    return {"ok": True, "slug": slug}


_CREATE_MUTEX = threading.Lock()
_USAGE_LAST = [0.0]


def usage_refresh() -> dict:
    with _CREATE_MUTEX:
        if time.time() - _USAGE_LAST[0] < 120:
            raise ValueError("refreshed less than 2 minutes ago")
        _USAGE_LAST[0] = time.time()
    spawn_cli(config_home() / "usage-refresh.log", ["all", "usage"])
    return {"ok": True}


def redactor_for(cfg):
    """The project's secret values as a Redactor: served files are redacted again at read time, because the
    runner redacts attempt files only after a session ends (and not at all if it died mid-session)."""
    from . import projectenv
    p = cfg.project_env
    if not p or not p.is_file():
        return projectenv.Redactor({})
    try:
        return projectenv.Redactor(projectenv.Store(p.parent, p.name).secrets())
    except projectenv.EnvError:
        return projectenv.Redactor({})


def log_tail(name: str, lines: int) -> dict:
    cfg = load_cfg(name)
    lines = max(1, min(lines, 2000))
    try:
        text, cut = read_file(cfg.state_dir / "runner.log", MAX_LOG_BYTES, tail=True)
    except OSError:
        return {"lines": []}
    return {"lines": redactor_for(cfg).text(text).splitlines()[-lines:]}


def attempts(name: str) -> list[dict]:
    cfg = load_cfg(name)
    root = cfg.state_dir / "attempts"
    if not root.is_dir() or root.is_symlink():
        return []
    rows = []
    red = redactor_for(cfg)
    for d in root.iterdir():
        if not ATTEMPT.match(d.name) or d.is_symlink() or not d.is_dir():
            continue
        files = []
        for f in sorted(d.iterdir()):
            if ATTEMPT_FILE.match(f.name) and not f.is_symlink() and f.is_file():
                files.append({"name": f.name, "size": f.stat().st_size})
        reasons = ""
        if (d / "reasons.txt").is_file():
            try:
                reasons = red.text(read_file(d / "reasons.txt", 4000)[0]).strip().splitlines()[0][:300]
            except (OSError, IndexError):
                pass
        verdict = read_json(d / "verdict.json", None) if (d / "verdict.json").is_file() else None
        rows.append({"name": d.name, "mtime": int(d.stat().st_mtime), "files": files, "reason": reasons,
                     "verdict_pass": verdict.get("pass") if isinstance(verdict, dict) else None})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:150]


def attempt_file(name: str, attempt: str, fname: str) -> tuple[str, bool]:
    cfg = load_cfg(name)
    if not ATTEMPT.match(attempt) or not ATTEMPT_FILE.match(fname):
        raise LookupError(fname)
    root = cfg.state_dir / "attempts"
    d = root / attempt
    if root.is_symlink() or d.is_symlink() or not d.is_dir():
        raise LookupError(attempt)
    text, cut = read_file(d / fname)
    return redactor_for(cfg).text(text), cut


def _sid(s: str) -> str:
    try:
        return sessions.validate_id(s)
    except ValueError:
        raise LookupError(s)


# ---------------------------------------------------------------- actions (run the CLI, so its checks apply)
ACTIONS = {"stop": ["stop"], "start": ["start"], "ack": ["ack"], "ack-clear-model": ["ack", "--clear-model"]}


def hands_argv(*args: str) -> list[str]:
    exe = Path.home() / ".local" / "bin" / "hands"
    if exe.is_file() and os.access(exe, os.X_OK) and not os.environ.get("HANDS_DASH_INPROC_CLI"):
        return [str(exe), *args]
    return [sys.executable, "-m", "hands", *args]


def run_action(name: str, action: str) -> dict:
    cfg = load_cfg(name)
    if action not in ACTIONS:
        raise LookupError(action)
    if action == "start":
        if not (cfg.spec_dir / "steps.json").is_file():
            return {"rc": 1, "output": "no plan yet: the spec repo has no steps.json"}
        if Lock.held(cfg.state_dir / "runner.lock"):
            return {"rc": 1, "output": "runner is already running"}
        stop = cfg.state_dir / "STOP"
        if stop.exists() and not stop.is_symlink():
            stop.unlink()
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = str(cfgmod.RUNNER_HOME)
    try:
        p = subprocess.run(hands_argv(name, *ACTIONS[action]), capture_output=True, text=True, timeout=60, env=env,
                           stdin=subprocess.DEVNULL)
        return {"rc": p.returncode, "output": (p.stdout + p.stderr)[-4000:]}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "output": "timed out"}


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "hands-dash"
    timeout = 10                           # idle/slow sockets do not pin threads for long
    sys_version = ""
    protocol_version = "HTTP/1.1"

    _requests = 0                          # on this connection
    _started = 0.0

    def handle_one_request(self):
        """One request on this connection. The first request line must arrive within `timeout`; later ones
        (keep-alive) may take KEEPALIVE_IDLE, so the browser gives up on an idle connection before we do.
        Headers and body always use the short timeout. Socket errors are logged, never raised."""
        try:
            self.connection.settimeout(self.timeout if self._requests == 0 else KEEPALIVE_IDLE)
            self.raw_requestline = self.rfile.readline(65537)
            self.connection.settimeout(self.timeout)
            self._started = time.monotonic()
            if len(self.raw_requestline) > 65536:
                self.requestline = self.request_version = self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self._requests += 1
            if self.command not in ("GET", "HEAD", "POST"):
                self.send_error(501, f"Unsupported method ({self.command!r})")
                return
            getattr(self, f"do_{self.command}")()
            self.wfile.flush()
        except TimeoutError:
            if self._requests:                                   # idle keep-alive connection: normal
                log_line(f"idle {self.client_address[0]} closed after {self._requests} requests")
            else:
                log_line(f"ERR {self.client_address[0]} timed out before its first request")
            self.close_connection = True
        except (ConnectionError, OSError) as e:                  # reset, broken pipe, EBADF: the peer left
            log_line(f"ERR {self.client_address[0]} {type(e).__name__} on {self._req_label()}")
            self.close_connection = True

    def _req_label(self) -> str:
        """The request as it is logged: method and path only (the query may carry a login code or ticket)."""
        path = urllib.parse.urlsplit(getattr(self, "path", "") or "").path
        path = re.sub(r"[^\x21-\x7e]", "?", path)[:200]
        return f"{getattr(self, 'command', '') or '-'} {path or '-'}"

    def log_request(self, code="-", size="-"):
        ms = round((time.monotonic() - self._started) * 1000) if self._started else 0
        log_line(f"{self.client_address[0]} {self._req_label()} {code} {ms}ms")

    def log_error(self, fmt, *args):
        log_line(f"ERR {self.client_address[0]} {self._req_label()} {fmt % args}")

    def log_message(self, fmt, *args):     # everything else http.server would print (nothing today)
        log_line(fmt % args)

    # -- plumbing
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        else:
            self.send_header("Keep-Alive", f"timeout={max(1, int(KEEPALIVE_IDLE) - 5)}")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _text(self, code: int, text: str) -> None:
        self._send(code, text.encode(), "text/plain; charset=utf-8")

    def _host_ok(self) -> bool:
        return bool(LOCAL_HOST.match(self.headers.get("Host", "")))

    def _authed(self) -> bool:
        return bool(self._login_sid())

    def _login_sid(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and self.server.session_ok(v):
                return v
        return ""

    def _same_origin(self) -> bool:
        return self.headers.get("Origin", "") == f"http://{self.headers.get('Host', '')}"

    def _origin_ok(self) -> bool:
        return self._same_origin() and self.headers.get("X-Hands") == "1"

    # -- verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            if not self._host_ok():
                return self._text(403, "forbidden host")
            u = urllib.parse.urlsplit(self.path)
            path = u.path
            if path == "/login" and self.command != "GET":
                return self._text(405, "use GET")
            if method == "GET" and path == "/login":
                return self._login(urllib.parse.parse_qs(u.query).get("c", [""])[0])
            if not self._authed():
                if method == "GET" and path == "/":
                    return self._text(401, "Not logged in. On the VPS run: hands-dash url   and open that URL.")
                return self._json(401, {"error": "not logged in"})
            if method == "POST":
                if not self._origin_ok():
                    return self._json(403, {"error": "bad origin"})
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0:
                    return self._json(400, {"error": "bad length"})
                if n > (MAX_ENV_BODY if path.endswith("/env") else MAX_BODY):
                    return self._json(413, {"error": "too large"})
                body = self.rfile.read(n) if n else b""
                return self._post(path, body)
            return self._get(path, urllib.parse.parse_qs(u.query))
        except LookupError:
            return self._json(404, {"error": "not found"})
        except PermissionError as e:
            return self._json(403, {"error": str(e)[:500] or "forbidden"})
        except sessions.Conflict as e:
            return self._json(409, {"error": str(e)[:500]})
        except ValueError as e:
            return self._json(400, {"error": str(e)[:500]})
        except Exception as e:  # never leak a traceback page
            log_line(f"ERR {self.client_address[0]} {self._req_label()} {type(e).__name__}: {str(e)[:300]}")
            return self._json(500, {"error": f"{type(e).__name__}"})

    def _login(self, code: str) -> None:
        if not code or len(code) > 100 or not redeem_login_code(code):
            time.sleep(0.5)
            return self._text(403, "login code invalid, used or expired; run hands-dash url again")
        cookie = f"{COOKIE}={self.server.new_session()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"
        self._send(303, b"", "text/plain", {"Set-Cookie": cookie, "Location": "/"})

    def _get(self, path: str, q: dict) -> None:
        if path in STATIC_FILES:
            fname, ctype = STATIC_FILES[path]
            return self._send(200, (STATIC / fname).read_bytes(), ctype)
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "creating"]:
            from . import newproject
            try:
                newproject.validate_slug(parts[2])
            except newproject.CreateError:
                raise LookupError(parts[2])
            info = create_info(parts[2])
            if info is None:
                raise LookupError(parts[2])
            info["project_exists"] = parts[2] in project_names()
            return self._json(200, info)
        if len(parts) == 4 and parts[:2] == ["api", "p"] and parts[3] == "planfile":
            text, cut = plan_file(parts[2], (q.get("path") or [""])[0])
            return self._text(200, text + (f"\n[... truncated at {MAX_FILE} bytes]" if cut else ""))
        if parts == ["api", "usage"]:
            from . import usage
            u = usage.read()
            u["refreshing"] = Lock.held(config_home() / "usage-refresh.lock")
            return self._json(200, u)
        if parts[:2] == ["api", "projects"] and len(parts) == 2:
            return self._json(200, [summary(n) for n in project_names()])
        if parts == ["api", "sessions"]:
            return self._json(200, sessions.Store().listing())
        if parts == ["api", "sessions", "defaults"]:
            return self._json(200, {"cwd": str(sessions.default_cwd()), "home": str(sessions.HOME),
                                    "max_terminals": sessions.MAX_TERMINALS, "telegram_channel": sessions.TELEGRAM_CHANNEL})
        if len(parts) == 3 and parts[:2] == ["ws", "sessions"]:
            return self._terminal(_sid(parts[2]), (q.get("t") or [""])[0])
        if len(parts) >= 3 and parts[:2] == ["api", "p"]:
            name, rest = parts[2], parts[3:]
            if rest == ["env"]:
                return self._json(200, env_listing(name))
            if rest == []:
                return self._json(200, detail(name))
            if rest == ["log"]:
                return self._json(200, log_tail(name, int((q.get("lines") or ["300"])[0] or 300)))
            if rest == ["attempts"]:
                return self._json(200, attempts(name))
            if len(rest) == 3 and rest[0] == "attempts":
                text, cut = attempt_file(name, rest[1], rest[2])
                return self._text(200, text + (f"\n[... truncated at {MAX_FILE} bytes]" if cut else ""))
        raise LookupError(path)

    def _post(self, path: str, body: bytes = b"") -> None:
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
        if parts == ["api", "usage", "refresh"]:
            return self._json(200, usage_refresh())
        if parts == ["api", "new"]:
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                raise ValueError("bad json")
            if not isinstance(req, dict):
                raise ValueError("bad json")
            return self._json(200, start_create(str(req.get("slug", "")), str(req.get("description", "")),
                                                str(req.get("from_repo", "") or ""), str(req.get("branch", "") or ""),
                                                req.get("ruleset") is True))
        if len(parts) == 5 and parts[:2] == ["api", "p"] and parts[3] == "action" and parts[4] in ("plan", "approve"):
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                raise ValueError("bad json")
            fb = str(req.get("feedback", "")) if isinstance(req, dict) else ""
            hs = str(req.get("head_sha", "")) if isinstance(req, dict) else ""
            return self._json(200, plan_action(parts[2], parts[4], fb, hs))
        if len(parts) == 5 and parts[:2] == ["api", "p"] and parts[3] == "action":
            return self._json(200, run_action(parts[2], parts[4]))
        if len(parts) == 4 and parts[:2] == ["api", "p"] and parts[3] in ("hide", "unhide"):
            return self._json(200, set_hidden(parts[2], parts[3] == "hide"))
        if len(parts) == 4 and parts[:2] == ["api", "p"] and parts[3] == "env":
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                raise ValueError("bad json")
            return self._json(200, env_update(parts[2], req))
        if parts == ["api", "sessions", "new"]:
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                raise ValueError("bad json")
            cwd = req.get("cwd") if isinstance(req, dict) else None
            tg = req.get("telegram") if isinstance(req, dict) else None
            return self._json(200, sessions.start_new(cwd if isinstance(cwd, str) and cwd else None, tg))
        if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
            sid, verb = _sid(parts[2]), parts[3]
            if verb == "ticket":
                sessions.Store().get(sid)
                return self._json(200, {"ticket": self.server.mint_ticket(self._login_sid(), sid),
                                        "ttl": sessions.TICKET_TTL})
            if verb == "resume":
                try:
                    req = json.loads(body or b"{}")
                except ValueError:
                    raise ValueError("bad json")
                return self._json(200, sessions.resume(sid, req.get("telegram") if isinstance(req, dict) else None))
            if verb == "stop":
                return self._json(200, sessions.stop(sid))
            if verb == "delete":
                return self._json(200, sessions.delete(sid))
        raise LookupError(path)

    # -- terminal WebSocket: every check runs before the 101 (Host and login already did in _dispatch)
    def _terminal(self, sid: str, ticket: str) -> None:
        if not self._same_origin():
            return self._json(403, {"error": "bad origin"})
        err = ws.handshake_error(self.headers) if self.command == "GET" else "websocket required"
        if err:
            return self._json(400, {"error": err})
        if not ticket or not self.server.redeem_ticket(ticket, self._login_sid(), sid):
            return self._json(403, {"error": "bad or expired ticket"})
        rec = sessions.Store().get(sid)                                   # 404 if unknown
        if not sessions.BACKEND.alive(sid):
            return self._json(409, {"error": "the session is not running; resume it first"})
        if not self.server.acquire_terminal():
            return self._json(503, {"error": f"too many terminals open ({sessions.MAX_TERMINALS} max); close one first"})
        try:
            viewer = sessions.Viewer(sid, sessions.BACKEND.attach_argv(sid), sessions.tmux_env())
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", ws.accept_key(self.headers["Sec-WebSocket-Key"].strip()))
            self.end_headers()
            self.wfile.flush()
            viewer.run(ws.WebSocket(self.connection, self.rfile))
        finally:
            self.server.release_terminal()
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int):
        self.sessions: dict[str, float] = {}
        self.tickets: dict[str, tuple[str, str, float]] = {}      # ticket -> (login session, claude session, expiry)
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONN)
        self._terminals = 0
        super().__init__(("127.0.0.1", port), Handler)

    # -- terminal tickets: minted by a CSRF-checked POST, redeemed once by the WebSocket upgrade
    def mint_ticket(self, login_sid: str, session_id: str) -> str:
        tok = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self.tickets = {k: v for k, v in self.tickets.items() if v[2] > now}
            if len(self.tickets) >= 64:
                raise ValueError("too many unused terminal tickets; wait a moment")
            self.tickets[tok] = (login_sid, session_id, now + sessions.TICKET_TTL)
        return tok

    def redeem_ticket(self, tok: str, login_sid: str, session_id: str) -> bool:
        with self._lock:
            t = self.tickets.get(tok)
            if t is None:
                return False
            if t[2] <= time.time():
                del self.tickets[tok]
                return False
            if not (hmac.compare_digest(t[0], login_sid) and t[1] == session_id):
                return False
            del self.tickets[tok]
            return True

    # -- terminal slots (MAX_TERMINALS open WebSocket terminals, all sessions together)
    def acquire_terminal(self) -> bool:
        with self._lock:
            if self._terminals >= sessions.MAX_TERMINALS:
                return False
            self._terminals += 1
            return True

    def release_terminal(self) -> None:
        with self._lock:
            self._terminals = max(0, self._terminals - 1)

    def terminals_open(self) -> int:
        with self._lock:
            return self._terminals

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            self.sessions[sid] = now + SESSION_TTL
        return sid

    def session_ok(self, sid: str) -> bool:
        with self._lock:
            exp = self.sessions.get(sid)
        return exp is not None and exp > time.time()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)       # too many open connections: drop
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def lock_path() -> Path:
    return config_home() / "dashboard.lock"


def serve(port: int) -> int:
    config_home().mkdir(parents=True, exist_ok=True)
    with Lock(lock_path()):
        srv = Server(port)
        print(f"hands dashboard on http://127.0.0.1:{port}  (login: hands-dash url)", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
    return 0


def _tmux_has() -> bool:
    return subprocess.run(["tmux", "has-session", "-t", f"={SESSION}"], capture_output=True).returncode == 0


def start(port: int) -> int:
    if Lock.held(lock_path()) or _tmux_has():
        print(f"already running (tmux attach -t {SESSION})")
        return 0
    exe = Path.home() / ".local" / "bin" / "hands-dash"
    cmd = (shlex.quote(str(exe)) if exe.is_file() else
           f"env PYTHONPATH={shlex.quote(str(cfgmod.RUNNER_HOME))} {shlex.quote(sys.executable)} -m hands.dashboard")
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, f"{cmd} run --port {int(port)}"], check=True, env=env)
    print(f"started on 127.0.0.1:{port}. Tunnel from your Mac: ssh -N -L {port}:127.0.0.1:{port} builder")
    return 0


def ensure_running(port: int = DEFAULT_PORT) -> None:
    if not Lock.held(lock_path()) and not _tmux_has():
        start(port)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    ap = argparse.ArgumentParser(prog="hands-dash", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "start", "stop", "url"])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    if a.command == "run":
        return serve(a.port)
    if a.command == "start":
        return start(a.port)
    if a.command == "stop":
        subprocess.run(["tmux", "kill-session", "-t", f"={SESSION}"], capture_output=True)
        print("stopped")
        return 0
    if not Lock.held(lock_path()):
        print("dashboard is not running; start it: hands-dash start")
        return 1
    print(f"http://127.0.0.1:{a.port}/login?c={mint_login_code()}   (single use, valid {LOGIN_TTL // 60} min)")
    print(f"tunnel from your Mac first:  ssh -N -L {a.port}:127.0.0.1:{a.port} builder")
    return 0


if __name__ == "__main__":
    sys.exit(main())
