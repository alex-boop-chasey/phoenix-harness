"""Telegram notifications from the host. Token file: TG_TOKEN=..., TG_CHAT_ID=... (chmod 600)."""
from __future__ import annotations

import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .projectenv import Redactor

ATTEMPTS = 3
BACKOFF_S = (2.0, 5.0)      # wait before attempt 2, before attempt 3
TIMEOUT_S = 15


def _read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Notifier:
    """Best-effort: send() never raises, and a failed notification never fails the runner. Every line it
    logs goes through a redactor seeded with the bot token (urllib puts the request URL, which contains
    the token, into some error messages)."""

    def __init__(self, env_file: Path | None, prefix: str, log=print, sleep=time.sleep, urlopen=None):
        self.prefix = prefix
        self._log = log
        self._sleep = sleep
        self._urlopen = urlopen or urllib.request.urlopen
        self.token = self.chat = None
        if env_file and env_file.is_file():
            mode = env_file.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                log(f"WARN: {env_file} is readable by group/others; chmod 600 it")
            env = _read_env(env_file)
            self.token, self.chat = env.get("TG_TOKEN"), env.get("TG_CHAT_ID")
        self._redactor = Redactor({"TG_TOKEN": self.token} if self.token else {})

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def log(self, msg: str) -> None:
        try:
            self._log(self._redactor.text(msg))
        except Exception:
            pass

    def send(self, msg: str) -> bool:
        if not self.enabled or os.environ.get("HANDS_NO_NOTIFY"):
            return False
        try:
            return self._send(msg)
        except Exception as e:  # never let notifications break the build
            self.log(f"WARN: telegram send failed: {_describe(e)}")
            return False

    def _send(self, msg: str) -> bool:
        text = f"[{self.prefix}] {msg}"[:3900]
        data = urllib.parse.urlencode({"chat_id": self.chat, "text": text, "disable_web_page_preview": "true"}).encode()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        reasons = []
        for attempt in range(1, ATTEMPTS + 1):
            wait = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]      # pause before the next attempt
            try:
                with self._urlopen(url, data, timeout=TIMEOUT_S) as r:
                    if json.loads(r.read()).get("ok", False):
                        return True
                    reasons.append("ok=false")
            except urllib.error.HTTPError as e:
                reasons.append(_describe(e))
                if e.code == 429:
                    wait = max(wait, _retry_after(e))
                elif 400 <= e.code < 500:
                    break               # our request is wrong; a retry cannot help
            except Exception as e:      # URLError (DNS, refused, timeout), socket errors, bad JSON
                reasons.append(_describe(e))
            if attempt < ATTEMPTS and wait:
                self._sleep(wait)
        self.log(f"WARN: telegram send failed after {len(reasons)} attempt(s): " + "; ".join(reasons))
        return False


def _retry_after(e: urllib.error.HTTPError) -> float:
    """Telegram's 429: Retry-After header or parameters.retry_after in the body; capped so a runner never stalls."""
    v = e.headers.get("Retry-After") if e.headers else None
    if v is None:
        try:
            v = json.loads(e.read()).get("parameters", {}).get("retry_after")
        except Exception:
            v = None
    try:
        return min(max(float(v), 1.0), 30.0)
    except (TypeError, ValueError):
        return BACKOFF_S[-1]


def _describe(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTPError {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        return f"URLError {type(r).__name__}: {r}" if isinstance(r, BaseException) else f"URLError {r}"
    return f"{type(e).__name__}: {e}"[:200]
