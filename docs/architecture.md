# Phoenix Harness 0.4.3 — architecture

Baseline written 2026-10-17 (step 01 of the 0.5 work), from the code on branch `phoenix-0.5`
(= `main` at the time). Everything here was read from the source in `~/harness`; the live install
in `~/.local/share/hands/runner` is an identical copy (see "Install and live copy").

Phoenix ("hands") is an autonomous build harness: a human writes an idea, a planner turns it into a
reviewed spec, and a runner loop has Claude Code build the app one spec step at a time inside locked-down
Docker containers, with runner-run acceptance checks and an independent AI evaluator gating every commit.

## Components

| Component | Code | Runs as |
|---|---|---|
| CLI | `hands/__main__.py` | `~/.local/bin/hands` (user `builder`) |
| Runner loop | `hands/loop.py` | tmux session `hands-<slug>`, one per project |
| Dashboard | `hands/dashboard.py` + `hands/static/{index.html,app.js,app.css}` | tmux session `hands_dashboard`, 127.0.0.1:8090 |
| Planner | `hands/planner.py` | child process spawned by CLI/dashboard |
| Project create/destroy | `hands/newproject.py` (+ `hands/codesummary.py` for imported repos) | child process spawned by CLI/dashboard |
| Watchdog | `hands <p> watchdog` / `hands all watchdog` | cron, every 10 min |
| Agent containers | `hands/docker.py`, `container/entry.sh`, image files in `image/` | Docker, image `hands:cc2.1.301-pw1.64.1` |

Supporting modules: `config.py` (per-project TOML), `state.py` (state.json, jsonl logs, flock-based `Lock`),
`spec.py` (steps.json validation, refs, path globs), `prompts.py` (prompt assembly), `claude.py`
(fail-closed result parsing + failure classification), `gitops.py` (host git, hardened env),
`workspace.py` (workspace export/apply + path allowlist), `accview.py` (per-step acceptance view),
`activity.py` (live "what is the session doing" tail), `usage.py` (plan usage from rate_limit_event),
`notify.py` (Telegram), `docker.py` (container/volume lifecycle).

Python 3.12 stdlib only, no pip dependencies. Dashboard UI is vanilla JS with a strict CSP.

## Processes and how they start/stop

- **Runner** (`hands <p> run`): runs in the foreground; `hands <p> start` wraps it in tmux session
  `hands-<slug>`. Exactly one per project, enforced by flock on `state_dir/runner.lock`.
  Stops via the `STOP` file (`hands <p> stop`, honoured at the next safe point, checked every ≤30 s
  during waits), session/cost caps, escalation, or completion.
- **Dashboard** (`hands-dash run|start|stop|url`): `start` launches tmux session `hands_dashboard`
  running `hands-dash run --port 8090`; single instance enforced by `~/.config/hands/dashboard.lock`.
- **Watchdog** (cron `*/10 * * * * ~/.local/bin/hands all watchdog`): for every project, restarts a
  runner that died while it should be running (status `running`/`paused_rate_limit`, or `stopped` by
  SIGTERM — never after Ctrl-C/tmux kill, escalation, done or STOP). At most 3 restarts per hour, then
  escalates (`watchdog.json` keeps the restart history). Also calls `dashboard.ensure_running()`.
- **Planner / create**: the dashboard spawns them as detached `hands` subprocesses
  (`start_new_session=True`) logging to files; single instance per project via `plan.lock` /
  `creating/<slug>.lock`. State files (`plan.json`, `creating/<slug>.json`) carry a `running` flag the
  dashboard cross-checks against the lock to detect a dead process (>20 s stale → shown as failed).
- **Agent containers**: created per session with `docker run --rm` and unique names
  `hands-<project>-<role>-<tag>-<ts>-<hex>`, labelled `hands.project=<p>` / `hands.role=<role>`;
  killed and force-removed on timeout; `cleanup_orphans`/`cleanup_volumes` sweep leftovers at runner start.

## Files and folders on disk

