"""Design-file uploads: the pre-body decision (precheck), the per-project inbox/outbox paths and the CLI behind
`hands <p> design-files` and the dashboard routes. Pure stdlib; `python3 -m hands.uploads precheck --json-in`."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

from . import designfiles, specoutbox

UPLOAD_PATH = re.compile(r"^/api/p/([a-z][a-z0-9-]{1,40})/design-files/upload$")
BATCH_ID = re.compile(r"^[A-Za-z0-9-]{1,40}$")
MAX_FILES = 50
PENDING_CAP = 1 << 30
MIB = designfiles.MIB


def _refuse(code: int, error: str, query: dict | None = None) -> dict:
    return {"code": code, "error": error, "close": True, "query": query or {}}


def _mb(n: int) -> str:
    return f"{n // MIB}"


def precheck(req: dict, caps: dict | None = None) -> dict:
    """Decide before any body byte is read. code 0 = proceed; otherwise the response to send (then close)."""
    headers = {str(k).lower(): str(v) for k, v in (req.get("headers") or {}).items()}
    if not req.get("logged_in"):
        return _refuse(401, "not logged in")
    if not req.get("same_origin") or headers.get("x-hands") != "1":
        return _refuse(403, "bad origin")
    raw = headers.get("content-length")
    if "transfer-encoding" in headers or raw is None:
        return _refuse(411, "length required")
    raw = raw.strip()
    if not re.fullmatch(r"[0-9]{1,12}", raw):
        return _refuse(400, "bad length")
    length = int(raw)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(str(req.get("path", ""))).query, keep_blank_values=True)
    query = {k: v[0] for k, v in q.items() if v}
    name = query.get("name", "")
    if not name:
        return _refuse(400, "name required", query)
    safe = designfiles.safe_name(name)
    kind = query.get("kind") or designfiles.guess_kind(safe)
    query["kind"] = kind
    if kind not in designfiles.KINDS:
        return _refuse(400, f"unknown kind {kind}", query)
    if len(query.get("note", "")) > designfiles.MAX_NOTE:
        return _refuse(400, f"note too long (max {designfiles.MAX_NOTE} characters)", query)
    if not BATCH_ID.match(query.get("batch", "")):
        return _refuse(400, "batch id required", query)
    group, group_n = query.get("group", ""), query.get("group_n", "")
    if group or group_n:
        if not (BATCH_ID.match(group) and group_n.isdigit() and 2 <= int(group_n) <= MAX_FILES):
            return _refuse(400, "bad group marker", query)
    ext = designfiles._ext(safe)
    if ext not in designfiles.EXT_CLASS:
        return _refuse(400, "type not allowed" + designfiles.TYPE_HINTS.get(ext, ""), query)
    try:
        cap = designfiles._caps(caps)
    except designfiles.Refused as exc:
        return _refuse(400, str(exc), query)
    limit = cap[designfiles.EXT_CLASS[ext]]
    if length > limit:
        return _refuse(413, f"too large (max {_mb(limit)} MB)", query)
    batch = req.get("batch") or {}
    if int(batch.get("count", 0)) >= MAX_FILES:
        return _refuse(413, f"batch over {MAX_FILES} files", query)
    if int(batch.get("total", 0)) + length > cap["submit"]:
        return _refuse(413, f"batch over {_mb(cap['submit'])} MB", query)
    return {"code": 0, "error": "", "close": False, "query": query}


def guard(length: int, free: int, min_free: int, pending: int) -> tuple[int, str] | None:
    """Disk guards, checked after precheck and before the body is read."""
    if free - length < min_free:
        return 507, "disk low"
    if pending + length > PENDING_CAP:
        return 413, "design files over 1 GB"
    return None


def tree_bytes(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        with contextlib.suppress(OSError):
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
    return total


def pending_bytes(cfg, inbox: Path) -> int:
    return tree_bytes(inbox) + tree_bytes(specoutbox.root(cfg) / "refs" / "design")


# ------------------------------------------------------------------ per-project paths
def inbox(cfg) -> Path:
    return Path(cfg.state_dir) / "design-inbox"


def thumbs(cfg) -> Path:
    return Path(cfg.state_dir) / "design-thumbs"


def creating_inbox(slug: str) -> Path:
    from . import newproject
    return newproject.creating_dir() / f"{slug}.design-inbox"


def caps(cfg) -> dict:
    return dict(getattr(cfg, "design_caps", None) or {})


def base_index(cfg) -> Path:
    return Path(cfg.spec_dir) / "refs" / "index.json"


@contextlib.contextmanager
def locked(cfg):
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(cfg.state_dir) / "design.lock", "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def secrets(cfg) -> dict:
    return designfiles.load_secrets(cfg.project_env)


def _read(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _group_done(cfg, upload_dir: Path, result: dict) -> dict:
    """Uploads sent with the same group marker become one style-pack entry once every member is finished. Runs
    under the design lock, so only the last member to finish sees the whole group; a failure is returned as
    group_error and written to that member's status so the project page can show it."""
    up = _read(upload_dir / "upload.json") or {}
    group, want = up.get("group"), up.get("group_n")
    if not group or not isinstance(want, int):
        return result
    members = []
    for d in sorted(upload_dir.parent.iterdir()):
        meta = _read(d / "upload.json")
        if meta and meta.get("group") == group:
            members.append((meta, _read(d / "status.json") or {}))
    if len(members) < want or any(st.get("status") not in ("ready", "refused") for _, st in members):
        return result
    ids = sorted(st["id"] for _, st in members if st.get("status") == "ready")
    try:
        if len(ids) < len(members):
            raise designfiles.Refused(f"style pack: {len(members) - len(ids)} of {len(members)} files were refused")
        entry = designfiles.group(specoutbox.root(cfg), ids, "")
    except (designfiles.Refused, OSError, ValueError) as exc:
        return {**designfiles._set_status(upload_dir, "ready", f"group failed: {exc}", result.get("design_id", "")),
                "group_error": str(exc), "design_id": result.get("design_id", "")}
    _retire(cfg, ids)
    return {**result, "group_id": entry["id"]}


