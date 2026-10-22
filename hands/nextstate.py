"""Per-project Phase 2 state: style references, setup choices and the planning chat (next.json).

One JSON file per project (`<state_dir>/next.json`, written atomically, mode 0600):
  {"schema": 1,
   "next_index": 3,                          # style ref indexes are never reused
   "style_refs": [{"index", "url", "added", "status", "reason", "updated"}],
   "setup": {"framework", "libs", "status", "record"},
   "plan_session": {"session_id", "status", "brief_sha256", "updated"}}
Statuses: style refs queued|capturing|analysing|frozen|failed; setup none|pending|running|done|failed;
plan_session none|open|brief_ready|approved. `gate(state)` lists the reasons planning may not start.

CLI: python3 -m hands.nextstate init|show|add-ref|set-ref|remove-ref|set-setup|set-plan-session|gate --file FILE
Exit codes: 0 ok, 1 invalid input or (gate) planning blocked.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .setup import FRAMEWORK_LABELS, LIB_IDS, SETUP_STATUSES, BLOCKING_STATUSES, InvalidOptions, SetupOptions
from .stylerefs import InvalidUrl, validate_url

SCHEMA = 1
FILE = "next.json"
MAX_REFS = 10
MAX_INDEX = 99                      # refs/style-refs/<nn>: two digits
REF_STATUSES = ("queued", "capturing", "analysing", "frozen", "failed")
PLAN_STATUSES = ("none", "open", "brief_ready", "approved")
MAX_REASON = 500
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidInput(ValueError):
    """Bad input or an unreadable state file (exit 1 on the CLI)."""


class TooManyRefs(InvalidInput):
    def __init__(self, message: str = f"at most {MAX_REFS} style refs per project"):
        super().__init__(message)


class UnknownRef(InvalidInput):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def empty() -> dict:
    return {"schema": SCHEMA, "next_index": 1, "style_refs": [],
            "setup": {"framework": 1, "libs": [], "status": "none", "record": None},
            "plan_session": {"session_id": None, "status": "none", "brief_sha256": None, "updated": None}}


# ------------------------------------------------------------------------------------------- file

def load(path: Path) -> dict:
    """The state in `path`; a missing file is the empty state (no refs, no setup, no planning chat)."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as e:
        raise InvalidInput(f"{path}: {e}") from None
    if not isinstance(raw, dict):
        raise InvalidInput(f"{path}: not a JSON object")
    state = empty()
    refs = raw.get("style_refs")
    if isinstance(refs, list):
        state["style_refs"] = [r for r in refs if isinstance(r, dict) and isinstance(r.get("index"), int)
                               and r.get("status") in REF_STATUSES and isinstance(r.get("url"), str)]
    top = max([r["index"] for r in state["style_refs"]] or [0])
    nxt = raw.get("next_index")
    state["next_index"] = max(nxt if isinstance(nxt, int) and not isinstance(nxt, bool) else 1, top + 1)
    for key in ("setup", "plan_session"):
        if isinstance(raw.get(key), dict):
            state[key].update(raw[key])
    if state["setup"].get("status") not in SETUP_STATUSES:
        state["setup"]["status"] = "none"
    if state["plan_session"].get("status") not in PLAN_STATUSES:
        state["plan_session"]["status"] = "none"
    return state


