"""Per-attempt workspaces, safe export of agent output, and the path allowlist."""
from __future__ import annotations

import os
import posixpath
import shutil
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .spec import path_allowed

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILES = 20000

# Always rejected, whatever allowed_paths says. The agent may never write instructions for itself
# or anyone else, change CI, or touch the spec mirror.
DENY_SEGMENTS = {".claude", ".github", ".git", ".husky", ".cursor", ".vscode", ".devcontainer"}
DENY_BASENAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md", ".mcp.json", ".cursorrules",
                  ".gitmodules", ".gitattributes", "claude.json", ".clauderc"}
DENY_TOP = {"docs", "acceptance", "specs", "spec", ".harness"}
# all comparisons are case-insensitive (macOS/Windows clones fold case)
DENY_SEGMENTS_CI = {x.lower() for x in DENY_SEGMENTS}
DENY_BASENAMES_CI = {x.lower() for x in DENY_BASENAMES}
DENY_TOP_CI = {x.lower() for x in DENY_TOP}


def _gitlike(seg: str) -> bool:
    s = seg.lower().rstrip(". ")
    return s in (".git", "git~1") or s.startswith(".git::")
DENY_EXT = {".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc"}
# Environment files never enter the repo (values come from /run/hands/project.env at run time); only the
# names-only templates may. Case-insensitive, at any depth.
ENV_EXAMPLES = {".env.example", ".env.sample"}


def is_env_file(basename: str) -> bool:
    b = basename.lower()
    return (b == ".env" or b.startswith(".env.") or b == ".dev.vars") and b not in ENV_EXAMPLES


class ExportError(Exception):
    pass


def rmtree(p: Path) -> None:
    """Delete a workspace written by a container (same uid). Never follows symlinks."""
    if not p.exists() and not p.is_symlink():
        return

    def onexc(func, path, exc):
        # dirs written read-only by the agent: make the parent writable and retry (never chmod via a symlink)
        parent = os.path.dirname(path)
        try:
            if not os.path.islink(parent):
                os.chmod(parent, 0o700)
            if not os.path.islink(path) and os.path.isdir(path):
                os.chmod(path, 0o700)
        except OSError:
            pass
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=onexc)
    else:  # 3.11: onerror gets (func, path, exc_info); the handler ignores the third argument
        shutil.rmtree(p, onerror=onexc)


def make_work(app_dir: Path, dest: Path) -> None:
    """Fresh clone of the runner's app repo at HEAD for the coder to edit. Runner-created, then handed over."""
    rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    gitops.git(dest.parent, "clone", "-q", "--no-hardlinks", str(app_dir), dest.name)
    gitops.git(dest, "remote", "remove", "origin")
    gitops.git(dest, "config", "user.name", "hands-agent")
    gitops.git(dest, "config", "user.email", "agent@hands.local")
    excl = dest / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    with open(excl, "a") as f:
        f.write("\n/acceptance/\n")


# Runs INSIDE a --network none container as the host uid, workspace mounted read-only.
EXPORT_SCRIPT = r"""
set -o pipefail
cd /workspace || exit 3
git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c safe.directory='*' \
    ls-files -z -c -o --exclude-standard -- . ':(exclude)acceptance' ':(exclude)acceptance/**' \
  | tar --null --no-recursion --ignore-failed-read -T - -cf /out/tree.tar 2>/out/tar.err
rc=$?
[ $rc -le 1 ] && exit 0 || exit $rc
"""


# Same, for the planner: the spec repo's acceptance/ IS the output, so nothing is excluded.
EXPORT_SCRIPT_ALL = r"""
set -o pipefail
cd /workspace || exit 3
git -c core.fsmonitor=false -c core.hooksPath=/dev/null -c safe.directory='*' \
    ls-files -z -c -o --exclude-standard -- . \
  | tar --null --no-recursion --ignore-failed-read -T - -cf /out/tree.tar 2>/out/tar.err
rc=$?
[ $rc -le 1 ] && exit 0 || exit $rc
"""


