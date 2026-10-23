# Changelog

## 0.6.0 (dashboard render rethink, 2026-10-22)

From `main` 0301dd6 (0.5.8). Session manager untouched.

- Reset controls. `hands <p> plan-chat reset` and "Reset planning chat" on the project panel stop the chat
  container and its PTY, delete the hidden Session manager row and the whole `plan-chat` state directory
  (session id, brief draft, coverage table, env dir, the chat's own `~/.claude`) and put `plan_session` back
  to `none`; the spec repo, an approved brief, the style refs and the setup record are untouched.
  `hands <p> reset --confirm <p> [--wipe-env]` and "Reset project" (type the slug to confirm) destroy what
  `create` recorded and create the project again with its saved settings (description, framework, libraries,
  style ref URLs; the project's variables are kept unless the box is ticked). Imported and self projects are
  refused, and so is a reset while the runner, the scaffold, the planner, a style-ref run or a create is
  going; the settings are staged under `~/.config/hands/creating/<slug>.reset/` before anything is deleted,
  so a reset that fails halfway says what exists and finishes when it is run again. Telegram notice on start,
  success and failure.
- The planning chat no longer resumes an empty session: `launch_spec` looks for the session's transcript in
  the chat's own home and sends the primer again (same session id) when there is none, so a chat whose first
  launch died at the connect check opens as a fresh primed chat instead of a blank one.

- Project payloads carry a change stamp: `GET /api/p/<name>` and `/log` answer with `ETag` + `version`
  (newest mtime over the runner's state files and the spec clone's HEAD, plus runner.log's size);
  `If-None-Match` → `304`. New `GET /api/p/<name>/events`: Server-Sent Events change feed (`event: change`
  with the new version, `: ping` after 15 s of silence, 10 min life, 8 streams max → 503 and the page polls).
- `hands/static/store.js`: one snapshot per project, fed by the feed and conditional fetches; subscribers get
  only the keys that changed; 15 s poll while the feed is down. `logAppend` finds the new tail of a log.
- `hands/static/render.js` + vendored morphdom 2.7.4: the builder page is patched in place instead of rebuilt
  every 4 s. Scroll positions, open `<details>`, focus and typed text survive updates; every section/row has a
  `data-key`; controls carry `data-act` and go through one delegated click listener (no closures on nodes);
  the env editor, file viewers and action output are self-managed nodes.
- `hands/static/logpanel.js`: the runner log appends only new lines, keeps 2000 at most, scrolls to the bottom
  only when it already was there; Follow and Copy buttons.
- Overview strip (sticky: status, runner dot, steps-passed bar, "Now: step · phase" + live activity, actions)
  over collapsible panels with remembered open state (`localStorage` `hands.panels.<name>`); on a phone the
  step rail folds into the strip.
- The housekeeping tick (5 s) only refreshes the tab strip (10 s), usage (60 s) and the creating page.
- Rig: `scroll-keep`, `log-keep` and `quiet` pages; `tools/js-test.sh` runs the JS unit tests
  (`hands/static/*.test.mjs`) and `node --check` on every served script with the node in the agent image.

## 0.5.8 (step 24: merge tap + dashboard rounds (B3), 2026-10-21)

From `main` d70179d (0.5.7). Session manager untouched.

- Merge tap (`hands/merge.py`, dashboard "Merge change C<n>", `hands <p> merge --branch-sha --main-sha`): the
  one merge path for a change round, on the runner's app clone with its deploy key (plain git, no GitHub
  API, never a force push). Sha-pinned: refused unless `origin/main` still equals the shown sha and the
  round's base and `origin/change/C<n>` equals the shown head; also unless every spec step passed, the runner
  is stopped and push is enabled. Squash locally, verify the merged tree equals the tested round head's tree,
  tag `hands/merged/C<n>` at the unsquashed head (pushed first), push main without force (a move in between is
  rejected by git and rolled back locally), record `merged` + `merged_sha` + `head_sha` + `merged_tag`,
  delete `change/C<n>` on origin, Telegram notice. `hands/pre-C<n>` (before) and `hands/merged/C<n>` (the
  per-step commits) stay as the hand rollback points.
- Discard (`hands <p> discard --branch-sha`, dashboard "Discard round C<n>"): the work is tagged
  `hands/discarded/C<n>-<utc>`, branch and pre-tag deleted, the round's steps go back to pending, the record
  is dropped; the next Start rebuilds the same steps in a fresh round. The steps stay in the spec: discarding
  the work is not discarding the change request (that needs a spec PR); the confirm says so.
- `self` projects (TOML `self = true`, Phoenix building itself, step 29): no Merge tap, the CLI refuses, the
  installer (step 28) calls `merge_round` directly: two callers, one merge path. Discard is allowed.
- Round history: `state/rounds.jsonl` gets `opened` (with the amendment's request and PR from plan.json),
  `merged` and `discarded` events; the record carries `request`, `pr`, `pr_url`.
- Dashboard: a `C<n>` tag per project tab (open / merge? / merged / ready), the round line in the project
  head, and a "Change rounds" section (one card per round: request, PR, steps, state, the sha-pinned Merge
  and Discard taps with confirmations, refusal reasons, merge/discard log) next to the Amend plan form.
  `GET /api/projects` rows carry `round` (chip), `GET /api/p/<name>` `round` becomes `{record, merge, history,
  log, branch}`, `POST /api/p/<name>/action/merge|discard` spawn the CLI detached (record `merging` /
  `discarding` meanwhile).
- `reset-step` after a merge: passed steps of a merged or earlier round, and round-0 steps once any round
  exists, are refused ("merged work is never rebuilt; make a new change request"). Only steps of the open
  round, or any step of a project without rounds, can be reset.
- Planner: `app_snapshot`'s unpushed-work check ignores commits reachable from a merged round's `head_sha`,
  so `plan --amend` works after the squash merge deleted `change/C<n>` (step 23 note 7).
- `hands <p> status` prints `round C1: open on change/C1 (… 2/2 passed, merge ready)` or
  `round C1: merged into main at <sha> (tags …)`.
- Independent review (fresh-context subagent): 0 critical, 2 important, 11 minor, all fixed with tests: the
  shown branch head must be the last passed step's commit (a hand-pushed, fast-forwarded commit is refused);
  discard resets failed/never-attempted round steps too and the UI lists every round step by id; the merged
  tag is pushed after main (a refusal changes nothing on origin) and the branch is deleted only if its head is
  still the merged one; squash cleanup uses `reset --hard`; dashboard/CLI gates count pending steps from
  steps.json; unexpected errors never leave the record in `merging`; a held lock is refused without saving;
  shas use `fullmatch`; discard with push off is pinned to the local head; a re-opened round keeps the
  discarded round's card; `merge_failed` events; the CLI refuses while the planner runs.
- Human steps in a round: `mark-done` records the branch head on origin as the step's commit (`by: "human"`),
  so the merge pins to the head the human left; outside a round the commit stays `"human"`. Push refusals are
  classified: repository rule (`GH013`, protected branch, hook) vs lost race, with distinct messages.
- Tests: `tests/test_v058.py` (merge happy path and 8 refusals incl. a main move between fetch and push,
  discard + rebuild, CLI, reset-step rules, app_snapshot, dashboard JSON and taps, request/PR in the record).
  Rig fixtures bakery / florist / garage / phoenix-next and the merge / discard dialog pages.

## 0.5.7 (step 23: change rounds + regression rule (B2), 2026-10-21)

From `main` ffaadfc (0.5.6). Session manager untouched.

- Change rounds (`hands/rounds.py`): the build of an approved amendment runs on its own app branch. A step
  belongs to the round its id names (`C<n>-` → round n; a fresh plan is round 0 and is unchanged: built and
  pushed on `main`, no branch, no tag, no record). When the next pending step belongs to a round that is not
  open yet, the runner opens it before the app sync: branch `change/C<n>` from `origin/main`, tag
  `hands/pre-C<n>` at that base (never moved; a tag at another commit escalates), both pushed, then the record
  `round {id, branch, base_sha, pre_tag, spec_commit, opened_at, status}` in state.json. Every part is
  idempotent (a crash in the middle re-runs it). Sync, push, `check` and the planner's app snapshot use the
  active branch; `main` is untouched until the merge tap (step 24). One round open at a time: a higher round's
  step, or `plan --amend`, while one is open is refused with "round C<n> is built on change/C<n> but not
  merged". `reset-step` refuses a passed step of an earlier round while a round is open (passed steps are
  never rebuilt). Recovery, STOP/start and the watchdog work unchanged on the branch. `hands <p> status`
  prints the round line; the project JSON carries `round` and per-step `round`.
- Per-step integrity record: when a step passes (also `mark-done`) the runner records `spec_hash` (sha256 over
  the step's canonical steps.json entry and the bytes of the acceptance files it owns, listed in
  `spec_files`), `round` and `branch`. Passed step entries and their owned acceptance files are immutable:
  the runner re-checks every record on every spec load and escalates before any session when one changed
  (hand-merged spec PRs included); the amend validator checks candidates against the same record, so the
  spec repo's history is no longer the only reference.
- Regression rule (rounds only): in a change round every attempt runs, after `setup` and `regression`, the
  acceptance commands and e2e of every passed step in spec order, then the step's own; the coder prompt lists
  them under the "change round" note and a failure reads "regression: acceptance of passed step S01 failed".
  Round 0 keeps the spec's `regression` list only, so fresh builds are unchanged.
- Tests changed/removed are data: an amendment declares every existing acceptance file it changes or removes
  in steps.json `tests_changed` (`[{"path", "change": "change"|"remove", "why"}]`); the validator compares
  the list with the real diff of existing `acceptance/**` files exactly (unlisted change, listed-but-unchanged,
  wrong kind, non-existing path, or a built step's file → rejected); a fresh plan may not have the list. Approval
  needs its own tick: dashboard `tests_ack` (a table with path, change and reason) and CLI `--ack-tests` (prints
  the list). PLAN.md's "Tests changed/removed" section is a human summary only.
- Planner fix rounds: a contract file the previous output removed stays removed in the fresh clone of the next
  round (`make_work` re-applies recorded deletions), closing the step 22 open issue.
- Fresh planning rejects step ids that look like `C<n>-` (reserved for amendment rounds).
- Docs: `docs/architecture.md` "Change requests" section (rounds, immutability rule, database migrations not
  supported yet), `docs/api.md`, `docs/install-0.5.7.md`. Tests: `tests/test_v057.py` (rounds, pre-tag,
  regression failure, frozen records, fresh project unchanged, recovery/STOP/watchdog on the branch, one open
  round, reset-step rule, status line), planner tests for `tests_changed`, the tick and the deletion re-apply.

## 0.5.6 (step 22: planner amend mode, 2026-10-20)

From `main` 1c3f3bb (0.5.3). 0.5.4 and 0.5.5 were abandoned (kept on tag `keep-0.5.5`); nothing from them is here.

- Planner amend mode: `hands <p> plan --amend` (change request on stdin) and the dashboard's **Amend plan** form
  on approved projects. An approved plan may only grow: the planner gets the spec clone at `/workspace`, the app
  repo at the built commit **read-only at `/app`** (a `git archive` of the pinned sha extracted on the host with
  the export tar rules: regular files only, no `..`, no symlinks or hard links, size caps; no `.git`, no
  credentials; refused when the clone has local changes or commits not yet on origin) and a build summary from
  `state.json`.
- Append-only validation on top of the fresh-plan checks: existing steps keep id, place and order; new ids must
  start with `C<round>-`; a step the runner already built may not change (its `steps.json` entry and the
  acceptance files it owns are compared byte for byte with the spec commit recorded when it passed;
  `mark-done` now records that commit too); a changed or removed existing acceptance file must be listed in
  PLAN.md under `## Tests changed/removed` (removed files become deletions in the PR); existing pins stay as
  they are (no pins at all is fine when the base had none; only new pins are age-checked); an amendment must
  add at least one step. Shared settings (setup, regression, e2e_command, allowed_paths,
  allow_text_paths, always_refs) may change but are flagged with old and new values; Approve is refused until
  the reviewer ticks them (`shared_ack`, like the network-domains tick). Both ticks are now enforced
  server-side and by the CLI: `hands <p> approve [sha] --ack-shared --ack-domains`; without the flag the
  CLI prints the old and new values (or the domains) it would be acknowledging and refuses; with it, the
  acknowledged values go to plan.log. The dashboard passes its ticks on as those flags.
- Each amendment round opens its own branch and PR `hands/amend-C<round>`; approve is the same sha-pinned
  squash merge and start. Early refusals (build running, a fresh plan awaiting review, no steps.json, empty
  change request, plain `plan` on an approved project) leave the plan state as it was and only set `error`.
- `plan.json`: `mode`, `round`, `request`, `branch`, `app_sha`, `new_steps`, `tests_changed`, `shared_changed`;
  `plan_info` returns them and marks new steps in `step_list`; `POST /api/p/NAME/action/amend`.
- Dashboard plan view: shown on approved projects again (compact Amend plan form); amendment review highlights
  new steps, lists the change request, the built commit, changed tests and the shared-settings diff; the
  button reads "Approve & build amendment". Fresh planning is unchanged.
- Tests: `tests/fake_docker.py` enforces `readonly` binds (a scenario `write` under one fails with
  "Read-only file system"); new `TestAmend` class in `tests/test_planner.py`; rig fixture `cafe` (amendment
  ready) in `tools/fixtures.py`, captured by `tools/ui-shots.sh` by default.

## 0.5.3 (step 18: public access, 2026-10-18)

- Public access through Cloudflare Access (`hands/publicaccess.py`, `docs/public-access.md`): with
  `~/.config/hands/public.json` (written by `hands-dash public set --host H --team T --aud A --email E`), a
  request whose Host is a listed public host is logged in by the Cloudflare Access JWT in
  `Cf-Access-Jwt-Assertion` on every request (pages, static, API, WebSocket upgrade). Verification is
  stdlib only and fails closed: RS256 only, `kid` from `https://<team>/cdn-cgi/access/certs` (cached 1 h,
  unknown kid refetched at most once a minute), RSASSA-PKCS1-v1_5/SHA-256 with the full encoding compared
  byte for byte, `aud`, `iss`, `exp` (60 s leeway), `nbf`/`iat`, email allowlist. No cookie login there,
  `/login` is 404, `Origin` must be `https://<host>`, the email is logged and the token never. The local
  Host (SSH tunnel, `hands-dash url`) is unchanged and ignores the token; any other Host stays 403;
  missing/empty/invalid config = off. The file is re-read on change: `set|off|show|test` need no restart.
- Request bodies: every request's body is read in full before any response (also on 401/403/404/405 and
  GET), so a kept-alive connection never carries leftover bytes into the next request (0.5.2 logged
  `{}POST … 501`). Bodies over 64 kB → 413 and close; `Transfer-Encoding` → 411 and close.
- Terminal tickets are bound to the principal that minted them (cookie session or Access email).

## 0.5.2 (step 17: owner fixes, 2026-10-18)

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
- Requirement 1 (no tmux): sessions are PTYs owned by the dashboard, the claude-harness model in stdlib
  (`docs/sessions-pty.md`): `sessions.Proc` spawns `claude` through `/bin/sh -c 'cd … && exec …'` with
  posix_spawn + setsid on a dashboard-owned PTY, a drain thread keeps a 256 kB scrollback and fans output
  out to every attached viewer; closing the browser detaches, reopening replays the scrollback; process
  exit closes the viewers and shows the session stopped. A dashboard restart ends the sessions (SIGHUP)
  and offers each for Resume (`claude --resume`). tmux, `env -i`, `tmux attach` and `TMUX_TMPDIR` are gone
  from the session path (the dashboard and the build loops still run in tmux). The rig runs the real
  backend with its fake claude; the real-PTY tests replace the tmux integration test (no skip).
- Requirements 2 and 8 (Telegram): a session started with Telegram selected gets
  `--settings '{"enabledPlugins":{"telegram@claude-plugins-official":true}}'` in front of `--channels`,
  so the plugin can stay disabled in `~/.claude/settings.json` (the ops Claude never loads it). Verified
  with a real Claude Code 2.1.292 process by debug log (`docs/telegram.md` §5); Alex flips the user
  setting to false after the live check.
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
