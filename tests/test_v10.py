"""0.5 step 10: Session manager backend (hands/ws.py + hands/sessions.py + dashboard routes). No docker or Claude."""
import base64
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401  (removes every mkdtemp dir after each test)
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import config as cfgmod, dashboard, sessions, ws  # noqa: E402
from test_v04 import Dash  # noqa: E402

SECRETS = {"GH_TOKEN": "ghp_secret1", "GITHUB_TOKEN": "ghp_secret2", "TG_TOKEN": "123456:tgsecret", "TG_CHAT_ID": "424242",
           "ANTHROPIC_API_KEY": "sk-ant-secret3", "AWS_SECRET_ACCESS_KEY": "awssecret4", "HANDS_SECRET": "handssecret5"}


def _mask(payload: bytes, mask: bytes) -> bytes:
    return bytes(b ^ mask[i & 3] for i, b in enumerate(payload))


def frame(op: int, payload: bytes = b"", fin: bool = True, masked: bool = True, rsv: int = 0) -> bytes:
    """Build a client frame (masked by default, as a browser would)."""
    head = bytes([(0x80 if fin else 0) | (rsv << 4) | op])
    n = len(payload)
    m = 0x80 if masked else 0
    if n < 126:
        head += bytes([m | n])
    elif n < 65536:
        head += bytes([m | 126]) + struct.pack(">H", n)
    else:
        head += bytes([m | 127]) + struct.pack(">Q", n)
    if masked:
        mask = b"\x12\x34\x56\x78"
        return head + mask + _mask(payload, mask)
    return head + payload


