"""Session manager backend: persistent Claude Code terminals for the dashboard (0.5 step 10).

Design (docs/console-review.md §6, option C): each session is one `claude` process inside its own tmux
session `hands_s_<uuid>`, started with a clean allowlisted environment (no dashboard environment, no
GitHub/Telegram secrets, no `--dangerously-skip-permissions`, normal permission prompts). A browser
viewer is a `tmux attach` client on a PTY owned by the dashboard thread that serves its WebSocket
(hands/ws.py); the session outlives every viewer, the dashboard and `bash install.sh`.

Owned files:
  ~/.config/hands/sessions/<uuid>.json   0600, {id, cwd, created, title}  (the known session list)
  ~/.claude/projects/<cwd encoded>/<uuid>.jsonl   Claude's own transcript; listing reads its first prompt
                                                  as the title; delete removes exactly this file.
Nothing else under ~/.claude is read, listed or deleted.
"""
from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import termios
import threading
import time
import uuid
from pathlib import Path

from . import config as cfgmod
from . import ws as wsmod

HOME = Path.home()                      # root for working folders (tests swap it)
MAX_TERMINALS = 8                       # open terminal connections, all sessions together
TICKET_TTL = 30                         # seconds a WebSocket ticket stays valid (single use)
ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")   # uuid4, lower case
COLS, ROWS = (20, 500), (5, 300)
TITLE_BYTES = 256 * 1024
PTY_CHUNK = 8192
# Claude's own login is ~/.claude/.credentials.json, reached through HOME (or CLAUDE_CONFIG_DIR). Token
# variables are deliberately not passed: the session command line (`env -i K=V ...`) is visible to tmux.
PASSTHROUGH = ("CLAUDE_CONFIG_DIR", "TMUX_TMPDIR")
# The one channel a "live" session may run (step 12): Claude Code's official Telegram plugin, whose bot
# token lives in ~/.claude/channels/telegram/.env and is read by the plugin inside the session, never here.
TELEGRAM_CHANNEL = "plugin:telegram@claude-plugins-official"


# ---------------------------------------------------------------- validation and pure helpers
def new_id() -> str:
    return str(uuid.uuid4())


def validate_id(sid) -> str:
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        raise ValueError("bad session id")
    return sid


def default_cwd() -> Path:
    return HOME / "ops"


def validate_cwd(raw, home: Path | None = None) -> Path:
    """An existing directory under home (never home itself, never a hidden top-level folder such as
    ~/.config or ~/.ssh), resolved through symlinks. Returns the real path."""
    home = (home or HOME).resolve()
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or "\0" in raw:
        raise ValueError("working folder: give a path under your home folder")
    if raw == "~" or raw.startswith("~/"):
        raw = str(home) + raw[1:]
    if not raw.startswith("/"):
        raise ValueError("working folder must be an absolute path (or start with ~/)")
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("working folder does not exist")
    if p == home or home not in p.parents:
        raise ValueError("working folder must be inside your home folder (and not the home folder itself)")
    if p.relative_to(home).parts[0].startswith("."):
        raise ValueError("working folder cannot be a hidden folder such as ~/.config or ~/.ssh")
    if not p.is_dir():
        raise ValueError("working folder is not a folder")
    return p


def claude_exe() -> str:
    override = os.environ.get("HANDS_CLAUDE")
    if override:
        if not (os.path.isabs(override) and os.path.isfile(override) and os.access(override, os.X_OK)):
            raise ValueError("HANDS_CLAUDE must be an absolute path to an executable")
        return override
    p = Path.home() / ".local" / "bin" / "claude"
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    w = shutil.which("claude")
    if w:
        return w
    raise ValueError("claude is not installed (~/.local/bin/claude)")


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def sessions_dir() -> Path:
    return cfgmod.CONFIG_HOME / "sessions"


def tmux_name(sid: str) -> str:
    return f"hands_s_{validate_id(sid)}"


