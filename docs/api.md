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
  else `403 {"error": "bad origin"}`. POST bodies over **10 000 bytes** → `413 {"error": "too large"}`.
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

## CLI entry points (context)

`hands-dash run|start|stop|url` manage the server (tmux session `hands_dashboard`,
lock `~/.config/hands/dashboard.lock`). The watchdog cron keeps it running via
`dashboard.ensure_running()`.

## Route inventory (for the coverage check)

`/` `/app.js` `/app.css` `/login` ·
GET `/api/projects` `/api/usage` `/api/creating/<slug>` `/api/p/<name>` `/api/p/<name>/log`
`/api/p/<name>/attempts` `/api/p/<name>/attempts/<attempt>/<file>` `/api/p/<name>/planfile` ·
POST `/api/usage/refresh` `/api/new` `/api/p/<name>/action/<action>` (stop, start, ack,
ack-clear-model, plan, approve)
