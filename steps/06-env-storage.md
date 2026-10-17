# Step 06: Environment variables: storage and API

## Goal
Each project can hold environment variables, marked secret or public, stored safely where only the runner can use them.

## Rules
- Values live in `~/.config/hands/secrets/<slug>/project.env` (mode 0600, folders 0700), the file `create` already makes. Metadata (key, visibility, updated time, never values) lives next to it in `env.json`.
- Secret values are write-only: once saved, no API response ever contains them. Public values may be returned.
- You must never read the real files under `~/.config/hands`. Develop and test only with a temporary config home (the tests already do this via `HANDS_CONFIG_HOME`).

## Tasks (tests first)
1. Module `hands/projectenv.py`: load, validate, save (atomic, 0600), list for API, delete key.
   - Key: `^[A-Z_][A-Z0-9_]*$`, max 128, unique. Value: one line, no NUL, max 8 KB. Dotenv quoting that round-trips every allowed value.
   - Reserved keys rejected: CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, GITHUB_TOKEN, TG_TOKEN, TG_CHAT_ID, PATH, HOME, USER, SHELL, CI, NODE_OPTIONS, anything starting with HANDS_ or CLAUDE_.
   - Existing `project.env` files written by hand (KEY=value lines) load correctly and are treated as secret.
2. Dashboard API: list (keys, visibility, masked secret, public value), set (one or many), delete. Same auth and CSRF as other POSTs. Works for an existing project; for a project being created, store as pending and move into place when `create` finishes (and clean up if create fails).
3. Update `docs/api.md`.

## Desired outcome
- A secret value can be saved, replaced and deleted, but never read back through the API.
- Files have the right modes. Nothing is written inside the project folder or any repo.

## Automated checks
- Unit tests: validation (good and bad keys and values, reserved names), round-trip quoting with tricky values (=, #, quotes, spaces, unicode), modes 0600/0700, atomic write, secret never in any API response (assert on the raw response bytes), pending-to-project move, cleanup on failed create, hand-written file compatibility.
- Full suite passes.

## Alex's check
None (backend only). Read the Done summary.
