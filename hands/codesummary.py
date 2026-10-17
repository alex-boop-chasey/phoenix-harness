"""Bounded summary of an existing (imported) app repo for the planner, plus the allowed_paths coverage check.

Runs on the host against the runner-owned app clone. It only lists tracked files (git ls-files) and reads
two text files (package.json, README*) with size caps, never through symlinks; nothing is executed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import gitops
from .spec import path_allowed

MAX_READ = 200_000            # bytes read from package.json / README
README_CHARS = 3000
PKG_CHARS = 6000
TREE_LINES = 220
EXAMPLES = 6                  # files listed per directory
CAP = 24_000                  # characters of the whole summary

FRAMEWORKS = [("next", "Next.js"), ("nuxt", "Nuxt"), ("@sveltejs/kit", "SvelteKit"), ("astro", "Astro"),
              ("@remix-run/react", "Remix"), ("@angular/core", "Angular"), ("gatsby", "Gatsby"), ("vue", "Vue"),
              ("svelte", "Svelte"), ("react", "React"), ("solid-js", "Solid"), ("express", "Express"),
              ("fastify", "Fastify"), ("hono", "Hono"), ("koa", "Koa"), ("@nestjs/core", "NestJS"),
              ("electron", "Electron"), ("vite", "Vite"), ("typescript", "TypeScript")]
TEST_TOOLS = [("vitest", "vitest"), ("jest", "jest"), ("@playwright/test", "Playwright"), ("cypress", "Cypress"),
              ("mocha", "mocha"), ("ava", "ava"), ("eslint", "eslint"), ("prettier", "prettier"), ("biome", "biome")]
CONFIG_FILES = re.compile(r"^(?:vitest|jest|playwright|cypress|eslint|prettier|biome|tsconfig|tailwind|postcss|"
                          r"vite|next|nuxt|astro|svelte|webpack|rollup|babel)[.\w-]*\.(?:[cm]?[jt]s|json|yaml|yml)$|"
                          r"^\.(?:eslintrc|prettierrc|babelrc)[.\w-]*$")
OTHER_ECOSYSTEMS = {"pyproject.toml": "Python (pyproject)", "requirements.txt": "Python (requirements)",
                    "go.mod": "Go", "Cargo.toml": "Rust", "Gemfile": "Ruby", "composer.json": "PHP",
                    "pom.xml": "Java (Maven)", "build.gradle": "Java (Gradle)", "Dockerfile": "Dockerfile",
                    "docker-compose.yml": "docker-compose"}
LOCKFILES = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "package-lock.json": "npm", "bun.lockb": "bun",
             "bun.lock": "bun"}
NOT_CODE_EXT = {".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc"}
NOT_CODE_FILES = {"license", "license.txt", "licence", "copying", "notice", "codeowners", "changelog"}
HUMAN_TOP = {"docs", "acceptance", "specs", "spec", ".harness"}


def tracked(app_dir: Path) -> list[str]:
    try:
        out = gitops.git(app_dir, "ls-files", "-z", timeout=120)
    except gitops.GitError:
        return []
    return sorted(p for p in out.split("\0") if p and ".." not in p.split("/"))


def _read_text(app_dir: Path, rel: str) -> str:
    """Text of a tracked regular file (never a symlink), capped; '' when unreadable or binary-looking."""
    p = app_dir / rel
    try:
        if p.is_symlink() or not p.is_file():
            return ""
        with open(p, "rb") as f:
            raw = f.read(MAX_READ)
    except OSError:
        return ""
    if b"\0" in raw:
        return ""
    return "".join(ch if (ch.isprintable() or ch in "\n\t") else " " for ch in raw.decode("utf-8", errors="replace"))


def _tree(files: list[str]) -> list[str]:
    """Directory-grouped listing: every directory (depth <= 3) with its file count and a few examples."""
    groups: dict[str, list[str]] = {}
    for f in files:
        parts = f.split("/")
        key = "/".join(parts[:-1][:3])
        groups.setdefault(key, []).append(parts[-1] if len(parts) <= 4 else "/".join(parts[3:]))
    lines = []
    for d in sorted(groups, key=lambda k: (k != "", k)):
        names = groups[d]
        label = (d + "/") if d else "(root)"
        shown = ", ".join(names[:EXAMPLES]) + (f", ... ({len(names)} files)" if len(names) > EXAMPLES else "")
        lines.append(f"{label}  {shown}")
    if len(lines) > TREE_LINES:
        lines = lines[:TREE_LINES] + [f"... {len(groups) - TREE_LINES} more directories (truncated)"]
    return lines


def _package(app_dir: Path, files: list[str]) -> tuple[str, dict]:
    if "package.json" not in files:
        return "", {}
    text = _read_text(app_dir, "package.json")
    if not text:
        return "package.json: present but not readable as text", {}
    try:
        pkg = json.loads(text)
    except ValueError:
        return "package.json: present but not valid JSON", {}
    if not isinstance(pkg, dict):
        return "package.json: present but not an object", {}
    keep = {k: pkg[k] for k in ("name", "version", "packageManager", "type", "engines", "scripts", "dependencies",
                                "devDependencies", "workspaces") if k in pkg}
    dump = json.dumps(keep, indent=1, sort_keys=True)
    if len(dump) > PKG_CHARS:
        dump = dump[:PKG_CHARS] + "\n... (truncated)"
    return "```json\n" + dump + "\n```", pkg


def _deps(pkg: dict) -> dict:
    out = {}
    for k in ("dependencies", "devDependencies", "peerDependencies"):
        v = pkg.get(k)
        if isinstance(v, dict):
            out.update({str(a): str(b) for a, b in v.items()})
    return out


def summarize(app_dir: Path, cap: int = CAP) -> str:
    files = tracked(app_dir)
    if not files:
        return "(the app repo has no tracked files, or git could not list them)"
    parts = []
    pkg_text, pkg = _package(app_dir, files)
    deps = _deps(pkg)
    facts = [f"- Tracked files: {len(files)}"]
    fw = [label for key, label in FRAMEWORKS if key in deps]
    if fw:
        facts.append("- Frameworks/libraries detected from package.json: " + ", ".join(fw))
    pm = pkg.get("packageManager") if isinstance(pkg.get("packageManager"), str) else ""
    locks = [LOCKFILES[f] for f in files if f in LOCKFILES]
    if pm or locks:
        facts.append(f"- Package manager: {pm or '(no packageManager field)'}"
                     + (f"; lockfile(s): {', '.join(locks)}" if locks else "; no lockfile"))
    other = [label for f, label in OTHER_ECOSYSTEMS.items() if f in files]
    if other:
        facts.append("- Other ecosystem files at the root: " + ", ".join(other))
    if not pkg_text:
        facts.append("- No package.json at the root (not a Node project, or a monorepo with packages elsewhere)")
    parts.append("### Facts\n" + "\n".join(facts))
    parts.append("### File tree (directories with examples)\n```\n" + "\n".join(_tree(files)) + "\n```")
    if pkg_text:
        parts.append("### package.json (selected keys)\n" + pkg_text)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    tests = []
    for key in ("test", "test:unit", "test:e2e", "lint", "typecheck", "check", "build", "dev"):
        if isinstance(scripts.get(key), str):
            tests.append(f"- script `{key}`: `{scripts[key][:200]}`")
    tools = [label for key, label in TEST_TOOLS if key in deps]
    if tools:
        tests.append("- Tooling in dependencies: " + ", ".join(tools))
    cfgs = [f for f in files if "/" not in f and CONFIG_FILES.match(f)]
    if cfgs:
        tests.append("- Config files at the root: " + ", ".join(cfgs[:30]))
    test_files = [f for f in files if re.search(r"(^|/)(tests?|__tests__|e2e|spec)/|\.(test|spec)\.[cm]?[jt]sx?$", f)]
    if test_files:
        tests.append(f"- Test files: {len(test_files)} (e.g. {', '.join(test_files[:5])})")
    workflows = [f.split("/")[-1] for f in files if f.startswith(".github/workflows/")]
    if workflows:
        tests.append("- GitHub workflows: " + ", ".join(workflows[:20]))
    parts.append("### Test and tooling setup\n" + ("\n".join(tests) if tests else "- (none detected)"))
    readme = next((f for f in files if "/" not in f and f.lower() in ("readme.md", "readme", "readme.txt", "readme.rst")), "")
    if readme:
        text = _read_text(app_dir, readme).strip()
        if text:
            if len(text) > README_CHARS:
                text = text[:README_CHARS] + "\n... (truncated)"
            parts.append(f"### {readme} (start)\n```\n{text}\n```")
    out = "\n\n".join(parts)
    if len(out) > cap:
        note = "\n... (code summary truncated)"
        out = out[:cap - len(note)] + note
    return out


def code_roots(app_dir: Path) -> list[str]:
    """Top-level entries holding existing code: directories as 'name/', files by name. Skips dotfiles, docs,
    licences and the runner's human-owned folders."""
    roots: dict[str, bool] = {}
    for f in tracked(app_dir):
        top = f.split("/", 1)[0]
        if top.startswith("."):
            continue
        if "/" in f:
            if top.lower() in HUMAN_TOP:
                continue
            roots[top + "/"] = True
        else:
            if os.path.splitext(top)[1].lower() in NOT_CODE_EXT or top.lower() in NOT_CODE_FILES:
                continue
            if top in LOCKFILES:
                continue
            roots[top] = True
    return sorted(roots)


def coverage_warnings(allowed: list[str], app_dir: Path) -> list[str]:
    """Existing code paths no allowed_paths pattern reaches (the builders could not change them)."""
    files = tracked(app_dir)
    if not files:
        return []
    missing = []
    for root in code_roots(app_dir):
        if root.endswith("/"):
            under = [f for f in files if f.startswith(root)]
            if not any(path_allowed(f, allowed) for f in under):
                missing.append(root)
        elif not path_allowed(root, allowed):
            missing.append(root)
    if not missing:
        return []
    return ["allowed_paths does not cover existing code in: " + ", ".join(missing)]
