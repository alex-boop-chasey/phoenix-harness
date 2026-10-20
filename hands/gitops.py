"""Git on the HOST, only ever inside runner-owned repos (spec clone, app clone).
The runner never runs git inside an agent-written workspace: a planted .git/config
(core.fsmonitor, hooks, filters) would execute on the host."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Git on the host must use only each repo's own config: env overrides (GIT_SSH_COMMAND, GIT_CONFIG_*) and
# global/system config (url.insteadOf, credential helpers) could swap a pinned deploy key for the user's identity.
_STRIP = ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG",
          "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
          "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PROXY_COMMAND")
ENV = {k: v for k, v in os.environ.items()
       if k not in _STRIP and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
ENV.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})


class GitError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True, input: bytes | None = None, timeout: int = 600) -> str:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=ENV, input=input, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} in {repo}: timed out after {timeout}s") from e
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} in {repo}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode(errors="replace")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def is_clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain", "--untracked-files=all").strip() == ""


def hard_reset(repo: Path, rev: str) -> None:
    git(repo, "reset", "-q", "--hard", rev)
    git(repo, "clean", "-qfdx")


def ahead_behind(app_dir: Path, branch: str) -> str:
    """Report-only (never pushes): used by `check`."""
    git(app_dir, "fetch", "-q", "origin", branch)
    out = git(app_dir, "rev-list", "--left-right", "--count", f"HEAD...FETCH_HEAD").split()
    return f"ahead {out[0]}, behind {out[1]}"


def refresh_spec(spec_dir: Path, branch: str) -> str:
    """Fetch the spec repo and pin the working tree to origin/<branch>. Returns commit sha."""
    git(spec_dir, "fetch", "-q", "origin", branch)
    git(spec_dir, "checkout", "-q", "--detach", "FETCH_HEAD")
    git(spec_dir, "reset", "-q", "--hard", "FETCH_HEAD")
    git(spec_dir, "clean", "-qfdx")
    return head(spec_dir)


def refresh_app(app_dir: Path, branch: str) -> str:
    """Pin the runner's app clone to origin/<branch> (used before planning an imported repo, when no build
    has run yet). Refuses when the clone has local changes (someone may be working there in a Session
    manager session): nothing is ever discarded. Returns commit sha."""
    dirty = git(app_dir, "status", "--porcelain", "--untracked-files=all").strip()
    if dirty:
        raise GitError(f"{app_dir} has local changes; commit, stash or remove them first: "
                       + " | ".join(dirty.splitlines()[:5]))
    git(app_dir, "fetch", "-q", "origin", branch)
    git(app_dir, "checkout", "-q", "-B", branch, "FETCH_HEAD")
    git(app_dir, "reset", "-q", "--hard", "FETCH_HEAD")
    git(app_dir, "clean", "-qfdx")
    return head(app_dir)


def sync_app(app_dir: Path, branch: str, push: bool) -> tuple[str, str]:
    """Make the runner's app clone consistent with origin. Returns (state, detail):
    ok | pushed (local commits were unpushed and are now pushed) | diverged | push_failed."""
    if not is_clean(app_dir):
        git(app_dir, "reset", "-q", "--hard", "HEAD")
        git(app_dir, "clean", "-qfdx")
    cur = git(app_dir, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if cur != branch:
        git(app_dir, "checkout", "-q", branch)
    if not push:
        return "ok", "push disabled"
    git(app_dir, "fetch", "-q", "origin", branch)
    local, remote = head(app_dir), git(app_dir, "rev-parse", "FETCH_HEAD").strip()
    if local == remote:
        return "ok", local
    base = git(app_dir, "merge-base", local, remote, check=False).strip()
    if base == local:                      # remote ahead (human pushed): fast-forward
        git(app_dir, "merge", "-q", "--ff-only", remote)
        return "ok", f"fast-forwarded to {remote[:9]}"
    if base == remote:                     # local ahead: push what the runner already accepted
        ok, msg = push_branch(app_dir, branch)
        return ("pushed", msg) if ok else ("push_failed", msg)
    return "diverged", f"local {local[:9]} and origin {remote[:9]} diverged"


def push_branch(app_dir: Path, branch: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(["git", "-C", str(app_dir), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"],
                           capture_output=True, env=ENV, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "push timed out"
    return p.returncode == 0, p.stderr.decode(errors="replace").strip()


def remote_sha(repo: Path, ref: str) -> str:
    """sha of `ref` (refs/heads/x or refs/tags/x) on origin, "" when it does not exist. Raises GitError offline."""
    out = git(repo, "ls-remote", "--exit-code", "origin", ref, check=False)
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    # distinguish "no such ref" from "cannot reach origin"
    git(repo, "ls-remote", "origin", "HEAD")
    return ""


def push_ref(repo: Path, src: str, dst: str) -> None:
    """Push one ref without force; raises GitError when refused."""
    git(repo, "push", "-q", "origin", f"{src}:{dst}", timeout=300)


def diff(repo: Path, *args: str) -> str:
    """A diff that agent-written .gitattributes / textconv / external diff drivers cannot hide or alter."""
    return git(repo, "-c", "core.attributesFile=/dev/null", "-c", "diff.external=", "diff", "--text",
               "--no-textconv", "--no-ext-diff", *args, check=False)


def log_oneline(repo: Path, n: int = 15) -> str:
    return git(repo, "log", f"-{n}", "--format=%h %s", check=False)
