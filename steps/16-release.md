# Step 16: Release 0.5.0: review, docs, install plan

## Goal
Phoenix Harness 0.5.0 is reviewed, documented and ready for Alex to install, with a tested rollback.

## Tasks
1. Independent review: start a fresh subagent (no access to your reasoning) and ask it to audit the diff `main..phoenix-0.5` for security issues, focusing on: secrets reaching logs, UI, containers or repos; auth, Origin and CSRF on every new route; path handling in Files; the console's spawn arguments and environment; CSP. Fix every real finding with a test. Record findings and fixes in `docs/review-0.5.md`.
2. Full regression: test suite, `node --check`, every rig flow from steps 04-14, full screenshot set `~/ops/shots/16/`.
3. Docs: update `README.md`, `docs/architecture.md`, `docs/api.md`, `docs/design.md`; write `CHANGELOG.md` for 0.5.0; bump `VERSION`.
4. Install plan for Alex, in order, as commands he runs:
   - confirm no build is running (`hands <p> status` for every project);
   - Alex merges `phoenix-0.5` into `main` and pushes (give the commands);
   - `bash install.sh`;
   - install the console's systemd unit (root commands);
   - new login link with `hands-dash url`;
   - smoke checks: `hands smoke check`, open each section, start a real Session manager session in `~/ops`, browse files;
   - rollback: `git -C ~/harness checkout step-NN-done` of the last good point, or tag `v0.4.3-installed` created before install, then `bash install.sh`.
5. Tag the release candidate `v0.5.0-rc1` on `phoenix-0.5`.

## Desired outcome
- Review findings fixed, all checks green, docs current, install and rollback plan ready.

## Automated checks
- Full suite, `node --check`, every rig flow, no console or CSP errors.
- `docs/review-0.5.md` lists every finding with its fix or a reason it is not a problem.

## Alex's check
Read `docs/review-0.5.md` and the install plan, then decide when to install.