def save(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------------------- validation

def check_urls(style_urls: Any) -> list[str]:
    if not isinstance(style_urls, list):
        raise InvalidInput("style_refs must be a list of URLs")
    urls: list[str] = []
    for u in style_urls:
        try:
            norm = validate_url(u)
        except InvalidUrl as e:
            raise InvalidInput(f"invalid url: {e}") from None
        if norm in urls:
            raise InvalidInput(f"duplicate style ref url: {norm}")
        urls.append(norm)
    if len(urls) > MAX_REFS:
        raise TooManyRefs()
    return urls


def check_options(framework: Any, libs: Any) -> SetupOptions:
    try:
        return SetupOptions.from_dict({"framework": framework, "libs": libs})
    except InvalidOptions as e:
        raise InvalidInput(str(e)) from None


def validate(style_urls: Any, framework: Any, libs: Any) -> tuple[list[str], int, list[str]]:
    """Normalised (urls, framework, libs) or InvalidInput: http(s) URLs without credentials (max 10),
    framework 1-5, libs from LIB_IDS."""
    urls = check_urls(style_urls)
    opts = check_options(framework, libs)
    return urls, opts.framework, opts.libs


def setup_status_for(framework: int, libs: list[str]) -> str:
    """`none` for framework 1 without libraries (nothing to scaffold), else `pending`."""
    return "none" if framework == 1 and not libs else "pending"


def _ref(state: dict, index: Any) -> dict:
    if isinstance(index, bool) or not isinstance(index, int):
        raise InvalidInput("index must be an integer")
    for r in state["style_refs"]:
        if r["index"] == index:
            return r
    raise UnknownRef(f"no style ref {index:02d}" if 0 <= index <= MAX_INDEX else f"no style ref {index}")


def ref(state: dict, index: int) -> dict:
    """The style ref entry with this index (UnknownRef when absent)."""
    return _ref(state, index)


# --------------------------------------------------------------------------------------- mutations

def init(path: Path, style_urls: list[str], framework: int, libs: list[str]) -> dict:
    """Write a new next.json (refuses to overwrite one): refs queued with indexes 1..n, setup none/pending."""
    urls, fw, lb = validate(style_urls, framework, libs)
    path = Path(path)
    if path.exists():
        raise InvalidInput(f"{path} already exists")
    state = empty()
    added = now_iso()
    state["style_refs"] = [{"index": i, "url": u, "added": added, "status": "queued", "reason": None, "updated": added}
                           for i, u in enumerate(urls, start=1)]
    state["next_index"] = len(urls) + 1
    state["setup"] = {"framework": fw, "libs": lb, "status": setup_status_for(fw, lb), "record": None}
    save(path, state)
    return state


def add_ref(path: Path, url: str) -> int:
    """Append a queued style ref; returns its index (the next one, never reused)."""
    try:
        norm = validate_url(url)
    except InvalidUrl as e:
        raise InvalidInput(f"invalid url: {e}") from None
    state = load(path)
    if len(state["style_refs"]) >= MAX_REFS:
        raise TooManyRefs()
    if any(r["url"] == norm for r in state["style_refs"]):
        raise InvalidInput(f"duplicate style ref url: {norm}")
    index = state["next_index"]
    if index > MAX_INDEX:
        raise TooManyRefs(f"style ref indexes are used up (max {MAX_INDEX})")
    added = now_iso()
    state["style_refs"].append({"index": index, "url": norm, "added": added, "status": "queued", "reason": None,
                                "updated": added})
    state["next_index"] = index + 1
    save(path, state)
    return index


def set_ref(path: Path, index: int, status: str, reason: str | None = None) -> dict:
    if status not in REF_STATUSES:
        raise InvalidInput(f"status must be one of {', '.join(REF_STATUSES)}")
    if reason is not None and not isinstance(reason, str):
        raise InvalidInput("reason must be text")
    state = load(path)
    r = _ref(state, index)
    r["status"] = status
    r["reason"] = " ".join(reason.split())[:MAX_REASON] if reason else None
    r["updated"] = now_iso()
    save(path, state)
    return r


def remove_ref(path: Path, index: int) -> None:
    state = load(path)
    r = _ref(state, index)
    state["style_refs"].remove(r)
    save(path, state)


def set_setup(path: Path, status: str, record: dict | None = None, framework: int | None = None,
              libs: list[str] | None = None) -> dict:
    """Set the setup status (and the record when given); framework/libs record new choices."""
    if status not in SETUP_STATUSES:
        raise InvalidInput(f"status must be one of {', '.join(SETUP_STATUSES)}")
    if record is not None and not isinstance(record, dict):
        raise InvalidInput("record must be an object")
    state = load(path)
    s = state["setup"]
    if framework is not None or libs is not None:
        opts = check_options(s.get("framework", 1) if framework is None else framework,
                             s.get("libs", []) if libs is None else libs)
        s.update({"framework": opts.framework, "libs": opts.libs})
    s["status"] = status
    if record is not None:
        s["record"] = record
    save(path, state)
    return s


def set_plan_session(path: Path, status: str, session_id: str | None = None, sha: str | None = None) -> dict:
    """Set the planning chat status; a session_id / sha given replaces the stored one (None keeps it)."""
    if status not in PLAN_STATUSES:
        raise InvalidInput(f"status must be one of {', '.join(PLAN_STATUSES)}")
    if session_id is not None and (not isinstance(session_id, str) or not UUID_RE.match(session_id)):
        raise InvalidInput("session_id must be a uuid")
    if sha is not None and (not isinstance(sha, str) or not SHA_RE.match(sha)):
        raise InvalidInput("sha must be 64 lower-case hex characters")
    state = load(path)
    p = state["plan_session"]
    p["status"] = status
    if session_id is not None:
        p["session_id"] = session_id
    if sha is not None:
        p["brief_sha256"] = sha
    p["updated"] = now_iso()
    save(path, state)
    return p


# --------------------------------------------------------------------------------------------- gate

def gate(state: dict) -> list[str]:
    """Reasons planning may not start: every style ref not frozen, and a setup that is pending/running/failed."""
    reasons = []
    for r in sorted(state.get("style_refs") or [], key=lambda x: x.get("index", 0)):
        status = r.get("status")
        if status == "frozen":
            continue
        line = f"style ref {r.get('index', 0):02d} {status}"
        if status == "failed":
            line += f": {r.get('reason') or 'unknown error'}"
        reasons.append(line)
    setup_status = (state.get("setup") or {}).get("status")
    if setup_status in BLOCKING_STATUSES:
        reasons.append(f"setup {setup_status}")
    return reasons


def describe(state: dict) -> str:
    """Human-readable summary (`show` without --json, `hands <slug> status`)."""
    s = state["setup"]
    libs = ", ".join(s.get("libs") or []) or "no libs"
    lines = [f"setup: {FRAMEWORK_LABELS.get(s.get('framework'), '?')} + {libs}: {s.get('status')}"]
    for r in state["style_refs"]:
        lines.append(f"style ref {r['index']:02d} {r['status']}" + (f": {r['reason']}" if r.get("reason") else "")
                     + f"  {r['url']}")
    p = state["plan_session"]
    lines.append(f"planning chat: {p.get('status')}" + (f" (brief {p['brief_sha256'][:12]})" if p.get("brief_sha256") else ""))
    reasons = gate(state)
    lines.append("planning may start" if not reasons else "planning blocked: " + "; ".join(reasons))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- CLI

def _record(path: str | None) -> dict | None:
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InvalidInput(f"{path}: {e}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.nextstate", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("--file", required=True, help="the project's next.json")
        return p

    p = cmd("init", "write a new state file")
    p.add_argument("--style-urls", nargs="*", default=[], metavar="URL")
    p.add_argument("--framework", type=int, default=1, help=", ".join(f"{k} {v}" for k, v in FRAMEWORK_LABELS.items()))
    p.add_argument("--libs", nargs="*", default=[], help=f"library ids ({', '.join(LIB_IDS)})")
    p = cmd("show", "print the state")
    p.add_argument("--json", action="store_true")
    p = cmd("add-ref", "add a style ref URL; prints its index")
    p.add_argument("--url", required=True)
    p = cmd("set-ref", "set a style ref's status")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, help=" | ".join(REF_STATUSES))
    p.add_argument("--reason")
    p = cmd("remove-ref", "drop a style ref")
    p.add_argument("--index", type=int, required=True)
    p = cmd("set-setup", "set the setup status")
    p.add_argument("--status", required=True, help=" | ".join(SETUP_STATUSES))
    p.add_argument("--record", help="JSON file with the setup record (refs/setup.json)")
    p = cmd("set-plan-session", "set the planning chat state")
    p.add_argument("--status", required=True, help=" | ".join(PLAN_STATUSES))
    p.add_argument("--session-id")
    p.add_argument("--sha")
    cmd("gate", "exit 1 and print the reasons when planning may not start")
    a = ap.parse_args(argv)

    f = Path(a.file)
    try:
        if a.cmd == "init":
            init(f, a.style_urls, a.framework, a.libs)
        elif a.cmd == "show":
            state = load(f)
            print(json.dumps(state, indent=1) if a.json else describe(state))
        elif a.cmd == "add-ref":
            print(add_ref(f, a.url))
        elif a.cmd == "set-ref":
            set_ref(f, a.index, a.status, a.reason)
        elif a.cmd == "remove-ref":
            remove_ref(f, a.index)
        elif a.cmd == "set-setup":
            set_setup(f, a.status, _record(a.record))
        elif a.cmd == "set-plan-session":
            set_plan_session(f, a.status, a.session_id, a.sha)
        elif a.cmd == "gate":
            reasons = gate(load(f))
            for r in reasons:
                print(r)
            if reasons:
                return 1
            print("planning may start")
    except InvalidInput as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
