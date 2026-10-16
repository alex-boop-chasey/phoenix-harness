#!/usr/bin/env bash
# Install/upgrade the runner for user `builder`. Run from the extracted folder:  bash install.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/runner"

[ "$(id -u)" != 0 ] || { echo "run as builder, not root"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || { echo "need python3 >= 3.12"; exit 1; }
for b in git tmux docker; do command -v $b >/dev/null || { echo "missing: $b"; exit 1; }; done
docker info >/dev/null 2>&1 || { echo "builder cannot reach docker (docker group?)"; exit 1; }

if [ "$SRC" != "$DEST" ]; then
  mkdir -p "$DEST"
  rm -rf "$DEST/hands" "$DEST/prompts" "$DEST/planner-skills" "$DEST/container" "$DEST/tests" "$DEST/examples"
  cp -r "$SRC"/hands "$SRC"/prompts "$SRC"/planner-skills "$SRC"/container "$SRC"/tests "$SRC"/examples "$SRC"/README.md "$SRC"/VERSION "$DEST"/
fi
chmod 755 "$DEST/container/entry.sh" "$DEST/tests/fake_docker.py" "$DEST/examples/smoke/setup.sh"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +

mkdir -p "$HOME/.local/bin" "$HOME/.config/hands/projects"
cat > "$HOME/.local/bin/hands" <<EOF
#!/bin/sh
PYTHONPATH="$DEST" exec python3 -m hands "\$@"
EOF
chmod 755 "$HOME/.local/bin/hands"
cat > "$HOME/.local/bin/hands-dash" <<EOF
#!/bin/sh
PYTHONPATH="$DEST" exec python3 -m hands.dashboard "\$@"
EOF
chmod 755 "$HOME/.local/bin/hands-dash"

echo "== self-test (fake docker, no Claude usage)"
( cd "$DEST" && python3 -m unittest discover -s tests 2>&1 | tail -3 )

IMG_TAG="$(sed -n 's/^CLAUDE_CODE_VERSION=//p' ~/hands/versions.env 2>/dev/null)"
PW_TAG="$(sed -n 's/^PLAYWRIGHT_VERSION=//p' ~/hands/versions.env 2>/dev/null)"
CFG="$HOME/.config/hands/projects/ra.toml"
if [ ! -f "$CFG" ] && [ -n "$IMG_TAG" ]; then
  cat > "$CFG" <<EOF
spec_dir = "~/ra-specs"
app_dir = "~/ra-app"
state_dir = "~/ra-state"
image = "hands:cc$IMG_TAG-pw$PW_TAG"
coder_model = "claude-fable-5-1"
eval_model = "claude-fable-5-1"
fallback_model = "claude-opus-5-5"
max_sessions = 60
EOF
  echo "wrote $CFG"
fi
NP="$HOME/.config/hands/new-project.toml"
if [ ! -f "$NP" ] && [ -n "$IMG_TAG" ]; then
  cat > "$NP" <<EOF
# defaults for: hands <slug> create  (and the dashboard's New project form)
org = "alex-boop-chasey"          # GitHub org that owns the repos
codeowner = "rebirth-wd"          # must approve spec PRs
image = "hands:cc$IMG_TAG-pw$PW_TAG"
coder_model = "claude-fable-5-1"
eval_model = "claude-fable-5-1"
fallback_model = "claude-opus-5-5"
max_sessions = 60
EOF
  echo "wrote $NP"
fi

echo "== watchdog cron (every 10 min: restarts dead runners, keeps the dashboard up)"
mkdir -p "$HOME/.local/state/hands"
case "$PATH$HOME" in *%*|*\'*) echo "PATH or HOME contains % or a quote; set up the cron line by hand"; exit 1;; esac
CRON_LINE="*/10 * * * * PATH='$PATH' $HOME/.local/bin/hands all watchdog >> $HOME/.local/state/hands/watchdog.log 2>&1"
if command -v crontab >/dev/null; then
  ( crontab -l 2>/dev/null | grep -vF 'hands all watchdog' || true; echo "$CRON_LINE" ) | crontab -
  crontab -l | grep -F 'hands all watchdog'
else
  echo "WARN: crontab not found; add this line by hand: $CRON_LINE"
fi

echo "== dashboard (127.0.0.1:8090)"
"$HOME/.local/bin/hands-dash" stop >/dev/null 2>&1 || true; sleep 1
"$HOME/.local/bin/hands-dash" start

echo "installed runner $(cat "$DEST/VERSION") at $DEST; command: hands (open a new shell if 'hands' is not found)"
