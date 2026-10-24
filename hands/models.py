"""Model choice per project: the harness allowlist and one precedence function.

auto-override (state.json) > manual (<config_home>/projects/<name>.models.json) > project TOML > default.
CLI: python3 -m hands.models allowlist|validate|effective|set|reset|check|usage|classify-limit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ALLOWLIST: dict[str, str] = {
    "claude-fable-5-1": "Most capable; best for planning, hard builds and review; highest cost.",
    "claude-opus-5-5": "Strong all-round model; good fallback for demanding work.",
    "claude-sonnet-5-5": "Balanced speed and cost; good for subagents and routine steps.",
    "claude-haiku-5-5": "Fastest and cheapest; for light, well-bounded tasks.",
}
ROLES = ("planner", "coder", "evaluator", "subagents", "resolver", "fallback")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_ROLES = ("planner", "coder", "evaluator", "resolver")
TOML_KEYS = {"planner": "planner_model", "coder": "coder_model", "evaluator": "eval_model",
             "fallback": "fallback_model", "resolver": "resolver_model"}
DEFAULT_CODER = "claude-fable-5-1"
DEFAULT_FALLBACK = "claude-opus-5-5"
DEFAULT_SUBAGENTS = "claude-sonnet-5-5"
_LIMIT_RE = re.compile(r"hit your (?:weekly |daily )?(opus|sonnet|fable|haiku) limit", re.I)


class InvalidModel(ValueError):
    pass


def validate(role: str, model: str, effort: str = "") -> None:
    if role not in ROLES:
        raise InvalidModel(f"unknown role {role!r}")
    if model not in ALLOWLIST:
        raise InvalidModel(f"model {model!r} is not on the allowlist")
    if effort and (effort not in EFFORTS or role not in EFFORT_ROLES):
        raise InvalidModel(f"effort {effort!r} is not valid for {role}")


def models_file(cfg_or_path) -> Path:
    if isinstance(cfg_or_path, (str, Path)):
        return Path(cfg_or_path)
    from .config import CONFIG_HOME
    return CONFIG_HOME / "projects" / f"{cfg_or_path.name}.models.json"


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def load_manual(path) -> dict:
    data = _read_json(path, {})
    roles = data.get("roles")
    data["roles"] = roles if isinstance(roles, dict) else {}
    data.setdefault("schema", 1)
    return data


def _active_override(role: str, state: dict, now: float) -> dict | None:
    ov = (state.get("model_overrides") or {}).get(role)
    if isinstance(ov, dict) and ov.get("model"):
        until = ov.get("until")
        return ov if until is None or until > now else None
    legacy = state.get("model_override")
    if role in ("coder", "evaluator") and legacy:
        return {"model": legacy, "reason": "model access", "until": None}
    return None


def effective(role: str, toml: dict, manual: dict, state: dict, now: float) -> dict:
    roles = (manual or {}).get("roles") or {}
    chosen = roles.get(role) or {}
    ov = _active_override(role, state or {}, now)
    if ov:
        model, effort, source = ov["model"], chosen.get("effort", ""), "auto-override"
    elif chosen.get("model"):
        model, effort, source = chosen["model"], chosen.get("effort", ""), "manual"
    elif toml.get(TOML_KEYS.get(role, "")):
        model, effort, source = toml[TOML_KEYS[role]], "", "toml"
    else:
        effort, source = "", "default"
        if role == "planner":
            model = effective("coder", toml, manual, state, now)["model"]
        elif role == "resolver":
            model = effective("evaluator", toml, manual, state, now)["model"]
        elif role == "subagents":
            model = DEFAULT_SUBAGENTS
        elif role == "fallback":
            model = DEFAULT_FALLBACK
        else:
            model = DEFAULT_CODER
    if role not in EFFORT_ROLES:
        effort = ""
    return {"model": model, "effort": effort, "source": source,
            "note": ALLOWLIST.get(model, ""), "override": ov}


def effective_all(toml: dict, manual: dict, state: dict, now: float) -> dict:
    return {r: effective(r, toml, manual, state, now) for r in ROLES}


def toml_values(cfg) -> dict:
    """The project TOML's model keys as read into Config (empty = not set)."""
    return {key: getattr(cfg, key, "") for key in TOML_KEYS.values()}


def for_session(cfg, role: str, state: dict | None = None, now: float | None = None) -> dict:
    """Effective choice for one session start, read afresh from the TOML values, models.json and state.json."""
    if state is None:
        state = _read_json(cfg.state_dir / "state.json", {})
    return effective(role, toml_values(cfg), load_manual(models_file(cfg)), state,
                     time.time() if now is None else now)


