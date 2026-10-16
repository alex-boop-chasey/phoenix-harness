"""Runner-owned state. Lives in state_dir on the host; never mounted into a container."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_text(data)
    os.replace(tmp, path)


class State:
    """state.json: {status, reason, model_override, steps:{id:{status, attempts, commit, reasons, ...}}}"""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.data: dict[str, Any] = {"status": "idle", "reason": "", "steps": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            self.data.setdefault("steps", {})

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        atomic_write(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    # status: idle | running | paused_rate_limit | escalation | done | stopped
    def set_status(self, status: str, reason: str = "") -> None:
        self.data["status"] = status
        self.data["reason"] = reason
        self.save()

    @property
    def status(self) -> str:
        return self.data.get("status", "idle")

    def step(self, sid: str) -> dict[str, Any]:
        return self.data["steps"].setdefault(sid, {"status": "pending", "attempts": 0, "reasons": []})

    def passed(self, sid: str) -> bool:
        return self.data["steps"].get(sid, {}).get("status") == "passed"

    # ---- append-only logs
    def append_jsonl(self, name: str, rec: dict[str, Any]) -> None:
        rec = {"ts": now_iso(), **rec}
        with open(self.dir / name, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    def read_jsonl(self, name: str) -> list[dict[str, Any]]:
        p = self.dir / name
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def log(self, msg: str) -> None:
        line = f"{now_iso()} {msg}"
        print(line, flush=True)
        with open(self.dir / "runner.log", "a") as f:
            f.write(line + "\n")


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        deadline = time.monotonic() + 3      # Lock.held() probes (dashboard, watchdog) hold it for microseconds
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() > deadline:
                    self.fd.close()
                    raise SystemExit(f"another runner holds {self.path}")
                time.sleep(0.1)

    def __exit__(self, *a):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()

    @staticmethod
    def held(path: Path) -> bool:
        if not path.exists():
            return False
        with open(path, "a") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
