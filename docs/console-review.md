# claude-harness review and Session manager architecture (step 09)

Written 2026-10-17 on branch `phoenix-0.5`. The source of truth is the imported code under
`console/`; nothing in this document was taken from the claude-harness README alone.

## 1. Import

`console/` was added with `git subtree add --prefix=console ~/claude-harness main` (commit
`0456d59`). Upstream `alex-boop-chasey/claude-harness` has a single commit (`e7a44c2`, "Initial
import of claude-harness"), so `git log -- console` shows that one commit plus the subtree merge.
Nothing else in Phoenix changed; `console/` is not imported by any Phoenix module, not installed by
`install.sh`, and not run. It is source material for steps 10–12.

Sizes: `server.py` 527 lines, `relay.js` 200, `start.sh` 77, `launch.sh` 46, `README.md` 37,
`static/` 294 KB of vendored xterm.js (`@xterm/xterm` 5.5.0, `@xterm/addon-fit`).

## 2. What each file does

| File | Role |
|---|---|
| `server.py` | aiohttp app on `127.0.0.1:9001` (env `DASH_PORT`/`DASH_BIND`). Owns two persistent PTYs ("kinds"): `local` (plain `claude`) and `live` (`claude --channels plugin:telegram@claude-plugins-official --append-system-prompt …`). Each `PtySession` spawns `claude --dangerously-skip-permissions --verbose [--resume <uuid>]` via `ptyprocess`, drains output on a thread into a 256 KB ring buffer and fans it out to every attached WebSocket (`/ws/<kind>`); input bytes go to the PTY; JSON `{"type":"resize"}` resizes it. HTTP: `/` (inline HTML+CSS+JS page with xterm.js), `/api/sessions` (scan `~/.claude/projects/*/*.jsonl`, title = first user prompt, newest 200), `/api/status?kind=` (busy = bytes in the last second), `POST /api/action` (`new`/`load` → kill and respawn the kind's PTY), `POST /api/delete` (unlink a session `.jsonl`), `/static/`. Auth: one Basic-auth credential read from `.cred` next to the file. `live` is prewarmed at startup; all PTYs are killed (HUP/TERM/KILL on the process group) at shutdown. |
| `relay.js` | Node reverse proxy on `0.0.0.0:8681` in front of the dashboard. Serves its own `/login` form (username/password from the same `.cred`), sets cookie `cha_sess` = `sha256("claude-harness-v1:" + cred)` (HttpOnly, SameSite=Lax, `Max-Age` 90 days when "remember" is ticked), redirects unauthenticated pages to `/login`, returns 401 for `/api/` and `/ws/`. Authenticated requests are forwarded to `127.0.0.1:9001` with the Basic `Authorization` header added; WebSocket upgrades are spliced as raw TCP after the same cookie check. Strips `Forwarded`/`X-Forwarded-*`/`X-Real-IP`/`Tailscale-*` headers. |
| `start.sh` | Idempotent launcher: writes a line into `~/.agent37/hooks/post-restart.sh`, creates `.venv` and pip-installs `requirements.txt`, starts `server.py` (nohup, logs in `/tmp/claude-harness/`), starts `relay.js` with `/usr/local/bin/node`, un-exposes port 8682 and exposes 8681 to the internet with `agent37 expose`, prints the public URL and the username. |
| `launch.sh` | Legacy launcher (pre-ptyprocess): manages two tmux sessions `claude-local` / `claude-live` with linuxbrew's tmux, same `claude` argv as `server.py`. Unused by the current server; worth keeping only as the reference for the tmux-based design (see §6). |
| `requirements.txt` | `ptyprocess>=0.7`, `aiohttp>=3.9`. |
| `static/` | Vendored `xterm.min.js` (5.5.0), `xterm.min.css`, `addon-fit.min.js`. No CDN at runtime. Reusable as-is. |
| `README.md`, `.gitignore` | Description and setup (`.cred`, `./start.sh`); ignores `.cred`, `.venv/`, `telegram-state/`. |

What is worth keeping for Phoenix: the PTY/ring-buffer/fan-out shape of `PtySession` (spawn,
drain thread, bounded replay buffer, resize clamp, process-group kill ladder), the session listing
idea (title from the first real user prompt in the `.jsonl`), the xterm.js client code (connect,
replay, resize, auto-reconnect, `respawn`/`exit` control messages, Ctrl-L nudge on attach), and the
vendored xterm files. Everything else (auth, relay, exposure, page, styling, `start.sh`) is replaced
by what Phoenix already has.

## 3. Security problems

Each item says what the code does, why it matters here, and what Phoenix does instead. None of
these are fixed in step 09; they are the requirements list for step 10.

1. **Both PTYs run `claude --dangerously-skip-permissions`** (`server.py` `build_argv`, also
   `launch.sh`). Every session is a fully autonomous Claude with the dashboard user's whole account:
   it can read `~/.config/hands/**`, `~/.ssh`, the GitHub PAT, push, delete projects. One prompt
   injected through Telegram or a web page the session reads would run with no confirmation.
   Phoenix's rule is that containers are the security boundary and this flag is banned on the host
   (`~/ops/CLAUDE.md`). Step 10 must never pass it; a Session manager session on the host runs with
   normal permission prompts (which is what the terminal is for), and anything autonomous belongs in
   the runner's containers.
2. **Relay listens on `0.0.0.0:8681` and `start.sh` exposes it to the internet** (`agent37 expose`).
   A single static password guards a shell-equivalent. Phoenix binds `127.0.0.1` only and is reached
   over the SSH tunnel; nothing in the merged app may listen on another address.
3. **No Origin/Host check on `/ws/<kind>` or `/api/*`**, neither in `server.py` nor in `relay.js`.
   A page in any other tab can `fetch("http://127.0.0.1:8681/api/delete")` or open
   `ws://127.0.0.1:8681/ws/local` and type into the terminal; only the `SameSite=Lax` cookie stands
   in the way, and the dashboard port 9001 itself has no cookie at all (Basic auth, which browsers
   cache and re-send automatically). DNS rebinding is not blocked either (any `Host` accepted).
   Phoenix already requires `Host` = `127.0.0.1[:port]`, and `Origin` + `X-Hands: 1` on POST; the
   WebSocket handshake must get the same Origin check (browsers always send `Origin` on it).
4. **`session_env()` drops every `CLAUDE_CODE_*` variable** plus `CLAUDECODE` and
   `CLAUDE_PARENT_SESSION_ID`, on purpose, so a `claude` started from inside a Claude session does
   not see itself as nested (that suppresses transcript saving). Side effects: the child also loses
   any legitimate `CLAUDE_CODE_*` configuration (OAuth token/config env, if the host ever uses one)
   and the nested-session guard is defeated. Phoenix must build the PTY environment explicitly
   (allowlist: `PATH`, `HOME`, `TERM`, `LANG`, `USER`, `SHELL`, `TMUX`-related, `COLORTERM` unset,
   see §5) instead of copying and scrubbing `os.environ`. `os.environ.copy()` also hands the whole
   dashboard environment to the PTY, which is exactly how a secret in the parent environment leaks.
5. **Cookie derived from the password, never expires, never rotates** (`relay.js`:
   `SESS_TOKEN = sha256("claude-harness-v1:" + cred)`). The cookie value is a pure function of
   the credential: it is the same on every login, from every browser, forever; `/logout` only clears
   the browser's copy; `remember` sets a 90-day `Max-Age`; there is no `Secure` flag; the password
   check is `===` (not constant-time) and unthrottled. Phoenix's model (random session id, in
   memory, 12 h, single-use login code minted on the VPS) replaces this entirely; the console must
   not have a login of its own.
6. **`POST /api/delete` and `GET /api/sessions` scope.** The listing walks every project directory
   under `~/.claude/projects` (every Claude Code transcript of the user, including this ops session)
   and shows the first prompt of each as its title; delete unlinks any `<uuid>.jsonl` it finds in
   any project directory with no confirmation server-side and no record. `load` resumes any uuid.
   In Phoenix the Session manager owns its own sessions (a known list, under a known directory), and
   destructive actions go through the same confirmation `<dialog>` pattern as closing a tab.
7. **Inline HTML, inline script, inline styles, `innerHTML` with interpolated data, inline
   `onclick=` handlers** (`server.py` `INDEX`). Titles are escaped for `&<>` only and then pasted
   into an `onclick` attribute string; a session title containing a quote breaks out of the
   attribute (the `del(...)` call escapes `'` but the `act(...)` calls do not escape at all; the uuid
   is validated so that one is safe). No CSP header at all. Phoenix: strict CSP, external JS only,
   `textContent` for everything an agent could have written, no inline handlers.
8. **Basic credential forwarded on every proxied request**, plaintext `.cred` file next to the code,
   logs in world-readable `/tmp/claude-harness/`, `pgrep -f` as the "already running" check.
   Phoenix keeps secrets in `~/.config/hands/` (0600) and has a lock-based single-instance check.
9. **No limits on WebSocket clients or on the PTY input**: any number of attached viewers; the 4 MB
   `max_msg_size` is the only bound; `resize` is clamped but a client can flood the PTY. Phoenix's
   dashboard is bounded (`MAX_CONN` semaphore, body limits) and the WebSocket path needs its own
   caps (clients per session, frame size, input rate).
10. **Zombie risk on crash**: PTY children are killed only on a clean `on_cleanup`; if `server.py`
    dies, the `claude` processes stay orphaned with no way to re-attach (the PTY master is gone).
    With tmux-backed sessions (§6) a dashboard restart or `bash install.sh` keeps every session.

## 4. Agent37-specific paths

claude-harness was written for an Agent37 container (user `node`, Homebrew on Linux, `agent37` CLI
for port exposure). None of these exist on this VPS (user `builder`, Ubuntu, no node on the host):

| Where | Agent37 assumption | Phoenix equivalent |
|---|---|---|
| `relay.js` `RELAY_CRED_FILE` default | `/home/node/claude-harness/.cred` | no relay; secrets in `~/.config/hands/` |
| `server.py` `session_env()`, `start.sh`, `launch.sh` `PATH` | `$HOME/.bun/bin`, `/home/linuxbrew/.linuxbrew/bin` | host `claude` at `~/.local/bin` (whatever `hands` already uses) |
| `launch.sh` | `TMUX=/home/linuxbrew/.linuxbrew/bin/tmux` | `/usr/bin/tmux` (already used by `hands` and `hands-dash`) |
| `start.sh` `start_relay` | `/usr/local/bin/node` | no node on the host (status.json step 01); nothing in the merged app may need it |
| `start.sh` `install_hook` | `~/.agent37/hooks/post-restart.sh` | systemd/cron are Alex's; the watchdog cron already restarts the dashboard |
| `start.sh` `expose_once`/`unexpose` | `agent37 expose|unexpose|list-exposed` | none: 127.0.0.1 + SSH tunnel |
| `server.py`, `launch.sh` | `TELEGRAM_STATE_DIR=~/claude-harness/telegram-state` for the `plugin:telegram` channel | step 12 decides where the Telegram plugin keeps state (under `~/.config/hands/`); Phoenix's own notifier (`hands/notify.py`) uses `TG_TOKEN`/`TG_CHAT_ID` from the config home |
| `server.py` `CHANNEL_FLAGS["live"]` | `--channels plugin:telegram@claude-plugins-official` + `--append-system-prompt` | step 12 |
| `server.py` `PROJECTS` | `~/.claude/projects` of user `node` | the same directory of `builder`, but scoped (§3.6) |
| `start.sh` logs | `/tmp/claude-harness/` | `~/.local/state/hands/` like the watchdog |

## 5. xterm.js under the dashboard CSP: tested

Question: does `@xterm/xterm` 5.5.0 (the vendored build) work under the dashboard's exact headers,
`Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; …` (no inline
styles, no style attributes)?

How it was tested: two static probe pages in `hands/static/mock/` (`xterm.html`,
`xterm-nonce.html`, driver `xterm-test.js`), served by the rig's mock server with the dashboard's
headers, Chromium from the agent image, light/dark × 1440/390. `tools/mockserve.py` learned two
small things for this: a `/vendor=<dir>` mount (serves `console/static/` under `/vendor/`) and a
`__NONCE__` marker in an `.html` file that is replaced by a fresh random nonce which is also added
to `style-src` for that response. Run: `bash tools/ui-shots.sh --mock 09/csp xterm xterm-nonce`;
screenshots in `~/ops/shots/09/csp/`. The probe prints its findings into the page (`#result`) and
the rig echoes them.

Result:

| Page | CSP | `<style>` elements xterm injected that took effect | Console |
|---|---|---|---|
| `xterm.html` | `style-src 'self'` (today's) | **0 of 2** | 4 `style-src` violations per load for the two `<style>` elements (the DOM renderer's dimensions sheet and its theme sheet, each written twice), plus 2 for the truecolor cell (style attribute) |
| `xterm-nonce.html` | `style-src 'self' 'nonce-<per response>'` | **2 of 2** | only the truecolor cell's style-attribute violation remains |

What it looks like (`~/ops/shots/09/csp/`): under today's CSP the text is there but **no colours**
(the ANSI palette classes are defined in the blocked theme sheet), the cell height rule is missing
and xterm's font option is ignored (the probe page's own `.term-body` font hides that; a bare
container would fall back to Courier). With the nonce: palette colours, bold, underline, correct
cell metrics, identical in Safari terms (nothing non-standard is used).