def process(cfg, upload_dir, docker: str | None = None) -> dict:
    upload_dir = Path(upload_dir)
    with locked(cfg):
        res = designfiles.process_upload(
            upload_dir, specoutbox.root(cfg), thumbs(cfg), Path(cfg.runner_home) / "capture", secrets(cfg), caps(cfg),
            designfiles.docker_launcher(docker or cfg.docker_bin), base_index(cfg), image=cfg.image)
        return _group_done(cfg, upload_dir, res)


def process_pending(cfg, pending: Path) -> list[dict]:
    """Process what was uploaded while the project was being created; the pending inbox is then dropped."""
    results = []
    if pending.is_dir():
        for d in sorted(pending.iterdir()):
            if (d / "upload.json").is_file():
                results.append(process(cfg, d))
        shutil.rmtree(pending, ignore_errors=True)
    return results


def _seed(cfg, ids: list[str]) -> None:
    """Make the outbox hold the spec's index (and the named entries' files) before an edit/remove/group."""
    ob, spec = specoutbox.root(cfg), Path(cfg.spec_dir)
    if not (ob / "refs" / "index.json").is_file() and base_index(cfg).is_file():
        specoutbox.put(ob, "refs/index.json", base_index(cfg))
    for ident in ids:
        if (spec / "refs" / "design" / ident).is_dir() and not (ob / "refs" / "design" / ident).is_dir():
            specoutbox.put_tree(ob, f"refs/design/{ident}", spec / "refs" / "design" / ident)


def _retire(cfg, ids: list[str]) -> None:
    for ident in ids:
        if (Path(cfg.spec_dir) / "refs" / "design" / ident).is_dir():
            specoutbox.delete(specoutbox.root(cfg), f"refs/design/{ident}")


def index(cfg) -> dict:
    ob = specoutbox.root(cfg)
    return designfiles.load_index(ob if (ob / "refs" / "index.json").is_file() else cfg.spec_dir)


