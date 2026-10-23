"""Plan with Claude: the planning chat behind a project's brief (docs/design.md, "Plan with Claude").

The chat is an interactive `claude` session in the Session manager terminal, sandboxed like the build agents:
the hands image, the firewalled network profile the caller passes in verbatim, the spec repo and the app code
mounted read-only, one read-write `brief` directory the session writes `brief.md` and `coverage.md` into, a
per-project `~/.claude` home (so the conversation persists and `--resume` works) and the skills read-only.
Its own one-line `allowed-domains` file is mounted read-only at /etc/hands/allowed-domains (the firewall's
built-in allowlist has no platform.claude.com, which interactive Claude Code needs: its first-run setup check
probes api.anthropic.com and platform.claude.com, and OAuth token exchange and refresh go to that host too).
Its env file holds exactly one variable, ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN (whichever the project's
build token file carries); nothing else from the secrets file reaches the container, and neither project.env nor
any git credential is mounted.

`primer` is the first user message (the planning-interviewer job, the context, the read-only rules and the
brief format); `launch_spec` returns the `docker run` argv plus the session record as data (only the existing
launcher executes containers); `brief_hash`/`status` read the brief directory; `approve` copies a hash-pinned
brief into the spec repo and commits it. The session uuid is created once per project (first start) and
stored by the caller in the project's `plan_session` state.

A stored session id alone does not mean there is a conversation to resume: a first launch that died before
Claude wrote its transcript (the connect check, a missing token) leaves the id behind. `launch_spec` therefore
asks the chat's home for the transcript (`has_transcript`) and sends the primer again when there is none,
instead of resuming an empty session.

CLI: python3 -m hands.planchat <primer|launch-spec|hash|status|approve>
Exit codes: 0 ok, 1 invalid input (unreadable JSON, missing brief, git failure), 4 neither ANTHROPIC_API_KEY nor
CLAUDE_CODE_OAUTH_TOKEN in the secrets file, 5 approve refused (`hash mismatch` / `coverage.md missing`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .gitops import GitError, git
from .setup import FRAMEWORK_LABELS

ROLE = "plan-chat"
KINDS = ("new", "existing")
STATUSES = ("none", "open", "brief_ready", "approved")
CONTAINER_KEY = "ANTHROPIC_API_KEY"
OAUTH_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_KEYS = (CONTAINER_KEY, OAUTH_KEY)   # either unlocks the chat; whichever is found is the one passed
SPEC_MOUNT = "/spec"
APP_MOUNT = "/app"
BRIEF_MOUNT = "/brief"
HOME_MOUNT = "/home/node/.claude"
SKILLS_MOUNT = "/home/node/.claude/skills"
PROJECTS_DIR = "projects"   # Claude Code keeps transcripts in <its home>/projects/<escaped cwd>/<session>.jsonl
DOMAINS_MOUNT = "/etc/hands/allowed-domains"
CHAT_DOMAINS = ("platform.claude.com",)   # the chat's whole per-run egress list, on top of the image allowlist
BRIEF_FILE = "brief.md"
COVERAGE_FILE = "coverage.md"
BRIEF_HEADINGS = ("## Idea", "## Must have", "## Nice to have", "## Out of scope", "## Style and motion",
                  "## Setup", "## Open decisions")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class HashMismatch(Exception):
    """The approval carries a different sha256 than the brief on disk (the brief changed since Alex read it)."""


class BriefIncomplete(Exception):
    """brief.md or coverage.md is missing from the brief directory."""


class TokenMissing(Exception):
    def __init__(self, message: str = f"{' or '.join(TOKEN_KEYS)} missing from the secrets file"):
        super().__init__(message)


# -------------------------------------------------------------------------------------------- data

def _opt_str(raw: dict, key: str, default: str | None = None) -> str | None:
    value = raw.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


@dataclass
class PlanContext:
    """What the planning chat is told about the project (JSON from the dashboard / project state)."""
    slug: str
    kind: str                                   # new | existing
    description: str = ""
    style_refs: list[dict] = field(default_factory=list)   # [{url, report_path}]
    setup: dict | None = None                   # {framework, libs, record} or None
    spec_dir: str | None = SPEC_MOUNT           # container path of the read-only spec repo, None when absent
    app_dir: str | None = None                  # container path of the read-only app code, None for new projects
    brief_dir: str = BRIEF_MOUNT
    skills_dir: str = SKILLS_MOUNT

    @classmethod
    def from_json(cls, raw: Any) -> "PlanContext":
        if not isinstance(raw, dict):
            raise ValueError("context must be an object")
        slug = raw.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise ValueError("slug must be a lower-case name (letters, digits, dashes)")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        refs = raw.get("style_refs", [])
        if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
            raise ValueError("style_refs must be a list of objects")
        setup = raw.get("setup")
        if setup is not None:
            if not isinstance(setup, dict) or setup.get("framework") not in FRAMEWORK_LABELS:
                raise ValueError(f"setup.framework must be one of {sorted(FRAMEWORK_LABELS)}")
            libs = setup.get("libs", [])
            if not isinstance(libs, list) or not all(isinstance(x, str) for x in libs):
                raise ValueError("setup.libs must be a list of strings")
        return cls(slug=slug, kind=kind, description=_opt_str(raw, "description", "") or "",
                   style_refs=[{"url": str(r.get("url", "")), "report_path": str(r.get("report_path", ""))} for r in refs],
                   setup=setup, spec_dir=_opt_str(raw, "spec_dir", SPEC_MOUNT), app_dir=_opt_str(raw, "app_dir"),
                   brief_dir=_opt_str(raw, "brief_dir", BRIEF_MOUNT) or BRIEF_MOUNT,
                   skills_dir=_opt_str(raw, "skills_dir", SKILLS_MOUNT) or SKILLS_MOUNT)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlanSession:
    """The project's `plan_session` state as the chat needs it."""
    session_id: str | None
    status: str                                 # none | open | brief_ready | approved
    brief_sha256: str | None

    @classmethod
    def from_json(cls, raw: Any) -> "PlanSession":
        if not isinstance(raw, dict):
            raise ValueError("state must be an object")
        status = raw.get("status") or "none"
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        return cls(session_id=_opt_str(raw, "session_id"), status=status, brief_sha256=_opt_str(raw, "brief_sha256"))


