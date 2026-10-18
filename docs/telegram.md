# Live on Telegram: setup (step 12)

One Session manager session at a time can be "live": connected to Claude Code's official Telegram
channel plugin, so Alex can message Claude from his phone and answer permission prompts there
(Allow / Deny buttons; see `docs/console-review.md` §8). The harness only adds
`--settings '{"enabledPlugins":{"telegram@claude-plugins-official":true}}' --channels
plugin:telegram@claude-plugins-official` to that session's `claude` command and keeps the count at one.
The plugin stays **disabled** in `~/.claude/settings.json` (requirement 8, section 5 below): only a
session started with Telegram selected loads it. Everything secret (bot token, allowlist) is the plugin's, under
`~/.claude/channels/telegram/`, which the dashboard and the assistant never read.

Requirements: Bun on the builder account, the plugin installed and configured once, a dedicated bot
(not the runner's notification bot). All commands below run as **builder** over SSH; none needs root.
The Claude settings already deny the assistant `Read`/`Edit` on `~/.claude/channels/**` and the sandbox
denies that folder (checked 2026-10-17); nothing to add there.

## 1. Install Bun (pinned)

```bash
curl -fsSL https://bun.com/install | bash -s "bun-v1.4.3"
~/.bun/bin/bun --version        # 1.4.3
```

The installer puts the binary in `~/.bun/bin` and adds that to `~/.bashrc`. Live sessions do not read
`.bashrc`: the Session manager's own allowlisted `PATH` includes `~/.bun/bin`, so nothing else is needed.
(1.4.3 is the latest release on 2026-10-10. To upgrade later, re-run with the new tag.)

## 2. Create the bot

In Telegram, open **@BotFather** → `/newbot` → a display name, then a username ending in `bot`.
Copy the token (`123456789:AAH...`). Keep it out of chat logs and the dashboard.

## 3. Install and configure the plugin (once, from the SSH shell)

Use a throwaway folder so the setup transcript is not one of your real sessions:

```bash
mkdir -p ~/ops/tmp/tg-setup && cd ~/ops/tmp/tg-setup && claude
```

At Claude's prompt, one at a time:

```
/plugin install telegram@claude-plugins-official
```

Choose the **user** scope when asked. If it says `Run /reload-plugins to apply.`, run `/reload-plugins`.
(If the marketplace is missing: `/plugin marketplace add anthropics/claude-plugins-official`; if the
plugin is "not found": `/plugin marketplace update claude-plugins-official`, then retry.)

```
/telegram:configure <token>
```

This writes `~/.claude/channels/telegram/.env` (mode 0600). Then `/exit`, and optionally remove the
setup transcript: `rm -rf ~/.claude/projects/-home-builder-ops-tmp-tg-setup ~/ops/tmp/tg-setup`.

## 4. Go live from the Session manager and pair

1. Dashboard → **Session manager** → **＋ New session** → tick **Live on Telegram** → Start session.
   (Or select an existing session → **Go live on Telegram**; it restarts with the channel.)
   The head shows the ✈ **live on Telegram** badge; the list shows "1 on Telegram".
2. On your phone, send the bot any message. It replies with a 6-character pairing code.
3. In that session's web terminal:
   ```
   /telegram:access pair <code>
   /telegram:access policy allowlist
   ```
   `allowlist` = messages from anyone not paired are dropped silently (no pairing code offered).
   `/telegram:access` shows the policy and the allowlist at any time.

## 5. Disable the plugin globally (requirement 8; once, after the live check below passed)

Every `claude` on this host that has the plugin enabled starts the plugin's MCP server (the bridge to
your bot), the ops Claude included. Phoenix enables it per live session with `--settings`, so the user
setting goes back to off. In `~/.claude/settings.json` (Alex edits it; the assistant never does):

```json
"enabledPlugins": { "telegram@claude-plugins-official": false }
```

Then check from a plain shell that nothing of the plugin loads without the flag, and that it does with it
(no network and no bot involved; the debug log is written locally):

```bash
cd ~ && claude --debug-file /tmp/tg-off.txt -p ok >/dev/null 2>&1; grep -c "plugin telegram" /tmp/tg-off.txt   # 0
claude --settings '{"enabledPlugins":{"telegram@claude-plugins-official":true}}' --debug-file /tmp/tg-on.txt -p ok >/dev/null 2>&1
grep -c "plugin telegram" /tmp/tg-on.txt    # > 0 ("Checking plugin telegram", "Loaded 2 skills from plugin telegram")
rm -f /tmp/tg-off.txt /tmp/tg-on.txt
```

Verified by the assistant on 2026-10-18 with Claude Code 2.1.292 (user setting true at the time):
`--settings …false` → "Found 7 plugins (5 enabled, 2 disabled)", no telegram line; `--settings …true` →
"6 enabled", "Attempting to load skills from plugin telegram", `MCP server "plugin:telegram:telegram":
Starting connection`. `--settings` wins over the user file in both directions, so the per-session enable
works once the file says false.

## Check

- From your phone: "what folder are you in?" → Claude answers in Telegram (the web terminal shows the
  inbound line and the `reply` tool call). The first `reply` may ask for permission: approve it in
  Telegram (✅ Allow) or in the terminal.
- From a second account (a friend): no reply at all.
- Only one session can be live: ticking **Live on Telegram** for a second one is refused inline, naming
  the session that holds the slot. **Leave Telegram** on a session restarts it without the channel.

## Notes

- Permission prompts go to **every allowlisted chat**: keep the allowlist to your own account, never
  add a group (`/telegram:access remove <id>` removes one).
- The bot only sees messages while the live session runs; stopping the session stops the bot.
- Channels are a research preview (Claude Code ≥ 2.1.29x accepts `--channels` but hides it from
  `--help`); if a future CLI changes the flag, `sessions.TELEGRAM_CHANNEL`/`claude_argv()` is the one
  place to update, with `tests/test_v12.py` pinning it.
