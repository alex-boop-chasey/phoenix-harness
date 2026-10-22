"""Project setup: scaffold the app repo with a framework and libraries (docs/design.md, "Project setup options").

Everything up to `run` is pure data: `pick_version` chooses versions from npm `time` objects, `plan` turns the
project's choices plus the resolved versions into an ordered list of shell commands, `verify` runs the three
check scripts, `record` builds the refs/setup.json document. `run` orchestrates for one project: versions are
resolved and the plan executed inside the firewalled container through the existing launcher (hands.docker),
the result is exported the same way a coder's workspace is (hands.workspace), committed and pushed only when
`verify` passes, and refs/setup.json is written to the spec repo. Nothing runs on the host except Docker and git.

The scaffold tools write into a scratch directory and the tree is copied into the app dir: create-next-app
refuses a directory that holds a README.md, which every app repo has. create-astro downloads its template from
GitHub (and probes github.com before doing anything), so the Astro frameworks need the GitHub hosts in their
egress list; the npm registry alone is not enough for them.

CLI: python3 -m hands.setup <pick|packages|plan|verify|record>
Exit codes: 0 ok (verify: all three scripts passed), 1 invalid input / verify failed, 2 no eligible version,
3 AI kit not configured or libraries chosen without a framework.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

FRAMEWORK_LABELS = {1: "None", 2: "Astro + Tailwind", 3: "Astro + Tailwind + AI kit", 4: "Next.js + Tailwind",
                    5: "Next.js + Tailwind + AI kit"}
LIB_IDS = ("shadcn", "gsap")
ASTRO_FRAMEWORKS = (2, 3)
NEXT_FRAMEWORKS = (4, 5)
AI_KIT_FRAMEWORKS = (3, 5)
PNPM = "pnpm@12.11.2"
MIN_AGE_DAYS = 3
# newest major a package may be pinned to: typescript 7 is the native compiler and typescript-eslint, @astrojs/check
# and tsc-based typechecks still need the 6.x JS API
VERSION_CAPS = {"typescript": 6}
REGISTRY_HOST = "registry.npmjs.org"
SHADCN_HOST = "ui.shadcn.com"
ASTRO_TEMPLATE_HOSTS = ("github.com", "api.github.com", "codeload.github.com")   # create-astro: online probe + giget
AI_KIT_DIR = "/tmp/aikit"
SCAFFOLD_DIR = "/tmp/hands-scaffold"
VERIFY_STEPS = ("build", "lint", "typecheck")
SETUP_STATUSES = ("none", "pending", "running", "done", "failed")
BLOCKING_STATUSES = ("pending", "running", "failed")

STABLE_RE = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]{1,64})?$")
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
REPO_URL_RE = re.compile(r"^(?:https?://[A-Za-z0-9.\-]+(?::\d+)?/[A-Za-z0-9._/\-~]+|[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-~]+)$")

GITIGNORE_LINES = ("node_modules/", ".pnpm-store/", "/acceptance/", ".env", ".env.*", "!.env.example", "dist/", ".next/")
ASTRO_PRETTIER_IGNORE = ("node_modules", "dist", ".astro", "pnpm-lock.yaml")
NEXT_PRETTIER_IGNORE = ("node_modules", ".next", "pnpm-lock.yaml")


class NoEligibleVersion(Exception):
    def __init__(self, message: str = "no eligible version"):
        super().__init__(message)


class AiKitNotConfigured(Exception):
    def __init__(self, message: str = "AI kit not configured"):
        super().__init__(message)


class LibsNeedFramework(Exception):
    def __init__(self, message: str = "libraries need a framework"):
        super().__init__(message)


class InvalidOptions(ValueError):
    """Bad input the CLI reports with exit 1 (options, versions, AI kit fields)."""


# ------------------------------------------------------------------------------------------- versions

def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pick_version(times: dict[str, str], now: datetime, min_age_days: int = MIN_AGE_DAYS,
                 max_major: int | None = None) -> str:
    """Newest stable semver (no prerelease tag) published at least `min_age_days` before `now`, from an npm
    `time` object (`created` / `modified` and prereleases are ignored), majors above `max_major` excluded.
    Raises NoEligibleVersion."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=min_age_days)
    best: tuple[tuple[int, int, int], str] | None = None
    for version, published in (times or {}).items():
        if not isinstance(version, str) or not STABLE_RE.match(version):
            continue
        when = _parse_time(published)
        if when is None or when > cutoff:
            continue
        key = tuple(int(x) for x in version.split("."))
        if max_major is not None and key[0] > max_major:
            continue
        if best is None or key > best[0]:
            best = (key, version)
    if best is None:
        raise NoEligibleVersion()
    return best[1]


