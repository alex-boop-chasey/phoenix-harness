# Step 14: File browser: page in the app, retire the old container

## Goal
Alex uses Phoenix's File browser instead of the File Browser container, which is then removed.

## Tasks
1. File browser page per `docs/design.md`: roots switcher, breadcrumb, list and grid views, sort, file preview (text with syntax colour optional, images), upload (drag and drop), download, new folder, rename, delete with confirmation, search.
2. Consistent look: only the shared CSS and components from `docs/design.md`; nothing styled after File Browser.
3. Settings: view and edit allowed roots (the denied paths stay denied and are shown as such).
4. Phone layout.
5. Write the retirement commands for Alex (run as root after he has used the new page): `docker stop filebrowser`, `docker rm filebrowser filebrowser-old`, `rm -rf /opt/filebrowser`, and removing the 8080 forward from his Mac `~/.ssh/config`. Do not run them.

## Desired outcome
- Everything Alex used File Browser for works on the new page.
- Retirement commands are ready, not run.

## Automated checks
- Playwright flow in the rig on a temporary folder tree: browse, preview, upload, download, rename, new folder, delete, search; attempts to open a denied path show a clear refusal.
- Screenshots `~/ops/shots/14/` at both widths, light and dark, side by side with the Project builder and Session manager; confirm they match.
- grep: no colour literals or font declarations in File browser code outside the shared CSS tokens.
- Full suite and `node --check` pass.

## Alex's check
Look at `ops/shots/14/`. After install (step 16), use it for a day, then run the retirement commands.
