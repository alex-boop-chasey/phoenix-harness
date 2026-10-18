"""Public access to the dashboard through Cloudflare Access (0.5.3, step 18).

A Cloudflare Tunnel (cloudflared, owned by the operator) forwards https://<public host> to the dashboard on
127.0.0.1:8090 and Cloudflare Access (one-time PIN, email allowlist) sits in front of it. cloudflared
connects from 127.0.0.1, so the remote address cannot tell a local request from a public one: the Host
header decides, and on a public host a valid Access token (header Cf-Access-Jwt-Assertion) on EVERY request
is the login. The Phoenix cookie login is never accepted there, and the Access token is never accepted on a
local Host.

Config: ~/.config/hands/public.json (0600):
  {"hosts": ["phoenix.example.com"], "team_domain": "team.cloudflareaccess.com", "aud": "<app AUD tag>",
   "emails": ["alex@example.com"]}
Missing, empty or invalid file = public access OFF (any non-local Host -> 403, as before). The file is
re-read when it changes (mtime/size/inode), so `hands-dash public set|off` needs no dashboard restart.

Token verification (fail closed, stdlib only; follows developers.cloudflare.com "Validate JWTs"):
- compact JWS, header alg must be exactly RS256, kid must match a key in
  https://<team_domain>/cdn-cgi/access/certs ("keys", JWK); keys are cached for 1 h and refetched on an
  unknown kid at most once per minute; a fetch failure with nothing cached rejects the token
- signature RSASSA-PKCS1-v1_5 with SHA-256, computed with pow(): the FULL EMSA-PKCS1-v1_5 encoding is
  compared byte for byte (never parsed), key size >= 2048 bits, public exponent odd and > 1
- claims: aud contains the configured AUD, iss == https://<team_domain>, exp in the future (60 s leeway),
  nbf/iat (if present) not in the future beyond the leeway, email present and (case-insensitively) in
  the configured list. Service tokens (no email, common_name instead) are therefore refused.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
import urllib.request
from pathlib import Path

from . import config as cfgmod

FILE = "public.json"
LEEWAY = 60
KEYS_TTL = 3600
REFETCH_MIN = 60
MIN_BITS = 2048
MAX_TOKEN = 8192
MAX_HOSTS = 8
MAX_EMAILS = 32
JWKS_PATH = "/cdn-cgi/access/certs"
HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+(?::\d{1,5})?$")
TEAM = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+$")
AUD = re.compile(r"^[A-Za-z0-9._-]{8,256}$")
EMAIL = re.compile(r"^[^\s@]{1,128}@[^\s@]{1,253}$")
KID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# DigestInfo prefix for SHA-256 (RFC 8017 section 9.2, note 1)
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class AccessError(Exception):
    """Token or configuration refused; str() is a short reason safe to show the client."""


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------- configuration
class Config:
    __slots__ = ("hosts", "team_domain", "aud", "emails")

    def __init__(self, hosts, team_domain, aud, emails):
        self.hosts, self.team_domain, self.aud, self.emails = hosts, team_domain, aud, emails

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def jwks_url(self) -> str:
        return f"https://{self.team_domain}{JWKS_PATH}"

    def as_dict(self) -> dict:
        return {"hosts": list(self.hosts), "team_domain": self.team_domain, "aud": self.aud, "emails": list(self.emails)}


def validate(d) -> Config:
    """A Config from a dict, or ConfigError naming the first problem."""
    if not isinstance(d, dict):
        raise ConfigError("public.json must be a JSON object")
    hosts = d.get("hosts")
    if not isinstance(hosts, list) or not hosts or len(hosts) > MAX_HOSTS:
        raise ConfigError(f"hosts: 1-{MAX_HOSTS} public host names")
    out_hosts = []
    for h in hosts:
        if not isinstance(h, str) or not HOST.match(h.strip().lower()):
            raise ConfigError(f"hosts: bad host name {str(h)[:80]!r} (lowercase DNS name, optional :port)")
        h = h.strip().lower()
        if h.split(":")[0] in ("localhost",) or h.startswith("127."):
            raise ConfigError("hosts: a public host cannot be localhost or 127.x")
        if h not in out_hosts:
            out_hosts.append(h)
    team = d.get("team_domain")
    if not isinstance(team, str) or not TEAM.match(team.strip().lower()):
        raise ConfigError("team_domain: a DNS name such as myteam.cloudflareaccess.com")
    team = team.strip().lower()
    aud = d.get("aud")
    if not isinstance(aud, str) or not AUD.match(aud.strip()):
        raise ConfigError("aud: the Access application's Audience (AUD) tag")
    aud = aud.strip()
    emails = d.get("emails")
    if not isinstance(emails, list) or not emails or len(emails) > MAX_EMAILS:
        raise ConfigError(f"emails: 1-{MAX_EMAILS} allowed email addresses")
    out_emails = []
    for e in emails:
        if not isinstance(e, str) or not EMAIL.match(e.strip()):
            raise ConfigError(f"emails: bad address {str(e)[:80]!r}")
        e = e.strip().lower()
        if e not in out_emails:
            out_emails.append(e)
    return Config(tuple(out_hosts), team, aud, tuple(out_emails))


def path() -> Path:
    return cfgmod.CONFIG_HOME / FILE


def _read(p: Path) -> bytes | None:
    """File bytes (regular file, never through a symlink, bounded), or None when there is no usable file."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 65536:
            return None
        data = b""
        while len(data) < 65536:
            chunk = os.read(fd, 65536 - len(data))
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(fd)


