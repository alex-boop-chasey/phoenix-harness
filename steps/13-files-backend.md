# Step 13: File browser: secure backend

## Goal
A file browser backend inside Phoenix that can only touch folders Alex allows and can never reach secrets.

## Rules
- Allowed roots come from a setting (default: `~/harness`, `~/projects`, `~/ops`, `~/claude-harness`). Alex can change it later in the UI.
- Always denied, even inside an allowed root: `~/.config/hands`, `~/.ssh`, `~/.config/gh`, `~/.git-credentials`, `~/.claude` (credentials and settings), any path whose real location is outside the allowed roots.
- Paths are resolved with `os.path.realpath` after joining; symlinks pointing outside are refused; `..`, NUL and absolute paths in requests are refused.
- Do not copy File Browser's Go code. Python, same login and checks as the dashboard.

## Tasks (tests first)
1. Operations: list folder (name, type, size, modified, mode), read text file (size cap, binary detection), download file, upload (size cap, no overwrite unless confirmed), new folder, rename/move within allowed roots, delete (files and empty folders; recursive delete only with explicit confirm flag), search by name within a root (bounded).
2. Every write goes through the same checks; refuse writes into `.git` folders unless Alex enables it.
3. Same auth, Origin and CSRF checks as the dashboard. Streams for download and upload, bounded memory.
4. Update `docs/api.md`.

## Desired outcome
- Every operation works inside allowed roots and refuses everything outside, including through symlinks and tricky paths.

## Automated checks
- Tests for each operation, plus a refusal table: `../`, encoded `%2e%2e`, absolute paths, symlink to `~/.config/hands`, symlink loop, path into `~/.ssh`, NUL byte, very long name, upload over cap, overwrite without confirm, recursive delete without flag, `.git` write.
- Full suite passes.

## Alex's check
None (backend only). Read the Done summary.
