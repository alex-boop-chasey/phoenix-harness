#!/usr/bin/env bash
# Idempotent single-port harness, tmux-free.
#   /                 -> python dashboard  (loopback 9001; owns PTYs via ptyprocess)
#   /ws/<kind>        -> xterm.js WebSocket on the same dashboard port
# Node relay fronts a single public port with cookie auth; everything behind
# it forwards to the dashboard.
set -euo pipefail

export PATH="$HOME/.bun/bin:$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin"
HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLIC_PORT=8681
DASH_PORT=9001
LOG=/tmp/claude-harness
VENV="$HERE/.venv"
VENV_PY="$VENV/bin/python"
mkdir -p "$LOG"
[ -r "$HERE/.cred" ] || { echo "no .cred" >&2; exit 1; }

install_hook() {
  local hook="$HOME/.agent37/hooks/post-restart.sh"
  local line="$HERE/start.sh >>$LOG/boot.log 2>&1 &"
  mkdir -p "$(dirname "$hook")"
  [ -f "$hook" ] || { printf '#!/usr/bin/env bash\n' > "$hook"; chmod +x "$hook"; }
  grep -qF "$line" "$hook" || printf '%s\n' "$line" >> "$hook"
}

ensure_venv() {
  if [ ! -x "$VENV_PY" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
  fi
}

start_dash() {
  if pgrep -f "$VENV_PY $HERE/server.py" >/dev/null; then echo "dash:$DASH_PORT already up"; return; fi
  cd "$HERE"
  DASH_PORT=$DASH_PORT DASH_BIND="127.0.0.1" \
    nohup "$VENV_PY" server.py >>"$LOG/dash.log" 2>&1 &
  disown || true
  echo "dash on 127.0.0.1:$DASH_PORT (ptyprocess + aiohttp; prewarms live)"
}

start_relay() {
  if pgrep -f "node $HERE/relay.js" >/dev/null; then echo "relay:$PUBLIC_PORT already up"; return; fi
  RELAY_LISTEN_PORT=$PUBLIC_PORT RELAY_DASH_PORT=$DASH_PORT \
  RELAY_CRED_FILE="$HERE/.cred" \
    nohup /usr/local/bin/node "$HERE/relay.js" >>"$LOG/relay.log" 2>&1 &
  disown || true
  echo "relay on 0.0.0.0:$PUBLIC_PORT"
}

expose_once() {
  local port="$1" label="$2" varname="$3"
  local url
  url=$(agent37 list-exposed 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); e=[x for x in d.get('exposedPorts',[]) if x['port']==$port]; print(e[0]['url'] if e else '')")
  [ -z "$url" ] && url=$(agent37 expose "$port" --label "$label" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('url',''))")
  eval "$varname=\$url"
}

unexpose() {
  local port="$1"
  agent37 list-exposed 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(e['port']==$port for e in d.get('exposedPorts',[])) else 1)" \
    && agent37 unexpose "$port" >/dev/null 2>&1 || true
}

install_hook
ensure_venv
start_dash
start_relay

sleep 1
unexpose 8682
expose_once "$PUBLIC_PORT" claude-harness URL

echo "---"
echo "URL:  $URL"
echo "User: $(cut -d: -f1 "$HERE/.cred")  (password in $HERE/.cred)"