class WSClient:
    """Raw WebSocket client: hand-written handshake so every header is under the test's control."""

    def __init__(self, port: int, path: str, headers: dict, timeout: float = 10.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.f = self.sock.makefile("rb")
        req = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.sock.sendall(req.encode())
        line = self.f.readline().decode()
        self.status = int(line.split()[1]) if line.startswith("HTTP/") else 0
        self.headers = {}
        while True:
            h = self.f.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            k, _, v = h.decode().partition(":")
            self.headers[k.strip().lower()] = v.strip()
        if self.status != 101:
            n = int(self.headers.get("content-length") or 0)
            self.body = self.f.read(n) if n else b""

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv_frame(self):
        """-> (opcode, fin, payload) of one server frame (server frames are never masked)."""
        b1, b2 = self.f.read(2)
        op, fin, n = b1 & 0x0F, bool(b1 & 0x80), b2 & 0x7F
        assert not b2 & 0x80, "server frame must not be masked"
        if n == 126:
            n = struct.unpack(">H", self.f.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.f.read(8))[0]
        return op, fin, self.f.read(n)

    def recv_until(self, needle: bytes, limit: float = 10.0) -> bytes:
        """Collect binary frames until needle appears; control frames are answered/ignored."""
        got, deadline = b"", time.time() + limit
        while needle not in got and time.time() < deadline:
            op, _, payload = self.recv_frame()
            if op == 2:
                got += payload
            elif op == 9:
                self.send(frame(10, payload))
            elif op == 8:
                raise AssertionError(f"closed: {payload!r} before {needle!r}; got {got!r}")
        return got

    def close(self):
        for x in (self.f, self.sock):            # both: the file object keeps the socket alive
            try:
                x.close()
            except OSError:
                pass


def ws_headers(d: Dash, cookie=True, origin=None, key="dGhlIHNhbXBsZSBub25jZQ==", version="13"):
    h = {"Host": f"127.0.0.1:{d.port}", "Upgrade": "websocket", "Connection": "Upgrade",
         "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": version,
         "Origin": origin if origin is not None else f"http://127.0.0.1:{d.port}"}
    if cookie:
        h["Cookie"] = f"{dashboard.COOKIE}={d.sid}"
    return h


class FakeBackend(sessions.PtyBackend):
    """The real PTY backend, but the session process is a plain shell command instead of claude; remembers
    the claude argv/env/cwd that would have run."""

    def __init__(self):
        super().__init__()
        self.started: list[tuple[str, list[str], dict, str]] = []
        self.attach = ["/bin/sh", "-c", "echo READY; exec cat"]

    def start(self, rec: dict, resume: bool) -> None:
        argv, env = sessions.claude_argv(rec["id"], resume, sessions.is_telegram(rec)), sessions.session_env()
        self.started.append((rec["id"], argv, env, rec["cwd"]))
        self.launch(rec["id"], list(self.attach), {"PATH": "/usr/bin:/bin", "TERM": "dumb"}, "/")

    @property
    def alive_ids(self) -> set[str]:
        return {sid for sid in list(self._procs) if self.alive(sid)}

    def close(self) -> None:
        self.shutdown()


# ---------------------------------------------------------------- pure functions
class TestValidation(unittest.TestCase):
    def test_id_regex_strict_uuid(self):
        good = "1b4e28ba-2fa1-4d3b-8b2c-0e1f2a3b4c5d"
        self.assertEqual(sessions.validate_id(good), good)
        for _ in range(20):
            fresh = sessions.new_id()
            self.assertEqual(sessions.validate_id(fresh), fresh)
        for bad in ("", "x", good.upper(), good + "a", good[:-1], good.replace("-", "/", 1), "../" + good[3:],
                    "1b4e28ba-2fa1-1d3b-8b2c-0e1f2a3b4c5d", "1b4e28ba-2fa1-4d3b-7b2c-0e1f2a3b4c5d",
                    "1b4e28ba_2fa1_4d3b_8b2c_0e1f2a3b4c5d", good + "\n", 5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                sessions.validate_id(bad)

    def test_cwd_validation(self):
        home = Path(tempfile.mkdtemp(prefix="hands-home-"))
        (home / "ops").mkdir()
        (home / "projects" / "x").mkdir(parents=True)
        (home / ".config" / "hands").mkdir(parents=True)
        (home / ".ssh").mkdir()
        (home / "afile").write_text("x")
        os.symlink("/etc", home / "etc-link")
        os.symlink(home / "ops", home / "ops-link")
        self.assertEqual(sessions.validate_cwd(str(home / "ops"), home), home / "ops")
        self.assertEqual(sessions.validate_cwd(str(home / "projects" / "x") + "/", home), home / "projects" / "x")
        self.assertEqual(sessions.validate_cwd("~/ops", home), home / "ops")
        self.assertEqual(sessions.validate_cwd(str(home / "ops-link"), home), home / "ops")      # resolves inside: ok
        self.assertEqual(sessions.validate_cwd(str(home), home), home)                           # home itself (0.5.2 default)
        self.assertEqual(sessions.validate_cwd(str(home) + "/", home), home)
        self.assertEqual(sessions.validate_cwd("~", home), home)
        for bad in ("", "/", "/etc", "/tmp", str(home / "nope"), str(home / "afile"),
                    str(home / ".config" / "hands"), str(home / ".ssh"), str(home / ".config"), str(home / "etc-link"),
                    str(home / "ops" / ".." / ".ssh"), "ops", "x" * 5000, str(home / "ops") + "\0", 7, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                sessions.validate_cwd(bad, home)

    def test_argv_default_permissions_and_resume(self):
        sid = sessions.new_id()
        argv = sessions.claude_argv(sid, resume=False)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("--permission-mode", argv)
        self.assertEqual(argv[1:], ["--session-id", sid])
        self.assertTrue(os.path.isabs(argv[0]))
        argv = sessions.claude_argv(sid, resume=True)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[1:], ["--resume", sid])
        # the exact argv posix_spawn runs (sh changes folder, then execs claude; no shell parsing of either)
        for resume in (False, True):
            run = sessions.spawn_argv("/home/x/my folder", sessions.claude_argv(sid, resume))
            self.assertEqual(run[:4], ["/bin/sh", "-c", 'cd -- "$1" && shift && exec "$@"', "hands-session"])
            self.assertEqual(run[4], "/home/x/my folder")
            self.assertEqual(run[5:], sessions.claude_argv(sid, resume))
            self.assertNotIn("dangerously", " ".join(run))

    def test_env_allowlist(self):
        old = dict(os.environ)
        try:
            os.environ.update(SECRETS)
            os.environ.update({"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%1", "CLAUDECODE": "1",
                               "CLAUDE_CODE_ENTRYPOINT": "cli", "COLORTERM": "truecolor", "TMUX_TMPDIR": "/tmp/tt",
                               "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x", "CLAUDE_CONFIG_DIR": "/home/x/.claude"})
            env = sessions.session_env()
            for k in SECRETS:
                self.assertNotIn(k, env, k)
            for k in ("TMUX", "TMUX_PANE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "COLORTERM", "HANDS_SECRET"):
                self.assertNotIn(k, env, k)
            self.assertEqual(env["TERM"], "xterm-256color")
            self.assertEqual(env["HOME"], str(Path.home()))
            self.assertIn("/.local/bin", env["PATH"])
            self.assertNotIn("linuxbrew", env["PATH"])
            # a fixed PATH built from HOME, never the dashboard's (~/.bun/bin is on it since step 12: the
            # Telegram channel plugin is `bun run ...`)
            h = Path.home()
            self.assertEqual(env["PATH"], f"{h}/.local/bin:{h}/.bun/bin:/usr/local/bin:/usr/bin:/bin")
            # Claude's login is the credentials file under HOME; a token variable has no place in the session
            # command line, so it is not passed. CLAUDE_CONFIG_DIR (a path) passes through.
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/x/.claude")
            self.assertEqual(set(env), {"PATH", "HOME", "TERM", "LANG", "USER", "SHELL", "CLAUDE_CONFIG_DIR"})
            self.assertNotIn("TMUX_TMPDIR", env)                      # no tmux anywhere on the session path (0.5.2)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_transcript_path_and_title(self):
        root = Path(tempfile.mkdtemp(prefix="hands-cl-"))
        sid = sessions.new_id()
        p = sessions.transcript_path(root, Path("/home/builder/ops"), sid)
        self.assertEqual(p, root / "projects" / "-home-builder-ops" / f"{sid}.jsonl")
        self.assertEqual(sessions.transcript_path(root, Path("/home/b/.config/x y"), sid).parent.name, "-home-b--config-x-y")
        p.parent.mkdir(parents=True)
        lines = [{"type": "summary", "summary": "nope"},
                 {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-stdout></local-command-stdout>"}},
                 {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
                 {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "  Fix the login bug\nmore"}]}},
                 {"type": "user", "message": {"role": "user", "content": "second prompt"}}]
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        self.assertEqual(sessions.read_title(p), "Fix the login bug")
        p.write_text(json.dumps({"type": "user", "message": {"content": "x" * 500}}) + "\n")
        self.assertEqual(len(sessions.read_title(p)), 80)
        p.write_text("")
        self.assertIsNone(sessions.read_title(p))
        self.assertIsNone(sessions.read_title(p.with_name("missing.jsonl")))
        os.symlink("/etc/hostname", p.with_name("link.jsonl"))
        self.assertIsNone(sessions.read_title(p.with_name("link.jsonl")))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.d = Dash()
        self.home = Path(tempfile.mkdtemp(prefix="hands-h-"))
        (self.home / "ops").mkdir()
        self.claude = Path(tempfile.mkdtemp(prefix="hands-cl-"))
        self.old_env = dict(os.environ)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude)
        self.old_home = sessions.HOME
        sessions.HOME = self.home
        self.old_backend = sessions.BACKEND
        sessions.BACKEND = self.fake = FakeBackend()

    def tearDown(self):
        self.d.close()
        self.fake.close()
        sessions.HOME = self.old_home
        sessions.BACKEND = self.old_backend
        os.environ.clear()
        os.environ.update(self.old_env)

    def test_records_private_and_listing(self):
        st = sessions.Store()
        rec = st.create(self.home / "ops")
        self.assertEqual(sessions.validate_id(rec["id"]), rec["id"])
        f = sessions.sessions_dir() / f"{rec['id']}.json"
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(f.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(st.get(rec["id"])["cwd"], str(self.home / "ops"))
        with self.assertRaises(LookupError):
            st.get(sessions.new_id())
        with self.assertRaises(ValueError):
            st.get("../x")
        (sessions.sessions_dir() / "junk.json").write_text("{}")
        (sessions.sessions_dir() / f"{sessions.new_id()}.json").write_text("not json")
        rows = st.listing()
        self.assertEqual([r["id"] for r in rows], [rec["id"]])
        self.assertEqual(rows[0]["title"], "")
        self.assertFalse(rows[0]["live"])
        t = sessions.transcript_path(self.claude, self.home / "ops", rec["id"])
        t.parent.mkdir(parents=True)
        t.write_text(json.dumps({"type": "user", "message": {"content": "hello there"}}) + "\n")
        rows = st.listing()
        self.assertEqual(rows[0]["title"], "hello there")
        self.assertTrue(rows[0]["transcript"])
        self.assertEqual(st.get(rec["id"])["title"], "hello there")        # cached in the record
        # delete removes exactly the record and the transcript, nothing else in the project dir
        other = t.with_name(f"{sessions.new_id()}.jsonl")
        other.write_text("{}\n")
        st.delete(rec["id"])
        self.assertFalse(f.exists())
        self.assertFalse(t.exists())
        self.assertTrue(other.exists())
        self.assertEqual(st.listing(), [])

    def test_delete_never_follows_symlinks(self):
        st = sessions.Store()
        rec = st.create(self.home / "ops")
        t = sessions.transcript_path(self.claude, self.home / "ops", rec["id"])
        t.parent.mkdir(parents=True)
        target = self.claude / "precious"
        target.write_text("keep")
        os.symlink(target, t)
        st.delete(rec["id"])
        self.assertTrue(target.exists())
        self.assertFalse(t.is_symlink())


# ---------------------------------------------------------------- WebSocket protocol (hands/ws.py)
class TestWSProtocol(unittest.TestCase):
    """Drives ws.WebSocket over a socketpair, the way the dashboard does after a 101."""

    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.a.settimeout(5)
        self.b.settimeout(5)
        self.srv_rfile = self.b.makefile("rb")
        self.srv = ws.WebSocket(self.b, self.srv_rfile, max_frame=1024, max_message=2048,
                                ping_interval=0.3, idle_timeout=1.0)
        self.cf = self.a.makefile("rb")

    def tearDown(self):
        for s in (self.a, self.b):
            try:
                s.close()
            except OSError:
                pass

    def srv_frame(self):
        b1, b2 = self.cf.read(2)
        n = b2 & 0x7F
        self.assertFalse(b2 & 0x80)
        if n == 126:
            n = struct.unpack(">H", self.cf.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.cf.read(8))[0]
        return b1 & 0x0F, bool(b1 & 0x80), self.cf.read(n)

    def expect_close(self, code: int):
        op, fin, payload = self.srv_frame()
        self.assertEqual(op, 8)
        self.assertEqual(struct.unpack(">H", payload[:2])[0], code)

    def test_accept_key_rfc_example(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_handshake_validation(self):
        ok = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13",
              "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
        self.assertIsNone(ws.handshake_error(ok))
        for k, v in (("Upgrade", "h2c"), ("Upgrade", ""), ("Connection", "keep-alive"), ("Sec-WebSocket-Version", "8"),
                     ("Sec-WebSocket-Key", "short"), ("Sec-WebSocket-Key", ""), ("Sec-WebSocket-Key", "x" * 24)):
            h = {**ok, k: v}
            self.assertIsNotNone(ws.handshake_error(h), (k, v))
        h = dict(ok)
        del h["Sec-WebSocket-Key"]
        self.assertIsNotNone(ws.handshake_error(h))

    def test_text_binary_roundtrip_and_masking(self):
        self.a.sendall(frame(1, "héllo".encode()))
        self.assertEqual(self.srv.recv(), ("text", "héllo".encode()))
        self.a.sendall(frame(2, bytes(range(256)) * 3))
        self.assertEqual(self.srv.recv(), ("binary", bytes(range(256)) * 3))
        self.srv.send_binary(b"\x1b[0m out")
        self.assertEqual(self.srv_frame(), (2, True, b"\x1b[0m out"))
        self.srv.send_text('{"type":"exit"}')
        self.assertEqual(self.srv_frame(), (1, True, b'{"type":"exit"}'))
        big = b"z" * 70000
        self.srv.send_binary(big)
        self.assertEqual(self.srv_frame(), (2, True, big))

    def test_unmasked_client_frame_rejected(self):
        self.a.sendall(frame(2, b"abc", masked=False))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        self.expect_close(1002)

    def test_rsv_bits_and_unknown_opcode_rejected(self):
        self.a.sendall(frame(2, b"abc", rsv=4))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        self.expect_close(1002)
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        srv = ws.WebSocket(b, b.makefile("rb"))
        a.sendall(frame(3, b"abc"))
        with self.assertRaises(ws.Closed) as cm:
            srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        a.close()
        b.close()

    def test_frame_size_cap(self):
        self.a.sendall(frame(2, b"x" * 1025))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1009)
        self.expect_close(1009)

    def test_message_size_cap_over_fragments(self):
        self.a.sendall(frame(2, b"x" * 1000, fin=False))
        self.a.sendall(frame(0, b"x" * 1000, fin=False))
        self.a.sendall(frame(0, b"x" * 100, fin=True))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1009)
        self.expect_close(1009)

    def test_fragmentation_with_interleaved_ping(self):
        self.a.sendall(frame(1, b"ab", fin=False))
        self.a.sendall(frame(9, b"ping!"))                 # control frame between fragments is legal
        self.a.sendall(frame(0, b"cd", fin=False))
        self.a.sendall(frame(0, b"ef", fin=True))
        self.assertEqual(self.srv.recv(), ("text", b"abcdef"))
        self.assertEqual(self.srv_frame(), (10, True, b"ping!"))
        # continuation without a start, and a new data frame while a message is open: both protocol errors
        self.a.sendall(frame(0, b"zz", fin=True))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        self.expect_close(1002)
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        srv = ws.WebSocket(b, b.makefile("rb"))
        a.sendall(frame(2, b"ab", fin=False))
        a.sendall(frame(2, b"cd", fin=True))
        with self.assertRaises(ws.Closed) as cm:
            srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        a.close()
        b.close()

    def test_control_frames_must_be_small_and_whole(self):
        self.a.sendall(frame(9, b"p" * 126))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        srv = ws.WebSocket(b, b.makefile("rb"))
        a.sendall(frame(9, b"p", fin=False))
        with self.assertRaises(ws.Closed) as cm:
            srv.recv()
        self.assertEqual(cm.exception.code, 1002)
        a.close()
        b.close()

    def test_invalid_utf8_text_rejected(self):
        self.a.sendall(frame(1, b"\xff\xfe"))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1007)
        self.expect_close(1007)

    def test_client_close_is_echoed(self):
        self.a.sendall(frame(8, struct.pack(">H", 1000) + b"bye"))
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1000)
        self.expect_close(1000)
        self.srv.close()                                   # idempotent: nothing more on the wire
        self.srv.send_binary(b"late")
        self.assertTrue(self.srv.closed)
        self.a.settimeout(0.3)
        with self.assertRaises(TimeoutError):
            self.cf.read(1)
        with self.assertRaises(ws.Closed):
            self.srv.recv()

    def test_server_close_then_client_ack(self):
        self.srv.close(1001, "going away")
        op, fin, payload = self.srv_frame()
        self.assertEqual((op, payload), (8, struct.pack(">H", 1001) + b"going away"))
        self.a.sendall(frame(8, struct.pack(">H", 1001)))
        with self.assertRaises(ws.Closed):
            self.srv.recv()

    def test_idle_timeout_pings_then_closes(self):
        # no client frames at all: server pings every ping_interval, closes after idle_timeout
        t0 = time.time()
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1001)
        self.assertGreaterEqual(time.time() - t0, 0.9)
        pings = 0
        while True:
            op, _, _ = self.srv_frame()
            if op == 9:
                pings += 1
            elif op == 8:
                break
        self.assertGreaterEqual(pings, 2)

    def test_pongs_keep_the_connection_alive(self):
        # answer every ping with a pong for 1.6 s (idle_timeout is 1.0), then send data: must still work
        def pong_back():
            end = time.time() + 1.6
            while time.time() < end:
                op, _, payload = self.srv_frame()
                if op == 9:
                    self.a.sendall(frame(10, payload))
            self.a.sendall(frame(2, b"still here"))
        th = threading.Thread(target=pong_back, daemon=True)
        th.start()
        self.assertEqual(self.srv.recv(), ("binary", b"still here"))
        th.join(2)
        self.assertFalse(self.srv.closed)

    def test_half_open_mid_frame_times_out(self):
        self.a.sendall(frame(2, b"x" * 500)[:7])         # header + part of the payload, then silence
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1002)

    def test_peer_gone(self):
        self.cf.close()
        self.a.close()
        with self.assertRaises(ws.Closed) as cm:
            self.srv.recv()
        self.assertEqual(cm.exception.code, 1006)


