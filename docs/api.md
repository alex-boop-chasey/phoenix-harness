# Phoenix Harness 0.5.6 — dashboard HTTP API

Baseline written 2026-10-17 (step 01), updated through step 22 (0.5.6). Server: `hands/dashboard.py` (`hands-dash`), stdlib
`ThreadingHTTPServer` bound to **127.0.0.1:8090** only. Reach it through an SSH tunnel:
`ssh -N -L 8090:127.0.0.1:8090 builder`, or (0.5.3) through a Cloudflare Tunnel + Cloudflare Access on a
configured public host: `docs/public-access.md`.

## Login flow

1. On the VPS (shell as `builder`): `hands-dash url` → prints
   `http://127.0.0.1:8090/login?c=<code>`. The code is `secrets.token_urlsafe(24)`, written to
   `~/.config/hands/dashboard-login` (0600, replaces any previous code), valid **5 minutes**,
   **single use**. No long-lived secret ever leaves the VPS.
2. `GET /login?c=<code>` redeems it (constant-time compare; the file must be a regular 0600 file owned
   by the server's uid) and replies `303 → /` with
   `Set-Cookie: hands_dash=<token_urlsafe(32)>; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200`.
   Sessions live **in memory only** (dict in the `Server`), expire after **12 h**, and all die when the
   dashboard restarts (e.g. after `install.sh`).
3. Invalid/expired/reused code → 0.5 s delay, then `403` text
   `login code invalid, used or expired; run hands-dash url again`.

## Request rules (every route)

- **Host check (all requests):** `Host` must match `^127\.0\.0\.1(:\d{1,5})?$` exactly (a *local* host: blocks
  DNS rebinding and keeps the cookie away from dev servers on "localhost"), or — since 0.5.3 — be one of the
  *public* hosts in `~/.config/hands/public.json` (reached through Cloudflare Access, see below). Otherwise
  `403` text `forbidden host`.
- **Auth, local host (everything except `/login`):** valid session cookie required. Unauthenticated `GET /` →
  `401` text with instructions; any other path → `401` JSON `{"error": "not logged in"}`. The Access token is
  ignored on a local host.
- **Auth, public host (every request: pages, static, API, WebSocket upgrade):** header
  `Cf-Access-Jwt-Assertion` must hold a valid Cloudflare Access JWT (RS256, `kid` in the team's JWKS,
  signature, `aud`, `iss`, `exp`, `nbf`/`iat`, allowlisted `email`; `hands/publicaccess.py`). Missing or invalid →
  `403` text `forbidden: <short reason>`; the cookie is never consulted and `/login` is `404` there. The email
  is logged with each public request; the token never is.
- **CSRF (all POSTs):** `Origin` must equal `http://<Host>` on a local host, `https://<Host>` on a public
  host, **and** header `X-Hands: 1` must be present, else `403 {"error": "bad origin"}`. POST bodies over
  **10 000 bytes** → `413 {"error": "too large"}` (`/api/p/<name>/env` allows 65 536 bytes).
- **Bodies (since 0.5.3):** every request's body (`Content-Length`, any method, any outcome) is read in
  full before the response is written, so a kept-alive connection never carries leftover bytes into the next
  request (0.5.2 logged such requests as `{}POST … 501`). `Content-Length` over 65 536 → `413` and the
  connection closes unread; `Transfer-Encoding` → `411` and close; a non-numeric length → `400` and close.
- `/login` accepts GET only (`405` otherwise). HEAD is handled like GET without a body.
- **Limits:** max 32 concurrent connections (excess dropped), 10 s socket timeout, file reads capped
  (2 MB per file, logs tail-read at 1 MB), symlinks never followed, path segments are allowlisted by
  regex (`NAME`, `ATTEMPT`, `ATTEMPT_FILE`, `PLAN_PATH`). `coder.json` / `eval-N.json` (full session
  streams, possibly containing secrets) are **never served**. Attempt files, the attempt list's reasons and
  `runner.log` are passed through the project's secret Redactor again when served (since 0.5.0, review
  finding 1): the runner redacts files only after each session ends.
- **Refused uploads** (409/400/403 before the body is read) answer with `Connection: close` and close the
  socket; the unread body is never parsed as a next request (review finding 4).
