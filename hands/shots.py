"""Screenshots of a built step (docs/design.md, "Screenshots").

`capture/shots.mjs` runs in a container next to agent code, so its PNGs are untrusted: `finish` verifies each one,
rewrites it through designfiles.clean_png, writes manifest.json and returns the manifest hash (`shots_sha`).
CLI: python3 -m hands.shots slug ROUTE | finish --raw D --dest E --step S --commit C [--json] | hash --manifest F |
command --routes R...   (finish exits 1 when refused)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import struct
import sys
from pathlib import Path

from . import designfiles

NAME_RE = re.compile(r"^[a-z0-9-]{1,80}-(desktop|mobile)\.png$")
MAX_BYTES = 8 * 1024 * 1024
MAX_W, MAX_H = 2880, 12000
PORT = 4321
CAPTURE_MOUNT = "/opt/hands/capture"
SHOTS_MOUNT = "/shots"
SIDE_FILES = {"shots.json", "errors.json"}


class Refused(Exception):
    pass


def slug(route: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", route.strip("/")).strip("-").lower()
    return s or "home"


def manifest_sha(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(routes: list[str]) -> str:
    return (f"pnpm install --frozen-lockfile && pnpm run build && node {CAPTURE_MOUNT}/shots.mjs "
            f"--start {shlex.quote('pnpm run preview')} --port {PORT} --routes "
            f"{' '.join(shlex.quote(r) for r in routes)} --out {SHOTS_MOUNT}")


def finish(raw, dest, step: str, commit: str) -> dict:
    """Validate the raw capture directory and write the sanitised PNGs and manifest.json into `dest`."""
    raw, dest = Path(raw), Path(dest)
    try:
        listed = json.loads((raw / "shots.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("no readable shots.json") from None
    if not isinstance(listed, list) or not listed:
        raise Refused("shots.json lists no screenshots")
    files = {p.name for p in raw.iterdir()} - SIDE_FILES
    entries, clean = [], {}
    for e in listed:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("route", "viewport", "file")):
            raise Refused("shots.json entry is malformed")
        name = e["file"]
        if not NAME_RE.match(name) or not name.endswith(f"-{e['viewport']}.png") or name in clean:
            raise Refused(f"unexpected screenshot name {name[:40]!r}")
        p = raw / name
        if not p.is_file() or p.is_symlink():
            raise Refused(f"{name} is missing")
        if p.stat().st_size > MAX_BYTES:
            raise Refused(f"{name} is larger than 8 MB")
        try:
            data = designfiles.clean_png(p.read_bytes())
        except designfiles.Refused as err:
            raise Refused(f"{name}: {err}") from None
        w, h = struct.unpack(">II", data[16:24])
        if w > MAX_W or h > MAX_H:
            raise Refused(f"{name} is larger than {MAX_W}x{MAX_H} px")
        clean[name] = data
        entries.append({"route": e["route"][:200], "viewport": e["viewport"], "file": name,
                        "sha256": hashlib.sha256(data).hexdigest(), "width": w, "height": h})
    if files - set(clean):
        raise Refused("unexpected files in the screenshot directory")
    manifest = {"schema": 1, "step": step, "commit": commit, "shots": entries}
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, data in clean.items():
        (dest / name).write_bytes(data)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"shots_sha": manifest_sha(manifest), "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.shots")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("slug").add_argument("route")
    p = sub.add_parser("finish")
    for k in ("--raw", "--dest", "--step", "--commit"):
        p.add_argument(k, required=True)
    p.add_argument("--json", action="store_true")
    sub.add_parser("hash").add_argument("--manifest", required=True)
    sub.add_parser("command").add_argument("--routes", nargs="+", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "slug":
        print(slug(a.route))
    elif a.cmd == "command":
        print(command(a.routes))
    elif a.cmd == "hash":
        print(manifest_sha(json.loads(Path(a.manifest).read_text(encoding="utf-8"))))
    else:
        try:
            res = finish(a.raw, a.dest, a.step, a.commit)
        except Refused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        print(json.dumps(res) if a.json else res["shots_sha"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