def _base_env() -> dict[str, str]:
    home = Path.home()
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = os.environ.get("USER", "builder")
    # ~/.bun/bin: the Telegram channel plugin is `bun run ...` resolved through this PATH (step 12)
    return {"PATH": f"{home}/.local/bin:{home}/.bun/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
            "TERM": "xterm-256color", "LANG": "C.UTF-8", "USER": user, "SHELL": "/bin/bash"}
    # no COLORTERM on purpose: 24-bit colour cells need a style attribute the CSP forbids (console-review §5)


def session_env() -> dict[str, str]:
    """The whole environment of a Claude session: an allowlist built from scratch, never a copy of the
    dashboard's environment (so no GH_TOKEN/TG_*/CLAUDECODE/COLORTERM can reach it). HOME carries
    Claude's own login and settings; CLAUDE_CONFIG_DIR passes through if the dashboard has one."""
    env = _base_env()
    for k in PASSTHROUGH:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def tmux_env() -> dict[str, str]:
    """Environment for tmux commands and `tmux attach` clients (no TMUX: the dashboard itself runs in tmux)."""
    env = _base_env()
    if os.environ.get("TMUX_TMPDIR"):
        env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
    return env


def is_telegram(rec: dict) -> bool:
    """The record's live-on-Telegram flag; only the JSON boolean true counts."""
    return rec.get("telegram") is True


def claude_argv(sid: str, resume: bool, telegram: bool = False) -> list[str]:
    """Default permission mode, the user's normal settings; our uuid is Claude's session id. A live
    session adds the Telegram channel plugin (and nothing else: no permission flags ever)."""
    argv = [claude_exe(), "--resume" if resume else "--session-id", validate_id(sid)]
    if telegram is True:
        argv += ["--channels", TELEGRAM_CHANNEL]
    return argv


def tmux_command(sid: str, resume: bool, telegram: bool = False) -> str:
    """What tmux's shell runs inside the session: `env -i` makes the allowlist the entire environment."""
    env = session_env()
    return "exec env -i " + " ".join(shlex.quote(f"{k}={v}") for k, v in env.items()) + " " + \
        " ".join(shlex.quote(a) for a in claude_argv(sid, resume, telegram))


def transcript_path(root: Path, cwd: Path, sid: str) -> Path:
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))       # Claude Code's project directory naming
    return root / "projects" / enc / f"{validate_id(sid)}.jsonl"