- **Errors:** unknown path/name → `404 {"error": "not found"}`; invalid input → `400 {"error": "<msg>"}`
  (message capped at 500 chars); anything unexpected → `500 {"error": "<ExceptionName>"}` — never a traceback.

## Connections and the request log (since 0.5.2)

HTTP/1.1 with keep-alive. The first request line of a connection must arrive within 10 s, and so must every
header and body; a kept-alive connection may then wait `KEEPALIVE_IDLE` = 120 s for its next request (the
`Keep-Alive: timeout=115` header says so). Browsers drop idle connections sooner than that, so they never
send a request into a socket the server has just closed (Safari reported that as "Load failed" on the
reopen POST). Every request is logged to `~/.config/hands/dashboard.log` (0600, rotated once at 2 MB) as
`<utc> <client> <method> <path> <status> <ms>ms` with the path only (no query: login codes and tickets
never reach the log); connection errors and server exceptions are `ERR` lines.

## Security headers (every response)

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
                         img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cache-Control: no-store
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
```

No inline script, no style attributes (the JS sets widths via CSSOM); every agent-influenced string is
rendered with `textContent`. Rule from `~/harness/CLAUDE.md` ("Never weaken these"): keep this CSP and
the Host/Origin/X-Hands/session checks.

## Routes

`<name>` must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$` and be an existing project (a `.toml` in
`~/.config/hands/projects/`, `all` excluded). All examples below are real responses from the `smoke`
project, captured 2026-10-17 (long lists truncated to one element).

### Static (GET, auth required)

| Path | Serves |
|---|---|
| `/` | `static/index.html` (`text/html`) |
| `/app.js` | `static/app.js` (`text/javascript`) |
| `/app.css` | `static/app.css` (`text/css`) |
| `/sessions.js` | `static/sessions.js` (Session manager page, since step 11) |
| `/vendor/xterm.min.js`, `/vendor/addon-fit.min.js`, `/vendor/xterm.min.css` | vendored xterm.js 5.5.0 + fit addon (`static/vendor/`, no CDN) |

### GET /login?c=CODE

See "Login flow". `303` + cookie on success; `403` text on failure.

### GET /api/projects

Summary of every project, for the sidebar.

```json
[
 {
  "name": "smoke",
  "status": "done",
  "reason": "all steps passed",
  "alive": false,
  "stop": false,
  "passed": 2,
  "current": null,
  "updated_at": "2026-10-16T08:10:15Z",
  "activity": null
 }
]
```

`status`: `idle | running | planning | paused_rate_limit | escalation | done | stopped` (`planning` is
synthesised when `plan.lock` is held and `runner.lock` is not). `alive` = runner.lock held. `current`
(when a step is in flight) = `{"step","attempt","base","phase"}`; `activity` = last live line
`{"text","at"}`. A project whose config fails to load yields `{"name", "error"}` instead.

`hidden` (since 0.5, step 05): `true` when the project's tab is closed in the UI. The UI shows
projects with `hidden: false` as tabs and the others in the "+" menu's "Reopen a closed project"
list (with `status` and `updated_at`). Closed tabs are recorded in `~/.config/hands/ui.json`
(`{"hidden": ["name", ...]}`, 0600, written atomically via a temp file + rename); nothing else
reads that file, and hiding never touches the project folder, repos, state or config. A running
build or planner is unaffected by hiding. Entries that are not valid existing project names are
ignored; an unreadable file counts as "nothing hidden".

### GET /api/p/NAME

Full project detail (summary fields plus the below; example truncated to one metrics/progress element):

