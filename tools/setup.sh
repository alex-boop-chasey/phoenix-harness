#!/usr/bin/env bash
# Prepare a checkout for `bash tools/check.sh`. Idempotent; safe to run again at any time.
# The harness itself is Python stdlib only, so the only thing to install is capture/ (its own pnpm
# package), and only when it exists. Run pnpm from inside capture/ so corepack picks the version in
# its "packageManager" field (`pnpm --dir` from the root fails with ERR_PNPM_BAD_PM_VERSION).
set -euo pipefail
cd "$(dirname "$0")/.."

# The first pnpm run in a fresh container downloads pnpm itself (corepack) and its native binary from
# registry.npmjs.org; a stalled connection there must not fail the whole setup, so retry a few times.
install_capture() {
  local attempt
  for attempt in 1 2 3; do
    if (cd capture && pnpm install --frozen-lockfile); then
      return 0
    fi
    echo "capture: pnpm install failed (attempt $attempt of 3)" >&2
    [ "$attempt" -lt 3 ] && sleep 15
  done
  return 1
}

if [ -f capture/package.json ]; then
  echo "== capture: pnpm install --frozen-lockfile"
  install_capture
else
  echo "SKIP capture dependencies (no capture/package.json)"
fi
echo "setup ok"