def load(p: Path | None = None) -> Config:
    """The current configuration; ConfigError if missing/empty/invalid (= public access OFF)."""
    data = _read(p or path())
    if data is None:
        raise ConfigError("no public.json")
    if not data.strip():
        raise ConfigError("public.json is empty")
    try:
        d = json.loads(data)
    except ValueError as e:
        raise ConfigError(f"public.json is not valid JSON: {e}")
    return validate(d)


def save(cfg: Config, p: Path | None = None) -> Path:
    p = p or path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg.as_dict(), f, indent=1)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return p


def remove(p: Path | None = None) -> bool:
    p = p or path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


class Settings:
    """Config cache that re-reads public.json when the file changes (stat per lookup; no restart needed)."""

    def __init__(self, p: Path | None = None, log=None):
        self._path = p
        self._log = log or (lambda s: None)
        self._lock = threading.Lock()
        self._stamp = None
        self._cfg: Config | None = None
        self._err = ""

    def _file_stamp(self):
        try:
            st = os.lstat(self._path or path())
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)

    def current(self) -> Config | None:
        """The valid Config, or None when public access is off (file missing, empty or invalid)."""
        stamp = self._file_stamp()
        with self._lock:
            if stamp != self._stamp or self._stamp is None:
                self._stamp = stamp
                try:
                    self._cfg, self._err = load(self._path), ""
                except ConfigError as e:
                    self._cfg, self._err = None, str(e)
                    if stamp is not None:
                        self._log(f"public access OFF: {e}")
            return self._cfg

    @property
    def error(self) -> str:
        return self._err


