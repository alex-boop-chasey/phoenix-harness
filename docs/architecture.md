# Phoenix Harness 0.5.0 — architecture

Baseline written 2026-10-17 (step 01 of the 0.5 work) and updated by every step up to 16 (release 0.5.0),
from the code on branch `phoenix-0.5`. Everything here was read from the source in `~/harness`; the live install
in `~/.local/share/hands/runner` is 0.4.3 until Alex installs 0.5.0 (see "Install and live copy" and `docs/install-0.5.md`).

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
| Agent containers | `hands/docker.py`, `container/entry.sh`, image files in `image/` | Docker, image `hands:cc2.1.301-pw1.64.1-dns2` |

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
    state.json                       status, steps {status, attempts, commit, reasons, spec_commit, spec_hash,
                                     spec_files, round, branch}, round {id, branch, base_sha, pre_tag, status},
                                     totals, current
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
   validate `steps.json` (`spec.load`); re-check every passed step's integrity record (0.5.7, below); pick
   the first non-passed step and, when its id says `C<n>-` and round n is not open yet, open the change
   round (branch `change/C<n>` from `origin/<branch>`, tag `hands/pre-C<n>`, both pushed, record in
   state.json); sync the app clone with origin on the active branch (push accepted commits, escalate on
   divergence — the runner never force-pushes).
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
   runs in its own container (in a change round also every passed step's own `acceptance` and e2e, in spec
   order, before the step's own: the regression rule, 0.5.7; a failure there reads "regression: acceptance
   of passed step S01 failed"); pass/fail is the container's exit code, output streams to a host file so
   nothing inside can forge it. First failure rejects the attempt.
7. **Independent evaluator**: another fresh clone + volume; read-only session (Write/Edit/Agent/WebFetch
   disallowed) gets the diff, acceptance report and coder summary plus a random nonce, and must end with
   `VERDICT: {"pass": bool, "reasons": [...], "nonce": ...}`. Verdict parsing is fail-closed
   (`claude.parse_verdict` on a clean session's own result only); wrong/missing nonce = no verdict;
   3 failures → tag `hands/unreviewed/...`, reset, escalate.
8. **Accept or reject**: pass → state updated first (`commit`, `spec_commit`, `round`, `branch`, and the
   integrity record `spec_hash` = sha256 over the step's canonical steps.json entry and the bytes of the
   acceptance files it owns, listed in `spec_files`; `mark-done` writes the same), then push to the active
   branch (an unpushed accepted commit is pushed by the next round's sync); progress note + Telegram. Fail → tag `hands/failed/<step>-<n>`, hard reset to
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
  api.anthropic.com, registry.npmjs.org + the spec repo's `allowed-domains`; no telemetry hosts;
  skipped only with `--network none`), gives the home dir to the host uid, then drops to that uid with
  an empty capability bounding set and `no_new_privs` — the agent cannot be root (uid 0 refused),
  change the firewall, or use sudo. Nothing else is allowed: no GitHub ranges, no host /24, no tcp/22
  (removed in 0.5.1; GitHub is reachable only if a project lists it in `allowed-domains`).
- DNS filter (`image/init-firewall.sh`, `image/hands-dns-conf.sh`, 0.5.1): a root-owned dnsmasq on
  127.0.0.1:53 (`no-resolv`, config under root-only `/run/hands-dns/`) forwards only the allowlisted
  names to the container's original resolvers and answers NXDOMAIN for everything else; the addresses
  it resolves are added to the firewall's ipset at query time (`ipset=/name/allowed-domains`), so
  CDN rotation does not break builds. iptables: the agent uid may send port 53 only to 127.0.0.1; only
  uid 0 (dnsmasq) may reach the upstream resolvers; all other port-53 egress is dropped. The config
  generator accepts only exact lowercase hostnames and IPv4 upstreams and fails closed, so an
  `allowed-domains` line cannot inject dnsmasq directives. dnsmasq matches by suffix: a listed name
  also resolves its subdomains.
- `docker run` flags: `--rm --init`, memory/swap = `memory` (default 6g), `--cpus 2`,
  `--pids-limit 4096`, `--security-opt no-new-privileges`; NET_ADMIN/NET_RAW only for the firewall
  setup before the privilege drop; `--network none` for export containers.
- Mounts are explicit and read-only except the workspace, `/pnpm-store` and `/out`: entry.sh, prompts,
  acceptance view (read-only), skills. The state dir, spec repo, app clone, GitHub token and `~/.ssh`
  are never mounted. Coder keeps a persistent per-project pnpm volume; acceptance/eval get a fresh
  throwaway volume per attempt (acceptance ran agent code in the previous one).
- `hands <p> check` live-probes the boundary from inside an agent container (`FIREWALL_PROBE` in
  `hands/__main__.py`): egress blocked by name and by raw IP, api.anthropic.com reachable, firewall not
  flushable, no sudo, no IPv6 leak, allowlisted name resolves, random name does not, direct queries to
  1.1.1.1/8.8.8.8 (udp+tcp) blocked, dnsmasq cannot be killed or reconfigured, tcp/22 to an outside IP
  blocked, a GitHub IP and api.github.com blocked (unless allowlisted), the host (default gateway)
  blocked, agent uid not root, expected skills/subagents visible.

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

### Public access through Cloudflare Access (0.5.3, step 18; `hands/publicaccess.py`)

Optionally the same server is reached at `https://<public host>` through a Cloudflare Tunnel (cloudflared,
operator-owned, connects from 127.0.0.1 to 8090) with Cloudflare Access in front. The Host header decides:
a host listed in `~/.config/hands/public.json` is public, and there the Access JWT (`Cf-Access-Jwt-Assertion`)
on every request is the login: RS256 only, `kid` from the team's JWKS (cached 1 h, unknown-kid refetch at
most once a minute, fail closed), signature verified with `pow()` against the full EMSA-PKCS1-v1_5 encoding,
`aud`/`iss`/`exp`/`nbf`/`iat`/allowlisted `email`. No cookie login on a public host, no token login on the
local host, `Origin` must be `https://<host>`, the email (never the token) is logged. The file is re-read on
change, so `hands-dash public set|off` needs no restart; missing/invalid = off = 403 for every non-local
Host, as before. Setup: `docs/public-access.md`.

### Session manager (0.5, step 10; `hands/sessions.py`, `hands/ws.py`; PTYs since 0.5.2)

Persistent interactive Claude Code sessions for the host user, run **outside** the container boundary
on purpose (they are Alex's own terminal, with Claude's normal permission prompts), so the rules are
the opposite of the runner's: never autonomous (`--dangerously-skip-permissions` is never passed; the
test suite asserts it on the argv), never the dashboard's environment (an allowlist `PATH HOME TERM LANG
USER SHELL` + `CLAUDE_CONFIG_DIR` built from scratch and passed to posix_spawn as the entire environment;
Claude's login is its credentials file under HOME), working folder = the home folder (default) or a
folder inside it, never a hidden folder.

**No tmux (0.5.2, requirement 1; `docs/sessions-pty.md`).** Each session is one `claude` child on a
pseudo-terminal owned by the dashboard process (`sessions.Proc`): `/bin/sh -c 'cd -- "$1" && shift && exec
"$@"'` spawned with posix_spawn + setsid and the PTY slave opened by path as fd 0 (controlling terminal;
never fork in the threaded server). A drain thread is the only reader of the master: it appends to a 256 kB
ring buffer (scrollback) and fans each chunk out to the attached WebSocket viewers. A viewer
(`sessions.Viewer`) is a set member: attaching replays the ring buffer, its first resize forces a SIGWINCH
so the TUI repaints; closing the browser just detaches, the process runs on; several viewers may be
attached at once (input from any, last resize wins). Process exit is detected by `waitpid` polling: viewers
get `{"type": "exit"}` and are closed, the row shows stopped. The dashboard owns the PTYs, so a dashboard
restart (`bash install.sh`, `hands-dash stop`) ends every session (SIGHUP, as a closed terminal would) and
the list offers each one for Resume (`claude --resume <uuid>`).
The WebSocket server is stdlib RFC 6455 in-process, so Host/cookie/Origin checks run before the upgrade,
plus a single-use 30 s ticket minted by a CSRF-checked POST (an upgrade cannot carry `X-Hands`). Caps: 8
open terminals, 64 kB frames, 256 kB messages, idle close after 60 s. The listing only reads
`~/.config/hands/sessions/*.json` and the one transcript per record; delete unlinks that one transcript.
A systemd unit for the dashboard is prepared in `console/deploy/` (not installed).

