"""Escalation resolver: toggle, gate, strict decision parsing, execution state machine, briefing, launch spec.

The resolver session only advises: its decision is parsed fail-closed (hands.claude._read_result, the same
reader as the evaluator verdict; stderr is never parsed) and anything invalid becomes escalate_to_alex.
CLI: python3 -m hands.resolver status|on|off|set-cap|gate|parse|execute|briefing|launch-spec
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import claude, designfiles, models, spec as specmod
from .config import Config

CAUSES_AUTO = ("attempts_exhausted", "same_failure", "evaluator_no_verdict", "infra_repeated", "overloaded",
               "rate_limit_timeout", "model_access", "runner_error", "push_failed")
CAUSES_NEVER = ("spec_problem", "spec_invalid", "passed_changed", "allowlist", "secret_leak", "auth", "disk_low",
                "verdict_nonce", "diverged", "round_conflict", "preflight", "watchdog", "human_step", "tests_or_spec",
                "design_gate")
_RETRY_CAUSES = ("attempts_exhausted", "same_failure", "evaluator_no_verdict", "rate_limit_timeout", "overloaded",
                 "infra_repeated", "model_access", "push_failed")
_RESTART_CAUSES = ("runner_error", "infra_repeated", "overloaded", "evaluator_no_verdict", "push_failed")
ACTIONS = {
    "hint_and_retry": _RETRY_CAUSES,
    "schedule_retry": _RETRY_CAUSES,
    "restart_after_crash": _RESTART_CAUSES,
    "escalate_to_alex": CAUSES_AUTO,
}
_KEYS = {
    "hint_and_retry": {"action", "reason", "hint"},
    "schedule_retry": {"action", "reason", "retry_at"},
    "restart_after_crash": {"action", "reason"},
    "escalate_to_alex": {"action", "reason", "note", "amend_draft", "root_command"},
}
PER_ESCALATION_CAP = 2
DEFAULT_DAILY_CAP = 6
REASON_MAX, HINT_MAX, NOTE_MAX, ROOT_MAX = 500, 2000, 4000, 400
RETRY_MIN_S, RETRY_MAX_S = 5 * 60, 48 * 3600
TIMEOUT_S = 1800
DISALLOWED = "Write,Edit,NotebookEdit,Agent,WebFetch,WebSearch"


# ------------------------------------------------------------------ files
def _read_json(path: Path, default):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _append(path: Path, obj: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def _jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict):
                out.append(o)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ settings and toggle
def settings(state_dir) -> dict:
    s = {"schema": 1, "enabled": False, "self_enabled": False, "daily_cap": DEFAULT_DAILY_CAP}
    raw = _read_json(Path(state_dir) / "resolver.json", {})
    for k in ("enabled", "self_enabled"):
        if isinstance(raw.get(k), bool):
            s[k] = raw[k]
    cap = raw.get("daily_cap")
    if isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= 20:
        s["daily_cap"] = cap
    return s


def _save_settings(state_dir, s: dict, by: str) -> None:
    d = Path(state_dir)
    _write_json(d / "resolver.json", s)
    _append(d / "resolver-audit.jsonl", {"ts": int(time.time()), "enabled": s["enabled"],
                                        "self_enabled": s["self_enabled"], "daily_cap": s["daily_cap"], "by": by})


def toggle(state_dir, on: bool, self_too: bool = False, by: str = "") -> dict:
    s = settings(state_dir)
    s["enabled"] = bool(on)
    if on:
        s["self_enabled"] = bool(self_too)
    _save_settings(state_dir, s, by)
    if not on:
        sp = Path(state_dir) / "state.json"
        st = _read_json(sp, {})
        if st.get("status") == "paused_resolver":
            st["status"] = "escalation"
            st["reason"] = f"{st.get('reason', '')} (resolver turned off; scheduled retry cancelled)".strip()
            st.pop("resolver_pending", None)
            _write_json(sp, st)
    return s


def set_cap(state_dir, n: int) -> dict:
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 20:
        raise ValueError("daily cap must be 1-20")
    s = settings(state_dir)
    s["daily_cap"] = n
    _save_settings(state_dir, s, "")
    return s


# ------------------------------------------------------------------ gate
def gate(state_dir, cause: str, step: str, is_self: bool, now: float) -> dict:
    s = settings(state_dir)
    if cause in CAUSES_NEVER:
        return {"run": False, "why": f"never auto-resolved: {cause}"}
    if cause not in CAUSES_AUTO:
        return {"run": False, "why": "unknown cause"}
    if not s["enabled"]:
        return {"run": False, "why": "resolver off"}
    if is_self and not s["self_enabled"]:
        return {"run": False, "why": "self project: resolver not enabled here"}
    esc = _read_json(Path(state_dir) / "state.json", {}).get("resolver_escalation") or {}
    if esc.get("step") == step and esc.get("cause") == cause and int(esc.get("decisions") or 0) >= PER_ESCALATION_CAP:
        return {"run": False, "why": "per-escalation cap reached"}
    recent = [r for r in _jsonl(Path(state_dir) / "resolver.jsonl")
              if isinstance(r.get("at"), (int, float)) and r["at"] > now - 86400]
    if len(recent) >= s["daily_cap"]:
        return {"run": False, "why": "daily cap reached"}
    return {"run": True, "why": ""}


# ------------------------------------------------------------------ decisions
def validate_decision(d: dict, cause: str | None, now: float, secrets: dict | None = None) -> str:
    """The problem with a decision as the session wrote it (only `nonce` besides the action's keys), or ''."""
    if not isinstance(d, dict):
        return "decision is not an object"
    action = d.get("action")
    if action not in ACTIONS:
        return f"unknown action {action!r}"
    extra = set(d) - _KEYS[action] - {"nonce"}
    if extra:
        return f"keys not allowed for {action}: {', '.join(sorted(extra))}"
    if cause is not None and cause not in ACTIONS[action]:
        return f"{action} is not allowed for cause {cause}"

    def text(key: str, limit: int, required: bool = True) -> str:
        v = d.get(key)
        if v is None and not required:
            return ""
        if not isinstance(v, str) or (required and not v.strip()):
            return f"{key} must be a non-empty string"
        return f"{key} is longer than {limit} characters" if len(v) > limit else ""

    if not isinstance(d.get("reason"), str):
        return "reason must be a string"
    if len(d["reason"]) > REASON_MAX:
        return f"reason is longer than {REASON_MAX} characters"
    if action == "hint_and_retry":
        p = text("hint", HINT_MAX)
        if p:
            return p
        hits = designfiles.secret_hits(d["hint"], secrets)
        return f"hint contains a secret ({', '.join(hits)})" if hits else ""
    if action == "schedule_retry":
        at = d.get("retry_at")
        if not isinstance(at, str):
            return "retry_at must be an ISO 8601 string"
        try:
            when = datetime.datetime.fromisoformat(at)
        except ValueError:
            return "retry_at is not ISO 8601"
        if when.tzinfo is None:
            return "retry_at needs a UTC offset"
        if not RETRY_MIN_S <= when.timestamp() - now <= RETRY_MAX_S:
            return "retry_at must be 5 minutes to 48 hours ahead"
        return ""
    if action == "escalate_to_alex":
        for key in ("note", "amend_draft"):
            p = text(key, NOTE_MAX, required=key == "note")
            if p:
                return p
        rc = d.get("root_command")
        if rc is not None:
            if not isinstance(rc, str) or not rc.strip() or "\n" in rc or "\r" in rc:
                return "root_command must be one line"
            if len(rc) > ROOT_MAX:
                return f"root_command is longer than {ROOT_MAX} characters"
    return ""


def _escalation(problem: str) -> dict:
    return {"valid": False, "action": "escalate_to_alex", "problem": problem, "reason": problem[:REASON_MAX], "note": ""}


def parse_decision(result_file, nonce: str, cause: str, now: float, secrets: dict | None = None) -> dict:
    raw = claude._read_result(Path(result_file))
    if not raw:
        return _escalation("no clean result in the session output")
    if raw.get("is_error") is not False or raw.get("subtype") != "success":
        return _escalation("the session did not finish cleanly")
    text = raw.get("result")
    if not isinstance(text, str):
        return _escalation("the session result has no text")
    lines = [ln.strip().strip("`").strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[-1].startswith("DECISION:"):
        return _escalation("the result does not end with a DECISION line")
    try:
        d = json.loads(lines[-1][len("DECISION:"):].strip())
    except ValueError:
        return _escalation("the DECISION line is not valid JSON")
    if not isinstance(d, dict):
        return _escalation("the DECISION is not an object")
    got = d.get("nonce")
    if not isinstance(got, str) or not hmac.compare_digest(got.encode(), str(nonce).encode()):
        return _escalation("nonce mismatch")
    problem = validate_decision(d, cause, now, secrets)
    if problem:
        return _escalation(problem)
    d.pop("nonce")
    return {**d, "valid": True, "problem": ""}


# ------------------------------------------------------------------ execution
def execute(state: dict, step: str, cause: str, decision: dict, now: float) -> dict:
    esc = state.get("resolver_escalation")
    if not isinstance(esc, dict) or esc.get("step") != step or esc.get("cause") != cause:
        esc = {"step": step, "cause": cause, "decisions": 0}
    esc["decisions"] = int(esc.get("decisions") or 0) + 1
    state["resolver_escalation"] = esc

    if not isinstance(decision, dict):
        decision = {}
    if decision.get("valid") is not True:
        problem = str(decision.get("problem") or "decision not valid")
    else:
        core = {k: v for k, v in decision.items() if k not in ("valid", "problem")}
        # acceptance/c1/resolver.test.mjs executes restart_after_crash for attempts_exhausted, so for
        # auto-resolvable causes the restart/cause pairing is enforced by parse_decision only
        lenient = core.get("action") == "restart_after_crash" and cause in CAUSES_AUTO
        problem = validate_decision(core, None if lenient else cause, now)
        if not problem and core["action"] in ("hint_and_retry", "schedule_retry") \
                and (state.get("steps") or {}).get(step, {}).get("status") == "passed":
            problem = f"step {step} has passed; a passed step is never reset"
        decision = core
    if problem:
        decision = _escalation(problem)
    action = decision["action"]
    if action == "hint_and_retry":
        rec = state.setdefault("steps", {}).setdefault(step, {})
        rec.pop("reasons", None)
        rec.update({"status": "pending", "attempts": 0, "resolver_hint": decision["hint"]})
        state["status"] = "running"
        state.pop("resolver_pending", None)
    elif action == "schedule_retry":
        resume = int(datetime.datetime.fromisoformat(decision["retry_at"]).timestamp())
        state["status"] = "paused_resolver"
        state["resolver_pending"] = {"step": step, "resume_at": resume, "reason": decision.get("reason", "")}
    elif action == "restart_after_crash":
        state["status"] = "running"
        state.pop("resolver_pending", None)
    else:
        note = decision.get("note") or decision.get("problem") or ""
        state["status"] = "escalation"
        state["reason"] = f"{state.get('reason', '')} | resolver: {note}"
        state["resolver_note"] = decision.get("note", "")
        for key in ("amend_draft", "root_command"):
            if decision.get(key):
                state[f"resolver_{key}"] = decision[key]
            else:
                state.pop(f"resolver_{key}", None)
        state.pop("resolver_pending", None)
    return {"executed": not problem, "status": state["status"]}


def record_run(state_dir, at: float, step: str, cause: str, decision: dict, executed: bool) -> None:
    _append(Path(state_dir) / "resolver.jsonl", {
        "at": at, "step": step, "cause": cause, "action": decision.get("action", ""),
        "valid": decision.get("valid") is True, "executed": executed, "problem": decision.get("problem", ""),
        "reason": str(decision.get("reason", ""))[:500], "note": str(decision.get("note", ""))[:4000],
        "root_command": str(decision.get("root_command", ""))[:400]})


# ------------------------------------------------------------------ briefing
def _frame(source: str, body: str) -> str:
    body = body.replace("</untrusted-data", "<\\/untrusted-data")
    return f'<untrusted-data source="{source}">\n{body}\n</untrusted-data>'


def _tail(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _git_log(app_dir) -> str:
    if not app_dir:
        return ""
    try:
        r = subprocess.run(["git", "-C", str(app_dir), "log", "--oneline", "-15"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def briefing(spec_dir, state_dir, step: str, attempt: int, cause: str, reason: str, nonce: str,
             app_dir=None, default_max_attempts: int | None = None) -> str:
    sp = specmod.load(Path(spec_dir))
    st = sp.step(step)
    sd = Path(state_dir)
    adir = sd / "attempts" / f"{step}-{attempt}"
    max_attempts = st.max_attempts or default_max_attempts or Config.default_max_attempts
    out = [f"# Escalation briefing: {step} {st.title}", "",
           f"Step: {step}\nTitle: {st.title}\nAttempt: {attempt} (max {max_attempts})\nCause code: {cause}\n"
           f"Nonce: {nonce}", "",
           "## Goal", _frame("spec goal", st.goal), "", "## Refs", _frame("spec refs", specmod.render_refs(sp, st)), "",
           "## Acceptance commands", *[f"- `{c}`" for c in st.acceptance], "",
           "## Escalation text", _frame("runner escalation", reason), ""]
    out += ["## Failure reasons (last 80 lines)", _frame("attempt reasons", _tail(adir / "reasons.txt", 80)), ""]
    logs = "\n".join(f"### {p.name}\n{_tail(p, 80)}" for p in sorted(adir.glob("accept-*.log")))
    out += ["## Acceptance log tail (last 80 lines each)", _frame("acceptance log", logs), ""]
    verdicts = []
    for p in sorted(adir.glob("eval-*.json")):
        r = claude._read_result(p)
        if r:
            verdicts.append(f"### {p.name}\n{str(r.get('result') or '')[-3000:]}")
    out += ["## Evaluator verdict", _frame("evaluator", "\n".join(verdicts)), ""]
    out += ["## runner.log tail (last 60 lines)", _frame("runner.log", _tail(sd / "runner.log", 60)), ""]
    notes = "\n".join(json.dumps(o)[:1600] for o in _jsonl(sd / "progress.jsonl")[-5:])
    out += ["## Last progress notes", _frame("progress notes", notes), "",
            "## git log --oneline -15", _frame("git log", _git_log(app_dir).rstrip()), "",
            "## Allowed actions for this cause"]
    out += [f"- {a}" for a, causes in ACTIONS.items() if cause in causes]
    out += ["", f"End your final message with one line `DECISION: {{json}}` that includes `\"nonce\": \"{nonce}\"`.", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ launch spec
def launch_spec(spec, app, attempt_dir, prompts, claude_env, model: str, effort: str = "") -> dict:
    models.validate("resolver", model, effort)
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--max-turns", "60", "--dangerously-skip-permissions", "--no-session-persistence",
            "--append-system-prompt-file", "/opt/hands/prompts/resolver.md", "--disallowedTools", DISALLOWED]
    return {"cmd": cmd,
            "mounts": [{"src": str(src), "dst": dst, "ro": True} for src, dst in
                       ((spec, "/spec"), (app, "/app"), (attempt_dir, "/attempt"), (prompts, "/opt/hands/prompts"))],
            "env_files": [str(claude_env)], "network": True, "timeout_s": TIMEOUT_S}


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hands.resolver")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, *flags):
        p = sub.add_parser(name)
        for f, kw in flags:
            p.add_argument(f, **kw)

    sd = ("--state-dir", {"required": True})
    js = ("--json", {"action": "store_true"})
    now = ("--now", {"type": float, "default": None})
    add("status", sd, js)
    add("on", sd, ("--self", {"action": "store_true"}), ("--by", {"default": ""}))
    add("off", sd, ("--by", {"default": ""}))
    add("set-cap", sd, ("--cap", {"type": int, "required": True}))
    add("gate", sd, ("--cause", {"required": True}), ("--step", {"required": True}),
        ("--self-project", {"action": "store_true"}), now, js)
    add("parse", ("--file", {"required": True}), ("--nonce", {"required": True}), ("--cause", {"required": True}),
        now, ("--secrets", {"default": None}), js)
    add("execute", sd, ("--step", {"required": True}), ("--cause", {"required": True}),
        ("--decision", {"required": True}), now, js)
    add("briefing", ("--spec", {"required": True}), sd, ("--step", {"required": True}),
        ("--attempt", {"type": int, "required": True}), ("--cause", {"required": True}),
        ("--reason", {"required": True}), ("--nonce", {"required": True}), ("--out", {"required": True}),
        ("--app", {"default": None}))
    add("launch-spec", ("--spec", {"required": True}), ("--app", {"required": True}),
        ("--attempt-dir", {"required": True}), ("--prompts", {"required": True}),
        ("--claude-env", {"required": True}), ("--model", {"required": True}), ("--effort", {"default": ""}), js)
    a = ap.parse_args(argv)
    t = time.time() if getattr(a, "now", None) is None else a.now
    try:
        if a.cmd == "status":
            print(json.dumps(settings(a.state_dir)))
        elif a.cmd == "on":
            toggle(a.state_dir, True, a.self, a.by)
        elif a.cmd == "off":
            toggle(a.state_dir, False, by=a.by)
        elif a.cmd == "set-cap":
            set_cap(a.state_dir, a.cap)
        elif a.cmd == "gate":
            print(json.dumps(gate(a.state_dir, a.cause, a.step, a.self_project, t)))
        elif a.cmd == "parse":
            secrets = designfiles.load_secrets(a.secrets) if a.secrets else None
            print(json.dumps(parse_decision(a.file, a.nonce, a.cause, t, secrets)))
        elif a.cmd == "execute":
            dec = json.loads(Path(a.decision).read_text())
            sp = Path(a.state_dir) / "state.json"
            state = _read_json(sp, {})
            res = execute(state, a.step, a.cause, dec, t)
            _write_json(sp, state)
            record_run(a.state_dir, t, a.step, a.cause, dec if isinstance(dec, dict) else {}, res["executed"])
            print(json.dumps(res))
        elif a.cmd == "briefing":
            text = briefing(a.spec, a.state_dir, a.step, a.attempt, a.cause, a.reason, a.nonce, app_dir=a.app)
            Path(a.out).write_text(text)
            print(hashlib.sha256(text.encode()).hexdigest())
        else:
            print(json.dumps(launch_spec(a.spec, a.app, a.attempt_dir, a.prompts, a.claude_env, a.model, a.effort)))
    except (ValueError, OSError, KeyError, specmod.SpecError, models.InvalidModel) as e:
        print(f"resolver: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
