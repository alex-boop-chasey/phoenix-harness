# Changelog

## 0.5.2 (step 17: owner fixes, in progress)

- File browser removed at Alex's request (`hands/files.py`, `files.js`, `/api/files/*`, its tests, rig
  flows, mock pages and `docs/retire-filebrowser.md`). Files on the VPS are browsed with the separate File
  Browser container on 127.0.0.1:8080, which stays as it is. The dashboard again touches nothing outside
  `~/.config/hands` and the project state it serves.
- Requirement 5 (errors inside the page): no `alert`, `confirm` or `prompt` anywhere; confirmations use the
  shared `<dialog>` (non-modal fallback where `showModal` is missing), failures a toast or the page's
  status line. Reopening a closed project retries once when the request got no response.
- Dashboard connections: a kept-alive connection waits 120 s for its next request (headers/bodies still
  10 s), so the browser always closes first; Safari's "Load failed" on the reopen POST came from the
  server closing idle connections after 10 s. Every request and connection error is logged to
  `~/.config/hands/dashboard.log` (path only, 0600, rotated at 2 MB).
- Requirement 4 (`+` opens on the first click): the `+` button is one persistent node reused by every
  render and the tab strip is rebuilt only when the tabs changed; the 4 s refresh used to replace the
  button under the pointer, which swallowed the click. The rig clicks `+` with a re-render between pointer
  down and up and asserts the menu opened (`tools/shots.js`, `plusDuringRefresh`).
- Requirement 6 (usage meter): `/api/usage` marks a window whose reset time has passed (`expired`, plus
  `stale` and the server clock `now`); the page dims it, strikes the percentage, says when it reset and
  that the data is stale, and refreshes once per expired window (remembered in localStorage) on its own;
  further refreshes are the ↻ button (one short Haiku reply each; the server still allows one per 2 min).
- Requirement 9 (session folder): new sessions default to the home folder, which is now a valid working
  folder (hidden top-level folders stay refused); `~/ops` is never the default.
- Telegram (`hands/notify.py`): a failed send is retried up to 3 attempts (2 s, then 5 s backoff; HTTP
  429 waits for `retry_after`, other 4xx are not retried); one WARN names the exception class and reason
  (URLError reason, HTTP status) for every attempt, with the bot token redacted. Still best-effort: a
  failed notification never raises or fails the runner. Tests: `tests/test_notify.py`.

## 0.5.1 (release candidate, 2026-10-17)

Closes the open-DNS hole in the build containers (`hands <p> check` used to warn "DNS to outside
resolvers is open: DNS tunnelling possible"). New agent image `hands:cc2.1.301-pw1.64.1-dns2`
(`image/versions.env` `IMAGE_VARIANT`); the old tag is untouched. Branch `dns-filter`.

rc2 (review fixes): sentry.io and statsig.com dropped from the built-in allowlist (Claude Code runs
with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; with suffix matching their ingest subdomains would
be an exfiltration route), so the built-in list is api.anthropic.com and registry.npmjs.org only;
dnsmasq `stop-dns-rebind` (no private-range answers from upstream); README residual risk: the firewall
allows IPs, so other sites on a shared CDN address stay reachable by naming them directly (future fix:
SNI-checking egress proxy). Image variant `dns2`; npm install through the filter verified by hand.

- Image: dnsmasq (`dnsmasq-base`) and `image/hands-dns-conf.sh`, which turns the allowlist into a
  dnsmasq config (`no-resolv`, one `server=`/`ipset=` pair per name, `local=/#/` so everything else is
  NXDOMAIN); it accepts only exact lowercase hostnames and IPv4 upstreams and fails closed.
- `init-firewall.sh`: starts dnsmasq as root on 127.0.0.1:53 (config in root-only `/run/hands-dns/`),
  upstream = the container's original resolvers, points `/etc/resolv.conf` at 127.0.0.1; resolved
  addresses join the `allowed-domains` ipset at query time. iptables: port 53 from the agent uid only
  to 127.0.0.1, upstream resolvers only for uid 0, all other port-53 egress dropped. Startup fails if
  example.com resolves or api.anthropic.com is unreachable.
- Firewall tightened: no GitHub IP ranges and no `api.github.com/meta` fetch (GitHub works only when a
  project lists it in `allowed-domains`), no host /24 allow, no outbound tcp/22. Nothing in the
  runner, prompts or container scripts needed any of them (GitHub API calls are host-side only).
- `hands <p> check`: the DNS warning is replaced by probes from inside the agent container (see
  `docs/architecture.md`): allowlisted name resolves, random name does not, direct 1.1.1.1/8.8.8.8
  queries (udp+tcp) blocked, dnsmasq not killable/reconfigurable, raw IP, tcp/22, GitHub IP,
  api.github.com and the host gateway blocked. GitHub probes are skipped when the project allowlists it.
- `install.sh` writes the variant into the default image in `new-project.toml`; planner prompt, README
  (residual risks: subdomains of listed names resolve; never allowlist wildcard-hosting apexes),
  architecture docs updated. Tests: `tests/test_v17.py` (config generation, probe evaluation).

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
- `install.sh` no longer seeds `~/.config/hands/projects/ra.toml` (the `ra` project is gone and must not
  come back); it creates no project config at all, only `new-project.toml` if missing
  (`tests/test_install.py` runs the script in a throwaway HOME and fails if any file appears under
  `projects/`). Release candidate 2 (`v0.5.0-rc2`).
- `bash install.sh` as before; log in again with `hands-dash url`. No config migration: new keys are
  optional. The old File Browser container is retired by hand (`docs/retire-filebrowser.md`).
- Telegram for live sessions needs the one-time setup in `docs/telegram.md` (Bun + plugin), optional.
