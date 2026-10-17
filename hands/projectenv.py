"""Per-project environment variables for the build containers (step 06).

Files, all under ~/.config/hands (never inside the project folder or a repo):
  secrets/<slug>/project.env   KEY=value lines, mode 0600, dir 0700. This is the file docker gets with
                               --env-file, which takes the value VERBATIM after the first "=": no quoting
                               layer exists in that format, so none is written here either. Every allowed
                               value (one line, no NUL, <= 8 KB) round-trips as is: "=", "#", quotes,
                               spaces and unicode are plain characters. Hand-written files load the same
                               way (a quoted value keeps its quotes, exactly as the container sees it).
  secrets/<slug>/env.json      metadata only: {"version": 1, "vars": {KEY: {"public": bool, "updated": iso}}}.
                               Never a value. Keys in project.env without metadata are secret.
  creating/<slug>.pending/     the same two files for a project whose `create` is still running; moved
                               into secrets/<slug>/ when create succeeds, deleted when it fails.

Secret values are write-only: listing() never returns them, only public values are returned.
Writes are atomic (temp file + rename) and validated completely before anything is written.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MAX_KEY = 128
MAX_VALUE = 8192            # bytes of UTF-8
RESERVED = {"CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "TG_TOKEN", "TG_CHAT_ID", "PATH", "HOME", "USER",
            "SHELL", "CI", "NODE_OPTIONS"}
RESERVED_PREFIXES = ("HANDS_", "CLAUDE_")
ENV_FILE = "project.env"
META_FILE = "env.json"
HEADER = ("# dev/test variables for this project's build containers (coder, acceptance, evaluator).\n"
          "# Managed by the Phoenix dashboard; KEY=value, the value is passed verbatim (no quotes needed).\n")


class EnvError(ValueError):
    pass


def validate_key(key) -> None:
    if not isinstance(key, str) or not key or len(key) > MAX_KEY or not KEY.match(key):
        raise EnvError(f"key must be UPPER_CASE letters, digits and _ (not starting with a digit), max {MAX_KEY} chars")
    if key in RESERVED or key.startswith(RESERVED_PREFIXES):
        raise EnvError(f"{key} is reserved")


def validate_value(value) -> None:
    if not isinstance(value, str):
        raise EnvError("value must be a string")
    if "\n" in value or "\r" in value or "\0" in value:
        raise EnvError("value must be a single line without NUL")
    if len(value.encode("utf-8")) > MAX_VALUE:
        raise EnvError(f"value too long (max {MAX_VALUE} bytes)")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_private(path: Path) -> str | None:
    """Read a regular file without following symlinks; None if it does not exist."""
    if path.is_symlink():
        raise EnvError(f"{path} is a symlink; refusing")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise EnvError(f"{path} is not a regular file")
        data = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 4_000_000:
                raise EnvError(f"{path} is too large")
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: new inode via O_EXCL temp file, then rename over the target."""
    if path.is_symlink():
        raise EnvError(f"{path} is a symlink; refusing")
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _mkdir_private(d: Path) -> None:
    if d.is_symlink():
        raise EnvError(f"{d} is a symlink; refusing")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)


