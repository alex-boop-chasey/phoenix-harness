# Phoenix Harness ops workspace (VPS "rebirth")

This is a Hostinger KVM 2 VPS (Ubuntu 26.04, 2 vCPU, 8 GB RAM, x86_64). Claude runs here as user `builder`. Alex (Brisbane, AEST) approves actions. The VPS clock is UTC.

## Communication
Short, matter-of-fact. Lead with the answer. Never guess: say when unsure, then check. Anything that needs root: write the command in one code block and Alex runs it.

## Hard rules
- Never read, print, copy or move secrets: `~/.config/hands/**` (Claude token, GitHub PAT with org-admin rights, Telegram token, per-project secrets in `secrets/<slug>/`), `~/.ssh`, `~/.config/gh`, `~/.git-credentials`, `~/.claude/channels`. Never ask Alex to approve a command that would read them.
- Never use `--dangerously-skip-permissions` on this host. Never edit `~/.claude/settings*.json`, this file, or anything in `~/ops/steps/`.
- `~/harness` is SOURCE. `bash install.sh` copies it to `~/.local/share/hands/runner` (the live copy) and restarts the dashboard. Only Alex installs, pushes and merges.
- Follow `~/harness/CLAUDE.md`, especially "Never weaken these".
- Services bind to 127.0.0.1 only. Never change ufw, sshd, systemd, cron or users yourself.
- No destructive Docker commands (prune, volume rm, removing containers you did not create) without asking.

## What exists
- `~/harness`: Phoenix 0.4.3 (runner `hands`, dashboard 127.0.0.1:8090 in tmux `hands_dashboard`, planner, agent image files in `image/`, tests). Repo `alex-boop-chasey/phoenix-harness` (private). Python 3 stdlib only; vanilla JS dashboard with strict CSP. Project secrets live in `~/.config/hands/secrets/<slug>/`.
- Watchdog cron every 10 min: `~/.local/bin/hands all watchdog`.
- Projects: only `~/projects/smoke` (harness self-test, keep).
- Agent image `hands:cc2.1.301-pw1.64.1`; per-session containers are created and removed by the runner.
- `~/claude-harness`: clone of github.com/alex-boop-chasey/claude-harness (public), the persistent Claude terminal app to merge in.
- File Browser container `filebrowser` (2.63.23, upstream archived 2026-09-01, no security fixes): uid 1000, `/home/builder` at /srv, 127.0.0.1:8080. Stopped `filebrowser-old` kept for rollback.
- Alex reaches 8080 and 8090 through SSH tunnels on his `ssh root` login.

## The job
Merge Phoenix, claude-harness and a file browser into one professional, easy app in `~/harness`, built on the existing code. The work is split into steps in `~/ops/steps/` (start with its README). Alex says "Do step NN". Progress is tracked in `~/ops/status.json`.
