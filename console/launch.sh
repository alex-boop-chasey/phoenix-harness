#!/usr/bin/env bash
# Two tmux sessions managed by the harness:
#   claude-local -> no --channels, free to jump around
#   claude-live  -> --channels plugin:telegram@... (persistent telegram-connected)
# Called by server.py (headless, LAUNCH_SETUP_ONLY=1) and by ttyd (per connection, attach).
set -euo pipefail

export PATH="$HOME/.bun/bin:$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin"
export TELEGRAM_STATE_DIR="$HOME/claude-harness/telegram-state"
TMUX=/home/linuxbrew/.linuxbrew/bin/tmux
APPEND_PROMPT='When a <channel source="plugin:telegram:telegram" chat_id="..."> message arrives:
1. Call the plugin reply tool with that chat_id — plain assistant text in the terminal never reaches the sender.
2. After the tool call, also emit the exact reply text as a plain assistant message in the terminal, prefixed with "→ telegram: ", so the terminal log keeps a readable record of both sides of the conversation. Do this every time, even for short replies.'

# $1 = kind (local|live), $2 = action (attach|new|load), $3 = uuid (for load)
KIND="${1:-local}"
ACTION="${2:-attach}"
UUID="${3:-}"
case "$KIND" in local) SESSION=claude-local; CHANNEL_FLAGS=() ;;
                 live)  SESSION=claude-live;  CHANNEL_FLAGS=(--channels plugin:telegram@claude-plugins-official --append-system-prompt "$APPEND_PROMPT") ;;
                 *) echo "bad kind: $KIND" >&2; exit 2 ;;
esac
COMMON_FLAGS=(--verbose)

build_cmd() {
  # Printf %q safely quotes every token for the tmux shell.
  local parts=(claude --dangerously-skip-permissions "${COMMON_FLAGS[@]}" "${CHANNEL_FLAGS[@]}")
  [ -n "$UUID" ] && parts=(claude --resume "$UUID" --dangerously-skip-permissions "${COMMON_FLAGS[@]}" "${CHANNEL_FLAGS[@]}")
  printf '%q ' "${parts[@]}"
}

start_session() {
  local cmd; cmd="$(build_cmd)"
  $TMUX kill-session -t "$SESSION" 2>/dev/null || true
  cd "$HOME"
  $TMUX new-session -d -s "$SESSION" -x 220 -y 60 "$cmd"
}

case "$ACTION" in
  new)    UUID=""; start_session ;;
  load)   [ -z "$UUID" ] && { echo "load needs uuid" >&2; exit 2; }; start_session ;;
  attach) $TMUX has-session -t "$SESSION" 2>/dev/null || start_session ;;
esac

[ "${LAUNCH_SETUP_ONLY:-}" = "1" ] && exit 0
exec $TMUX attach-session -t "$SESSION"