def read_title(path: Path) -> str | None:
    """First real user prompt of a Claude transcript (bounded read, no symlinks), or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b""
        while len(data) < TITLE_BYTES:
            chunk = os.read(fd, min(65536, TITLE_BYTES - len(data)))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict) or o.get("type") != "user" or o.get("isMeta"):
            continue
        m = o.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            c = next((p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"), "")
        if not isinstance(c, str):
            continue
        c = c.strip()
        if not c or c.startswith("<") or c.startswith("/"):
            continue        # meta/XML lines and slash commands (`/telegram:configure <token>`) never become titles
        return c.splitlines()[0].strip()[:80]
    return None


# ---------------------------------------------------------------- tmux backend
class Tmux:
    def _run(self, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], env=tmux_env(), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def start(self, rec: dict, resume: bool) -> None:
        name = tmux_name(rec["id"])
        r = self._run("new-session", "-d", "-s", name, "-c", rec["cwd"], "-x", "200", "-y", "50",
                      tmux_command(rec["id"], resume, is_telegram(rec)))
        if r.returncode:
            raise ValueError(f"could not start the session: {r.stderr.strip()[:300] or 'tmux failed'}")
        self._run("set-option", "-t", f"={name}", "status", "off")      # the browser is the only UI

    def alive(self, sid: str) -> bool:
        try:
            return self._run("has-session", "-t", f"={tmux_name(sid)}").returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def stop(self, sid: str) -> None:
        self._run("kill-session", "-t", f"={tmux_name(sid)}")

    def attach_argv(self, sid: str) -> list[str]:
        exe = shutil.which("tmux", path=tmux_env()["PATH"])
        if not exe:
            raise ValueError("tmux is not installed")
        return [exe, "attach-session", "-t", f"={tmux_name(sid)}"]


BACKEND = Tmux()


# ---------------------------------------------------------------- session records
class Store:
    def __init__(self, d: Path | None = None):
        self.dir = d or sessions_dir()

    def _path(self, sid: str) -> Path:
        return self.dir / f"{validate_id(sid)}.json"

    def _write(self, rec: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = self._path(rec["id"])
        tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(rec, f, indent=1)
                f.write("\n")
            os.replace(tmp, p)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def create(self, cwd: Path, telegram: bool = False) -> dict:
        rec = {"id": new_id(), "cwd": str(cwd), "created": round(time.time(), 3), "title": "", "telegram": telegram is True}
        self._write(rec)
        return rec

    def live_telegram(self, exclude: str = "") -> dict | None:
        """The record (other than `exclude`) that is flagged for Telegram and currently running, if any."""
        for rec in self.records():
            if rec["id"] != exclude and is_telegram(rec) and BACKEND.alive(rec["id"]):
                return rec
        return None

    def get(self, sid: str) -> dict:
        p = self._path(sid)
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise LookupError(sid)
        try:
            rec = json.loads(os.read(fd, 65536).decode("utf-8", errors="replace"))
        except ValueError:
            raise LookupError(sid)
        finally:
            os.close(fd)
        if not isinstance(rec, dict) or rec.get("id") != sid or not isinstance(rec.get("cwd"), str):
            raise LookupError(sid)
        return rec

    def records(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in self.dir.glob("*.json"):
            if ID.match(p.stem):
                try:
                    out.append(self.get(p.stem))
                except LookupError:
                    pass
        return out

    def row(self, rec: dict) -> dict:
        sid = rec["id"]
        t = transcript_path(claude_home(), Path(rec["cwd"]), sid)
        title = rec.get("title") or ""
        if not title:
            title = read_title(t) or ""
            if title:
                rec["title"] = title
                try:
                    self._write(rec)
                except OSError:
                    pass
        created = float(rec.get("created") or 0)
        last = created
        try:
            st = os.lstat(t)
            if stat.S_ISREG(st.st_mode):
                last = max(last, st.st_mtime)
                has_transcript = True
            else:
                has_transcript = False
        except OSError:
            has_transcript = False
        return {"id": sid, "title": title, "cwd": rec["cwd"], "created": created, "last_active": last,
                "live": BACKEND.alive(sid), "viewers": viewer_count(sid), "transcript": has_transcript,
                "telegram": is_telegram(rec)}

    def listing(self) -> list[dict]:
        rows = [self.row(r) for r in self.records()]
        rows.sort(key=lambda r: (r["last_active"], r["created"], r["id"]), reverse=True)
        return rows

    def delete(self, sid: str) -> None:
        rec = self.get(sid)
        BACKEND.stop(sid)
        terminate_viewers(sid)
        t = transcript_path(claude_home(), Path(rec["cwd"]), sid)
        try:
            if not stat.S_ISDIR(os.lstat(t).st_mode):
                os.unlink(t)                    # a symlink is unlinked, never followed
        except OSError:
            pass
        self._path(sid).unlink(missing_ok=True)


# ---------------------------------------------------------------- actions (the dashboard calls these)
_MUTEX = threading.Lock()


def _flag(telegram) -> bool:
    if telegram is not None and not isinstance(telegram, bool):
        raise ValueError("telegram must be true or false")
    return telegram is True


def _telegram_free(st: Store, sid: str) -> None:
    """Only one session may be live on Telegram at a time (one bot, one poller, one allowlist)."""
    other = st.live_telegram(exclude=sid)
    if other:
        raise Conflict(f"another session is already live on Telegram ({other.get('title') or other['id'][:8]}); stop it first")


def start_new(cwd_raw, telegram=None) -> dict:
    """telegram=True starts the session with the Telegram channel plugin (`--channels`)."""
    cwd = validate_cwd(cwd_raw if cwd_raw else str(default_cwd()))
    live = _flag(telegram)
    with _MUTEX:
        st = Store()
        if live:
            _telegram_free(st, "")
        rec = st.create(cwd, live)
        try:
            BACKEND.start(rec, resume=False)
        except Exception:
            st._path(rec["id"]).unlink(missing_ok=True)
            raise
        return st.row(rec)


def resume(sid: str, telegram=None) -> dict:
    """Restart a stopped session. telegram=None keeps the record's flag; True/False sets it (persisted)
    before the start, so a session can be moved on or off Telegram by stop + resume."""
    want = None if telegram is None else _flag(telegram)
    with _MUTEX:
        st = Store()
        rec = st.get(sid)
        if BACKEND.alive(sid):
            raise Conflict("the session is already running")
        validate_cwd(rec["cwd"])
        live = is_telegram(rec) if want is None else want
        if live:
            _telegram_free(st, sid)
        if live != is_telegram(rec):
            rec["telegram"] = live
            st._write(rec)
        BACKEND.start(rec, resume=True)
        return st.row(rec)


def stop(sid: str) -> dict:
    with _MUTEX:
        st = Store()
        rec = st.get(sid)
        BACKEND.stop(sid)
        terminate_viewers(sid)
        return st.row(rec)


def delete(sid: str) -> dict:
    with _MUTEX:
        Store().delete(sid)
        return {"ok": True, "id": sid}


class Conflict(Exception):
    pass


# ---------------------------------------------------------------- viewers (one PTY + pump per WebSocket)
_VIEWERS: dict[str, set] = {}
_VLOCK = threading.Lock()


def viewer_count(sid: str) -> int:
    with _VLOCK:
        return len(_VIEWERS.get(sid, ()))


def terminate_viewers(sid: str) -> None:
    with _VLOCK:
        vs = list(_VIEWERS.get(sid, ()))
    for v in vs:
        v.terminate()


class Viewer:
    """Runs `argv` (normally `tmux attach`) on a fresh PTY and bridges it to one WebSocket:
    binary frames in -> PTY input; PTY output -> binary frames out; text frames carry JSON control
    messages ({"type": "resize", "cols", "rows"}); the viewer sends {"type": "exit"} when the PTY's
    process ends, then closes. Ends when the socket closes, the peer idles out or the session stops."""

    def __init__(self, sid: str, argv: list[str], env: dict[str, str]):
        self.sid, self.argv, self.env = sid, argv, env
        self.pid = 0
        self.fd = -1                    # PTY master
        self._slave = -1                # kept open here so the master never reports EIO early
        self._lock = threading.Lock()
        self._done = False
        self._quiet = False             # True: the viewer's socket is gone, nothing to tell it
        self._exited = False
        self._wake_r, self._wake_w = os.pipe()        # terminate() pokes the pump out of its select
        self.ws: wsmod.WebSocket | None = None

    def run(self, ws: wsmod.WebSocket) -> None:
        self.ws = ws
        self.fd, self._slave = os.openpty()
        tty = os.ttyname(self._slave)
        # posix_spawn, not fork: this process is multi-threaded. setsid + opening the slave by path as fd 0
        # makes it the child's controlling terminal (so SIGWINCH/SIGHUP reach it). Python fds are
        # close-on-exec, so the child sees nothing of the dashboard but the PTY.
        self.pid = os.posix_spawn(
            self.argv[0], self.argv, self.env, setsid=True, setsigdef=[signal.SIGPIPE, signal.SIGXFSZ],
            file_actions=[(os.POSIX_SPAWN_OPEN, 0, tty, os.O_RDWR, 0), (os.POSIX_SPAWN_DUP2, 0, 1),
                          (os.POSIX_SPAWN_DUP2, 0, 2)])
        with _VLOCK:
            _VIEWERS.setdefault(self.sid, set()).add(self)
        pump = threading.Thread(target=self._pump, name=f"pty-{self.sid[:8]}", daemon=True)
        pump.start()
        try:
            while True:
                kind, data = ws.recv()
                if kind == "binary":
                    self._write(data)
                else:
                    self._control(data)
        except wsmod.Closed:
            pass
        finally:
            self.terminate(quiet=True)
            with _VLOCK:
                _VIEWERS.get(self.sid, set()).discard(self)
                if not _VIEWERS.get(self.sid):
                    _VIEWERS.pop(self.sid, None)
            pump.join(5)
            self._reaped(0)
            with self._lock:
                for fd in (self._slave, self._wake_r, self._wake_w):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._slave = self._wake_r = self._wake_w = -1

    def _write(self, data: bytes) -> None:
        with self._lock:                 # the lock also keeps the fd from being closed under the write
            while data and self.fd >= 0:
                try:
                    n = os.write(self.fd, data)
                except OSError:
                    return
                data = data[n:]

    def _control(self, data: bytes) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("type") == "resize":
            try:
                self.resize(int(msg.get("cols", 0)), int(msg.get("rows", 0)))
            except (TypeError, ValueError, OSError):
                pass

    def resize(self, cols: int, rows: int) -> None:
        cols = max(COLS[0], min(cols, COLS[1]))
        rows = max(ROWS[0], min(rows, ROWS[1]))
        with self._lock:
            if self.fd >= 0:
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _pump(self) -> None:
        """PTY output -> WebSocket. Ends at EOF/EIO (the process ended) or when the socket is gone;
        it owns closing the PTY fd, so no other thread can close it under a read."""
        ws = self.ws
        fd = self.fd
        try:
            while not self._done:
                try:
                    r, _, _ = select.select([fd, self._wake_r], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if self._wake_r in r:
                    break
                if not r:
                    if self._reaped(0):                          # process ended: drain what is left, then stop
                        os.set_blocking(fd, False)
                        while True:
                            try:
                                chunk = os.read(fd, PTY_CHUNK)
                            except OSError:
                                break
                            if not chunk:
                                break
                            ws.send_binary(chunk)
                        break
                    continue
                try:
                    chunk = os.read(fd, PTY_CHUNK)
                except OSError:                                   # EIO: hung up
                    break
                if not chunk:
                    break
                ws.send_binary(chunk)
            if not self._quiet:
                try:
                    ws.send_text(json.dumps({"type": "exit"}))
                except wsmod.Closed:
                    pass
                ws.close(1000, "exit")
        except wsmod.Closed:
            pass
        finally:
            with self._lock:
                self.fd = -1
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                ws.sock.shutdown(socket.SHUT_RD)                  # wake the reader; the handler closes the socket
            except OSError:
                pass

    def _reaped(self, wait: float) -> bool:
        """True once the child has been waited for (polls up to `wait` seconds)."""
        deadline = time.monotonic() + wait
        while True:
            with self._lock:
                if self._exited:
                    return True
                try:
                    done = os.waitpid(self.pid, os.WNOHANG)[0] != 0
                except ChildProcessError:
                    done = True
                if done:
                    self._exited = True
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def terminate(self, quiet: bool = False) -> None:
        """End the attach process (idempotent; safe from any thread); the pump then finishes and, unless
        quiet (the viewer's socket is already gone), tells the browser {"type": "exit"} and closes."""
        with self._lock:
            if self._done:
                return
            self._done = True
            self._quiet = quiet
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        for sig in (signal.SIGHUP, signal.SIGKILL):
            if self._reaped(0):
                return
            try:
                os.killpg(self.pid, sig)                           # the child is its own session/group leader
            except OSError:
                return
            if self._reaped(1.0):
                return
