#!/usr/bin/env bash
# Creates a self-contained smoke project: local bare "origin" repos, runner clones, config.
# Usage: ~/runner/examples/smoke/setup.sh   (safe to re-run: wipes ~/smoke)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
S="$HOME/smoke"
IMAGE="${1:-$(docker images hands --format '{{.Tag}}' | grep -v latest | head -1)}"
[ -n "$IMAGE" ] || { echo "no pinned hands image found"; exit 1; }
rm -rf "$S"; mkdir -p "$S"
git init -q --bare -b main "$S/specs.git"; git init -q --bare -b main "$S/app.git"
git clone -q "$S/specs.git" "$S/specs-src"
cp -r "$HERE/spec/." "$S/specs-src/"
git -C "$S/specs-src" add -A; git -C "$S/specs-src" commit -qm "smoke spec"; git -C "$S/specs-src" push -q origin main
git clone -q "$S/app.git" "$S/app-src"
printf 'node_modules/\n/acceptance/\n' > "$S/app-src/.gitignore"
git -C "$S/app-src" add -A; git -C "$S/app-src" commit -qm "init"; git -C "$S/app-src" push -q origin main
rm -rf "$S/specs-src" "$S/app-src"
git clone -q "$S/specs.git" "$S/specs"; git clone -q "$S/app.git" "$S/app"
mkdir -p "$HOME/.config/hands/projects"
cat > "$HOME/.config/hands/projects/smoke.toml" <<TOML
spec_dir = "$S/specs"
app_dir = "$S/app"
state_dir = "$S/state"
image = "hands:$IMAGE"
max_sessions = 12
TOML
echo "smoke project ready: hands smoke check && hands smoke run"
