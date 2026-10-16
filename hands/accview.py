"""Per-step view of the spec's acceptance/ dir.

A step sees: shared files, files claimed by steps that already passed, and files claimed by the
current step. Files claimed only by later steps are hidden, so the coder cannot build ahead against
tests it is not meant to see yet.

A step claims: its `e2e` specs, its `acceptance_files` entries (files or dirs), and every
`acceptance/<path>` (or `/workspace/acceptance/<path>`) token in its own acceptance commands
(globs and trailing-slash dirs allowed). Shared: files nobody claims, and anything named by the
spec-wide `setup`, `regression` or `e2e_command` (those run for every step).

Parsing is strict (fail closed): a step command whose acceptance reference cannot be parsed
(`acceptance/{a,b}`, `acceptance/"x"`, `acceptance/$X`, bare `acceptance/`, `cd acceptance`) makes
the spec invalid; list such files in `acceptance_files` and keep the command simple.
Files referenced only indirectly (e.g. via a variable) are shared unless listed in acceptance_files;
`hands <p> validate` prints every step's view so this can be checked.

The view is a runner-owned copy on the host (regular files only), mounted read-only.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .spec import Spec, SpecError, Step, glob_to_regex

_PATH = r"[\w.*?/@+-]*"
TOKEN = re.compile(r"(?:^|(?<=[\s='\"(:,<`]))(?:/workspace/|\./)?acceptance/(" + _PATH + r")(?::\d+)?(?P<after>.?)")
OK_AFTER = set(" \t\n;&|)'\"<>,`") | {""}
HINT = "; list the files in this step's acceptance_files and reference them plainly as acceptance/<path>"


def tokens(cmd: str, where: str) -> list[str]:
    """acceptance/<path> patterns in a shell command. Raises SpecError on anything ambiguous."""
    out = []
    rest = TOKEN.sub(" ", cmd)
    if "acceptance" in rest.lower():
        raise SpecError(f"{where}: `{cmd}` mentions 'acceptance' in a form the runner cannot attribute to files "
                        f"(it only understands acceptance/<path>){HINT}")
    for m in TOKEN.finditer(cmd):
        pat, after = m.group(1), m.group("after")
        if after not in OK_AFTER:
            raise SpecError(f"{where}: cannot parse acceptance path in `{cmd}` (unsupported character {after!r} "
                            f"after acceptance/{pat}){HINT}")
        if pat.strip("/") in ("", ".", "*", "**"):
            raise SpecError(f"{where}: `{cmd}` names the whole acceptance/ dir; name specific files or a glob "
                            f"like acceptance/*.test.mjs")
        if ".." in pat.split("/"):
            raise SpecError(f"{where}: '..' not allowed in acceptance path in `{cmd}`")
        out.append(pat)
    return out


def all_files(spec_root: Path) -> list[str]:
    """Regular files under acceptance/, as posix paths relative to it. Symlinks are skipped."""
    acc = spec_root / "acceptance"
    if not acc.is_dir() or acc.is_symlink():
        return []
    out = []
    for root, dirs, files in os.walk(acc, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
        for f in sorted(files):
            p = os.path.join(root, f)
            if not os.path.islink(p) and os.path.isfile(p):
                out.append(Path(p).relative_to(acc).as_posix())
    return out


def symlinks(spec_root: Path) -> list[str]:
    acc = spec_root / "acceptance"
    if acc.is_symlink():
        return ["acceptance"]
    if not acc.is_dir():
        return []
    out = []
    for root, dirs, files in os.walk(acc, followlinks=False):
        for n in dirs + files:
            p = os.path.join(root, n)
            if os.path.islink(p):
                out.append(Path(p).relative_to(spec_root).as_posix())
    return out


def _match(pattern: str, files: list[str]) -> set[str]:
    pattern = pattern.strip()
    if any(ch in pattern for ch in "*?"):
        rx = glob_to_regex(pattern)
        return {f for f in files if rx.match(f)}
    pattern = pattern.rstrip("/")
    if pattern in ("", "."):
        return set(files)               # only reachable via acceptance_files ["."]: explicit "this step owns all"
    return {f for f in files if f == pattern or f.startswith(pattern + "/")}


def step_patterns(step: Step) -> list[str]:
    pats = [*step.e2e, *step.acceptance_files]
    for cmd in step.acceptance:
        pats += tokens(cmd, step.id)
    return pats


def shared_patterns(spec: Spec) -> list[str]:
    """Exact files named by spec-wide commands (they run for every step, so must be visible to all).
    Globs/dirs there claim nothing: they simply run over whatever the current step's view contains."""
    pats = []
    for cmd in [*spec.setup, *spec.regression, spec.e2e_command.replace("{files}", "")]:
        if cmd:
            pats += [p for p in tokens(cmd, "setup/regression/e2e_command")
                     if not p.endswith("/") and not any(ch in p for ch in "*?")]
    return pats


def validate(spec: Spec) -> None:
    """Called by spec.load: strict parse of every command, and no symlinks under acceptance/."""
    links = symlinks(spec.root)
    if links:
        raise SpecError(f"symlinks are not allowed under acceptance/: {', '.join(links[:5])}")
    shared_patterns(spec)
    for s in spec.steps:
        step_patterns(s)


def views(spec: Spec, files: list[str] | None = None) -> tuple[set[str], dict[str, set[str]]]:
    files = all_files(spec.root) if files is None else files
    forced_shared: set[str] = set()
    for p in shared_patterns(spec):
        forced_shared |= _match(p, files)
    by_step = {}
    for s in spec.steps:
        owned: set[str] = set()
        for p in step_patterns(s):
            owned |= _match(p, files)
        by_step[s.id] = owned - forced_shared
    claimed = set().union(*by_step.values()) if by_step else set()
    return set(files) - claimed, by_step


def visible(spec: Spec, step: Step, passed: set[str], files: list[str] | None = None) -> list[str]:
    shared, by_step = views(spec, files)
    show = set(shared)
    for sid, owned in by_step.items():
        if sid == step.id or sid in passed:
            show |= owned
    return sorted(show)


def build(spec: Spec, step: Step, passed: set[str], dest: Path) -> list[str]:
    """(Re)create dest as a copy of the visible files. Returns the relative paths copied."""
    from .workspace import rmtree
    rmtree(dest)
    dest.mkdir(parents=True)
    src = spec.root / "acceptance"
    copied = []
    for rel in visible(spec, step, passed):
        s, d = src / rel, dest / rel
        if s.is_symlink() or not s.is_file():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(s, d, follow_symlinks=False)
        os.chmod(d, 0o755 if os.stat(s).st_mode & 0o100 else 0o644)
        copied.append(rel)
    return copied
