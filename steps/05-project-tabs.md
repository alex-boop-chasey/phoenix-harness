# Step 05: Project builder tabs

## Goal
Projects show as browser-style tabs across the top of the Project builder. Closing a tab hides the project from the screen without deleting anything; closed projects can be reopened.

## Tasks
1. Backend (tests first):
   - Store which projects are hidden in a small file under `~/.config/hands/` (for example `ui.json`), written atomically. Hiding never touches the project's folder, repos, state or config.
   - API routes to hide and unhide a project, with the same auth and CSRF checks as other POST routes. Unknown or invalid project names are rejected.
   - The project list API reports hidden status, so the UI can show open tabs and a "closed projects" list.
   - Hiding a project with a running build is allowed but the tab shows a warning first (UI), and the runner is not affected.
2. Front end:
   - Tabs across the top: name, stage badge, close (x). Selected tab is clear. Overflow scrolls horizontally with visible affordance.
   - Last tab "+": menu with "New project" (existing form) and "Reopen a closed project" (list of hidden projects with stage and last activity).
   - Remove the old left project list.
   - Remember the selected tab per browser (localStorage, wrapped in try/catch).
   - Keyboard: arrow keys move between tabs, Enter opens, Delete or the x closes, with confirmation.
3. Update `docs/api.md`.

## Desired outcome
- With 0, 1, 5 and 20 projects the tab bar works and looks right at 1440 and 390.
- Close then reopen restores the project with all its data.
- Hidden status survives a dashboard restart.

## Automated checks
- New unit tests for hide/unhide (valid, invalid name, auth missing, CSRF missing, persistence across restart) pass, plus the full suite.
- Rig screenshots `~/ops/shots/05/` for 0, 1, 5, 20 projects, "+" menu open, reopen list, close confirmation.
- Playwright flow: close a tab, reopen it from "+", confirm it returns.

## Alex's check
Look at `ops/shots/05/`.