# -------------------------------------------------------------------------------------------- options

@dataclass
class SetupOptions:
    framework: int
    libs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any) -> "SetupOptions":
        if not isinstance(raw, dict):
            raise InvalidOptions("options must be an object")
        fw = raw.get("framework", 1)
        if isinstance(fw, bool) or not isinstance(fw, int) or fw not in FRAMEWORK_LABELS:
            raise InvalidOptions(f"framework must be one of {sorted(FRAMEWORK_LABELS)}")
        libs = raw.get("libs", [])
        if not isinstance(libs, list) or not all(isinstance(x, str) for x in libs):
            raise InvalidOptions("libs must be a list of ids")
        unknown = [x for x in libs if x not in LIB_IDS]
        if unknown:
            raise InvalidOptions(f"unknown libs: {', '.join(unknown)} (known: {', '.join(LIB_IDS)})")
        return cls(framework=fw, libs=[x for x in LIB_IDS if x in libs])

    def to_dict(self) -> dict:
        return {"framework": self.framework, "libs": list(self.libs)}


@dataclass(frozen=True)
class AiKit:
    repo: str
    ref: str
    install_command: str


def ai_kit_from(raw: Any) -> AiKit | None:
    """The AI kit from a config table ({repo, ref, install_command}); None when it is absent or incomplete."""
    if not isinstance(raw, dict):
        return None
    vals = [raw.get(k) for k in ("repo", "ref", "install_command")]
    if not all(isinstance(v, str) and v.strip() for v in vals):
        return None
    return AiKit(*(v.strip() for v in vals))


def _check_ai_kit(kit: AiKit) -> None:
    if not REPO_URL_RE.match(kit.repo):
        raise InvalidOptions("ai_kit.repo must be an https:// URL or a user@host:path git address")
    if not GIT_REF_RE.match(kit.ref):
        raise InvalidOptions("ai_kit.ref must be a tag or branch name")
    if not kit.install_command.strip() or any(ord(ch) < 32 for ch in kit.install_command):
        raise InvalidOptions("ai_kit.install_command must be a single-line command")


def packages(opts: SetupOptions) -> list[str]:
    """The npm packages whose versions must be resolved before `plan`, in plan order."""
    out: list[str] = []
    if opts.framework in ASTRO_FRAMEWORKS:
        out += ["create-astro", "astro", "tailwindcss", "@tailwindcss/vite", "@astrojs/check", "typescript", "prettier",
                "prettier-plugin-astro", "eslint", "eslint-plugin-astro", "@eslint/js", "typescript-eslint"]
    elif opts.framework in NEXT_FRAMEWORKS:
        out += ["create-next-app", "prettier"]
    else:
        return out
    if "shadcn" in opts.libs:
        out.append("shadcn")
    if "gsap" in opts.libs:
        out.append("gsap")
        if opts.framework in NEXT_FRAMEWORKS:
            out.append("@gsap/react")
    return out


# ----------------------------------------------------------------------------------------------- plan

@dataclass
class SetupPlan:
    commands: list[str]
    versions: dict[str, str]
    packages: list[str]
    framework: int
    libs: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "SetupPlan":
        if not isinstance(raw, dict):
            raise InvalidOptions("plan must be an object")
        opts = SetupOptions.from_dict({"framework": raw.get("framework", 1), "libs": raw.get("libs", [])})
        cmds, vers, pkgs = raw.get("commands", []), raw.get("versions", {}), raw.get("packages", [])
        if not isinstance(cmds, list) or not all(isinstance(c, str) for c in cmds):
            raise InvalidOptions("plan.commands must be a list of strings")
        if not isinstance(vers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in vers.items()):
            raise InvalidOptions("plan.versions must map package names to versions")
        if not isinstance(pkgs, list) or not all(isinstance(p, str) for p in pkgs):
            raise InvalidOptions("plan.packages must be a list of strings")
        return cls(commands=cmds, versions=vers, packages=pkgs, framework=opts.framework, libs=opts.libs)


def _write_file(path: str, text: str) -> str:
    """A shell command writing `text` to `path` (heredoc with a quoted delimiter: nothing is expanded)."""
    assert "\nEOF\n" not in text and not text.endswith("\nEOF")
    body = text if text.endswith("\n") else text + "\n"
    return f"cat > {shlex.quote(path)} <<'EOF'\n{body}EOF"