```json
{
 "name": "smoke", "status": "done", "reason": "all steps passed", "alive": false, "stop": false,
 "passed": 2, "current": null, "updated_at": "2026-10-16T08:10:15Z", "activity": null,
 "model_override": null,
 "total_sessions": 4,
 "total_cost_usd": 1.1696,
 "config": {
  "coder_model": "claude-fable-5-1", "eval_model": "claude-fable-5-1",
  "fallback_model": "claude-opus-5-5", "image": "hands:cc2.1.301-pw1.64.1-dns2",
  "max_sessions": 60, "cost_cap_usd": 0.0, "push": true, "branch": "main"
 },
 "metrics": [
  {"attempt": 1, "cost_usd": 0.230278, "duration_s": 25, "kind": "ok",
   "models": ["claude-fable-5-1"], "rc": 0, "role": "eval",
   "spec_commit": "8f8290e547519ebf3aef4e8999778eba49ae3b2b", "step": "S02",
   "ts": "2026-10-16T08:10:14Z", "turns": 4}
 ],
 "progress": [
  {"attempts": 1, "commit": "7f734159f7a5ff7952e9c804f0b5559b4a51cbe4",
   "risks": ["docs/ directory is absent from the workspace; review relied on the ref text embedded in the step message"],
   "step": "S02", "summary": "Added shout(name) to src/greet.js ...",
   "title": "shout function", "ts": "2026-10-16T08:10:14Z"}
 ],
 "steps": [
  {"id": "S01", "title": "greet function", "type": "code", "status": "passed",
   "attempts": 1, "commit": "48f5ff55bc4423ef51fe0107e6e189407396b095", "reasons": []},
  {"id": "S02", "title": "shout function", "type": "code", "status": "passed",
   "attempts": 1, "commit": "7f734159f7a5ff7952e9c804f0b5559b4a51cbe4", "reasons": []}
 ],
 "spec_error": null,
 "spec_commit": "8f8290e547519ebf3aef4e8999778eba49ae3b2b",
 "managed": false,
 "setup": {"spec_ready": true, "project_env": "", "project_env_keys": 0,
           "allowed_domains": 0, "repo_specs": "", "repo_app": "", "imported": false, "description": ""},
 "create": null,
 "plan": {"state": "none", "error": null, "pr": null, "pr_url": null, "head_sha": null,
          "summary": null, "files": null, "domains": null, "steps": null, "activity": null,
          "files_written": null, "started": null, "running": false, "log": [],
          "plan_md": "", "step_list": []}
}
```

