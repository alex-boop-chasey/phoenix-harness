"""Parsing `claude -p --output-format json` results and classifying failures."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RATE_LIMIT = re.compile(
    r"rate.?limit|usage limit|hit your (?:session |weekly |opus |sonnet |fable |individual )?(?:usage )?limit"
    r"|too many requests|\b429\b|limit reached|spend limit",
    re.I,
)
AUTH = re.compile(r"invalid api key|oauth token (?:has )?expired|authentication|not logged in|401|please run /login|invalid bearer", re.I)
MODEL_ACCESS = re.compile(
    r"model.{0,40}(?:not (?:found|available|supported)|unavailable|do(?:es)? not have access|not allowed)"
    r"|not_found_error|invalid model|no access to model|permission_error",
    re.I,
)
OVERLOADED = re.compile(r"overloaded|\b529\b|internal server error|\b5\d\d\b.*api|api error", re.I)


@dataclass
class Result:
    ok: bool                 # process exited 0 and JSON says not is_error
    rc: int
    is_error: bool
    subtype: str
    text: str                # .result plus any .errors
    turns: int
    cost: float
    models: list[str]
    session_id: str
    raw: dict

    @property
    def kind(self) -> str:
        """ok | rate_limit | auth | model_access | overloaded | timeout | max_turns | crash"""
        if self.rc in (124, 137) and not self.raw:
            return "timeout"
        if self.ok:
            return "ok"
        if self.subtype == "error_max_turns":
            return "max_turns"
        # Classify infra errors only from short CLI/API error text, never from a long model answer that
        # happens to mention "authentication" or "429" (this is a web-app build).
        t = self.text if (len(self.text) <= 600 or not self.raw) else ""
        status = str(self.raw.get("api_error_status") or "")
        if status == "429":
            return "rate_limit"
        if status in ("401", "403") and AUTH.search(self.text):
            return "auth"
        if RATE_LIMIT.search(t):
            return "rate_limit"
        if AUTH.search(t):
            return "auth"
        if MODEL_ACCESS.search(t):
            return "model_access"
        if OVERLOADED.search(t):
            return "overloaded"
        if self.rc in (124, 137):
            return "timeout"
        return "crash"


MAX_LINE = 5_000_000


def _read_result(out_file: Path) -> dict:
    """The CLI's result object, failing closed.

    stream-json: the LAST non-empty line must be valid JSON with type "result", and it must be the ONLY
    line that starts like a result line. Anything else (a corrupted or appended last line, an extra
    forged result line written to the container's stdout by a process inside it) -> {} (crash).
    Plain json (one object, no "type" lines): that object."""
    count, last, nonempty = 0, b"", 0
    try:
        with open(out_file, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                nonempty += 1
                if b'"result"' in line and len(line) <= MAX_LINE:
                    try:
                        o = json.loads(line)
                        if isinstance(o, dict) and o.get("type") == "result":
                            count += 1
                    except (json.JSONDecodeError, UnicodeError):
                        pass
                last = line if len(line) <= MAX_LINE else b"<too long>"
    except OSError:
        return {}
    if not last:
        return {}
    try:
        obj = json.loads(last)
    except (json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    if obj.get("type") == "result":
        return obj if count == 1 else {}
    if nonempty == 1 and "type" not in obj:        # plain --output-format json
        return obj
    return {}


def parse(out_file: Path, rc: int, stderr_file: Path | None = None) -> Result:
    raw = _read_result(out_file)
    errs = raw.get("errors") or []
    if isinstance(errs, list):
        errs = " ".join(str(e) for e in errs)
    text = f"{raw.get('result') or ''} {errs}".strip()
    if not raw and stderr_file and stderr_file.exists():
        text = stderr_file.read_text(errors="replace")[-4000:]
    is_error = bool(raw.get("is_error", not raw))
    usage = raw.get("modelUsage") or {}
    return Result(
        ok=(rc == 0 and bool(raw) and not is_error),
        rc=rc,
        is_error=is_error,
        subtype=str(raw.get("subtype", "")),
        text=text,
        turns=int(raw.get("num_turns") or 0),
        cost=float(raw.get("total_cost_usd") or 0.0),
        models=sorted(usage.keys()) if isinstance(usage, dict) else [],
        session_id=str(raw.get("session_id", "")),
        raw=raw,
    )


# ------------------------------------------------------------- reset time parsing
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_resets_at(text: str, tz: str = "Australia/Brisbane", now: float | None = None) -> float | None:
    """Epoch seconds of the limit reset, or None. Bare clock times are read in `tz`
    (the container's TZ, which renders the message). Future-biased."""
    zone = ZoneInfo(tz)
    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), zone)
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*(Z|UTC|[+-]\d{2}:?\d{2})?", text)
    if m:
        d, hh, mm, ss, z = m.groups()
        base = datetime.fromisoformat(f"{d}T{hh}:{mm}:{ss or '00'}")
        if z in ("Z", "UTC"):
            base = base.replace(tzinfo=ZoneInfo("UTC"))
        elif z:
            z = z.replace(":", "")
            base = datetime.fromisoformat(f"{d}T{hh}:{mm}:{ss or '00'}{z[:3]}:{z[3:]}")
        else:
            base = base.replace(tzinfo=zone)
        return base.timestamp()
    m = re.search(r"resets\s+(?:(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if not m:
        return None
    wd, h, mi, ap = m.groups()
    h = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
    cand = now_dt.replace(hour=h, minute=int(mi or 0), second=0, microsecond=0)
    if wd:
        delta = (_WEEKDAYS.index(wd.lower()[:3]) - cand.weekday()) % 7
        cand += timedelta(days=delta)
    if cand <= now_dt:
        cand += timedelta(days=7 if wd else 1)
    return cand.timestamp()


# ------------------------------------------------------------- verdict parsing
def parse_verdict(text: str) -> dict | None:
    """Evaluator ends with a line: VERDICT: {"pass": bool, "reasons": [...], ...}"""
    for line in reversed(text.splitlines()):
        line = line.strip().strip("`")
        if line.upper().startswith("VERDICT:"):
            try:
                v = json.loads(line.split(":", 1)[1].strip())
            except json.JSONDecodeError:
                return None
            if isinstance(v, dict) and isinstance(v.get("pass"), bool):
                v.setdefault("reasons", [])
                if not isinstance(v["reasons"], list):
                    v["reasons"] = [str(v["reasons"])]
                return v
            return None
    return None


def parse_summary(text: str) -> str:
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s.upper().startswith("SUMMARY:"):
            return s.split(":", 1)[1].strip()[:2000]
    return text.strip()[-1500:]


def parse_tagged(text: str, tag: str) -> str:
    """Last line starting with `TAG:` (e.g. SPEC_PROBLEM), or ''."""
    for line in reversed(text.splitlines()):
        s = line.strip().strip("`")
        if s.upper().startswith(tag + ":"):
            return s.split(":", 1)[1].strip()[:2000]
    return ""
