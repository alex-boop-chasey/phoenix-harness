#!/usr/bin/env python3
"""Static server for the design mock (tools/ui-shots.sh --mock), with the dashboard's exact security
headers, so the browser enforces the same CSP (no inline script, no style attributes) on the mock.

  python3 mockserve.py <dir> <port> [/prefix=dir ...]
      serves <dir> on 127.0.0.1:<port>; each /prefix=dir serves that directory under /prefix/
      (the rig uses /vendor=console/static for the xterm.js CSP probe, step 09).

A .html file containing the marker __NONCE__ is served with a fresh random nonce in its place and
`'nonce-<value>'` added to the CSP style-src (what the dashboard would do for a page that must let
xterm.js inject <style> elements). Everything else keeps the dashboard's headers unchanged.
"""
from __future__ import annotations

import secrets
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hands.dashboard import SECURITY_HEADERS

NONCE_MARK = b"__NONCE__"


class Handler(SimpleHTTPRequestHandler):
    mounts: dict[str, str] = {}

    def __init__(self, *a, **kw):
        self._csp = SECURITY_HEADERS["Content-Security-Policy"]
        super().__init__(*a, **kw)

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v if k != "Content-Security-Policy" else self._csp)
        super().end_headers()

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        for prefix, d in self.mounts.items():
            if clean.startswith(prefix + "/"):
                rel = Path(clean[len(prefix) + 1:])
                if any(p in ("..", "") for p in rel.parts):
                    return str(Path(d) / "__missing__")
                return str(Path(d) / rel)
        return super().translate_path(path)

    def do_GET(self):
        p = Path(self.translate_path(self.path))
        if p.suffix == ".html" and p.is_file():
            body = p.read_bytes()
            if NONCE_MARK in body:
                nonce = secrets.token_urlsafe(16)
                body = body.replace(NONCE_MARK, nonce.encode())
                self._csp = self._csp.replace("style-src 'self'", f"style-src 'self' 'nonce-{nonce}'")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        super().do_GET()

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    d, port = sys.argv[1], int(sys.argv[2])
    for m in sys.argv[3:]:
        prefix, _, mdir = m.partition("=")
        if not prefix.startswith("/") or not mdir:
            sys.exit(f"bad mount {m!r}: expected /prefix=dir")
        Handler.mounts[prefix.rstrip("/")] = mdir
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=d))
    print(f"mock on http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
