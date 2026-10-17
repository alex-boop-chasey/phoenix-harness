# Step 12: Session manager: Telegram live session

## Goal
One Session manager session can be "live": connected to Alex's Telegram so he can message Claude from his phone, safely.

## Facts
- Telegram is a Claude Code channel plugin (`telegram@claude-plugins-official`), needs Bun, a bot token from BotFather, and pairing; then lock it to Alex's account with `/telegram:access policy allowlist`.
- The token is saved to `~/.claude/channels/telegram/.env`. You must never read it.
- A permission prompt pauses the session until answered. Check the plugin source (github.com/anthropics/claude-plugins-official, external_plugins/telegram) for the permission relay capability and report whether approvals can be answered from Telegram.

## Tasks
1. Write the exact root and Alex commands, in order, for Alex to run (you do not run them):
   - install Bun for builder (official installer, pinned version);
   - add these to `~/.claude/settings.json` (Alex edits it, you cannot): Read and Edit deny for `~/.claude/channels/**`; sandbox credentials deny for `~/.claude/channels`;
   - create a new bot in BotFather (not the runner's notification bot);
   - in a Claude session: install the plugin, run `/telegram:configure <token>` himself, pair, set the allowlist policy.
2. Add the "live" option to the backend and page: start or restart the live session with `--channels plugin:telegram@claude-plugins-official`; only one live session at a time; clear badge in the UI.
3. Record the permission-relay finding in `docs/console-review.md`.

## Decision for Alex
If approvals cannot be answered from Telegram: accept that Telegram requests needing approval wait for the web terminal, or something else? Recommend, then wait.

## Desired outcome
- After Alex runs his commands: messaging the bot from his phone reaches the live session and Claude replies; messages from anyone else are dropped.
- The bot token is never read by you or shown anywhere.

## Automated checks
- Tests: only one live session; live argv contains the channels flag and never `--dangerously-skip-permissions`.
- Full suite passes.

## Alex's check
After his setup commands: send the bot "what folder are you in?" and get an answer. Ask a friend (or a second account) to message it: no reply.
