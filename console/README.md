# claude-harness

Minimal persistent web harness for Claude Code with the Telegram channel attached. Hermes-style: no tmux, no ttyd — a single Python dashboard owns the PTYs via `ptyprocess`, streams them over WebSocket to xterm.js in the browser.

## What it does

- Left sidebar lists every session in `~/.claude/projects/*/*.jsonl` with its first prompt as a title.
- Right pane has two tabs — **Local** (free-roaming claude) and **Live (Telegram)** (claude launched with `--channels plugin:telegram@…`) — each backed by its own long-running PTY.
- `→Live` on a row promotes that session (runs `claude --resume <uuid>` under the live PTY). `New (local)` starts a fresh local session. `Restart Live` recycles the Telegram-connected one. `✕` deletes a session's `.jsonl`.
- Live is prewarmed on boot so Telegram keeps answering without a browser.

## Files

- `server.py` — aiohttp dashboard. Owns both PTYs via `ptyprocess`, drains output into a bounded ringbuffer, fans out over `/ws/<kind>`. Builds the `claude` argv (incl. `--channels` for live), scrubs `CLAUDE_CODE_CHILD_SESSION` from the inherited env, and reaps all children on shutdown.
- `relay.js` — single-port Node reverse proxy with cookie login (`/login`) in front of the dashboard. HTTP and WebSocket upgrades both route to the dashboard on loopback.
- `start.sh` — idempotent launcher: creates `.venv`, installs `requirements.txt`, starts dashboard + relay, exposes the public port via `agent37 expose`, installs a `post-restart.sh` hook so it comes back after a container restart.
- `requirements.txt` — `ptyprocess`, `aiohttp`.
- `static/` — vendored `@xterm/xterm` + `@xterm/addon-fit` (no CDN at runtime).
- `launch.sh` — legacy tmux launcher, left in place but unused by the current server.

## Setup

```bash
# 1. Credentials for Basic auth (used by both the relay login and the dashboard)
echo 'user:password' > .cred && chmod 600 .cred

# 2. Boot
./start.sh
```

The script prints the public URL and the username. The password lives in `.cred` — never printed to logs.

## Dependencies

- Python 3.11+
- Node (for `relay.js`)
- `agent37` CLI (for `expose` / `list-exposed`); drop that section of `start.sh` if you're binding the relay directly.
