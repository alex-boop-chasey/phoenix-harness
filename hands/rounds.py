"""Change rounds (0.5.7, B2): the build of one approved amendment on its own app branch.

Round 0 is the fresh plan: no record, no branch, no tag. A step's round is read from its id (`C<n>-...`
means amendment round n). When the next pending step belongs to a higher round than the runner's record,
the runner opens that round in `Runner.next_step`: branch `change/C<n>` from origin/<cfg.branch>, tag
`hands/pre-C<n>` at that base, both pushed, then the record below is written to state.json:

  "round": {"id": 1, "branch": "change/C1", "base_sha": "...", "pre_tag": "hands/pre-C1",
            "spec_commit": "...", "opened_at": "...", "status": "open"}      # B3 sets merged + merged_sha

Per-step integrity record (written when a step passes, also by `mark-done`): `spec_hash` = sha256 over the
step's canonical steps.json entry and the bytes of the acceptance files it owned at that moment
(`spec_files`). Passed step entries and their owned acceptance files are immutable from then on: the runner
re-checks the hash on every spec load and the planner's amend validator checks candidates against it.

0.6.0: the one exception is a passed step's acceptance FILES (never its entry), changed by an amendment that lists
them in `tests_changed` and that the human approved with the separate "tests of steps that already passed" tick
(`approve --ack-passed-tests`). Approve then re-records exactly those steps (`rerecord`): new spec_hash/spec_files
plus `tests_amended` {commit, round, at, files}, the commit the amend validator compares those files with from then
on. Before a round's first new step, the runner runs every passed code step's acceptance once on the round's base
(the baseline) and records the result in the round record:

  "baseline": {"key": "...", "base_sha": "...", "spec_commit": "...", "status": "passed"|"failed", "at": "...",
               "logs": "...", + on failure "step", "cmd", "files", "rc"}

`key` covers the base, the commands and the passed steps' spec_hash, so a passed baseline is not re-run on restart
unless one of them changes.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import accview

ROUND_ID = re.compile(r"^C([1-9][0-9]*)-")
BRANCH = "change/C{round}"
PRE_TAG = "hands/pre-C{round}"


class RoundError(Exception):
    """A round cannot be opened safely (e.g. the pre-tag exists at another commit). Escalate, do not retry."""


def round_of(step_id: str) -> int:
    m = ROUND_ID.match(step_id)
    return int(m.group(1)) if m else 0


def branch_name(rnd: int) -> str:
    return BRANCH.format(round=rnd)


def pre_tag(rnd: int) -> str:
    return PRE_TAG.format(round=rnd)


def record(state_data: dict) -> dict | None:
    r = state_data.get("round")
    return r if isinstance(r, dict) and isinstance(r.get("id"), int) else None


def is_open(state_data: dict) -> bool:
    r = record(state_data)
    return bool(r and r.get("status") == "open")


def active_branch(state_data: dict, default: str) -> str:
    r = record(state_data)
    return str(r["branch"]) if r and r.get("status") == "open" and r.get("branch") else default


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def raw_entry(raw: dict, sid: str) -> dict | None:
    return next((s for s in raw.get("steps") or [] if isinstance(s, dict) and s.get("id") == sid), None)


def step_hash(entry: dict, files: dict[str, bytes | None]) -> str:
    """sha256 over the canonical entry and (path, sha256(bytes)) pairs; a missing file hashes as 'missing'."""
    parts = [("entry", _canon(entry))]
    for path in sorted(files):
        b = files[path]
        parts.append((path, hashlib.sha256(b).hexdigest() if b is not None else "missing"))
    return hashlib.sha256(_canon(parts).encode()).hexdigest()


def read_files(spec_root: Path, paths: list[str]) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for p in paths:
        f = spec_root / "acceptance" / p
        out[p] = f.read_bytes() if f.is_file() and not f.is_symlink() else None
    return out


def make_record(spec_root: Path, sid: str) -> dict:
    """{spec_hash, spec_files} for step `sid` as the spec at spec_root defines it now (owned files per accview)."""
    try:
        raw = json.loads((spec_root / "steps.json").read_text())
    except (OSError, ValueError):
        raw = {}
    entry = raw_entry(raw, sid) if isinstance(raw, dict) else None
    if entry is None:
        return {}
    owned = sorted(accview.owned(raw, sid, accview.all_files(spec_root)))
    return {"spec_hash": step_hash(entry, read_files(spec_root, owned)), "spec_files": owned}


def check_record(spec_root: Path, sid: str, rec: dict) -> str:
    """'' when the step's entry and recorded files at spec_root still match rec, else a reason."""
    want = rec.get("spec_hash")
    if not isinstance(want, str) or not want:
        return ""
    files = [p for p in (rec.get("spec_files") or []) if isinstance(p, str)]
    try:
        raw = json.loads((spec_root / "steps.json").read_text())
    except (OSError, ValueError):
        return f"{sid}: steps.json unreadable"
    entry = raw_entry(raw, sid) if isinstance(raw, dict) else None
    if entry is None:
        return f"{sid}: no longer in steps.json"
    have = read_files(spec_root, files)
    now = accview.owned(raw, sid, accview.all_files(spec_root))
    extra = sorted(now - set(files))
    if extra:                                   # a new file its acceptance would run (its pattern matches it)
        return (f"{sid}: now owns acceptance file(s) it did not pass with: "
                + ", ".join(f"acceptance/{p}" for p in extra[:5]))
    if step_hash(entry, have) == want:
        return ""
    missing = [p for p, b in have.items() if b is None]
    if missing:
        return f"{sid}: owned acceptance file(s) removed: " + ", ".join(f"acceptance/{p}" for p in missing[:5])
    return f"{sid}: its steps.json entry or owned acceptance file(s) ({', '.join('acceptance/' + p for p in files[:5]) or 'none'}) changed"


def baseline_key(base_sha: str, commands: list[str], hashes: dict[str, str]) -> str:
    """What a recorded baseline result is valid for: the base, the commands run, the passed steps' records."""
    return hashlib.sha256(_canon({"base": base_sha, "commands": commands, "hashes": hashes}).encode()).hexdigest()


def rerecord(state_data: dict, spec_root: Path, files_by_step: dict[str, list[str]], commit: str, rnd: int) -> list[str]:
    """Route B (0.6.0): re-record spec_hash/spec_files of exactly the passed steps whose acceptance files an approved
    amendment changed (`files_by_step`: step id -> the acceptance/<path>s it changed), as the spec at spec_root (the
    merged commit) defines them now. Returns one log line per step; raises RoundError when a step is not passed or
    is gone from the spec (nothing is changed then)."""
    steps = state_data.get("steps") or {}
    new = {}
    for sid in sorted(files_by_step):
        st = steps.get(sid)
        if not isinstance(st, dict) or st.get("status") != "passed":
            raise RoundError(f"{sid} is not a passed step in state.json; nothing re-recorded")
        rec = make_record(spec_root, sid)
        if not rec:
            raise RoundError(f"{sid} is not in the merged steps.json; nothing re-recorded")
        new[sid] = rec
    out = []
    for sid, rec in new.items():
        files = sorted(files_by_step[sid])
        steps[sid].update({**rec, "tests_amended": {"commit": commit, "round": rnd,
                                                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                    "files": files}})
        out.append(f"re-recorded spec_hash for {sid} after its tests changed in amendment C{rnd} "
                   f"({', '.join(files)}; now {rec['spec_hash'][:12]})")
    return out
