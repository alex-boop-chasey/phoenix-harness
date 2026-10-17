# hands runner 0.4.3

Outer loop for unattended builds: one spec step per fresh `claude -p` session in the `hands`
container, then runner-run acceptance and an independent evaluator, then commit + push.

## Commands
```
hands <project> check            # preflight + live probes; run after any change
hands <project> start            # loop in tmux session hands-<project>
hands <project> status
hands <project> stop             # stop at next safe point (STOP file)
hands <project> ack              # after an escalation (resets failed steps' attempts)
hands <project> prompt [STEP]    # show the prompt a step would get (no session)
hands <project> mark-done STEP   # human steps
hands <project> watchdog         # restarts the loop if it crashed (not after escalation/done/STOP/Ctrl-C)
hands all watchdog               # cron (installed every 10 min): every project's watchdog + keeps dashboard up
hands-dash url                   # single-use login link for the dashboard (5 min)
hands-dash start | stop          # dashboard in tmux session hands-dash, 127.0.0.1:8090
```

## Dashboard
From the Mac: `ssh -N -L 8090:127.0.0.1:8090 builder`, then on the VPS `hands-dash url` and open the link
(must be http://127.0.0.1:8090, not localhost). Shows status, steps, sessions, runner log, attempts
(prompt, diff, acceptance logs, verdict) and progress notes; buttons: Start, Stop, Ack, Ack + clear model.
Login codes are single use; sessions live in memory for 12 h (restart = log in again).
Session manager (0.5): persistent Claude terminals in tmux; one can be live on Telegram (setup: docs/telegram.md).

## New projects (0.4.1)
Dashboard → "+ New project" (slug + idea), or on the VPS: `echo "idea" | hands <slug> create`.
Creates private repos <slug>-specs (PR + code-owner review required) and <slug>-app (no force-push/deletion),
deploy keys in ~/.config/hands/secrets/<slug>/keys/ (specs key read-only, verified), clones pinned to those keys
via core.sshCommand (your ~/.ssh/config, agent and personal keys are never used), GitHub host keys from /meta,
~/.config/hands/secrets/<slug>/project.env (0600; edit it in the dashboard's Environment panel, which keeps
secret/public metadata in env.json next to it), ~/projects/<slug>/{state,description.txt,created.json}
and the runner config. All project secrets live under ~/.config/hands (dirs 0700), so one deny rule for
~/.config/hands keeps them away from an operator Claude session; the project folder holds none.
Defaults (org, code owner, image, models): ~/.config/hands/new-project.toml.
Remove a hands-created project (CLI only): `hands <slug> destroy --confirm <slug>`; it deletes only the repos
recorded in created.json and only if their GitHub id still matches.

### Start from an existing repo (0.5)
Tick "Start from an existing repo" in the form (owner/repo, optional branch, optional ruleset), or on the VPS:
`echo "idea" | hands <slug> create --from-repo <owner>/<repo> [--branch B] [--ruleset]`. Checks first: the repo
is reachable with the token, not archived, has a default branch, the token has admin on it (deploy keys need
it) and the branch exists. Then only <slug>-specs is created; the existing repo gets a write deploy key (and
the protect-main ruleset only with --ruleset; it may have its own rules), nothing else is written to it, and it
is cloned at the chosen branch. created.json records it under "imported" (repo, id, deploy_key_id, branch),
never under "repos": `destroy` removes that deploy key, the specs repo and the local folders/secrets, and
leaves the imported repo untouched (it never deletes, force-pushes or re-creates it). The config gets
`imported = true`, `repo_app = "owner/repo"`, `branch` = the app branch and `spec_branch = main` stays implicit
(the specs repo is always on main). The planner gets a bounded summary of the existing code (file tree,
package.json, framework and test setup, README start) and plans changes on top of it; builders are told the
codebase existed before and its conventions win; `allowed_paths` that leave existing code out produce a
warning in the plan (PR body, dashboard, `hands <p> validate`).

## Planner (0.4.2): idea -> plan -> one-click approve -> build
New projects from the dashboard are planned automatically; or press "Plan it" / run `hands <p> plan`.
The planner is one unattended `claude -p` session (Fable) in the same locked-down container, with the
pinned Superpowers skills **brainstorming** and **writing-plans** mounted read-only (planner-skills/, MIT):
brainstorming in its "skip the questions" mode (+ its builder-check subagent) writes docs/design.md;
writing-plans writes docs/plan.md; each plan task becomes one step in steps.json with acceptance tests.
WebSearch (server-side) and `npm view` are its only outside sources; WebFetch is off
(`planner_web_search = false` in the project toml turns search off too).
Output is exported through a --network none container, limited to steps.json, PLAN.md, allowed-domains,
docs/*.md, acceptance/**, validated by the runner (up to 2 automatic fix rounds), then pushed as PR
`hands/plan` to <slug>-specs via the GitHub API. The dashboard shows PLAN.md (incl. "Decisions for you
to check"), steps, checks, files, and any new network domains. **Approve & build** merges exactly the
reviewed commit (sha-pinned; refused if the branch changed), refreshes the spec and starts the runner.
**Re-plan with feedback** revises the plan and updates the same PR.

## Acceptance tests per step (0.4)
Each step sees only: shared files, files of steps already passed, and its own files. A step owns
the files its acceptance commands name as `acceptance/<path>` (globs and `dir/` allowed), its `e2e`
specs, and its optional `acceptance_files` list. Files named by `setup`/`regression`/`e2e_command`,
and files nobody names, are shared. Commands with references the runner cannot parse
(`acceptance/{a,b}`, quotes or `$VAR` after `acceptance/`, `cd acceptance`) make the spec invalid;
symlinks under acceptance/ are not allowed. `hands <p> validate` prints every step's view.
Config: `~/.config/hands/projects/<project>.toml` (see hands/config.py for all keys).
State/logs: `<state_dir>/` — runner.log, state.json, metrics.jsonl, progress.jsonl, attempts/<STEP>-<n>/.

## Per step
1. Runner pins the spec repo (origin/main), validates steps.json, syncs the app clone.
2. Fresh clone -> coder container (firewall, agent uid, no caps, skills + explorer/test-runner subagents).
   Prompt = step + inlined refs + runner-written progress notes + git log + previous rejection reasons.
3. Export via a `--network none` container (git ls-files | tar); tar validated (regular files only).
4. Allowlist: spec `allowed_paths`; always denied: .md/.txt/docs, CLAUDE.md, AGENTS.md, .claude/,
   .mcp.json, .github/, .gitattributes, .gitmodules, symlinks, acceptance/ (case-insensitive).
5. Local commit; acceptance commands, each in its own container on a fresh clone + fresh package
   store; pass = container exit code 0.
6. Evaluator on another fresh clone (no Write/Edit/Agent), verdict only from a clean session's final
   result with a per-run nonce.
7. Pass -> state, progress note, push, Telegram. Fail -> tag hands/failed/..., reset, retry with
   reasons; escalate after max attempts. Usage limits pause and resume; auth errors escalate.

## Watchdog
Restarts a project only if its status is running/paused (crashed, OOM-killed, reboot) or it stopped on
SIGTERM. After 3 restarts within an hour it escalates instead. A preflight failure (missing image,
token file, ...) escalates immediately. Log: ~/.local/state/hands/watchdog.log.

## 0.4.3
- Dashboard: steps sidebar (current step highlighted with a spinner and live activity), planner progress
  (spinner, current document and phase checklist), usage meter (5-hour session and weekly usage with reset
  times; refresh button = one tiny Haiku reply, or `hands all usage`).
- All Claude sessions use `--output-format stream-json --verbose`. Usage comes from the stream's
  `rate_limit_event` (observed, not documented: the meter shows "no data" if it disappears). The result is
  accepted only if the last line is the single valid result line (fails closed).
- Planner: `pins` (exact npm versions) required in steps.json and checked against npm publish dates
  (`min_release_age_days`, default 3; pnpm 12 refuses releases < 1 day old); Playwright configs must write
  under os.tmpdir() with list/line/dot reporters (acceptance/ is read-only). Telegram on plan ready/failed.
- Coder sessions run with CI=1 (same pnpm policies as the checks). A coder can end with
  `SPEC_PROBLEM: ...`; a failed attempt with one escalates at once. Two consecutive failures with the same
  specific error (file:line compile error, named test, specific pnpm code) escalate instead of a 3rd try.
- Runner files are created owner-only (umask 077); raw session streams are no longer served by the dashboard.

## Upgrading from 0.3.x
- Spec commands that reference acceptance files in forms the runner cannot attribute (bare `acceptance/`,
  `cd acceptance`, `{a,b}`, `$VAR`) now make the spec invalid; run `hands <p> validate` after upgrading.
- The dashboard login is `hands-dash url` (single-use code); there is no long-lived dashboard token.

## Known residual risks
- Reference firewall: allows the host /24 and DNS to any resolver (DNS tunnelling possible).
- Agent-written code runs as the same uid as the evaluator's claude process; the nonce raises the
  bar for forging a verdict but is not a hard boundary. Acceptance exit codes are the hard gate.
- Coder and evaluator containers hold the Claude token and project_env (use dev-only secrets). Since 0.5
  the runner mounts project_env read-only at `/run/hands/project.env` in coder, acceptance and evaluator
  containers (the planner sees key names only), rejects attempts that commit an env file or a secret
  value, and redacts secret values (8+ characters) as `[REDACTED:KEY]` in everything it stores, serves
  or sends. Shorter values are not redacted; the dashboard's Environment panel manages the file.
- Dashboard: any process running as builder can read state files anyway; the dashboard adds no new
  access beyond start/stop/ack. Keep the SSH tunnel closed when not in use.