@dataclass
class Paths:
    """Host paths for the launch: None for spec_dir/app_dir means no such mount (new project)."""
    spec_dir: Path | None
    app_dir: Path | None
    brief_dir: Path
    home_dir: Path
    skills_dir: Path
    env_dir: Path
    secrets_file: Path

    @classmethod
    def from_json(cls, raw: Any) -> "Paths":
        if not isinstance(raw, dict):
            raise ValueError("paths must be an object")
        def path(key: str, optional: bool = False) -> Path | None:
            value = raw.get(key)
            if value is None and optional:
                return None
            if not isinstance(value, str) or not value:
                raise ValueError(f"paths.{key} must be a path")
            return Path(value)
        return cls(spec_dir=path("spec_dir", optional=True), app_dir=path("app_dir", optional=True),
                   brief_dir=path("brief_dir"), home_dir=path("home_dir"), skills_dir=path("skills_dir"),
                   env_dir=path("env_dir"), secrets_file=path("secrets_file"))


# ------------------------------------------------------------------------------------------ primer

def setup_summary(setup: dict | None) -> str | None:
    """`Astro + Tailwind, libs: gsap` from a setup record; None when no setup is chosen."""
    if not setup:
        return None
    label = FRAMEWORK_LABELS.get(setup.get("framework"))
    if label is None:
        return None
    libs = [str(x) for x in setup.get("libs") or []]
    text = label + (f", libs: {', '.join(libs)}" if libs else ", no extra libraries")
    if isinstance(setup.get("record"), dict):
        versions = setup["record"].get("versions")
        if isinstance(versions, dict) and versions:
            text += " (scaffolded: " + ", ".join(f"{k} {v}" for k, v in sorted(versions.items())) + ")"
    return text


