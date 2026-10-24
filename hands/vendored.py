"""Vendored skills and kits: validation and per-role read-only mounts.

Layout (ops-owned, may be absent): kits/<name>-<semver>/{inventory.json,LICENSE,PIN,...source} and
skills/<name>/{SKILL.md,LICENSE,PIN}. An invalid item is reported by scan() and never mounted.
mounts() composes <state_dir>/skills/<role>/ (base skills + the role's vendored skills, regular files
only) and returns read-only mounts as dicts {"src", "dst", "ro"}.

CLI: python3 -m hands.vendored list --root R [--json]
     python3 -m hands.vendored mounts --root R --role ROLE --state S --base-skills DIR [--map F] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

MAP_FILE = Path(__file__).resolve().parent.parent / "prompts" / "vendored.json"
SKILLS_DST = "/home/node/.claude/skills"
KITS_DST = "/opt/kits"
KIT_DIR = re.compile(r"^([a-z0-9][a-z0-9-]*?)-(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
FORBIDDEN_FILES = {".mcp.json", "settings.json", "settings.local.json", "hooks.json", "plugin.json"}
MAX_FILES = 5000
MAX_BYTES = 50 * 1024 * 1024
PIN_KEYS = ("source", "ref", "sha256", "reviewed_by", "date")
ROLES = ("planner", "coder", "evaluator")


def _walk(d: Path, problems: list[str]) -> None:
    files = size = 0
    for cur, dirs, names in os.walk(d, followlinks=False):
        rel = Path(cur).relative_to(d)
        for sub in list(dirs):
            p = Path(cur) / sub
            if p.is_symlink():
                problems.append(f"symlink {(rel / sub).as_posix()}")
                dirs.remove(sub)
            elif sub == "hooks":
                problems.append(f"forbidden directory {(rel / sub).as_posix()}")
        for n in names:
            p = Path(cur) / n
            if p.is_symlink():
                problems.append(f"symlink {(rel / n).as_posix()}")
                continue
            if n in FORBIDDEN_FILES:
                problems.append(f"forbidden file {n}")
            files += 1
            size += p.stat().st_size
    if files > MAX_FILES:
        problems.append(f"too many files ({files} > {MAX_FILES})")
    if size > MAX_BYTES:
        problems.append(f"too large ({size} bytes > {MAX_BYTES})")


def _pin(d: Path, problems: list[str]) -> None:
    f = d / "PIN"
    if not f.is_file():
        problems.append("PIN missing")
        return
    got = {}
    for line in f.read_text(errors="replace").splitlines():
        k, sep, v = line.partition("=")
        if sep and v.strip():
            got[k.strip()] = v.strip()
    missing = [k for k in PIN_KEYS if k not in got]
    if missing:
        problems.append(f"PIN incomplete (missing {', '.join(missing)})")


def _common(d: Path, required: str) -> list[str]:
    problems: list[str] = []
    for n in (required, "LICENSE"):
        if not (d / n).is_file():
            problems.append(f"{n} missing")
    _pin(d, problems)
    _walk(d, problems)
    return problems


def _inventory(d: Path, name: str, version: str, problems: list[str]) -> None:
    try:
        inv = json.loads((d / "inventory.json").read_text())
    except (OSError, ValueError):
        if (d / "inventory.json").is_file():
            problems.append("inventory.json does not parse")
        return
    if not isinstance(inv, dict) or inv.get("schema") != 1 or inv.get("name") != name \
            or inv.get("version") != version or not isinstance(inv.get("components"), list):
        problems.append("inventory.json: schema/name/version/components do not match the directory")


def scan(root: Path) -> dict:
    root = Path(root)
    kits, skills = [], []
    kd = root / "kits"
    for d in sorted(kd.iterdir()) if kd.is_dir() else []:
        if not d.is_dir():
            continue
        m = KIT_DIR.match(d.name)
        if d.is_symlink() or not m:
            kits.append({"name": d.name, "version": "", "dir": str(d), "ok": False,
                         "problems": ["directory name must be <name>-<semver>" if not m else "symlink"]})
            continue
        problems = _common(d, "inventory.json")
        _inventory(d, m.group(1), m.group(2), problems)
        kits.append({"name": m.group(1), "version": m.group(2), "dir": str(d), "ok": not problems, "problems": problems})
    sd = root / "skills"
    for d in sorted(sd.iterdir()) if sd.is_dir() else []:
        if not d.is_dir():
            continue
        problems = ["symlink"] if d.is_symlink() else _common(d, "SKILL.md")
        if not NAME.match(d.name):
            problems.append("bad skill name")
        skills.append({"name": d.name, "dir": str(d), "ok": not problems, "problems": problems})
    return {"kits": kits, "skills": skills}


def load_map(path: Path | None = None) -> dict:
    try:
        data = json.loads(Path(path or MAP_FILE).read_text())
    except (OSError, ValueError):
        return {"skills": {}, "kits": []}
    if not isinstance(data, dict):
        return {"skills": {}, "kits": []}
    skills = data.get("skills") if isinstance(data.get("skills"), dict) else {}
    kits = data.get("kits") if isinstance(data.get("kits"), list) else []
    return {"skills": {k: [r for r in v if isinstance(r, str)] for k, v in skills.items() if isinstance(v, list)},
            "kits": [k for k in kits if isinstance(k, str)]}


def _copy_regular(src: Path, dst: Path) -> None:
    for cur, dirs, names in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(cur) / d).is_symlink()]
        out = dst / Path(cur).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for n in names:
            p = Path(cur) / n
            if not p.is_symlink() and p.is_file():
                shutil.copyfile(p, out / n)


def compose_skills(root: Path, role: str, state_dir: Path, base_skills: Path | None, mapping: dict,
                   found: dict | None = None) -> Path:
    dest = Path(state_dir) / "skills" / role
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    if base_skills and Path(base_skills).is_dir():
        _copy_regular(Path(base_skills), dest)
    found = found or scan(root)
    ok = {s["name"]: s for s in found["skills"] if s["ok"]}
    for name, roles in sorted(mapping.get("skills", {}).items()):
        if role in roles and name in ok:
            _copy_regular(Path(ok[name]["dir"]), dest / name)
    return dest


def mounts(root: Path, role: str, state_dir: Path, base_skills: Path | None, mapping: dict | None = None) -> list[dict]:
    """Read-only mounts for `role`; [] when there is neither a base skills dir nor a vendored item to mount."""
    mapping = load_map() if mapping is None else mapping
    found = scan(root)
    out: list[dict] = []
    has_base = bool(base_skills and Path(base_skills).is_dir())
    wanted = [n for n, roles in mapping.get("skills", {}).items()
              if role in roles and any(s["name"] == n and s["ok"] for s in found["skills"])]
    if has_base or wanted:
        out.append({"src": str(compose_skills(root, role, state_dir, base_skills, mapping, found)),
                    "dst": SKILLS_DST, "ro": True})
    ok_kits = {k["name"]: k for k in found["kits"] if k["ok"]}
    for name in mapping.get("kits", []):
        k = ok_kits.get(name)
        if not k:
            continue
        if role == "coder":
            out.append({"src": k["dir"], "dst": f"{KITS_DST}/{name}", "ro": True})
        else:
            out.append({"src": str(Path(k["dir"]) / "inventory.json"), "dst": f"{KITS_DST}/{name}/inventory.json", "ro": True})
    return out


def problems_report(root: Path) -> list[str]:
    """One line per invalid item, for `hands <p> check`."""
    found = scan(root)
    return [f"{kind} {i['name']}: {'; '.join(i['problems'])}"
            for kind in ("kits", "skills") for i in found[kind] if not i["ok"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.vendored", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("mounts")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--role", required=True, choices=ROLES)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--base-skills", type=Path)
    p.add_argument("--map", type=Path, default=MAP_FILE)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        found = scan(a.root)
        if a.json:
            print(json.dumps(found, indent=1))
        else:
            for kind in ("kits", "skills"):
                for i in found[kind]:
                    print(f"{kind[:-1]} {i['name']}: " + ("ok" if i["ok"] else "INVALID " + "; ".join(i["problems"])))
        return 0
    res = mounts(a.root, a.role, a.state, a.base_skills, load_map(a.map))
    if a.json:
        print(json.dumps({"mounts": res}, indent=1))
    else:
        for m in res:
            print(f"{m['src']} -> {m['dst']} (ro)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