def _node_edit(script: str) -> str:
    """`node -e` with a script that uses double quotes only (so the shell quoting is a plain single quote)."""
    assert "'" not in script
    return f"node -e '{script}'"


def _ensure_scripts(scripts: dict[str, str]) -> str:
    want = json.dumps(scripts, separators=(",", ":"))
    return _node_edit(
        'const fs=require("fs");const p=JSON.parse(fs.readFileSync("package.json","utf8"));'
        'p.scripts=p.scripts||{};const want=' + want + ';'
        'for(const k of Object.keys(want)){if(!p.scripts[k])p.scripts[k]=want[k];}'
        f'p.packageManager="{PNPM}";'
        'fs.writeFileSync("package.json",JSON.stringify(p,null,2)+"\\n");')


def _ensure_gitignore() -> str:
    need = json.dumps(list(GITIGNORE_LINES), separators=(",", ":"))
    return _node_edit(
        'const fs=require("fs");const need=' + need + ';'
        'let t=fs.existsSync(".gitignore")?fs.readFileSync(".gitignore","utf8"):"";'
        'const have=new Set(t.split(/\\r?\\n/).map((l)=>l.trim()));const add=need.filter((l)=>!have.has(l));'
        'if(add.length){if(t&&!t.endsWith("\\n"))t+="\\n";t+="# hands\\n"+add.join("\\n")+"\\n";fs.writeFileSync(".gitignore",t);}')


def _scaffold(create: str) -> str:
    """Run a create-* tool into a scratch dir and copy the result over the app dir (README.md and .gitignore from
    the repo are replaced; the gitignore lines the harness needs are re-added by `_ensure_gitignore`)."""
    return (f"rm -rf {SCAFFOLD_DIR} && {create} && cp -a {SCAFFOLD_DIR}/. . && rm -rf {SCAFFOLD_DIR}")


ASTRO_CONFIG = """// @ts-check
import { defineConfig } from "astro/config";
import tailwindcss from "@tailwindcss/vite";

// https://astro.build/config
export default defineConfig({
  vite: { plugins: [tailwindcss()] },
});
"""

ASTRO_ESLINT = """import js from "@eslint/js";
import tseslint from "typescript-eslint";
import astro from "eslint-plugin-astro";

export default [
  { ignores: ["dist/**", ".astro/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...astro.configs.recommended,
];
"""

ASTRO_PRETTIERRC = """{
  "plugins": ["prettier-plugin-astro"],
  "overrides": [{ "files": "*.astro", "options": { "parser": "astro" } }]
}
"""

NEXT_PRETTIERRC = "{}\n"

# The create-next-app template loads Geist from Google Fonts at build time (unreachable behind the firewall) and
# shows a demo page built on next/image and the template's SVGs: both are replaced by minimal files.
NEXT_LAYOUT = """import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "App",
  description: "Scaffolded by hands",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="flex min-h-full flex-col">{children}</body>
    </html>
  );
}
"""

NEXT_PAGE = """export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center p-16">
      <h1 className="text-3xl font-semibold tracking-tight">Hello</h1>
    </main>
  );
}
"""

NEXT_GLOBALS_CSS = """@import "tailwindcss";

:root {
  --background: #ffffff;
  --foreground: #171717;
}

@theme inline {
  --color-background: var(--background);
  --color-foreground: var(--foreground);
}

@media (prefers-color-scheme: dark) {
  :root {
    --background: #0a0a0a;
    --foreground: #ededed;
  }
}

body {
  background: var(--background);
  color: var(--foreground);
}
"""
NEXT_TEMPLATE_SVGS = ("file.svg", "globe.svg", "next.svg", "vercel.svg", "window.svg")