def primer(ctx: PlanContext) -> str:
    """The first user message of the planning chat."""
    brief = ctx.brief_dir.rstrip("/") or BRIEF_MOUNT
    brief_md, coverage_md = f"{brief}/{BRIEF_FILE}", f"{brief}/{COVERAGE_FILE}"
    lines = [
        f"You are the planning interviewer for Phoenix project `{ctx.slug}`. Your job is to turn the idea below "
        "into an approved brief through a conversation with the owner. You do not build anything.",
        "",
        "## Context",
        f"- Project: `{ctx.slug}` ({'an existing app' if ctx.kind == 'existing' else 'a new project'})",
        f"- Idea so far: {ctx.description.strip() or '(none yet; ask for it first)'}",
    ]
    if ctx.style_refs:
        lines.append("- Style references (frozen analyst reports, read them before asking about look and feel):")
        for ref in ctx.style_refs:
            url, report = ref.get("url", ""), ref.get("report_path", "")
            lines.append(f"  - {url}" + (f" -> {report}" if report else ""))
    else:
        lines.append("- Style references: none frozen yet")
    summary = setup_summary(ctx.setup)
    lines.append(f"- Setup: {summary}" if summary else "- Setup: not chosen yet (ask which framework and libraries)")
    if ctx.spec_dir:
        lines.append(f"- Spec repo: `{ctx.spec_dir}` (read-only): the project description, refs/ "
                     "(style refs, setup.json) and any earlier brief or briefs/change-*.md live there.")
    if ctx.app_dir:
        lines.append(f"- App code: `{ctx.app_dir}` (read-only): read it to understand what exists before "
                     "proposing changes.")
    lines.append(f"- Brief directory: `{brief}` (read-write): the only place you write.")
    lines.append(f"- Skills: `{ctx.skills_dir}` (read-only).")
    lines += [
        "",
        "## Rules",
        "- Everything outside the brief directory is read-only: never write the spec repo, never edit the app "
        "code, never run git push, never run installs or builds. You have no credentials and need none.",
        "- Never record secrets, tokens or personal data in the brief.",
        "- Use the brainstorming skill in full Q&A mode: one question per message, play back what you heard "
        "before moving on, and size the work (small / medium / large) with the owner before you finish.",
        "- Keep questions concrete and ordered: the idea and who it is for, must-haves, nice-to-haves, what is "
        "out of scope, style and motion (lean on the style references), setup, then the open decisions.",
        "",
        "## Output",
        f"When the owner agrees the plan is complete, write `{brief_md}` with exactly these headings, in this order:",
        f"- `# Brief: {ctx.slug}`",
    ]
    lines += [f"- `{heading}`" for heading in BRIEF_HEADINGS]
    lines += [
        f"Then write `{coverage_md}`: a Markdown table with the columns `Requirement` and `Where` that maps every "
        "requirement the owner stated to the brief section that covers it (or `Out of scope`).",
        "Both files are reviewed and approved by the owner on the dashboard; they are not the spec. Do not write "
        "anything else. Start by introducing yourself in one sentence and asking the first question.",
    ]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------- launch

def _host_path(p: Path | str) -> str:
    """Absolute host path as a string (normalised, symlinks kept: docker binds the path as given)."""
    s = os.path.abspath(str(p))
    if ":" in s or "," in s:
        raise ValueError(f"host path with ':' or ',' cannot be bind-mounted: {s}")
    return s