# ---------------------------------------------------------------- dashboard routes and the upgrade
class TestSessionsAPI(unittest.TestCase):
    def setUp(self):
        self.d = Dash()
        self.home = Path(tempfile.mkdtemp(prefix="hands-h-"))
        (self.home / "ops").mkdir()
        (self.home / "work").mkdir()
        self.claude = Path(tempfile.mkdtemp(prefix="hands-cl-"))
        self.old_env = dict(os.environ)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude)
        os.environ.update(SECRETS)
        self.old_home = sessions.HOME
        sessions.HOME = self.home
        self.old_backend = sessions.BACKEND
        sessions.BACKEND = self.fake = FakeBackend()
        self.old_ping = ws.PING_INTERVAL, ws.IDLE_TIMEOUT
        self.clients: list[WSClient] = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.d.close()
        self.fake.close()
        sessions.HOME = self.old_home
        sessions.BACKEND = self.old_backend
        ws.PING_INTERVAL, ws.IDLE_TIMEOUT = self.old_ping
        os.environ.clear()
        os.environ.update(self.old_env)

    def post(self, path, body=None, **kw):
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        h = {"Host": kw.get("host") or f"127.0.0.1:{self.d.port}", "Cookie": f"{dashboard.COOKIE}={self.d.sid}",
             "Origin": kw.get("origin", f"http://127.0.0.1:{self.d.port}"), "X-Hands": "1"}
        if kw.get("cookie") is False:
            del h["Cookie"]
        if kw.get("xh") is False:
            del h["X-Hands"]
        c.request("POST", path, headers=h, body=json.dumps(body or {}).encode())
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw or b"{}")
        except ValueError:
            return r.status, {"text": raw.decode(errors="replace")}

    def get(self, path, **kw):
        st, _, body = self.d.req("GET", path, **kw)
        return st, json.loads(body or b"{}")

    def new_session(self, cwd=None):
        st, rec = self.post("/api/sessions/new", {"cwd": cwd} if cwd else {})
        self.assertEqual(st, 200, rec)
        return rec

    def ticket(self, sid):
        st, t = self.post(f"/api/sessions/{sid}/ticket")
        self.assertEqual(st, 200, t)
        return t["ticket"]

    def connect(self, path, **kw) -> WSClient:
        c = WSClient(self.d.port, path, ws_headers(self.d, **kw))
        self.clients.append(c)
        return c

    def test_page_assets_served_locally(self):
        # step 11: xterm.js is vendored (no CDN) and the page's scripts/styles are all same-origin files
        st, _, html = self.d.req("GET", "/")
        self.assertEqual(st, 200)
        for ref in (b"/vendor/xterm.min.js", b"/vendor/addon-fit.min.js", b"/vendor/xterm.min.css", b"/sessions.js", b"/app.js"):
            self.assertIn(ref, html)
        self.assertNotIn(b"http://", html)
        self.assertNotIn(b"https://", html)
        for p, ct in (("/vendor/xterm.min.js", "text/javascript"), ("/vendor/addon-fit.min.js", "text/javascript"),
                      ("/vendor/xterm.min.css", "text/css"), ("/sessions.js", "text/javascript")):
            st, hdr, body = self.d.req("GET", p)
            self.assertEqual(st, 200, p)
            self.assertIn(ct, hdr["Content-Type"])
            self.assertGreater(len(body), 1000)
            self.assertIn("default-src 'none'", hdr["Content-Security-Policy"])
        self.assertEqual(self.d.req("GET", "/vendor/../app.js")[0], 404)
        # the Session manager page code carries no colour or font literals: everything comes from the CSS tokens
        js = (ROOT / "hands" / "static" / "sessions.js").read_text()
        import re
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsl\(|font-family\s*:|fontFamily:\s*\"|fontSize:\s*\d", js), [])

    def test_bind_address_is_loopback(self):
        self.assertEqual(self.d.srv.server_address[0], "127.0.0.1")
        self.assertEqual(dashboard.Server.__init__.__code__.co_consts.count("127.0.0.1"), 1)

    def test_new_defaults_to_home_and_validates_cwd(self):
        rec = self.new_session()
        self.assertEqual(rec["cwd"], str(self.home))                   # requirement 9: home, never ~/ops
        self.assertTrue(rec["live"])
        sid, argv, env, cwd = self.fake.started[-1]
        self.assertEqual((sid, cwd), (rec["id"], rec["cwd"]))
        self.assertNotIn("--dangerously-skip-permissions", argv)
        for k in SECRETS:
            self.assertNotIn(k, env)
        rec2 = self.new_session(str(self.home / "work"))
        self.assertEqual(rec2["cwd"], str(self.home / "work"))
        for bad in (str(self.home / ".config"), "/etc", str(self.home / ".ssh"), "relative", str(self.home / "nope")):
            st, err = self.post("/api/sessions/new", {"cwd": bad})
            self.assertEqual(st, 400, bad)
            self.assertIn("folder", err["error"])
        st, rows = self.get("/api/sessions")
        self.assertEqual(st, 200)
        self.assertEqual({r["id"] for r in rows}, {rec["id"], rec2["id"]})
        self.assertEqual(rows[0]["id"], rec2["id"])                  # newest first
        st, info = self.get("/api/sessions/defaults")
        self.assertEqual(info["cwd"], str(self.home))
        self.assertNotIn("/ops", info["cwd"])
        self.assertEqual(info["max_terminals"], sessions.MAX_TERMINALS)

    def test_stop_resume_delete(self):
        rec = self.new_session()
        sid = rec["id"]
        st, r = self.post(f"/api/sessions/{sid}/stop")
        self.assertEqual((st, r["live"]), (200, False))
        self.assertNotIn(sid, self.fake.alive_ids)
        st, r = self.post(f"/api/sessions/{sid}/resume")
        self.assertEqual((st, r["live"]), (200, True))
        self.assertEqual(self.fake.started[-1][1][1:], ["--resume", sid])
        self.assertEqual(self.post(f"/api/sessions/{sid}/resume")[0], 409)       # already running
        st, r = self.post(f"/api/sessions/{sid}/delete")
        self.assertEqual(st, 200)
        self.assertNotIn(sid, self.fake.alive_ids)
        self.assertEqual(self.get("/api/sessions")[1], [])
        for p in (f"/api/sessions/{sid}/resume", f"/api/sessions/{sid}/stop", f"/api/sessions/{sid}/delete",
                  f"/api/sessions/{sid}/ticket", f"/api/sessions/{sessions.new_id()}/stop"):
            self.assertEqual(self.post(p)[0], 404, p)
        for p in ("/api/sessions/../x/stop", "/api/sessions/%s/stop" % sid.upper(), "/api/sessions/x/ticket",
                  f"/api/sessions/{sid}/boom"):
            self.assertEqual(self.post(p)[0], 404, p)

    def test_plan_chat_resume_goes_to_its_project_never_the_host_claude(self):
        # a planning chat (hands.nextproject) must only ever run in its container: Resume in the Session manager
        # relaunches it through the project (POST /api/projects/<p>/plan-chat), not with the dashboard's login
        sid = sessions.new_id()
        sessions.Store().create_plan_chat(sid, "proj", self.home / "work")
        calls = []
        old = dashboard.plan_chat
        dashboard.plan_chat = lambda name: calls.append(name) or {"session_id": sid}
        try:
            st, r = self.post(f"/api/sessions/{sid}/resume")
            self.assertEqual((st, r["id"], r["role"], r["hidden"]), (200, sid, "plan-chat", True))
            self.assertEqual(calls, ["proj"])
            self.assertEqual(self.post(f"/api/sessions/{sid}/resume", {"telegram": True})[0], 400)
        finally:
            dashboard.plan_chat = old
        self.assertEqual(self.fake.started, [], "no host claude was started")
        with self.assertRaises(sessions.PlanChatSession):
            sessions.resume(sid)

    def test_api_unauthenticated_and_csrf(self):
        rec = self.new_session()
        sid = rec["id"]
        self.assertEqual(self.get("/api/sessions", cookie=False)[0], 401)
        self.assertEqual(self.post(f"/api/sessions/{sid}/stop", cookie=False)[0], 401)
        self.assertEqual(self.post("/api/sessions/new", cookie=False)[0], 401)
        self.assertEqual(self.post(f"/api/sessions/{sid}/stop", xh=False)[0], 403)
        self.assertEqual(self.post(f"/api/sessions/{sid}/delete", origin="http://evil.example")[0], 403)
        self.assertEqual(self.post(f"/api/sessions/{sid}/ticket", origin=f"http://localhost:{self.d.port}")[0], 403)
        self.assertEqual(self.post(f"/api/sessions/{sid}/stop", host="localhost")[0], 403)
        self.assertTrue(rec["live"])
        self.assertIn(sid, self.fake.alive_ids)                       # none of those stopped it

    def test_ws_checks_run_before_the_upgrade(self):
        rec = self.new_session()
        sid = rec["id"]
        t = self.ticket(sid)
        path = f"/ws/sessions/{sid}?t={t}"
        self.assertEqual(self.connect(path, cookie=False).status, 401)                       # no session
        self.assertEqual(self.connect(path, origin="http://evil.example").status, 403)       # wrong origin
        self.assertEqual(self.connect(path, origin="").status, 403)                          # no origin
        self.assertEqual(self.connect(path, origin=f"http://localhost:{self.d.port}").status, 403)
        c = WSClient(self.d.port, path, {**ws_headers(self.d), "Host": "localhost:%d" % self.d.port,
                                         "Origin": "http://localhost:%d" % self.d.port})
        self.clients.append(c)
        self.assertEqual(c.status, 403)                                                       # wrong host
        self.assertEqual(self.connect(f"/ws/sessions/{sid}").status, 403)                     # no ticket (CSRF)
        self.assertEqual(self.connect(f"/ws/sessions/{sid}?t=nope").status, 403)
        other = self.new_session()
        t2 = self.ticket(other["id"])
        self.assertEqual(self.connect(f"/ws/sessions/{sid}?t={t2}").status, 403)              # ticket for another session
        self.assertEqual(self.connect(path, version="8").status, 400)                         # bad handshake
        self.assertEqual(self.connect(path, key="short").status, 400)
        c = WSClient(self.d.port, path, {k: v for k, v in ws_headers(self.d).items() if k != "Upgrade"})
        self.clients.append(c)
        self.assertEqual(c.status, 400)
        st, _, _ = self.d.req("GET", path, origin=f"http://127.0.0.1:{self.d.port}")          # plain GET
        self.assertEqual(st, 400)
        self.assertEqual(self.connect(f"/ws/sessions/{sessions.new_id()}?t={t}").status, 403)  # ticket is per session
        gone = self.new_session()
        tg = self.ticket(gone["id"])
        self.post(f"/api/sessions/{gone['id']}/delete")
        self.assertEqual(self.connect(f"/ws/sessions/{gone['id']}?t={tg}").status, 404)        # deleted meanwhile
        c = self.connect(path)                                                                # finally: all good
        self.assertEqual(c.status, 101)
        self.assertEqual(c.headers["sec-websocket-accept"], "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertIn(b"READY", c.recv_until(b"READY"))
        self.assertEqual(self.connect(path).status, 403)                                      # ticket was single use
        # a ticket minted by one login session is not valid for another
        t3 = self.ticket(sid)
        other_sid = self.d.srv.new_session()
        h = {**ws_headers(self.d), "Cookie": f"{dashboard.COOKIE}={other_sid}"}
        c2 = WSClient(self.d.port, f"/ws/sessions/{sid}?t={t3}", h)
        self.clients.append(c2)
        self.assertEqual(c2.status, 403)
        # tickets expire
        t4 = self.ticket(sid)
        with self.d.srv._lock:
            self.d.srv.tickets[t4] = (self.d.srv.tickets[t4][0], sid, time.time() - 1)
        self.assertEqual(self.connect(f"/ws/sessions/{sid}?t={t4}").status, 403)

    def test_ws_stopped_session_is_409(self):
        rec = self.new_session()
        self.post(f"/api/sessions/{rec['id']}/stop")
        t = self.ticket(rec["id"])
        self.assertEqual(self.connect(f"/ws/sessions/{rec['id']}?t={t}").status, 409)

    def test_terminal_io_resize_and_exit(self):
        self.fake.attach = ["/bin/sh", "-c", "echo READY; stty -echo; while read -r l; do [ \"$l\" = quit ] && exit 3; "
                                              "echo \"got:$l\"; echo \"size:$(stty size)\"; done"]
        rec = self.new_session()
        sid = rec["id"]
        c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
        self.assertEqual(c.status, 101)
        c.recv_until(b"READY")
        c.send(frame(1, json.dumps({"type": "resize", "cols": 101, "rows": 33}).encode()))
        c.send(frame(2, b"hel"))
        c.send(frame(2, b"lo\n"))
        out = c.recv_until(b"size:33 101")
        self.assertIn(b"got:hello", out)
        c.send(frame(1, json.dumps({"type": "resize", "cols": 99999, "rows": 0}).encode()))      # clamped, not fatal
        c.send(frame(2, b"x\n"))
        c.recv_until(b"size:5 500")
        c.send(frame(1, b"not json"))                                                           # ignored
        c.send(frame(1, json.dumps({"type": "nope"}).encode()))
        c.send(frame(2, b"quit\n"))
        exit_seen, closed = False, False
        for _ in range(50):
            op, _, payload = c.recv_frame()
            if op == 1 and json.loads(payload).get("type") == "exit":
                exit_seen = True
            if op == 8:
                closed = True
                break
        self.assertTrue(exit_seen and closed)
        self.assertEqual(self.d.srv.terminals_open(), 0)
        st, rows = self.get("/api/sessions")
        self.assertEqual(rows[0]["viewers"], 0)

    def test_terminal_cap_and_slot_release(self):
        rec = self.new_session()
        sid = rec["id"]
        conns = []
        for _ in range(sessions.MAX_TERMINALS):
            c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
            self.assertEqual(c.status, 101)
            c.recv_until(b"READY")
            conns.append(c)
        self.assertEqual(self.d.srv.terminals_open(), sessions.MAX_TERMINALS)
        self.assertEqual(self.get("/api/sessions")[1][0]["viewers"], sessions.MAX_TERMINALS)
        c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
        self.assertEqual(c.status, 503)
        self.assertIn("terminals", c.body.decode())
        conns[0].send(frame(8, struct.pack(">H", 1000)))
        for _ in range(20):
            if self.d.srv.terminals_open() == sessions.MAX_TERMINALS - 1:
                break
            time.sleep(0.1)
        self.assertEqual(self.d.srv.terminals_open(), sessions.MAX_TERMINALS - 1)
        c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
        self.assertEqual(c.status, 101)
        # dropping the socket without a close frame also frees the slot (peer gone)
        conns[1].close()
        for _ in range(50):
            if self.d.srv.terminals_open() == sessions.MAX_TERMINALS - 1:
                break
            time.sleep(0.1)
        self.assertEqual(self.d.srv.terminals_open(), sessions.MAX_TERMINALS - 1)

    def test_idle_viewer_is_closed_and_slot_freed(self):
        ws.PING_INTERVAL, ws.IDLE_TIMEOUT = 0.2, 0.7
        rec = self.new_session()
        sid = rec["id"]
        c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
        c.recv_until(b"READY")
        t0 = time.time()
        while True:
            op, _, _ = c.recv_frame()                      # never answer the pings
            if op == 8:
                break
        self.assertLess(time.time() - t0, 5)
        for _ in range(50):
            if self.d.srv.terminals_open() == 0:
                break
            time.sleep(0.1)
        self.assertEqual(self.d.srv.terminals_open(), 0)

    def test_stop_ends_attached_viewers(self):
        rec = self.new_session()
        sid = rec["id"]
        c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
        c.recv_until(b"READY")
        self.assertEqual(self.post(f"/api/sessions/{sid}/stop")[0], 200)
        for _ in range(50):
            op, _, _ = c.recv_frame()
            if op == 8:
                break
        else:
            self.fail("viewer not closed")
        for _ in range(50):
            if self.d.srv.terminals_open() == 0:
                break
            time.sleep(0.1)
        self.assertEqual(self.d.srv.terminals_open(), 0)

    def test_input_frame_size_cap(self):
        rec = self.new_session()
        sid = rec["id"]
        c = self.connect(f"/ws/sessions/{sid}?t={self.ticket(sid)}")
        c.recv_until(b"READY")
        c.send(frame(2, b"x" * (ws.MAX_FRAME + 1)))
        for _ in range(50):
            op, _, payload = c.recv_frame()
            if op == 8:
                self.assertEqual(struct.unpack(">H", payload[:2])[0], 1009)
                break
        else:
            self.fail("not closed")


# ---------------------------------------------------------------- the real PTY backend with a fake claude
FAKE_CLAUDE = r"""#!/bin/sh
# fake claude for tests: records argv + env, writes a transcript like Claude Code, echoes input
printf '%s\n' "$0 $*" > "$FAKE_OUT/argv.tmp" && mv "$FAKE_OUT/argv.tmp" "$FAKE_OUT/argv"
env > "$FAKE_OUT/env.tmp" && mv "$FAKE_OUT/env.tmp" "$FAKE_OUT/env"
sid=""
while [ $# -gt 0 ]; do case "$1" in --session-id|--resume) sid="$2"; shift;; esac; shift; done
enc=$(pwd | sed 's/[^A-Za-z0-9]/-/g')
mkdir -p "$CLAUDE_CONFIG_DIR/projects/$enc"
echo 'fake claude ready'
while read -r line; do
  printf '{"type":"user","message":{"role":"user","content":"%s"}}\n' "$line" >> "$CLAUDE_CONFIG_DIR/projects/$enc/$sid.jsonl"
  [ "$line" = exit ] && exit 0
  echo "fake:$line"
done
"""


class TestRealPty(unittest.TestCase):
    """The real backend with a fake `claude` (a shell script): the session is a PTY owned by this process,
    survives its viewers, replays scrollback to a new viewer, stops and resumes with --resume."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-pty-"))
        self.d = Dash()
        self.home = self.tmp / "home"
        (self.home / "ops").mkdir(parents=True)
        self.claude = self.tmp / "claude"
        self.out = self.tmp / "out"
        self.out.mkdir()
        exe = self.tmp / "claude-fake"
        exe.write_text(FAKE_CLAUDE.replace("$FAKE_OUT", str(self.out)))
        exe.chmod(0o700)
        self.old_env = dict(os.environ)
        os.environ.update(SECRETS)
        os.environ.update({"CLAUDE_CONFIG_DIR": str(self.claude), "HANDS_CLAUDE": str(exe), "TMUX": "/tmp/tmux-0/default,1,0"})
        self.old_home = sessions.HOME
        sessions.HOME = self.home
        self.old_backend = sessions.BACKEND
        sessions.BACKEND = sessions.PtyBackend()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        sessions.BACKEND.shutdown()
        self.d.close()
        sessions.HOME = self.old_home
        sessions.BACKEND = self.old_backend
        os.environ.clear()
        os.environ.update(self.old_env)

    def post(self, path, body=None):
        c = __import__("http.client").client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.d.port}", "Cookie": f"{dashboard.COOKIE}={self.d.sid}",
             "Origin": f"http://127.0.0.1:{self.d.port}", "X-Hands": "1"}
        c.request("POST", path, headers=h, body=json.dumps(body or {}).encode())
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def viewer(self, sid):
        _, t = self.post(f"/api/sessions/{sid}/ticket")
        c = WSClient(self.d.port, f"/ws/sessions/{sid}?t={t['ticket']}", ws_headers(self.d))
        self.clients.append(c)
        self.assertEqual(c.status, 101)
        return c

    def test_session_is_a_dashboard_pty_and_survives_viewers(self):
        st, rec = self.post("/api/sessions/new", {"cwd": str(self.home / "ops")})
        self.assertEqual(st, 200, rec)
        sid = rec["id"]
        self.assertTrue(rec["live"])
        for _ in range(50):
            if (self.out / "env").exists():
                break
            time.sleep(0.1)
        argv = (self.out / "argv").read_text()
        self.assertIn(f"--session-id {sid}", argv)
        self.assertNotIn("dangerously", argv)
        env = dict(l.split("=", 1) for l in (self.out / "env").read_text().splitlines() if "=" in l)
        for k in SECRETS:
            self.assertNotIn(k, env, k)
        for k in ("CLAUDECODE", "TMUX", "TMUX_TMPDIR", "COLORTERM"):
            self.assertNotIn(k, env, k)
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertEqual(env["HOME"], str(Path.home()))
        self.assertEqual(env["PWD"], str(self.home / "ops"))                      # sh changed into the folder
        self.assertEqual(set(env) - {"PWD", "OLDPWD", "_", "SHLVL"}, {"PATH", "HOME", "TERM", "LANG", "USER", "SHELL", "CLAUDE_CONFIG_DIR"})
        proc = sessions.BACKEND.proc(sid)
        self.assertIsNotNone(proc)
        self.assertEqual(os.getpgid(proc.pid), proc.pid)                           # its own session/process group
        # viewer 1: scrollback replayed (the banner was printed before anyone attached), typing works
        c = self.viewer(sid)
        c.recv_until(b"fake claude ready")
        c.send(frame(1, json.dumps({"type": "resize", "cols": 100, "rows": 30}).encode()))
        c.send(frame(2, b"hello pty\r"))
        self.assertIn(b"fake:hello pty", c.recv_until(b"fake:hello"))
        c.close()                                                                  # browser closed...
        time.sleep(0.3)
        st, _, rows = self.d.req("GET", "/api/sessions")
        rows = json.loads(rows)
        self.assertTrue(rows[0]["live"])                                           # ...the session runs on
        self.assertEqual(rows[0]["viewers"], 0)
        self.assertEqual(rows[0]["title"], "hello pty")
        # viewer 2 and 3: both get the whole scrollback, both see new output, either can type
        c2 = self.viewer(sid)
        c3 = self.viewer(sid)
        for c in (c2, c3):
            back = c.recv_until(b"fake:hello pty")
            self.assertIn(b"fake claude ready", back)
        self.assertEqual(json.loads(self.d.req("GET", "/api/sessions")[2])[0]["viewers"], 2)
        c3.send(frame(2, b"again\r"))
        self.assertIn(b"fake:again", c2.recv_until(b"fake:again"))
        self.assertIn(b"fake:again", c3.recv_until(b"fake:again"))
        # stop: the process is gone and every viewer is told and closed
        self.assertEqual(self.post(f"/api/sessions/{sid}/stop")[1]["live"], False)
        for c in (c2, c3):
            seen = set()
            for _ in range(50):
                op, _, payload = c.recv_frame()
                seen.add(op)
                if op == 8:
                    break
            self.assertIn(8, seen)
        for _ in range(50):
            if self.d.srv.terminals_open() == 0:
                break
            time.sleep(0.1)
        self.assertEqual(self.d.srv.terminals_open(), 0)
        self.assertFalse(proc.alive())
        # resume = claude --resume <id>, a fresh PTY, empty scrollback
        st, r = self.post(f"/api/sessions/{sid}/resume")
        self.assertEqual((st, r["live"]), (200, True))
        for _ in range(50):
            if f"--resume {sid}" in (self.out / "argv").read_text():
                break
            time.sleep(0.1)
        self.assertIn(f"--resume {sid}", (self.out / "argv").read_text())
        c4 = self.viewer(sid)
        self.assertNotIn(b"hello pty", c4.recv_until(b"fake claude ready"))
        self.assertEqual(self.post(f"/api/sessions/{sid}/delete")[0], 200)
        self.assertFalse(sessions.BACKEND.alive(sid))

    def test_process_exit_ends_viewers_and_marks_stopped(self):
        st, rec = self.post("/api/sessions/new", {"cwd": str(self.home / "ops")})
        sid = rec["id"]
        c = self.viewer(sid)
        c.recv_until(b"fake claude ready")
        c.send(frame(2, b"exit\r"))
        exit_seen = closed = False
        for _ in range(50):
            op, _, payload = c.recv_frame()
            if op == 1 and json.loads(payload).get("type") == "exit":
                exit_seen = True
            if op == 8:
                closed = True
                break
        self.assertTrue(exit_seen and closed)
        for _ in range(50):
            if not json.loads(self.d.req("GET", "/api/sessions")[2])[0]["live"]:
                break
            time.sleep(0.1)
        self.assertFalse(json.loads(self.d.req("GET", "/api/sessions")[2])[0]["live"])
        self.assertEqual(self.post(f"/api/sessions/{sid}/resume")[0], 200)        # a dead session resumes

    def test_scrollback_is_bounded(self):
        old = sessions.SCROLLBACK
        sessions.SCROLLBACK = 4096
        try:
            p = sessions.BACKEND.launch(sessions.new_id(), ["/bin/sh", "-c", "i=0; while [ $i -lt 400 ]; do printf 'line %04d xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n' $i; i=$((i+1)); done; exec cat"],
                                        {"PATH": "/usr/bin:/bin"}, "/")
            for _ in range(100):
                if b"line 0399" in p.snapshot():
                    break
                time.sleep(0.05)
            snap = p.snapshot()
            self.assertLessEqual(len(snap), 4096)
            self.assertIn(b"line 0399", snap)
            self.assertNotIn(b"line 0000", snap)
            p.kill()
            self.assertFalse(p.alive())
        finally:
            sessions.SCROLLBACK = old

    def test_shutdown_ends_every_session(self):
        a = self.post("/api/sessions/new", {"cwd": str(self.home / "ops")})[1]
        b = self.post("/api/sessions/new", {"cwd": str(self.home)})[1]
        pa, pb = sessions.BACKEND.proc(a["id"]), sessions.BACKEND.proc(b["id"])
        sessions.BACKEND.shutdown()
        self.assertFalse(pa.alive() or pb.alive())
        rows = json.loads(self.d.req("GET", "/api/sessions")[2])
        self.assertEqual([r["live"] for r in rows], [False, False])              # offered for resume after a restart


if __name__ == "__main__":
    unittest.main()