Notes: `metrics`/`progress` are the last 30 jsonl records; step `reasons` capped at 12. If the spec is
mid-refresh or invalid, `spec_error` carries `"Type: message"` and `steps` fall back to state-only rows.
`create` (non-null while a dashboard-started create exists) = `{"state","error","lines"}` (last 200 log
lines; a dead create process is shown as `failed` after 20 s). `plan.state`:
`none | planning | ready | approved | failed`; when `ready`, `head_sha`/`pr`/`pr_url`/`summary`/
`files`/`domains`/`steps` describe the reviewable PR (`warnings`, 0.5: strings such as "allowed_paths does
not cover existing code in: lib/" for imported repos), `plan_md` is the candidate PLAN.md (≤200 kB),
`step_list` the parsed candidate steps (≤100) and `shared` its spec-wide commands/paths; when
`planning`, `activity`/`files_written` give live progress; `log` is the last 150 plan.log lines.
0.5.6 (amend mode): `mode` (`fresh` | `amend`), `round` (amendment number, new step ids start with
`C<round>-`), `app_sha` (app repo commit the planner saw read-only at `/app`), `request` (the change
request), `branch` (`hands/plan` or `hands/amend-C<round>`), `new_steps` (ids added), `tests_changed`
(0.5.7: `[{"path": "acceptance/<file>", "change": "change"|"remove", "why"}]`, the steps.json
`tests_changed` list the planner checked against the real diff; older string entries are normalised to
`change`), `shared_changed` (`[{"key","old","new"}]` for
setup/regression/e2e_command/allowed_paths/allow_text_paths/always_refs); each `step_list` entry carries
`new: true|false`. The lists are `[]` when absent.

0.5.7 (change rounds): the project object has `round` (the runner's round record from state.json,
`{id, branch, base_sha, pre_tag, spec_commit, opened_at, status: "open"|"merged"}`, or `null` for a fresh
project) and every `steps[]` entry has `round` (0 = fresh plan). Per passed step state.json also holds
`spec_hash`, `spec_files` and `branch` (not served).

### GET /api/p/NAME/log?lines=N

Last N lines (default 300, clamped 1–2000) of `runner.log`, tail-read from at most the last 1 MB.
Missing log → `{"lines": []}`.

```json
{"lines": ["2026-10-16T08:10:14Z evaluator S02#1: pass=True reasons=0",
           "2026-10-16T08:10:14Z PASSED S02 @ 7f734159f (pushed)",
           "2026-10-16T08:10:15Z DONE: all steps passed"]}
```

### GET /api/p/NAME/attempts

Attempt directories (newest first, max 150), with servable files only:

```json
[
 {
  "name": "S02-1",
  "mtime": 1792214051,
  "files": [
   {"name": "accept-0.log", "size": 328}, {"name": "accept-1.log", "size": 807},
   {"name": "changes.txt", "size": 51}, {"name": "coder.stderr", "size": 0},
   {"name": "diff.patch", "size": 1757}, {"name": "eval-1.stderr", "size": 0},
   {"name": "eval-prompt.md", "size": 3311}, {"name": "export.stderr", "size": 0},
   {"name": "prompt.md", "size": 1551}, {"name": "verdict.json", "size": 1051}
  ],
  "reason": "",
  "verdict_pass": true
 }
]
```

`reason` = first line of `reasons.txt` (≤300 chars); `verdict_pass` = verdict.json's `pass` or null.

### GET /api/p/NAME/attempts/ATTEMPT/FILE

Plain text (≤2 MB, then `[... truncated at 2000000 bytes]`). `ATTEMPT` must match
`^[A-Za-z][A-Za-z0-9._-]{0,40}-\d{1,4}(\.\d{1,12})?$`; `FILE` must be one of
`prompt.md eval-prompt.md reasons.txt verdict.json changes.txt diff.patch acceptance-files.txt
accept-<n>.log coder.stderr export.stderr eval-<n>.stderr spec-problem.txt`. Anything else → 404.

### GET /api/p/NAME/planfile?path=REL

A file from the plan candidate dir, as text. `REL` must match
`^(steps\.json|PLAN\.md|allowed-domains|docs/[A-Za-z0-9._/-]+|acceptance/[A-Za-z0-9._/@+-]+)$`,
no `..`, no symlink in any path segment.

### GET /api/creating/SLUG

Progress of a project create. Slug validated by `newproject.validate_slug`; 404 until a create was
started. Response: `{"state": "running|done|failed", "error": "", "lines": [...last 200 log lines...],
"project_exists": bool}`.

### GET /api/usage

Contents of `~/.config/hands/usage.json` plus `refreshing` (usage-refresh.lock held), `now` (server
clock), `stale` (no data, or a window's reset time has passed) and per window `expired` (its `resets_at`
≤ `now`: the percentage describes a window that is over and the page shows it as stale, never as current;
since 0.5.2). Empty cache → `{"refreshing": false, "now": ..., "stale": true}`. With data:

```json
{"status": "allowed", "at": 1792214051,
 "five_hour": {"used_pct": 19.0, "resets_at": 1792174800},
 "seven_day": {"used_pct": 16.0, "resets_at": 1792530000},
 "refreshing": false}
```

### POST /api/usage/refresh

Spawns `hands all usage` (tiny Haiku probe) detached; log `~/.config/hands/usage-refresh.log`.
`200 {"ok": true}`; more than once per 2 minutes → `400 {"error": "refreshed less than 2 minutes ago"}`.
Body: none.

### POST /api/new

Body: `{"slug": "...", "description": "...", "from_repo": "owner/repo", "branch": "...", "ruleset": true}`
(JSON; bad JSON → `400 {"error":"bad json"}`; `from_repo`, `branch` and `ruleset` are optional, 0.5).
Validates the slug (`^[a-z][a-z0-9-]{1,30}[a-z0-9]$`, no `--`, not reserved `all/dash/dashboard`) and
description (1–4000 chars); `from_repo` must be `owner/repo` (`newproject.parse_repo`), `branch` a git-like
branch name and only with `from_repo`, `ruleset` exactly `true`; rejects an existing or in-flight project.
Spawns `hands <slug> create --then-plan [--from-repo O/R [--branch B] [--ruleset]]` detached (description on
stdin), log `~/.config/hands/creating/<slug>.log`. The existence/archived/access/branch checks on the
existing repo happen in that process (see its log). `200 {"ok": true, "slug": "<slug>"}`; errors as
`400 {"error": ...}`.

### POST /api/p/NAME/action/ACTION  (ACTION ∈ stop, start, ack, ack-clear-model)

Runs the `hands` CLI as a subprocess (60 s timeout), so the CLI's own checks apply.
`start` is pre-checked: no steps.json → `{"rc":1,"output":"no plan yet: ..."}`; already running →
`{"rc":1,"output":"runner is already running"}`; clears the STOP file first.
Response: `200 {"rc": <int>, "output": "<combined stdout+stderr, last 4000 chars>"}` (`rc` 124 = timed out).
Unknown action → 404. Body: none.

### POST /api/p/NAME/action/plan, /api/p/NAME/action/amend and /api/p/NAME/action/approve

Managed (hands-created) projects only. Body: `{"feedback": "...", "head_sha": "..."}` — `feedback`
(≤8000 chars) only for `plan`, `head_sha` only for `approve`. Refused (`400`) while the planner or the
build is running; `approve` additionally requires plan state `ready` and `head_sha` equal to the stored
40-hex head (stale page → `"the plan changed since this page showed it; review the new version, then
approve"`); `plan` is refused once a plan was approved. Spawns `hands <name> plan|approve [sha]`
detached, log `state/plan/plan.log`. `200 {"rc": 0, "output": "plan started"}` (or `"approve started"`).
`amend` (0.5.6) takes `{"feedback": "<change request>"}` (required, ≤8000 chars) and spawns
`hands <name> plan --amend` with it on stdin; refused when the spec branch has no `steps.json` yet or a fresh
plan is waiting for review. `approve` of an amendment whose `shared_changed` is non-empty additionally
requires `{"shared_ack": true}` (the review tick; 400 `"this amendment changes shared settings; …"` otherwise), one
whose `tests_changed` is non-empty requires `{"tests_ack": true}` (0.5.7; 400 `"this amendment changes or removes
existing tests; …"` otherwise, passed to the CLI as `--ack-tests`), and
`approve` of a plan whose `domains` is non-empty requires `{"domains_ack": true}` (400 `"this plan adds network
domains; …"`). The ticks are passed to the CLI as `--ack-shared` / `--ack-domains`, which enforces them too.

### POST /api/p/NAME/hide and /api/p/NAME/unhide  (since 0.5, step 05)

Close or reopen the project's tab (see `hidden` above). Same auth and CSRF rules as every POST;
`NAME` must match the name regex and be an existing project, else `404`. Idempotent. Body: none.

```json
{"ok": true, "name": "smoke", "hidden": true}
```

### GET /api/p/NAME/env  (since 0.5, step 06)

Environment variables for the project's build containers (module `hands/projectenv.py`). Values live
in `~/.config/hands/secrets/<slug>/project.env` (0600, dir 0700; the path `project_env` in the
project's toml, or that default when unset), metadata in `env.json` next to it. Nothing is ever
written inside the project folder or a repo; a `project_env` that points inside a repo is refused.

```json
{"vars": [{"key": "STRIPE_KEY", "secret": true, "updated": "2026-10-17T12:51:11Z"},
          {"key": "SITE_URL", "secret": false, "value": "https://x.test", "updated": "2026-10-17T12:51:11Z"}],
 "problems": [], "file": "/home/builder/.config/hands/secrets/shop/project.env", "active": true, "pending": false}
```

- **Secret values are write-only.** A secret entry has no `value` field (and no length); only public
  values are returned. Nothing else in the API (project detail, logs, attempts) reads the file.
- `updated` is null for lines written by hand before the dashboard managed the file; such lines are
  secret by default. `problems` lists lines the dashboard cannot manage (no `=`, lowercase key,
  reserved key) by line number only, never with values. Comments are dropped on the next save.
- `active`: whether the runner passes this file to containers (`project_env` set in the toml).
- `pending: true` with `NAME` = a slug whose `create` is running (status `running`): variables go to
  `~/.config/hands/creating/<slug>.pending/` and are moved into `secrets/<slug>/` when create succeeds
  (logged as `[ok] N environment variable(s) saved`), or deleted when it fails. Otherwise an unknown
  name → `404`.

File format = docker's `--env-file` format: `KEY=value`, the value is everything after the first `=`,
verbatim. Docker applies no unquoting, so the dashboard writes none: every allowed value (one line,
no NUL, ≤ 8192 UTF-8 bytes) round-trips as is, and a hand-written `KEY="x"` keeps its quotes exactly
as the container sees them. Keys: `^[A-Z_][A-Z0-9_]*$`, ≤ 128 chars; reserved (400): `CLAUDE_CODE_OAUTH_TOKEN
GH_TOKEN GITHUB_TOKEN TG_TOKEN TG_CHAT_ID PATH HOME USER SHELL CI NODE_OPTIONS` and any `HANDS_*` / `CLAUDE_*`.

### POST /api/p/NAME/env

Body (JSON, up to **64 kB** on this route only): `{"set": [{"key": "K", "value": "v", "public": false}, ...],
"delete": ["K2", ...]}` — at least one of `set`/`delete`. In `set`, `public` defaults to false; `value`
may be omitted for an existing key to change only its visibility, except that turning a stored secret
public requires the value again (`400 "K: enter the value again to make it public"`), so a secret is
never revealed by a visibility change. The whole request is validated
before anything is written (one bad item → `400`, nothing changes); writes are atomic (temp file +
rename, 0600). Deleting an unknown key → `400`. Response: the same object as GET. Same auth and CSRF
rules as every POST.

## Session manager (since 0.5, step 10)

Persistent Claude Code terminals (`hands/sessions.py`, `hands/ws.py`; design in
`docs/console-review.md` §6, option C; PTYs owned by the dashboard since 0.5.2, `docs/sessions-pty.md`). A session is one `claude` process on a PTY
`hands_s_<uuid>`; `<uuid>` is a lower-case UUID4 (`sessions.ID`, `fullmatch`) and is also Claude's own
session id (`claude --session-id <uuid>`, resumed with `claude --resume <uuid>`). Records live in
`~/.config/hands/sessions/<uuid>.json` (0600). Same auth, Host and CSRF rules as every other route.

### GET /api/sessions

Known sessions, newest activity first:
`[{"id", "title", "cwd", "created", "last_active", "live", "viewers", "transcript", "telegram"}]`. `title` is the
first user prompt of Claude's transcript `~/.claude/projects/<cwd encoded>/<uuid>.jsonl` (bounded read,
cached in the record once found), `live` = the process is running in this dashboard, `viewers` = open terminals on it,
`telegram` = the session is flagged to run with the Telegram channel (step 12; at most one flagged
session is running at a time).

### GET /api/sessions/defaults

`{"cwd": "<home>", "home": "...", "max_terminals": 8, "telegram_channel": "plugin:telegram@claude-plugins-official"}`
(the New session form's defaults and the one channel a live session runs).

### POST /api/sessions/new

Body `{"cwd": "/abs/or/~/path", "telegram": false}` (both optional, defaults: the home folder (requirement 9,
never `~/ops`) and `false`; `telegram` must be a JSON boolean, `400` otherwise). The working folder must
exist and be the home folder or a folder inside it, and not a hidden top-level folder (`~/.config`,
`~/.ssh`, …); symlinks are resolved first (`400 "working folder …"` otherwise). Starts
`claude --session-id <uuid>` on a dashboard-owned PTY (via `/bin/sh -c 'cd -- "$1" && shift && exec "$@"'`)
with a clean allowlisted environment: `PATH HOME TERM LANG USER SHELL` (+ `CLAUDE_CONFIG_DIR` if the
dashboard has it), **nothing copied from the
dashboard's environment** (no GH/TG tokens, no `CLAUDECODE`, no `COLORTERM`), default permission
mode (never `--dangerously-skip-permissions`). With `"telegram": true` the argv also gets
`--settings '{"enabledPlugins":{"telegram@claude-plugins-official":true}}' --channels
plugin:telegram@claude-plugins-official` (the plugin is disabled in the user settings and enabled for this
one process; it reads its own bot token from
`~/.claude/channels/telegram/.env` inside the session; the dashboard never touches that folder) and the
record is flagged; `409 "another session is already live on Telegram (<title>); stop it first"` if a
flagged session is running. Response: the session's row.

### POST /api/sessions/UUID/resume · /stop · /delete · /ticket

`resume` restarts a stopped session with `--resume <uuid>` (`409` if it is running). Its optional body
`{"telegram": true|false}` sets the flag before the start (persisted in the record; omitted or `null`
keeps it), so a session moves on or off Telegram by stop + resume; a flagged start is `409` while
another flagged session runs, and nothing is started or changed. `stop` ends the
process (SIGHUP, then SIGTERM/SIGKILL) and closes its terminals; `delete` stops it, removes the record and unlinks exactly
`~/.claude/projects/<cwd encoded>/<uuid>.jsonl` (never following a symlink; nothing else under
`~/.claude` is listed or touched). `ticket` returns `{"ticket": "...", "ttl": 30}`: a single-use,
30-second token bound to this login session and this session id, the CSRF proof for the WebSocket
below (browsers cannot send `X-Hands` on an upgrade). Unknown or malformed id → `404`.

### GET /ws/sessions/UUID?t=TICKET  (WebSocket)

Every check runs before the `101`, in this order: Host (as everywhere), login (cookie on a local host, Access
token on a public one), `Origin` == `http://<Host>` (`https://` on a public host), a valid RFC 6455 handshake (`Upgrade: websocket`, `Connection: Upgrade`, version 13,
16-byte key), the ticket (→ `403`), the record (→ `404`), the session running (→ `409`), a free
terminal slot (**8** open terminals in total → `503`). Then the server attaches the socket to the
session's PTY: the scrollback (up to 256 kB) is sent first, then live output; the first resize forces a
repaint:

- client → server: binary frames = terminal input; text frames = JSON `{"type": "resize", "cols", "rows"}`
  (clamped to 20–500 × 5–300);
- server → client: binary frames = terminal output; text `{"type": "exit"}` then close `1000` when the
  attach process ends (session stopped or Claude exited).

Protocol limits (`hands/ws.py`): client frames must be masked, no reserved bits, only opcodes
0/1/2/8/9/10, control frames ≤ 125 bytes and unfragmented, payload ≤ 64 kB per frame, ≤ 256 kB per
assembled message, text must be UTF-8 (close codes 1002 / 1009 / 1007). Liveness: a ping after 20 s
of silence, close `1001` after 60 s without any client frame; a frame that stalls half-way closes
with 1002; peer gone = 1006. The session itself keeps running when the viewer disconnects.

## Public hosts (since 0.5.3)

Config `~/.config/hands/public.json` (0600): `{"hosts": [...], "team_domain": "<team>.cloudflareaccess.com",
"aud": "<AUD tag>", "emails": [...]}`; written by `hands-dash public set`, removed by `hands-dash public off`,
re-read by the running server whenever the file changes (no restart). Missing/empty/invalid = OFF.
Terminal tickets are bound to the principal that minted them (`cf:<email>` on a public host, the cookie
session locally), so a ticket never crosses hosts. Setup and verification details: `docs/public-access.md`.

## File browser (removed in 0.5.2)

The `/api/files/*` routes and `hands/files.py` from steps 13-14 are gone; see git tag `v0.5.0-rc1` for
their contract.

## CLI entry points (context)

`hands-dash run|start|stop|url` manage the server; `hands-dash public set|show|test|off` the public-access config (tmux session `hands_dashboard`,
lock `~/.config/hands/dashboard.lock`). The watchdog cron keeps it running via
`dashboard.ensure_running()`.

## Route inventory (for the coverage check)

`/` `/app.js` `/sessions.js` `/app.css` `/vendor/xterm.min.js` `/vendor/addon-fit.min.js` `/vendor/xterm.min.css` `/login` ·
GET `/api/projects` `/api/usage` `/api/creating/<slug>` `/api/p/<name>` `/api/p/<name>/log`
`/api/p/<name>/attempts` `/api/p/<name>/attempts/<attempt>/<file>` `/api/p/<name>/planfile`
`/api/p/<name>/env` `/api/sessions` `/api/sessions/defaults` ·
POST `/api/usage/refresh` `/api/new` `/api/p/<name>/action/<action>` (stop, start, ack,
ack-clear-model, plan, approve) `/api/p/<name>/hide` `/api/p/<name>/unhide` `/api/p/<name>/env`
`/api/sessions/new` `/api/sessions/<uuid>/resume|stop|delete|ticket` ·
WebSocket `/ws/sessions/<uuid>?t=<ticket>`