def read_secrets(path: Path) -> dict[str, str]:
    """KEY=value lines (project.env format: value verbatim after the first '=', '#' lines skipped)."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        return values
    for raw in text.split("\n"):
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        key, eq, value = line.partition("=")
        if eq and re.match(r"^[A-Z_][A-Z0-9_]*$", key):
            values[key] = value.rstrip("\r")
    return values


def token_in(secrets_file: Path) -> tuple[str, str] | None:
    """The first usable (key, token) pair from the secrets file, or None when neither key carries one.
    ANTHROPIC_API_KEY wins over CLAUDE_CODE_OAUTH_TOKEN when the file holds both."""
    values = read_secrets(secrets_file)
    for key in TOKEN_KEYS:
        token = values.get(key, "").strip()
        if token and "\n" not in token and "\0" not in token:
            return key, token
    return None


def build_token(secrets_file: Path) -> tuple[str, str]:
    """(key, token) for the container's one env var; TokenMissing when the file carries neither key."""
    found = token_in(secrets_file)
    if found is None:
        raise TokenMissing()
    return found


def write_env_file(env_dir: Path, token: str, slug: str, key: str = CONTAINER_KEY) -> Path:
    """A private file under env_dir holding exactly <key>=<token> and nothing else; the caller deletes it."""
    if key not in TOKEN_KEYS:
        raise ValueError(f"env key must be one of {TOKEN_KEYS}")
    env_dir = Path(env_dir)
    env_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"planchat-{slug}-", suffix=".env", dir=env_dir)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"{key}={token}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return Path(name)


def write_domains_file(env_dir: Path, slug: str, domains: tuple[str, ...] = CHAT_DOMAINS) -> Path:
    """A file under env_dir holding the chat's extra egress hosts, one per line, for the firewall to read
    (mounted at DOMAINS_MOUNT, as hands.setup does for the setup containers); the caller deletes it."""
    env_dir = Path(env_dir)
    env_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"planchat-{slug}-", suffix=".domains", dir=env_dir)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, "".join(f"{h}\n" for h in domains).encode("utf-8"))
    finally:
        os.close(fd)
    return Path(name)


def container_name(slug: str) -> str:
    """The chat's container name: one per project, so a reset knows what to remove."""
    return f"hands-{slug}-{ROLE}"


def new_session_id() -> str:
    return str(uuid.uuid4())


def transcript_files(home_dir: Path | str, session_id: str | None) -> list[Path]:
    """The chat's transcripts for `session_id` inside its own `~/.claude` (the home_dir mount): Claude Code
    writes <home>/projects/<cwd with separators replaced>/<session id>.jsonl. The cwd folder is globbed, so
    this does not depend on how Claude escapes the working directory."""
    if not session_id or not UUID_RE.match(session_id):
        return []
    try:
        return sorted(p for p in (Path(home_dir) / PROJECTS_DIR).glob(f"*/{session_id}.jsonl") if p.is_file())
    except OSError:
        return []


def has_transcript(home_dir: Path | str, session_id: str | None) -> bool:
    """True when `claude --resume <session_id>` would find a conversation in this home."""
    return bool(transcript_files(home_dir, session_id))


