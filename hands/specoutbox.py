"""Spec outbox: a per-project mirror (<state_dir>/spec-outbox) of the harness-owned spec paths.

Harness-owned paths are refs/**, brief.md, brief.sha256 and briefs/*.md. They reach the spec repo only through the
plan/amend PR (open_pr adds what differs from spec main) and survive gitops.refresh_spec, which wipes the clone.
The planner sees them (make_work overlays the outbox) but may not change them (check). Deletions are recorded in
<root>/deletions.json. CLI: python3 -m hands.specoutbox put|put-tree|delete|list|changed|overlay|check|clear-merged.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

HARNESS_PATH = re.compile(r"^(?:refs/[A-Za-z0-9._/@+-]+|brief\.md|brief\.sha256|briefs/[A-Za-z0-9._-]+\.md)$")
DELETIONS = "deletions.json"
DESIGN_PREFIXES = ("refs/design/", "refs/prototype/")
_TOP = ("refs", "briefs")                       # directories holding harness-owned files
_TOP_FILES = ("brief.md", "brief.sha256")


def root(cfg) -> Path:
    return Path(cfg.state_dir) / "spec-outbox"


def is_harness_path(rel: str) -> bool:
    parts = rel.split("/") if isinstance(rel, str) else []
    return bool(HARNESS_PATH.match(rel or "")) and all(p not in ("", ".", "..") for p in parts)


def _need(rel: str) -> str:
    if not is_harness_path(rel):
        raise ValueError(f"{rel!r} is not a harness-owned spec path (refs/**, brief.md, brief.sha256, briefs/*.md)")
    return rel


def _deletions(root: Path) -> list[str]:
    try:
        data = json.loads((Path(root) / DELETIONS).read_text())
    except (OSError, ValueError):
        return []
    return sorted({d for d in data if isinstance(d, str) and is_harness_path(d)}) if isinstance(data, list) else []


def _write_deletions(root: Path, items) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (DELETIONS + ".tmp")
    tmp.write_text(json.dumps(sorted(set(items)), indent=2) + "\n")
    os.replace(tmp, root / DELETIONS)


def _files(base: Path) -> list[str]:
    """Regular files of a tree that belong to the harness-owned areas, as sorted posix paths."""
    base = Path(base)
    out = []
    for top in _TOP:
        for dirpath, dirs, names in os.walk(base / top, followlinks=False):
            for n in names:
                p = Path(dirpath) / n
                if not p.is_symlink():
                    out.append(p.relative_to(base).as_posix())
    out += [n for n in _TOP_FILES if (base / n).is_file() and not (base / n).is_symlink()]
    return sorted(out)


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def put_bytes(root: Path, rel: str, data: bytes) -> None:
    dst = Path(root) / _need(rel)
    if dst.is_dir():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    _write_deletions(root, [d for d in _deletions(root) if d != rel])


def put(root: Path, rel: str, src: Path) -> None:
    put_bytes(root, rel, Path(src).read_bytes())


def put_tree(root: Path, rel: str, src_dir: Path) -> None:
    """Replace the subtree <rel> with the regular files under src_dir."""
    _need(rel)
    dst = Path(root) / rel
    if dst.is_dir():
        shutil.rmtree(dst)
    elif dst.exists():
        dst.unlink()
    files = []
    for dirpath, _, names in os.walk(src_dir, followlinks=False):
        for n in names:
            p = Path(dirpath) / n
            if not p.is_symlink():
                files.append(p)
    for p in sorted(files):
        _need(f"{rel}/{p.relative_to(src_dir).as_posix()}")
    for p in files:
        target = dst / p.relative_to(src_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(p.read_bytes())
    _write_deletions(root, [d for d in _deletions(root) if not _under(d, rel)])


def delete(root: Path, rel: str) -> None:
    """Remove a file or subtree from the outbox and record the deletion for the next PR."""
    _need(rel)
    target = Path(root) / rel
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    _write_deletions(root, [*_deletions(root), rel])


def listing(root: Path) -> dict:
    return {"files": [f for f in _files(root)], "deletions": _deletions(root)}


def _read(p: Path) -> bytes | None:
    return p.read_bytes() if p.is_file() and not p.is_symlink() else None


def _deleted_in(spec: Path, root: Path) -> list[str]:
    """Spec files named by a recorded deletion that the outbox does not (again) provide."""
    dels = _deletions(root)
    have = set(_files(root))
    return [f for f in _files(spec) if f not in have and any(_under(f, d) for d in dels)]


def changed(root: Path, spec: Path) -> dict:
    ch = [f for f in _files(root) if _read(Path(root) / f) != _read(Path(spec) / f)]
    return {"changed": sorted(ch), "deleted": sorted(_deleted_in(Path(spec), Path(root)))}


def overlay(root: Path, into: Path) -> None:
    into = Path(into)
    for f in _deleted_in(into, root):
        (into / f).unlink()
    for f in _files(root):
        dst = into / f
        if dst.is_symlink() or dst.is_dir():
            shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((Path(root) / f).read_bytes())


def check(root: Path, spec: Path, candidate: Path) -> list[str]:
    """Problems when the planner's candidate differs from the expected harness files (spec main + outbox)."""
    expected = Path(root), Path(spec)
    gone = set(_deleted_in(Path(spec), Path(root)))
    errs = []
    for f in sorted(set(_files(Path(spec))) | set(_files(Path(root))) | set(_files(Path(candidate)))):
        want = _read(expected[0] / f)
        if want is None and f not in gone:
            want = _read(expected[1] / f)
        got = _read(Path(candidate) / f)
        if want == got:
            continue
        errs.append(f"{f}: harness-owned file {'deleted' if got is None else 'changed'} by the planner")
    return errs


