"""Launching hands containers. The container is the security boundary for every agent session."""
from __future__ import annotations

import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

ENTRY = "/opt/hands/entry.sh"


@dataclass
class Mount:
    src: Path | str          # host path or named volume
    dst: str
    ro: bool = True

    def arg(self) -> str:
        if isinstance(self.src, Path):
            src = self.src.resolve()
            if not src.exists():
                raise FileNotFoundError(f"bind source missing: {src}")
            spec = f"type=bind,src={src},dst={self.dst}"
        else:
            spec = f"type=volume,src={self.src},dst={self.dst}"
        if "," in str(self.src) or "," in self.dst:
            raise ValueError(f"comma in mount path: {spec}")
        return spec + (",readonly" if self.ro else "")


@dataclass
class Run:
    name: str
    role: str                               # coder | accept | eval | export | probe
    cmd: list[str]
    mounts: list[Mount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_files: list[Path] = field(default_factory=list)
    network: bool = True                    # False => --network none, no firewall needed
    stdin: Path | None = None
    stdout: Path | None = None
    stderr: Path | None = None
    timeout_s: int = 3600
    append_stdout: bool = False             # stdout (and stderr if the same file) appended, not truncated
    extra_args: list[str] = field(default_factory=list)  # more `docker run` flags (e.g. hands.hooks.mount_args)


def base_mounts(cfg: Config, spec_root: Path) -> list[Mount]:
    rh = cfg.runner_home
    m = [Mount(rh / "container" / "entry.sh", ENTRY)]
    domains = spec_root / "allowed-domains"
    if domains.is_file():
        m.append(Mount(domains, "/etc/hands/allowed-domains"))
    return m


def build_argv(cfg: Config, r: Run) -> list[str]:
    argv = [
        cfg.docker_bin, "run", "--rm", "-i", "--init",
        "--name", r.name,
        "--label", f"hands.project={cfg.name}",
        "--label", f"hands.role={r.role}",
        "--user", "0:0", "--entrypoint", ENTRY,
        "--shm-size", "1g",
        "-e", f"TZ={cfg.tz}",
        "--memory", cfg.memory, "--memory-swap", cfg.memory,
        "--cpus", cfg.cpus, "--pids-limit", str(cfg.pids_limit),
        "--security-opt", "no-new-privileges",
        "-e", f"HOST_UID={os.getuid()}", "-e", f"HOST_GID={os.getgid()}",
        "-e", f"HANDS_ROLE={r.role}",
        "-w", "/workspace",
    ]
    if r.network:
        argv += ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "-e", "HANDS_FIREWALL=1"]
    else:
        argv += ["--network", "none", "-e", "HANDS_FIREWALL=0"]
    for k, v in r.env.items():
        argv += ["-e", f"{k}={v}"]
    for f in r.env_files:
        argv += ["--env-file", str(f)]
    for m in r.mounts:
        argv += ["--mount", m.arg()]
    argv += r.extra_args
    argv += [cfg.image, *r.cmd]
    return argv


def run(cfg: Config, r: Run) -> int:
    """Run a container to completion. Returns exit code; 124 on timeout (container force-removed)."""
    argv = build_argv(cfg, r)
    stdin = open(r.stdin, "rb") if r.stdin else subprocess.DEVNULL
    stdout = open(r.stdout, "ab" if r.append_stdout else "wb") if r.stdout else subprocess.DEVNULL
    if r.stderr and r.stderr == r.stdout:
        stderr = subprocess.STDOUT
    else:
        stderr = open(r.stderr, "ab") if r.stderr else subprocess.DEVNULL
    try:
        p = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr)
        try:
            return p.wait(timeout=r.timeout_s)
        except subprocess.TimeoutExpired:
            kill(cfg, r.name)
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
            return 124
        except BaseException:
            kill(cfg, r.name)
            raise
    finally:
        for fh in (stdin, stdout, stderr):
            if hasattr(fh, "close"):
                fh.close()


def kill(cfg: Config, name: str) -> None:
    subprocess.run([cfg.docker_bin, "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cleanup_orphans(cfg: Config) -> int:
    out = subprocess.run(
        [cfg.docker_bin, "ps", "-aq", "--filter", f"label=hands.project={cfg.name}"],
        capture_output=True, text=True,
    ).stdout.split()
    for cid in out:
        subprocess.run([cfg.docker_bin, "rm", "-f", cid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return len(out)


def reachable(cfg: Config) -> bool:
    try:
        return subprocess.run([cfg.docker_bin, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_exists(cfg: Config) -> bool:
    return subprocess.run([cfg.docker_bin, "image", "inspect", cfg.image],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def unique_name(cfg: Config, role: str, tag: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in tag)
    return f"hands-{cfg.name}-{role}-{safe}-{int(time.time())}-{secrets.token_hex(3)}"


def volume_rm(cfg: Config, name: str) -> None:
    subprocess.run([cfg.docker_bin, "volume", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cleanup_volumes(cfg: Config) -> None:
    out = subprocess.run([cfg.docker_bin, "volume", "ls", "-q", "--filter", f"label=hands.project={cfg.name}"],
                         capture_output=True, text=True).stdout.split()
    for v in out:
        volume_rm(cfg, v)


def volume_create(cfg: Config, name: str) -> str:
    subprocess.run([cfg.docker_bin, "volume", "create", "--label", f"hands.project={cfg.name}", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return name