def launch_spec(ctx: PlanContext, state: PlanSession, paths: Paths, network_args: list[str],
                image: str = "hands") -> dict:
    """`docker run` argv for the planning chat plus its session record: {argv, session, session_id, temp_files}.
    `temp_files` are the per-run files the argv points at (the token env file and the allowed-domains file);
    the caller deletes them when the terminal ends.
    First start (and any later start with no transcript for the stored id): `claude --session-id <uuid4>
    <primer>`; once the chat has a transcript: `claude --resume <uuid>`."""
    if not isinstance(network_args, list) or not all(isinstance(a, str) for a in network_args):
        raise ValueError("network_args must be a list of strings")
    session_id = state.session_id
    if session_id is not None and not UUID_RE.match(session_id):
        raise ValueError("stored session_id is not a uuid")
    token_key, token = build_token(paths.secrets_file)
    mounts = []
    if paths.spec_dir and ctx.spec_dir:
        mounts.append(f"{_host_path(paths.spec_dir)}:{SPEC_MOUNT}:ro")
    if paths.app_dir and ctx.app_dir:
        mounts.append(f"{_host_path(paths.app_dir)}:{APP_MOUNT}:ro")
    mounts += [f"{_host_path(paths.brief_dir)}:{BRIEF_MOUNT}:rw",
               f"{_host_path(paths.home_dir)}:{HOME_MOUNT}:rw",
               f"{_host_path(paths.skills_dir)}:{SKILLS_MOUNT}:ro"]
    env_file = write_env_file(paths.env_dir, token, ctx.slug, token_key)
    domains_file = write_domains_file(paths.env_dir, ctx.slug)
    mounts.append(f"{_host_path(domains_file)}:{DOMAINS_MOUNT}:ro")
    argv = ["docker", "run", "--rm", "-it", "--init",
            "--name", container_name(ctx.slug),
            "--label", f"hands.project={ctx.slug}", "--label", f"hands.role={ROLE}",
            *network_args,
            "--security-opt", "no-new-privileges",
            "--shm-size", "1g", "--pids-limit", "512",
            "-e", f"HANDS_ROLE={ROLE}",
            "--env-file", str(env_file)]
    for mount in mounts:
        argv += ["-v", mount]
    argv += ["-w", BRIEF_MOUNT, image]
    if session_id is None:
        session_id = new_session_id()
    resumed = has_transcript(paths.home_dir, session_id)
    if resumed:
        argv += ["claude", "--resume", session_id]
    else:                                       # no conversation on disk: start this id with the primer
        argv += ["claude", "--session-id", session_id, primer(ctx)]
    return {"argv": argv, "session": {"project": ctx.slug, "role": ROLE, "hidden": True}, "session_id": session_id,
            "resumed": resumed, "temp_files": [str(env_file), str(domains_file)]}


# ------------------------------------------------------------------------------------ brief state

def brief_hash(brief_dir: Path) -> str:
    """sha256 (hex) of <brief_dir>/brief.md; BriefIncomplete when it does not exist."""
    path = Path(brief_dir) / BRIEF_FILE
    if not path.is_file():
        raise BriefIncomplete(f"{BRIEF_FILE} missing")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status(brief_dir: Path) -> str:
    """`brief_ready` when brief.md and coverage.md both exist, else `open`."""
    d = Path(brief_dir)
    return "brief_ready" if (d / BRIEF_FILE).is_file() and (d / COVERAGE_FILE).is_file() else "open"


def plan_session_record(session_id: str | None, brief_dir: Path, brief_sha256: str | None = None,
                        updated: str = "") -> dict:
    """The project's `plan_session` entry for state.json; `approved` when a brief_sha256 is passed."""
    if brief_sha256:
        state = "approved"
    elif session_id is None:
        state = "none"
    else:
        state = status(brief_dir)
    return {"session_id": session_id, "status": state, "brief_sha256": brief_sha256, "updated": updated}


# ---------------------------------------------------------------------------------------- approve

def _targets(round: int | None) -> tuple[str, str]:
    if round is None:
        return BRIEF_FILE, f"refs/{COVERAGE_FILE}"
    if isinstance(round, bool) or not isinstance(round, int) or round < 1:
        raise ValueError("round must be a positive integer")
    return f"briefs/change-{round}.md", f"refs/coverage-change-{round}.md"


