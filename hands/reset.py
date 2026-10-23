"""Reset a project: delete what `create` recorded, then create it again with the same settings.

  hands <slug> reset --confirm <slug> [--wipe-env]      -- CLI, and the dashboard's "Reset project" tap

Only for a project hands created itself: `managed = true`, not `imported` (an existing app repo is never
deleted, so there would be nothing to recreate) and not `self` (Phoenix). Refused while the runner, the
scaffold, the planner or a create is running.

What the recreate replays (everything the New project form asks for):
  description.txt           the idea; also the GitHub repo descriptions and the planning chat's context
  next.json setup           framework + libraries (the scaffold runs again)
  next.json style_refs      the URLs (captured, analysed and frozen again)
  project.env + env.json    the project's variables, kept by default (--wipe-env starts empty)
Org, codeowner, image and models come from ~/.config/hands/new-project.toml, exactly as they did on the
first create. Everything else is new: repos, deploy keys, created.json, an empty state/ and an empty spec
repo. A reset is a new project under the same name, not a rewind: the build history, the plan and the app
code are gone, and the planner is not started.

The settings are staged in ~/.config/hands/creating/<slug>.reset/ (reset.json, plus a copy of the two
variable files: 0600 in a 0700 directory, under ~/.config/hands like every other project secret) before
anything is deleted, and the stage is removed once the recreate is done. A reset that fails halfway leaves
it behind and prints what exists; running the same command again uses the staged settings and skips straight
to the create half (destroying a half-created project first).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import config as cfgmod
from . import newproject
from .newproject import CreateError, say
from .state import Lock

SCHEMA = 1
STAGE_FILE = "reset.json"
# What `create` writes into <slug>.toml (hands.newproject.create): any other key in the old config was put
# there by hand and is not replayed, so a reset names those keys before it deletes anything.
CREATE_KEYS = {"managed", "imported", "github_org", "repo_specs", "repo_app", "spec_dir", "app_dir",
               "state_dir", "project_env", "image", "coder_model", "eval_model", "fallback_model",
               "max_sessions", "branch"}


def stage_dir(slug: str) -> Path:
    return newproject.creating_dir() / f"{slug}.reset"


def stage_file(slug: str) -> Path:
    return stage_dir(slug) / STAGE_FILE


def lock_path(slug: str) -> Path:
    """Held for the whole reset (destroy and create): `create`'s own lock is taken by run_create inside it."""
    return newproject.creating_dir() / f"{slug}.reset.lock"


def running(slug: str) -> bool:
    return Lock.held(lock_path(slug))


# ---------------------------------------------------------------- what may be reset
def refusal(cfg) -> str:
    """Why this project may not be reset now ('' when it may)."""
    from . import nextproject, planner
    if not cfg.managed:
        return f"{cfg.name} was not created by hands (managed = true missing)"
    if cfg.imported:
        return f"{cfg.name} was imported from an existing repo; that repo is never deleted, so there is nothing to recreate"
    if cfg.is_self:
        return f"{cfg.name} is Phoenix itself"
    if Lock.held(Path(cfg.state_dir) / "runner.lock"):
        return f"the build (or the scaffold) is running; hands {cfg.name} stop first"
    if Lock.held(planner.pdir(cfg) / "plan.lock"):
        return "the planner is running"
    if Lock.held(nextproject.refs_lock(cfg)):
        return "style refs are being processed"
    if Lock.held(newproject.lock_path(cfg.name)):
        return f"{cfg.name} is being created right now"
    return ""