def plan(opts: SetupOptions, versions: dict[str, str], ai_kit: AiKit | None) -> SetupPlan:
    """Ordered shell commands (run in the app dir, inside the container) for the project's choices. Pure.
    Raises LibsNeedFramework, AiKitNotConfigured, KeyError (a package without a resolved version), InvalidOptions."""
    opts = SetupOptions.from_dict(opts.to_dict())
    if opts.framework == 1:
        if opts.libs:
            raise LibsNeedFramework()
        return SetupPlan(commands=[], versions={}, packages=[], framework=1, libs=[])
    if opts.framework in AI_KIT_FRAMEWORKS:
        if ai_kit is None:
            raise AiKitNotConfigured()
        _check_ai_kit(ai_kit)
    pkgs = packages(opts)
    used: dict[str, str] = {}
    for p in pkgs:
        v = versions[p]
        if not isinstance(v, str) or not VERSION_RE.match(v):
            raise InvalidOptions(f"bad version for {p}: {v!r}")
        used[p] = v

    def pin(*names: str) -> str:
        return " ".join(f"{n}@{used[n]}" for n in names)

    cmds: list[str] = []
    if opts.framework in ASTRO_FRAMEWORKS:
        cmds.append(_scaffold(f"pnpm dlx {pin('create-astro')} {SCAFFOLD_DIR} --template minimal --no-install --no-git "
                              f"--no-ai --yes --skip-houston"))
        cmds.append(f"pnpm add {pin('astro')}")
        cmds.append("pnpm add -D " + pin("tailwindcss", "@tailwindcss/vite", "@astrojs/check", "typescript", "prettier",
                                         "prettier-plugin-astro", "eslint", "eslint-plugin-astro", "@eslint/js",
                                         "typescript-eslint"))
        cmds.append(_write_file("astro.config.mjs", ASTRO_CONFIG))
        cmds.append("mkdir -p src/styles && " + _write_file("src/styles/global.css", '@import "tailwindcss";\n'))
        cmds.append(_write_file(".prettierrc", ASTRO_PRETTIERRC))
        cmds.append(_write_file(".prettierignore", "\n".join(ASTRO_PRETTIER_IGNORE) + "\n"))
        cmds.append(_write_file("eslint.config.js", ASTRO_ESLINT))
        scripts = {"build": "astro build", "lint": "eslint .", "typecheck": "astro check",
                   "format": "prettier --write .", "format:check": "prettier --check ."}
    else:
        cmds.append(_scaffold(f"pnpm dlx {pin('create-next-app')} {SCAFFOLD_DIR} --ts --tailwind --eslint --app --src-dir "
                              f"--use-pnpm --import-alias '@/*' --skip-install --disable-git --no-agents-md "
                              f"--no-agent-feedback --yes"))
        cmds.append("pnpm add -D " + pin("prettier"))
        cmds.append(_write_file("src/app/layout.tsx", NEXT_LAYOUT))
        cmds.append(_write_file("src/app/page.tsx", NEXT_PAGE))
        cmds.append(_write_file("src/app/globals.css", NEXT_GLOBALS_CSS))
        cmds.append("rm -f " + " ".join(f"public/{f}" for f in NEXT_TEMPLATE_SVGS))
        cmds.append(_write_file(".prettierrc", NEXT_PRETTIERRC))
        cmds.append(_write_file(".prettierignore", "\n".join(NEXT_PRETTIER_IGNORE) + "\n"))
        scripts = {"build": "next build", "lint": "eslint", "typecheck": "tsc --noEmit",
                   "format": "prettier --write .", "format:check": "prettier --check ."}
    if "shadcn" in opts.libs:
        if opts.framework in ASTRO_FRAMEWORKS:
            cmds.append("pnpm astro add react --yes")          # shadcn components are React components
        cmds.append(f"pnpm dlx {pin('shadcn')} init --yes --defaults")
    if "gsap" in opts.libs:
        cmds.append("pnpm add " + (pin("gsap", "@gsap/react") if opts.framework in NEXT_FRAMEWORKS else pin("gsap")))
    if opts.framework in AI_KIT_FRAMEWORKS:
        assert ai_kit is not None
        cmds.append(f"rm -rf {AI_KIT_DIR}")
        cmds.append(f"git clone --depth 1 --branch {ai_kit.ref} {ai_kit.repo} {AI_KIT_DIR}")
        cmds.append(ai_kit.install_command)
    cmds.append(_ensure_scripts(scripts))
    cmds.append(_ensure_gitignore())
    cmds.append("pnpm install && pnpm run format")
    cmds.append("pnpm run build && pnpm run lint && pnpm run typecheck")
    return SetupPlan(commands=cmds, versions=used, packages=pkgs, framework=opts.framework, libs=list(opts.libs))


def egress_hosts(opts: SetupOptions, ai_kit: AiKit | None) -> list[str]:
    """Hosts the setup container may reach: the npm registry always, ui.shadcn.com for shadcn, the GitHub hosts
    for the Astro frameworks (create-astro's template), the AI kit's host for frameworks 3/5."""
    hosts = [REGISTRY_HOST]
    if opts.framework in ASTRO_FRAMEWORKS:
        hosts += ASTRO_TEMPLATE_HOSTS
    if "shadcn" in opts.libs:
        hosts.append(SHADCN_HOST)
    if opts.framework in AI_KIT_FRAMEWORKS and ai_kit is not None:
        host = _repo_host(ai_kit.repo)
        if host:
            hosts.append(host)
    return list(dict.fromkeys(hosts))