**Live on Telegram (step 12; per-session plugin enable since 0.5.2).** One session at a time may run with
`--settings '{"enabledPlugins":{"telegram@claude-plugins-official":true}}' --channels
plugin:telegram@claude-plugins-official` (Claude Code's official Telegram channel plugin, a research
preview; `sessions.TELEGRAM_SETTINGS`/`TELEGRAM_CHANNEL`, the only values ever passed). The plugin is
disabled in the user's settings (requirement 8: no other `claude` on the host, the ops Claude included,
loads it); `--settings` merges over the user file for that one process, verified on 2.1.292
(`docs/telegram.md` §5). The flag lives in the session record
(`"telegram": true`), is set at creation or on resume, and `Store.live_telegram()` refuses a second
running flagged session (`409`). The plugin, not the harness, owns the bot: token in
`~/.claude/channels/telegram/.env`, sender allowlist in `access.json`, pairing codes, and the permission
relay (Allow / Deny buttons in Telegram, allowlisted senders only). The dashboard never reads
`~/.claude/channels` (and the sandbox/settings deny it to Claude); it only adds the flag to the argv.
Setup and the permission-relay review: `docs/console-review.md` §8, `docs/telegram.md`.

### File browser (removed in 0.5.2)

Steps 13-14 built a file browser page (`hands/files.py`, `/api/files/*`); 0.5.2 removed it at Alex's request:
files on the VPS are browsed with the separate File Browser container on 127.0.0.1:8080, and the dashboard
again touches nothing outside `~/.config/hands` and the project state it serves. The deny list and refusal
table live in the git history (tag `v0.5.0-rc1`).

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
## Change requests (amend mode and change rounds)