```
~/harness                            SOURCE repo (alex-boop-chasey/phoenix-harness)
~/.local/share/hands/runner          live install (install.sh copies hands/ prompts/ planner-skills/
                                     container/ tests/ examples/ image/ README.md VERSION)
~/.local/bin/hands, hands-dash       launchers: PYTHONPATH=<install dir> python3 -m hands[.dashboard]
~/.local/state/hands/watchdog.log    cron output

~/.config/hands/                     config + SECRETS (0700; one deny rule covers everything)
  projects/<slug>.toml               per-project config (config.py `Config`)
  new-project.toml                   defaults for `create` (org, codeowner, image, models)
  claude.env                         Claude token (0600) — only mounted into agent containers via --env-file
  github.env                         GH_TOKEN (0600) — read ONLY by create/destroy/plan/approve
  telegram.env                       TG_TOKEN, TG_CHAT_ID — read by the host-side Notifier
  secrets/<slug>/keys/deploy_*       per-project deploy keys (never in ~/.ssh)
  secrets/<slug>/project.env         dev/test variables for build containers (coder, acceptance, eval)
  secrets/<slug>/env.json            dashboard metadata for them: secret/public + updated, never values
  creating/<slug>.pending/           the same two files for a project whose create is still running
  usage.json                         plan usage cache (dashboard header)
  dashboard-login                    single-use login code (0600, 5 min TTL)
  dashboard.lock, usage-refresh.lock|.log, creating/<slug>.{json,lock,log}

~/projects/<slug>/                   one folder per project
  specs/                             clone of <slug>-specs (steps.json, docs/, acceptance/, allowed-domains)
  app/                               clone of <slug>-app — the runner's own clone; agents never touch it
  state/                             runner-owned state (never mounted into any container):
    state.json                       status, steps {status, attempts, commit, reasons}, totals, current
    runner.lock, runner.log, STOP, watchdog.json, activity.json
    metrics.jsonl, progress.jsonl, attempts.jsonl
    attempts/<STEP>-<n>/             prompt.md, coder.json (full stream, NOT served), coder.stderr,
                                     changes.txt, diff.patch, accept-N.log, eval-prompt.md, eval-N.json,
                                     verdict.json, reasons.txt, acceptance-files.txt, export.stderr
    work/, verify/, acc-view/, probe/, export/   transient per-attempt dirs
    plan/                            plan.json, plan.lock, plan.log, plan-activity.json, candidate/,
                                     previous/, reviewed/, prompt-N.md, session-N.json
  description.txt, created.json, known_hosts
```

## Data flow of one build step (`Runner.attempt`, loop.py)

1. **Sync**: fetch the spec repo, pin working tree to `origin/<branch>` (`gitops.refresh_spec`),
   validate `steps.json` (`spec.load`); sync the app clone with origin (push accepted commits,
   escalate on divergence — the runner never force-pushes). Pick the first non-passed step.
