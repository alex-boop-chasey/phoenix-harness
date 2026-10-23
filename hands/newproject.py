"""Create (and destroy) a project from nothing: GitHub repos, protection, deploy keys, clones, config.

  hands <slug> create            (description on stdin)   -- also started by the dashboard's New project form
  hands <slug> destroy --confirm <slug>                   -- CLI only: deletes what create recorded

For slug S in org O (settings: ~/.config/hands/new-project.toml) create makes:
  O/S-specs  private, CODEOWNERS "* @<codeowner>", ruleset protect-main: no deletion, no force-push,
             PR + 1 approval + code-owner review + dismiss stale; org admins may merge their own PRs
  O/S-app    private, .gitignore, ruleset protect-main: no deletion, no force-push (the runner pushes)
  ~/.config/hands/secrets/S/keys/deploy_S-{specs,app}  (specs key read-only, app key write), never in ~/.ssh
  ~/.config/hands/secrets/S/project.env (0600)           dev/test secrets for the build containers
           (all project secrets live under ~/.config/hands so one deny rule covers them; dirs 0700);
           env.json next to it holds the dashboard's metadata (hands/projectenv.py); variables set
           while create runs wait in ~/.config/hands/creating/S.pending/ and are moved in at the end
  ~/projects/S/known_hosts                 GitHub host keys from the authenticated /meta API (no TOFU)
  ~/projects/S/{specs,app}                 clones whose core.sshCommand pins that key: ~/.ssh/config,
                                           ssh-agent and personal keys are never used or changed
  ~/projects/S/{state/,description.txt,created.json}
  ~/.config/hands/projects/S.toml          (managed = true)

created.json records exactly what this run made (repo ids); destroy deletes only those, and only if the
repo on GitHub still has the recorded id. The GitHub token (~/.config/hands/github.env, GH_TOKEN=...) is
read only by this process: never by the dashboard server, never by a container, never logged.

  hands <slug> create --from-repo <owner>/<repo> [--branch B] [--ruleset]      (0.5: import an existing repo)
Same as above, but the app repo already exists: no app repo is created, nothing is written to it; Phoenix
only adds a write deploy key (and the protect-main ruleset only with --ruleset) and clones it. created.json
records it under "imported" (repo, id, deploy_key_id, branch), never under "repos": destroy removes the
deploy key it added, the specs repo and the local folders/secrets, and leaves the imported repo untouched.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

from . import config as cfgmod
from . import gitops

SLUG = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
RESERVED = {"all", "dash", "dashboard"}
GH_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
DEFAULT_API = "https://api.github.com"
GITIGNORE = "node_modules/\n/acceptance/\n.env\n.env.*\n!.env.example\ndist/\nbuild/\n.next/\ncoverage/\n" \
            "playwright-report/\ntest-results/\n*.log\n.DS_Store\n"


class CreateError(Exception):
    pass


# ---------------------------------------------------------------- paths / test hooks
def home() -> Path:
    if os.environ.get("HANDS_HOME") and os.environ.get("HANDS_ALLOW_TEST_API") == "1":
        return Path(os.environ["HANDS_HOME"])
    return Path.home()


def projects_root() -> Path:
    return home() / "projects"


def secrets_root(slug: str) -> Path:
    """Per-project secrets (deploy keys, project.env). Kept under ~/.config/hands, never in the project folder."""
    return cfgmod.CONFIG_HOME / "secrets" / slug


def creating_dir() -> Path:
    return cfgmod.CONFIG_HOME / "creating"


def testing() -> bool:
    return os.environ.get("HANDS_ALLOW_TEST_API") == "1"


def api_base() -> str:
    v = os.environ.get("HANDS_GH_API")
    return v if (v and testing()) else DEFAULT_API


def settle(seconds: float) -> None:
    time.sleep(seconds * float(os.environ.get("HANDS_GH_SETTLE", "1")))


def remote_url(org: str, repo: str) -> str:
    override = os.environ.get("HANDS_TEST_REMOTE_ROOT")        # tests: local bare repos
    if override and testing():
        return str(Path(override) / f"{repo}.git")
    return f"git@github.com:{org}/{repo}.git"


def ssh_command(key: Path, known_hosts: Path) -> str:
    q = shlex.quote
    return (f"ssh -F /dev/null -i {q(str(key))} -o IdentitiesOnly=yes -o IdentityAgent=none "
            f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={q(str(known_hosts))} -o BatchMode=yes")


# ---------------------------------------------------------------- settings + token
def settings() -> dict:
    p = cfgmod.CONFIG_HOME / "new-project.toml"
    if not p.is_file():
        raise CreateError(f"{p} missing (the installer writes it; keys: org, codeowner, image)")
    s = tomllib.loads(p.read_text())
    for k in ("org", "codeowner", "image"):
        if not isinstance(s.get(k), str) or not s[k]:
            raise CreateError(f"{p}: '{k}' required")
    for k in ("org", "codeowner"):
        if not GH_NAME.match(s[k]):
            raise CreateError(f"{p}: {k} {s[k]!r} is not a valid GitHub name")
    if s["image"].endswith(":latest") or ":" not in s["image"]:
        raise CreateError(f"{p}: image must be a pinned tag")
    return s


def token() -> str:
    p = cfgmod.CONFIG_HOME / "github.env"
    try:
        st = os.stat(p, follow_symlinks=False)
    except OSError:
        raise CreateError(f"{p} missing (GH_TOKEN=...)")
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise CreateError(f"{p} must be a regular file you own with mode 600")
    for line in p.read_text().splitlines():
        if line.startswith("GH_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise CreateError(f"{p}: no GH_TOKEN line")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # never re-send the token anywhere else
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class GitHub:
    def __init__(self, tok: str):
        self.tok = tok
        self.base = api_base()
        if not self.base.startswith("https://") and not testing():
            raise CreateError("GitHub API must be https")

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 201, 204),
             retry: tuple = ()) -> tuple[int, dict]:
        for n in range(1, 8):
            code, data = self._once(method, path, body)
            if code in retry and n < 7:
                settle(3 * n)             # just-created repos can 404/409 for a few seconds
                continue
            break
        if code not in ok:
            msg = data.get("message", "") if isinstance(data, dict) else ""
            errs = data.get("errors", "") if isinstance(data, dict) else ""
            raise CreateError(f"GitHub {method} {path}: HTTP {code} {msg} {errs}".strip())
        return code, data

    def _once(self, method, path, body):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {self.tok}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(req, timeout=60) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CreateError(f"GitHub {method} {path}: {type(e).__name__}: {e}")
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return code, data if isinstance(data, dict) else {"items": data}

    def repo_id(self, org: str, repo: str) -> int | None:
        code, data = self.call("GET", f"/repos/{org}/{repo}", ok=(200, 404))
        return data.get("id") if code == 200 else None


def keygen(path: Path, comment: str) -> str:
    if path.exists() or path.is_symlink():
        raise CreateError(f"{path} already exists")
    exe = os.environ.get("HANDS_SSH_KEYGEN") if testing() else None
    subprocess.run([exe or "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                   check=True, capture_output=True)
    os.chmod(path, 0o600)
    return path.with_suffix(".pub").read_text().strip()


def ruleset(spec_repo: bool) -> dict:
    rules: list[dict] = [{"type": "deletion"}, {"type": "non_fast_forward"}]
    body = {"name": "protect-main", "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}, "rules": rules,
            "bypass_actors": []}
    if spec_repo:
        rules.append({"type": "pull_request", "parameters": {
            "required_approving_review_count": 1, "require_code_owner_review": True,
            "dismiss_stale_reviews_on_push": True, "require_last_push_approval": False,
            "required_review_thread_resolution": False}})
        body["bypass_actors"] = [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "pull_request"}]
    return body


# ---------------------------------------------------------------- status
def status_path(slug: str) -> Path:
    return creating_dir() / f"{slug}.json"


def lock_path(slug: str) -> Path:
    return creating_dir() / f"{slug}.lock"


def write_status(slug: str, state: str, error: str = "") -> None:
    creating_dir().mkdir(parents=True, exist_ok=True)
    p = status_path(slug)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"slug": slug, "state": state, "error": error, "at": int(time.time())}))
    os.replace(tmp, p)


def say(msg: str) -> None:
    print(msg, flush=True)


def validate_slug(slug: str) -> None:
    if not SLUG.match(slug) or "--" in slug or slug in RESERVED:
        raise CreateError("slug: 3-32 chars, lowercase letters, digits and single hyphens, starting with a letter "
                          f"(reserved: {', '.join(sorted(RESERVED))})")


def parse_repo(ref: str) -> tuple[str, str]:
    """'owner/repo' of an existing GitHub repo -> (owner, repo). Raises CreateError."""
    ref = (ref or "").strip()
    owner, sep, repo = ref.partition("/")
    if not sep or not GH_NAME.match(owner) or not REPO_NAME.match(repo) or repo in (".", "..") \
            or repo.lower().endswith(".git"):
        raise CreateError("existing repo: use owner/repo (GitHub owner name, then the repository name)")
    return owner, repo


def validate_branch(name: str) -> None:
    if not BRANCH_NAME.match(name or "") or ".." in name or "//" in name or name.endswith(("/", ".lock")) \
            or "@{" in name or name.endswith("."):
        raise CreateError("branch: letters, digits, '.', '_', '-' and '/' (max 120 chars), like git allows")


def toml_str(v: str) -> str:
    return json.dumps(v)        # JSON string escapes are valid TOML basic strings


def gh_description(text: str, prefix: str) -> str:
    clean = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return (prefix + clean)[:300]


# ---------------------------------------------------------------- create
class Ctx:
    """What this run has created so far (persisted to created.json as it goes)."""

    def __init__(self, root: Path, org: str):
        self.root, self.org = root, org
        self.toml_written = False
        self.data = {"org": org, "repos": {}, "created_at": int(time.time())}

    def record_repo(self, name: str, rid: int) -> None:
        self.data["repos"][name] = rid
        self.save()

    def save(self) -> None:
        p = self.root / "created.json"
        tmp = p.with_name(".created.json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        os.replace(tmp, p)


def inspect_repo(gh: "GitHub", owner: str, repo: str, branch: str) -> dict:
    """The existing repo to import: must be reachable with the token, not archived, with a default branch and
    admin access (deploy keys need it). Returns {"repo": "owner/name", "id", "branch"}."""
    full = f"{owner}/{repo}"
    code, data = gh.call("GET", f"/repos/{owner}/{repo}", ok=(200, 404))
    if code == 404:
        raise CreateError(f"repo {full} not found, or the GitHub token cannot access it")
    if data.get("archived"):
        raise CreateError(f"repo {full} is archived (unarchive it on GitHub first)")
    if data.get("disabled"):
        raise CreateError(f"repo {full} is disabled on GitHub")
    rid = data.get("id")
    if not isinstance(rid, int):
        raise CreateError(f"GitHub returned no id for {full}")
    default = data.get("default_branch")
    if not isinstance(default, str) or not default:
        raise CreateError(f"repo {full} has no default branch (empty repo?); push a first commit")
    perms = data.get("permissions")
    if isinstance(perms, dict) and not perms.get("admin"):
        raise CreateError(f"the GitHub token needs admin access on {full} to add a deploy key")
    if branch:
        validate_branch(branch)
        if branch != default:
            code, _ = gh.call("GET", f"/repos/{owner}/{repo}/branches/{branch}", ok=(200, 404))
            if code == 404:
                raise CreateError(f"branch {branch!r} does not exist in {full}")
    else:
        validate_branch(default)
    return {"repo": full, "id": rid, "branch": branch or default}


def preflight(slug: str, description: str, from_repo: str = "", branch: str = "") -> tuple[dict, "GitHub", list[str], dict | None]:
    """All checks that need no side effects. Raises CreateError."""
    validate_slug(slug)
    if not description.strip():
        raise CreateError("description required")
    imp_ref = parse_repo(from_repo) if from_repo else None
    if branch and not imp_ref:
        raise CreateError("--branch is only used with --from-repo")
    s = settings()
    cfg_path = cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml"
    root = projects_root() / slug
    if cfg_path.exists():
        raise CreateError(f"project {slug} already exists ({cfg_path})")
    if root.exists() or root.is_symlink():
        raise CreateError(f"{root} already exists")
    sec = secrets_root(slug)
    if sec.exists() or sec.is_symlink():
        raise CreateError(f"{sec} already exists")
    gh = GitHub(token())
    org = s["org"]
    imp = inspect_repo(gh, imp_ref[0], imp_ref[1], branch) if imp_ref else None
    for r in ([f"{slug}-specs"] if imp else [f"{slug}-specs", f"{slug}-app"]):
        if gh.repo_id(org, r) is not None:
            raise CreateError(f"repo {org}/{r} already exists")
    if imp and imp["repo"].lower() == f"{org}/{slug}-specs".lower():
        raise CreateError("the existing repo cannot be the new specs repo")
    _, o = gh.call("GET", f"/orgs/{org}")
    plan = (o.get("plan") or {}).get("name", "")
    if plan == "free":
        raise CreateError(f"org {org} is on GitHub Free: rulesets/code-owner review on private repos need Team")
    _, meta = gh.call("GET", "/meta")
    host_keys = [k for k in meta.get("ssh_keys", []) if isinstance(k, str) and re.match(r"^ssh-[\w-]+ [A-Za-z0-9+/=]+$", k)]
    if not host_keys and not testing():
        raise CreateError("could not read GitHub's SSH host keys from /meta")
    return s, gh, host_keys, imp


def create(slug: str, description: str, ctx_holder: list, from_repo: str = "", branch: str = "",
           ruleset_on_import: bool = False) -> int:
    description = description.strip()
    s, gh, host_keys, imp = preflight(slug, description, from_repo, branch)
    org, owner = s["org"], s["codeowner"]
    cfg_path = cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml"
    root = projects_root() / slug
    sec = secrets_root(slug)
    specs = f"{slug}-specs"
    if imp:
        app_owner, app = imp["repo"].split("/", 1)
        repo_app_cfg = imp["repo"]              # owner/name: the app repo may live outside the specs org
        say(f"[..] creating project {slug} in {org} from existing repo {imp['repo']} (branch {imp['branch']})")
    else:
        app_owner, app = org, f"{slug}-app"
        repo_app_cfg = app
        say(f"[..] creating project {slug} in {org}")

    ctx = Ctx(root, org)
    ctx_holder.append(ctx)
    if imp:
        ctx.data["imported"] = {"repo": imp["repo"], "id": imp["id"], "branch": imp["branch"]}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(
            f"# created by hands {time.strftime('%Y-%m-%d %H:%M')}\nmanaged = true\n"
            + ("imported = true\n" if imp else "")
            + f"github_org = {toml_str(org)}\nrepo_specs = {toml_str(specs)}\nrepo_app = {toml_str(repo_app_cfg)}\n"
            f"spec_dir = {toml_str(str(root / 'specs'))}\napp_dir = {toml_str(str(root / 'app'))}\n"
            f"state_dir = {toml_str(str(root / 'state'))}\nproject_env = {toml_str(str(sec / 'project.env'))}\n"
            f"image = {toml_str(s['image'])}\n"
            + "".join(f"{k} = {toml_str(s[k])}\n" for k in ("coder_model", "eval_model", "fallback_model")
                      if isinstance(s.get(k), str) and s[k])
            + (f"max_sessions = {int(s['max_sessions'])}\n" if s.get("max_sessions") else ""))
    ctx.toml_written = True             # from here on destroy can clean up (toml first, then the folder)
    say(f"[ok] config {cfg_path}")
    root.mkdir(parents=True)
    ctx.save()
    (root / "description.txt").write_text(description + "\n")
    sec.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(sec.parent, 0o700)
    sec.mkdir(mode=0o700)
    keys = sec / "keys"
    keys.mkdir(mode=0o700)
    kh = root / "known_hosts"
    kh.write_text("".join(f"github.com {k}\n" for k in host_keys))

    branches = {}
    to_create = [(specs, True)] if imp else [(specs, True), (app, False)]
    for repo, is_spec in to_create:
        _, data = gh.call("POST", f"/orgs/{org}/repos", {
            "name": repo, "private": True, "auto_init": True, "has_wiki": False, "has_projects": False,
            "has_issues": False, "delete_branch_on_merge": True,
            "description": gh_description(description, "Spec: " if is_spec else "App: ")})
        if not isinstance(data.get("id"), int):
            raise CreateError(f"GitHub returned no id for {repo}")
        ctx.record_repo(repo, data["id"])
        branches[repo] = data.get("default_branch") or "main"
        say(f"[ok] repo {org}/{repo} (default branch {branches[repo]})")
    if imp:
        branches[app] = imp["branch"]
        if branches[specs] != "main":
            raise CreateError(f"specs repo default branch is {branches[specs]!r}; expected main")
    elif branches[specs] != branches[app]:
        raise CreateError(f"default branches differ: {branches}")
    settle(2)

    def put(repo: str, path: str, text: str, msg: str) -> None:
        gh.call("PUT", f"/repos/{org}/{repo}/contents/{path}",
                {"message": msg, "content": b64encode(text.encode()).decode()}, retry=(404, 409))
        say(f"[ok] {repo}: {path}")

    put(specs, ".github/CODEOWNERS", f"* @{owner}\n", "codeowners")
    if not imp:
        put(app, ".gitignore", GITIGNORE, "gitignore")
    protect = [(org, specs, True)]
    if not imp or ruleset_on_import:
        protect.append((app_owner, app, False))
    for r_owner, repo, is_spec in protect:
        gh.call("POST", f"/repos/{r_owner}/{repo}/rulesets", ruleset(is_spec), retry=(404, 409))
        say(f"[ok] {r_owner}/{repo}: ruleset protect-main")

    key_for = {}
    for r_owner, repo, ro in ((org, specs, True), (app_owner, app, False)):
        key_for[repo] = keys / f"deploy_{repo}"
        pub = keygen(key_for[repo], f"hands {repo}")
        _, kdata = gh.call("POST", f"/repos/{r_owner}/{repo}/keys",
                           {"title": f"hands-runner {slug}", "key": pub, "read_only": ro}, retry=(404, 409))
        if imp and not ro:
            if not isinstance(kdata.get("id"), int):
                raise CreateError(f"GitHub returned no id for the deploy key on {imp['repo']}")
            ctx.data["imported"]["deploy_key_id"] = kdata["id"]
            ctx.save()
        say(f"[ok] {r_owner}/{repo}: deploy key ({'read-only' if ro else 'write'})")

    for r_owner, repo, sub in ((org, specs, "specs"), (app_owner, app, "app")):
        sshc = ssh_command(key_for[repo], kh)
        extra = ["-b", branches[app]] if (imp and sub == "app") else []
        for n in range(1, 4):
            try:
                gitops.git(root, "clone", "-q", "-c", f"core.sshCommand={sshc}", *extra, remote_url(r_owner, repo), sub,
                           timeout=1800 if imp else 300)
                break
            except gitops.GitError as e:
                if n == 3:
                    raise CreateError(f"clone {repo} failed: {e}")
                say(f"[..] clone {repo} failed, retrying ({e})")
                settle(10 * n)
        if not os.environ.get("HANDS_TEST_REMOTE_ROOT"):
            url = gitops.git(root / sub, "remote", "get-url", "--push", "origin").strip()
            if url != f"git@github.com:{r_owner}/{repo}.git":
                raise CreateError(f"{sub} push URL resolves to {url!r}, expected ssh to github.com")
        say(f"[ok] cloned {r_owner}/{repo} -> {root / sub} (pinned to its deploy key)")
    (root / "state").mkdir()
    fd = os.open(sec / "project.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"# dev/test secrets for this project (KEY=value, no quotes). Seen by coder, acceptance, evaluator.\n")
    os.close(fd)
    if branches[app] != "main":
        with open(cfg_path, "a") as f:
            f.write(f"branch = {toml_str(branches[app])}\n")

    if not os.environ.get("HANDS_TEST_REMOTE_ROOT"):
        p = subprocess.run(["git", "-C", str(root / "specs"), "push", "--dry-run", "origin", "HEAD:refs/heads/hands-probe"],
                           capture_output=True, text=True, timeout=120, env=gitops.ENV)
        if p.returncode == 0 or not re.search(r"read[ -]?only", p.stderr, re.I):
            raise CreateError(f"could not confirm the specs deploy key is read-only (rc={p.returncode}): {p.stderr.strip()[:300]}")
        say("[ok] specs deploy key is read-only (push refused by GitHub)")
    say(f"DONE: project {slug} created. Next: plan it (the spec repo needs steps.json); "
        f"test keys go in {sec / 'project.env'}; extra domains in the spec repo's allowed-domains.")
    return 0


def run_create(slug: str, description: str, from_repo: str = "", branch: str = "", ruleset: bool = False) -> int:
    from .state import Lock
    try:
        validate_slug(slug)
    except CreateError as e:
        say(f"FAILED: {e}")
        return 2
    creating_dir().mkdir(parents=True, exist_ok=True)
    try:
        lk = Lock(lock_path(slug)).__enter__()
    except SystemExit:
        say(f"FAILED: {slug} is already being created")
        return 1
    try:
        exists = (cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml").exists()
        if not exists:
            write_status(slug, "running")
        holder: list[Ctx] = []
        from . import projectenv
        try:
            rc = create(slug, description, holder, from_repo, branch, ruleset)
            try:
                n = projectenv.adopt_pending(slug, secrets_root(slug))      # variables set while creating
                if n:
                    say(f"[ok] {n} environment variable(s) saved to {secrets_root(slug) / 'project.env'}")
            except Exception as e:
                say(f"WARN: pending environment variables not applied ({type(e).__name__}: {e}); "
                    f"they are still in {projectenv.pending_dir(slug)}")
            write_status(slug, "done")
            return rc
        except Exception as e:
            msg = str(e) if isinstance(e, CreateError) else f"{type(e).__name__}: {e}"
            say(f"FAILED: {msg}")
            try:
                projectenv.discard_pending(slug)
            except OSError:
                pass
            ctx = holder[0] if holder else None
            if ctx and ctx.toml_written:
                say(f"Partially created. Remove what this run made with: hands {slug} destroy --confirm {slug}")
            if not exists:                      # never touch the status of a project that already existed
                write_status(slug, "failed", msg)
            return 1
    finally:
        lk.__exit__(None, None, None)


# ---------------------------------------------------------------- destroy
def destroy(slug: str, confirm: str, keep_log: bool = False) -> int:
    """keep_log (hands.reset): leave the create status file and the create log in place, because the reset
    that called this writes its own progress into them and the dashboard reads them while it runs."""
    from . import docker
    from .state import Lock
    validate_slug(slug)
    if confirm != slug:
        raise CreateError(f"refusing: pass --confirm {slug}")
    cfg_path = cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml"
    root = projects_root() / slug
    if not cfg_path.is_file():
        for p in (status_path(slug), creating_dir() / f"{slug}.log"):
            if p.exists():
                p.unlink()
        raise CreateError(f"{cfg_path} not found: nothing hands created to destroy (GitHub untouched)")
    raw = tomllib.loads(cfg_path.read_text())
    if not raw.get("managed"):
        raise CreateError(f"{cfg_path} was not created by hands (managed = true missing); clean it up by hand")
    if Lock.held(lock_path(slug)):
        raise CreateError(f"{slug} is being created right now")
    state_dir = Path(raw.get("state_dir", root / "state"))
    if Lock.held(state_dir / "runner.lock"):
        raise CreateError(f"runner is running; hands {slug} stop first")
    try:
        rec = json.loads((root / "created.json").read_text())
    except (OSError, ValueError):
        rec = None
    if rec is None:
        say("[warn] no created.json: GitHub repos are left alone (delete them by hand if needed)")
    else:
        repos = rec.get("repos") or {}
        imported = rec.get("imported") if isinstance(rec.get("imported"), dict) else None
        imp_repo = str(imported.get("repo", "")) if imported else ""
        gh = GitHub(token()) if (repos or imported) else None
        if repos:
            org = rec.get("org")
            for repo, rid in repos.items():
                if repo not in (f"{slug}-specs", f"{slug}-app") or not isinstance(rid, int) or not org \
                        or (imp_repo and f"{org}/{repo}".lower() == imp_repo.lower()):
                    say(f"[warn] skipping unexpected record {repo}")
                    continue
                cur = gh.repo_id(org, repo)
                if cur is None:
                    say(f"[ok] {org}/{repo}: already gone")
                elif cur != rid:
                    say(f"[warn] {org}/{repo} is a different repo (id {cur}, created {rid}); NOT deleted")
                else:
                    gh.call("DELETE", f"/repos/{org}/{repo}", ok=(204, 404))
                    say(f"[ok] {org}/{repo}: deleted")
        if imported:
            kid = imported.get("deploy_key_id")
            try:
                owner, name = parse_repo(imp_repo)
            except CreateError:
                owner = name = ""
            if owner and name not in (f"{slug}-specs", f"{slug}-app") and isinstance(kid, int) and kid > 0:
                gh.call("DELETE", f"/repos/{owner}/{name}/keys/{kid}", ok=(204, 404))
                say(f"[ok] {imp_repo}: removed the deploy key hands added (id {kid}); the repo itself is untouched")
            elif owner and name not in (f"{slug}-specs", f"{slug}-app"):
                say(f"[ok] {imp_repo}: imported repo left untouched (no deploy key recorded)")
            else:
                say(f"[warn] skipping unexpected imported record {imp_repo!r}")
    try:
        cfg = cfgmod.load(slug)
        docker.cleanup_orphans(cfg)
        docker.cleanup_volumes(cfg)
        docker.volume_rm(cfg, cfg.pnpm_volume)
    except Exception as e:
        say(f"[warn] docker cleanup: {e}")
    subprocess.run(["tmux", "kill-session", "-t", f"=hands-{slug}"], capture_output=True)
    if root.exists() and not root.is_symlink():
        from .workspace import rmtree
        rmtree(root)
        say(f"[ok] removed {root}")
    sec = secrets_root(slug)
    if sec.exists() and not sec.is_symlink():
        from .workspace import rmtree
        rmtree(sec)
        say(f"[ok] removed {sec} (deploy keys and project.env)")
    gone = (cfg_path,) if keep_log else (cfg_path, status_path(slug), creating_dir() / f"{slug}.log")
    for p in gone:
        if p.exists():
            p.unlink()
    say(f"DONE: {slug} destroyed")
    return 0


def main_create(slug: str, from_repo: str = "", branch: str = "", ruleset: bool = False) -> int:
    desc = sys.stdin.read() if not sys.stdin.isatty() else input("One-line description: ")
    return run_create(slug, desc, from_repo, branch, ruleset)
