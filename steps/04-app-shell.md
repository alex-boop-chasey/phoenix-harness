# Step 04: App shell with left sidebar

## Goal
The real dashboard gets the approved layout's frame: left sidebar with sections, and the existing project features moved into the Project builder section, still working exactly as before.

## Tasks
1. Apply the design tokens from `docs/design.md` to `hands/static/app.css`.
2. Add the sidebar (Project builder, Session manager, File browser) with hash-based navigation (for example `#/builder`, `#/sessions`, `#/files`). Session manager and File browser show a designed "coming soon" placeholder for now.
3. Move everything the dashboard does today into the Project builder page. The old left project list stays for now (tabs come in step 05).
4. Keep every rule from `~/harness/CLAUDE.md`: CSP unchanged, textContent for agent and log text, Host/Origin/X-Hands/session checks untouched.
5. Light and dark themes, keyboard navigation, visible focus.

## Desired outcome
- Every existing dashboard feature works from the Project builder page: project list, new project, plan review and approve, start, stop, ack, steps, sessions, log, attempt files, usage meter.
- Sidebar navigation works on desktop and phone (collapsible).
- No backend behaviour changed.

## Automated checks
- Test suite passes; `node --check hands/static/app.js`.
- Screenshot rig: every page at both widths, light and dark, in `~/ops/shots/04/`. Compare with `02-before` and confirm no feature is missing.
- Playwright script in the rig that clicks each sidebar item and each existing control's view, and fails on any console error or CSP violation.

## Alex's check
Install is not part of this step. Look at `ops/shots/04/`, and compare with `02-before`.
