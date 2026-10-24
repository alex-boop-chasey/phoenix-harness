"""Site inputs (the copy fields): audience, offer, proof, tone notes, banned words, number of directions.

Setup fields of the new-project and change-request forms and of the Plan-with-Claude copywriter interview.
Stored in next.json under `copy`, written to the spec outbox as refs/site-inputs.json.
CLI: python3 -m hands.sitecopy validate --file F [--json] | write --file F --outbox R (exit 1 invalid).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = 1
REL_PATH = "refs/site-inputs.json"
MAX_AUDIENCE = 500
MAX_OFFER = 500
MAX_PROOF = 20
MAX_PROOF_LINE = 300
MAX_TONE = 1000
MAX_BANNED = 50
MAX_BANNED_WORD = 40
MIN_DIRECTIONS = 2
MAX_DIRECTIONS = 8
DEFAULT_DIRECTIONS = 6
KEYS = ("schema", "audience", "offer", "proof", "tone_notes", "banned_words", "directions")


class InvalidInputs(ValueError):
    pass


def _text(obj: dict, key: str, limit: int) -> str:
    v = obj.get(key, "")
    if not isinstance(v, str):
        raise InvalidInputs(f"{key} must be a string")
    v = v.strip()
    if len(v) > limit:
        raise InvalidInputs(f"{key} is longer than {limit} characters")
    return v


def _lines(obj: dict, key: str, max_items: int, max_len: int) -> list[str]:
    v = obj.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise InvalidInputs(f"{key} must be a list of strings")
    items = [x.strip() for x in v if x.strip()]
    if len(items) > max_items:
        raise InvalidInputs(f"{key} has more than {max_items} items")
    if any(len(x) > max_len for x in items):
        raise InvalidInputs(f"{key} has an item longer than {max_len} characters")
    return items


def validate(obj: Any) -> dict:
    """The normalised inputs (all keys present, schema 1); unknown keys and limit breaches raise InvalidInputs."""
    if not isinstance(obj, dict):
        raise InvalidInputs("not a JSON object")
    extra = sorted(set(obj) - set(KEYS))
    if extra:
        raise InvalidInputs(f"unknown key: {', '.join(map(str, extra))}")
    if obj.get("schema", SCHEMA) != SCHEMA or isinstance(obj.get("schema"), bool):
        raise InvalidInputs(f"schema must be {SCHEMA}")
    n = obj.get("directions", DEFAULT_DIRECTIONS)
    if isinstance(n, bool) or not isinstance(n, int) or not MIN_DIRECTIONS <= n <= MAX_DIRECTIONS:
        raise InvalidInputs(f"directions must be a whole number from {MIN_DIRECTIONS} to {MAX_DIRECTIONS}")
    return {"schema": SCHEMA,
            "audience": _text(obj, "audience", MAX_AUDIENCE),
            "offer": _text(obj, "offer", MAX_OFFER),
            "proof": _lines(obj, "proof", MAX_PROOF, MAX_PROOF_LINE),
            "tone_notes": _text(obj, "tone_notes", MAX_TONE),
            "banned_words": _lines(obj, "banned_words", MAX_BANNED, MAX_BANNED_WORD),
            "directions": n}


def read(path: Path) -> dict:
    try:
        return validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        if isinstance(e, InvalidInputs):
            raise
        raise InvalidInputs(f"{path}: {e}") from None


def write(obj: Any, outbox: Path) -> dict:
    """Validate and put the inputs into the spec outbox as refs/site-inputs.json; returns the normalised dict."""
    from . import specoutbox
    clean = validate(obj)
    specoutbox.put_bytes(Path(outbox), REL_PATH, (json.dumps(clean, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return clean


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.sitecopy", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="check a site-inputs file; exit 1 when invalid")
    p.add_argument("--file", required=True)
    p.add_argument("--json", action="store_true", help="print the normalised inputs")
    p = sub.add_parser("write", help="validate and put the file into the spec outbox")
    p.add_argument("--file", required=True)
    p.add_argument("--outbox", required=True, help="the spec outbox root")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "validate":
            clean = read(Path(a.file))
            print(json.dumps(clean, indent=1, ensure_ascii=False) if a.json else "site inputs ok")
        else:
            read(Path(a.file))
            raw = json.loads(Path(a.file).read_text(encoding="utf-8"))
            write(raw, Path(a.outbox))
    except InvalidInputs as e:
        print(f"invalid site inputs: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
