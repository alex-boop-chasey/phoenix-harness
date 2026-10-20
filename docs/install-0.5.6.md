# Installing Phoenix Harness 0.5.6 (step 22: planner amend mode)

Written 2026-10-20. Source: `~/harness`, branch `phoenix-0.5.6` (from `main` 1c3f3bb = 0.5.3; 0.5.4 and
0.5.5 are abandoned and stay on tag `keep-0.5.5` / their branches). Same procedure as
`docs/install-0.5.3.md`; only the differences are listed. Nothing here touches cloudflared, Cloudflare, the
File Browser container, `~/.ssh` or `~/.claude`.

## 1. Before installing (builder)

`bash install.sh` restarts the dashboard and ends every Session manager session (offered for Resume
afterwards). No build or planner may be running (`hands smoke status` → runner alive: False; no
`plan.lock` held).

## 2. Merge (builder)

```
cd ~/harness && git status            # clean, on main at 1c3f3bb
git merge --ff-only phoenix-0.5.6
git tag step-22-done
git push origin main step-22-done
```

## 3. Install (builder)

Run `install.sh` from `~`, not from inside `~/harness`, so the self-test and the copy use the merged source:

```
cd ~ && bash ~/harness/install.sh
cat ~/.local/share/hands/runner/VERSION      # 0.5.6
```

The self-test (fake docker, no Claude usage) runs before anything is copied; nothing is installed if it fails.

## 4. Live checks (Alex)

1. **Dashboard:** reload; the sidebar says 0.5.6. Open a project whose plan is approved (e.g. `smoke` is not
   managed and shows no Plan section; a managed, approved project shows a compact **Amend plan** form).
2. **Amend (optional, uses Claude):** type a one-line change request, press **Amend plan**. The log shows
   `=== amending`, `app repo at <sha>: N files, mounted read-only at /app`, `amendment round 1: … frozen; new
   ids C1-*`. When ready, the review shows the new step(s) tagged *new*, the change request, the built commit;
   a shared-settings diff appears only if the planner changed setup/regression/paths, and then
   **Approve & build amendment** stays disabled until its box is ticked. PR `hands/amend-C1` exists in
   `<slug>-specs`. Approve merges it and the runner starts on the new step; passed steps stay passed.
3. **Fresh planning unchanged:** a new project plans and approves as before (PR `hands/plan`).
4. **Refusals:** `hands <p> plan` on an approved project prints `already approved; use plan --amend` and the
   plan state in the dashboard is unchanged (not "failed"). `hands <p> approve` on an amendment with
   shared-settings changes (or a plan with new domains) refuses and prints the old and new values; it needs
   `--ack-shared` (`--ack-domains`).

## 5. Rollback

```
cd ~/harness && git checkout 1c3f3bb && cd ~ && bash ~/harness/install.sh
cd ~/harness && git checkout main
```

0.5.3 ignores the new `plan.json` fields. A project left with an amendment in state `ready` shows as a
normal ready plan there; its PR (`hands/amend-C<n>`) can be merged on GitHub or closed by hand.
