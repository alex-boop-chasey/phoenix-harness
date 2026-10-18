# Sessions without tmux: how claude-harness keeps a session alive, and what Phoenix copied (step 17)

Read for step 17 item 6: `~/claude-harness/server.py` (527 lines, aiohttp + ptyprocess). Requirement 1
says Phoenix must use the same model, with Python stdlib only (Alex, 2026-10-18).

## What claude-harness does

- **One long-lived PTY process per session kind** (`PtySession`): `ptyprocess.PtyProcess.spawn(argv,
  cwd=HOME, env, dimensions)` is started once and is owned by the server, not by any browser connection.
- **A drain task** (`_drain`) is the only reader of the PTY. Every chunk goes into a bounded
  `RingBuffer` (256 kB) and is then sent to every attached WebSocket in `self.clients` (a set). A client
  that fails to receive is simply discarded from the set.
- **Attach = add to the set, then replay** (`h_ws`): the new WebSocket first receives
  `buffer.snapshot()` (the scrollback), then `\x0c` (Ctrl-L) is written to the PTY so the TUI repaints
  for the new viewer, then it receives live chunks like everyone else. Input frames are written to the
  PTY, `resize` messages call `setwinsize`.
- **Detach = remove from the set** (`finally: sess.clients.discard(ws)`). The process is untouched, so
  closing the browser changes nothing for Claude; reopening attaches again and gets the scrollback back.
- **Exit**: when the PTY reaches EOF the drain task sends `{"type": "exit"}` to the viewers.
- **Server restart**: the processes are children of the server, so they die with it; `on_startup`
  prewarms the live session and `claude --resume <uuid>` (`act('load')`) brings a conversation back.
- Things Phoenix deliberately does **not** copy: `--dangerously-skip-permissions`, the copied server
  environment (`os.environ.copy()` minus a few keys), Basic auth, the one-session-per-kind limit, the
  inline `<style>`/`onclick` page.

## What Phoenix does (hands/sessions.py)

Same shape, stdlib only: `Proc` = `os.openpty()` + `os.posix_spawn("/bin/sh", ["sh", "-c", 'cd -- "$1"
&& shift && exec "$@"', ..., cwd, *claude_argv], env, setsid=True, file_actions=[open slave as fd 0,
dup2 1, dup2 2])` (controlling terminal, no fork in the threaded server, the allowlisted env is the
whole environment). A drain **thread** per session reads the master into a 256 kB ring buffer and fans
out to `Viewer`s; exit is detected with `waitpid(WNOHANG)` every 0.5 s (the slave stays open on our
side, so EIO is not relied on). `Viewer.run(ws)` attaches (replay first), forwards input and resizes;
its first resize is forced (one column smaller, then back) so the TUI repaints for the new viewer
instead of Ctrl-L, which Claude Code treats as "clear screen". Detaching leaves the process alone.
`PtyBackend.stop` sends SIGHUP, then SIGTERM, then SIGKILL to the process group; `shutdown()` runs when
the dashboard exits. After a dashboard restart every record is stopped and offered for Resume
(`claude --resume <uuid>`). Kept from step 10: the env allowlist, Origin/Host/cookie checks before the
upgrade, single-use tickets, the 8-terminal cap, frame/message/idle limits.
