"""Skill, plugin and tool usage of one `claude -p --output-format stream-json --verbose` session.

summarize(lines) reads the transcript and counts every tool_use block in assistant messages by name,
including Skill itself; it lists the skills (Skill tool uses, in order of first use), plugins (the
system/init line plus the plugin part of plugin:skill names), MCP servers (mcp__<server>__<tool> names),
subagents (Task/Agent subagent_type) and what the session had available (tools and mcp_servers from the
system/init line). Malformed or truncated lines are counted in skipped_lines, never fatal.

check(summary, role, required) returns the role's required skills that were not used (a warning, not a
failure: the runner logs it, the dashboard shows a badge). write_tools_json() writes summary + missing
next to a transcript as tools.json after every session.

Everything here is agent-influenced text (tool names, skill names): the values are only ever counted,
truncated and stored as plain strings.

CLI: python3 -m hands.toolusage summarize FILE [--json]
     python3 -m hands.toolusage check FILE --role R [--required FILE] [--step-skills A B] [--json]   (prints {"missing": [...]})
Exit codes: 0 ok (missing skills are not an error), 2 unreadable transcript or required-skills file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

MAX_LINE = 5_000_000          # same cap as hands.activity / hands.claude
MAX_NAME = 200
REQUIRED_FILE = Path(__file__).resolve().parent.parent / "prompts" / "required-skills.json"
SUBAGENT_TOOLS = ("Task", "Agent")
MCP_PREFIX = "mcp__"


def _name(v) -> str:
    """A tool/skill/plugin/server name as a short plain string, or "" when it is not a string."""
    if not isinstance(v, str):
        return ""
    return " ".join(v.split())[:MAX_NAME]


def _add(seq: list[str], name: str) -> None:
    if name and name not in seq:
        seq.append(name)


def _plugin_of(skill: str) -> str:
    """"superpowers" for "superpowers:writing-plans", "" for a bare skill name."""
    plugin, sep, rest = skill.partition(":")
    return plugin.strip() if sep and plugin.strip() and rest.strip() else ""


def _mcp_server(tool: str) -> str:
    """"github" for "mcp__github__get_issue", "" for anything else."""
    if not tool.startswith(MCP_PREFIX):
        return ""
    server, sep, _ = tool[len(MCP_PREFIX):].partition("__")
    return server if sep and server else ""


def _decode(line) -> dict | None:
    """One transcript line as a dict; {} for valid JSON of another shape or a blank line (ignored);
    None for a malformed, truncated or oversized line (skipped)."""
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("utf-8", errors="replace")
    s = str(line).strip()
    if not s:
        return {}
    if len(s) > MAX_LINE:
        return None
    try:
        ev = json.loads(s)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else {}


def _init(ev: dict, out: dict) -> None:
    tools, servers, plugins = ev.get("tools"), ev.get("mcp_servers"), ev.get("plugins")
    for t in tools if isinstance(tools, list) else []:
        _add(out["available"]["tools"], _name(t))
    for s in servers if isinstance(servers, list) else []:
        _add(out["available"]["mcp_servers"], _name(s.get("name")) if isinstance(s, dict) else _name(s))
    for p in plugins if isinstance(plugins, list) else []:
        _add(out["plugins"], _name(p.get("name")) if isinstance(p, dict) else _name(p))


def _tool_use(block: dict, out: dict) -> None:
    name = _name(block.get("name"))
    if not name:
        return
    out["tools"][name] = out["tools"].get(name, 0) + 1
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    server = _mcp_server(name)
    if server:
        out["mcp"][server] = out["mcp"].get(server, 0) + 1
    if name == "Skill":
        skill = _name(inp.get("skill") or inp.get("name"))
        _add(out["skills"], skill)
        _add(out["plugins"], _plugin_of(skill))
    elif name in SUBAGENT_TOOLS:
        _add(out["subagents"], _name(inp.get("subagent_type")))


def summarize(lines: Iterable[str | bytes]) -> dict:
    """Usage summary of a stream-json transcript given as lines (str or bytes, newline optional)."""
    out: dict = {"tools": {}, "skills": [], "plugins": [], "mcp": {}, "subagents": [],
                 "available": {"tools": [], "mcp_servers": []}, "skipped_lines": 0}
    for line in lines:
        ev = _decode(line)
        if ev is None:
            out["skipped_lines"] += 1
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            _init(ev, out)
        elif ev.get("type") == "assistant":
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    _tool_use(block, out)
    return out


def summarize_file(path: Path) -> dict:
    """summarize() over a transcript file; an unreadable file summarizes as empty."""
    try:
        with open(path, "rb") as fh:
            return summarize(fh)
    except OSError:
        return summarize([])


def check(summary: dict, role: str, required: dict) -> list[str]:
    """The role's required skills that the session did not use. A bare required name such as
    "writing-plans" is satisfied by a plugin-qualified use such as "superpowers:writing-plans"."""
    wanted = required.get(role) if isinstance(required, dict) else None
    if not isinstance(wanted, list):
        return []
    skills = summary.get("skills") if isinstance(summary, dict) else None
    used: set[str] = set()
    for s in skills if isinstance(skills, list) else []:
        if isinstance(s, str):
            used.add(s)
            if _plugin_of(s):
                used.add(s.partition(":")[2])
    return [s for s in wanted if isinstance(s, str) and s not in used]


def with_step_skills(required: dict, role: str, extra: list[str] | None) -> dict:
    """`required` with the step's own skills appended to `role`'s list (no duplicates)."""
    if not extra:
        return required
    merged = dict(required)
    merged[role] = list(dict.fromkeys([*(required.get(role) or []), *extra]))
    return merged


def load_required(path: Path = REQUIRED_FILE) -> dict:
    """{role: [skill, ...]} from a required-skills JSON file; {} when unreadable or not an object."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [s for s in v if isinstance(s, str)] for k, v in data.items() if isinstance(v, list)}


