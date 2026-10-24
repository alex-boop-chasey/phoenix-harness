"""Copy check: banned words, lorem ipsum, invented proof and (in the final step) placeholders in user-facing text.

The runner computes these facts after acceptance and hands them to the evaluator; it never fails a step itself.
Scanned: visible text and the alt/title/aria-label/placeholder/content attribute values of .astro .html .jsx .tsx
.vue .svelte files (Astro frontmatter is code), prose lines of .md/.mdx, string values of .json files under a
content/ directory. Never node_modules, acceptance, tests, test, e2e, docs, *.test.*, *.spec.*, or code and CSS.
CLI: python3 -m hands.copylint check --root APP --inputs F [--copy docs/copy.md] [--files a b ...] [--final] --json
(exit 1 when there are problems).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

MARKUP = {".astro", ".html", ".jsx", ".tsx", ".vue", ".svelte"}
PROSE = {".md", ".mdx"}
SKIP_DIRS = {"node_modules", "acceptance", "tests", "test", "e2e", "docs", ".git", "dist", ".astro", ".next"}
ATTRS = ("alt", "title", "aria-label", "placeholder", "content")
LIMIT = 200
PLACEHOLDER = "[NEEDS INPUT"

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<(?:/?[A-Za-z][^<>]*|![^<>]*)>")
_ATTR = re.compile(r"""(?<![\w-])(%s)\s*=\s*(?:"([^"]*)"|'([^']*)')""" % "|".join(ATTRS), re.I)
_LOREM = re.compile(r"lorem ipsum|dolor sit amet", re.I)
_PROOF = [
    re.compile(r"\b\d+(?:\.\d+)?\s?%\s+of\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?x\s+(?:faster|more|better|cheaper|quicker)\b", re.I),
    re.compile(r"\b\d[\d,]*\+\s+(?:[\w-]+\s+){0,2}?(?:clients?|customers?|projects?|years?|reviews?)\b", re.I),
    re.compile(r"\b\d(?:\.\d)?\s*/\s*5\b"),
    re.compile(r"★{3,}"),
    re.compile(r"""(?:^|["”’»])\s*[—–]\s+[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*)*,\s+\S[^,.;]*"""),
]


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def _line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1


def _markup_segments(src: str, path: Path) -> Iterator[tuple[int, str]]:
    if path.suffix == ".astro":
        m = re.match(r"\s*---[ \t]*\n.*?\n---[ \t]*(?:\n|$)", src, re.S)
        if m:
            src = _blank(m) + src[m.end():]
    src = _BLOCK.sub(_blank, _COMMENT.sub(_blank, src))
    pos = 0
    pieces: list[tuple[int, str]] = []
    for tag in _TAG.finditer(src):
        pieces.append((pos, src[pos:tag.start()]))
        for a in _ATTR.finditer(tag.group(0)):
            val = a.group(2) if a.group(2) is not None else a.group(3)
            pieces.append((tag.start() + a.start(), val))
        pos = tag.end()
    pieces.append((pos, src[pos:]))
    for off, text in pieces:
        for i, line in enumerate(text.split("\n")):
            line = html.unescape(line).strip()
            if line:
                yield _line_of(src, off) + i, line


def _prose_segments(src: str) -> Iterator[tuple[int, str]]:
    lines = src.split("\n")
    i = 0
    if lines and lines[0].strip() == "---":
        i = next((n + 1 for n in range(1, len(lines)) if lines[n].strip() == "---"), 0)
    fenced = False
    for n in range(i, len(lines)):
        line = lines[n]
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.startswith(("    ", "\t")) or re.match(r"\s*(import|export)\s", line):
            continue
        if line.strip():
            yield n + 1, line.strip()


