# Phoenix Harness 0.4.3 — dashboard HTTP API

Baseline written 2026-10-17 (step 01). Server: `hands/dashboard.py` (`hands-dash`), stdlib
`ThreadingHTTPServer` bound to **127.0.0.1:8090** only. Reach it through an SSH tunnel:
`ssh -N -L 8090:127.0.0.1:8090 builder`.

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

- **Host check (all requests):** `Host` must match `^127\.0\.0\.1(:\d{1,5})?$` exactly — blocks DNS
  rebinding and keeps the cookie away from dev servers on "localhost". Otherwise `403` text `forbidden host`.
- **Auth (everything except `/login`):** valid session cookie required. Unauthenticated `GET /` →
  `401` text with instructions; any other path → `401` JSON `{"error": "not logged in"}`.
- **CSRF (all POSTs):** `Origin` must equal `http://<Host>` **and** header `X-Hands: 1` must be present,
  else `403 {"error": "bad origin"}`. POST bodies over **10 000 bytes** → `413 {"error": "too large"}`
  (`/api/p/<name>/env` allows 65 536 bytes).
- `/login` accepts GET only (`405` otherwise). HEAD is handled like GET without a body.
- **Limits:** max 32 concurrent connections (excess dropped), 10 s socket timeout, file reads capped
  (2 MB per file, logs tail-read at 1 MB), symlinks never followed, path segments are allowlisted by
  regex (`NAME`, `ATTEMPT`, `ATTEMPT_FILE`, `PLAN_PATH`). `coder.json` / `eval-N.json` (full session
  streams, possibly containing secrets) are **never served**.
- **Errors:** unknown path/name → `404 {"error": "not found"}`; invalid input → `400 {"error": "<msg>"}`
  (message capped at 500 chars); anything unexpected → `500 {"error": "<ExceptionName>"}` — never a traceback.

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
  "fallback_model": "claude-opus-5-5", "image": "hands:cc2.1.301-pw1.64.1",
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
           "allowed_domains": 0, "repo_specs": "", "repo_app": "", "description": ""},
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
`files`/`domains`/`steps` describe the reviewable PR, `plan_md` is the candidate PLAN.md (≤200 kB),
`step_list` the parsed candidate steps (≤100) and `shared` its spec-wide commands/paths; when
`planning`, `activity`/`files_written` give live progress; `log` is the last 150 plan.log lines.

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

Contents of `~/.config/hands/usage.json` plus `refreshing` (usage-refresh.lock held). Empty cache →
`{"refreshing": false}`. With data:

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

Body: `{"slug": "...", "description": "..."}` (JSON; bad JSON → `400 {"error":"bad json"}`).
Validates the slug (`^[a-z][a-z0-9-]{1,30}[a-z0-9]$`, no `--`, not reserved `all/dash/dashboard`) and
description (1–4000 chars); rejects an existing or in-flight project. Spawns
`hands <slug> create --then-plan` detached (description on stdin), log
`~/.config/hands/creating/<slug>.log`. `200 {"ok": true, "slug": "<slug>"}`; errors as `400 {"error": ...}`.

### POST /api/p/NAME/action/ACTION  (ACTION ∈ stop, start, ack, ack-clear-model)

Runs the `hands` CLI as a subprocess (60 s timeout), so the CLI's own checks apply.
`start` is pre-checked: no steps.json → `{"rc":1,"output":"no plan yet: ..."}`; already running →
`{"rc":1,"output":"runner is already running"}`; clears the STOP file first.
Response: `200 {"rc": <int>, "output": "<combined stdout+stderr, last 4000 chars>"}` (`rc` 124 = timed out).
Unknown action → 404. Body: none.

### POST /api/p/NAME/action/plan and /api/p/NAME/action/approve

