#!/usr/bin/env python3
"""Static server for the design mock (tools/ui-shots.sh --mock), with the dashboard's exact security
headers, so the browser enforces the same CSP (no inline script, no style attributes) on the mock.

  python3 mockserve.py <dir> <port>      serves <dir> on 127.0.0.1:<port>
"""
from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from hands.dashboard import SECURITY_HEADERS


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    d, port = sys.argv[1], int(sys.argv[2])
    srv = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=d))
    print(f"mock on http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