Why: xterm 5.5.0's DOM renderer creates `<style>` elements with `document.createElement("style")`
and fills them with `textContent` (dimensions and theme; upstream also added a third one for the
scrollbar in later versions). Inline `<style>` content is governed by `style-src`; without
`'unsafe-inline'`, a hash (impossible, the content is dynamic) or a nonce it is blocked. This is
upstream issue [xtermjs/xterm.js#4445](https://github.com/xtermjs/xterm.js/issues/4445), open
since 2023; xterm has no nonce option (the bundle contains no `nonce` at all).

Mitigation that works and does not weaken the CSP (tested above): the Session manager page is
served with a per-response random nonce in `style-src` (keeping `'self'`, no `'unsafe-inline'`),
and the terminal is created with xterm's supported `documentOverride` option set to a `Proxy` of
`document` whose `createElement("style")` stamps that nonce on the element (`xterm-test.js`, 12
lines). Everything else in the app keeps the unchanged CSP. The nonce is per response and never
reused, so it adds no attack surface: an injected `<style>` without the nonce is still blocked.
Notes for step 11:

- Only the Session manager page needs the nonce; the dashboard builds `SECURITY_HEADERS` per
  request for that one route (today the headers are a constant dict).
- **Truecolor cells** (24-bit `\x1b[38;2;…m`) are rendered by xterm with a `style="color:#…"`
  attribute, which no nonce can allow (attributes need `'unsafe-inline'` or `'unsafe-hashes'`).
  They show in the default foreground instead. Claude Code picks 24-bit colour only when
  `COLORTERM=truecolor`; with `TERM=xterm-256color` and `COLORTERM` unset it uses the 256-colour
  palette, which xterm renders with classes. The PTY environment must therefore not set
  `COLORTERM`. `minimumContrastRatio` must stay at its default 1 for the same reason.
- Alternatives considered: `'unsafe-inline'` in `style-src` for the whole app (weakens the CSP,
  rejected), patching the vendored xterm to use `adoptedStyleSheets` (not subject to `style-src`,
  but a local patch of a 290 KB minified file to re-do at every upgrade), or waiting for upstream
  nonce support (no sign of it).

## 6. Decision for Alex: how the Session manager runs

Constraints: the dashboard is Python 3.12+ stdlib (`ThreadingHTTPServer`, 868 lines, exercised
by the dashboard tests in `tests/test_v04.py` … `test_v08.py`); terminals need a WebSocket per viewer and a PTY per session; the host has Python
3.14.4, **no pip** (`python3 -m pip` is missing), no node, tmux at `/usr/bin/tmux`; `python3-aiohttp`
3.13.3 and `python3-websockets` 15.0.1 exist in the Ubuntu archive but are not installed (root,
`apt install`). `bash install.sh` restarts the dashboard, and the watchdog cron restarts it if it
dies, so whatever owns the terminals must survive a dashboard restart or Alex loses every session on
every install.

### Options

| | A: move the dashboard onto aiohttp | B: keep the console as a second process (aiohttp) behind the dashboard | C: stdlib WebSocket in the existing dashboard, sessions in tmux |
|---|---|---|---|
| New dependencies | `aiohttp` (+ `ptyprocess`): root `apt install`, and the "stdlib only" promise in both CLAUDE.md files ends | same packages, same `apt install`, plus a venv or system Python for the second service | none |
| Code to write | rewrite every handler, the session store, the CSRF/Host checks and all dashboard tests for aiohttp (~900 lines touched), then port `PtySession` | a trimmed `server.py` (~300 lines) + in the dashboard a WebSocket **splice** proxy (hijack the socket after the upgrade, copy bytes both ways) + a trust channel so the console accepts the dashboard's session: either the dashboard proxies everything and the console checks a per-boot secret, or the console calls back into the dashboard | a server-side RFC 6455 implementation (handshake, masked frames, ping/pong, close; no extensions) ≈ 200 lines + tests, a `PtySession` port on `os.openpty` + `subprocess` (≈ 150 lines), tmux helpers (≈ 60) |
| Processes to supervise | 1 | 2 (watchdog, locks, logs, `install.sh` for both; a dead console = dead terminals but a live dashboard) | 1 |
| Same origin / login / CSP | yes, by construction | yes if proxied through the dashboard; the splice must re-do the Origin and cookie checks (it does, it is the dashboard) | yes, by construction: `_authed()`, `_host_ok()`, Origin check and headers apply before the upgrade |
| Session persistence across dashboard restart | no (PTYs die with the process) unless tmux is added anyway | yes if the console is not restarted by `install.sh`, otherwise no | yes: `claude` runs inside `tmux new-session -d -s claude_<name>`; the WebSocket attaches a PTY running `tmux attach -t …`; the dashboard can die, be installed, or be restarted and the session continues. Same mechanism `hands <p> start` and `hands-dash start` already use |
| Threads/concurrency model | event loop; `ptyprocess` reads still need executor threads | event loop in the console, threads in the dashboard | one thread per attached viewer (bounded, e.g. 8 terminals × 2 viewers), one reader thread per live PTY; `select` on the PTY fd |
| Risk | big-bang rewrite of the control plane right before 0.5.0; aiohttp on Python 3.14 is new | two auth paths to keep consistent; the hand-off (shared session check) is bespoke either way | own WebSocket code: must be fail-closed on bad frames, size-capped, tested with a raw client; no permessage-deflate (not needed, PTY output is small) |
| Removes from claude-harness | relay, login, cookie, exposure | relay, login, cookie, exposure | relay, login, cookie, exposure, aiohttp, ptyprocess |

### Recommendation: **C**

Keep one stdlib process and put the WebSocket into the dashboard; run each Claude session inside a
tmux session so it outlives the dashboard. Reasons, in order:

1. **Nothing new to install, nothing to supervise**; the stdlib-only rule and the "one process on
   127.0.0.1" security model stay exactly as documented, and every existing check (Host, cookie,
   Origin, CSP, bounded connections) applies to the terminal path for free.
