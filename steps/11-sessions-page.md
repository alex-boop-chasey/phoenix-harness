# Step 11: Session manager: page in the app

## Goal
Alex can start, watch, resume and stop persistent Claude sessions from the Session manager section.

## Tasks
1. Session manager page per `docs/design.md`: list of sessions (title from first prompt, working folder, last activity, live or stopped), a terminal pane (xterm.js vendored under `hands/static/`, no CDN), and actions: New session (pick working folder), Resume, Stop, Delete (with confirmation).
2. Terminal resizes with the window, reconnects after a network blip, and shows connection state.
3. Phone layout: list and terminal as separate views.
4. Apply the CSP finding from step 09. If xterm needs a CSP change, stop and ask Alex (that is a security rule).

## Desired outcome
- From the page: start a session in `~/ops`, type to Claude, see output live, close the browser, come back and find it still running, resume an old one, stop and delete.

## Automated checks
- Playwright flow in the rig against a fake `claude` binary (a small script that echoes input), covering new, type, reconnect, resume, stop, delete. No real Claude usage.
- Screenshots `~/ops/shots/11/` at both widths, light and dark.
- Full suite and `node --check` pass; no console or CSP errors.

## Alex's check
Look at `ops/shots/11/`. Live testing with real Claude happens after install (step 16).
