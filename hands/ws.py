"""Server-side WebSocket (RFC 6455, no extensions) for the dashboard's terminal connections.

Runs on the socket a BaseHTTPRequestHandler already owns, after the handler has checked Host, session,
Origin and the CSRF ticket and written the 101. Fail-closed on anything the RFC calls a protocol error:
unmasked client frames, reserved bits, unknown opcodes, fragmented or oversized control frames, bad
UTF-8 in text, continuation without a start. Bounded: payload per frame <= MAX_FRAME, assembled message
<= MAX_MESSAGE. Liveness: a ping every PING_INTERVAL seconds of silence, close 1001 after IDLE_TIMEOUT
without any client frame (browsers answer pings automatically); a frame that stalls half-way is a
protocol error. Thread-safe sends (the PTY pump and the reader share one connection).
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 64 * 1024           # client payload bytes per frame (terminal input, resize messages)
MAX_MESSAGE = 256 * 1024        # assembled fragmented message
PING_INTERVAL = 20.0
IDLE_TIMEOUT = 60.0

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10


class Closed(Exception):
    """The connection is over; .code is the close code seen or sent (1006 = peer vanished)."""

    def __init__(self, code: int, reason: str = ""):
        super().__init__(f"websocket closed ({code}) {reason}".rstrip())
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


def handshake_error(headers) -> str | None:
    """None if the request headers form a valid RFC 6455 client handshake, else the reason."""
    if (headers.get("Upgrade") or "").strip().lower() != "websocket":
        return "Upgrade: websocket required"
    tokens = [t.strip().lower() for t in (headers.get("Connection") or "").split(",")]
    if "upgrade" not in tokens:
        return "Connection: Upgrade required"
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        return "Sec-WebSocket-Version: 13 required"
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    try:
        if len(key) != 24 or len(base64.b64decode(key, validate=True)) != 16:
            return "bad Sec-WebSocket-Key"
    except (ValueError, TypeError):
        return "bad Sec-WebSocket-Key"
    return None


def _unmask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    if not n:
        return b""
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ m).to_bytes(n, "big")


class WebSocket:
    def __init__(self, sock: socket.socket, rfile, *, max_frame: int = 0, max_message: int = 0,
                 ping_interval: float = 0.0, idle_timeout: float = 0.0):
        self.sock = sock
        self.max_frame = max_frame or MAX_FRAME
        self.max_message = max_message or MAX_MESSAGE
        self.ping_interval = ping_interval or PING_INTERVAL
        self.idle_timeout = idle_timeout or IDLE_TIMEOUT
        self._wlock = threading.Lock()
        self._closed = False
        self.close_code: int | None = None
        self._last_rx = time.monotonic()
        # Bytes the HTTP handler's buffered reader already pulled off the socket (a client may send its
        # first frames right behind the handshake). From here on we read the socket directly: SocketIO
        # refuses every read after one timeout, and timeouts are how liveness pings are paced.
        self._buf = b""
        if rfile is not None:
            old = sock.gettimeout()
            sock.settimeout(0)
            try:
                self._buf = rfile.peek(1) or b""          # non-blocking: buffered leftovers only
                if self._buf:
                    rfile.read(len(self._buf))
            except (OSError, ValueError):
                self._buf = b""
            finally:
                sock.settimeout(old)

    # -- sending
    def _send_frame(self, op: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | op, n])
        elif n < 65536:
            head = bytes([0x80 | op, 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
        with self._wlock:
            if self._closed:
                return
            try:
                self.sock.sendall(head + payload)
            except (OSError, ValueError) as e:
                self._closed = True
                raise Closed(1006, f"send failed: {type(e).__name__}") from None

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, data)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload[:125])

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send the close frame once. The caller stops using the connection; the server closes the socket."""
        with self._wlock:
            if self._closed:
                return
            self._closed = True
            self.close_code = self.close_code or code
            payload = struct.pack(">H", code) + reason.encode()[:123]
            try:
                self.sock.sendall(bytes([0x80 | OP_CLOSE, len(payload)]) + payload)
            except (OSError, ValueError):
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    # -- receiving
    def _read(self, n: int, started: bool) -> bytes:
        """Exactly n bytes. Timeout at a frame boundary -> TimeoutError (liveness handled by recv);
        timeout inside a frame -> half-open peer -> 1002. EOF/reset -> 1006."""
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(max(4096, n - len(self._buf)))
            except TimeoutError:
                if not started and not self._buf:
                    raise
                self._fail(1002, "stalled mid-frame")
            except (OSError, ValueError) as e:
                self._gone(f"recv failed: {type(e).__name__}")
            if not chunk:
                self._gone("eof")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _fail(self, code: int, reason: str):
        self.close(code, reason)
        raise Closed(code, reason)

    def _gone(self, reason: str):
        with self._wlock:
            self._closed = True
            self.close_code = self.close_code or 1006
        raise Closed(1006, reason)

    def _read_frame(self) -> tuple[int, bool, bytes]:
        self.sock.settimeout(self.ping_interval)
        head = self._read(2, False)
        b1, b2 = head
        fin, rsv, op = bool(b1 & 0x80), b1 & 0x70, b1 & 0x0F
        masked, n = bool(b2 & 0x80), b2 & 0x7F
        if rsv:
            self._fail(1002, "reserved bits set")
        if not masked:
            self._fail(1002, "client frames must be masked")
        if op not in (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG):
            self._fail(1002, "unknown opcode")
        if op >= OP_CLOSE and (not fin or n > 125):
            self._fail(1002, "bad control frame")
        if n == 126:
            n = struct.unpack(">H", self._read(2, True))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8, True))[0]
        if n > self.max_frame:
            self._fail(1009, "frame too large")
        mask = self._read(4, True)
        payload = _unmask(self._read(n, True), mask) if n else b""
        self._last_rx = time.monotonic()
        return op, fin, payload

    def recv(self) -> tuple[str, bytes]:
        """Next data message as ("text", utf8 bytes) or ("binary", bytes). Control frames are handled here.
        Raises Closed when the connection ends (for any reason); after that the object is unusable."""
        if self._closed:
            raise Closed(self.close_code or 1006, "already closed")
        kind: int | None = None
        parts: list[bytes] = []
        size = 0
        while True:
            try:
                op, fin, payload = self._read_frame()
            except TimeoutError:
                idle = time.monotonic() - self._last_rx
                if idle >= self.idle_timeout:
                    self._fail(1001, "idle")
                self.ping(b"hb")
                continue
            if op == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                if len(payload) == 1 or (len(payload) >= 2 and not (1000 <= code <= 4999)):
                    code = 1002
                self.close(code if code != 1005 else 1000)
                raise Closed(code, "closed by peer")
            if op == OP_CONT:
                if kind is None:
                    self._fail(1002, "continuation without a start")
            else:
                if kind is not None:
                    self._fail(1002, "new message while one is open")
                kind = op
            size += len(payload)
            if size > self.max_message:
                self._fail(1009, "message too large")
            parts.append(payload)
            if not fin:
                continue
            data = b"".join(parts)
            if kind == OP_TEXT:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    self._fail(1007, "invalid utf-8")
                return "text", data
            return "binary", data
