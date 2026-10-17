# Step 08: Environment variables: build containers and safety

## Goal
The build agents can use a project's variables, the app they build reads them correctly, and secrets cannot leak into repos, logs or notifications.

## Tasks (tests first, using the fake docker in tests/)
1. Containers: mount the project's env file read-only at `/run/hands/project.env` in coder, evaluator and every acceptance container, and export the same variables into their environment. Planner gets key names and visibility only, never values.
2. Prompts: `prompts/coder.md` gains a section: the key list (names and visibility) is in the prompt; values are at `/run/hands/project.env`; the app gets them through a committed, secret-free sync script (for example `scripts/sync-env.mjs`) run from package.json pre-scripts that writes the framework's gitignored env file (`.env`, `.env.local`, `.dev.vars`, whatever the framework needs) and maps names to the framework's prefixes; ensure that file is gitignored; commit `.env.example` with names only; public keys may get client prefixes (PUBLIC_, VITE_, NEXT_PUBLIC_), secret keys never; if a needed key is missing, output the existing SPEC_PROBLEM line naming it. `prompts/evaluator.md`: check that no secret value is in the client bundle and that `.env.example` matches the key list.
3. Runner gates:
   - Allowlist always denies `.env`, `.env.*`, `.dev.vars` (any case) except `.env.example` and `.env.sample`.
   - Reject an attempt if any secret value (8+ characters) appears anywhere in the staged tree or diff. The reason names the key, never the value.
   - Redact secret values as `[REDACTED:KEY]` in everything the runner stores, serves or sends: session streams, attempt files, logs, verdicts, progress, Telegram.
4. `hands <p> check`: report key count by visibility; fail if the env file is not 0600 or has invalid lines.
5. Update `README.md` and `docs/architecture.md`.

## Desired outcome
- Agents receive the variables in all three container types; the planner never sees values.
- A coder attempt that commits a secret or an env file is rejected with a clear reason.
- No secret value appears in any file the runner writes or anything the dashboard serves.

## Automated checks
- Unit tests: mount and env flags per container type (fake docker records them); planner prompt contains names but no values; allowlist denies each env file variant and allows `.env.example`; secret scan rejects a diff containing a value; redaction in each output path; `check` reports and fails correctly.
- Full suite passes.

## Alex's check
None (backend only). Read the Done summary.
