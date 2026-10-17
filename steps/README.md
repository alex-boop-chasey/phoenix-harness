# Phoenix Harness build steps

"Do step NN" means read `~/ops/steps/NN-*.md` and carry it out. When a step passes, report it and go straight on to the next step without waiting, unless the next step is 03, 09, 12 or 16 (those start only when Alex says "Do step NN").
These files are read-only. Never edit, move or delete anything in `~/ops/steps/`.

## Before every step
1. Read `~/ops/CLAUDE.md`, `~/harness/CLAUDE.md`, this README, the step file, and `~/ops/status.json`.
2. Confirm every earlier step is `"passes": true` in `~/ops/status.json`. If not, stop and say which.
3. Work in `~/harness` on branch `phoenix-0.5` (create it from `main` in step 01 if missing). `git status` must be clean when you start.

## How to work
- Keep going until every item under "Desired outcome" is true and every check under "Automated checks" passes. Do not stop to ask for permission between tasks.
- Stop and ask Alex only when:
  - a command needs root (write it in one code block for Alex to run, then wait for his output);
  - the step lists a "Decision for Alex" (present 2-3 options with your recommendation, then wait);
  - you are blocked after real debugging (find the root cause first: reproduce, read the error, check one hypothesis at a time). Report what you tried and what you need.
- Tests first for backend behaviour: write the failing test, then the code, then run it.
- Small commits on `phoenix-0.5` with clear messages. Never push, never install, never merge to `main`. Alex does those.
- Never weaken a security check to make a test pass. If a rule in `~/harness/CLAUDE.md` ("Never weaken these") gets in the way, stop and ask.
- Docker commands run outside the sandbox and need Alex's approval each time. Batch them so he approves few prompts.
- One design system for the whole app: every page (Project builder, Session manager, File browser and anything added later) uses the tokens and components in `docs/design.md` (from step 03). Do not keep claude-harness's or File Browser's own look, CSS, fonts, colours or login page. No page-specific colours or one-off styles; if a component is missing, add it to `docs/design.md` and the shared CSS first.
- UI work: check it yourself with screenshots (from step 02 on) at desktop 1440x900 and phone 390x844, before calling it done.

## When a step is done
1. Run the full checks: `python3 -m unittest discover -s tests` (all pass), `node --check` on every changed `.js` file, plus the step's own checks.
2. Commit, then tag: `git tag step-NN-done`.
3. Update `~/ops/status.json`: set the step's `"passes": true`, add `"commit"`, `"finished"` (UTC ISO time) and a one-line `"note"`. Only after all checks pass.
4. Report to Alex in this shape:
   - **Done:** one or two lines.
   - **Checks:** each automated check with its result.
   - **Your check:** copy the step's "Alex's check" with exact URLs or commands.
   - **Screenshots:** file paths under `~/ops/shots/NN/` (Alex views them in File Browser).
   - **Rollback:** `git -C ~/harness reset --hard step-PP-done` where PP is the previous step.
   - **Open issues:** anything you noticed but did not fix.

## Status file
`~/ops/status.json` is a list of `{"step": "NN", "title": "...", "passes": false}` entries. Step 01 creates it if missing, with every step from this folder listed and `"passes": false`. Never mark a step passed without its checks passing. Never remove or rename entries.

## Step list
01 Baseline, architecture and API map
02 UI screenshot rig
03 Design system and layout mock (decision for Alex)
04 App shell with left sidebar
05 Project builder tabs (close, reopen, new)
06 Environment variables: storage and API
07 Environment variables: dashboard UI
08 Environment variables: build containers and safety
09 Session manager: import claude-harness and architecture decision (decision for Alex)
10 Session manager: secure backend
11 Session manager: page in the app
12 Session manager: Telegram live session (root and Alex steps)
13 File browser: secure backend
14 File browser: page in the app, retire the old container
15 Import an existing repo as a project
16 Release 0.5.0: review, docs, install plan