# ---------------------------------------------------------------- JWKS
def default_fetch(url: str, timeout: float = 10.0) -> bytes:
    if not url.startswith("https://"):
        raise AccessError("jwks url must be https")
    req = urllib.request.Request(url, headers={"User-Agent": "hands-dash", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (https only, checked above)
        return r.read(262144)


def parse_jwks(data: bytes) -> dict[str, tuple[int, int]]:
    """kid -> (n, e) for every usable RSA signing key in a JWKS document."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise AccessError("jwks: not json")
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list):
        raise AccessError("jwks: no keys")
    out = {}
    for k in keys:
        if not isinstance(k, dict) or k.get("kty") != "RSA":
            continue
        if k.get("alg") not in (None, "RS256") or k.get("use") not in (None, "sig"):
            continue
        kid = k.get("kid")
        if not isinstance(kid, str) or not KID.match(kid):
            continue
        try:
            n = int.from_bytes(b64url_decode(k["n"]), "big")
            e = int.from_bytes(b64url_decode(k["e"]), "big")
        except (KeyError, TypeError, AccessError):
            continue
        if n.bit_length() < MIN_BITS or e < 3 or e % 2 == 0:
            continue
        out[kid] = (n, e)
    if not out:
        raise AccessError("jwks: no usable RSA keys")
    return out


class KeyCache:
    """Signing keys per team domain: cached KEYS_TTL, refetched for an unknown kid at most once per REFETCH_MIN."""

    def __init__(self, fetch=default_fetch, clock=time.monotonic):
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[int, int]] = {}
        self._url = ""
        self._fetched = -1e18
        self._attempt = -1e18

    def _refresh(self, url: str, reason: str) -> None:
        self._attempt = self._clock()
        try:
            keys = parse_jwks(self._fetch(url))
        except AccessError:
            raise
        except Exception as e:                       # network, TLS, HTTP: all mean "no fresh keys"
            raise AccessError(f"jwks fetch failed ({type(e).__name__})")
        self._keys, self._url, self._fetched = keys, url, self._clock()

    def key(self, url: str, kid: str) -> tuple[int, int]:
        with self._lock:
            now = self._clock()
            if url != self._url:
                self._keys, self._fetched = {}, -1e18
            if not self._keys or now - self._fetched > KEYS_TTL:
                try:
                    self._refresh(url, "stale")
                except AccessError:
                    if not self._keys:
                        raise
            k = self._keys.get(kid)
            if k is None and now - self._attempt >= REFETCH_MIN:
                self._refresh(url, "unknown kid")
                k = self._keys.get(kid)
            if k is None:
                raise AccessError("unknown signing key")
            return k

    def count(self, url: str) -> int:
        with self._lock:
            self._refresh(url, "test")
            return len(self._keys)


# ---------------------------------------------------------------- JWT
def b64url_decode(s) -> bytes:
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", s):
        raise AccessError("bad base64url")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        raise AccessError("bad base64url")


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def emsa_pkcs1_v15_sha256(message: bytes, k: int) -> bytes:
    """EMSA-PKCS1-v1_5 encoding of SHA-256(message) at k bytes (RFC 8017 section 9.2)."""
    t = SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise AccessError("key too short")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def rsa_verify(n: int, e: int, signature: bytes, message: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 / SHA-256 with pow(): the whole expected encoding is compared, nothing is parsed."""
    if n.bit_length() < MIN_BITS or e < 3 or e % 2 == 0:
        return False
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s <= 1 or s >= n - 1:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    return hmac.compare_digest(em, emsa_pkcs1_v15_sha256(message, k))


def _json_part(part: str, what: str) -> dict:
    try:
        obj = json.loads(b64url_decode(part))
    except ValueError:
        raise AccessError(f"bad {what}")
    if not isinstance(obj, dict):
        raise AccessError(f"bad {what}")
    return obj


def verify(token: str, cfg: Config, keys: KeyCache, now: float | None = None) -> str:
    """The allowlisted email the token proves, or AccessError. Every check fails closed."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN:
        raise AccessError("access token missing or too long")
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise AccessError("malformed access token")
    header = _json_part(parts[0], "token header")
    if header.get("alg") != "RS256" or not isinstance(header.get("alg"), str):
        raise AccessError("token alg not RS256")
    if header.get("typ") not in (None, "JWT"):
        raise AccessError("token typ not JWT")
    kid = header.get("kid")
    if not isinstance(kid, str) or not KID.match(kid):
        raise AccessError("token kid missing")
    n, e = keys.key(cfg.jwks_url, kid)
    if not rsa_verify(n, e, b64url_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii")):
        raise AccessError("token signature invalid")
    claims = _json_part(parts[1], "token claims")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not any(isinstance(a, str) and hmac.compare_digest(a, cfg.aud) for a in auds):
        raise AccessError("token audience mismatch")
    if claims.get("iss") != cfg.issuer:
        raise AccessError("token issuer mismatch")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + LEEWAY <= now:
        raise AccessError("token expired")
    for name in ("nbf", "iat"):
        v = claims.get(name)
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v - LEEWAY > now:
            raise AccessError(f"token {name} in the future")
    if isinstance(claims.get("iat"), (int, float)) and exp - claims["iat"] > 90 * 86400:
        raise AccessError("token lifetime implausible")
    email = claims.get("email")
    if not isinstance(email, str) or not EMAIL.match(email.strip()):
        raise AccessError("token has no email")
    email = email.strip().lower()
    if not any(hmac.compare_digest(email, allowed) for allowed in cfg.emails):
        raise AccessError("email not allowed")
    return email
