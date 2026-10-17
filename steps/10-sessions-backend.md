# Step 10: Session manager: secure backend

## Goal
The console backend runs safely on this host using the architecture Alex chose in step 09.

## Tasks (tests first)
1. Implement the chosen architecture. Bind 127.0.0.1 only. One login: Phoenix's session (no separate password, no `.cred` file, no Node relay).
2. Origin and CSRF: WebSocket upgrades and every state-changing request require the dashboard's own Origin and its existing session and X-Hands checks. Reject everything else.
3. Claude sessions start in default permission mode (no `--dangerously-skip-permissions`), with the user's normal settings (sandbox on). Keep the environment clean of secrets: do not pass GH_TOKEN, TG_* or other secret variables; do not strip Claude's own login.
4. Sessions start in a working folder Alex chooses per session, default `~/ops`; never `$HOME` itself.
5. Session list and delete only cover Claude's own session files, with strict id validation (already a UUID regex; keep and test it).
6. Remove Agent37 paths; use `shutil.which` or fixed system paths that exist on this host (`tmux`, `claude` in `~/.local/bin`).
7. Output buffer and message size limits stay bounded.
8. Produce a systemd user or system unit file in `console/deploy/` for Alex to install later (do not install it).

## Desired outcome
- Backend runs locally and serves terminals only to a logged-in dashboard session from the dashboard's origin.
- No secret reaches a Claude session environment beyond Claude's own login.

## Automated checks
- Tests: unauthenticated WS and API rejected; wrong Origin rejected; missing CSRF rejected; spawn argv never contains `--dangerously-skip-permissions`; env passed to the PTY contains no GH_TOKEN, GITHUB_TOKEN, TG_TOKEN, TG_CHAT_ID; working folder validation; id validation; bind address is 127.0.0.1.
- Full suite passes.

## Alex's check
None yet (the page comes in step 11). Read the Done summary.
