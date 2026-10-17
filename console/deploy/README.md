# console/deploy: running the dashboard under systemd (prepared, not installed)

Today the dashboard runs in tmux session `hands_dashboard`, started by `bash install.sh` and kept
alive by the watchdog cron (`hands all watchdog` → `dashboard.ensure_running()`). That stays the
supported way until Alex decides otherwise. `hands-dashboard.service` is the alternative for a later
step: one system unit (not a user unit, so no lingering/session dependencies) running
`hands-dash run --port 8090` as `builder`.

Why it is safe to switch at any time:

- `hands-dash run` holds `~/.config/hands/dashboard.lock`; `ensure_running()` and `hands-dash start`
  see the lock and do nothing, so the cron and systemd never fight.
- Claude sessions (`hands_s_<uuid>` tmux sessions) are started by the dashboard but belong to tmux's
  server, not to the service: `KillMode=process` makes `systemctl restart` stop only the dashboard.
- Binding stays 127.0.0.1 (hard-coded in `hands/dashboard.py`); reach it through the SSH tunnel.

What changes when it is installed (for step 16's install plan):

1. `install.sh` currently ends with `hands-dash stop` + `hands-dash start` (tmux). Under systemd that
   must become `sudo systemctl restart hands-dashboard` (root), or the installed code keeps running
   until the next restart. Do not run both.
2. The cron watchdog can keep running; it becomes a no-op for the dashboard.
3. Logs move from the tmux pane to `journalctl -u hands-dashboard`.

Install (root):

```
sudo cp ~/harness/console/deploy/hands-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
~/.local/bin/hands-dash stop          # as builder: stop the tmux copy first
sudo systemctl enable --now hands-dashboard
systemctl status hands-dashboard
```

Rollback: `sudo systemctl disable --now hands-dashboard && sudo rm /etc/systemd/system/hands-dashboard.service
&& sudo systemctl daemon-reload`, then `hands-dash start` as builder.