# ---------------------------------------------------------------- the settings a recreate needs
def hand_written_keys(slug: str) -> list[str]:
    """Keys in the project's own <slug>.toml that `create` does not write (models and the image come from
    new-project.toml): a reset replays the form's settings, not these."""
    import tomllib
    try:
        raw = tomllib.loads((cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml").read_text())
    except (OSError, ValueError):
        return []
    return sorted(k for k in raw if k not in CREATE_KEYS)


def collect(cfg) -> dict:
    """The saved settings of the project as it is now (no values, only names, are ever printed)."""
    from . import nextstate, setup
    from .state import State
    root = Path(cfg.spec_dir).parent
    try:
        description = (root / "description.txt").read_text(errors="replace").strip()[:4000]
    except OSError as e:
        raise CreateError(f"cannot read {root / 'description.txt'} ({e}); reset needs the description that "
                          f"created the project. Use `hands {cfg.name} destroy --confirm {cfg.name}` and create it again.")
    if not description:
        raise CreateError(f"{root / 'description.txt'} is empty; reset needs a description")
    try:
        state = nextstate.load(Path(cfg.state_dir) / nextstate.FILE)
        s = state["setup"]
        framework, libs = int(s.get("framework") or 1), [str(x) for x in s.get("libs") or []]
        style_refs = [r["url"] for r in state["style_refs"]]
    except (nextstate.InvalidInput, OSError):           # no next.json (project created before phase 2)
        opts = setup.options_of(State(cfg.state_dir).data)
        framework, libs, style_refs = opts.framework, list(opts.libs), []
    return {"schema": SCHEMA, "slug": cfg.name, "description": description, "framework": framework,
            "libs": libs, "style_refs": style_refs, "dropped_config_keys": hand_written_keys(cfg.name),
            "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def write_stage(slug: str, data: dict, env_dir: Path | None) -> list[str]:
    """Stage the settings (and a copy of the variable files when env_dir is given, so the recreate can put
    them back). Returns the variable names that were staged (names only, never values)."""
    from . import projectenv
    d = stage_dir(slug)
    if d.is_symlink():
        raise CreateError(f"{d} is a symlink; refusing")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    names: list[str] = []
    if env_dir is not None:
        for name in (projectenv.ENV_FILE, projectenv.META_FILE):
            src = Path(env_dir) / name
            if src.is_file() and not src.is_symlink():
                shutil.copyfile(src, d / name)
                os.chmod(d / name, 0o600)
        names = [k for k, _ in projectenv.Store(d).names()]
    tmp = d / f".{STAGE_FILE}.tmp"
    tmp.write_text(json.dumps(dict(data, env_kept=env_dir is not None), indent=1) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, stage_file(slug))
    return names


def read_stage(slug: str) -> dict | None:
    """The staged settings of an unfinished reset, or None when there is no (usable) stage."""
    try:
        data = json.loads(stage_file(slug).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("slug") != slug:
        return None
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        return None
    data["framework"] = data["framework"] if isinstance(data.get("framework"), int) else 1
    data["libs"] = [str(x) for x in data.get("libs") or []]
    data["style_refs"] = [str(u) for u in data.get("style_refs") or []]
    return data


def clear_stage(slug: str, keep_env: bool = False) -> None:
    """Remove the stage. keep_env leaves the copied variable files behind (for a restore that failed) and
    takes reset.json away, so no later reset replays these settings by accident."""
    d = stage_dir(slug)
    if not d.is_dir() or d.is_symlink():
        return
    if keep_env:
        stage_file(slug).unlink(missing_ok=True)
        return
    shutil.rmtree(d)


def restore_env(slug: str, target: Path) -> int:
    """Put the staged variables back into the recreated project's secrets dir; returns how many."""
    from . import projectenv
    src = projectenv.Store(stage_dir(slug))
    if not src.values:
        return 0
    projectenv.Store(target).set([{"key": k, "value": v, "public": src.meta[k]["public"]}
                                  for k, v in src.values.items()])
    return len(src.values)


# ---------------------------------------------------------------- reset
def _exists_report(slug: str) -> list[str]:
    """What is on disk for this slug right now (for a reset that stopped halfway)."""
    cfg_path = cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml"
    root = newproject.projects_root() / slug
    lines = []
    for label, p in (("config", cfg_path), ("project folder", root), ("specs clone", root / "specs"),
                     ("app clone", root / "app"), ("secrets", newproject.secrets_root(slug)),
                     ("staged settings", stage_dir(slug))):
        lines.append(f"  {label}: {p} {'exists' if p.exists() else 'gone'}")
    try:
        rec = json.loads((root / "created.json").read_text())
        repos = ", ".join(f"{rec.get('org')}/{r} (id {i})" for r, i in (rec.get("repos") or {}).items())
        lines.append(f"  created.json: {repos or 'no repos recorded'}")
    except (OSError, ValueError):
        lines.append("  created.json: none")
    return lines


def reset(slug: str, confirm: str, wipe_env: bool = False) -> int:
    """Destroy what `create` recorded and create the project again with its saved settings.
    0 done, 1 refused or failed (the message says what exists), 2 bad arguments."""
    from . import notify
    newproject.validate_slug(slug)
    if confirm != slug:
        raise CreateError(f"refusing: pass --confirm {slug}")
    cfg_path = cfgmod.CONFIG_HOME / "projects" / f"{slug}.toml"
    newproject.creating_dir().mkdir(parents=True, exist_ok=True)
    staged = read_stage(slug)
    if not cfg_path.is_file() and staged is None:
        raise CreateError(f"{cfg_path} not found and no staged reset in {stage_dir(slug)}: nothing to reset")
    try:
        lk = Lock(lock_path(slug)).__enter__()
    except SystemExit:
        raise CreateError(f"{slug} is already being reset")
    try:
        tg = cfgmod.CONFIG_HOME / "telegram.env"
        if cfg_path.is_file():
            cfg = cfgmod.load(slug)
            why = refusal(cfg)
            if why:
                raise CreateError(f"reset refused: {why}")
            tg = cfg.telegram_env or tg
            if staged is None:
                staged = collect(cfg)
                staged["env_kept"] = not wipe_env
                names = write_stage(slug, staged, None if wipe_env else newproject.secrets_root(slug))
                say(f"[ok] settings staged in {stage_dir(slug)}: framework {staged['framework']}, "
                    f"libs {', '.join(staged['libs']) or 'none'}, {len(staged['style_refs'])} style ref(s), "
                    + (f"variables kept: {', '.join(names) or 'none'}" if not wipe_env else "variables wiped"))
                if staged["dropped_config_keys"]:
                    say(f"WARN: {slug}.toml is written fresh by create; these hand-set keys are NOT replayed: "
                        f"{', '.join(staged['dropped_config_keys'])} (set them again after the reset)")
            else:
                say(f"[..] using the staged settings from an earlier reset ({stage_file(slug)})")
            notifier = notify.Notifier(tg, slug)
            notifier.send(f"reset: destroying and recreating {slug} "
                          f"(variables {'kept' if staged.get('env_kept') else 'wiped'})")
            newproject.write_status(slug, "running")
            say(f"[..] reset {slug}: destroying what create recorded")
            try:
                newproject.destroy(slug, slug, keep_log=True)
            except Exception as e:
                msg = str(e) if isinstance(e, CreateError) else f"{type(e).__name__}: {e}"
                say(f"FAILED: reset {slug}: destroy failed: {msg}")
                say("Nothing was recreated. What exists now:")
                for line in _exists_report(slug):
                    say(line)
                say(f"Fix the cause, then run: hands {slug} reset --confirm {slug}")
                newproject.write_status(slug, "failed", f"destroy failed: {msg}")
                notifier.send(f"RESET FAILED: {slug} destroy failed: {msg}")
                return 1
        else:
            notifier = notify.Notifier(tg, slug)
            say(f"[..] {slug} is already destroyed; finishing the reset from {stage_file(slug)}")
        say(f"[..] reset {slug}: creating it again")
        rc = newproject.run_create(slug, staged["description"])
        if rc != 0:
            say(f"FAILED: reset {slug}: the project was destroyed but not recreated (create rc={rc}).")
            say("What exists now:")
            for line in _exists_report(slug):
                say(line)
            say(f"The settings are kept. Run this again to finish: hands {slug} reset --confirm {slug}")
            notifier.send(f"RESET FAILED: {slug} was destroyed but not recreated; settings kept in {stage_dir(slug)}")
            return 1
        cfg = cfgmod.load(slug)
        kept, env_failed = 0, False
        try:
            kept = restore_env(slug, newproject.secrets_root(slug))
        except Exception as e:
            env_failed = True
            say(f"WARN: the project's variables were not restored ({type(e).__name__}: {e}); they are still "
                f"in {stage_dir(slug)} (set them again in the dashboard, then delete that folder)")
        if kept:
            say(f"[ok] {kept} environment variable(s) restored")
        from .__main__ import after_create
        rc = after_create(cfg, staged["style_refs"], staged["framework"], staged["libs"])
        clear_stage(slug, keep_env=env_failed)
        say(f"DONE: {slug} reset" + (" (style refs or the scaffold need attention; see above)" if rc else ""))
        notifier.send(f"reset: {slug} recreated"
                      + (f", {kept} variable(s) kept" if kept else "")
                      + (" -- style refs or the scaffold failed, see the dashboard" if rc else ""))
        return rc
    finally:
        lk.__exit__(None, None, None)
