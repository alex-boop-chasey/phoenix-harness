# Step 01: Baseline, architecture and API map

## Goal
Know exactly what Phoenix is today before changing it, and write that down so every later step builds on facts.

## Tasks
1. In `~/harness`: confirm `main` is clean, create branch `phoenix-0.5` from `main`.
2. Run the test suite and record the result (count, skipped tests and why they skip).
3. Confirm how the live install works: read `install.sh`, compare `~/harness` with `~/.local/share/hands/runner` (`diff -rq`, ignoring `__pycache__`), check what `~/.local/bin/hands` and `hands-dash` run. Report any difference.
4. Read the code: `hands/dashboard.py`, `hands/static/*`, `hands/config.py`, `hands/state.py`, `hands/newproject.py`, `hands/loop.py`, `hands/planner.py`, `hands/docker.py`, `hands/__main__.py`.
5. Write `docs/architecture.md`: components, processes (runner, dashboard, watchdog, containers), files and folders on disk, data flow of one build step, where secrets live, security boundaries.
6. Write `docs/api.md`: every dashboard HTTP route (method, path, auth and CSRF requirements, request body, response shape with a real example, errors), the login flow (`hands-dash url`), and the CSP and header rules.
7. Create `~/ops/status.json` (see README) and `~/ops/shots/`.

## Desired outcome
- Branch `phoenix-0.5` exists, based on current `main`.
- `docs/architecture.md` and `docs/api.md` exist, are accurate, and cover every route in `dashboard.py`.
- `~/ops/status.json` lists steps 01-16.
- No code behaviour changed.

## Automated checks
- Test suite passes, same count as before.
- A script or one-liner that lists every route found in `dashboard.py` and confirms each appears in `docs/api.md`. Show its output.

## Alex's check
Open `~/harness/docs/architecture.md` in File Browser and skim it: does it match how you understand Phoenix?
