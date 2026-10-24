"""Spec repo: steps.json loading/validation, ref extraction, path globs."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

STEP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,40}$")
REF_LIMIT_CHARS = 40000      # per ref
REFS_TOTAL_LIMIT = 120000    # all refs for one step


class SpecError(Exception):
    pass


@dataclass
class Step:
    id: str
    title: str
    goal: str
    type: str = "code"                       # code | human
    refs: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    e2e: list[str] = field(default_factory=list)
    acceptance_files: list[str] = field(default_factory=list)   # extra files/dirs under acceptance/ this step owns
    max_attempts: int | None = None
    notes: str = ""
    skills: list[str] = field(default_factory=list)          # vendored/extra skills the coder should use (code steps)
    screenshots: list[str] = field(default_factory=list)     # routes screenshotted after acceptance passes (code steps)
    gate: str = ""                                           # directions | tweaks | prototype (human steps)


@dataclass
class Spec:
    root: Path
    commit: str
    allowed_paths: list[str]
    allow_text_paths: list[str]
    setup: list[str]
    regression: list[str]
    e2e_command: str
    always_refs: list[str]
    steps: list[Step]
    pins: dict = field(default_factory=dict)       # npm package -> exact version (checked for release age)
    tests_changed: list[dict] = field(default_factory=list)   # amendment: existing acceptance files changed/removed

    def step(self, sid: str) -> Step:
        for s in self.steps:
            if s.id == sid:
                return s
        raise KeyError(sid)


STEP_KEYS = {"id", "title", "goal", "type", "refs", "depends_on", "acceptance", "e2e", "acceptance_files",
             "max_attempts", "notes", "skills", "screenshots", "gate"}
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SHOT_ROUTE = re.compile(r"^/[A-Za-z0-9/_.-]{0,100}$")
GATES = ("directions", "tweaks", "prototype")
TOP_KEYS = {"version", "project", "allowed_paths", "allow_text_paths", "setup", "regression", "e2e_command", "always_refs",
            "steps", "pins", "tests_changed"}
TEST_CHANGE = re.compile(r"^acceptance/[A-Za-z0-9._/@+-]+$")
NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _strlist(v, where: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise SpecError(f"{where}: must be a list of non-empty strings")
    return list(v)


def _checked_list(v, where: str, pattern: re.Pattern, limit: int, what: str) -> list[str]:
    out = _strlist(v, where)
    if len(out) > limit or any(not pattern.match(x) for x in out):
        raise SpecError(f"{where}: at most {limit} {what}")
    return out


def _pins(v) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict) or len(v) > 200:
        raise SpecError("pins: must be an object of npm package name -> exact version")
    for k, ver in v.items():
        if not isinstance(k, str) or not NPM_NAME.match(k) or not isinstance(ver, str) or not EXACT_VERSION.match(ver):
            raise SpecError(f"pins: {k!r}: {ver!r} must be a valid npm name and an exact version like 1.2.3")
    return dict(v)


def _tests_changed(v) -> list[dict]:
    """[{"path": "acceptance/<file>", "change": "change"|"remove", "why": "..."}]: the machine-readable list of
    existing acceptance files an amendment changes or removes (checked against the real diff by the planner)."""
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 500:
        raise SpecError("tests_changed: must be a list of {path, change, why} objects")
    out, seen = [], set()
    for i, x in enumerate(v):
        if not isinstance(x, dict) or set(x) - {"path", "change", "why"}:
            raise SpecError(f"tests_changed[{i}]: must be an object with path, change and (optional) why")
        path, change, why = x.get("path"), x.get("change"), x.get("why", "")
        if not isinstance(path, str) or not TEST_CHANGE.match(path) or ".." in path.split("/"):
            raise SpecError(f"tests_changed[{i}]: path must be acceptance/<file>")
        if change not in ("change", "remove"):
            raise SpecError(f"tests_changed[{i}]: change must be \"change\" or \"remove\"")
        if not isinstance(why, str) or len(why) > 400:
            raise SpecError(f"tests_changed[{i}]: why must be a string of at most 400 characters")
        if path in seen:
            raise SpecError(f"tests_changed: {path} listed twice")
        seen.add(path)
        out.append({"path": path, "change": change, "why": why})
    return out


def load(root: Path, commit: str = "") -> Spec:
    f = root / "steps.json"
    if not f.is_file():
        raise SpecError(f"{f} missing")
    try:
        raw = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise SpecError(f"steps.json does not parse: {e}") from e
    if not isinstance(raw, dict):
        raise SpecError("steps.json: top level must be an object")
    bad = set(raw) - TOP_KEYS
    if bad:
        raise SpecError(f"steps.json: unknown top-level keys {sorted(bad)}")
    if raw.get("version") != 1:
        raise SpecError("steps.json: version must be 1")
    allowed = _strlist(raw.get("allowed_paths"), "allowed_paths")
    if not allowed:
        raise SpecError("allowed_paths: at least one entry required")
    for pat in allowed:
        if pat.startswith("/") or ".." in pat.split("/"):
            raise SpecError(f"allowed_paths: bad pattern {pat!r}")
    steps: list[Step] = []
    seen: set[str] = set()
    for i, s in enumerate(raw.get("steps") or []):
        where = f"steps[{i}]"
        if not isinstance(s, dict):
            raise SpecError(f"{where}: must be an object")
        bad = set(s) - STEP_KEYS
        if bad:
            raise SpecError(f"{where}: unknown keys {sorted(bad)}")
        sid = s.get("id", "")
        if not isinstance(sid, str) or not STEP_ID.match(sid):
            raise SpecError(f"{where}: bad id {sid!r}")
        if sid in seen:
            raise SpecError(f"duplicate step id {sid}")
        seen.add(sid)
        for k in ("title", "goal"):
            if not isinstance(s.get(k), str) or not s[k].strip():
                raise SpecError(f"{sid}: '{k}' required")
        typ = s.get("type", "code")
        if typ not in ("code", "human"):
            raise SpecError(f"{sid}: type must be code or human")
        st = Step(
            id=sid, title=s["title"].strip(), goal=s["goal"].strip(), type=typ,
            refs=_strlist(s.get("refs"), f"{sid}.refs"),
            depends_on=_strlist(s.get("depends_on"), f"{sid}.depends_on"),
            acceptance=_strlist(s.get("acceptance"), f"{sid}.acceptance"),
            e2e=_strlist(s.get("e2e"), f"{sid}.e2e"),
            acceptance_files=_strlist(s.get("acceptance_files"), f"{sid}.acceptance_files"),
            max_attempts=s.get("max_attempts"),
            notes=s.get("notes", "") or "",
            skills=_checked_list(s.get("skills"), f"{sid}.skills", SKILL_NAME, 8, "skill names like 'hallmark'"),
            screenshots=_checked_list(s.get("screenshots"), f"{sid}.screenshots", SHOT_ROUTE, 12,
                                      "routes like '/directions/1'"),
            gate=s.get("gate", "") or "",
        )
        if typ != "code" and (st.skills or st.screenshots):
            raise SpecError(f"{sid}: skills and screenshots are for code steps only")
        if st.gate and (typ != "human" or st.gate not in GATES):
            raise SpecError(f"{sid}: gate must be one of {', '.join(GATES)} and is for human steps only")
        if st.gate and not any(x.screenshots for x in steps):
            raise SpecError(f"{sid}: a gate step must come after a code step with screenshots")
        if st.max_attempts is not None and (not isinstance(st.max_attempts, int) or not 1 <= st.max_attempts <= 10):
            raise SpecError(f"{sid}: max_attempts must be 1..10")
        if typ == "code" and not st.acceptance and not st.e2e:
            raise SpecError(f"{sid}: code steps need at least one acceptance command or e2e spec")
        for d in st.depends_on:
            if d not in seen or d == sid:
                raise SpecError(f"{sid}: depends_on {d!r} must name an EARLIER step")
        for e in st.e2e:
            if not (root / "acceptance" / e).is_file():
                raise SpecError(f"{sid}: e2e spec acceptance/{e} not found")
        for a in st.acceptance_files:
            if a.strip("/") in ("", ".") or a.startswith("/") or ".." in a.split("/") or not (root / "acceptance" / a).exists():
                raise SpecError(f"{sid}: acceptance_files entry acceptance/{a} not found")
        steps.append(st)
    if not steps:
        raise SpecError("steps.json: no steps")
    spec = Spec(
        root=root, commit=commit, allowed_paths=allowed,
        allow_text_paths=_strlist(raw.get("allow_text_paths"), "allow_text_paths"),
        setup=_strlist(raw.get("setup"), "setup"),
        regression=_strlist(raw.get("regression"), "regression"),
        e2e_command=raw.get("e2e_command", "") or "",
        always_refs=_strlist(raw.get("always_refs"), "always_refs"),
        steps=steps,
        pins=_pins(raw.get("pins")),
        tests_changed=_tests_changed(raw.get("tests_changed")),
    )
    if any(s.e2e for s in steps) and "{files}" not in spec.e2e_command:
        raise SpecError("e2e_command with a {files} placeholder is required when any step lists e2e specs")
    for s in steps:
        for r in s.refs:
            resolve_ref(root, r)  # raises on missing file / anchor
    for r in spec.always_refs:
        resolve_ref(root, r)
    from . import accview
    accview.validate(spec)
    return spec


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="python3 -m hands.spec")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="load and validate DIR/steps.json (exit 1 with the error)")
    p.add_argument("dir", type=Path)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        spec = load(a.dir)
    except SpecError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"ok": True, "steps": [
            {"id": s.id, "type": s.type, "skills": s.skills, "screenshots": s.screenshots, "gate": s.gate}
            for s in spec.steps]}))
    else:
        print(f"ok: {len(spec.steps)} steps")
    return 0


# ------------------------------------------------------------------ refs
def slug(heading: str) -> str:
    h = heading.strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return re.sub(r" ", "-", h)


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def resolve_ref(root: Path, ref: str) -> str:
    path, _, anchor = ref.partition("#")
    if path.startswith("/") or ".." in Path(path).parts:
        raise SpecError(f"ref {ref!r}: must be a relative path inside the spec repo")
    f = root / path
    if not f.is_file():
        raise SpecError(f"ref {ref!r}: file not found")
    text = f.read_text(errors="replace")
    if not anchor:
        return text
    lines = text.splitlines()
    in_code = False
    start = level = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        lv = len(m.group(1))
        if start is None:
            if slug(m.group(2)) == anchor:
                start, level = i, lv
        elif lv <= level:
            return "\n".join(lines[start:i])
    if start is None:
        raise SpecError(f"ref {ref!r}: heading #{anchor} not found")
    return "\n".join(lines[start:])


def render_refs(spec: Spec, step: Step) -> str:
    out, total = [], 0
    for r in [*spec.always_refs, *[x for x in step.refs if x not in spec.always_refs]]:
        body = resolve_ref(spec.root, r)
        if len(body) > REF_LIMIT_CHARS:
            body = body[:REF_LIMIT_CHARS] + f"\n[... truncated at {REF_LIMIT_CHARS} chars; split {r} into smaller sections]"
        chunk = f'<ref path="{r}">\n{body}\n</ref>'
        if total + len(chunk) > REFS_TOTAL_LIMIT:
            out.append(f'<ref path="{r}">[omitted: total ref size limit reached]</ref>')
            continue
        total += len(chunk)
        out.append(chunk)
    return "\n\n".join(out)


# ------------------------------------------------------------------ globs
def glob_to_regex(pat: str) -> re.Pattern:
    if pat.endswith("/"):
        pat += "**"
    i, out = 0, []
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def path_allowed(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


if __name__ == "__main__":
    import sys
    sys.exit(main())
