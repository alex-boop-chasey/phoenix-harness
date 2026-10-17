#!/usr/bin/env python3
"""Sample projects for the UI screenshot rig (tools/ui-shots.sh). Stdlib only, no network, no Docker,
no Claude, nothing real: everything is written under the two directories given on the command line.

  python3 fixtures.py <config_home> <fixtures_root> [count] [hidden...]

Creates <config_home>/projects/<slug>.toml + usage.json, and per project under <fixtures_root>/<slug>/:
specs/ (git repo), app/ (git repo), state/ (state.json, logs, attempts). Covers every state the
dashboard shows:

  bookings  building: runner alive, current step in flight, live activity line
  crm       failed step + escalation
  blog      done (all steps passed)
  wiki      stopped (STOP file)
  landing   plan ready for review (managed; candidate plan + PR metadata)
  notes     planning right now (managed; planner lock held, live activity)

[count] (default 6) = how many projects: 0..6 takes the first N of the list above; above 6 adds
simple extra projects (done / idle / stopped / paused) named from EXTRA, up to 20. [hidden...] =
project names written to <config_home>/ui.json as closed tabs.

"alive"/"planning" need a held flock, so this script leaves one small sleeping child per lock
(they die with the container; the rig runs this inside a --rm container only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from hashlib import sha1
from pathlib import Path

NOW = int(time.time())
IMAGE = "hands:cc2.1.301-pw1.64.1"


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def sha(seed: str) -> str:
    return sha1(seed.encode()).hexdigest()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixtures", "-c", "user.email=fixtures@local",
                    *args], check=True, capture_output=True)


def make_repo(d: Path, files: dict[str, str]) -> None:
    d.mkdir(parents=True)
    git(d.parent, "init", "-q", d.name)
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "fixture")


def steps_json(slug: str, steps: list[tuple[str, str]]) -> str:
    return json.dumps({
        "version": 1,
        "allowed_paths": ["src/", "test/", "package.json"],
        "steps": [{"id": sid, "title": title, "goal": f"{title} for the {slug} app.",
                   "acceptance": ["npm test"]} for sid, title in steps],
    }, indent=1)


def hold_lock(path: Path) -> None:
    """Leave a child holding an exclusive flock on path (so the dashboard sees alive/planning)."""
    src = ("import fcntl,sys,time\n"
           "f=open(sys.argv[1],'w')\nfcntl.flock(f,fcntl.LOCK_EX)\ntime.sleep(1800)\n")
    subprocess.Popen([sys.executable, "-c", src, str(path)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):          # wait until it is actually held
        import fcntl
        with open(path, "a") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                return
        time.sleep(0.1)
    raise SystemExit(f"lock holder for {path} did not start")


def jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def attempt_dir(state: Path, name: str, *, passed: bool, reasons: list[str] | None = None) -> None:
    d = state / "attempts" / name
    d.mkdir(parents=True)
    (d / "prompt.md").write_text(f"# Step {name.split('-')[0]}\nSample coder prompt for the screenshot rig.\n")
    (d / "changes.txt").write_text("M 100644 src/app.ts\nA 100644 test/app.test.ts\n")
    (d / "diff.patch").write_text("--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1 +1,2 @@\n-export {}\n+export const ok = true\n")
    (d / "accept-0.log").write_text("### npm test\n> test\n2 passing\n" if passed else "### npm test\n> test\n1 failing\n  1) app renders\n")
    (d / "coder.stderr").write_text("")
    (d / "verdict.json").write_text(json.dumps(
        {"pass": passed, "reasons": [] if passed else ["the failing test was not addressed"], "nonce": "0" * 16}, indent=1))
    if reasons:
        (d / "reasons.txt").write_text("\n".join(reasons) + "\n")


def metrics(step_ids: list[str]) -> list[dict]:
    rows = []
    for i, sid in enumerate(step_ids):
        t = NOW - (len(step_ids) - i) * 900
        for role, cost, turns in (("coder", 0.42, 38), ("eval", 0.11, 6)):
            rows.append({"ts": iso(t), "role": role, "step": sid, "attempt": 1, "kind": "ok", "rc": 0,
                         "turns": turns, "cost_usd": cost, "models": ["claude-fable-5-1"],
                         "duration_s": 310 if role == "coder" else 70, "spec_commit": sha("spec")})
    return rows


def progress(steps: list[tuple[str, str]]) -> list[dict]:
    return [{"ts": iso(NOW - (len(steps) - i) * 900), "step": sid, "title": title, "commit": sha(sid),
             "attempts": 1, "summary": f"Implemented {title.lower()} with tests; all acceptance commands pass.",
             "risks": []} for i, (sid, title) in enumerate(steps)]


def runner_log(state: Path, lines: list[str]) -> None:
    (state / "runner.log").write_text("".join(f"{iso(NOW - 60 * (len(lines) - i))} {l}\n" for i, l in enumerate(lines)))


def write_toml(cfg_home: Path, slug: str, root: Path, managed: bool = False) -> None:
    extra = ""
    if managed:
        extra = (f'managed = true\ngithub_org = "acme"\nrepo_specs = "{slug}-specs"\nrepo_app = "{slug}-app"\n')
    (cfg_home / "projects" / f"{slug}.toml").write_text(
        f'spec_dir = "{root}/specs"\napp_dir = "{root}/app"\nstate_dir = "{root}/state"\n'
        f'image = "{IMAGE}"\n{extra}')


def state_json(state: Path, status: str, reason: str, steps: dict, *, current=None, extra=None) -> None:
    d = {"status": status, "reason": reason, "steps": steps, "updated_at": iso(NOW - 30),
         "total_sessions": 2 * len(steps) or 2, "total_cost_usd": round(0.53 * (len(steps) or 2), 2)}
    if current:
        d["current"] = current
    d.update(extra or {})
    state.mkdir(parents=True)
    (state / "state.json").write_text(json.dumps(d, indent=1))


def passed(sid: str) -> dict:
    return {"status": "passed", "attempts": 1, "commit": sha(sid), "reasons": [],
            "passed_at": iso(NOW - 3600)}


def p_bookings(cfg_home: Path, root: Path) -> None:
    """building right now"""
    steps =[("S01", "Project scaffold"), ("S02", "Data model"), ("S03", "Booking form"),
             ("S04", "Calendar view"), ("S05", "Email notifications"), ("S06", "Polish and a11y")]
    r = root / "bookings"
    make_repo(r / "specs", {"steps.json": steps_json("bookings", steps)})
    make_repo(r / "app", {"src/app.ts": "export {}\n", "package.json": "{}\n"})
    st = r / "state"
    state_json(st, "running", "", {s: passed(s) for s, _ in steps[:2]},
               current={"step": "S03", "attempt": 1, "base": sha("base"), "phase": "coder"})
    (st / "activity.json").write_text(json.dumps({"text": "editing src/routes/booking.ts", "at": NOW - 5}))
    jsonl(st / "metrics.jsonl", metrics([s for s, _ in steps[:2]]))
    jsonl(st / "progress.jsonl", progress(steps[:2]))
    runner_log(st, ["runner started (coder claude-fable-5-1, image " + IMAGE + ")",
                    "=== S01 attempt 1/3: Project scaffold (base {}, spec {})".format(sha("base")[:9], sha("spec")[:9]),
                    "PASSED S01 @ {} (pushed)".format(sha("S01")[:9]),
                    "PASSED S02 @ {} (pushed)".format(sha("S02")[:9]),
                    "=== S03 attempt 1/3: Booking form (base {}, spec {})".format(sha("base")[:9], sha("spec")[:9])])
    for s, _ in steps[:2]:
        attempt_dir(st, f"{s}-1", passed=True)
    hold_lock(st / "runner.lock")
    write_toml(cfg_home, "bookings", r)


def p_crm(cfg_home: Path, root: Path) -> None:
    """a step failed, escalated"""
    steps =[("S01", "Project scaffold"), ("S02", "Contacts table"), ("S03", "Import CSV"),
             ("S04", "Deal pipeline"), ("S05", "Reports")]
    r = root / "crm"
    make_repo(r / "specs", {"steps.json": steps_json("crm", steps)})
    make_repo(r / "app", {"src/app.ts": "export {}\n"})
    st = r / "state"
    reasons = ["acceptance command failed (exit 1): `npm test`",
               "last output:\n  1) pipeline drag and drop\n     Expected stage 'won', got 'lost'"]
    sd = {s: passed(s) for s, _ in steps[:3]}
    sd["S04"] = {"status": "failed", "attempts": 3, "reasons": reasons}
    state_json(st, "escalation", "S04 failed 3 attempts. Last: " + reasons[0], sd)
    jsonl(st / "metrics.jsonl", metrics([s for s, _ in steps[:3]] + ["S04"] * 3))
    jsonl(st / "progress.jsonl", progress(steps[:3]))
    runner_log(st, ["PASSED S03 @ {} (pushed)".format(sha("S03")[:9]),
                    "=== S04 attempt 3/3: Deal pipeline (base {}, spec {})".format(sha("base")[:9], sha("spec")[:9]),
                    "FAILED S04 attempt 3/3: " + reasons[0],
                    "ESCALATION: S04 failed 3 attempts. Last: " + reasons[0]])
    for s, _ in steps[:3]:
        attempt_dir(st, f"{s}-1", passed=True)
    for n in (1, 2, 3):
        attempt_dir(st, f"S04-{n}", passed=False, reasons=reasons)
    write_toml(cfg_home, "crm", r)


def p_blog(cfg_home: Path, root: Path) -> None:
    """done"""
    steps =[("S01", "Project scaffold"), ("S02", "Posts and tags"), ("S03", "RSS feed"), ("S04", "Dark mode")]
    r = root / "blog"
    make_repo(r / "specs", {"steps.json": steps_json("blog", steps)})
    make_repo(r / "app", {"src/app.ts": "export {}\n"})
    st = r / "state"
    state_json(st, "done", "all steps passed", {s: passed(s) for s, _ in steps})
    jsonl(st / "metrics.jsonl", metrics([s for s, _ in steps]))
    jsonl(st / "progress.jsonl", progress(steps))
    runner_log(st, ["PASSED S04 @ {} (pushed)".format(sha("S04")[:9]), "DONE: all steps passed"])
    for s, _ in steps:
        attempt_dir(st, f"{s}-1", passed=True)
    write_toml(cfg_home, "blog", r)


def p_wiki(cfg_home: Path, root: Path) -> None:
    """stopped by the STOP file"""
    steps =[("S01", "Project scaffold"), ("S02", "Page editor"), ("S03", "Search"),
             ("S04", "History"), ("S05", "Permissions")]
    r = root / "wiki"
    make_repo(r / "specs", {"steps.json": steps_json("wiki", steps)})
    make_repo(r / "app", {"src/app.ts": "export {}\n"})
    st = r / "state"
    state_json(st, "stopped", "STOP file", {s: passed(s) for s, _ in steps[:3]})
    (st / "STOP").write_text("")
    jsonl(st / "metrics.jsonl", metrics([s for s, _ in steps[:3]]))
    jsonl(st / "progress.jsonl", progress(steps[:3]))
    runner_log(st, ["PASSED S03 @ {} (pushed)".format(sha("S03")[:9]), "STOP file present; stopping"])
    for s, _ in steps[:3]:
        attempt_dir(st, f"{s}-1", passed=True)
    write_toml(cfg_home, "wiki", r)


def p_landing(cfg_home: Path, root: Path) -> None:
    """managed, plan ready for review (no steps.json on main yet)"""
    r = root / "landing"
    make_repo(r / "specs", {"README.md": "# landing-specs\n"})
    make_repo(r / "app", {"README.md": "# landing-app\n"})
    (r / "description.txt").write_text("Marketing site for a small bakery: menu, opening hours, order form.\n")
    st = r / "state"
    state_json(st, "idle", "", {})
    plan = st / "plan"
    cand = plan / "candidate"
    cand.mkdir(parents=True)
    (cand / "PLAN.md").write_text(
        "# Plan: landing\n\nAstro static site, 6 steps: scaffold, menu page, hours, order form "
        "(posts to Formspree), styling, Lighthouse pass.\n")
    (cand / "steps.json").write_text(json.dumps({
        "version": 1, "allowed_paths": ["src/", "public/", "package.json"],
        "setup": ["pnpm install --frozen-lockfile"], "regression": ["pnpm test"],
        "always_refs": ["docs/conventions.md"],
        "steps": [
            {"id": "S01", "title": "Scaffold Astro site", "goal": "Astro + Tailwind scaffold.",
             "acceptance": ["pnpm build"]},
            {"id": "S02", "title": "Menu page", "goal": "Menu from a JSON file.",
             "acceptance": ["pnpm test"]},
            {"id": "S03", "title": "Order form", "goal": "Form posting to Formspree.",
             "acceptance": ["pnpm test", "curl -s https://formspree.io/f/demo"]},
        ]}, indent=1))
    (plan / "plan.json").write_text(json.dumps({
        "state": "ready", "error": "", "summary": "6 steps: Astro static site with menu, hours and order form.",
        "files": ["steps.json", "PLAN.md", "docs/design.md"], "steps": 6,
        "domains": ["formspree.io"], "pr": 3, "pr_url": "https://github.com/acme/landing-specs/pull/3",
        "head_sha": sha("landing-head"), "base_sha": sha("landing-base"),
        "started": NOW - 1500, "updated": NOW - 60}))
    (plan / "plan.log").write_text("".join(f"{time.strftime('%H:%M:%S', time.gmtime(NOW - 1500 + i * 120))} {l}\n" for i, l in enumerate([
        "=== planning", "planner session (claude-fable-5-1, web search on) started",
        "writing docs/design.md", "writing steps.json",
        "planner session ended: kind=ok turns=92 cost~$1.84 1180s",
        "PR #3 https://github.com/acme/landing-specs/pull/3 @ " + sha("landing-head")[:9],
        "READY: 6 steps: Astro static site with menu, hours and order form.",
    ])))
    write_toml(cfg_home, "landing", r, managed=True)


def p_notes(cfg_home: Path, root: Path) -> None:
    """managed, planner running right now"""
    r = root / "notes"
    make_repo(r / "specs", {"README.md": "# notes-specs\n"})
    make_repo(r / "app", {"README.md": "# notes-app\n"})
    (r / "description.txt").write_text("Personal note-taking app with markdown and full-text search.\n")
    st = r / "state"
    state_json(st, "idle", "", {})
    plan = st / "plan"
    plan.mkdir(parents=True)
    (plan / "plan.json").write_text(json.dumps({"state": "planning", "error": "",
                                                "started": NOW - 540, "updated": NOW - 10}))
    (plan / "plan-activity.json").write_text(json.dumps({
        "activity": {"text": "writing docs/design.md", "at": NOW - 10,
                     "file": "docs/design.md", "phase": "Design (brainstorming)"},
        "files_written": ["docs/design.md"]}))
    (plan / "plan.log").write_text("".join(f"{time.strftime('%H:%M:%S', time.gmtime(NOW - 540 + i * 60))} {l}\n" for i, l in enumerate([
        "=== planning", "planner session (claude-fable-5-1, web search on) started",
        "searching: markdown editor component comparison", "writing docs/design.md"])))
    hold_lock(plan / "plan.lock")
    write_toml(cfg_home, "notes", r, managed=True)


STANDARD = [p_bookings, p_crm, p_blog, p_wiki, p_landing, p_notes]
EXTRA = ["shop", "forum", "invoices", "chat", "gallery", "recipes", "budget", "habits", "fitness", "quiz",
         "inventory", "tickets", "portfolio", "newsletter"]


def p_extra(cfg_home: Path, root: Path, slug: str, i: int) -> None:
    """simple projects to fill a wide tab strip: done / idle / stopped / paused, in turn"""
    steps = [("S01", "Project scaffold"), ("S02", "Core feature"), ("S03", "Polish")]
    r = root / slug
    make_repo(r / "specs", {"steps.json": steps_json(slug, steps)})
    make_repo(r / "app", {"src/app.ts": "export {}\n"})
    st = r / "state"
    kind = ("done", "idle", "stopped", "paused_rate_limit")[i % 4]
    done = {"done": 3, "idle": 0, "stopped": 1, "paused_rate_limit": 2}[kind]
    reason = {"done": "all steps passed", "idle": "", "stopped": "STOP file",
              "paused_rate_limit": "rate limit: resets 3pm"}[kind]
    state_json(st, kind, reason, {s: passed(s) for s, _ in steps[:done]},
               extra={"updated_at": iso(NOW - 3600 * (i + 2))})
    if kind == "stopped":
        (st / "STOP").write_text("")
    if done:
        jsonl(st / "metrics.jsonl", metrics([s for s, _ in steps[:done]]))
        jsonl(st / "progress.jsonl", progress(steps[:done]))
        runner_log(st, [f"PASSED S0{done} @ {sha('S0' + str(done))[:9]} (pushed)"])
    write_toml(cfg_home, slug, r)


def main() -> int:
    cfg_home, root = Path(sys.argv[1]), Path(sys.argv[2])
    count = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "" else len(STANDARD)
    hidden = sys.argv[4:]
    if not 0 <= count <= len(STANDARD) + len(EXTRA):
        raise SystemExit(f"count must be 0..{len(STANDARD) + len(EXTRA)}")
    (cfg_home / "projects").mkdir(parents=True, exist_ok=True)
    (cfg_home / "usage.json").write_text(json.dumps(
        {"status": "allowed", "at": NOW - 240,
         "five_hour": {"used_pct": 42.0, "resets_at": NOW + 2 * 3600},
         "seven_day": {"used_pct": 31.5, "resets_at": NOW + 4 * 86400}}))
    for make in STANDARD[:count]:
        make(cfg_home, root)
    for i, slug in enumerate(EXTRA[:max(0, count - len(STANDARD))]):
        p_extra(cfg_home, root, slug, i)
    names = sorted(p.stem for p in (cfg_home / "projects").glob("*.toml"))
    bad = [h for h in hidden if h not in names]
    if bad:
        raise SystemExit(f"hidden projects not in the fixture set: {' '.join(bad)}")
    if hidden:
        (cfg_home / "ui.json").write_text(json.dumps({"hidden": sorted(hidden)}, indent=1) + "\n")
    print(f"fixtures ready: {', '.join(names) or '(no projects)'}" + (f"; closed: {', '.join(sorted(hidden))}" if hidden else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
