"""Session manager backend: persistent Claude Code terminals for the dashboard (0.5 step 10).

Design (0.5.2, step 17, requirement 1: no tmux; the claude-harness model, docs/sessions-pty.md): each
session is one `claude` process on a pseudo-terminal OWNED BY THE DASHBOARD (Proc), started with a clean
allowlisted environment (no dashboard environment, no GitHub/Telegram secrets, no
`--dangerously-skip-permissions`, normal permission prompts). A drain thread reads the PTY into a bounded
ring buffer (scrollback) and fans the bytes out to every attached WebSocket viewer (hands/ws.py). A viewer
is just a set member: closing the browser detaches it and the process runs on; reopening attaches a new
viewer, which first receives the ring buffer and then live output. The session lives as long as the
dashboard: when the dashboard restarts, every record shows as stopped and is offered for Resume
(`claude --resume <uuid>` brings the conversation back).

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
import shutil
import signal
import socket
import stat
import struct
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
# variables are deliberately not passed: the allowlist is the whole environment and stays token-free.
PASSTHROUGH = ("CLAUDE_CONFIG_DIR",)
SCROLLBACK = 256 * 1024                 # bytes of PTY output kept per session for viewers that attach later
DEFAULT_COLS, DEFAULT_ROWS = 120, 40    # until the first viewer sends its size
# The one channel a "live" session may run (step 12): Claude Code's official Telegram plugin, whose bot
# token lives in ~/.claude/channels/telegram/.env and is read by the plugin inside the session, never here.
# The plugin is DISABLED in the user's settings (requirement 8: the ops Claude and every other claude on
# this host must never load it); a live session enables it for itself with --settings (step 17), which
# merges over the user settings. Verified on Claude Code 2.1.292: with that flag the debug log shows the
# plugin enabled, its skills loaded and its MCP server started; without it, nothing of the plugin loads.
TELEGRAM_PLUGIN = "telegram@claude-plugins-official"
TELEGRAM_CHANNEL = f"plugin:{TELEGRAM_PLUGIN}"
TELEGRAM_SETTINGS = json.dumps({"enabledPlugins": {TELEGRAM_PLUGIN: True}}, separators=(",", ":"))


# ---------------------------------------------------------------- validation and pure helpers
def new_id() -> str:
    return str(uuid.uuid4())


def validate_id(sid) -> str:
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        raise ValueError("bad session id")
    return sid


def default_cwd() -> Path:
    """New sessions start in the home folder (requirement 9), never in ~/ops (the ops Claude workspace)."""
    return HOME


def validate_cwd(raw, home: Path | None = None) -> Path:
    """The home folder itself or an existing directory under it (never a hidden top-level folder such as
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
    if p != home and home not in p.parents:
        raise ValueError("working folder must be your home folder or a folder inside it")
    if p != home and p.relative_to(home).parts[0].startswith("."):
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
    Claude's own login and settings; CLAUDE_CONFIG_DIR passes through if the dashboard has one.
    posix_spawn passes exactly this dict: nothing is inherited."""
    env = _base_env()
    for k in PASSTHROUGH:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def is_telegram(rec: dict) -> bool:
    """The record's live-on-Telegram flag; only the JSON boolean true counts."""
    return rec.get("telegram") is True


def claude_argv(sid: str, resume: bool, telegram: bool = False) -> list[str]:
    """Default permission mode, the user's normal settings; our uuid is Claude's session id. A live
    session enables the Telegram plugin for itself (--settings) and runs it as its channel (--channels);
    nothing else, and no permission flags ever."""
    argv = [claude_exe(), "--resume" if resume else "--session-id", validate_id(sid)]
    if telegram is True:
        argv += ["--settings", TELEGRAM_SETTINGS, "--channels", TELEGRAM_CHANNEL]
    return argv


def spawn_argv(cwd: str, argv: list[str]) -> list[str]:
    """The exact argv posix_spawn runs: /bin/sh changes into the working folder and execs the command, so the
    multi-threaded dashboard never forks Python. $1 is the folder, the rest the command (no shell parsing of
    either)."""
    return ["/bin/sh", "-c", 'cd -- "$1" && shift && exec "$@"', "hands-session", cwd, *argv]


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


# ---------------------------------------------------------------- PTY backend (step 17)
class Proc:
    """One running session: a child on a PTY the dashboard owns, its scrollback and its viewers.

    The drain thread is the only reader of the master fd and the only writer of the ring buffer; it fans
    every chunk out to the attached viewers and, when the child has exited, tells them {"type": "exit"}
    and closes them. Input and resizes come from any viewer (last resize wins). Nothing here forks:
    posix_spawn with setsid + opening the slave by path as fd 0 gives the child a controlling terminal
    (SIGWINCH/SIGHUP reach it), and Python fds are close-on-exec so it sees nothing else."""

    def __init__(self, sid: str, argv: list[str], env: dict[str, str], cwd: str):
        self.sid, self.argv, self.cwd = sid, list(argv), cwd
        self.started = time.time()
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._viewers: set["Viewer"] = set()
        self._exited = False
        self._status: int | None = None
        self.cols, self.rows = DEFAULT_COLS, DEFAULT_ROWS
        self.fd, self._slave = os.openpty()
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
            tty = os.ttyname(self._slave)
            self.pid = os.posix_spawn(
                "/bin/sh", spawn_argv(cwd, argv), env, setsid=True, setsigdef=[signal.SIGPIPE, signal.SIGXFSZ],
                file_actions=[(os.POSIX_SPAWN_OPEN, 0, tty, os.O_RDWR, 0), (os.POSIX_SPAWN_DUP2, 0, 1),
                              (os.POSIX_SPAWN_DUP2, 0, 2)])
        except BaseException:
            for fd in (self.fd, self._slave):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._drain, name=f"pty-{sid[:8]}", daemon=True)
        self._thread.start()

    # -- state
    def alive(self) -> bool:
        return not self._reaped(0)

    def _reaped(self, wait: float) -> bool:
        deadline = time.monotonic() + wait
        while True:
            with self._lock:
                if self._exited:
                    return True
                try:
                    pid, status = os.waitpid(self.pid, os.WNOHANG)
                    done = pid != 0
                except ChildProcessError:
                    done, status = True, None
                if done:
                    self._exited, self._status = True, status
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def viewer_count(self) -> int:
        with self._lock:
            return len(self._viewers)

    # -- viewers
    def attach(self, v: "Viewer") -> bytes:
        """Add a viewer; returns the scrollback it should replay first."""
        with self._lock:
            self._viewers.add(v)
            return bytes(self._buf)

    def detach(self, v: "Viewer") -> None:
        with self._lock:
            self._viewers.discard(v)

    # -- input
    def write(self, data: bytes) -> None:
        with self._lock:
            while data and self.fd >= 0:
                try:
                    n = os.write(self.fd, data)
                except OSError:
                    return
                data = data[n:]

    def resize(self, cols: int, rows: int, force: bool = False) -> None:
        """Set the terminal size (clamped). `force` makes a same-size request still raise SIGWINCH (one
        column smaller, then back), so a TUI repaints for a viewer that just attached."""
        cols = max(COLS[0], min(cols, COLS[1]))
        rows = max(ROWS[0], min(rows, ROWS[1]))
        with self._lock:
            if self.fd < 0:
                return
            if force and (cols, rows) == (self.cols, self.rows):
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, max(COLS[0], cols - 1), 0, 0))
            self.cols, self.rows = cols, rows
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # -- output
    def _fanout(self, chunk: bytes) -> None:
        with self._lock:
            self._buf += chunk
            if len(self._buf) > SCROLLBACK:
                del self._buf[:len(self._buf) - SCROLLBACK]
            vs = list(self._viewers)
        for v in vs:
            v.send(chunk)

    def _drain(self) -> None:
        fd = self.fd
        try:
            while True:
                try:
                    r, _, _ = select.select([fd, self._wake_r], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if self._wake_r in r:                                 # kill(): stop reading, the child is going
                    break
                if not r:
                    if self._reaped(0):                               # exited: take what is left, then stop
                        os.set_blocking(fd, False)
                        while True:
                            try:
                                chunk = os.read(fd, PTY_CHUNK)
                            except OSError:
                                break
                            if not chunk:
                                break
                            self._fanout(chunk)
                        break
                    continue
                try:
                    chunk = os.read(fd, PTY_CHUNK)
                except OSError:                                       # EIO: the terminal hung up
                    break
                if not chunk:
                    break
                self._fanout(chunk)
        finally:
            self._reaped(2.0)
            with self._lock:
                self.fd = -1
                for x in (fd, self._slave, self._wake_r, self._wake_w):
                    try:
                        os.close(x)
                    except OSError:
                        pass
                self._slave = self._wake_r = self._wake_w = -1
                vs = list(self._viewers)
                self._viewers.clear()
            for v in vs:
                v.ended()

    def kill(self) -> None:
        """End the session: SIGHUP (what a closed terminal sends), then SIGKILL. Idempotent."""
        with self._lock:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass
        for sig, wait in ((signal.SIGHUP, 1.5), (signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
            if self._reaped(0):
                break
            try:
                os.killpg(self.pid, sig)                              # the child is its own session/group leader
            except OSError:
                break
            if self._reaped(wait):
                break
        self._thread.join(5)


class PtyBackend:
    """The running sessions of this dashboard process, by id."""

    def __init__(self):
        self._procs: dict[str, Proc] = {}
        self._lock = threading.Lock()

    def start(self, rec: dict, resume: bool) -> None:
        argv = claude_argv(rec["id"], resume, is_telegram(rec))
        self.launch(rec["id"], argv, session_env(), rec["cwd"])

    def launch(self, sid: str, argv: list[str], env: dict[str, str], cwd: str) -> Proc:
        validate_id(sid)
        with self._lock:
            old = self._procs.get(sid)
            if old is not None and old.alive():
                raise Conflict("the session is already running")
            try:
                proc = Proc(sid, argv, env, cwd)
            except OSError as e:
                raise ValueError(f"could not start the session: {e}")
            self._procs[sid] = proc
            return proc

    def proc(self, sid: str) -> Proc | None:
        with self._lock:
            p = self._procs.get(sid)
        return p if p is not None and p.alive() else None

    def alive(self, sid: str) -> bool:
        return self.proc(sid) is not None

    def viewers(self, sid: str) -> int:
        p = self.proc(sid)
        return p.viewer_count() if p else 0

    def stop(self, sid: str) -> None:
        with self._lock:
            p = self._procs.pop(sid, None)
        if p is not None:
            p.kill()

    def shutdown(self) -> None:
        """Dashboard exit: end every session (they are offered for resume after the restart)."""
        with self._lock:
            procs, self._procs = list(self._procs.values()), {}
        for p in procs:
            p.kill()


BACKEND = PtyBackend()


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
                "live": BACKEND.alive(sid), "viewers": BACKEND.viewers(sid), "transcript": has_transcript,
                "telegram": is_telegram(rec)}

    def listing(self) -> list[dict]:
        rows = [self.row(r) for r in self.records()]
        rows.sort(key=lambda r: (r["last_active"], r["created"], r["id"]), reverse=True)
        return rows

    def delete(self, sid: str) -> None:
        rec = self.get(sid)
        BACKEND.stop(sid)
        t = transcript_path(claude_home(), Path(rec["cwd"]), sid)
        try:
            if not stat.S_ISDIR(os.lstat(t).st_mode):
                os.unlink(t)                    # a symlink is unlinked, never followed
        except OSError:
            pass
        sub = t.with_suffix("")                  # Claude Code keeps subagent transcripts in <uuid>/
        try:
            if stat.S_ISDIR(os.lstat(sub).st_mode):      # a symlink named like it is left alone
                shutil.rmtree(sub)
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
        return st.row(rec)


def delete(sid: str) -> dict:
    with _MUTEX:
        Store().delete(sid)
        return {"ok": True, "id": sid}


class Conflict(Exception):
    pass


# ---------------------------------------------------------------- viewers (one WebSocket attached to a Proc)
class Viewer:
    """Bridges one WebSocket to a running session: the scrollback first, then live output as it arrives
    (sent by the session's drain thread); binary frames in -> PTY input; text frames carry JSON control
    messages ({"type": "resize", "cols", "rows"}). When the session ends the viewer gets {"type": "exit"}
    and is closed. Detaching (socket closed, idle timeout) leaves the session running."""

    def __init__(self, proc: Proc):
        self.proc = proc
        self.ws: wsmod.WebSocket | None = None
        self._first_resize = True
        self._gone = False

    def run(self, ws: wsmod.WebSocket) -> None:
        self.ws = ws
        snapshot = self.proc.attach(self)
        try:
            if snapshot:
                ws.send_binary(snapshot)
            if not self.proc.alive():                               # ended between the 101 and the attach
                self.ended()
            while True:
                kind, data = ws.recv()
                if kind == "binary":
                    self.proc.write(data)
                else:
                    self._control(data)
        except wsmod.Closed:
            pass
        finally:
            self._gone = True
            self.proc.detach(self)

    def _control(self, data: bytes) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("type") == "resize":
            try:
                self.proc.resize(int(msg.get("cols", 0)), int(msg.get("rows", 0)), force=self._first_resize)
            except (TypeError, ValueError, OSError):
                pass
            self._first_resize = False

    # called by the session's drain thread
    def send(self, chunk: bytes) -> None:
        ws = self.ws
        if ws is None or self._gone:
            return
        try:
            ws.send_binary(chunk)
        except (wsmod.Closed, OSError):
            self._gone = True
            self.proc.detach(self)

    def ended(self) -> None:
        """The session's process is gone: tell the browser, close, and wake the reader so the handler returns."""
        ws = self.ws
        if ws is None or self._gone:
            return
        self._gone = True
        try:
            ws.send_text(json.dumps({"type": "exit"}))
        except (wsmod.Closed, OSError):
            pass
        try:
            ws.close(1000, "exit")
        except (wsmod.Closed, OSError):
            pass
        try:
            ws.sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass
