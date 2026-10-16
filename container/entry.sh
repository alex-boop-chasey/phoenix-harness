#!/bin/bash
# Container entrypoint, mounted read-only from the runner. Runs as root only long enough to:
#   1. apply the egress firewall (unless HANDS_FIREWALL=0, used with --network none)
#   2. hand the home dir and caches to the host uid
# then drops to the host uid with an EMPTY capability bounding set and no_new_privs, so nothing
# the agent runs can change the firewall or regain root (sudo is dead under no_new_privs).
set -euo pipefail

: "${HOST_UID:?HOST_UID required}" "${HOST_GID:?HOST_GID required}"
if [ "$HOST_UID" = 0 ]; then echo "hands: refusing to run the agent as root" >&2; exit 91; fi

if [ "${HANDS_FIREWALL:-1}" = 1 ]; then
  if ! /usr/local/bin/init-firewall.sh >/tmp/firewall.log 2>&1; then
    echo "hands: firewall setup failed:" >&2; tail -20 /tmp/firewall.log >&2; exit 90
  fi
fi

# give the host uid a passwd entry (node's os.userInfo(), git, ssh need one)
if ! getent passwd "$HOST_UID" >/dev/null; then
  echo "agent:x:$HOST_UID:$HOST_GID:agent:/home/node:/bin/bash" >> /etc/passwd
  getent group "$HOST_GID" >/dev/null || echo "agent:x:$HOST_GID:" >> /etc/group
fi

# -xdev: never descend into bind mounts (skills, workspace, read-only spec files)
find /home/node -xdev -exec chown -h "$HOST_UID:$HOST_GID" {} + 2>/dev/null || true
for d in /pnpm-store /pnpm-store/corepack /home/node/.cache; do
  mkdir -p "$d" 2>/dev/null || true
  if [ -d "$d" ] && [ "$(stat -c %u "$d")" != "$HOST_UID" ]; then chown "$HOST_UID:$HOST_GID" "$d"; fi
done

export HOME=/home/node
exec setpriv --reuid="$HOST_UID" --regid="$HOST_GID" --clear-groups \
  --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs -- "$@"
