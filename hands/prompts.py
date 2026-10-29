"""Prompt assembly. The agent never sees the spec repo; it gets only what the runner puts here."""
from __future__ import annotations

import json

from .spec import Spec, Step, render_refs


def _step_cmds(spec: Spec, step: Step) -> list[str]:
    cmds = list(step.acceptance)
    if step.e2e:
        cmds.append(spec.e2e_command.replace("{files}", " ".join(f"acceptance/{e}" for e in step.e2e)))
    return cmds


def acceptance_plan(spec: Spec, step: Step, regression_steps: list[Step] = ()) -> list[tuple[str, str]]:
    """(command, owner) pairs in run order: setup + regression (owner ""), then every step in
    `regression_steps` (passed steps, in a change round: their tests must stay green), then the step's own.
    Duplicates are dropped, keeping the first owner."""
    cmds: list[tuple[str, str]] = [(c, "") for c in (*spec.setup, *spec.regression)]
    own = set(_step_cmds(spec, step))
    for s in regression_steps:
        cmds += [(c, step.id if c in own else s.id) for c in _step_cmds(spec, s)]
    cmds += [(c, step.id) for c in _step_cmds(spec, step)]
    return _dedupe(cmds)


def baseline_plan(spec: Spec, regression_steps: list[Step]) -> list[tuple[str, str]]:
    """The change round's baseline (0.6.0): setup + regression (owner ""), then every passed step's own commands
    (owner = that step), run once on the round's base commit before any new step is built."""
    cmds: list[tuple[str, str]] = [(c, "") for c in (*spec.setup, *spec.regression)]
    for s in regression_steps:
        cmds += [(c, s.id) for c in _step_cmds(spec, s)]
    return _dedupe(cmds)


def _dedupe(cmds: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop repeated commands, keeping the first owner."""
    seen, out = set(), []
    for c, who in cmds:
        if c not in seen:
            seen.add(c)
            out.append((c, who))
    return out


def acceptance_commands(spec: Spec, step: Step, regression_steps: list[Step] = ()) -> list[str]:
    return [c for c, _ in acceptance_plan(spec, step, regression_steps)]


IMPORTED_NOTE = (
    "## Existing codebase\n"
    "This codebase existed before this build system started working on it: people wrote it, and they keep "
    "working with it. Existing conventions win over the ones in your instructions: keep the project's layout, "
    "language level, style, package manager, test framework and scripts; extend them rather than replacing them. "
    "Change only what this step needs; do not reformat, rename or rewrite unrelated code, and do not add tooling "
    "the project does not already use unless the step says so.")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- `{i}`" for i in items) if items else "- (none)"


def env_section(env_vars: list[tuple[str, bool]] | None, *, for_builder: bool) -> str:
    """Names and visibility of the project's variables (never values). env_vars None = no env file."""
    names = "\n".join(f"- `{k}` ({'public' if pub else 'secret'})" for k, pub in (env_vars or [])) or "- (none configured)"
    text = "## Environment variables (project.env)\n" + names
    if for_builder:
        text += ("\n\nThe values are in your process environment and in `/run/hands/project.env` (read-only, "
                 "KEY=value lines). Follow the 'Environment variables' rules in your instructions: a committed, "
                 "secret-free sync script writes the framework's gitignored env file; commit `.env.example` with "
                 "names only; never commit a value or an env file.")
    else:
        text += ("\n\nValues are in the environment and in `/run/hands/project.env`. Check that no secret value is in "
                 "the client bundle or in any committed file, and that `.env.example` lists exactly these names.")
    return text


def coder(spec: Spec, step: Step, *, attempt: int, max_attempts: int, progress: list[dict],
          git_log: str, feedback: list[str], prev_diff: str, acc_files: list[str] | None = None,
          env_vars: list[tuple[str, bool]] | None = None, imported: bool = False,
          regression_steps: list[Step] = (), round_id: int = 0) -> str:
    parts = [
        f"# Step {step.id}: {step.title}",
        f"Attempt {attempt} of {max_attempts}. Spec commit {spec.commit[:12]}.",
        "## Goal\n" + step.goal,
    ]
    if step.notes:
        parts.append("## Notes from the spec\n" + step.notes)
    if imported:
        parts.append(IMPORTED_NOTE)
    plan = acceptance_plan(spec, step, regression_steps)
    regress = [c for c, who in plan if who and who != step.id]
    parts.append(
        "## Acceptance (the runner runs these, in order, in a clean clone after you finish)\n"
        + _bullets([c for c, _ in plan])
        + ((f"\n\nThis is change round C{round_id}: the commands below are the acceptance tests of steps already "
            "built (regression). They run again for this step and must stay green; you may not change the tests, "
            "so change the code until both old and new tests pass.\n" + _bullets(regress)) if regress else "")
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
    parts.append(env_section(env_vars, for_builder=True))
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
              nonce: str, env_vars: list[tuple[str, bool]] | None = None) -> str:
    acc = "\n".join(f"- exit {r['rc']}: `{r['cmd']}`" for r in accept_report) or "- (none)"
    parts = [
        f"# Review step {step.id}: {step.title}",
        "## Goal\n" + step.goal,
    ]
    if step.notes:
        parts.append("## Notes from the spec\n" + step.notes)
    parts += [
        "## Reference docs\n" + (render_refs(spec, step) or "(none)"),
        env_section(env_vars, for_builder=False),
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
