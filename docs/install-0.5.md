# Installing Phoenix Harness 0.5.0 (plan for Alex)

Written 2026-10-17 (step 16). The source is `~/harness` on branch `phoenix-0.5`, release candidate tag
`v0.5.0-rc2`; the live copy in `~/.local/share/hands/runner` is still 0.4.3 until step 3 below. Run the
steps in order; each one says who runs it (builder = normal shell as `builder`, root = `ssh root` shell).
Nothing here touches `~/.config/hands` secrets, ufw, sshd or Docker images.

## 1. Confirm no build is running (builder)

```
for p in $(ls ~/.config/hands/projects/ | sed 's/\.toml$//'); do echo "== $p"; hands "$p" status | head -3; done
tmux ls
```

Every project must show `runner alive: False`, and `tmux ls` must list no `hands-<project>` session
(`hands_dashboard` and `hands_s_*` sessions are fine). If a runner is alive: `hands <p> stop`, wait for it to
reach a safe point, check again. Also make sure no planner is running (project page shows "planning").

## 2. Merge and push (builder)

```
cd ~/harness
git status                      # must be clean
git tag v0.4.3-installed main   # rollback point = what is installed today
git checkout main
git merge --ff-only phoenix-0.5
git push origin main v0.4.3-installed v0.5.0-rc1 v0.5.0-rc2 step-16-done
git log --oneline -1            # expect "install.sh: stop creating ra.toml" (v0.5.0-rc2)
```

`--ff-only` works because `phoenix-0.5` was branched from `main` and `main` has not moved; if it refuses,
stop and ask before merging any other way.

## 3. Install (builder)

```
cd ~/harness && bash install.sh
cat ~/.local/share/hands/runner/VERSION      # 0.5.0
```

`install.sh` copies the source to the live folder, runs the test suite (249 tests, 2 skips are expected:
node on the host, tmux inside the sandbox), keeps the watchdog cron line and restarts the dashboard in tmux
(`hands_dashboard`). Open Session manager sessions survive (they live in their own tmux sessions); the
browser session does not: log in again in step 5.

## 4. Dashboard as a systemd service (root, optional but recommended)

Today the dashboard runs in tmux and the cron watchdog restarts it. The prepared unit
(`console/deploy/hands-dashboard.service`, see `console/deploy/README.md`) runs the same command as a
system service. Both cannot run at once (the dashboard holds `~/.config/hands/dashboard.lock`), so stop
the tmux copy first.

As builder:
```
hands-dash stop
```

As root:
```
cp /home/builder/harness/console/deploy/hands-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hands-dashboard
systemctl status hands-dashboard --no-pager
journalctl -u hands-dashboard -n 20 --no-pager
```

Skip this step to keep the tmux dashboard; everything else works the same. From now on, after any later
`bash install.sh`, restart with `systemctl restart hands-dashboard` (root) instead of `hands-dash start`
(the cron watchdog and `hands-dash start` see the lock and do nothing).

## 5. New login link (builder)

```
hands-dash url
```

Open the link through the tunnel (`ssh -N -L 8090:127.0.0.1:8090 ...`; the URL must be http://127.0.0.1:8090).
The code is single use and valid 5 minutes.

## 6. Smoke checks (builder + browser)

```
hands smoke check          # ALL CHECKS PASSED (uses Docker and the Claude token: a few cents)
hands smoke validate
```

In the browser:
1. Project builder: the `smoke` tab opens, steps and runner log show; close the tab and reopen it from "+".
2. "+" → New project: the form shows "Start from an existing repo"; tick and untick it (do not submit).
3. Session manager → New session with folder `~/ops`: the terminal shows Claude Code starting; type
   `/status`, then Stop, then Resume (the transcript comes back), then Delete.
4. File browser: browse `harness/`, open `README.md` in the preview, search for `install`, open Settings
   and check the denied list (`.config/hands`, `.ssh`, `.config/gh`, `.git-credentials`, `.claude`).
5. Phone width (narrow the window to ~390 px): the drawer opens from the menu button, each section works.

Then retire the old File Browser container: `docs/retire-filebrowser.md` (stop + remove, keep the
`filebrowser-old` container until you are happy with the new page).

## 7. Rollback (builder, plus root if step 4 was done)

Back to the last good point before 0.5.0:
```
cd ~/harness && git checkout v0.4.3-installed && bash install.sh
```
or to a specific step: `git checkout step-NN-done && bash install.sh` (`step-14-done` = everything but the
repo import; `step-16-done` = this release). Then `hands-dash url` and log in again.

If the systemd unit was installed and the rollback target should run in tmux again (root):
```
systemctl disable --now hands-dashboard
rm /etc/systemd/system/hands-dashboard.service
systemctl daemon-reload
```
then as builder `hands-dash start`. Project data, `~/.config/hands` and session records are not touched by
install or rollback; 0.4.3 ignores the new config keys only if no project uses them (`imported`,
`spec_branch` would make 0.4.3 refuse that project's config: destroy or edit such a project first).

Afterwards: `git checkout main` to get back to the branch.
