# Installing Phoenix Harness 0.5.7 (step 23: change rounds + regression rule)

Written 2026-10-21. Source: `~/harness`, branch `phoenix-0.5.7` (from `main` ffaadfc = 0.5.6). Same
procedure as `docs/install-0.5.6.md`; only the differences are listed. Nothing here touches cloudflared,
Cloudflare, the File Browser container, `~/.ssh` or `~/.claude`.

## 1. Before installing (builder)

`bash install.sh` restarts the dashboard and ends every Session manager session (offered for Resume
afterwards). No build or planner may be running (`hands smoke status` → runner alive: False; no
`plan.lock` held). Existing projects need nothing: passed steps without a `spec_hash` record (built before
0.5.7, e.g. `smoke`) are simply not hash-checked; a `tests_changed` list in an old `plan.json` is read as
`change` entries (dashboard and CLI alike). A project amended under 0.5.6 whose `C1-` steps are still
pending at upgrade time builds them on `change/C1` from now on, not on `main` (none exists today).

## 2. Merge (builder)

```
cd ~/harness && git status            # clean, on main at ffaadfc
git merge --ff-only phoenix-0.5.7
git tag step-23-done
git push origin main step-23-done
```

## 3. Install (builder)

Run `install.sh` from `~`, not from inside `~/harness`, so the self-test and the copy use the merged source:

```
cd ~ && bash ~/harness/install.sh
cat ~/.local/share/hands/runner/VERSION      # 0.5.7
```

The self-test (fake docker, no Claude usage) runs before anything is copied; nothing is installed if it fails.

## 4. Live checks (Alex)

1. **Dashboard:** reload; the sidebar says 0.5.7. `smoke` looks as before (status done, no round line).
2. **CLI:** `hands smoke status` prints no round line (fresh project, round 0); `hands smoke check` still
   reports "app repo vs origin" against `main`.
3. **Deferred to the first real planned project (step 25 proof):** the step 22 install-doc section 4 checks
   2-4 (Amend form in the dashboard, the refusals) are still untested live, and so are this step's live
   paths: an amendment with `tests_changed` shows the "changes or removes existing acceptance tests" table
   and keeps **Approve & build amendment** disabled until its box is ticked; after approve, `runner.log`
   shows `opened round C1: branch change/C1 from <sha>, tag hands/pre-C1`, the app repo on GitHub has
   branch `change/C1` and tag `hands/pre-C1` at the old `main` head, `main` is unchanged, every C1 attempt
   runs the passed steps' acceptance commands first, and `hands <p> status` prints `round C1: open on
   change/C1 (base …, tag hands/pre-C1)`.

## 5. Rollback

```
cd ~/harness && git checkout ffaadfc && cd ~ && bash ~/harness/install.sh
cd ~/harness && git checkout main
```

0.5.6 ignores `round`, `spec_hash`, `spec_files` and per-step `round`/`branch` in state.json and the
`tests_changed` key in steps.json is **rejected** by its spec loader (unknown top-level key): a project whose
spec was amended under 0.5.7 with that key needs the key removed from `steps.json` on the spec branch before
0.5.6 can build it. A project with an open round (clone on `change/C<n>`) would be synced back to `main` by
0.5.6's runner and build the remaining steps there; stop such a project (`hands <p> stop`) before rolling
back and merge or discard `change/C<n>` by hand.