Managed (hands-created) projects only. Body: `{"feedback": "...", "head_sha": "..."}` — `feedback`
(≤8000 chars) only for `plan`, `head_sha` only for `approve`. Refused (`400`) while the planner or the
build is running; `approve` additionally requires plan state `ready` and `head_sha` equal to the stored
40-hex head (stale page → `"the plan changed since this page showed it; review the new version, then
approve"`); `plan` is refused once a plan was approved. Spawns `hands <name> plan|approve [sha]`
detached, log `state/plan/plan.log`. `200 {"rc": 0, "output": "plan started"}` (or `"approve started"`).

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
`docs/console-review.md` §6, option C). A session is one `claude` process inside tmux session
`hands_s_<uuid>`; `<uuid>` is a lower-case UUID4 (`sessions.ID`, `fullmatch`) and is also Claude's own
session id (`claude --session-id <uuid>`, resumed with `claude --resume <uuid>`). Records live in
`~/.config/hands/sessions/<uuid>.json` (0600). Same auth, Host and CSRF rules as every other route.

### GET /api/sessions

Known sessions, newest activity first:
`[{"id", "title", "cwd", "created", "last_active", "live", "viewers", "transcript", "telegram"}]`. `title` is the
first user prompt of Claude's transcript `~/.claude/projects/<cwd encoded>/<uuid>.jsonl` (bounded read,
cached in the record once found), `live` = the tmux session exists, `viewers` = open terminals on it,
`telegram` = the session is flagged to run with the Telegram channel (step 12; at most one flagged
session is running at a time).

### GET /api/sessions/defaults

