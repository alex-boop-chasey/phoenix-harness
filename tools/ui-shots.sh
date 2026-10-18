#!/usr/bin/env bash
# UI screenshot rig: see the dashboard exactly as a browser does, with sample data, without touching
# the live dashboard, real projects or any secret.
#
# Usage (on the host, as builder):
#   bash tools/ui-shots.sh <label> [page ...]
#
#   <label>   output folder: ~/ops/shots/<label>/   (e.g. 02-before, or a subfolder like 05/p20)
#   [page]    what to capture; default: every sample project plus the New-project form:
#             bookings (building)  crm (failed step)  blog (done)  wiki (stopped)
#             landing (plan ready)  notes (planning)  new (New project form)
#             sessions, files (sections), nav (phone drawer open)
#             tabs (builder as it opens), menu ("+" menu open), close (close-tab confirmation)
#             newrepo (New project form: start from an existing repo), newbad (New project form with
#             invalid variables), env (blog's Environment panel),
#             envbad (that panel with validation errors)
#             sessions (Session manager: list + live terminal; phone: the list), sessions-term (phone:
#             the terminal view), sessions-new (New session dialog), sessions-delete (Delete dialog),
#             sessions-telegram (Leave/Go live on Telegram dialog; the live session is on Telegram)
#             files (File browser list), files-grid, files-preview (text), files-image, files-search,
#             files-upload (upload list + drop zone), files-settings (roots dialog), files-denied (refusal)
#   Also writes compare-<theme>-<width>.png: Project builder, Session manager and File browser side by side.
#   Each page is saved as <page>-light-1440.png, <page>-dark-390.png, ...
#
#   Environment: UI_SHOTS_PROJECTS=N   how many sample projects (0..20, default 6; see fixtures.py)
#                UI_SHOTS_HIDDEN="a b"  sample projects that start as closed tabs
#
#   bash tools/ui-shots.sh --mock <label> [page ...]
#             captures the static design mock in hands/static/mock/ instead of the dashboard, in
#             light and dark: <page>-light-1440.png, <page>-dark-390.png, ...  Default pages:
#             builder sessions files files-grid. Served with the dashboard's security headers; any
#             browser console error (e.g. a CSP violation) fails the run.
#
# How it works: one throwaway container (--rm, --network none, no firewall entrypoint needed) from the
# agent image runs everything: tools/fixtures.py builds a temporary config home + sample projects under
# /tmp, the dashboard is started from the read-only /src mount against that config home, a login code
# is minted the same way `hands-dash url` does, and tools/shots.js (Playwright Chromium from the image)
# takes the screenshots into the mounted output folder. Nothing outside the output folder is written.
set -euo pipefail

IMAGE="${UI_SHOTS_IMAGE:-hands:cc2.1.301-pw1.64.1-dns2}"
PORT=8090

# ---------------------------------------------------------------- inner mode (runs inside the container)
if [ "${1:-}" = "--inner" ]; then
  export HANDS_CONFIG_HOME=/tmp/hands-cfg
  python3 --version >/dev/null                      # the rig needs Python 3 in the image
  node --check /src/tools/shots.js
  node --check /src/hands/static/app.js                # the host has no node; this is the JS syntax check
  node --check /src/hands/static/sessions.js
  node --check /src/hands/static/files.js
  NODE_PATH="$(ls -d /home/node/.npm/_npx/*/node_modules | head -1)"   # playwright lives in the npx cache
  if [ "${UI_SHOTS_MODE:-}" = "mock" ]; then
    PYTHONPATH=/src python3 /src/tools/mockserve.py /src/hands/static/mock "$PORT" /vendor=/src/console/static >/tmp/dash.log 2>&1 &
  else
    # shellcheck disable=SC2086  # UI_SHOTS_HIDDEN is a space-separated list on purpose
    python3 /src/tools/fixtures.py "$HANDS_CONFIG_HOME" /tmp/fixtures "${UI_SHOTS_PROJECTS:-}" ${UI_SHOTS_HIDDEN:-}
    # rigdash = the real dashboard with a stand-in session backend (no tmux in the image) + fake claude
    CLAUDE_CONFIG_DIR=/tmp/claude-home PYTHONPATH=/src python3 /src/tools/rigdash.py run --port "$PORT" >/tmp/dash.log 2>&1 &
  fi
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
  if [ "${UI_SHOTS_MODE:-}" = "mock" ]; then
    NODE_PATH="$NODE_PATH" node /src/tools/shots.js --mock "http://127.0.0.1:$PORT" /out "$UI_SHOTS_PAGES"
  else
    CODE="$(PYTHONPATH=/src python3 -c 'from hands.dashboard import mint_login_code; print(mint_login_code())')"
    NODE_PATH="$NODE_PATH" node /src/tools/shots.js "http://127.0.0.1:$PORT" "$CODE" /out "$UI_SHOTS_PAGES" || { echo "--- dashboard log:"; grep -n "dashboard error\|Error:" /tmp/dash.log | head -20; tail -12 /tmp/dash.log; exit 1; }
  fi
  kill "$DASH" 2>/dev/null || true
  exit 0
fi

# ---------------------------------------------------------------- host mode
MODE=dash
if [ "${1:-}" = "--mock" ]; then MODE=mock; shift; fi
LABEL="${1:-}"
[ -n "$LABEL" ] || { echo "usage: bash tools/ui-shots.sh <label> [page ...]" >&2; exit 2; }
case "$LABEL" in *[!A-Za-z0-9._/-]*|*..*|/*) echo "label: letters, digits, . _ - and / (no .., not absolute) only" >&2; exit 2;; esac
shift
if [ "$MODE" = mock ]; then PAGES="${*:-builder sessions files files-grid}"; else PAGES="${*:-bookings crm blog wiki landing notes new newrepo newbad env envbad sessions sessions-term sessions-new sessions-delete sessions-telegram files files-grid files-preview files-image files-search files-upload files-settings files-denied nav tabs menu close}"; fi
case "${UI_SHOTS_PROJECTS:-}" in ""|[0-9]|1[0-9]|20) ;; *) echo "UI_SHOTS_PROJECTS: 0..20" >&2; exit 2;; esac
case "${UI_SHOTS_HIDDEN:-}" in *[!A-Za-z0-9_\ -]*) echo "UI_SHOTS_HIDDEN: project names separated by spaces" >&2; exit 2;; esac
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HOME/ops/shots/$LABEL"
mkdir -p "$OUT"

docker run --rm --init --network none --entrypoint bash \
  --shm-size 1g \
  -e UI_SHOTS_PAGES="$PAGES" -e UI_SHOTS_MODE="$MODE" -e UI_SHOTS_DEBUG="${UI_SHOTS_DEBUG:-}" \
  -e UI_SHOTS_PROJECTS="${UI_SHOTS_PROJECTS:-}" -e UI_SHOTS_HIDDEN="${UI_SHOTS_HIDDEN:-}" \
  --mount "type=bind,src=$REPO,dst=/src,readonly" \
  --mount "type=bind,src=$OUT,dst=/out" \
  "$IMAGE" /src/tools/ui-shots.sh --inner

echo "done: $(ls "$OUT" | wc -l) files in $OUT"