def clear_merged(root: Path, spec: Path) -> None:
    """Drop outbox entries that spec main now has identically, and deletions that are done."""
    for f in _files(root):
        if _read(Path(root) / f) == _read(Path(spec) / f):
            (Path(root) / f).unlink()
    for dirpath, _, _ in os.walk(root, topdown=False):
        if Path(dirpath) != Path(root) and not any(Path(dirpath).iterdir()):
            Path(dirpath).rmdir()
    _write_deletions(root, [d for d in _deletions(root)
                            if any(_under(f, d) for f in _files(Path(spec)))])


def design_changed(paths) -> bool:
    return any(p.startswith(DESIGN_PREFIXES) or p == "refs/index.json" for p in paths)


def design_ids(paths) -> list[str]:
    """The design entry ids (refs/design/<id>/...) the given harness paths touch, sorted."""
    ids = set()
    for p in paths:
        parts = p.split("/")
        if len(parts) > 3 and parts[:2] == ["refs", "design"] and re.fullmatch(r"d[0-9]{3}", parts[2]):
            ids.add(parts[2])
    return sorted(ids)


def mirror(cfg, rels, source: Path | None = None) -> None:
    """Copy harness files from the spec clone (default cfg.spec_dir) into the outbox; a missing file is a deletion."""
    src = Path(source or cfg.spec_dir)
    ob = root(cfg)
    for rel in rels:
        if (src / rel).is_file():
            put(ob, rel, src / rel)
        else:
            delete(ob, rel)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.specoutbox", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("put", "put-tree", "delete", "list", "changed", "overlay", "check", "clear-merged"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        if name in ("put", "put-tree", "delete"):
            p.add_argument("--path", required=True)
        if name in ("put", "put-tree"):
            p.add_argument("--from", dest="src", required=True)
        if name in ("changed", "check", "clear-merged"):
            p.add_argument("--spec", required=True)
        if name == "overlay":
            p.add_argument("--into", required=True)
        if name == "check":
            p.add_argument("--candidate", required=True)
        if name in ("list", "changed", "check"):
            p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = Path(a.root)
    try:
        if a.cmd == "put":
            put(r, a.path, Path(a.src))
        elif a.cmd == "put-tree":
            put_tree(r, a.path, Path(a.src))
        elif a.cmd == "delete":
            delete(r, a.path)
        elif a.cmd == "overlay":
            overlay(r, Path(a.into))
        elif a.cmd == "clear-merged":
            clear_merged(r, Path(a.spec))
        elif a.cmd in ("list", "changed"):
            out = listing(r) if a.cmd == "list" else changed(r, Path(a.spec))
            print(json.dumps(out) if a.json else "\n".join([*(f"{k}: {v}" for k, vs in out.items() for v in vs)]))
        else:
            problems = check(r, Path(a.spec), Path(a.candidate))
            print(json.dumps({"problems": problems}) if a.json else "\n".join(problems))
            return 1 if problems else 0
    except (ValueError, OSError) as e:
        print(f"specoutbox: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