def write_tools_json(transcript: Path, role: str, dest: Path, required: dict | None = None,
                     step_skills: list[str] | None = None) -> dict:
    """Summarize `transcript` for `role`, write summary + role + missing to `dest` (atomically, 0644) and
    return it. Never raises on a bad transcript; a write failure propagates (the caller owns the directory)."""
    req = with_step_skills(load_required() if required is None else required, role, step_skills)
    out = summarize_file(Path(transcript))
    out["role"] = role
    out["missing"] = check(out, role, req)
    dest = Path(dest)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(out, fh, indent=1)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


def _counts(d: dict) -> str:
    return ", ".join(f"{k}×{v}" for k, v in d.items()) or "-"


def _text(summary: dict) -> str:
    lines = ["tools: " + _counts(summary["tools"]),
             "skills: " + (", ".join(summary["skills"]) or "-"),
             "plugins: " + (", ".join(summary["plugins"]) or "-"),
             "mcp: " + _counts(summary["mcp"]),
             "subagents: " + (", ".join(summary["subagents"]) or "-"),
             "available tools: " + (", ".join(summary["available"]["tools"]) or "-"),
             "available mcp servers: " + (", ".join(summary["available"]["mcp_servers"]) or "-"),
             f"skipped lines: {summary['skipped_lines']}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.toolusage", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("summarize", help="tools, skills, plugins, MCP servers and subagents used")
    p.add_argument("file", type=Path)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="required skills a role's session did not use (exit 0 either way)")
    p.add_argument("file", type=Path)
    p.add_argument("--role", required=True)
    p.add_argument("--required", type=Path, default=REQUIRED_FILE, help="required-skills JSON (default: prompts/)")
    p.add_argument("--step-skills", nargs="*", default=[], metavar="SKILL", help="extra skills required for this step")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        with open(a.file, "rb") as fh:
            summary = summarize(fh)
    except OSError as e:
        print(f"cannot read {a.file}: {e}", file=sys.stderr)
        return 2
    if a.cmd == "summarize":
        print(json.dumps(summary, indent=1) if a.json else _text(summary), end="\n" if a.json else "")
        return 0
    try:
        required = json.loads(Path(a.required).read_text())
    except (OSError, ValueError) as e:
        print(f"cannot read {a.required}: {e}", file=sys.stderr)
        return 2
    req = load_required(a.required) if isinstance(required, dict) else {}
    missing = check(summary, a.role, with_step_skills(req, a.role, a.step_skills))
    if a.json:
        print(json.dumps({"missing": missing}))
    else:
        print(f"{a.role}: " + ("all required skills used" if not missing else "required skills not used: " + ", ".join(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
