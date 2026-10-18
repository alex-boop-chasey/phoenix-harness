# Installing Phoenix Harness 0.5.3 (step 18: public access + keep-alive body fix)

Written 2026-10-18. Source: `~/harness`, branch `phoenix-0.5.3` (from `main` 0d2c5d4), tag `step-18-done`.
Same procedure as `docs/install-0.5.2.md`; only the differences are listed. Nothing here touches
cloudflared, Cloudflare, the File Browser container, `~/.ssh` or `~/.claude`.

## 1. Before installing (builder)

`bash install.sh` restarts the dashboard and ends every Session manager session (offered for Resume
afterwards). No build may be running (`hands smoke status` → runner alive: False).

## 2. Merge (builder)

```
cd ~/harness && git status            # clean
git checkout main && git merge --ff-only phoenix-0.5.3
git push origin main step-18-done
```

## 3. Install (builder)

```
cd ~/harness && bash install.sh
cat ~/.local/share/hands/runner/VERSION      # 0.5.3
```

## 4. Turn public access on (builder; no restart needed afterwards)

Prerequisites are already in place on this VPS: the tunnel routes `https://phoenix.rebirthwebdesign.com.au`
to `http://localhost:8090` and the Access application protects it. Needed from the Cloudflare dashboard:
the team domain (`<team>.cloudflareaccess.com`) and the application's **AUD tag** (Zero Trust → Access →
Applications → the app → Overview).

```
hands-dash public set --host phoenix.rebirthwebdesign.com.au --team <TEAM>.cloudflareaccess.com \
    --aud <AUD_TAG> --email <YOUR_ALLOWLISTED_EMAIL>
hands-dash public test
```

`test` must print `config valid …` and `jwks OK: N RSA signing key(s)`. If the AUD or team is wrong, every
public request is 403 (`token audience mismatch` / `token issuer mismatch` in `dashboard.log`); fix with
another `set`.

## 5. Live checks (Alex)

1. **Public:** on the phone or Mac without the SSH tunnel, open https://phoenix.rebirthwebdesign.com.au/.
   Cloudflare asks for the PIN (first time), then the app loads directly: no "Not logged in" text, no
   `hands-dash url` needed. Projects list, project page, Session manager list and a terminal (New session,
   type, reload the page, reattach) all work. `tail -n 20 ~/.config/hands/dashboard.log` shows your email
   on those requests and no token.
2. **Public without Cloudflare:** on the VPS
   `curl -s -H 'Host: phoenix.rebirthwebdesign.com.au' http://127.0.0.1:8090/api/projects` → 403
   `forbidden: access token missing or too long`.
3. **Local unchanged:** through the SSH tunnel, http://127.0.0.1:8090/ still needs `hands-dash url`;
   the login link still works; `hands-dash public off` does not affect it.
4. **Keep-alive bodies:** after a few minutes of normal use (open/close a project tab, open a terminal),
   `grep -c ' 501 ' ~/.config/hands/dashboard.log` does not grow and no line starts with `{}`.
5. **Off switch:** `hands-dash public off` → the public URL is 403 within one request; `set` again
   restores it, both without restarting the dashboard.

## 6. Rollback

```
cd ~/harness && git checkout step-17-done && bash install.sh
hands-dash public off          # optional: 0.5.2 ignores public.json anyway (non-local Host -> 403)
```