def extract_tree(tar_path: Path, dest: Path) -> list[str]:
    """Write a validated export into a fresh runner-owned dir (regular files only). Returns the paths."""
    members = validate_tar(tar_path)
    rmtree(dest)
    dest.mkdir(parents=True)
    out = []
    with _open_regular(tar_path) as fh, tarfile.open(fileobj=fh, mode="r:") as tf:
        for m in members:
            target = dest / m.name
            cur = dest
            for part in Path(m.name).parts[:-1]:
                cur = cur / part
                if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
                    raise ExportError(f"path conflict at {m.name}")
                cur.mkdir(exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                raise ExportError(f"cannot read {m.name}")
            with open(target, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(target, 0o755 if m.mode & stat.S_IXUSR else 0o644)
            out.append(m.name)
    return out


@dataclass
class Change:
    status: str     # A M D T
    path: str
    mode: str       # new mode ("000000" for deletions)


def _open_regular(path: Path):
    """Open a file written by a container: no symlinks, no FIFOs/devices."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ExportError(f"cannot open {path.name}: {e}") from e
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ExportError(f"{path.name} is not a regular file")
    os.set_blocking(fd, True)
    return os.fdopen(fd, "rb")


def validate_tar(tar_path: Path) -> list[tarfile.TarInfo]:
    """Reject anything that is not a plain regular file at a clean relative path."""
    fh = _open_regular(tar_path)
    try:
        tf = tarfile.open(fileobj=fh, mode="r:")
    except (tarfile.TarError, OSError) as e:
        fh.close()
        raise ExportError(f"export tar unreadable: {e}") from e
    files, total = [], 0
    with fh, tf:
        for m in tf.getmembers():
            name = m.name
            if m.isdir():
                continue
            norm = posixpath.normpath(name)
            if (name.startswith("/") or norm.startswith("../") or norm in ("..", ".", "") or "\\" in name
                    or any(ord(ch) < 32 for ch in name)):
                raise ExportError(f"unsafe path in export: {name!r}")
            if any(_gitlike(seg) for seg in norm.split("/")):
                raise ExportError(f"export contains .git path: {name!r}")
            if not m.isreg():
                kind = "symlink" if m.issym() else "hardlink" if m.islnk() else f"type {m.type!r}"
                raise ExportError(f"{kind} not allowed in workspace: {name}")
            if m.size > MAX_FILE_BYTES:
                raise ExportError(f"file too large ({m.size} bytes): {name}")
            total += m.size
            m.name = norm
            files.append(m)
        if total > MAX_TOTAL_BYTES or len(files) > MAX_FILES:
            raise ExportError(f"export too large: {len(files)} files, {total} bytes")
    return files


def apply_tree(app_dir: Path, tar_path: Path) -> None:
    """Replace the tracked contents of the (clean, runner-owned) app clone with the exported tree, stage all.
    Any failure is an ExportError (the attempt is rejected, the runner keeps going)."""
    try:
        _apply_tree(app_dir, tar_path)
    except ExportError:
        raise
    except (OSError, gitops.GitError, tarfile.TarError, UnicodeError) as e:
        raise ExportError(f"workspace could not be applied: {e}") from e


def _apply_tree(app_dir: Path, tar_path: Path) -> None:
    members = validate_tar(tar_path)
    for rel in gitops.git(app_dir, "ls-files", "-z").split("\0"):
        if rel:
            p = app_dir / rel
            if p.is_file() or p.is_symlink():
                p.unlink()
    with _open_regular(tar_path) as fh, tarfile.open(fileobj=fh, mode="r:") as tf:
        for m in members:
            dest = app_dir / m.name
            # parents must be real directories inside app_dir
            cur = app_dir
            for part in Path(m.name).parts[:-1]:
                cur = cur / part
                if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
                    raise ExportError(f"path conflict at {cur}")
                cur.mkdir(exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                raise ExportError(f"cannot read {m.name}")
            with open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(dest, 0o755 if m.mode & stat.S_IXUSR else 0o644)
    # prune empty dirs left by deletions
    for root, dirs, files in os.walk(app_dir, topdown=False):
        if ".git" in Path(root).relative_to(app_dir).parts[:1]:
            continue
        if root != str(app_dir) and not os.listdir(root):
            os.rmdir(root)
    gitops.git(app_dir, "add", "-A")


def staged_changes(app_dir: Path, base: str) -> list[Change]:
    out = gitops.git(app_dir, "diff", "--cached", "--raw", "-z", "--no-renames", base)
    parts = [p for p in out.split("\0")]
    changes, i = [], 0
    while i < len(parts) - 1:
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        _, new_mode, _, _, st = meta[1:].split(" ")
        changes.append(Change(status=st[0], path=parts[i + 1], mode=new_mode))
        i += 2
    return changes


def scan_secrets(app_dir: Path, changes: list[Change], redactor) -> list[str]:
    """Which staged files (added/modified) contain a secret value. Names the key, never the value."""
    import subprocess
    bad = []
    for c in changes:
        if c.status == "D":
            continue
        p = subprocess.run(["git", "-C", str(app_dir), "show", f":{c.path}"], capture_output=True, env=gitops.ENV)
        if p.returncode != 0:
            continue
        keys = redactor.found(p.stdout)
        if keys:
            bad.append(f"{c.path}: contains the value of {', '.join(keys)}")
    return bad


def check_allowlist(changes: list[Change], allowed: list[str], allow_text: list[str] | tuple = ()) -> list[str]:
    bad = []
    for c in changes:
        segs = c.path.split("/")
        lsegs = [x.lower() for x in segs]
        base = segs[-1]
        ext = os.path.splitext(base)[1].lower()
        why = None
        if c.mode in ("120000", "160000"):
            why = "symlinks and submodules are not allowed"
        elif c.status != "D" and is_env_file(base):
            why = "environment files are never committed (values live in /run/hands/project.env); only .env.example with names"
        elif DENY_SEGMENTS_CI.intersection(lsegs) or any(_gitlike(x) for x in segs):
            why = f"'{(DENY_SEGMENTS_CI.intersection(lsegs) or {'.git'}).pop()}/' is runner/human-owned"
        elif base.lower() in DENY_BASENAMES_CI:
            why = f"{base} is human-owned (agent instruction/config file)"
        elif lsegs[0] in DENY_TOP_CI:
            why = f"'{segs[0]}/' is human-owned"
        elif ext in DENY_EXT and not path_allowed(c.path, list(allow_text)):
            why = f"documentation/text files ({ext}) are human-owned; put notes in your final SUMMARY line"
        elif not path_allowed(c.path, allowed):
            why = "outside allowed_paths"
        if why:
            bad.append(f"{c.status} {c.path}: {why}")
    return bad
