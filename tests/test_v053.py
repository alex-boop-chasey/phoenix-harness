"""0.5.3 (step 18): public access through Cloudflare Access (hands/publicaccess.py) and request bodies on
kept-alive connections. No network: the JWKS and every token come from the fixed test key."""
import base64
import hashlib
import http.client
import io
import json
import os
import re
import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"

from hands import dashboard, publicaccess, sessions, ws  # noqa: E402
from test_v04 import Dash  # noqa: E402
import access_fixture as KEY  # noqa: E402

TEAM = "unit.cloudflareaccess.com"
AUD = "a" * 64
HOST = "phoenix.example.test"
EMAIL = "Alex@Example.test"
KID = "test-key-1"
JWKS_URL = f"https://{TEAM}/cdn-cgi/access/certs"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwk(kid=KID, n=KEY.N, e=KEY.E) -> dict:
    return {"kid": kid, "kty": "RSA", "alg": "RS256", "use": "sig",
            "n": b64(n.to_bytes((n.bit_length() + 7) // 8, "big")), "e": b64(e.to_bytes(3, "big"))}


JWKS = json.dumps({"keys": [jwk()], "public_cert": {"kid": KID, "cert": "-----BEGIN..."}, "public_certs": []}).encode()


def sign(signing_input: bytes, n=KEY.N, d=KEY.D) -> bytes:
    k = (n.bit_length() + 7) // 8
    em = publicaccess.emsa_pkcs1_v15_sha256(signing_input, k)
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def token(claims=None, header=None, *, bad_sig=False, **over) -> str:
    now = int(time.time())
    c = {"aud": [AUD], "iss": f"https://{TEAM}", "exp": now + 600, "iat": now - 5, "nbf": now - 5,
         "email": EMAIL, "sub": "x", "type": "app", "identity_nonce": "n", "country": "AU"}
    c.update(claims or {})
    keep_sig = over.pop("_keep_sig", False)
    for k, v in over.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    h = {"alg": "RS256", "kid": KID, "typ": "JWT"}
    h.update(header or {})
    si = f"{b64(json.dumps(h).encode())}.{b64(json.dumps(c).encode())}"
    sig = sign(si.encode())
    if bad_sig:
        sig = bytes([sig[0] ^ 1]) + sig[1:]
    if h.get("alg") == "none":
        return si + "." + (b64(sig) if keep_sig else "")
    if h.get("alg") == "HS256":
        import hmac
        return si + "." + b64(hmac.new(b"secret", si.encode(), hashlib.sha256).digest())
    return si + "." + b64(sig)


class FakeFetch:
    def __init__(self, data=JWKS, fail=False):
        self.data, self.fail, self.calls = data, fail, []

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("no network")
        return self.data


def cfg(**over) -> publicaccess.Config:
    d = {"hosts": [HOST], "team_domain": TEAM, "aud": AUD, "emails": [EMAIL]}
    d.update(over)
    return publicaccess.validate(d)


# ---------------------------------------------------------------- unit: token verification
class TestVerify(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeFetch()
        self.keys = publicaccess.KeyCache(fetch=self.fetch)
        self.cfg = cfg()

    def ok(self, tok):
        return publicaccess.verify(tok, self.cfg, self.keys)

    def bad(self, tok, reason):
        with self.assertRaises(publicaccess.AccessError) as cm:
            self.ok(tok)
        self.assertIn(reason, str(cm.exception))
        if tok:
            self.assertNotIn(tok[-20:], str(cm.exception))

    def test_valid_token_gives_lowercased_email(self):
        self.assertEqual(self.ok(token()), "alex@example.test")
        self.assertEqual(self.ok(token(aud=AUD)), "alex@example.test")        # string aud too
        self.assertEqual(self.fetch.calls, [JWKS_URL])                       # one JWKS fetch, cached

    def test_rejects_every_wrong_claim(self):
        self.bad(token(aud=["b" * 64]), "audience")
        self.bad(token(aud=[AUD + "x"]), "audience")
        self.bad(token(iss="https://other.cloudflareaccess.com"), "issuer")
        self.bad(token(iss=f"http://{TEAM}"), "issuer")
        self.bad(token(exp=int(time.time()) - 120), "expired")
        self.bad(token(exp=None), "expired")
        self.bad(token(exp="9999999999"), "expired")
        self.bad(token(nbf=int(time.time()) + 600), "nbf")
        self.bad(token(iat=int(time.time()) + 600), "iat")
        self.bad(token(email="mallory@example.test"), "not allowed")
        self.bad(token(email=None), "no email")
        self.bad(token(email=None, common_name="service-token-id"), "no email")   # service tokens refused
        self.assertEqual(self.ok(token(exp=int(time.time()) + 30)), "alex@example.test")   # inside leeway

    def test_rejects_alg_none_hs256_and_others(self):
        self.bad(token(header={"alg": "none"}), "malformed")                 # empty signature part
        self.bad(token(header={"alg": "none"}, _keep_sig=True), "alg")       # alg none with a real-looking signature
        self.bad(token(header={"alg": "HS256"}), "alg")
        self.bad(token(header={"alg": "RS512"}), "alg")
        self.bad(token(header={"alg": "rs256"}), "alg")
        self.bad(token(header={"alg": ["RS256"]}), "alg")

    def test_bad_signature_unknown_kid_and_malformed(self):
        self.bad(token(bad_sig=True), "signature")
        t = token()
        h, c, s = t.split(".")
        other = b64(json.dumps({**json.loads(publicaccess.b64url_decode(c)), "email": "mallory@example.test"}).encode())
        self.bad(f"{h}.{other}.{s}", "signature")                          # claims swapped under a good signature
        self.bad(f"{h}.{c}.{s[:-4]}", "signature")                         # truncated signature
        self.bad(f"{h}.{c}.{s}" + "A" * 4, "signature")
        self.bad(token(header={"kid": "rotated-away"}), "unknown signing key")
        self.bad(token(header={"kid": None}), "kid")
        self.bad("", "missing")
        self.bad("a.b", "malformed")
        self.bad("a.b.c.d", "malformed")
        self.bad("not base64!.x.y", "bad")
        self.bad(f"{b64(b'[]')}.{c}.{s}", "bad token header")
        self.bad("x" * 9000, "too long")

    def test_unknown_kid_refetches_once_per_minute_then_uses_new_key(self):
        clock = [1000.0]
        fetch = FakeFetch()
        keys = publicaccess.KeyCache(fetch=fetch, clock=lambda: clock[0])
        publicaccess.verify(token(), self.cfg, keys)
        self.assertEqual(len(fetch.calls), 1)
        t2 = token(header={"kid": "test-key-2"})
        with self.assertRaises(publicaccess.AccessError):
            publicaccess.verify(t2, self.cfg, keys)                            # fetched again (new kid), still unknown
        self.assertEqual(len(fetch.calls), 1)                                  # refetch suppressed: last fetch < 60 s ago
        clock[0] += 61
        with self.assertRaises(publicaccess.AccessError):
            publicaccess.verify(t2, self.cfg, keys)
        self.assertEqual(len(fetch.calls), 2)
        with self.assertRaises(publicaccess.AccessError):
            publicaccess.verify(t2, self.cfg, keys)                            # no third fetch within the minute
        self.assertEqual(len(fetch.calls), 2)
        clock[0] += 61
        fetch.data = json.dumps({"keys": [jwk(), jwk(kid="test-key-2")]}).encode()
        self.assertEqual(publicaccess.verify(t2, self.cfg, keys), "alex@example.test")
        self.assertEqual(len(fetch.calls), 3)

    def test_fetch_failure_without_cache_rejects_with_cache_keeps_working(self):
        fetch = FakeFetch(fail=True)
        keys = publicaccess.KeyCache(fetch=fetch)
        self.bad_with(keys, token(), "jwks fetch failed")
        clock = [0.0]
        fetch = FakeFetch()
        keys = publicaccess.KeyCache(fetch=fetch, clock=lambda: clock[0])
        publicaccess.verify(token(), self.cfg, keys)
        fetch.fail = True
        clock[0] += publicaccess.KEYS_TTL + 1                                  # cache stale, refresh fails: old keys stay
        self.assertEqual(publicaccess.verify(token(), self.cfg, keys), "alex@example.test")

    def bad_with(self, keys, tok, reason):
        with self.assertRaises(publicaccess.AccessError) as cm:
            publicaccess.verify(tok, self.cfg, keys)
        self.assertIn(reason, str(cm.exception))

    def test_jwks_parsing_skips_unusable_keys(self):
        small_n = (1 << 1023) | 1
        doc = {"keys": [jwk(kid="small", n=small_n), {"kty": "EC", "kid": "ec", "crv": "P-256"},
                        jwk(kid="even-e", e=65538), {**jwk(kid="hs"), "alg": "HS256"}, jwk()]}
        keys = publicaccess.parse_jwks(json.dumps(doc).encode())
        self.assertEqual(sorted(keys), [KID])
        for bad in (b"nope", b"[]", b'{"keys": {}}', b'{"keys": []}', b'{"public_cert": {}}'):
            with self.assertRaises(publicaccess.AccessError):
                publicaccess.parse_jwks(bad)

    def test_rsa_verify_compares_full_encoding(self):
        msg = b"hello"
        sig = sign(msg)
        self.assertTrue(publicaccess.rsa_verify(KEY.N, KEY.E, sig, msg))
        self.assertFalse(publicaccess.rsa_verify(KEY.N, KEY.E, sig, b"hellp"))
        self.assertFalse(publicaccess.rsa_verify(KEY.N, KEY.E, sig[1:], msg))
        self.assertFalse(publicaccess.rsa_verify(KEY.N, KEY.E, b"\x00" + sig, msg))
        self.assertFalse(publicaccess.rsa_verify(KEY.N, KEY.E, (KEY.N - 1).to_bytes(256, "big"), msg))
        self.assertFalse(publicaccess.rsa_verify(KEY.N, 65538, sig, msg))
        self.assertFalse(publicaccess.rsa_verify(KEY.N >> 1, KEY.E, sig[1:], msg))        # < 2048 bits
        # a forged encoding with the right hash but short padding (Bleichenbacher-style) must not pass:
        k = 256
        t = publicaccess.SHA256_PREFIX + hashlib.sha256(msg).digest()
        forged_em = b"\x00\x01\xff\x00" + t + b"\x00" * (k - 4 - len(t))
        forged_sig = pow(int.from_bytes(forged_em, "big"), KEY.D, KEY.N).to_bytes(k, "big")
        self.assertFalse(publicaccess.rsa_verify(KEY.N, KEY.E, forged_sig, msg))


# ---------------------------------------------------------------- unit: config
class TestConfig(unittest.TestCase):
    def test_validate(self):
        c = cfg(hosts=[" Phoenix.Example.Test ", HOST], emails=[EMAIL, EMAIL.lower()])
        self.assertEqual(c.hosts, (HOST,))
        self.assertEqual(c.emails, ("alex@example.test",))
        self.assertEqual(c.issuer, f"https://{TEAM}")
        self.assertEqual(c.jwks_url, JWKS_URL)
        for bad in ({"hosts": []}, {"hosts": ["127.0.0.1:8090"]}, {"hosts": ["localhost"]}, {"hosts": ["bad host"]},
                    {"hosts": ["single"]}, {"hosts": "x"}, {"team_domain": "https://x.cloudflareaccess.com"},
                    {"team_domain": ""}, {"aud": "short"}, {"aud": "a b"}, {"emails": []}, {"emails": ["nope"]},
                    {"emails": "a@b.c"}, {"hosts": [HOST] * 9}):
            with self.assertRaises(publicaccess.ConfigError, msg=str(bad)):
                cfg(**bad)
        with self.assertRaises(publicaccess.ConfigError):
            publicaccess.validate([])

    def test_settings_reload_on_change_and_off_when_invalid(self):
        d = Dash()
        try:
            msgs = []
            s = publicaccess.Settings(log=msgs.append)
            self.assertIsNone(s.current())                                     # missing = off
            p = publicaccess.save(cfg())
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
            self.assertEqual(s.current().hosts, (HOST,))
            p.write_text("{not json")
            os.utime(p, (1, 1))
            self.assertIsNone(s.current())
            self.assertIn("public access OFF", msgs[-1])
            p.write_text("")
            os.utime(p, (2, 2))
            self.assertIsNone(s.current())
            publicaccess.save(cfg(hosts=["other.example.test"]))
            self.assertEqual(s.current().hosts, ("other.example.test",))
            publicaccess.remove()
            self.assertIsNone(s.current())
            p.symlink_to(d.home / "elsewhere.json")
            (d.home / "elsewhere.json").write_text(json.dumps(cfg().as_dict()))
            self.assertIsNone(s.current(), "a symlinked public.json must not be followed")
        finally:
            d.close()


# ---------------------------------------------------------------- integration: the dashboard
class PublicDash(Dash):
    def __init__(self):
        super().__init__()
        self.fetch = FakeFetch()
        self.srv.keys = publicaccess.KeyCache(fetch=self.fetch)

    def raw(self, method, path, headers, body=b""):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request(method, path, headers=headers, body=body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def pub(self, method, path, tok="", *, host=HOST, origin=None, body=b"", xh=True, extra=None):
        h = {"Host": host, "Cf-Access-Jwt-Assertion": tok} if tok is not None else {"Host": host}
        if origin is not None:
            h["Origin"] = origin
        if xh and method == "POST":
            h["X-Hands"] = "1"
        h.update(extra or {})
        return self.raw(method, path, h, body)


class TestPublicHost(unittest.TestCase):
    def setUp(self):
        self.d = PublicDash()

    def tearDown(self):
        self.d.close()

    def on(self):
        publicaccess.save(cfg())

    def log(self):
        p = dashboard.log_path()
        return p.read_text() if p.exists() else ""

    def test_public_host_off_is_403_as_before(self):
        self.assertEqual(self.d.pub("GET", "/", token())[0], 403)
        self.assertEqual(self.d.pub("GET", "/api/projects", token())[0], 403)
        self.assertEqual(self.d.req("GET", "/api/projects", host="evil.example.test")[0], 403)
        self.assertEqual(self.d.fetch.calls, [], "no JWKS fetch while public access is off")

    def test_valid_token_is_the_login_on_every_kind_of_request(self):
        self.on()
        tok = token()
        st, hdr, body = self.d.pub("GET", "/", tok)
        self.assertEqual(st, 200)
        self.assertIn(b"<html", body.lower())
        self.assertEqual(self.d.pub("GET", "/app.js", tok)[0], 200)
        st, _, body = self.d.pub("GET", "/api/projects", tok)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body), [])
        st, _, body = self.d.pub("POST", "/api/sessions/new", tok, origin=f"https://{HOST}", body=b"{}")
        self.assertIn(st, (200, 400, 409, 500))                      # CSRF/login passed; whether claude starts is not the point
        self.assertNotEqual(st, 403)
        text = self.log()
        self.assertIn("alex@example.test GET /api/projects 200", text)
        self.assertNotIn(tok[-30:], text)
        self.assertNotIn(tok.split(".")[2][:30], text)

    def test_missing_or_invalid_token_is_403_without_detail_and_never_falls_back_to_cookie(self):
        self.on()
        for tok, reason in ((None, "missing"), ("", "missing"), (token(bad_sig=True), "signature"),
                            (token(exp=1), "expired"), (token(email="mallory@example.test"), "not allowed"),
                            (token(header={"alg": "none"}, _keep_sig=True), "alg"), (token(header={"alg": "HS256"}), "alg"),
                            (token(header={"kid": "nope"}), "unknown"), (token(aud=["b" * 64]), "audience"),
                            (token(iss="https://x.cloudflareaccess.com"), "issuer")):
            st, hdr, body = self.d.pub("GET", "/api/projects", tok)
            self.assertEqual(st, 403, reason)
            self.assertIn(reason, body.decode())
            self.assertLess(len(body), 120)
        # the Phoenix cookie is worthless on the public host, with or without a token
        st, _, _ = self.d.pub("GET", "/api/projects", None, extra={"Cookie": f"{dashboard.COOKIE}={self.d.sid}"})
        self.assertEqual(st, 403)
        st, _, _ = self.d.pub("GET", "/api/projects", token(bad_sig=True), extra={"Cookie": f"{dashboard.COOKIE}={self.d.sid}"})
        self.assertEqual(st, 403)
        # the login link does not exist on the public host, even with a valid token and a valid code
        code = dashboard.mint_login_code()
        st, hdr, _ = self.d.pub("GET", f"/login?c={code}", token())
        self.assertEqual(st, 404)
        self.assertNotIn("Set-Cookie", hdr)
        self.assertTrue(dashboard.redeem_login_code(code), "the code must not have been consumed")
        text = self.log()
        self.assertIn("access refused", text)
        self.assertNotIn(token()[:40], text)

    def test_local_host_ignores_the_access_token(self):
        self.on()
        tok = token()
        st, _, _ = self.d.pub("GET", "/api/projects", tok, host=f"127.0.0.1:{self.d.port}")
        self.assertEqual(st, 401)
        st, _, _ = self.d.pub("GET", "/", tok, host=f"127.0.0.1:{self.d.port}")
        self.assertEqual(st, 401)
        self.assertEqual(self.d.fetch.calls, [], "no JWKS fetch for a local request")
        self.assertEqual(self.d.req("GET", "/api/projects")[0], 200)        # cookie login unchanged
        self.assertEqual(self.d.req("GET", "/api/projects", host=f"localhost:{self.d.port}")[0], 403)
        self.assertEqual(self.d.req("GET", "/api/projects", host="other.example.test")[0], 403)

    def test_origin_on_public_host_must_be_https_host(self):
        self.on()
        tok = token()
        for origin in (f"http://{HOST}", f"https://evil.example.test", f"https://{HOST}:8443", "null", None):
            st, _, body = self.d.pub("POST", "/api/usage/refresh", tok, origin=origin)
            self.assertEqual(st, 403, origin)
            self.assertEqual(json.loads(body), {"error": "bad origin"})
        st, _, _ = self.d.pub("POST", "/api/usage/refresh", tok, origin=f"https://{HOST}", xh=False)
        self.assertEqual(st, 403)
        st, _, body = self.d.pub("POST", "/api/p/nope/unhide", tok, origin=f"https://{HOST}")
        self.assertEqual(st, 404)                                           # passed the CSRF check
        # local rules unchanged: http origin
        self.assertEqual(self.d.req("POST", "/api/p/nope/unhide", origin=f"http://127.0.0.1:{self.d.port}")[0], 404)
        self.assertEqual(self.d.req("POST", "/api/p/nope/unhide", origin=f"https://127.0.0.1:{self.d.port}")[0], 403)

    def ws_upgrade(self, path, headers):
        s = socket.create_connection(("127.0.0.1", self.d.port), timeout=10)
        try:
            lines = [f"GET {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
            s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            return data.split(b"\r\n")[0].decode()
        finally:
            s.close()

    def test_websocket_upgrade_on_public_host_needs_token_https_origin_and_matching_ticket(self):
        self.on()
        tok = token()
        sid = sessions.Store().create(self.d.home)["id"]
        st, _, body = self.d.pub("POST", f"/api/sessions/{sid}/ticket", tok, origin=f"https://{HOST}")
        self.assertEqual(st, 200)
        t_pub = json.loads(body)["ticket"]
        wsh = {"Host": HOST, "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
               "Sec-WebSocket-Key": base64.b64encode(b"0123456789abcdef").decode(), "Origin": f"https://{HOST}"}
        # no token on the upgrade: 403 even with a good ticket
        self.assertIn(" 403 ", self.ws_upgrade(f"/ws/sessions/{sid}?t={t_pub}", wsh))
        # bad token
        self.assertIn(" 403 ", self.ws_upgrade(f"/ws/sessions/{sid}?t={t_pub}", {**wsh, "Cf-Access-Jwt-Assertion": token(bad_sig=True)}))
        # http origin on the public host
        self.assertIn(" 403 ", self.ws_upgrade(f"/ws/sessions/{sid}?t={t_pub}", {**wsh, "Cf-Access-Jwt-Assertion": tok, "Origin": f"http://{HOST}"}))
        # a ticket minted by the local cookie login does not open a terminal through the public host
        st, _, body = self.d.req("POST", f"/api/sessions/{sid}/ticket", origin=f"http://127.0.0.1:{self.d.port}")
        self.assertEqual(st, 200)
        t_local = json.loads(body)["ticket"]
        self.assertIn(" 403 ", self.ws_upgrade(f"/ws/sessions/{sid}?t={t_local}", {**wsh, "Cf-Access-Jwt-Assertion": tok}))
        # the public ticket with the public token passes every pre-101 check; the session is not running -> 409
        self.assertIn(" 409 ", self.ws_upgrade(f"/ws/sessions/{sid}?t={t_pub}", {**wsh, "Cf-Access-Jwt-Assertion": tok}))
        # and that ticket is now spent
        self.assertIn(" 403 ", self.ws_upgrade(f"/ws/sessions/{sid}?t={t_pub}", {**wsh, "Cf-Access-Jwt-Assertion": tok}))

    def test_config_change_applies_without_restart(self):
        self.assertEqual(self.d.pub("GET", "/api/projects", token())[0], 403)
        self.on()
        self.assertEqual(self.d.pub("GET", "/api/projects", token())[0], 200)
        publicaccess.save(cfg(hosts=["other.example.test"]))
        self.assertEqual(self.d.pub("GET", "/api/projects", token())[0], 403)
        self.assertEqual(self.d.pub("GET", "/api/projects", token(), host="other.example.test")[0], 200)
        publicaccess.remove()
        self.assertEqual(self.d.pub("GET", "/api/projects", token(), host="other.example.test")[0], 403)


# ---------------------------------------------------------------- bug: leftover bodies on keep-alive connections
class TestKeepAliveBodies(unittest.TestCase):
    def setUp(self):
        self.d = Dash()

    def tearDown(self):
        self.d.close()

    def send(self, sock, method, path, body=b"", *, cookie=True, host=None, extra="", origin=None):
        h = (f"{method} {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{self.d.port}'}\r\n"
             f"Origin: {origin or f'http://127.0.0.1:{self.d.port}'}\r\nX-Hands: 1\r\n")
        if cookie:
            h += f"Cookie: {dashboard.COOKIE}={self.d.sid}\r\n"
        h += extra + f"Content-Length: {len(body)}\r\n\r\n"
        sock.sendall(h.encode() + body)
        return self.read(sock)

    def read(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                return None, data
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        n = int(re.search(rb"Content-Length: (\d+)", head).group(1))
        while len(rest) < n:
            chunk = sock.recv(65536)
            if not chunk:
                break
            rest += chunk
        return head.decode(), rest[:n]

    def conn(self):
        return socket.create_connection(("127.0.0.1", self.d.port), timeout=10)

    def status(self, head):
        return int(head.split()[1])

    def test_body_to_an_endpoint_that_ignores_it_then_next_request_works(self):
        s = self.conn()
        try:
            head, body = self.send(s, "POST", "/api/p/nope/unhide", b"{}")       # ignores its body -> 404
            self.assertEqual(self.status(head), 404)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertIsNotNone(head, "connection closed")
            self.assertEqual(self.status(head), 200, body)
            self.assertEqual(json.loads(body), [])
        finally:
            s.close()

    def test_body_on_401_403_404_405_and_get_paths_is_drained(self):
        s = self.conn()
        try:
            head, _ = self.send(s, "POST", "/api/sessions/new", b'{"cwd": "x"}', cookie=False)        # 401
            self.assertEqual(self.status(head), 401)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertEqual(self.status(head), 200, body)
            head, _ = self.send(s, "POST", "/api/sessions/new", b"{}", host="evil.example.test")       # 403 host
            self.assertEqual(self.status(head), 403)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertEqual(self.status(head), 200, body)
            head, _ = self.send(s, "POST", "/login", b"{}")                                             # 405
            self.assertEqual(self.status(head), 405)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertEqual(self.status(head), 200, body)
            head, _ = self.send(s, "GET", "/api/sessions", b"{}")                                       # GET with a body
            self.assertEqual(self.status(head), 200)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertEqual(self.status(head), 200, body)
            head, _ = self.send(s, "POST", "/api/p/nope/unhide", b"{}", origin="http://evil")             # 403 origin
            self.assertEqual(self.status(head), 403)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertEqual(self.status(head), 200, body)
        finally:
            s.close()

    def test_too_large_body_is_413_and_closes_chunked_is_refused(self):
        s = self.conn()
        try:
            big = b"x" * (dashboard.MAX_ENV_BODY + 1)
            s.sendall((f"POST /api/p/nope/unhide HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                       f"Cookie: {dashboard.COOKIE}={self.d.sid}\r\nContent-Length: {len(big)}\r\n\r\n").encode())
            head, body = self.read(s)
            self.assertEqual(self.status(head), 413)
            self.assertIn("Connection: close", head)
            try:
                s.sendall(big)
            except OSError:
                pass
            self.assertEqual(s.recv(10), b"")
        finally:
            s.close()
        s = self.conn()
        try:
            s.sendall((f"POST /api/p/nope/unhide HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                       f"Cookie: {dashboard.COOKIE}={self.d.sid}\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{{}}\r\n0\r\n\r\n").encode())
            head, body = self.read(s)
            self.assertEqual(self.status(head), 411)
            self.assertIn("Connection: close", head)
            self.assertEqual(s.recv(10), b"")
        finally:
            s.close()
        s = self.conn()
        try:
            s.sendall((f"POST /api/p/nope/unhide HTTP/1.1\r\nHost: 127.0.0.1:{self.d.port}\r\n"
                       f"Content-Length: abc\r\n\r\n").encode())
            head, body = self.read(s)
            self.assertEqual(self.status(head), 400)
            self.assertIn("Connection: close", head)
        finally:
            s.close()
        # a body over the route's cap (10 kB for most routes) is still 413, after it was drained, so the
        # connection stays usable
        s = self.conn()
        try:
            head, body = self.send(s, "POST", "/api/p/nope/unhide", b"x" * (dashboard.MAX_BODY + 1))
            self.assertEqual(self.status(head), 413)
            head, body = self.send(s, "GET", "/api/sessions")
            self.assertEqual(self.status(head), 200, body)
        finally:
            s.close()


# ---------------------------------------------------------------- CLI
class TestPublicCli(unittest.TestCase):
    def setUp(self):
        self.d = Dash()
        self.out = []

    def tearDown(self):
        self.d.close()

    def run_cli(self, *args):
        return dashboard.public_main(list(args), out=self.out.append)

    def test_set_show_off(self):
        self.assertEqual(self.run_cli("show"), 1)
        self.assertIn("OFF", self.out[-1])
        self.assertEqual(self.run_cli("set", "--host", HOST, "--host", "b.example.test", "--team", TEAM,
                                      "--aud", AUD, "--email", EMAIL, "--email", "two@example.test"), 0)
        self.assertIn("no restart", self.out[-1])
        p = publicaccess.path()
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(p.read_text()), {"hosts": [HOST, "b.example.test"], "team_domain": TEAM, "aud": AUD,
                                                     "emails": ["alex@example.test", "two@example.test"]})
        self.assertEqual(self.run_cli("show"), 0)
        self.assertIn(AUD, "\n".join(self.out))
        self.assertEqual(self.run_cli("set", "--host", "localhost", "--team", TEAM, "--aud", AUD, "--email", EMAIL), 2)
        self.assertIn("error:", self.out[-1])
        self.assertEqual(json.loads(p.read_text())["hosts"], [HOST, "b.example.test"], "a failed set keeps the old file")
        self.assertEqual(self.run_cli("off"), 0)
        self.assertFalse(p.exists())
        self.assertEqual(self.run_cli("off"), 0)
        self.assertEqual(dashboard.main(["public", "show"]), 1)

    def test_test_reports_key_count(self):
        publicaccess.save(cfg())
        old = publicaccess.default_fetch
        calls = []

        def fake(url, timeout=10.0):
            calls.append(url)
            return JWKS
        publicaccess.default_fetch = fake
        publicaccess.KeyCache.__init__.__defaults__ = (fake, time.monotonic)
        try:
            self.assertEqual(self.run_cli("test"), 0)
        finally:
            publicaccess.default_fetch = old
            publicaccess.KeyCache.__init__.__defaults__ = (old, time.monotonic)
        self.assertEqual(calls, [JWKS_URL])
        self.assertIn("1 RSA signing key", self.out[-1])
        self.assertTrue(JWKS_URL.startswith("https://"))

    def test_default_fetch_refuses_plain_http(self):
        with self.assertRaises(publicaccess.AccessError):
            publicaccess.default_fetch("http://example.test/certs")


if __name__ == "__main__":
    unittest.main()
