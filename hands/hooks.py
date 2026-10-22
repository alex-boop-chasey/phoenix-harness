"""Claude Code hook settings for agent containers (docs/design.md, "Format and lint hooks for builders").

The coder gets one PostToolUse hook, hooks/format-on-write.mjs, which formats and lints every file an
Edit/Write touches. The harness writes the settings fragment into project state and mounts it read-only
at /home/node/.claude/settings.json next to the read-only hooks directory at /hands-hooks, so the agent
cannot change hook configuration. Every other role gets no hooks: empty settings, no mounts.

Everything here returns data (dicts, argv fragments); only the existing launcher (hands.docker) runs
containers.

CLI: python3 -m hands.hooks render --role R [--json]
     python3 -m hands.hooks mount-args --role R --hooks-dir D --settings-file F [--json]
Exit codes: 0 ok, 2 invalid arguments.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

HOOK_COMMAND = "node /hands-hooks/format-on-write.mjs"
HOOK_TIMEOUT_S = 120
HOOK_MATCHER = "Edit|Write"
HOOKS_MOUNT = "/hands-hooks"
SETTINGS_MOUNT = "/home/node/.claude/settings.json"
SETTINGS_FILE_NAME = "settings-coder.json"
HOOKED_ROLES = frozenset({"coder"})

_CODER_SETTINGS = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": HOOK_MATCHER,
                "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT_S}],
            }
        ]
    }
}


def coder_settings() -> dict:
    """The coder's settings fragment: the format/lint hook on every Edit and Write."""
    return copy.deepcopy(_CODER_SETTINGS)


def settings_for(role: str) -> dict:
    """coder_settings() for the coder, {} for every other role."""
    return coder_settings() if role in HOOKED_ROLES else {}


def hooks_dir() -> Path:
    """The harness checkout's hooks/ directory (next to the hands package)."""
    return Path(__file__).resolve().parent.parent / "hooks"


def _mount_src(p: Path | str) -> str:
    s = str(p)
    if ":" in s or "," in s:
        raise ValueError(f"':' or ',' in mount path: {s}")
    return s


def mount_args(role: str, hooks_dir: Path | str, settings_file: Path | str) -> list[str]:
    """Extra `docker run` arguments: the hooks dir and the settings file, read-only, for the coder; [] otherwise."""
    if role not in HOOKED_ROLES:
        return []
    return ["-v", f"{_mount_src(hooks_dir)}:{HOOKS_MOUNT}:ro",
            "-v", f"{_mount_src(settings_file)}:{SETTINGS_MOUNT}:ro"]


def write_settings(role: str, dest: Path) -> Path:
    """Write settings_for(role) to dest (atomic replace, parent created) and return dest."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(settings_for(role), indent=2) + "\n")
    tmp.chmod(0o644)
    os.replace(tmp, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.hooks", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render", help="print the settings fragment for a role")
    p.add_argument("--role", required=True)
    p.add_argument("--json", action="store_true", help="compact JSON (the default output is JSON too)")
    p = sub.add_parser("mount-args", help="print the extra docker run arguments for a role")
    p.add_argument("--role", required=True)
    p.add_argument("--hooks-dir", required=True)
    p.add_argument("--settings-file", required=True)
    p.add_argument("--json", action="store_true", help="JSON list (the default output is JSON too)")
    a = ap.parse_args(argv)
    if a.cmd == "render":
        print(json.dumps(settings_for(a.role), indent=None if a.json else 2))
        return 0
    try:
        print(json.dumps(mount_args(a.role, a.hooks_dir, a.settings_file)))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
