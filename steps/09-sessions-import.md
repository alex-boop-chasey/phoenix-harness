# Step 09: Session manager: import claude-harness and decide the architecture

## Goal
claude-harness's code is inside Phoenix with its history, fully understood, and Alex has chosen how it runs.

## Tasks
1. Import with history: `git subtree add --prefix=console <path-or-url of ~/claude-harness> main` on `phoenix-0.5`. Nothing else changes yet.
2. Write `docs/console-review.md`: what each file does; every security problem (at least: both Claude PTYs use `--dangerously-skip-permissions`; relay on 0.0.0.0:8681 plus `agent37 expose`; no Origin check on `/ws` and `/api`; `session_env` strips all `CLAUDE_CODE_*` variables; cookie derived from the password never expires; delete endpoint and session listing scope); every Agent37-specific path (`/home/node`, linuxbrew tmux, `/usr/local/bin/node`, `agent37` hooks, `TELEGRAM_STATE_DIR`).
3. Check whether xterm.js works under the dashboard's CSP (`style-src 'self'`, no inline styles). Test it in the screenshot rig with a static page. Record the result.
4. Prepare the decision below with a short pros and cons table.

## Decision for Alex
How the Session manager runs, given the dashboard is Python stdlib only (no WebSocket server) and sessions need WebSockets:
- A: add `aiohttp` to Phoenix and move the dashboard onto it (one process, one dependency added to a stdlib-only codebase);
- B: keep the console as a second process on 127.0.0.1 that trusts the dashboard's login (shared session check), served under the same origin through the dashboard or a tiny local proxy;
- C: another option you find better.
Recommend one, then wait. Record the decision in `docs/console-review.md`.

## Desired outcome
- `console/` exists with full claude-harness history (`git log -- console` shows it).
- Review, CSP result and Alex's decision are recorded.

## Automated checks
- Full suite passes (the import must not break anything).
- `git log --oneline -- console | wc -l` shows the imported history.

## Alex's check
Read `docs/console-review.md` and make the decision.
