#!/usr/bin/env python3
"""Dashboard launcher for the UI screenshot rig (tools/ui-shots.sh), run INSIDE the rig container.

The real Session manager backend (PTYs owned by the dashboard, step 17) runs the fake `claude` below
(an echo script that writes a Claude-style transcript so titles appear) through HANDS_CLAUDE. The
routes, the WebSocket, the tickets, the caps and the page are the real ones. Nothing here is installed
or reachable from the real dashboard.

  python3 rigdash.py --port 8090      (HANDS_CONFIG_HOME and CLAUDE_CONFIG_DIR set by the rig)
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from hands import dashboard

FAKE_CLAUDE = r"""#!/bin/sh
# fake claude for the screenshot rig: same argv as claude (--session-id ID | --resume ID), run in the
# session's working folder with CLAUDE_CONFIG_DIR set, exactly like the real one
sid=""
while [ $# -gt 0 ]; do case "$1" in --session-id|--resume) sid="$2"; shift;; esac; shift; done
cwd=$(pwd)
enc=$(printf '%s' "$cwd" | sed 's/[^A-Za-z0-9]/-/g')
dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$enc"
mkdir -p "$dir"
printf '\033[1m Claude Code\033[0m \033[2m(screenshot rig stand-in)\033[0m  %s\n' "$cwd"
printf '\033[2m────────────────────────────────────────────\033[0m\n'
printf '\033[32m✓\033[0m ready · \033[33mtype a line and press Enter\033[0m · \033[31mred\033[0m \033[34mblue\033[0m \033[35mmagenta\033[0m \033[36mcyan\033[0m \033[38;5;208m256-colour\033[0m\n'
printf '\033[1;34m❯\033[0m '
while IFS= read -r line; do
  esc=$(printf '%s' "$line" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '{"type":"user","message":{"role":"user","content":"%s"}}\n' "$esc" >> "$dir/$sid.jsonl"
  case "$line" in exit) printf '\033[2mbye\033[0m\n'; exit 0;; esac
  printf '\033[36m●\033[0m fake claude: %s\n' "$line"
  printf '\033[1;34m❯\033[0m '
done
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rigdash-"))
    fake = tmp / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(stat.S_IRWXU)
    os.environ["HANDS_CLAUDE"] = str(fake)            # the real backend starts this instead of claude
    os.environ.setdefault("CLAUDE_CONFIG_DIR", str(tmp / "claude-home"))
    for d in ("ops", "projects/demo", "scratch"):
        (Path.home() / d).mkdir(parents=True, exist_ok=True)
    return dashboard.main(sys.argv[1:] or ["run"])


if __name__ == "__main__":
    sys.exit(main())