def _repo_host(repo: str) -> str:
    from urllib.parse import urlsplit
    if "://" in repo:
        return (urlsplit(repo).hostname or "").lower()
    m = re.match(r"^[^@]+@([A-Za-z0-9.\-]+):", repo)
    return m.group(1).lower() if m else ""


# --------------------------------------------------------------------------------------------- verify

@dataclass
class VerifyResult:
    ok: bool
    steps: list[dict] = field(default_factory=list)     # {name, exit, log}

    def to_dict(self) -> dict:
        return {"ok": self.ok, "steps": list(self.steps)}

    @classmethod
    def from_dict(cls, raw: Any) -> "VerifyResult":
        if not isinstance(raw, dict) or not isinstance(raw.get("ok"), bool) or not isinstance(raw.get("steps"), list):
            raise InvalidOptions("verify result must be {ok, steps}")
        return cls(ok=raw["ok"], steps=[dict(s) for s in raw["steps"] if isinstance(s, dict)])


StepRunner = Callable[[str, list[str], Path], tuple[int, str]]


def _run_local(name: str, argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=1800,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        return 127, f"{argv[0]}: {e}"
    except subprocess.TimeoutExpired:
        return 124, f"{name}: timed out"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def verify(app_dir: Path, run_step: StepRunner | None = None) -> VerifyResult:
    """Run `pnpm run build`, `pnpm run lint`, `pnpm run typecheck` in `app_dir`; ok iff all three exit 0.
    `run_step(name, argv, cwd) -> (exit, log)` lets `run` execute each step in a container instead."""
    runner = run_step or _run_local
    steps = []
    for name in VERIFY_STEPS:
        rc, log = runner(name, ["pnpm", "run", name], Path(app_dir))
        steps.append({"name": name, "exit": int(rc), "log": log})
    return VerifyResult(ok=all(s["exit"] == 0 for s in steps), steps=steps)


def record(plan: SetupPlan, verify: VerifyResult, now: datetime | None = None) -> dict:
    """The refs/setup.json document."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return {"schema": 1, "framework": plan.framework, "libs": list(plan.libs), "versions": dict(plan.versions),
            "commands": list(plan.commands), "verified_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ok": bool(verify.ok)}


# ------------------------------------------------------------------------------------- project state

def options_of(state_data: dict) -> SetupOptions:
    """The project's choices from state.json's `setup` record (framework 1, no libs when unset)."""
    s = state_data.get("setup") if isinstance(state_data, dict) else None
    s = s if isinstance(s, dict) else {}
    return SetupOptions.from_dict({"framework": s.get("framework", 1), "libs": s.get("libs", [])})


def blocking_status(state_data: dict) -> str:
    """The setup status that stops `hands <slug> start` (pending | running | failed), else ''."""
    s = state_data.get("setup") if isinstance(state_data, dict) else None
    status = s.get("status") if isinstance(s, dict) else ""
    return status if status in BLOCKING_STATUSES else ""


def commit_subject(opts: SetupOptions) -> str:
    label = FRAMEWORK_LABELS[opts.framework]
    return f"scaffold: {label}" + (f" + {', '.join(opts.libs)}" if opts.libs else "")


def ai_kit_for(cfg) -> AiKit | None:
    """The AI kit: the project's `[ai_kit]` table, else the harness-wide one in new-project.toml."""
    kit = ai_kit_from(getattr(cfg, "ai_kit", None))
    if kit:
        return kit
    import tomllib
    from .config import CONFIG_HOME
    p = CONFIG_HOME / "new-project.toml"
    try:
        return ai_kit_from(tomllib.loads(p.read_text()).get("ai_kit"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


# ------------------------------------------------------------------------------------------------ run

SETUP_ENV = {
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "COREPACK_HOME": "/home/node/.cache/corepack",
    "npm_config_store_dir": "/pnpm-store",
    "CI": "1",
}
RESOLVE_TIMEOUT = 900
SCAFFOLD_TIMEOUT = 3600
VERIFY_TIMEOUT = 1800
EXPORT_TIMEOUT = 600


def _resolve_script(pkgs: list[str]) -> str:
    lines = ["set -u", "rc=0"]
    for i, p in enumerate(pkgs):
        lines.append(f"npm view {shlex.quote(p)} time --json > /out/{i}.json || {{ echo \"npm view {p} failed\" >&2; rc=1; }}")
    lines.append("exit $rc")
    return "\n".join(lines)


def _scaffold_script(commands: list[str]) -> str:
    return "set -euo pipefail\ncd /workspace\n" + "\n".join(f"echo '### ' {shlex.quote(c.splitlines()[0])}\n{c}" for c in commands)


def _set_setup(st, opts: SetupOptions, status: str, record_doc: dict | None = None, error: str = "") -> None:
    cur = st.data.get("setup") if isinstance(st.data.get("setup"), dict) else {}
    cur.update({"framework": opts.framework, "libs": list(opts.libs), "status": status, "record": record_doc})
    if error:
        cur["error"] = error
    else:
        cur.pop("error", None)
    cur["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st.data["setup"] = cur
    st.save()
    _mirror_next(Path(st.dir), opts, status, record_doc)


def _mirror_next(state_dir: Path, opts: SetupOptions, status: str, record_doc: dict | None) -> None:
    """next.json (hands.nextstate) carries the same setup record when the project has one."""
    from . import nextstate
    path = state_dir / nextstate.FILE
    if not path.exists():
        return
    state = nextstate.load(path)
    state["setup"] = {"framework": opts.framework, "libs": list(opts.libs), "status": status, "record": record_doc}
    nextstate.save(path, state)


def run(cfg, framework: int | None = None, libs: list[str] | None = None) -> int:
    """`hands <slug> setup`: resolve versions and execute the plan in the firewalled container, verify, commit and
    push the scaffold, write refs/setup.json to the spec repo, set the project's setup status. Returns the exit code."""
    from . import docker, gitops, workspace
    from .docker import Mount, Run
    from .state import Lock, State

    st = State(cfg.state_dir)
    if Lock.held(cfg.state_dir / "runner.lock"):
        print("runner is running; stop it first (hands %s stop)" % cfg.name)
        return 1
    try:
        cur = options_of(st.data)
        opts = SetupOptions.from_dict({"framework": cur.framework if framework is None else framework,
                                       "libs": cur.libs if libs is None else libs})
    except InvalidOptions as e:
        print(f"setup: {e}")
        return 1
    kit = ai_kit_for(cfg)
    try:
        pkgs_needed = packages(opts)
        if opts.framework == 1 and opts.libs:
            raise LibsNeedFramework()
        if opts.framework in AI_KIT_FRAMEWORKS and kit is None:
            raise AiKitNotConfigured()
    except (LibsNeedFramework, AiKitNotConfigured) as e:
        _set_setup(st, opts, "failed", None, str(e))
        print(f"setup: {e}")
        return 3
    if opts.framework == 1:
        _set_setup(st, opts, "none")
        print("setup: framework None, no libraries: nothing to scaffold")
        return 0
    with Lock(cfg.state_dir / "runner.lock"):
        return _run_locked(cfg, st, opts, kit, pkgs_needed, docker, gitops, workspace, Mount, Run)


def _run_locked(cfg, st, opts, kit, pkgs_needed, docker, gitops, workspace, Mount, Run) -> int:
    sdir = cfg.state_dir / "setup"
    sdir.mkdir(parents=True, exist_ok=True)
    for old in sdir.glob("*.log"):
        old.unlink()
    hosts = sdir / "allowed-domains"
    hosts.write_text("".join(f"{h}\n" for h in egress_hosts(opts, kit)))
    entry = Mount(cfg.runner_home / "container" / "entry.sh", docker.ENTRY)
    _set_setup(st, opts, "running")
    st.log(f"setup: {FRAMEWORK_LABELS[opts.framework]} + {', '.join(opts.libs) or 'no libs'}; egress {', '.join(egress_hosts(opts, kit))}")

    def fail(msg: str, rec: dict | None = None) -> int:
        _set_setup(st, opts, "failed", rec, msg)
        st.log(f"setup FAILED: {msg}")
        print(f"setup failed: {msg}\nlogs: {sdir}")
        return 1

    # ---- 1. versions, resolved inside the container (npm view <pkg> time --json)
    out = sdir / "times"
    workspace.rmtree(out)
    out.mkdir()
    rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "setup", "resolve"), role="setup",
                             cmd=["bash", "-c", _resolve_script(pkgs_needed)],
                             mounts=[entry, Mount(hosts, "/etc/hands/allowed-domains"), Mount(out, "/out", ro=False)],
                             env=SETUP_ENV, stdout=sdir / "resolve.log", stderr=sdir / "resolve.log",
                             timeout_s=RESOLVE_TIMEOUT))
    if rc != 0:
        return fail(f"could not resolve package versions (rc={rc}); see resolve.log")
    versions: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    for i, p in enumerate(pkgs_needed):
        try:
            times = json.loads((out / f"{i}.json").read_text())
            versions[p] = pick_version(times, now, int(cfg.min_release_age_days), VERSION_CAPS.get(p))
        except (OSError, ValueError, NoEligibleVersion) as e:
            return fail(f"no eligible version for {p}: {e}")
    workspace.rmtree(out)
    try:
        sp = plan(opts, versions, kit)
    except (LibsNeedFramework, AiKitNotConfigured, InvalidOptions, KeyError) as e:
        return fail(f"plan: {e}")
    (sdir / "plan.json").write_text(json.dumps(sp.to_dict(), indent=2) + "\n")
    st.log("setup: versions " + ", ".join(f"{k}@{v}" for k, v in versions.items()))

    # ---- 2. the plan, in a fresh clone of the app repo mounted read-write
    work = sdir / "work"
    workspace.make_work(cfg.app_dir, work)
    base = gitops.head(cfg.app_dir)
    store = docker.volume_create(cfg, f"hands-{cfg.name}-setup-{int(time.time())}")
    try:
        mounts = [entry, Mount(hosts, "/etc/hands/allowed-domains"), Mount(work, "/workspace", ro=False),
                  Mount(store, "/pnpm-store", ro=False)]
        rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "setup", "scaffold"), role="setup",
                                 cmd=["bash", "-c", _scaffold_script(sp.commands)], mounts=mounts, env=SETUP_ENV,
                                 stdout=sdir / "scaffold.log", stderr=sdir / "scaffold.log", timeout_s=SCAFFOLD_TIMEOUT))
        if rc != 0:
            return fail(f"scaffold commands failed (rc={rc}); see scaffold.log")

        # ---- 3. verify: each script in its own container, logs kept
        def run_step(name: str, argv: list[str], cwd: Path) -> tuple[int, str]:
            logf = sdir / f"verify-{name}.log"
            rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "setup", f"verify-{name}"), role="setup",
                                     cmd=["bash", "-c", "cd /workspace && exec " + shlex.join(argv)], mounts=mounts,
                                     env=SETUP_ENV, stdout=logf, stderr=logf, timeout_s=VERIFY_TIMEOUT))
            try:
                log = logf.read_text(errors="replace")
            except OSError:
                log = ""
            return rc, log[-20000:]

        vr = verify(Path("/workspace"), run_step=run_step)
        (sdir / "verify.json").write_text(json.dumps(vr.to_dict(), indent=2) + "\n")
        rec = record(sp, vr)
        if not vr.ok:
            bad = ", ".join(f"{s['name']} exit {s['exit']}" for s in vr.steps if s["exit"] != 0)
            return fail(f"verify failed: {bad}; see verify-*.log", rec)

        # ---- 4. export like a coder workspace (validated tar, no .git, regular files only), then commit
        exp = sdir / "export"
        workspace.rmtree(exp)
        exp.mkdir()
        rc = docker.run(cfg, Run(name=docker.unique_name(cfg, "setup", "export"), role="export",
                                 cmd=["bash", "-c", workspace.EXPORT_SCRIPT], network=False,
                                 mounts=[entry, Mount(work, "/workspace", ro=True), Mount(exp, "/out", ro=False)],
                                 stderr=sdir / "export.log", timeout_s=EXPORT_TIMEOUT))
        if rc != 0:
            return fail(f"could not export the scaffold (rc={rc}); see export.log", rec)
        try:
            workspace.apply_tree(cfg.app_dir, exp / "tree.tar")
        except workspace.ExportError as e:
            gitops.hard_reset(cfg.app_dir, base)
            return fail(str(e), rec)
        finally:
            workspace.rmtree(exp)
    finally:
        docker.volume_rm(cfg, store)
        workspace.rmtree(work)
    if not workspace.staged_changes(cfg.app_dir, base):
        gitops.hard_reset(cfg.app_dir, base)
        return fail("the scaffold changed no files", rec)
    msg = f"{commit_subject(opts)}\n\n" + "".join(f"{k}@{v}\n" for k, v in sp.versions.items())
    gitops.git(cfg.app_dir, "-c", "user.name=hands-runner", "-c", "user.email=runner@hands.local",
               "commit", "-q", "--no-verify", "-F", "-", input=msg.encode())
    commit = gitops.head(cfg.app_dir)
    pushed = "push disabled"
    if cfg.push:
        ok, err = gitops.push_branch(cfg.app_dir, cfg.branch)
        pushed = "pushed" if ok else f"push failed ({err[:200]}); sync_app pushes it at the next start"

    # ---- 5. refs/setup.json in the spec repo, status
    ref = cfg.spec_dir / "refs" / "setup.json"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(json.dumps(rec, indent=2) + "\n")
    try:
        gitops.git(cfg.spec_dir, "add", "-A", "--", "refs/setup.json")
        if gitops.git(cfg.spec_dir, "status", "--porcelain", "--", "refs/setup.json").strip():
            gitops.git(cfg.spec_dir, "-c", "user.name=hands-runner", "-c", "user.email=runner@hands.local",
                       "commit", "-q", "--no-verify", "-m", "setup: scaffold", "--", "refs/setup.json")
    except gitops.GitError as e:
        st.log(f"setup: WARN refs/setup.json not committed: {e}")
    _set_setup(st, opts, "done", rec)
    st.log(f"setup DONE @ {commit[:9]} ({pushed})")
    print(f"setup done: {commit_subject(opts)} @ {commit[:9]} ({pushed}); record {ref}")
    return 0


