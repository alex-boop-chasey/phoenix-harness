# Changelog

## 0.5.0 (release candidate, 2026-10-17)

One app, one design system: the Project builder (the 0.4.3 dashboard), a Session manager (persistent
Claude Code terminals, merged in from claude-harness) and a File browser (replacing the File Browser
container) behind the same login on 127.0.0.1:8090. Python 3 stdlib only, vanilla JS, strict CSP
(no inline script or style). Everything below was built on branch `phoenix-0.5` in steps 01-16
(`docs/architecture.md`, `docs/api.md`, `docs/design.md` are current; `docs/review-0.5.md` is the
security review).

### App shell and design (steps 03-05)
- "Workbench" design system (`docs/design.md`): tokens, light/dark themes, components shared by every
  page; works in Safari (JS fallbacks for uncertain CSS). Left sidebar with the three sections, hash
  routing, phone layout (390 px) with a drawer.
- Project builder: project tabs with close/reopen (persisted in `~/.config/hands/ui.json`,
  `/api/p/<name>/hide|unhide`), "+" menu (New project, reopen closed tabs), close confirmation dialog.

### Environment variables for build containers (steps 06-08)
- Per-project variables with secret/public visibility: `~/.config/hands/secrets/<slug>/project.env`
  (0600, `docker --env-file` format) + `env.json` metadata; `GET/POST /api/p/<name>/env` (secrets are
  write-only); editor in the New project form (variables set while create runs wait in
  `creating/<slug>.pending/`), the creating page and the project page.
- Runner: `project.env` mounted read-only at `/run/hands/project.env` and passed as `--env-file` to
  coder, acceptance and evaluator containers (the planner sees names only); attempts that commit
  `.env`/`.env.*`/`.dev.vars` (except `.env.example`/`.env.sample`) or a secret value are rejected;
  secret values (8+ chars) are redacted as `[REDACTED:KEY]` in streams, logs, diffs, reasons, progress,
  verdicts, activity and Telegram; `hands <p> check` verifies the file mode and contents.

### Session manager (steps 09-12)
- `hands/ws.py`: stdlib RFC 6455 WebSocket server (masked client frames only, 64 kB message / 256 kB
  assembled caps, fragmentation, ping/pong, close, 60 s idle timeout).
- `hands/sessions.py`: Claude Code sessions in tmux (`hands_s_<uuid>`), `env -i` allowlisted
  environment, `--session-id`/`--resume`, never `--dangerously-skip-permissions`, working folder under
  the home directory, 8 open terminals at most, records in `~/.config/hands/sessions/`.
- Routes `/api/sessions*` and `/ws/sessions/<uuid>?t=<ticket>` (single-use 30 s ticket, all Host /
  Origin / login checks before the 101). Page with list, xterm.js 5.5 (vendored, same-origin, styles via
  adopted stylesheets so the CSP is unchanged), reconnect with fresh tickets, phone views.
- Live on Telegram: one session at a time runs with the official Telegram channel plugin; approvals
  can be answered from Telegram (allowlisted chats). Setup commands for Alex: `docs/telegram.md`.
- `console/`: claude-harness imported with history (git subtree) for reference; not installed.
  `console/deploy/hands-dashboard.service`: optional systemd unit (prepared, not installed).

### File browser (steps 13-14)
- `hands/files.py`: allowed roots (`~/.config/hands/files.json`, inside the home directory, non-hidden)
  and a deny list no setting overrides (`~/.config/hands`, `~/.ssh`, `~/.config/gh`,
  `~/.git-credentials`, `~/.claude`, the hands config home, the Claude config dir) checked by realpath;
  list/read/download/upload (256 MB, temp + rename)/mkdir/rename/delete/search/image with refusals for
  `..`, NUL, absolute paths, symlinks out of a root, loops, long names, overwrites and recursive deletes
  unless confirmed, and any write under `.git`.
- Page: list/grid, sort, text and raster image preview, upload button and drag-drop, download, new
  folder, rename/move, edit text, delete, search, roots settings dialog showing the denied list.
- `docs/retire-filebrowser.md`: commands to stop and remove the old File Browser container.

### Projects from an existing repo (step 15)
- `hands <slug> create --from-repo owner/repo [--branch B] [--ruleset]` and the form option "Start
  from an existing repo": only `<slug>-specs` is created; the existing repo gets a write deploy key
  (ruleset only when asked), nothing is written to it; `created.json` records it under `imported`;
  `destroy` removes only that key and the specs repo. Config keys `imported`, `spec_branch`.
- `hands/codesummary.py`: bounded code summary for the planner (file tree, package.json, framework and
  test setup, README start) and `allowed_paths` coverage warnings (plan state, PR body, dashboard,
  `hands <p> validate`); the coder prompt says the codebase existed before and its conventions win.

### Security review (step 16, `docs/review-0.5.md`)
- Independent review of the whole diff: 11 findings (1 medium, 5 low, 5 info), all fixed with tests:
  served attempt files and runner.log redacted again at read time, interrupted attempts redacted on
  recovery, project state folders denied in the File browser, reserved keys in a hand-edited project.env
  stop the runner, hidden roots dropped on load, refused uploads close the socket, whole-clone delete/move
  guarded, planning never discards local work, vendored xterm pinned by hash, untrusted-summary note,
  encoded (URL/base64) secret forms redacted, subagent transcripts removed with a session.

### Tooling
- `tools/ui-shots.sh`: screenshot rig (one throwaway container, sample fixtures, Playwright flows for
  every page incl. env, sessions and files; fails on any console error or CSP violation; also runs
  `node --check` on every JS file). Tests sweep every temp dir they create.

### Upgrading from 0.4.3
- `bash install.sh` as before; log in again with `hands-dash url`. No config migration: new keys are
  optional. The old File Browser container is retired by hand (`docs/retire-filebrowser.md`).
- Telegram for live sessions needs the one-time setup in `docs/telegram.md` (Bun + plugin), optional.
