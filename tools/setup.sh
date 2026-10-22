#!/usr/bin/env bash
# Prepare a checkout for `bash tools/check.sh`. Idempotent; safe to run again at any time.
# The harness itself is Python stdlib only, so the only thing to install is capture/ (its own pnpm
# package), and only when it exists. Run pnpm from inside capture/ so corepack picks the version in
# its "packageManager" field (`pnpm --dir` from the root fails with ERR_PNPM_BAD_PM_VERSION).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f capture/package.json ]; then
  echo "== capture: pnpm install --frozen-lockfile"
  (cd capture && pnpm install --frozen-lockfile)
else
  echo "SKIP capture dependencies (no capture/package.json)"
fi
echo "setup ok"
