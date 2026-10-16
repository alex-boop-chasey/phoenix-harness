"""Prompt assembly. The agent never sees the spec repo; it gets only what the runner puts here."""
from __future__ import annotations

import json

from .spec import Spec, Step, render_refs


def acceptance_commands(spec: Spec, step: Step) -> list[str]:
    cmds = [*spec.setup, *spec.regression, *step.acceptance]
    if step.e2e:
        cmds.append(spec.e2e_command.replace("{files}", " ".join(f"acceptance/{e}" for e in step.e2e)))
    # de-duplicate, keep order
    seen, out = set(), []
    for c in cmds:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- `{i}`" for i in items) if items else "- (none)"


def coder(spec: Spec, step: Step, *, attempt: int, max_attempts: int, progress: list[dict],
          git_log: str, feedback: list[str], prev_diff: str, acc_files: list[str] | None = None) -> str:
    parts = [
        f"# Step {step.id}: {step.title}",
        f"Attempt {attempt} of {max_attempts}. Spec commit {spec.commit[:12]}.",
        "## Goal\n" + step.goal,
    ]
    if step.notes:
        parts.append("## Notes from the spec\n" + step.notes)
    parts.append(
        "## Acceptance (the runner runs these, in order, in a clean clone after you finish)\n"
        + _bullets(acceptance_commands(spec, step))
        + "\n\nAcceptance tests live in `/workspace/acceptance/` (read-only)."
        + (("\nFiles there for this step: " + ", ".join(f"`acceptance/{f}`" for f in acc_files[:60])
            + (" ..." if len(acc_files) > 60 else "")) if acc_files is not None else "")
        + (f"\nEnd-to-end flows for this step: {', '.join('acceptance/' + e for e in step.e2e)}" if step.e2e else "")
    )
    parts.append(
        "## Allowed paths (any change elsewhere rejects the attempt)\n" + _bullets(spec.allowed_paths)
        + ("\nText files allowed only at: " + ", ".join(f"`{p}`" for p in spec.allow_text_paths) if spec.allow_text_paths else "")
    )
    parts.append("## Reference docs\n" + (render_refs(spec, step) or "(none)"))
    if progress:
        lines = [f"- {p['step']} {p.get('title','')} @ {p.get('commit','')[:9]}: {p.get('summary','')}" for p in progress]
        parts.append("## Progress so far (written by the runner from earlier sessions)\n" + "\n".join(lines))
    parts.append("## Recent commits\n```\n" + (git_log.strip() or "(none)") + "\n```")
    if feedback:
        parts.append(
            "## Previous attempt was REJECTED. Fix these first\n"
            + "\n".join(f"- {r}" for r in feedback)
            + "\n\nYou start again from the last accepted commit; the rejected attempt's diff is shown for reference only."
        )
        if prev_diff:
            parts.append("### Rejected attempt diff (truncated)\n```diff\n" + prev_diff + "\n```")
    parts.append("When done, end with the single `SUMMARY:` line described in your instructions.")
    return "\n\n".join(parts) + "\n"


def evaluator(spec: Spec, step: Step, *, diff: str, diffstat: str, accept_report: list[dict], coder_summary: str,
              nonce: str) -> str:
    acc = "\n".join(f"- exit {r['rc']}: `{r['cmd']}`" for r in accept_report) or "- (none)"
    parts = [
        f"# Review step {step.id}: {step.title}",
        "## Goal\n" + step.goal,
    ]
    if step.notes:
        parts.append("## Notes from the spec\n" + step.notes)
    parts += [
        "## Reference docs\n" + (render_refs(spec, step) or "(none)"),
        "## Acceptance commands already run by the runner (all passed)\n" + acc,
        "## E2E specs for this step\n" + (_bullets([f"acceptance/{e}" for e in step.e2e]) if step.e2e else "- (none listed; still exercise user-facing behaviour if any)"),
        "## Builder's own summary (unverified claim)\n" + (coder_summary or "(none)"),
        "## Diff stat\n```\n" + diffstat + "\n```",
        "## Diff (may be truncated; read files in /workspace for full context)\n```diff\n" + diff + "\n```",
        f'End with the single-line `VERDICT:` JSON described in your instructions, including "nonce": "{nonce}".',
    ]
    return "\n\n".join(parts) + "\n"


def truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n[... {len(s) - limit} more chars truncated]"


def dumps(o) -> str:
    return json.dumps(o, indent=2, sort_keys=True)