class Store:
    """One project's variables: values from project.env (docker env-file format), metadata from env.json."""

    def __init__(self, directory: Path, env_name: str = ENV_FILE):
        self.dir = directory
        self.env_path = directory / env_name
        self.meta_path = directory / META_FILE
        self.values: dict[str, str] = {}
        self.meta: dict[str, dict] = {}
        self.problems: list[str] = []
        self._load()

    # -- read
    def _load(self) -> None:
        text = _read_private(self.env_path)
        if text is not None:
            for n, raw in enumerate(text.split("\n"), 1):
                line = raw.lstrip()                       # docker: leading whitespace of the line is ignored
                if not line or line.startswith("#"):
                    continue
                key, eq, value = line.partition("=")
                if not eq:
                    self.problems.append(f"line {n}: no '=' (docker would copy that name from the host environment)")
                    continue
                if not KEY.match(key) or len(key) > MAX_KEY:
                    self.problems.append(f"line {n}: key is not UPPER_CASE; the dashboard cannot manage it")
                    continue
                if key in RESERVED or key.startswith(RESERVED_PREFIXES):
                    self.problems.append(f"line {n}: {key} is reserved and should not be set here")
                self.values[key] = value
        raw_meta = _read_private(self.meta_path)
        vars_meta = {}
        if raw_meta:
            try:
                d = json.loads(raw_meta)
                vars_meta = d.get("vars") or {} if isinstance(d, dict) else {}
            except ValueError:
                self.problems.append(f"{self.meta_path.name} is not valid JSON; visibility reset to secret")
        for k in self.values:
            m = vars_meta.get(k) if isinstance(vars_meta, dict) else None
            self.meta[k] = {"public": bool(m.get("public")) if isinstance(m, dict) else False,
                            "updated": m.get("updated") if isinstance(m, dict) and isinstance(m.get("updated"), str) else None}

    def listing(self) -> list[dict]:
        rows = []
        for k, v in self.values.items():
            m = self.meta[k]
            row = {"key": k, "secret": not m["public"], "updated": m["updated"]}
            if m["public"]:
                row["value"] = v
            rows.append(row)
        return rows

    # -- write
    def set(self, items: list[dict]) -> None:
        """items: [{"key", "value"?, "public"?}] — value required for a new key; omitted = keep the value."""
        if not isinstance(items, list) or not items:
            raise EnvError("set: expected a non-empty list")
        new_values, new_meta, seen = dict(self.values), {k: dict(m) for k, m in self.meta.items()}, set()
        for it in items:
            if not isinstance(it, dict):
                raise EnvError("set: each item must be an object")
            key = it.get("key")
            validate_key(key)
            if key in seen:
                raise EnvError(f"{key} given twice")
            seen.add(key)
            public = it.get("public", False)
            if not isinstance(public, bool):
                raise EnvError(f"{key}: public must be true or false")
            if "value" in it and it["value"] is not None:
                validate_value(it["value"])
                new_values[key] = it["value"]
            elif key not in new_values:
                raise EnvError(f"{key}: value required")
            elif public and not new_meta.get(key, {}).get("public"):
                # a stored secret is write-only: making it public would reveal it, so the value must be given again
                raise EnvError(f"{key}: enter the value again to make it public")
            new_meta[key] = {"public": public, "updated": _now()}
        self._save(new_values, new_meta)

    def delete(self, key: str) -> None:
        if not isinstance(key, str) or key not in self.values:
            raise EnvError(f"no such key: {key if isinstance(key, str) else '?'}"[:200])
        new_values = {k: v for k, v in self.values.items() if k != key}
        new_meta = {k: m for k, m in self.meta.items() if k != key}
        self._save(new_values, new_meta)

    def _save(self, values: dict[str, str], meta: dict[str, dict]) -> None:
        if self.dir.parent.name == "secrets":
            _mkdir_private(self.dir.parent)
        _mkdir_private(self.dir)
        _write_private(self.env_path, HEADER + "".join(f"{k}={v}\n" for k, v in values.items()))
        _write_private(self.meta_path, json.dumps({"version": 1, "vars": {k: meta[k] for k in values}}, indent=1) + "\n")
        self.values, self.meta, self.problems = values, meta, []


# ---------------------------------------------------------------- pending (project still being created)
def pending_dir(slug: str) -> Path:
    from . import newproject
    return newproject.creating_dir() / f"{slug}.pending"


def pending_store(slug: str) -> Store:
    return Store(pending_dir(slug))


def discard_pending(slug: str) -> None:
    d = pending_dir(slug)
    if d.is_dir() and not d.is_symlink():
        shutil.rmtree(d)


def adopt_pending(slug: str, target: Path) -> int:
    """Move pending variables into the project's secrets dir (after a successful create). Returns the count."""
    d = pending_dir(slug)
    if not d.is_dir() or d.is_symlink():
        return 0
    src = Store(d)
    n = 0
    if src.values:
        dst = Store(target)
        dst.set([{"key": k, "value": v, "public": src.meta[k]["public"]} for k, v in src.values.items()])
        n = len(src.values)
    discard_pending(slug)
    return n
