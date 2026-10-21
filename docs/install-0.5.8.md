# Installing Phoenix Harness 0.5.8 (step 24: merge tap + dashboard rounds)

Written 2026-10-21. Source: `~/harness`, branch `phoenix-0.5.8` (from `main` d70179d = 0.5.7). Same
procedure as `docs/install-0.5.7.md`; only the differences are listed. Nothing here touches cloudflared,
Cloudflare, the File Browser container, `~/.ssh` or `~/.claude`.

## 1. Before installing (builder)

`bash install.sh` restarts the dashboard and ends every Session manager session (offered for Resume
afterwards). No build, planner, merge or discard may be running (`hands smoke status` → runner alive: False;
no `plan.lock` held; no round in state `merging`/`discarding`). Existing projects need nothing: a project
without a round record (`smoke`, every fresh project) shows no round tag, no round line and no "Change
rounds" section. New state files: `state/rounds.jsonl` (round history) and `state/round.log` (merge/discard
log), both written only when a round opens or a tap is pressed. New TOML key `self` (default false; nothing
sets it before step 29).

## 2. Merge (builder)

```
cd ~/harness && git status            # clean, on main at d70179d
git merge --ff-only phoenix-0.5.8
git tag step-24-done
git push origin main step-24-done
```

## 3. Install (builder)

Run `install.sh` from `~`, not from inside `~/harness`, so the self-test and the copy use the merged source:

```
cd ~ && bash ~/harness/install.sh
cat ~/.local/share/hands/runner/VERSION      # 0.5.8
```

The self-test (fake docker, no Claude usage) runs before anything is copied; nothing is installed if it fails.

## 4. Live checks (Alex)

1. **Dashboard:** reload; the sidebar says 0.5.8. `smoke` looks as before (status done, no round tag on
   its tab, no round line, no "Change rounds" section).
2. **CLI:** `hands smoke status` prints no round line; `hands smoke merge` answers
   `merge: --branch-sha and --main-sha required …` (rc 2); `hands smoke merge --branch-sha <40 zeros>
   --main-sha <40 zeros>` answers `merge refused: no open round to merge (state: none)` (rc 1), nothing changes.
3. **Deferred to the first planned project with an amendment (step 25 proof):** after the round's last step
   passes, the project tab shows `C1 merge?`, the head shows the round line `… 2/2 steps passed · ready to
   merge`, the "Change rounds" card lists the steps with ✓ and offers **Merge change C1 (<branch> → main
   <base>)** and **Discard round C1**. Merge: the confirm names both shas; after it the card says `merged
   into main <sha>`, GitHub shows one new commit on `main` (`Change C1: …`), tags `hands/pre-C1` and
   `hands/merged/C1`, and no `change/C1` branch; Telegram says `merged C1 into main (<sha>)`;
   `hands <p> reset-step C1-01` is refused with "merged work is never rebuilt"; **Amend plan** works again.
   Pressing Merge after pushing anything to `change/C1` or `main` by hand is refused ("moved since …").

## 5. Rollback

```
cd ~/harness && git checkout d70179d && cd ~ && bash ~/harness/install.sh
cd ~/harness && git checkout main
```

0.5.7 ignores `request`, `pr`, `pr_url`, `head_sha`, `merged_sha`, `merged_tag`, `merged_at`, `error` in the
round record, `rounds.jsonl` and `round.log`, and the TOML key `self` is **rejected** by its config loader
(unknown key): remove it from the project's TOML before rolling back. A record left in state `merging` or
`discarding` (tap pressed, CLI killed) is not `open` to 0.5.7 either: set `"status": "open"` in state.json
by hand (the merge itself changed nothing on origin until its record says `merged`).

## 6. Rolling back a merged change (by hand, any version)

`hands/pre-C<n>` = `main` before the change, `hands/merged/C<n>` = the unsquashed round commits. To undo a
merged round on GitHub: `git push origin hands/pre-C<n>:refs/heads/main --force-with-lease=main:<merged sha>`
from a clone, then in the project's state.json set the C<n> steps back to pending and remove the round
record (or re-plan). Nothing in Phoenix automates this: the runner never force-pushes.
