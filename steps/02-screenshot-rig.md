# Step 02: UI screenshot rig

## Goal
A one-command way to see the dashboard exactly as a browser does, with realistic sample data, without touching the live dashboard, real projects or any secret. Every later UI step uses it.

## Tasks
1. Write `tools/ui-shots.sh` (in the repo) that:
   - starts a throwaway container (`--rm`) from the agent image (`hands:cc2.1.301-pw1.64.1`: Node, Playwright Chromium; confirm Python 3 is present first) with `--network none`, its own `--entrypoint` (the image's firewall entrypoint is not needed with no network), the repo mounted read-only, and an output folder mounted read-write;
   - inside the container: creates a temporary config home and sample projects (see 2), starts the dashboard from the mounted source on 127.0.0.1 with that config home, logs in the same way `hands-dash url` does, and takes Playwright screenshots;
   - writes PNGs to `~/ops/shots/<label>/` named `<page>-<width>.png`, for widths 1440 and 390;
   - takes a list of pages or routes to capture, with a sensible default.
2. Write `tools/fixtures.py` that creates sample projects covering every state the UI shows (new, planning, plan ready, building with a running step, failed step, done, stopped), with fake logs and attempts. Nothing real: no GitHub, no Docker, no Claude.
3. Capture the current dashboard as the "before" set: `~/ops/shots/02-before/`.
4. Document usage at the top of the script.

## Desired outcome
- `bash tools/ui-shots.sh 02-before` produces screenshots of the current dashboard with sample data, at 1440 and 390 wide.
- The rig never reads `~/.config/hands`, never uses the network, and leaves no containers behind.

## Automated checks
- Run it twice; both runs succeed and `docker ps -a` shows no leftover containers.
- `grep` the script and fixtures for `.config/hands` (excluding the temporary config home variable): no hits.
- Look at every screenshot yourself and confirm it shows real dashboard content with sample data, not an error or login page.

## Alex's check
In File Browser open `ops/shots/02-before/` and look at the PNGs: this is today's dashboard with sample projects.
