"""File browser backend (0.5 step 13): the dashboard's view of a few folders under the builder home.

Allowed roots come from ~/.config/hands/files.json  {"version": 1, "roots": [abs paths], "git_writes": bool}
(0600, written atomically); default roots: ~/harness ~/projects ~/ops ~/claude-harness. A root must be an
existing folder inside the home folder (not the home itself, not a hidden top-level folder) whose real
location is not a secret folder.

Always denied, whatever the settings say: ~/.config/hands (and HANDS_CONFIG_HOME), ~/.ssh, ~/.config/gh,
~/.git-credentials, ~/.claude (and CLAUDE_CONFIG_DIR), and every path whose REAL location is outside the
allowed roots. Every request path is relative to a root, joined, then resolved with os.path.realpath:
`..`, NUL, absolute paths and over-long names are refused before resolution; a symlink that leads out of
the roots or into a denied folder is refused after it; a symlink loop or a missing path is "not found".
Writes never go through a symlink in the final component, and never into a `.git` folder unless
`git_writes` is on. Delete removes a link, never what it points to; recursive delete needs the flag.

Errors: ValueError = bad request (400), LookupError = unknown root or missing path (404),
PermissionError = denied (403), Conflict = exists / not empty / no overwrite (409).
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path

from . import config as cfgmod

HOME = Path.home()                      # tests swap it
DEFAULT_ROOTS = ("harness", "projects", "ops", "claude-harness")
DENIED_UNDER_HOME = (".config/hands", ".ssh", ".config/gh", ".git-credentials", ".claude")
MAX_ROOTS = 50
MAX_PATH = 4096
MAX_NAME = 255
MAX_LIST = 5000                         # entries per folder listing
MAX_TEXT = 2_000_000                    # bytes returned by read (text)
MAX_UPLOAD = 256 * 1024 * 1024          # bytes per upload (streamed to disk, never held in memory)
MAX_IMAGE = 32 * 1024 * 1024            # bytes per image preview
# raster images the page may preview inline (by the real file's extension). SVG is never served as an
# image: it can carry script, so the page shows it as text.
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
               "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp", "ico": "image/x-icon"}
MAX_HITS = 500                          # search results
MAX_SCAN = 100_000                      # directory entries a search may visit
SEARCH_SECONDS = 5.0
MAX_QUERY = 200
SKIP_DIRS = {".git", "node_modules"}    # never searched (listing still shows them)
CHUNK = 65536
SNIFF = 8192                            # bytes checked for NUL to call a file binary
FILE_MODE, DIR_MODE = 0o644, 0o755

_MUTEX = threading.Lock()


class Conflict(Exception):
    pass


class TooLarge(Exception):
    pass


# ---------------------------------------------------------------- settings
def settings_path() -> Path:
    return cfgmod.CONFIG_HOME / "files.json"


def _home() -> str:
    return os.path.realpath(str(HOME))


def denied_paths() -> list[str]:
    """Real paths that are never served, even inside an allowed root: the secret folders plus every
    project's state dir (attempt files and session streams hold tool output, possibly secret values,
    until the runner has redacted them)."""
    home = _home()
    out = [os.path.realpath(os.path.join(home, d)) for d in DENIED_UNDER_HOME]
    out.append(os.path.realpath(str(cfgmod.CONFIG_HOME)))
    out.append(os.path.realpath(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")))
    for d in project_state_dirs():
        if d not in out:
            out.append(d)
    return out


def project_state_dirs() -> list[str]:
    """state_dir of every project config (parsed leniently: a broken config still yields its state_dir)."""
    import tomllib
    out = []
    pdir = cfgmod.CONFIG_HOME / "projects"
    try:
        tomls = sorted(pdir.glob("*.toml"))
    except OSError:
        return out
    for t in tomls[:500]:
        try:
            raw = tomllib.loads(t.read_text())
            sd = raw.get("state_dir")
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            continue
        if isinstance(sd, str) and sd:
            out.append(os.path.realpath(os.path.expanduser(sd)))
    return out


def _within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _denied(real: str) -> bool:
    return any(_within(real, d) for d in denied_paths())


def _expand(raw) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH or "\0" in raw:
        raise ValueError("root: give an absolute path under your home folder")
    home = _home()
    if raw == "~" or raw.startswith("~/"):
        raw = home + raw[1:]
    if not raw.startswith("/"):
        raise ValueError(f"root {raw!r} must be an absolute path (or start with ~/)")
    return raw


def _root_allowed(real: str) -> bool:
    """The one rule for roots, used on save AND on every load of files.json: inside home, not home itself,
    not a hidden top-level folder (~/.config, ~/.local, ...), not a denied place."""
    home = _home()
    return (real != home and _within(real, home) and not real[len(home) + 1:].split("/")[0].startswith(".")
            and not _denied(real))


def validate_root(raw) -> str:
    """An existing folder inside home (never home itself or a hidden top-level folder), by real path."""
    raw = _expand(raw)
    home = _home()
    try:
        real = os.path.realpath(raw, strict=True)
    except OSError:
        raise ValueError(f"root {raw} does not exist")
    if real == home or not _within(real, home):
        raise ValueError(f"root {raw} must be inside your home folder (and not the home folder itself)")
    if real[len(home) + 1:].split("/")[0].startswith("."):
        raise ValueError(f"root {raw} cannot be a hidden folder such as ~/.config or ~/.ssh")
    if _denied(real):
        raise ValueError(f"root {raw} is a protected folder")
    if not os.path.isdir(real):
        raise ValueError(f"root {raw} is not a folder")
    return real


def default_roots() -> list[str]:
    return [os.path.join(str(HOME), n) for n in DEFAULT_ROOTS]


def settings() -> dict:
    """{"roots": [real paths], "git_writes": bool}. Roots in a protected place are dropped, missing ones kept."""
    roots, git_writes = default_roots(), False
    p = settings_path()
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        fd = -1
    if fd >= 0:
        try:
            d = json.loads(os.read(fd, 1_000_000).decode("utf-8", errors="replace"))
        except ValueError:
            d = None
        finally:
            os.close(fd)
        if isinstance(d, dict):
            if isinstance(d.get("roots"), list):
                roots = [r for r in d["roots"] if isinstance(r, str)]
            git_writes = d.get("git_writes") is True
    out: list[str] = []
    for r in roots[:MAX_ROOTS]:
        try:
            r = _expand(r)
        except ValueError:
            continue
        real = os.path.realpath(r)
        if not _root_allowed(real):
            continue
        if real not in out:
            out.append(real)
    return {"roots": out, "git_writes": git_writes}


def save_settings(req) -> dict:
    if not isinstance(req, dict) or set(req) - {"roots", "git_writes"}:
        raise ValueError("settings: give 'roots' (list of folders) and 'git_writes' (true/false)")
    raw = req.get("roots")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ROOTS:
        raise ValueError(f"roots: give 1 to {MAX_ROOTS} folders")
    if not isinstance(req.get("git_writes"), bool):
        raise ValueError("git_writes must be true or false")
    roots: list[str] = []
    for r in raw:
        real = validate_root(r)
        if real not in roots:
            roots.append(real)
    data = {"version": 1, "roots": roots, "git_writes": req["git_writes"]}
    with _MUTEX:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
                f.write("\n")
            os.replace(tmp, p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return settings()


def denied_display() -> list[str]:
    """The always-denied places, as the settings view shows them (~/... where under home), no duplicates."""
    home, out = _home(), []
    for d in denied_paths():
        shown = "~" + d[len(home):] if _within(d, home) and d != home else d
        if shown not in out:
            out.append(shown)
    return out


def roots_info() -> dict:
    s = settings()
    return {"roots": [{"name": os.path.basename(r), "path": r, "exists": os.path.isdir(r)} for r in s["roots"]],
            "git_writes": s["git_writes"], "home": _home(), "denied": denied_display(),
            "limits": {"max_upload": MAX_UPLOAD, "max_text": MAX_TEXT, "max_list": MAX_LIST, "max_hits": MAX_HITS,
                       "max_image": MAX_IMAGE}}


# ---------------------------------------------------------------- resolution
def _root(root, s: dict | None = None) -> str:
    if not isinstance(root, str):
        raise ValueError("root must be a string")
    s = s or settings()
    if root not in s["roots"]:
        raise LookupError("unknown root")
    return root


def split_rel(rel) -> list[str]:
    """Relative path -> clean segments. Refuses NUL, absolute paths, `..` and over-long names."""
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    if len(rel) > MAX_PATH or "\0" in rel:
        raise ValueError("path too long or contains NUL")
    if rel.startswith("/"):
        raise ValueError("path must be relative to the root")
    parts = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("path may not contain ..")
        if len(seg.encode("utf-8", "surrogateescape")) > MAX_NAME:
            raise ValueError("name too long")
        parts.append(seg)
    return parts


def _check_bounds(real: str, s: dict) -> None:
    if _denied(real):
        raise PermissionError("that location is protected")
    if not any(_within(real, r) for r in s["roots"]):
        raise PermissionError("that location is outside the allowed folders")


def _strict(path: str) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise LookupError("symlink loop")
        raise LookupError("not found")


def resolve(root, rel, s: dict | None = None) -> Path:
    """The real path of an existing entry (symlinks followed), or ValueError / LookupError / PermissionError."""
    s = s or settings()
    root = _root(root, s)
    parts = split_rel(rel)
    joined = os.path.join(root, *parts) if parts else root
    _check_bounds(os.path.realpath(joined), s)       # the lexical/partial resolution first: outside = 403, not 404
    real = _strict(joined)
    _check_bounds(real, s)
    return Path(real)


def canonical(real: Path, prefer: str, s: dict) -> tuple[str, str]:
    """(root, relative path) for a real path: the requested root if it contains it, else the longest root."""
    r = str(real)
    cands = [x for x in s["roots"] if _within(r, x)]
    if not cands:
        raise PermissionError("that location is outside the allowed folders")
    root = prefer if prefer in cands else max(cands, key=len)
    return root, r[len(root) + 1:]


def _git_guard(root: str, parent_real: str, name: str, s: dict, whole_dir: bool = False) -> None:
    """With git_writes off: nothing inside a .git folder may change, and (whole_dir) a folder that contains
    a .git anywhere below it may not be moved or deleted (that would take a clone's history with it)."""
    if s["git_writes"]:
        return
    rel = canonical(Path(os.path.join(parent_real, name)), root, s)[1]
    if ".git" in rel.split("/"):
        raise PermissionError("writing inside a .git folder is off (File browser settings)")
    if whole_dir and _contains_git(os.path.join(parent_real, name)):
        raise PermissionError("that folder contains a git repository (.git); moving or deleting it is off "
                              "(File browser settings)")


def _contains_git(path: str, budget: int = 200_000) -> bool:
    stack = [path]
    while stack and budget > 0:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for de in it:
                budget -= 1
                try:
                    if de.is_dir(follow_symlinks=False):
                        if de.name == ".git":
                            return True
                        stack.append(de.path)
                    elif de.name == ".git":           # a .git file (worktree/submodule pointer)
                        return True
                except OSError:
                    continue
    return False


def resolve_new(root, rel, s: dict | None = None) -> tuple[Path, str]:
    """(real parent folder, name) for a path to create or move to. The parent must exist inside the
    roots; the final component may not be a symlink (it may exist: callers decide). Never the root itself."""
    s = s or settings()
    root = _root(root, s)
    parts = split_rel(rel)
    if not parts or rel.rstrip("/").rsplit("/", 1)[-1] in ("", "."):
        raise ValueError("give a name")
    parent = resolve(root, "/".join(parts[:-1]), s)
    if not parent.is_dir():
        raise LookupError("not found")
    name = parts[-1]
    final = parent / name
    try:
        if stat.S_ISLNK(os.lstat(final).st_mode):
            raise PermissionError("that name is a symbolic link; writes through links are refused")
    except FileNotFoundError:
        pass
    _git_guard(root, str(parent), name, s)
    return parent, name


def resolve_entry(root, rel, s: dict | None = None) -> tuple[Path, os.stat_result]:
    """An existing entry to rename or delete: its own path (a symlink is the link, not its target) and
    lstat. A link must point inside the roots; the root itself is refused; .git is guarded."""
    s = s or settings()
    root = _root(root, s)
    parts = split_rel(rel)
    if not parts:
        raise ValueError("the root folder itself cannot be changed")
    parent = resolve(root, "/".join(parts[:-1]), s)
    name = parts[-1]
    entry = parent / name
    try:
        st = os.lstat(entry)
    except OSError:
        raise LookupError("not found")
    if stat.S_ISLNK(st.st_mode):
        _check_bounds(os.path.realpath(entry), s)
    _git_guard(root, str(parent), name, s, whole_dir=stat.S_ISDIR(st.st_mode))
    return entry, st


# ---------------------------------------------------------------- operations
def _type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def _entry(de: os.DirEntry, s: dict) -> dict:
    st = de.stat(follow_symlinks=False)
    row = {"name": de.name, "type": _type(st.st_mode), "size": st.st_size, "modified": int(st.st_mtime),
           "mode": f"{st.st_mode & 0o7777:04o}"}
    if row["type"] == "link":
        ok, ttype = False, None
        try:
            real = os.path.realpath(de.path, strict=True)
            if not _denied(real) and any(_within(real, r) for r in s["roots"]):
                ok, ttype = True, _type(os.stat(real).st_mode)
        except OSError:
            pass
        row["ok"], row["target_type"] = ok, ttype
    return row


def listing(root, rel) -> dict:
    s = settings()
    real = resolve(root, rel, s)
    if not real.is_dir():
        raise ValueError("not a folder")
    croot, crel = canonical(real, root, s)
    rows, truncated = [], False
    with os.scandir(real) as it:
        for de in it:
            if len(rows) >= MAX_LIST:
                truncated = True
                break
            try:
                if de.is_dir(follow_symlinks=False) and _denied(os.path.realpath(de.path)):
                    continue                      # protected places are not even named
                rows.append(_entry(de, s))
            except OSError:
                continue
    rows.sort(key=lambda r: (r["type"] != "dir", r["name"]))
    return {"root": croot, "path": crel, "entries": rows, "truncated": truncated, "git": ".git" in crel.split("/"),
            "git_writes": s["git_writes"]}


def _open_file(real: Path) -> tuple[int, int]:
    fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise ValueError("that is a folder")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st.st_size


def read_text(root, rel) -> dict:
    s = settings()
    real = resolve(root, rel, s)
    croot, crel = canonical(real, root, s)
    fd, size = _open_file(real)
    try:
        data = b""
        while len(data) < MAX_TEXT:
            chunk = os.read(fd, min(CHUNK, MAX_TEXT - len(data)))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    out = {"root": croot, "path": crel, "size": size, "truncated": size > MAX_TEXT, "binary": True}
    if b"\0" in data[:SNIFF]:
        return out
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        if not out["truncated"] or e.start < len(data) - 4:
            return out
        text = data[:e.start].decode("utf-8")          # a multi-byte character cut by the cap
    out["binary"] = False
    out["text"] = text
    return out


def open_download(root, rel) -> tuple[int, int, str]:
    """(fd, size, file name) of a regular file, opened for streaming. The caller closes the fd."""
    s = settings()
    real = resolve(root, rel, s)
    fd, size = _open_file(real)
    return fd, size, real.name


def open_image(root, rel) -> tuple[int, int, str, str]:
    """(fd, size, file name, MIME type) of a raster image for the preview. The caller closes the fd."""
    s = settings()
    real = resolve(root, rel, s)
    ctype = IMAGE_TYPES.get(real.name.rsplit(".", 1)[-1].lower()) if "." in real.name else None
    if not ctype:
        raise ValueError("not an image the browser can preview (png, jpg, gif, webp, avif, bmp, ico)")
    fd, size = _open_file(real)
    if size > MAX_IMAGE:
        os.close(fd)
        raise TooLarge(f"image too large to preview (max {MAX_IMAGE // (1024 * 1024)} MB); download it instead")
    return fd, size, real.name, ctype


def mkdir(root, rel) -> dict:
    s = settings()
    parent, name = resolve_new(root, rel, s)
    target = parent / name
    try:
        os.mkdir(target, DIR_MODE)
    except FileExistsError:
        raise Conflict("something with that name already exists")
    os.chmod(target, DIR_MODE)
    croot, crel = canonical(target, root, s)
    return {"ok": True, "root": croot, "path": crel}


def rename(root, rel, to_root, to_rel) -> dict:
    s = settings()
    src, st = resolve_entry(root, rel, s)
    parent, name = resolve_new(to_root if to_root is not None else root, to_rel, s)
    dst = parent / name
    if stat.S_ISDIR(st.st_mode) and _within(str(parent), str(src)):
        raise ValueError("cannot move a folder into itself")
    try:
        os.lstat(dst)
    except FileNotFoundError:
        pass
    else:
        raise Conflict("something with that name already exists")
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise ValueError("cannot move across file systems")
        if e.errno in (errno.EEXIST, errno.ENOTEMPTY):
            raise Conflict("something with that name already exists")
        raise
    croot, crel = canonical(dst, to_root if to_root is not None else root, s)
    return {"ok": True, "root": croot, "path": crel}


def delete(root, rel, recursive=False) -> dict:
    if not isinstance(recursive, bool):
        raise ValueError("recursive must be true or false")
    s = settings()
    entry, st = resolve_entry(root, rel, s)
    if stat.S_ISDIR(st.st_mode):
        if recursive:
            shutil.rmtree(entry)          # symlinks inside are unlinked, never followed
        else:
            try:
                os.rmdir(entry)
            except OSError as e:
                if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    raise Conflict("the folder is not empty; confirm to delete it with everything inside")
                raise
    else:
        os.unlink(entry)
    return {"ok": True}


class Upload:
    """Streams a request body into a temp file next to the target, then renames it into place.
    Nothing is created when the checks fail; an aborted upload leaves no file behind."""

    def __init__(self, root, rel, overwrite: bool):
        s = settings()
        self.parent, self.name = resolve_new(root, rel, s)
        self.target = self.parent / self.name
        try:
            st = os.lstat(self.target)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISREG(st.st_mode):
                raise Conflict("something with that name already exists and is not a file")
            if not overwrite:
                raise Conflict("a file with that name already exists; confirm to replace it")
        self.croot, self.crel = canonical(self.target, root, s)
        prefix = self.name.encode("utf-8")[:120].decode("utf-8", errors="ignore")      # NAME_MAX is in bytes
        self.tmp = self.parent / f".{prefix}.upload-{os.getpid()}-{secrets.token_hex(4)}"
        self.fd = os.open(self.tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.size = 0

    def write(self, chunk: bytes) -> None:
        os.write(self.fd, chunk)
        self.size += len(chunk)

    def commit(self) -> dict:
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = -1
        os.chmod(self.tmp, FILE_MODE)
        os.replace(self.tmp, self.target)
        return {"ok": True, "root": self.croot, "path": self.crel, "size": self.size}

    def abort(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        try:
            os.unlink(self.tmp)
        except OSError:
            pass


def search(root, q, limit: int = MAX_HITS) -> dict:
    s = settings()
    if not isinstance(q, str) or not q.strip() or len(q) > MAX_QUERY:
        raise ValueError(f"search: 1 to {MAX_QUERY} characters")
    base = resolve(root, "", s)
    needle = q.strip().lower()
    limit = max(1, min(int(limit), MAX_HITS))
    hits, scanned, truncated = [], 0, False
    deadline = time.monotonic() + SEARCH_SECONDS
    stack = [str(base)]
    while stack and not truncated:
        d = stack.pop()
        if _denied(d):
            continue
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for de in it:
                scanned += 1
                if scanned > MAX_SCAN or time.monotonic() > deadline or len(hits) >= limit:
                    truncated = True
                    break
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if is_dir and de.name in SKIP_DIRS:
                    continue
                if needle in de.name.lower():
                    hits.append({"path": os.path.relpath(de.path, str(base)), "type": _type(st.st_mode),
                                 "size": st.st_size, "modified": int(st.st_mtime)})
                if is_dir:
                    stack.append(de.path)
    hits.sort(key=lambda h: h["path"])
    return {"root": str(base), "q": q.strip(), "hits": hits, "truncated": truncated}
