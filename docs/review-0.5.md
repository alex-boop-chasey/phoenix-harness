# Security review of 0.5.0 (`main..phoenix-0.5`)

Written 2026-10-17 (step 16). The review was done by a fresh Claude subagent with no access to the
author's reasoning, briefed only with the diff, the focus list below and the rule not to read secrets.
Diff reviewed: 72 files, +12,712 / -216 lines (30 files under `hands/`). Focus: secrets reaching logs,
UI, containers or repos; auth, Origin and CSRF on every new route; path handling in the File browser;
the console's spawn arguments and environment; CSP.

Every finding below was verified by reading the code, then fixed in commit `step-16` with a test in
`tests/test_v16.py`, or recorded with the reason it is not a problem. Severity is the reviewer's.

## Findings and fixes

### 1. Medium: secret values could be visible before the runner's redaction ran
**Finding.** The runner redacts an attempt's files (`coder.json`, `coder.stderr`, `accept-N.log`,
`eval-N.json`) only after the container exits (`Runner.redact_files` after `docker.run`). During a session
the dashboard served `coder.stderr` and `accept-N.log` live, so a coder running `cat /run/hands/project.env`
or a test dumping `process.env` showed raw values for up to `session_timeout_min`; if the runner died
mid-session (OOM, SIGKILL, reboot) the files stayed unredacted. Separately, `~/projects` is a default File
browser root, so `~/projects/<slug>/state/attempts/*/coder.json`, `eval-N.json`, `state/plan/session-N.json`
and the live workspace `state/work/**` were readable through `/api/files/read|download`.
**Fix (three layers).**
- Dashboard: everything served from `attempts/` (file bodies, the attempt list's reasons) and `runner.log`
  passes through the project's `Redactor` again at read time (`dashboard.redactor_for`), so a value in a
  not-yet-redacted file never reaches the browser. Test: `test_dashboard_redacts_attempt_files_while_serving`.
- Runner: `Runner.recover()` (runs at every start after an interrupted attempt) redacts the interrupted
  attempt's files. Test: `test_recover_redacts_interrupted_attempt`.
- File browser: every project's `state_dir` (parsed from each `~/.config/hands/projects/*.toml`) joins the
  always-denied list; denied folders are not even named in listings or search. Test:
  `test_project_state_dirs_denied`.
In-stream redaction (piping container output through the Redactor as it is written) was considered and not
done in this release: the three layers above close the exposure paths that exist, and in-stream redaction
would change `docker.run`'s file handling for every role. Noted under "Later".

### 2. Low: reserved keys in a hand-edited `project.env` still reached containers
**Finding.** `projectenv.Store` records `GH_TOKEN=...` or `CLAUDE_CODE_OAUTH_TOKEN=...` lines as
problems but keeps the value; the runner passed the file verbatim (`--env-file` + bind mount). Only
`hands <p> check` failed on it.
**Fix.** `Runner.preflight` refuses to start when the store reports a reserved key (same rule as `check`).
Test: `test_preflight_refuses_reserved_keys_in_project_env`.

### 3. Low: roots loaded from `files.json` skipped the hidden-folder rule
**Finding.** `validate_root()` refuses hidden top-level folders (`~/.config`, `~/.local`) but only on
save; `settings()` applied just the deny list, so a hand-edited or restored `files.json` could expose
`~/.config/claude-harness`, `~/.local/share/hands/runner`, npm/pip caches.
**Fix.** One rule (`files._root_allowed`) is used on save and on every load. Test:
`test_hidden_roots_in_settings_file_dropped`.

### 4. Low: a refused upload left its body on a keep-alive socket
**Finding.** When `files.Upload(...)` raised (409 exists, 400, 403) before the body was read, the
response went out without `close_connection`, so up to 256 MB of body was parsed as the next request.
**Fix.** The upload handler sets `close_connection` on any failure before the body is read, and
`_send` adds `Connection: close` whenever the connection will close. Test:
`test_failed_upload_closes_the_connection` (raw socket: 409, then the socket is closed, nothing parsed).

### 5. Low: the `.git` guard was per-entry; a whole clone could be deleted or moved
**Finding.** `_git_guard` only checked the entry's own path for a `.git` segment; a recursive delete or a
rename of a folder containing `.git` (e.g. `~/projects/smoke/app`) was allowed with `git_writes` off.
**Fix.** For folders, the guard also refuses when a `.git` exists anywhere below (bounded scan, no
symlink following) unless `git_writes` is on. Test: `test_git_guard_covers_folders_containing_git`.

### 6. Low: planning an imported project reset the app clone
**Finding.** `gitops.refresh_app` did `reset --hard` + `clean -fdx` on every plan of an imported project;
a Session manager session may be working in that folder.
**Fix.** `refresh_app` refuses when the clone has local changes (named in the error); the planner reports
it instead of discarding anything. Test: `test_refresh_app_refuses_dirty_clone`.

### 7. Info: vendored xterm.js had no recorded integrity
**Fix.** `test_vendored_xterm_pinned` pins the sha256 of the three vendored files
(`xterm.min.js`, `addon-fit.min.js`, `xterm.min.css`) and fails if any other file appears in `vendor/`.
Not done: comparison against the npm tarballs of `@xterm/xterm@5.5.0` / `@xterm/addon-fit` (no network
from this host); Alex can run `npm pack @xterm/xterm@5.5.0` on the Mac and compare `lib/xterm.js` /
`css/xterm.css` hashes with the ones in the test.

### 8. Info: imported-repo content is an untrusted prompt source
**Finding.** README and package.json text go into the planner prompt (printable-filtered, capped). A
third-party repo could try to steer the plan. The human-reviewed PR is the control.
**Fix.** The prompt now says the summary is untrusted data that describes the code and never gives
instructions. Test: `test_planner_prompt_marks_summary_untrusted`. The PR review stays the real gate.

### 9. Info: upload temp name could exceed NAME_MAX
**Fix.** The temp-file prefix is bounded in bytes (120) instead of characters. Test:
`test_upload_temp_name_fits_name_max` (a 244-byte multibyte name uploads fine).

### 10. Info: Redactor matched only raw and JSON-escaped forms
**Fix.** `Redactor` also matches the URL-encoded form and the base64 form (three alignments) of every
secret value (8+ chars), for redaction and for the pre-commit scan. Test:
`test_redactor_matches_encoded_forms`. Still not covered (documented in README "Known residual risks"):
values shorter than 8 characters, hex or custom encodings.

### 11. Info: session delete left subagent transcripts
**Fix.** `Store.delete` also removes Claude Code's `<uuid>/` subagent folder next to the transcript
(a symlink of that name is left alone, never followed). Test: `test_session_delete_removes_subagent_dir`.

## Checked and found sound (reviewer's list, kept as the record)
- Dashboard auth: Host must be 127.0.0.1, then the login cookie; every POST needs `Origin == http://Host`
  and `X-Hands: 1`; all new POST routes (`/api/new`, hide/unhide, env, sessions new/ticket/resume/stop/
  delete, files settings/mkdir/rename/delete/upload) pass the single gate in `_dispatch`; upload is
  origin-checked before streaming.
- GET data routes rely on `SameSite=Strict` + CORP/COOP/no-referrer; a cross-site fetch cannot carry
  the cookie.
- WebSocket tickets: minted only by a CSRF-checked POST, 30 s TTL, single use, bound to the login
  session (constant-time compare) and the session id, 64 outstanding at most; the upgrade checks Origin,
  the RFC handshake, ticket, record, liveness and the terminal cap before the 101.
- `ws.py`: masked frames only, RSV/opcode/control-frame checks, 64 KiB frame / 256 KiB message caps
  checked before allocation, UTF-8 validation, ping every 20 s, 1001 after 60 s idle, stalled frame = 1002,
  one write lock.
- Session spawn: `env -i` with a six-key allowlist (+ `CLAUDE_CONFIG_DIR`/`TMUX_TMPDIR` passthrough);
  argv is `claude --session-id|--resume <uuid4>` (+ the Telegram channel flag), shlex-quoted, no
  permission flags; cwd validated by realpath under home, not home, not hidden, re-validated on resume;
  tmux names `hands_s_<uuid>` matched exactly; viewers attach on a fresh PTY with close-on-exec fds;
  `killpg` targets the attach client only. Records 0600, `O_NOFOLLOW` reads, bounded title read, titles
  starting with `/` or `<` ignored.
- `files.py` paths: NUL, absolute, `..` and >255-byte names refused lexically; realpath bounds checked
  before and after strict resolution (403 before 404: no existence oracle); symlink final components
  refused for writes; link targets outside the roots refused for delete/rename; fd-based `rmtree`;
  `O_NOFOLLOW|O_EXCL` temp files and `os.replace`; uploads need Content-Length (chunked refused), 256 MB
  cap, streamed; reads capped at 2 MB; search bounded (100k entries, 5 s, 500 hits; skips links, `.git`,
  `node_modules`, denied places). Image endpoint: extension allow-list, no SVG, `nosniff`, 32 MB cap,
  RFC 5987 filenames; downloads are `application/octet-stream` attachments.
- Env API: secret values write-only; turning a secret public needs the value again; key regex, reserved
  list and `HANDS_`/`CLAUDE_` prefixes; single-line values, no NUL, 8 KiB max (no env-file injection);
  atomic 0600 writes in 0700 dirs; pending variables discarded when a create fails. Planner, coder and
  evaluator prompts carry names and visibility only.
- GitHub token: read by `create`/`destroy`/`plan`/`approve` processes only; never the dashboard,
  containers or sessions. `project.env` mode 0600 enforced; mounted read-only.
- Import flow: `parse_repo`/`validate_branch` prevent option injection into `git clone -b` and argv;
  admin permission required for the deploy key; ruleset only with `--ruleset`; destroy never deletes an
  imported repo; `codesummary` reads only the root `package.json`/README (no symlinks, 200 KB, NUL-sniffed,
  24k-char cap); host git runs with `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM`.
- CSP: no inline script or style in `index.html`; all scripts same-origin; `mock/` not served; no
  `innerHTML`/`insertAdjacentHTML`/`setAttribute("style")` in `app.js`, `sessions.js`, `files.js`; only
  CSSOM style writes; xterm's `<style>` elements diverted to constructed stylesheets (no nonce, header
  unchanged); terminal bytes go only to `term.write`; no OSC 52 / hyperlink addons.
- Deny list plus "roots are non-hidden folders under home" keeps `~/.claude.json`, `~/.netrc`,
  `~/.npmrc` and similar unreachable even through symlinks (realpath checked).

## Later (not in 0.5.0)
- In-stream redaction of container output (write through the Redactor as the session runs) would
  shrink the window further for anyone with shell access to `~/projects/<slug>/state`; the browser paths
  are closed by finding 1.
- Compare the vendored xterm files with the npm tarballs (finding 7).
