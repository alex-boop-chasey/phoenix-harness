# Step 15: Import an existing repo as a project

## Goal
Phoenix can build on an existing codebase: a new spec repo, but the app repo is one that already exists.

## Rules
- The existing app repo is never deleted, force-pushed or re-created. `destroy` must leave it untouched (only remove what Phoenix added: its deploy key and Phoenix's local folders and secrets).
- Phoenix adds a write deploy key to the existing repo, and a ruleset only if Alex ticks it (the repo may already have its own rules).
- The runner keeps working on the repo's default branch as today, unless Alex picks a branch.

## Tasks (tests first, fake GitHub API as in tests/test_newproject.py)
1. `hands <slug> create --from-repo <org>/<repo>` and the "+" menu's New project form: option "Start from an existing repo". Checks: repo exists and is accessible with the PAT, not archived, has a default branch.
2. `created.json` records the imported repo as imported (not created). `destroy` deletes only the specs repo it created and the deploy key it added.
3. Planner: gets a bounded summary of the existing code (file tree, package.json and framework detection, README, test setup) and plans changes on top of it rather than a new app. Update planner prompts and tests.
4. Coder prompt: notes that the codebase existed before Phoenix and existing conventions win.
5. Allowlist: the plan's `allowed_paths` must cover existing code paths; validation warns when it does not.

## Desired outcome
- Create from an existing repo works end to end against the fake API and local bare repos, and destroy leaves the imported repo in place.

## Automated checks
- Tests: create from repo (happy path, missing repo, archived repo, no access), created.json marks, destroy keeps the imported repo and removes only the added deploy key and the specs repo, planner prompt includes the code summary within size caps.
- Full suite passes.

## Alex's check
Read the Done summary. A real test on a throwaway copy of a repo happens after install.