# ------------------------------------------------------------------------------------------------ CLI

def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise InvalidOptions(f"{path}: {e}") from e


def _options(path: str) -> SetupOptions:
    return SetupOptions.from_dict(_load_json(path))


def _ai_kit(path: str | None) -> AiKit | None:
    if not path:
        return None
    kit = ai_kit_from(_load_json(path))
    if kit is None:
        raise InvalidOptions(f"{path}: needs repo, ref and install_command")
    return kit


def _now(text: str) -> datetime:
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise InvalidOptions(f"--now: {e}") from e
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.setup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pick", help="newest stable version at least --min-age-days old from an npm time object")
    p.add_argument("--times", required=True)
    p.add_argument("--now", required=True)
    p.add_argument("--min-age-days", type=int, default=MIN_AGE_DAYS)
    p.add_argument("--max-major", type=int, help="ignore majors above this (run uses VERSION_CAPS)")
    p = sub.add_parser("packages", help="packages whose versions must be resolved for the options")
    p.add_argument("--options", required=True)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("plan", help="the ordered shell commands for the options and versions")
    p.add_argument("--options", required=True)
    p.add_argument("--versions", required=True)
    p.add_argument("--ai-kit")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("verify", help="run build, lint and typecheck in DIR")
    p.add_argument("dir")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("record", help="the refs/setup.json document for a plan and a verify result")
    p.add_argument("--plan", required=True)
    p.add_argument("--verify", required=True)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "pick":
            times = _load_json(a.times)
            if not isinstance(times, dict):
                raise InvalidOptions(f"{a.times}: must be an npm time object")
            try:
                print(pick_version(times, _now(a.now), a.min_age_days, a.max_major))
            except NoEligibleVersion as e:
                print(e, file=sys.stderr)
                return 2
            return 0
        if a.cmd == "packages":
            pkgs = packages(_options(a.options))
            print(json.dumps(pkgs) if a.json else "\n".join(pkgs))
            return 0
        if a.cmd == "plan":
            versions = _load_json(a.versions)
            if not isinstance(versions, dict):
                raise InvalidOptions(f"{a.versions}: must map package names to versions")
            try:
                sp = plan(_options(a.options), versions, _ai_kit(a.ai_kit))
            except (AiKitNotConfigured, LibsNeedFramework) as e:
                print(e, file=sys.stderr)
                return 3
            except KeyError as e:
                raise InvalidOptions(f"no version for {e.args[0]} in {a.versions}") from e
            print(json.dumps(sp.to_dict(), indent=2) if a.json else "\n".join(sp.commands))
            return 0
        if a.cmd == "verify":
            d = Path(a.dir)
            if not (d / "package.json").is_file():
                raise InvalidOptions(f"{d}: no package.json")
            vr = verify(d)
            if a.json:
                print(json.dumps(vr.to_dict(), indent=2))
            else:
                for s in vr.steps:
                    print(f"{s['name']}: exit {s['exit']}")
                print("ok" if vr.ok else "FAILED")
            return 0 if vr.ok else 1
        if a.cmd == "record":
            doc = record(SetupPlan.from_dict(_load_json(a.plan)), VerifyResult.from_dict(_load_json(a.verify)))
            print(json.dumps(doc, indent=2))
            return 0
    except InvalidOptions as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