`plan --amend` (0.5.6, dashboard "Amend plan"): once a spec is approved the plan may only grow. The change
request (stdin) goes to the same planner session with the spec clone at `/workspace`, the app repo at the
built commit read-only at `/app` (a `git archive` of the pinned sha, extracted on the host with the export
tar rules: regular files only, no `..`, no links, size caps; no `.git`, no credentials; refused when the clone
is dirty or has commits not on origin, so an accepted-but-unpushed step is never reset away) and a build summary
from `state.json`. On top of the fresh-plan checks the validator rejects: a missing or reordered existing
step; a new id without the `C<round>-` prefix; any change to a step the runner already built (its
`steps.json` entry and the acceptance files it owns are compared byte for byte with the spec commit recorded
when it passed, `mark-done` records it too, and since 0.5.7 the runner's own record `spec_hash`/`spec_files`
is the final arbiter, independent of the spec repo's history); a changed or removed existing acceptance file
that is not declared in steps.json `tests_changed` (`[{"path", "change": "change"|"remove", "why"}]`, 0.5.7:
the list must match the real diff of existing `acceptance/**` files exactly, in both directions; a file of a
built step may not appear; PLAN.md may summarise it but is not the source of truth; removed files become
deletions in the PR); a dropped or re-versioned pin (no pins at all is fine when the base had none; only new
pins are age-checked); an amendment that adds no step; in a fresh plan, ids that look like `C<n>-` or a
`tests_changed` list. Changes to shared settings (setup, regression, e2e_command, allowed_paths,
allow_text_paths, always_refs) are accepted but flagged (`shared_changed`, old vs new): Approve is refused
until the reviewer ticks them, in the dashboard (`shared_ack`, server-side) and in the CLI (`--ack-shared`,
which prints the old and new values); changed or removed tests need their own tick (`tests_ack` /
`--ack-tests`, which prints the list with reasons); new network domains need the same (`domains_ack` /
`--ack-domains`). Each round opens its own branch `hands/amend-C<round>` and PR; approve is the same
sha-pinned squash merge; the runner then picks up the first non-passed step as usual. Early refusals (build
running, a fresh plan awaiting review, no steps.json, empty change request, an open change round) leave the
plan state untouched and only set `error`. Fix rounds of the planner (output rejected, up to 3 tries) start
from a fresh spec clone with the previous output overlaid; a contract file the previous output had removed
is removed again by the harness (`make_work`), so a deletion never silently comes back.

**Change rounds (0.5.7, B2).** The build of an approved amendment is a *round*. Round 0 is the fresh plan:
built and pushed on `cfg.branch` (`main`), nothing else. A step belongs to the round its id names
(`C<n>-` → round n). When the next pending step belongs to round n and the runner's record is older, the
runner opens round n before the app sync: fetch `origin/<branch>`, branch `change/C<n>` from it (or pick up
the branch if a crashed run already pushed it), tag `hands/pre-C<n>` at the base (never moved; a tag at
another commit escalates), push tag and branch, then write `state.json` `round` = `{id, branch, base_sha,
pre_tag, spec_commit, opened_at, status: "open"}`. Every part is idempotent, so a crash in the middle just
re-runs it. From then on the runner syncs, builds and pushes on the round branch; `main` is untouched until
the merge tap (below) squash-merges `change/C<n>` and sets `status: "merged"`. One round is open at
a time: a higher round's step while one is open, or `plan --amend` while one is open, is refused. In a round
every pending step builds on the branch. Recovery, STOP/start and the watchdog need no
special case: the clone stays on the branch, `recover()` resets to the attempt's base there.

**Merge tap and discard (0.5.8, B3, `hands/merge.py`).** One merge path for every kind of project, on the
runner's app clone with its deploy key: plain git, no GitHub API, never a force push. The dashboard tap
("Merge change C<n>") and `hands <p> merge --branch-sha X --main-sha Y` are sha-pinned: the shas are the
heads the page showed (`change/C<n>` from the clone's local branch, main = the round's `base_sha`).
`merge_round` refuses, with nothing changed on origin, unless the round is open, every spec step passed
(so the regression rule ran on the last one), the runner is not running, push is enabled, `origin/main`
still equals the shown sha and the round's base, and `origin/change/C<n>` equals the shown head. Then,
locally: `main` at the base, `merge --squash`, one commit (`Change C<n>: <request line>`, author
`hands <hands@localhost>`), the merged tree must equal the round head's tree (a squash onto the unchanged
base is exactly the tested tree; otherwise local main is put back and the merge refused), push
`HEAD:refs/heads/main` without force (origin moving in between makes git reject the non-fast-forward: local
main is put back, "press Merge again"; up to here a refusal changes nothing on origin and appends a
`merge_failed` event), record `{status: merged, head_sha, merged_sha, merged_tag, merged_at}`, tag
`hands/merged/C<n>` at the unsquashed head and push it (the per-step commits and their trailers stay
reachable), then delete `change/C<n>` on origin only if its head is still the merged one (a push in the
window leaves the branch alone with a WARN; a failed tag or delete is logged, not fatal). The shown branch
head must also be the commit of the round's last passed step: a commit pushed to `change/C<n>` by hand after
the last pass (even one the runner fast-forwarded into its clone) is refused, "only the tested head is
merged". Tags stay: `hands/pre-C<n>` = the state before the change, `hands/merged/C<n>` = the unsquashed
work; rollback is by hand (`git reset --hard hands/pre-C<n>` and a force push nobody automates).
The dashboard POSTs `/api/p/<name>/action/merge` with both shas; the server re-checks the gate
(`merge.tap_state`: open, complete, not running, not `self`, shas equal to the clone's), sets the record
`merging`, and spawns the CLI detached (log `state/round.log`); the CLI finishes or puts the record back to
`open` with `error`. A project with `self = true` in its TOML (Phoenix building itself, U4) has no tap: the
gate answers "merged by Install update", the CLI refuses, and the installer (U3) calls `merge_round`
directly. Two callers, one merge. Telegram gets "merged C<n> into main (<sha>)".

*Discard* (`hands <p> discard --branch-sha X`, dashboard "Discard round C<n>"): pinned on the branch head;
tags the work `hands/discarded/C<n>-<utc>` (pushed), deletes `change/C<n>` and `hands/pre-C<n>` on origin
and locally (the reset point is consumed; a reopened round gets a fresh tag at the current main), puts the
clone on `main` at `origin/main`, sets every step of that round back to pending, drops the record and
appends the event. The amendment's steps stay in the spec (the validator never removes a step) and the
next Start rebuilds them in a fresh round C<n> from the same base: discarding the work is not discarding
the change request; that needs a spec PR.

*History*: `state/rounds.jsonl` (append-only) gets `opened` (with the amendment's `request`, `pr`,
`pr_url` copied from plan.json when it matches the round), `merged` and `discarded` events; the dashboard's
"Change rounds" section lists every round from it with its steps (per-step `round`), PR and outcome, and
the projects' tabs carry a `C<n>` tag (open / merge? / merged / ready for a `self` project).

*After a merge*: `reset-step` refuses every passed step of a merged or earlier round, and round-0 steps once
any round exists (rebuilding them on main would bypass the round branch, the pre-tag and the regression
rule): "merged work is never rebuilt; make a new change request (Amend plan)". Only steps of the open round,
or any step of a project that never had a round, can be reset. The planner's `app_snapshot` ahead check
excludes commits reachable from the merged round's `head_sha`, so a clone left on the deleted branch is not
mistaken for unpushed work (`plan --amend` works again as soon as the round is merged).

**Immutability rule.** A passed step's `steps.json` entry and the acceptance files it owns are immutable:
the runner records `spec_hash` over exactly those when the step passes and re-checks it on every spec load
(a mismatch escalates before any session starts: "passed step(s) changed in the spec since they passed"),
and the amend validator checks candidates against the same record. Everything else may change in later
rounds through the validator: `docs/*.md`, PLAN.md, shared settings (ticked), tests of steps not built yet
and shared tests (declared in `tests_changed`, ticked), pins (only additions).

**Not supported yet: database migrations.** A change round rebuilds nothing and runs on a branch of the
app repo only; it has no notion of a database schema, data migration, or rollback of either. A project
with a database (Supabase, Postgres, SQLite files, …) whose change needs a schema change requires a
separate design (migration steps, acceptance against a seeded database, a rollback story for the
`hands/pre-C<n>` tag) before change rounds are used on it.

## Install and live copy

`bash install.sh` (only Alex runs it): checks python ≥ 3.12, git/tmux/docker, copies the source dirs to
`~/.local/share/hands/runner`, writes the `hands`/`hands-dash` launchers, seeds `new-project.toml` if
missing (never a project config: `tests/test_install.py`), installs the watchdog cron line, runs the test suite, restarts the
dashboard. Until 0.5.0 is installed the live copy is 0.4.3 (see `docs/install-0.5.md`); at the 0.4.3 baseline
`diff -rq` showed the install identical to source;
only repo-side files that install.sh does not copy differ (`.git`, `.gitignore`, `.claude`, `.mcp.json`,
`CLAUDE.md`, `install.sh` itself).

## Tests

`python3 -m unittest discover -s tests`: 249 tests at 0.5.0, 2 skipped
(`test_runner.TestSmokeSpec.test_smoke_spec` — node is not installed on the host; it runs inside the
image — and the tmux integration test, which the sandbox cannot run; it passes outside it). `tests/fake_docker.py` stands in for Docker, so the suite runs without containers or Claude usage.
