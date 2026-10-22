#!/usr/bin/env bash
# Run the dashboard's JS unit tests (node --test) with the node from the agent image: the host has no node.
#   bash tools/js-test.sh            # every hands/static/*.test.mjs
# Also syntax-checks every served script, as CLAUDE.md asks (`node --check`).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${HANDS_IMAGE:-hands:cc2.1.301-pw1.64.1-dns2}"
exec docker run --rm --network none -v "$SRC:/src:ro" -w /src "$IMAGE" bash -c '
  for f in hands/static/*.js hands/static/vendor/*.js; do node --check "$f"; done
  node --test hands/static/*.test.mjs'
