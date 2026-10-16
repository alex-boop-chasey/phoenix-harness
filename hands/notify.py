"""Telegram notifications from the host. Token file: TG_TOKEN=..., TG_CHAT_ID=... (chmod 600)."""
from __future__ import annotations

import json
import os
import stat
import urllib.parse
import urllib.request
from pathlib import Path


def _read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Notifier:
    def __init__(self, env_file: Path | None, prefix: str, log=print):
        self.prefix = prefix
        self.log = log
        self.token = self.chat = None
        if env_file and env_file.is_file():
            mode = env_file.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                log(f"WARN: {env_file} is readable by group/others; chmod 600 it")
            env = _read_env(env_file)
            self.token, self.chat = env.get("TG_TOKEN"), env.get("TG_CHAT_ID")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, msg: str) -> bool:
        if not self.enabled or os.environ.get("HANDS_NO_NOTIFY"):
            return False
        text = f"[{self.prefix}] {msg}"[:3900]
        data = urllib.parse.urlencode({"chat_id": self.chat, "text": text, "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage", data, timeout=15) as r:
                ok = json.loads(r.read()).get("ok", False)
                if not ok:
                    self.log("WARN: telegram returned ok=false")
                return ok
        except Exception as e:  # never let notifications break the build
            self.log(f"WARN: telegram send failed: {type(e).__name__}")
            return False