def approve(brief_dir: Path, spec_dir: Path, sha: str, round: int | None = None) -> dict:
    """Copy a hash-pinned brief into the spec repo and commit `brief: approved <sha[:12]>`.
    Raises BriefIncomplete (brief.md / coverage.md missing), HashMismatch (sha differs from the brief on disk),
    ValueError (bad round) and GitError."""
    brief_dir, spec_dir = Path(brief_dir), Path(spec_dir)
    brief_rel, coverage_rel = _targets(round)
    if not (brief_dir / BRIEF_FILE).is_file():
        raise BriefIncomplete(f"{BRIEF_FILE} missing")
    if not (brief_dir / COVERAGE_FILE).is_file():
        raise BriefIncomplete(f"{COVERAGE_FILE} missing")
    current = brief_hash(brief_dir)
    if not isinstance(sha, str) or sha.strip().lower() != current:
        raise HashMismatch(f"hash mismatch: brief.md is {current}")
    for rel, source in ((brief_rel, BRIEF_FILE), (coverage_rel, COVERAGE_FILE)):
        target = spec_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(brief_dir / source, target)
    (spec_dir / "brief.sha256").write_text(current + "\n", encoding="utf-8")
    changed = [brief_rel, coverage_rel, "brief.sha256"]
    git(spec_dir, "add", "-A", "--", *changed)
    if git(spec_dir, "status", "--porcelain", "--", *changed).strip():
        git(spec_dir, "commit", "-q", "-m", f"brief: approved {current[:12]}", "--", *changed)
    return {"sha256": current, "round": round, "brief_path": brief_rel, "coverage_path": coverage_rel,
            "commit": git(spec_dir, "rev-parse", "HEAD").strip()}


# -------------------------------------------------------------------------------------------- CLI

def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: {e}") from e


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.planchat", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("primer", help="print the first user message for a context JSON")
    p.add_argument("--ctx", required=True)
    p = sub.add_parser("launch-spec", help="docker run argv + session record for the planning chat")
    p.add_argument("--ctx", required=True)
    p.add_argument("--state", required=True, help="plan_session JSON: {session_id, status, brief_sha256}")
    p.add_argument("--paths", required=True, help="host paths JSON (spec_dir, app_dir, brief_dir, home_dir, "
                                                  "skills_dir, env_dir, secrets_file)")
    p.add_argument("--network-args", default="[]", help="JSON list of docker network flags, passed verbatim")
    p.add_argument("--image", default="hands")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("hash", help="sha256 of <brief-dir>/brief.md")
    p.add_argument("--brief-dir", required=True)
    p = sub.add_parser("status", help="brief_ready when brief.md and coverage.md exist, else open")
    p.add_argument("--brief-dir", required=True)
    p = sub.add_parser("approve", help="copy the hash-pinned brief into the spec repo and commit")
    p.add_argument("--brief-dir", required=True)
    p.add_argument("--spec-dir", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--round", type=int, default=None)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "primer":
            sys.stdout.write(primer(PlanContext.from_json(_load_json(a.ctx))))
        elif a.cmd == "launch-spec":
            try:
                network_args = json.loads(a.network_args)
            except ValueError as e:
                raise ValueError(f"--network-args: {e}") from e
            spec = launch_spec(PlanContext.from_json(_load_json(a.ctx)), PlanSession.from_json(_load_json(a.state)),
                               Paths.from_json(_load_json(a.paths)), network_args, image=a.image)
            if a.json:
                print(json.dumps(spec, indent=1))
            else:
                print(shlex.join(spec["argv"]))
        elif a.cmd == "hash":
            print(brief_hash(Path(a.brief_dir)))
        elif a.cmd == "status":
            print(status(Path(a.brief_dir)))
        elif a.cmd == "approve":
            result = approve(Path(a.brief_dir), Path(a.spec_dir), a.sha, round=a.round)
            print(f"approved {result['sha256'][:12]} -> {result['brief_path']} ({result['commit'][:12]})")
    except HashMismatch as e:
        print(str(e), file=sys.stderr)
        return 5
    except BriefIncomplete as e:
        print(str(e), file=sys.stderr)
        return 5 if a.cmd == "approve" else 1
    except TokenMissing as e:
        print(str(e), file=sys.stderr)
        return 4
    except (ValueError, GitError) as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