def listing(cfg) -> dict:
    rows = []
    root = inbox(cfg)
    if root.is_dir():
        for d in sorted(root.iterdir()):
            try:
                st = json.loads((d / "status.json").read_text(encoding="utf-8"))
                up = json.loads((d / "upload.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({"id": d.name, "name": up.get("name", ""), "kind": up.get("kind", ""),
                         "status": st.get("status", ""), "reason": st.get("reason", "")})
    return {"files": index(cfg)["files"], "inbox": rows}


def remove(cfg, ident: str) -> None:
    with locked(cfg):
        _seed(cfg, [ident])
        designfiles.remove(specoutbox.root(cfg), ident)
        _retire(cfg, [ident])


def edit(cfg, ident: str, kind: str | None = None, note: str | None = None) -> dict:
    with locked(cfg):
        _seed(cfg, [ident])
        return designfiles.edit(specoutbox.root(cfg), ident, kind, note)


def group(cfg, ids: list[str], note: str = "") -> dict:
    with locked(cfg):
        _seed(cfg, ids)
        entry = designfiles.group(specoutbox.root(cfg), ids, note)
        _retire(cfg, ids)
        return entry


def add(cfg, files: list[str], kind: str | None, note: str, docker: str | None = None) -> list[dict]:
    """CLI `add`: each file is checked like an upload (one batch per call), received and processed."""
    batch = f"cli-{int(time.time())}"
    box = inbox(cfg)
    out, total = [], 0
    for n, f in enumerate(files):
        p = Path(f)
        size = p.stat().st_size
        q = urllib.parse.urlencode({"name": p.name, "kind": kind or "", "note": note, "batch": batch})
        pre = precheck({"path": f"/x?{q}", "headers": {"Content-Length": str(size), "X-Hands": "1"},
                        "logged_in": True, "same_origin": True, "batch": {"total": total, "count": n}}, caps(cfg))
        if pre["code"]:
            out.append({"file": f, "status": "refused", "reason": pre["error"]})
            continue
        with open(p, "rb") as fh:
            ident = designfiles.receive(box, fh, p.name, pre["query"]["kind"], note, batch, size)
        total += size
        res = process(cfg, box / ident, docker)
        out.append({"file": f, "id": ident, "status": res["status"], "reason": res.get("reason", ""),
                    "design_id": res.get("design_id", "")})
    return out


# ------------------------------------------------------------------ CLI
def cli(cfg, verb: str, args: list[str], kind: str | None, note: str | None, docker: str | None = None) -> int:
    try:
        if verb == "add" and args:
            rc = 0
            for r in add(cfg, args, kind, note or "", docker):
                print(f"{r['file']}: {r['status']}" + (f" {r['design_id']}" if r.get("design_id") else "")
                      + (f" ({r['reason']})" if r["reason"] else ""))
                rc |= r["status"] != "ready"
            return rc
        if verb == "list":
            for e in index(cfg)["files"]:
                print(f"{e['id']}\t{e['kind']}\t{e['file']}\t{e['note']}")
            return 0
        if verb == "remove" and len(args) == 1:
            remove(cfg, args[0])
        elif verb == "edit" and len(args) == 1:
            edit(cfg, args[0], kind, note)
        elif verb == "group" and len(args) >= 2:
            print(group(cfg, args, note or "")["id"])
        elif verb == "process" and len(args) == 1:
            if not re.fullmatch(r"u[0-9]+-[0-9a-f]{6}", args[0]):
                print("bad upload id")
                return 2
            res = process(cfg, inbox(cfg) / args[0], docker)
            print(f"{res['status']}" + (f": {res['reason']}" if res.get("reason") else ""))
            return 0 if res["status"] == "ready" else 1
        else:
            print("usage: hands <project> design-files add FILE... [--kind K] [--note T] | list | remove ID | "
                  "edit ID [--kind K] [--note T] | group ID ID... [--note T] | process UPLOAD_ID")
            return 2
    except (designfiles.Refused, OSError, ValueError) as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.uploads")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("precheck")
    p.add_argument("--json-in", action="store_true", required=True)
    p.add_argument("--caps")
    a = ap.parse_args(argv)
    req = json.load(sys.stdin)
    print(json.dumps(precheck(req, json.loads(a.caps) if a.caps else None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
