# Installing Phoenix Harness 0.5.2 (step 17: owner fixes)

Written 2026-10-18. Source: `~/harness`, branch `phoenix-0.5.2` (from `main` 8c493d4), tag `step-17-done`.
Same procedure as `docs/install-0.5.md`; only the differences are listed. Nothing here touches
`~/.config/hands` secrets, ufw, sshd or Docker images. The File Browser container stays.

## 1. Before installing (builder)

Sessions are now PTYs of the dashboard process: `bash install.sh` restarts the dashboard and **ends every
Session manager session** (Claude gets SIGHUP; each is offered for Resume afterwards). Finish or stop
anything important first. Also no build may be running (`hands smoke status` → runner alive: False).

## 2. Merge (builder)

```
cd ~/harness && git status            # clean
git checkout main && git merge --ff-only phoenix-0.5.2
git push origin main step-17-done
```

## 3. Install and log in (builder)

```
cd ~/harness && bash install.sh
cat ~/.local/share/hands/runner/VERSION      # 0.5.2
hands-dash url
```

## 4. Live checks (browser, through the tunnel; Safari)

1. Req 4: click **+** once, straight after the page loads and again a minute later: the menu opens every time.
2. Req 5: close the `smoke` tab, reopen it from **+** ten times; never a browser alert. Pull the network
   (turn Wi-Fi off, click Reopen): the error is a toast inside the page.
   `tail -f ~/.config/hands/dashboard.log` shows one line per request, `ERR` lines for failures.
3. Req 9: Session manager → **＋ New session**: the folder field says `/home/builder`. Start it; `pwd`
   in the terminal prints `/home/builder`.
4. Req 1: type something, close the browser tab, reopen the dashboard and the session: the scrollback is
   back and typing works. Open the same session in a second tab: both show new output.
   `tmux ls` lists no `hands_s_*` session. Then `hands-dash stop && hands-dash start`, log in: the session
   shows **stopped**; **Resume** brings the conversation back (`claude --resume`).
5. Req 6: sidebar usage meter: a window whose reset time has passed is dimmed, struck through and says
   "stale"; it refreshes once by itself, ↻ refreshes again (2-minute throttle).
6. Req 2 + 8: New session with **Live on Telegram** ticked → send a message from your phone → Claude answers
   in Telegram. Then set `"telegram@claude-plugins-official": false` under `enabledPlugins` in
   `~/.claude/settings.json`, start another Telegram session and repeat the phone message
   (`docs/telegram.md` §5 has the no-network check too).

## 5. Rollback (builder)

```
cd ~/harness && git checkout step-16-done && bash install.sh     # or: git checkout v0.5.1
```
(`main` before the merge is 8c493d4 = 0.5.1.) Then `hands-dash url` and log in again.
