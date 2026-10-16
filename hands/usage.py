"""Plan usage (5-hour session window and weekly window) as reported by Claude Code.

Source: the `rate_limit_event` line that `claude -p --output-format stream-json --verbose` emits
(observed in Claude Code 2.1.301; not a documented contract, so every field is optional and the
dashboard says "no recent data" when it is missing). Shape seen:
  {"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":...,"rateLimitType":"five_hour",
   "unifiedWindows":{"five_hour":{"utilization":0.19,"resetsAt":1792174800},
                     "seven_day":{"utilization":0.16,"resetsAt":1792530000}}}}
Written to ~/.config/hands/usage.json (shared by all projects: they use the same subscription).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config as cfgmod

WINDOWS = ("five_hour", "seven_day")


def path() -> Path:
    return cfgmod.CONFIG_HOME / "usage.json"


def extract(event: dict) -> dict | None:
    """Normalised usage from one stream event, or None."""
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    out: dict = {"status": str(info.get("status", ""))[:40], "at": int(time.time())}
    wins = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    for w in WINDOWS:
        x = wins.get(w)
        if isinstance(x, dict):
            u, r = x.get("utilization"), x.get("resetsAt")
            if (isinstance(u, (int, float)) and 0 <= u <= 10 and isinstance(r, (int, float))
                    and time.time() - 300 < r < time.time() + 8 * 86400):   # plausible future reset only
                out[w] = {"used_pct": round(float(u) * 100, 1), "resets_at": int(r)}
    if not any(w in out for w in WINDOWS):
        # older/other shape: a single window described at the top level
        t = info.get("rateLimitType")
        if t in WINDOWS and isinstance(info.get("resetsAt"), (int, float)):
            out[t] = {"used_pct": None, "resets_at": int(info["resetsAt"])}
    return out if any(w in out for w in WINDOWS) else None


def record(u: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".usage.json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(u))
    os.replace(tmp, p)


def record_from_stream(stream_file: Path) -> dict | None:
    """After a session: store the last usage event found in its stream output."""
    last = None
    try:
        with open(stream_file, "rb") as f:
            for line in f:
                if b"rate_limit_event" not in line:
                    continue
                try:
                    u = extract(json.loads(line))
                except ValueError:
                    continue
                if u:
                    last = u
    except OSError:
        return None
    if last:
        record(last)
    return last


def read() -> dict:
    try:
        u = json.loads(path().read_text())
        return u if isinstance(u, dict) else {}
    except (OSError, ValueError):
        return {}


def refresh(model: str = "claude-haiku-5-5") -> int:
    """Tiny probe session (one short reply on Haiku) just to read fresh usage. Uses the first project's
    config for the image and token file."""
    from .__main__ import project_names
    from .loop import Runner
    from .state import Lock
    cfgs = []
    for n in project_names():
        try:
            c = cfgmod.load(n)
        except SystemExit:
            continue
        if not Lock.held(c.state_dir / "runner.lock"):      # don't share a probe dir with a live runner
            cfgs.append(c)
    if not cfgs:
        print("no idle project to run the probe with")
        return 1
    cfgmod.CONFIG_HOME.mkdir(parents=True, exist_ok=True)
    try:
        lk = Lock(cfgmod.CONFIG_HOME / "usage-refresh.lock").__enter__()
    except SystemExit:
        print("a refresh is already running")
        return 1
    try:
        r = Runner(cfgs[0])
        res = r.probe(model)
        if res.kind == "model_access":
            res = r.probe(r.cfg.coder_model)
    finally:
        lk.__exit__(None, None, None)
    u = read()
    if u and time.time() - u.get("at", 0) < 120:
        print(json.dumps(u))
        return 0
    print(f"no usage data in the probe output (kind={res.kind})")
    return 1
