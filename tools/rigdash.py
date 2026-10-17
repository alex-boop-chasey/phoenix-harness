#!/usr/bin/env python3
"""Dashboard launcher for the UI screenshot rig (tools/ui-shots.sh), run INSIDE the rig container.

The agent image has no tmux, so the Session manager backend is swapped for a stand-in: a session is
"alive" while it is in a set, and every terminal viewer runs the fake `claude` below (an echo script
that writes a Claude-style transcript so titles appear). The routes, the WebSocket, the tickets, the
caps and the page are the real ones; only `sessions.BACKEND` differs. Nothing here is installed or
reachable from the real dashboard.

It also builds a throwaway folder tree under the container's home for the File browser flow (step 14):
~/projects/site (text, code, a PNG, a binary, hidden files, an empty folder, a symlink to ~/.ssh that
must be refused), ~/harness (a few files), ~/ops/inbox; ~/claude-harness is left missing on purpose.

  python3 rigdash.py --port 8090      (HANDS_CONFIG_HOME and CLAUDE_CONFIG_DIR set by the rig)
"""
from __future__ import annotations

import os
import stat
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from hands import dashboard, sessions

FAKE_CLAUDE = r"""#!/bin/sh
# fake claude for the screenshot rig: $1 = session id, $2 = working folder, $3 = Claude config dir
sid="$1"; cwd="$2"
enc=$(printf '%s' "$cwd" | sed 's/[^A-Za-z0-9]/-/g')
dir="$3/projects/$enc"
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


class RigBackend(sessions.Tmux):
    def __init__(self, fake: str):
        super().__init__()
        self.fake = fake
        self.alive_ids: set[str] = set()

    def start(self, rec: dict, resume: bool) -> None:
        sessions.claude_argv(rec["id"], resume, sessions.is_telegram(rec))   # the real argv builder still runs
        self.alive_ids.add(rec["id"])

    def alive(self, sid: str) -> bool:
        return sid in self.alive_ids

    def stop(self, sid: str) -> None:
        self.alive_ids.discard(sid)

    def attach_argv(self, sid: str) -> list[str]:
        return [self.fake, sid, sessions.Store().get(sid)["cwd"], str(sessions.claude_home())]


def png(w: int, h: int) -> bytes:
    """A small gradient PNG (stdlib only) for the image preview and grid thumbnails."""
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (40 + x * 180 // w, 90 + y * 120 // h, 200 - x * 120 // w))
                    for y in range(h))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def file_tree(home: Path) -> None:
    site = home / "projects" / "site"
    files = {
        "README.md": "# Site\n\nA small sample project for the File browser rig.\n\n- `src/` code\n- `assets/` images\n",
        "src/app.js": "// entry point\nexport function hello(name) {\n  return `hello ${name}`;\n}\n",
        "src/style.css": "body { margin: 0; }\n",
        "src/server.py": "def main():\n    print('serving')\n",
        "notes/todo.txt": "- ship the file browser\n- retire the old container\n",
        "notes/meeting-2026-10-16.md": "## Notes\nDecided: Workbench design.\n",
        "package.json": '{"name": "site", "version": "0.1.0"}\n',
        ".env.example": "API_URL=\n",
        ".gitignore": "node_modules/\n",
        "logs/build.log": "".join(f"2026-10-17T0{i % 10}:00:00Z step {i} ok\n" for i in range(400)),
        "docs/guide.md": "# Guide\n" + "Lorem ipsum dolor sit amet. " * 40 + "\n",
    }
    for rel, text in files.items():
        p = site / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (site / "assets").mkdir(exist_ok=True)
    (site / "assets" / "logo.png").write_bytes(png(240, 160))
    (site / "assets" / "icon.png").write_bytes(png(48, 48))
    (site / "data.bin").write_bytes(bytes(range(256)) * 16)
    (site / "empty").mkdir(exist_ok=True)
    ssh = home / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    (ssh / "id_rig").write_text("NOT A REAL KEY (rig)\n")
    if not (site / "keys").is_symlink():
        os.symlink(ssh, site / "keys")                     # leads into a denied folder: listed as blocked, refused
    os.symlink(site / "src", site / "src-link") if not (site / "src-link").is_symlink() else None
    h = home / "harness"
    (h / "hands").mkdir(parents=True, exist_ok=True)
    (h / "README.md").write_text("# Phoenix Harness (rig copy)\n")
    (h / "VERSION").write_text("0.5.0-rig\n")
    (h / "hands" / "files.py").write_text("# stand-in\n")
    (home / "ops" / "inbox").mkdir(parents=True, exist_ok=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rigdash-"))
    fake = tmp / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(stat.S_IRWXU)
    os.environ["HANDS_CLAUDE"] = str(fake)            # claude_argv() needs an executable; the stand-in is it
    os.environ.setdefault("CLAUDE_CONFIG_DIR", str(tmp / "claude-home"))
    for d in ("ops", "projects/demo", "scratch"):
        (Path.home() / d).mkdir(parents=True, exist_ok=True)
    file_tree(Path.home())
    sessions.BACKEND = RigBackend(str(fake))
    return dashboard.main(sys.argv[1:] or ["run"])


if __name__ == "__main__":
    sys.exit(main())