def _json_segments(src: str) -> Iterator[tuple[int, str]]:
    try:
        data = json.loads(src)
    except ValueError:
        return
    lines = src.split("\n")

    def walk(v: Any) -> Iterator[str]:
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                yield from walk(x)
        elif isinstance(v, list):
            for x in v:
                yield from walk(x)

    for s in walk(data):
        enc = json.dumps(s, ensure_ascii=False)[1:-1]
        n = next((k + 1 for k, ln in enumerate(lines) if enc in ln), 1)
        yield n, s


def segments(path: Path, rel: str, src: str) -> Iterator[tuple[int, str]]:
    ext = path.suffix.lower()
    if ext in MARKUP:
        return _markup_segments(src, path)
    if ext in PROSE:
        return _prose_segments(src)
    if ext == ".json" and "content" in rel.split("/")[:-1]:
        return _json_segments(src)
    return iter(())


def scannable(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    if re.search(r"\.(test|spec)\.", name):
        return False
    ext = os.path.splitext(name)[1].lower()
    return ext in MARKUP or ext in PROSE or (ext == ".json" and "content" in parts[:-1])


def _tree(root: Path) -> list[str]:
    out = []
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in names:
            out.append((Path(dirpath) / n).relative_to(root).as_posix())
    return out


def _banned_re(words: list[str]) -> re.Pattern | None:
    words = [w for w in words if w.strip()]
    if not words:
        return None
    return re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(w.strip()) for w in words), re.I)


def check(root: Path, inputs: dict, copy_md: str | None, files: list[str] | None = None,
          final: bool = False) -> list[dict]:
    """Problems as {file, line, rule, text}, sorted by file and line; `files` are root-relative (None = whole tree)."""
    root = Path(root).resolve()
    banned = _banned_re(list((inputs or {}).get("banned_words") or []))
    allowed = _squash(" ".join([copy_md or "", *((inputs or {}).get("proof") or [])]))
    rels = _tree(root) if files is None else [Path(f).as_posix() for f in files]
    problems: dict[tuple, dict] = {}
    for rel in sorted(set(rels)):
        path = (root / rel).resolve()
        if not scannable(rel) or root not in path.parents or not path.is_file():
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        raw_lines = src.split("\n")
        for line_no, text in segments(path, rel, src):
            shown = (raw_lines[line_no - 1] if line_no <= len(raw_lines) else text).strip()[:LIMIT]

            def add(rule: str) -> None:
                problems.setdefault((rel, line_no, rule), {"file": rel, "line": line_no, "rule": rule, "text": shown})

            if banned and banned.search(text):
                add("banned")
            placeholder = PLACEHOLDER in text
            if _LOREM.search(text) and not placeholder:
                add("lorem")
            for pat in _PROOF:
                if any(_squash(m.group(0)) not in allowed for m in pat.finditer(text)):
                    add("invented-proof")
            if final and placeholder:
                add("placeholder-final")
    return sorted(problems.values(), key=lambda p: (p["file"], p["line"], p["rule"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.copylint", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="scan user-facing text; exit 1 when there are problems")
    p.add_argument("--root", required=True, help="the app directory")
    p.add_argument("--inputs", required=True, help="site-inputs.json")
    p.add_argument("--copy", help="docs/copy.md (the drafted copy; its text counts as real proof)")
    p.add_argument("--files", nargs="*", help="root-relative files to scan (default: the whole tree)")
    p.add_argument("--final", action="store_true", help="the last code step: placeholders are problems")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    from .sitecopy import InvalidInputs, read
    try:
        inputs = read(Path(a.inputs))
        copy_md = Path(a.copy).read_text(encoding="utf-8") if a.copy and Path(a.copy).is_file() else None
    except (InvalidInputs, OSError, UnicodeDecodeError) as e:
        print(f"copylint: {e}", file=sys.stderr)
        return 2
    problems = check(Path(a.root), inputs, copy_md, a.files, a.final)
    if a.json:
        print(json.dumps({"problems": problems}, indent=1, ensure_ascii=False))
    else:
        for q in problems:
            print(f"{q['file']}:{q['line']}: {q['rule']}: {q['text']}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