2. **Acceptance view** (`accview.build`): copy into `state/acc-view/` only the acceptance files this
   step may see (shared + passed steps' + its own); mounted read-only at `/workspace/acceptance`.
3. **Coder session**: fresh clone of the app repo at HEAD into `state/work/` (origin removed);
   prompt assembled by `prompts.coder` (spec refs, progress notes, feedback, previous diff) — the agent
   never sees the spec repo itself. Container runs `claude -p --output-format stream-json` with
   `--dangerously-skip-permissions` *inside the container boundary*, coder skills mounted, WebFetch
   disallowed; claude.env + project.env via `--env-file`, and project.env also bind-mounted read-only at
   `/run/hands/project.env` (the prompt lists its key names and visibility, from `projectenv.Store`, never
   values). `activity.Tail` follows the stream for the
   dashboard's live line.
4. **Export + allowlist**: a `--network none` container re-reads the workspace (mounted read-only) and
   tars `git ls-files -c -o` output to the host (`EXPORT_SCRIPT`); the agent's workspace is then deleted
   and never read again. `validate_tar` rejects symlinks/hardlinks/devices, absolute/`..`/`.git` paths,
   oversized files (>10 MB) or trees (>500 MB, >20000 files). `apply_tree` replaces the tracked
   contents of the runner's app clone and stages everything. `check_allowlist` rejects changes outside
   `allowed_paths`, plus always-denied paths: `.git/.github/.claude/...` segments, agent-instruction
   basenames (CLAUDE.md, .mcp.json, …), human-owned top dirs (docs/, acceptance/, specs/…), and
   documentation extensions unless in `allow_text_paths`. Any violation rejects the whole attempt.
5. **Commit**: as `hands-runner <runner@hands.local>` with the coder's SUMMARY, `Spec-Commit`,
   `Hands-Attempt` and `Hands-Model` trailers; diff recorded with attribute/textconv-proof `gitops.diff`.
6. **Acceptance (runner-run)**: fresh `--no-hardlinks` clone of the committed tree into `state/verify/`,
   fresh pnpm-store volume. Each command from `setup + regression + step.acceptance (+ e2e_command)`
   runs in its own container; pass/fail is the container's exit code, output streams to a host file so
   nothing inside can forge it. First failure rejects the attempt.
7. **Independent evaluator**: another fresh clone + volume; read-only session (Write/Edit/Agent/WebFetch
   disallowed) gets the diff, acceptance report and coder summary plus a random nonce, and must end with
   `VERDICT: {"pass": bool, "reasons": [...], "nonce": ...}`. Verdict parsing is fail-closed
   (`claude.parse_verdict` on a clean session's own result only); wrong/missing nonce = no verdict;
   3 failures → tag `hands/unreviewed/...`, reset, escalate.
8. **Accept or reject**: pass → state updated first, then push (an unpushed accepted commit is pushed by
   the next round's sync); progress note + Telegram. Fail → tag `hands/failed/<step>-<n>`, hard reset to
   base, reasons recorded, attempt counted; escalates on max attempts, a coder-reported SPEC_PROBLEM, or
   the same specific failure signature twice in a row.

Infra errors (rate limit / auth / model access / overloaded) don't count as attempts: the runner waits
for limit resets (parsing "resets …" times in `cfg.tz`), switches to the fallback model on model-access
errors, escalates on auth failure or >10 infra errors per step. Crash recovery (`recover`): an
interrupted attempt's local commit is tagged `hands/interrupted/...` and reset before anything could push it.

## Container security boundary

Everything an agent wrote or could have touched is untrusted; the runner trusts only container exit
codes, files it writes itself, and git objects in its own clones (`loop.py` trust model, `gitops.py`
host-git hardening: env stripped of GIT_SSH/GIT_CONFIG overrides, global/system config disabled, and
git is never run inside an agent-written workspace).

- `container/entry.sh` (mounted read-only): applies the egress firewall as root (allowlist =
  api.anthropic.com etc. + the spec repo's `allowed-domains`; skipped only with `--network none`), gives
  the home dir to the host uid, then drops to that uid with an empty capability bounding set and
  `no_new_privs` — the agent cannot be root (uid 0 refused), change the firewall, or use sudo.
- `docker run` flags: `--rm --init`, memory/swap = `memory` (default 6g), `--cpus 2`,
  `--pids-limit 4096`, `--security-opt no-new-privileges`; NET_ADMIN/NET_RAW only for the firewall
  setup before the privilege drop; `--network none` for export containers.
- Mounts are explicit and read-only except the workspace, `/pnpm-store` and `/out`: entry.sh, prompts,
  acceptance view (read-only), skills. The state dir, spec repo, app clone, GitHub token and `~/.ssh`
  are never mounted. Coder keeps a persistent per-project pnpm volume; acceptance/eval get a fresh
  throwaway volume per attempt (acceptance ran agent code in the previous one).
- `hands <p> check` live-probes the boundary: egress blocked, api.anthropic.com reachable, firewall
  not flushable, no sudo, no IPv6 leak, agent uid not root, expected skills/subagents visible.

## Where secrets live and who reads them

| Secret | Path | Read by |
|---|---|---|
| Claude token | `~/.config/hands/claude.env` | coder/eval/probe/planner containers via `--env-file` (preflight enforces 0600); never the dashboard |
| GitHub PAT | `~/.config/hands/github.env` | `create`/`destroy`/`plan`/`approve` processes only — never the dashboard server, never any container, never logged; HTTPS-only API client that never follows redirects |
| Telegram token | `~/.config/hands/telegram.env` | host-side `Notifier` only |
| Deploy keys | `~/.config/hands/secrets/<slug>/keys/` | host git via per-clone pinned `core.sshCommand` (no agent, IdentitiesOnly, known_hosts from the authenticated /meta API); specs key read-only, app key write |
| Project dev/test secrets | `~/.config/hands/secrets/<slug>/project.env` | coder, acceptance and evaluator containers (`--env-file` + read-only mount at `/run/hands/project.env`); the dashboard reads it for the Environment panel but never returns secret values; the planner gets key names only |

### Project variables: gates and redaction (0.5, step 08)

`hands/projectenv.py` owns the file format (docker `--env-file`: `KEY=value`, values verbatim) and the
secret/public metadata. The runner (`loop.py`) reloads it at every attempt and:

- **Allowlist** (`workspace.check_allowlist`): `.env`, `.env.*` and `.dev.vars` at any depth and in any
  case are always rejected, except `.env.example` and `.env.sample` (names-only templates). Deleting one
  is allowed.
- **Secret scan** (`workspace.scan_secrets`): after the allowlist, every added/modified staged file is
  searched for each secret value of 8+ characters (raw and JSON-escaped). A hit rejects the whole attempt
  with a reason naming the file and the key, never the value.
- **Redaction** (`projectenv.Redactor`): secret values are replaced by `[REDACTED:KEY]` in session
  streams and stderr (rewritten before they are parsed), acceptance logs, diff.patch, the evaluator
  prompt's diff, reasons, progress notes, the runner log, activity.json and every Telegram message.
  Values shorter than 8 characters are neither scanned for nor redacted (documented limit).
- **Preflight / `hands <p> check`**: the file must be a regular 0600 file; `check` reports counts by
  visibility and fails on lines the dashboard cannot manage.
| Dashboard login code | `~/.config/hands/dashboard-login` | minted by `hands-dash url`, redeemed once by the dashboard |

Session streams (`coder.json`, `eval-N.json`) can contain tool output including secrets: files are
0600 (`os.umask(0o077)`) and the dashboard refuses to serve them (`ATTEMPT_FILE` allowlist).

## Dashboard security model (details in docs/api.md)

Control plane on 127.0.0.1:8090, reached through an SSH tunnel. Single-use 5-minute login code
(`hands-dash url`) exchanged for an in-memory 12 h session cookie (HttpOnly, SameSite=Strict); exact-Host
check (anti DNS-rebinding), Origin + `X-Hands: 1` on POSTs (anti CSRF), strict CSP with no inline
script/styles, all agent-influenced text rendered with `textContent`. Read-only over state files except:
the STOP file, and actions that run the `hands` CLI as a subprocess (so the CLI's own checks apply).
It never reads agent workspaces, never holds the GitHub token, and never mounts anything. Bounded:
32 connections, 10 s socket timeout, 10 kB POST bodies, capped file reads, never follows symlinks.

### Session manager (0.5, step 10; `hands/sessions.py`, `hands/ws.py`)

Persistent interactive Claude Code sessions for the host user, run **outside** the container boundary
on purpose (they are Alex's own terminal, with Claude's normal permission prompts), so the rules are
the opposite of the runner's: never autonomous (`--dangerously-skip-permissions` is never passed; the
test suite asserts it on the argv and on the tmux command line), never the dashboard's environment
(an allowlist `PATH HOME TERM LANG USER SHELL` + `CLAUDE_CONFIG_DIR`/`TMUX_TMPDIR` built from scratch
and applied with `env -i`; Claude's login is its credentials file under HOME), working folder under
home but never home itself or a hidden folder. One `claude` per tmux session `hands_s_<uuid>` so it
survives viewers, the dashboard and `bash install.sh`; the dashboard only ever attaches
(`tmux attach-session` on a PTY per WebSocket, spawned with posix_spawn + setsid, never fork in the
threaded server). The WebSocket server is stdlib RFC 6455 in-process, so Host/cookie/Origin checks run
before the upgrade, plus a single-use 30 s ticket minted by a CSRF-checked POST (an upgrade cannot carry
`X-Hands`). Caps: 8 open terminals, 64 kB frames, 256 kB messages, idle close after 60 s. The listing
only reads `~/.config/hands/sessions/*.json` and the one transcript per record; delete unlinks that one
transcript. A systemd unit for the dashboard is prepared in `console/deploy/` (not installed).

**Live on Telegram (step 12).** One session at a time may run with `--channels
plugin:telegram@claude-plugins-official` (Claude Code's official Telegram channel plugin, a research
preview; `sessions.TELEGRAM_CHANNEL`, the only value ever passed). The flag lives in the session record
(`"telegram": true`), is set at creation or on resume, and `Store.live_telegram()` refuses a second
running flagged session (`409`). The plugin, not the harness, owns the bot: token in
`~/.claude/channels/telegram/.env`, sender allowlist in `access.json`, pairing codes, and the permission
relay (Allow / Deny buttons in Telegram, allowlisted senders only). The dashboard never reads
`~/.claude/channels` (and the sandbox/settings deny it to Claude); it only adds the flag to the argv.
Setup and the permission-relay review: `docs/console-review.md` §8, `docs/telegram.md`.

### File browser (0.5, step 13; `hands/files.py`)

The dashboard's only window onto the host file system, and the one place it writes outside
`~/.config/hands`. It replaces the File Browser container (archived upstream) with Python under the same
login, Host and CSRF checks. The boundary is a list of **allowed roots** in `~/.config/hands/files.json`
(default `~/harness ~/projects ~/ops ~/claude-harness`; each must be an existing non-hidden folder inside
home) plus a **deny list that no setting can override**: `~/.config/hands`, `~/.ssh`, `~/.config/gh`,
`~/.git-credentials`, `~/.claude` (and `HANDS_CONFIG_HOME` / `CLAUDE_CONFIG_DIR`), compared by real path.
Every request names a root and a relative path; `..`, NUL and absolute paths are refused before resolving,
then `os.path.realpath` decides: a result outside every root or inside a denied folder is 403 whether it
got there by symlink or otherwise, a loop or missing path is 404. Writes (mkdir, rename, delete, upload)
additionally refuse a symlink as the final component, refuse `.git` trees unless `git_writes` is on,
never overwrite without the flag and never delete a non-empty folder without `recursive: true`; delete
and `shutil.rmtree` unlink links instead of following them. Download and upload stream in 64 kB chunks
(upload: Content-Length required, 256 MB cap checked first, temp file + rename, nothing left on abort);
text reads stop at 2 MB; listings at 5000 entries; search is bounded by hits, entries and time and
never follows links. The refusal table lives in `tests/test_v13.py`.

## Planner and project lifecycle (newproject.py, planner.py)

`create` (CLI or dashboard form): private repos `<org>/<slug>-specs` (CODEOWNERS, ruleset: PR +
code-owner review, no force-push/deletion) and `<slug>-app` (ruleset: no force-push/deletion); per-repo
deploy keys (specs read-only — verified by a push --dry-run that must be refused); clones pinned to
those keys; `created.json` records repo ids so `destroy` deletes only what this run made.
`create --from-repo owner/repo` (0.5, "Start from an existing repo" in the form): the app repo already exists.
Preflight checks it (reachable with the token, not archived, default branch, token has admin, `--branch`
exists); only `<slug>-specs` is created; the existing repo gets a write deploy key (and the protect-main
ruleset only with `--ruleset`), nothing is written to it, and it is cloned at the chosen branch.
`created.json` records it under `imported` (repo, id, deploy_key_id, branch), never under `repos`; `destroy`
deletes only that deploy key, the specs repo and local folders/secrets, and fails closed (deletes nothing on
GitHub) if the record names the imported repo as created or vice versa. Config: `imported = true`,
`repo_app = "owner/repo"`, `branch` = app branch; `spec_branch` (default `branch`) keeps the specs repo
on `main`. `hands/codesummary.py` gives the planner a bounded summary of the clone (tracked files only,
package.json and README read with size caps, never through symlinks) and warns when `allowed_paths` leaves
existing code out (plan state `warnings`, PR body, `hands <p> validate`); the coder prompt says the codebase
existed before and its conventions win.
`plan`: untrusted planner session (web search on, WebFetch off) writes into a clone of the spec repo;
output is exported through a `--network none` container, restricted to the contract
(`steps.json, PLAN.md, allowed-domains, docs/*.md, acceptance/**`), validated (spec loader, printable-ASCII
commands, npm pins ≥ `min_release_age_days` old, Playwright must write outside acceptance/, hostname
syntax for allowed-domains), retried up to 3 rounds, then PR `hands/plan` is opened via the GitHub API.
`approve`: merges exactly the reviewed head sha (squash) after checking the PR and base haven't moved,
re-validates the merged spec, then starts the build. The dashboard's Approve button re-sends the sha it
displayed, and highlights risky acceptance commands and new network domains for human review.

## Install and live copy

`bash install.sh` (only Alex runs it): checks python ≥ 3.12, git/tmux/docker, copies the source dirs to
`~/.local/share/hands/runner`, writes the `hands`/`hands-dash` launchers, seeds `ra.toml` and
`new-project.toml` if missing, installs the watchdog cron line, runs the test suite, restarts the
dashboard. As of this baseline `diff -rq` shows the install is identical to source (both VERSION 0.4.3);
only repo-side files that install.sh does not copy differ (`.git`, `.gitignore`, `.claude`, `.mcp.json`,
`CLAUDE.md`, `install.sh` itself).

## Tests

`python3 -m unittest discover -s tests`: 108 tests, 1 skipped
(`test_runner.TestSmokeSpec.test_smoke_spec` — node is not installed on the host; it runs inside the
image). `tests/fake_docker.py` stands in for Docker, so the suite runs without containers or Claude usage.
