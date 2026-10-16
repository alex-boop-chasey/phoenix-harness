"""Live activity from a `claude -p --output-format stream-json --verbose` session.

A tail thread reads the session's stdout file while the container runs and turns tool calls into short
human-readable lines ("writing docs/design.md", "searching: astro 7 tailwind"). Everything here is
agent-influenced text: it is only ever truncated and stored/displayed as plain text.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

MAX = 160
MAX_LINE = 5_000_000


def _short(s, n=MAX) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _rel(path: str) -> str:
    p = str(path)
    for pre in ("/workspace/", "/home/node/"):
        if p.startswith(pre):
            return p[len(pre):]
    return p


def describe(event: dict) -> list[dict]:
    """[{kind, text, file?}] for the interesting tool calls in one stream event."""
    out = []
    if event.get("type") != "assistant":
        return out
    msg = event.get("message") or {}
    sub = event.get("parent_tool_use_id") is not None
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name", ""))
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        prefix = "subagent: " if sub else ""
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            f = _rel(inp.get("file_path") or inp.get("notebook_path") or "")
            out.append({"kind": "write", "text": f"{prefix}writing {_short(f, 120)}", "file": _short(f, 200)})
        elif name == "WebSearch":
            out.append({"kind": "search", "text": f"{prefix}searching: {_short(inp.get('query', ''), 120)}"})
        elif name in ("Agent", "Task"):
            out.append({"kind": "agent", "text": f"{prefix}subagent: {_short(inp.get('description') or inp.get('subagent_type') or '', 120)}"})
        elif name == "Skill":
            out.append({"kind": "skill", "text": f"{prefix}skill: {_short(inp.get('skill') or inp.get('name') or '', 80)}"})
        elif name == "Bash":
            out.append({"kind": "bash", "text": f"{prefix}running: {_short(inp.get('command', ''), 120)}"})
        elif name in ("Read", "Glob", "Grep"):
            target = inp.get("file_path") or inp.get("pattern") or inp.get("path") or ""
            out.append({"kind": "read", "text": f"{prefix}{name.lower()} {_short(_rel(target), 120)}"})
    return out


class Tail:
    """Follow a growing JSONL file in a thread; call on_item for each described tool call."""

    def __init__(self, path: Path, on_item: Callable[[dict], None], poll: float = 1.0):
        self.path, self.on_item, self.poll = path, on_item, poll
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.tools = 0

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=10)

    def _run(self):
        pos, buf, skipping = 0, b"", False
        while True:
            stopping = self._stop.is_set()
            try:
                with open(self.path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(1_000_000)
                    pos += len(chunk)
            except OSError:
                chunk = b""
            data = chunk
            if skipping:                        # inside an oversized line: drop bytes until its end
                nl = data.find(b"\n")
                data, skipping = (b"", True) if nl < 0 else (data[nl + 1:], False)
            buf += data
            *lines, buf = buf.split(b"\n")
            if len(buf) > MAX_LINE:             # never buffer a line with no end in sight
                buf, skipping = b"", True
            for line in lines:
                self._handle(line)
            if not chunk:
                if stopping:
                    return
                self._stop.wait(self.poll)

    def _handle(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"{") or len(line) > MAX_LINE:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        if ev.get("type") == "rate_limit_event":
            from . import usage
            u = usage.extract(ev)
            if u:
                try:
                    usage.record(u)
                except OSError:
                    pass
            return
        for item in describe(ev):
            self.tools += 1
            try:
                self.on_item({**item, "at": int(time.time())})
            except Exception:
                pass
