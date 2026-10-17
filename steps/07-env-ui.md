# Step 07: Environment variables: dashboard UI

## Goal
Alex can enter a project's environment variables in the dashboard, both when creating a project and later.

## Tasks
1. New project form: an "Environment variables" section with rows [Key] [Value] [Secret | Public toggle, default Secret] [remove], a "+" button to add a row, and Save. Client-side validation matching the backend, with clear error messages next to the field.
2. Project page: an "Environment" panel with the same editor. Secret values show masked and can be replaced or deleted, never revealed. Public values are shown.
3. Paste support: pasting several `KEY=value` lines into a Key field fills multiple rows (all marked Secret).
4. Explain in one line under the editor what Secret and Public mean (public values may end up in the browser bundle of the built app; secrets never do).

## Desired outcome
- Adding, editing, deleting and pasting variables works in both places and survives reload.
- No secret value ever appears in the page after save (check the DOM and network responses).

## Automated checks
- Playwright flow in the rig: add three variables (two secret, one public), save, reload, confirm masks, replace a secret, delete one, paste a block. Assert no secret value in `document.body.innerHTML` or any response body after save.
- Screenshots `~/ops/shots/07/` at both widths, light and dark, including validation errors.
- Full suite and `node --check` pass.

## Alex's check
Look at `ops/shots/07/`.
