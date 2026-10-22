#!/usr/bin/env bash
# The whole check suite, in order; exits non-zero at the first failing stage.
#   1. python3 -m compileall over hands, console and tools
#   2. the new unittest suite in tests/next (always)
#   3. the legacy suite under pytest, only when pytest is installed
#   4. node --test over hands/static/ and, when present, capture/ and hooks/
# Run from anywhere; everything happens relative to the repo root (`python3 -m` puts it on sys.path).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== compileall"
python3 -m compileall -q hands console tools

echo "== unit tests (tests/next)"
python3 -m unittest discover -s tests/next -p 'test_*.py'

echo "== legacy pytest"
if python3 -c 'import pytest' 2>/dev/null; then
  python3 -m pytest -q tests
else
  echo "SKIP legacy pytest suite (pytest not installed)"
fi

echo "== js tests"
# `node --test <dir>/` is not a valid pattern for every Node release, so the *.test.mjs files are
# listed explicitly (never from node_modules); a directory without any is reported, not failed.
js_tests() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    echo "SKIP js tests in $dir (directory absent)"
    return 0
  fi
  local files=()
  while IFS= read -r -d '' f; do files+=("$f"); done < <(
    find "$dir" -path '*/node_modules' -prune -o -name '*.test.mjs' -type f -print0 | sort -z)
  if [ "${#files[@]}" -eq 0 ]; then
    echo "SKIP js tests in $dir (no *.test.mjs files)"
    return 0
  fi
  echo "-- node --test ${files[*]}"
  node --test "${files[@]}"
}
js_tests hands/static
if [ -d capture ]; then
  js_tests capture
  js_tests hooks
else
  echo "SKIP js tests in capture/ and hooks/ (no capture/ yet)"
fi
echo "check ok"
