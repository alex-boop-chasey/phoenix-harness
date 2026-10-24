"""Project configuration (TOML), one file per project: ~/.config/hands/projects/<name>.toml"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_HOME = Path(os.environ.get("HANDS_CONFIG_HOME", Path.home() / ".config" / "hands"))
RUNNER_HOME = Path(__file__).resolve().parent.parent  # install dir: ~/.local/share/hands/runner


def _p(v: str | None) -> Path | None:
    return Path(os.path.expanduser(v)) if v else None


@dataclass
class Config:
    name: str
    spec_dir: Path
    app_dir: Path
    state_dir: Path
    image: str
    branch: str = "main"                # app repo branch the runner builds on
    spec_branch: str = ""               # specs repo branch (default: same as branch; imported projects keep main)
    push: bool = True
    coder_model: str = "claude-fable-5-1"
    eval_model: str = "claude-fable-5-1"
    fallback_model: str = "claude-opus-5-5"
    max_sessions: int = 60              # coder + evaluator sessions per runner start
    cost_cap_usd: float = 0.0           # 0 = off; sum of total_cost_usd estimates per runner start
    session_timeout_min: int = 120
    accept_timeout_min: int = 45
    eval_timeout_min: int = 60
    max_turns: int = 250
    eval_max_turns: int = 120
    default_max_attempts: int = 3
    memory: str = "6g"
    cpus: str = "2"
    pids_limit: int = 4096
    claude_env: Path = field(default_factory=lambda: CONFIG_HOME / "claude.env")
    telegram_env: Path | None = field(default_factory=lambda: CONFIG_HOME / "telegram.env")
    project_env: Path | None = None     # dev/test secrets passed to coder, acceptance and evaluator
    skills_dir: Path | None = field(default_factory=lambda: RUNNER_HOME / "image" / "skills")
    pnpm_volume: str = ""               # coder-only cache volume; default hands-<project>-coder-store
    tz: str = "Australia/Brisbane"       # container TZ; also used to read "resets 3pm" messages
    min_free_gb: int = 8
    rate_limit_probe_s: int = 1800
    rate_limit_max_wait_s: int = 43200
    progress_notes: int = 12            # how many past step notes go into the coder prompt
    diff_limit_chars: int = 60000
    docker_bin: str = "docker"
    planner_model: str = ""              # default: coder_model
    resolver_model: str = ""             # default: eval_model
    planner_max_turns: int = 300
    planner_timeout_min: int = 90
    planner_web_search: bool = True      # WebSearch (server-side); WebFetch is always off
    min_release_age_days: float = 3.0    # planned npm pins must be at least this old (pnpm 12 enforces >= 1 day)
    ai_kit: dict = field(default_factory=dict)   # [ai_kit] repo, ref, install_command for setup frameworks 3/5
    design_caps: dict = field(default_factory=dict)   # [design_caps] text, image, font, zip, video, submit (bytes)
    # set by `hands <p> create`; `destroy` only deletes GitHub repos of managed projects
    managed: bool = False
    imported: bool = False              # app repo existed before hands (never deleted by destroy)
    is_self: bool = False               # TOML `self = true`: Phoenix building itself (U4); rounds are merged by Install update, not the tap
    github_org: str = ""
    repo_specs: str = ""
    repo_app: str = ""

    @property
    def runner_home(self) -> Path:
        return RUNNER_HOME


def load(name: str, path: Path | None = None) -> Config:
    path = path or CONFIG_HOME / "projects" / f"{name}.toml"
    if not path.is_file():
        raise SystemExit(f"config not found: {path}")
    raw = tomllib.loads(path.read_text())
    if "self" in raw:
        raw["is_self"] = raw.pop("self")
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise SystemExit(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    for req in ("spec_dir", "app_dir", "state_dir", "image"):
        if req not in raw:
            raise SystemExit(f"{path}: missing required key '{req}'")
    for k in ("spec_dir", "app_dir", "state_dir", "claude_env", "telegram_env", "project_env", "skills_dir"):
        if k in raw:
            raw[k] = _p(raw[k])
    if raw["image"].endswith(":latest") or ":" not in raw["image"]:
        raise SystemExit(f"{path}: image must be a pinned tag, not '{raw['image']}'")
    cfg = Config(name=name, **raw)
    if not cfg.pnpm_volume:
        cfg.pnpm_volume = f"hands-{name}-coder-store"
    if not cfg.spec_branch:
        cfg.spec_branch = cfg.branch
    for k in ("spec_dir", "app_dir", "state_dir"):
        setattr(cfg, k, getattr(cfg, k).resolve())
    return cfg