def family_of(model: str) -> str:
    return next((f for f in ("fable", "opus", "sonnet", "haiku") if f in model), "")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save(path: Path, data: dict) -> None:
    path = Path(path)
    if path.exists():
        _atomic_write(path.with_name(path.name + ".bak"), path.read_text())
    data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _atomic_write(path, json.dumps(data, indent=2) + "\n")


def _audit(audit, role, old, new, old_effort, new_effort, by) -> None:
    audit = Path(audit)
    audit.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "role": role, "old": old, "new": new,
            "old_effort": old_effort, "new_effort": new_effort, "by": by}
    with audit.open("a") as f:
        f.write(json.dumps(line) + "\n")


def set_choice(path, role: str, model: str, effort: str, audit, by: str) -> dict:
    validate(role, model, effort)
    data = load_manual(path)
    old = data["roles"].get(role) or {}
    data["roles"][role] = {"model": model, "effort": effort}
    _save(Path(path), data)
    _audit(audit, role, old.get("model", ""), model, old.get("effort", ""), effort, by)
    return {"old": old.get("model", ""), "new": model}


def reset(path, audit, by: str) -> None:
    data = load_manual(path)
    for role, c in data["roles"].items():
        _audit(audit, role, c.get("model", ""), "", c.get("effort", ""), "", by)
    data["roles"] = {}
    _save(Path(path), data)


def check(toml: dict, manual: dict) -> list[str]:
    problems = []
    for role, key in TOML_KEYS.items():
        m = toml.get(key)
        if m and m not in ALLOWLIST:
            problems.append(f"unknown model {m} for {role}")
    for role, c in ((manual or {}).get("roles") or {}).items():
        m = c.get("model") if isinstance(c, dict) else None
        if m and m not in ALLOWLIST:
            problems.append(f"unknown model {m} for {role}")
    return problems


def usage(metrics_lines) -> list[dict]:
    out: dict[str, dict] = {}
    for line in metrics_lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        used = rec.get("models")
        m = rec.get("model") or (used[0] if isinstance(used, list) and used else None)
        if not m:
            continue
        e = out.setdefault(m, {"model": m, "sessions": 0, "cost_usd": 0})
        e["sessions"] += 1
        cost = rec.get("cost_usd")
        if isinstance(cost, (int, float)):
            e["cost_usd"] += cost
    return list(out.values())


def classify_limit(text: str) -> dict:
    m = _LIMIT_RE.search(text or "")
    return {"model_only": bool(m), "family": m.group(1).lower() if m else None}


def _toml(path) -> dict:
    try:
        return tomllib.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.models")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("allowlist").add_argument("--json", action="store_true")
    p = sub.add_parser("validate")
    p.add_argument("--role", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--effort", default="")
    p = sub.add_parser("effective")
    p.add_argument("--toml")
    p.add_argument("--models")
    p.add_argument("--state")
    p.add_argument("--now", type=float, default=None)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("set")
    p.add_argument("--models", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--effort", default="")
    p.add_argument("--audit", required=True)
    p.add_argument("--by", default="cli")
    p = sub.add_parser("reset")
    p.add_argument("--models", required=True)
    p.add_argument("--audit", required=True)
    p.add_argument("--by", default="cli")
    p = sub.add_parser("check")
    p.add_argument("--toml")
    p.add_argument("--models")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("usage")
    p.add_argument("--metrics", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("classify-limit")
    p.add_argument("--text", required=True)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def emit(obj):
        print(json.dumps(obj))

    try:
        if a.cmd == "allowlist":
            emit({"models": [{"id": k, "note": v} for k, v in ALLOWLIST.items()], "roles": list(ROLES),
                  "efforts": list(EFFORTS), "effort_roles": list(EFFORT_ROLES)})
        elif a.cmd == "validate":
            validate(a.role, a.model, a.effort)
        elif a.cmd == "effective":
            now = time.time() if a.now is None else a.now
            emit({"roles": effective_all(_toml(a.toml) if a.toml else {}, load_manual(a.models) if a.models else {"roles": {}},
                                         _read_json(a.state, {}) if a.state else {}, now)})
        elif a.cmd == "set":
            emit(set_choice(a.models, a.role, a.model, a.effort, a.audit, a.by))
        elif a.cmd == "reset":
            reset(a.models, a.audit, a.by)
        elif a.cmd == "check":
            problems = check(_toml(a.toml) if a.toml else {}, load_manual(a.models) if a.models else {"roles": {}})
            emit({"problems": problems})
            return 1 if problems else 0
        elif a.cmd == "usage":
            lines = Path(a.metrics).read_text().splitlines() if Path(a.metrics).is_file() else []
            emit({"models": usage(lines)})
        elif a.cmd == "classify-limit":
            emit(classify_limit(a.text))
    except InvalidModel as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