`{"cwd": "~/ops", "home": "...", "max_terminals": 8, "telegram_channel": "plugin:telegram@claude-plugins-official"}`
(the New session form's defaults and the one channel a live session runs).

### POST /api/sessions/new

Body `{"cwd": "/abs/or/~/path", "telegram": false}` (both optional, defaults `~/ops` and `false`; `telegram`
must be a JSON boolean, `400` otherwise). The working folder must exist, be
inside the home folder, not be the home folder itself and not be a hidden top-level folder (`~/.config`,
`~/.ssh`, …); symlinks are resolved first (`400 "working folder …"` otherwise). Starts
`claude --session-id <uuid>` in tmux with a clean allowlisted environment: `PATH HOME TERM LANG USER
SHELL` (+ `CLAUDE_CONFIG_DIR`, `TMUX_TMPDIR` if the dashboard has them), **nothing copied from the
dashboard's environment** (no GH/TG tokens, no `CLAUDECODE`, no `COLORTERM`), default permission
mode (never `--dangerously-skip-permissions`). With `"telegram": true` the argv also gets
`--channels plugin:telegram@claude-plugins-official` (the plugin reads its own bot token from
`~/.claude/channels/telegram/.env` inside the session; the dashboard never touches that folder) and the
record is flagged; `409 "another session is already live on Telegram (<title>); stop it first"` if a
flagged session is running. Response: the session's row.

### POST /api/sessions/UUID/resume · /stop · /delete · /ticket

`resume` restarts a stopped session with `--resume <uuid>` (`409` if it is running). Its optional body
`{"telegram": true|false}` sets the flag before the start (persisted in the record; omitted or `null`
keeps it), so a session moves on or off Telegram by stop + resume; a flagged start is `409` while
another flagged session runs, and nothing is started or changed. `stop` kills the
tmux session and closes its terminals; `delete` stops it, removes the record and unlinks exactly
`~/.claude/projects/<cwd encoded>/<uuid>.jsonl` (never following a symlink; nothing else under
`~/.claude` is listed or touched). `ticket` returns `{"ticket": "...", "ttl": 30}`: a single-use,
30-second token bound to this login session and this session id, the CSRF proof for the WebSocket
below (browsers cannot send `X-Hands` on an upgrade). Unknown or malformed id → `404`.

### GET /ws/sessions/UUID?t=TICKET  (WebSocket)

Every check runs before the `101`, in this order: Host (as everywhere), login cookie, `Origin` ==
`http://<Host>`, a valid RFC 6455 handshake (`Upgrade: websocket`, `Connection: Upgrade`, version 13,
16-byte key), the ticket (→ `403`), the record (→ `404`), the session running (→ `409`), a free
terminal slot (**8** open terminals in total → `503`). Then the server runs `tmux attach-session` on
a fresh PTY (posix_spawn, setsid, controlling tty) and bridges it:

- client → server: binary frames = terminal input; text frames = JSON `{"type": "resize", "cols", "rows"}`
  (clamped to 20–500 × 5–300);
- server → client: binary frames = terminal output; text `{"type": "exit"}` then close `1000` when the
  attach process ends (session stopped or Claude exited).

Protocol limits (`hands/ws.py`): client frames must be masked, no reserved bits, only opcodes
0/1/2/8/9/10, control frames ≤ 125 bytes and unfragmented, payload ≤ 64 kB per frame, ≤ 256 kB per
assembled message, text must be UTF-8 (close codes 1002 / 1009 / 1007). Liveness: a ping after 20 s
of silence, close `1001` after 60 s without any client frame; a frame that stalls half-way closes
with 1002; peer gone = 1006. The session itself keeps running when the viewer disconnects.

## File browser (since 0.5, step 13)

A bounded view of a few folders under the builder home (`hands/files.py`). Same Host, login and CSRF
rules as every other route; the download and upload routes stream in 64 kB chunks and never hold a
file in memory. Errors: `400` bad request (NUL, `..`, absolute path, over-long name, wrong types),
`403` denied (protected folder, outside the allowed roots, symlink leading there, write into `.git`,
write through a symlink), `404` unknown root or missing path (also a symlink loop), `409` conflict
(exists, not empty, overwrite not confirmed).

**Allowed roots** come from `~/.config/hands/files.json`
(`{"version": 1, "roots": ["/abs/path", ...], "git_writes": false}`, 0600, atomic write); default
`~/harness ~/projects ~/ops ~/claude-harness`. A root must be an existing folder inside the home folder,
not the home itself, not a hidden top-level folder (`~/.config`, `~/.ssh`, …), and not in a protected
place by real path; `~` is expanded and symlinks resolved when saved. A root that later disappears is
listed with `exists: false` and answers `404`; a root that is (or resolves into) a protected folder is
dropped from the listing even if the file names it.

**Always denied, whatever the settings say:** `~/.config/hands` (and `HANDS_CONFIG_HOME`), `~/.ssh`,
`~/.config/gh`, `~/.git-credentials`, `~/.claude` (and `CLAUDE_CONFIG_DIR`), and any path whose real
location (after `os.path.realpath`) is outside the allowed roots. Every request path is relative to a
root (`root` = one of the configured roots, exactly; `path` = relative, `/`-separated); `..`, NUL,
absolute paths, names over 255 bytes and paths over 4096 chars are refused before resolution
(`%2e%2e` is decoded once by the URL layer and is then a literal name). Symlinks inside the roots work
(a link to another allowed root answers with that root as `root`); a link out of bounds is listed with
`ok: false` and refused on every operation; writes never go through a symlink in the final component.
Writes inside a `.git` folder are refused unless `git_writes` is on. Delete removes a link, never what
it points to; a non-empty folder needs `"recursive": true` (JSON boolean).

### GET /api/files/roots

```json
{"roots": [{"name": "harness", "path": "/home/builder/harness", "exists": true}, ...],
 "git_writes": false,
 "limits": {"max_upload": 268435456, "max_text": 2000000, "max_list": 5000, "max_hits": 500}}
```

### POST /api/files/settings

Body (up to 64 kB): `{"roots": ["~/harness", "/home/builder/projects"], "git_writes": false}` — both keys,
nothing else; 1–50 roots, each validated as above (one bad root → `400`, nothing saved). Response: as
`GET /api/files/roots`.

### GET /api/files/list?root=ROOT&path=REL

```json
{"root": "/home/builder/harness", "path": "hands", "git": false, "git_writes": false, "truncated": false,
 "entries": [{"name": "static", "type": "dir", "size": 4096, "modified": 1792300000, "mode": "0755"},
             {"name": "files.py", "type": "file", "size": 15210, "modified": 1792300000, "mode": "0644"},
             {"name": "lnk", "type": "link", "size": 12, "modified": 1792300000, "mode": "0777",
              "ok": true, "target_type": "dir"}]}
```

`type`: `dir | file | link | other`; folders first, then by name; at most 5000 entries (`truncated`).
`root`/`path` are canonical (symlinks resolved). `git` = the folder is inside a `.git` tree. A file
instead of a folder → `400`.

### GET /api/files/read?root=ROOT&path=REL

`{"root", "path", "size", "truncated", "binary": false, "text": "..."}` — at most 2 MB of text
(`truncated: true` beyond that). A file with a NUL in its first 8 kB or invalid UTF-8 answers
`{"root", "path", "size", "truncated", "binary": true}` with no `text`. A folder → `400`.

### GET /api/files/download?root=ROOT&path=REL

The file as `application/octet-stream` with `Content-Length` and
`Content-Disposition: attachment; filename="name"` (or `filename*=UTF-8''...` for names outside
`[A-Za-z0-9 ._+()-]`), streamed; opened `O_NOFOLLOW`, regular files only (`400` otherwise).

### GET /api/files/search?root=ROOT&q=TEXT

Case-insensitive substring match on names under the root: `{"root", "q", "hits": [{"path", "type",
"size", "modified"}], "truncated"}`, sorted by path. Bounded: 500 hits, 100 000 entries visited, 5 s;
symlinks are neither followed nor reported; `.git` and `node_modules` are skipped; `q` is 1–200 chars.

### POST /api/files/mkdir

Body `{"root", "path"}`. Creates one folder (mode 0755) whose parent exists. `409` if anything with
that name exists. Response `{"ok": true, "root", "path"}`.

### POST /api/files/rename

Body `{"root", "path", "to", "to_root"?}` — moves or renames a file, folder or link to `to` (relative to
`to_root`, default `root`; another allowed root is fine). The target must not exist (`409`), a folder
cannot move into itself (`400`), the root itself cannot be moved (`400`), both ends obey the `.git` and
symlink rules; a move across file systems is refused (`400`). Response `{"ok": true, "root", "path"}` for
the new location.

### POST /api/files/delete

Body `{"root", "path", "recursive": false}`. Removes a file, a link (only the link) or an empty folder;
`409 "the folder is not empty; …"` otherwise unless `recursive` is `true` (then `shutil.rmtree`, which
unlinks symlinks instead of following them). The root itself → `400`. Response `{"ok": true}`.

### POST /api/files/upload?root=ROOT&path=REL[&overwrite=1]

Raw body = the file (any `Content-Type`); `Content-Length` required (`411` for chunked), at most
**256 MB** (`413`, checked before anything is read). All checks run before the body is read: the parent
must exist, the name may not be a symlink, a folder with that name → `409`, an existing file → `409`
unless `overwrite=1`. The body streams into a temp file next to the target (`.<name>.upload-<pid>-<rand>`,
0600, `O_EXCL`) and is renamed into place (mode 0644) on completion; a short or stalled body leaves
nothing behind (`400 "upload ended early"` when the client is still there). Response
`{"ok": true, "root", "path", "size"}`.

## CLI entry points (context)

`hands-dash run|start|stop|url` manage the server (tmux session `hands_dashboard`,
lock `~/.config/hands/dashboard.lock`). The watchdog cron keeps it running via
`dashboard.ensure_running()`.

## Route inventory (for the coverage check)

`/` `/app.js` `/sessions.js` `/app.css` `/vendor/xterm.min.js` `/vendor/addon-fit.min.js` `/vendor/xterm.min.css` `/login` ·
GET `/api/projects` `/api/usage` `/api/creating/<slug>` `/api/p/<name>` `/api/p/<name>/log`
`/api/p/<name>/attempts` `/api/p/<name>/attempts/<attempt>/<file>` `/api/p/<name>/planfile`
`/api/p/<name>/env` `/api/sessions` `/api/sessions/defaults` `/api/files/roots` `/api/files/list`
`/api/files/read` `/api/files/download` `/api/files/search` ·
POST `/api/usage/refresh` `/api/new` `/api/p/<name>/action/<action>` (stop, start, ack,
ack-clear-model, plan, approve) `/api/p/<name>/hide` `/api/p/<name>/unhide` `/api/p/<name>/env`
`/api/sessions/new` `/api/sessions/<uuid>/resume|stop|delete|ticket` `/api/files/settings`
`/api/files/mkdir` `/api/files/rename` `/api/files/delete` `/api/files/upload` ·
WebSocket `/ws/sessions/<uuid>?t=<ticket>`
