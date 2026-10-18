# Public access to the dashboard through Cloudflare Access (0.5.3, step 18)

Written 2026-10-18. Code: `hands/publicaccess.py`, request handling in `hands/dashboard.py`. Works for any
VPS: nothing about a host, team, AUD or email is in the code; it all lives in `~/.config/hands/public.json`.

## How it works

The dashboard still binds 127.0.0.1:8090 only. A Cloudflare Tunnel (`cloudflared`, a systemd service owned
by the operator) connects out to Cloudflare and forwards requests for `https://<public host>` to
`http://127.0.0.1:8090`. Cloudflare Access sits in front: a visitor gets a one-time PIN by email and only
addresses in the Access policy pass. Every request Cloudflare forwards carries the Access JWT in the header
`Cf-Access-Jwt-Assertion` (also the `CF_Authorization` cookie, which Phoenix ignores).

cloudflared connects from 127.0.0.1, so the remote address cannot tell a public request from a tunnel-local
one. The `Host` header decides, and the two sets never overlap:

| Host                               | login                                                     |
|------------------------------------|-----------------------------------------------------------|
| `127.0.0.1[:port]` (SSH tunnel)    | unchanged: `hands-dash url` code → cookie. The Access token is ignored here. |
| a host listed in `public.json`     | a valid Access token on **every** request (pages, static, API, WebSocket upgrade). No cookie, no `/login` (404). |
| anything else                      | `403 forbidden host` (as before)                          |

Without a valid `public.json`, public access is OFF and every non-local Host is 403: exactly the 0.5.2
behaviour. CSRF on a public host: `Origin` must be `https://<that host>` (plus `X-Hands: 1`); the local rule
(`http://127.0.0.1:port`) is unchanged. The request log (`~/.config/hands/dashboard.log`) records the token's
email on public requests, never the token; refusals are logged with their short reason.

Token verification is stdlib only and fails closed:

- compact JWS; header `alg` must be exactly `RS256` (`none`, `HS*`, `RS512`... are refused); `kid` must name
  a key in `https://<team_domain>/cdn-cgi/access/certs` (`keys`, JWK). Keys are cached 1 h; an unknown
  `kid` triggers one refetch at most once per minute (Access rotates keys every 6 weeks, old keys stay
  valid 7 days); a fetch failure with nothing cached refuses the token.
- RSASSA-PKCS1-v1_5 / SHA-256 computed with `pow()`; the full EMSA-PKCS1-v1_5 encoding is compared byte
  for byte (never parsed); keys under 2048 bits or with an even exponent are unusable.
- claims: `aud` (string or list) contains the configured AUD, `iss` == `https://<team_domain>`, `exp` in
  the future (60 s leeway), `nbf`/`iat` not in the future, `email` present and in the configured list
  (case-insensitive). Service tokens (no `email`) are refused.

Difference from Cloudflare's "Validate JWTs" page (checked 2026-10-18): Cloudflare documents the header
and cookie names, the certs URL, RS256, matching `kid` against the certs (not reading `public_cert`, which
may be stale), and verifying `iss` and `aud`. It does not spell out `exp`/`nbf`/`iat` or the email check;
Phoenix adds them (and the allowlist, which duplicates the Access policy on purpose: defence in depth).

## Cloudflare setup (once per VPS; operator, in the Cloudflare dashboard and as root)

1. **Domain on Cloudflare.** The zone (e.g. `example.com`) must use Cloudflare DNS (proxied).
2. **Zero Trust team.** Zero Trust → Settings → Custom Pages shows the team domain
   `<team>.cloudflareaccess.com`. Note it.
3. **Tunnel.** Zero Trust → Networks → Tunnels → Create (cloudflared). Install `cloudflared` on the VPS as
   root with the connector token shown, as a systemd service (`cloudflared service install <token>`).
   Add a public hostname: `phoenix.example.com` → service **HTTP** `127.0.0.1:8090`. Leave "HTTP Host
   Header" empty so the origin sees the public host name (Phoenix needs it to tell the request is public).
   cloudflared is the only process other than the SSH tunnel that reaches 8090; ufw stays closed.
4. **Access application.** Zero Trust → Access → Applications → Add → Self-hosted. Application domain
   `phoenix.example.com`. Session duration as you like (the token's `exp`; Phoenix enforces it). Policy:
   Allow, include **Emails** = the allowed address(es). Identity provider: One-time PIN is enough.
   After saving, open the application → Overview and copy the **Application Audience (AUD) Tag** (64 hex
   characters).
5. **WebSockets** are on by default for proxied zones (Network → WebSockets); the Session manager needs them.

## Turning it on in Phoenix (builder)

```
hands-dash public set --host phoenix.example.com --team <team>.cloudflareaccess.com \
    --aud <AUD tag> --email you@example.com [--email second@example.com]
hands-dash public test        # validates public.json and fetches the team's signing keys
hands-dash public show        # prints the config (the AUD is not secret)
hands-dash public off         # back to 403 for every non-local host
```

`set` validates the input and writes `~/.config/hands/public.json` (0600). The running dashboard re-reads
the file whenever it changes (mtime/size/inode check on each request), so **no restart is needed** after
`set` or `off`. An invalid or empty file turns public access OFF and writes one line to `dashboard.log`.
`--host` and `--email` may be repeated (8 hosts, 32 emails max).

## Checking it

- `https://phoenix.example.com/` → Cloudflare's PIN page → the app, no Phoenix login link needed.
- `curl -sI https://phoenix.example.com/api/projects` (no token, from anywhere) → Cloudflare answers 302
  to the login page; the request never reaches Phoenix.
- On the VPS: `curl -s -H 'Host: phoenix.example.com' http://127.0.0.1:8090/api/projects` → `403 forbidden:
  access token missing or too long` (the public host without Cloudflare in front).
- `tail ~/.config/hands/dashboard.log` shows `127.0.0.1 you@example.com GET /api/projects 200 …` for public
  requests and `access refused: <reason>` for refusals.
- The SSH tunnel plus `hands-dash url` works exactly as before.

## What it does not do

- It does not replace the Access policy: keep the email list in Cloudflare tight too.
- It does not accept the `CF_Authorization` cookie, service tokens or `alg` values other than RS256.
- It does not make Phoenix listen on anything but 127.0.0.1.
