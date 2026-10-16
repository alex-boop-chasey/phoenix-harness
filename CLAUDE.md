# hands runner (the harness) - working copy

Python 3.12 stdlib only (no pip deps). Dashboard UI: hands/static/{index.html,app.js,app.css}; server: hands/dashboard.py.
Runner loop: hands/loop.py; planner: hands/planner.py; projects: hands/newproject.py; CLI: hands/__main__.py.

## Workflow
- Make small changes; run `python3 -m unittest discover -s tests` (all must pass) and `node --check hands/static/app.js` after JS edits.
- Commit every change with git (clear message).
- Install only when the user says so AND no build is running (`hands <project> status` -> runner alive: False):
  `bash install.sh` (copies to ~/.local/share/hands/runner, restarts the dashboard: users must log in again via `hands-dash url`).

## Never weaken these
- Dashboard: CSP has no inline script and no style attributes (set styles via element.style or CSS classes);
  render all agent/log text with textContent, never innerHTML; keep Host/Origin/X-Hands/session checks.
- Containers are the security boundary: acceptance/ is read-only, path allowlist, no secrets in logs,
  the GitHub token (github.env) is read only by `create`/`destroy`/`plan`/`approve`, never by the dashboard or containers.
- Result parsing is fail-closed (hands/claude.py); evaluator verdict needs the nonce.