2. **Sessions survive `bash install.sh`**, which A and B only get by adding tmux anyway. tmux is
   already the harness's process manager; `launch.sh` in `console/` is the sketch of this design.
3. **The WebSocket server side is small and well-specified** (RFC 6455 without extensions:
   `Sec-WebSocket-Accept`, 2–10 byte frame headers, client-to-server masking, opcodes 1/2/8/9/10,
   fragmentation can be refused). It is comparable in size to what step 06 added, and it is the
   kind of code the test suite can pin down byte by byte.
4. B pays most of C's cost (socket hijack + splice) and adds the dependency, the second process and
   a bespoke trust hand-off; A pays a rewrite of working, tested code for a feature that touches one
   page.

Known trade-offs of C to design around in step 10/11: tmux owns the scrollback (xterm's scrollback
shows only what tmux repaints; mouse wheel can be mapped to tmux copy-mode or the status bar hidden
with `set -g status off`), one thread per viewer (bounded), and the `tmux attach` PTY has to be
resized together with the browser (`tmux resize-window` / `setwinsize` on the attach PTY both work).

### Decision

**C, chosen by Alex on 2026-10-17**, with four conditions for step 10:

1. Origin, Host, session (cookie) and CSRF (X-Hands) checks run before the WebSocket upgrade is
   accepted; anything failing is rejected with a plain HTTP error, never upgraded.
2. The WebSocket server rejects unmasked client frames, caps frame and message size, handles
   fragmentation, ping/pong and close correctly, and times out idle or half-open connections.
3. The number of open terminal connections is capped.
4. Each of these has its own unit test.
