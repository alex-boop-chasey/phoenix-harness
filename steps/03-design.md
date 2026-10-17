# Step 03: Design system and layout mock

## Goal
Agree on how Phoenix Harness looks before building it: a design system and a clickable mock of the new layout, approved by Alex.

## Layout to design (Alex's brief)
- Left sidebar: app sections only. Project builder, Session manager, File browser now; space for MCP servers, Environment variables and more later. Collapsible on small screens.
- Project builder: browser-style tabs across the top, one per project, each with name, a stage badge (planning, plan ready, building, failed, done, stopped) and a close (x) button. Last tab is a small "+" that opens a menu: New project, Reopen a closed project. Many tabs scroll horizontally. The selected project's content fills the page.
- Session manager and File browser: no project tabs.
- Each section's controls live on its own page, never in the sidebar.

## Tasks
1. Write `docs/design.md`: design tokens (colours for light and dark, type scale, spacing, radius, shadows), components (sidebar, tabs, badges, buttons, forms, tables, log viewer, terminal frame, file list, empty states, errors, toasts), states (hover, focus, disabled, loading), and accessibility (contrast, keyboard use of tabs and sidebar, focus rings).
2. Build a static mock at `hands/static/mock/` (HTML and CSS, no inline scripts or style attributes, same CSP rules as the dashboard) showing: Project builder with 5 tabs in different stages, the "+" menu open, a project page; Session manager page (terminal placeholder); File browser page (sample listing).
3. The design must cover the Session manager and File browser as fully as the Project builder: terminal frame and its colour theme (light and dark, derived from the tokens), session list, file list and grid, breadcrumb, preview pane, upload area. These pages get no separate look from the apps they replace.
4. Serve the mock through the screenshot rig and capture it at both widths in light and dark: `~/ops/shots/03-mock/`.
5. Iterate on your own screenshots until it looks professional and consistent: no overflow, no clipped text, readable on phone.

## Decision for Alex
Present the screenshots and 2-3 named visual directions you considered (one line each), with your recommendation. Wait for Alex to approve or ask for changes. Apply his changes and re-screenshot until he approves. This approval is the step's outcome.

## Desired outcome
- `docs/design.md` and the mock exist and match each other.
- Alex has approved the design (record his approval and any notes in `docs/design.md`).

## Automated checks
- Mock pages pass the dashboard's CSP (no inline script, no style attributes): grep check plus a browser console with no CSP errors in the rig.
- Screenshots exist for every mock page at 1440 and 390, light and dark.

## Alex's check
Look at `ops/shots/03-mock/` and approve or ask for changes.
