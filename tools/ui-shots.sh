#!/usr/bin/env bash
# UI screenshot rig: see the dashboard exactly as a browser does, with sample data, without touching
# the live dashboard, real projects or any secret.
#
# Usage (on the host, as builder):
#   bash tools/ui-shots.sh <label> [page ...]
#
#   <label>   output folder: ~/ops/shots/<label>/   (e.g. 02-before)
#   [page]    what to capture; default: every sample project plus the New-project form:
#             bookings (building)  crm (failed step)  blog (done)  wiki (stopped)
#             landing (plan ready)  notes (planning)  new (New project form)
#   Each page is saved as <page>-1440.png and <page>-390.png.
#
# How it works: one throwaway container (--rm, --network none, no firewall entrypoint needed) from the
# agent image runs everything: tools/fixtures.py builds a temporary config home + sample projects under
# /tmp, the dashboard is started from the read-only /src mount against that config home, a login code
# is minted the same way `hands-dash url` does, and tools/shots.js (Playwright Chromium from the image)
# takes the screenshots into the mounted output folder. Nothing outside the output folder is written.
set -euo pipefail

IMAGE="${UI_SHOTS_IMAGE:-hands:cc2.1.301-pw1.64.1}"
PORT=8090

# ---------------------------------------------------------------- inner mode (runs inside the container)
if [ "${1:-}" = "--inner" ]; then
  export HANDS_CONFIG_HOME=/tmp/hands-cfg
  python3 --version >/dev/null                      # the rig needs Python 3 in the image
  node --check /src/tools/shots.js
  python3 /src/tools/fixtures.py "$HANDS_CONFIG_HOME" /tmp/fixtures
  PYTHONPATH=/src python3 -m hands.dashboard run --port "$PORT" >/tmp/dash.log 2>&1 &
  DASH=$!
  python3 - <<EOF
import socket, time, sys
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", $PORT), 0.2).close(); sys.exit(0)
    except OSError:
        time.sleep(0.1)
sys.exit("dashboard did not start; see /tmp/dash.log")
EOF
  CODE="$(PYTHONPATH=/src python3 -c 'from hands.dashboard import mint_login_code; print(mint_login_code())')"
  # the image ships playwright in its npx cache, not as a global module
  NODE_PATH="$(ls -d /home/node/.npm/_npx/*/node_modules | head -1)" \
    node /src/tools/shots.js "http://127.0.0.1:$PORT" "$CODE" /out "$UI_SHOTS_PAGES"
  kill "$DASH" 2>/dev/null || true
  exit 0
fi

# ---------------------------------------------------------------- host mode
LABEL="${1:-}"
[ -n "$LABEL" ] || { echo "usage: bash tools/ui-shots.sh <label> [page ...]" >&2; exit 2; }
case "$LABEL" in *[!A-Za-z0-9._-]*) echo "label: letters, digits, . _ - only" >&2; exit 2;; esac
shift
PAGES="${*:-bookings crm blog wiki landing notes new}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HOME/ops/shots/$LABEL"
mkdir -p "$OUT"

docker run --rm --init --network none --entrypoint bash \
  --shm-size 1g \
  -e UI_SHOTS_PAGES="$PAGES" \
  --mount "type=bind,src=$REPO,dst=/src,readonly" \
  --mount "type=bind,src=$OUT,dst=/out" \
  "$IMAGE" /src/tools/ui-shots.sh --inner

echo "done: $(ls "$OUT" | wc -l) files in $OUT"
