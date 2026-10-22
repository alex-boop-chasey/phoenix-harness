"""Planning self-audit check: PLAN.md's `## Self-audit` table against the overlooked-areas checklist.

The checklist (prompts/plan-audit-checklist.md) has one line per area, `- <id>: <what to check>`. The
planner answers every id in PLAN.md under `## Self-audit`, a table `| Area | Status | Where / why |`
(status `addressed` or `n/a`, reason at least MIN_REASON characters), and writes what it checked as
bullets under `## Audit findings`. Header and separator rows of the table are skipped; cells may have
surrounding spaces; ids and statuses are compared case-insensitively after trimming.

check(plan_md, checklist_md) returns the problems, one string each, in the planner-facing form:
  missing area: <id>            bad status for <id>: <status>        reason too short for <id>
  missing section: ## Self-audit / ## Audit findings                 empty section: ## Audit findings
The runner runs it next to the steps.json validation of every planner output: problems fail the attempt.

CLI: python3 -m hands.planaudit check PLAN.md [--checklist FILE]
Exit codes: 0 ok, 1 problems (one per line on stdout), 2 unreadable plan or checklist.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CHECKLIST_FILE = Path(__file__).resolve().parent.parent / "prompts" / "plan-audit-checklist.md"
DESIGN_IDS = ("auth", "errors", "empty-states", "validation", "accessibility", "responsive", "performance",
              "seo-meta", "security-headers", "secrets", "logging", "data-and-backups", "deploy", "rollback",
              "rate-limits", "dependency-age", "licences", "copy-and-i18n", "legal-pages", "analytics-consent",
              "tests-review-focus", "docs-conventions", "style-refs-applied", "setup-record-used")
AUDIT_SECTION = "## Self-audit"
FINDINGS_SECTION = "## Audit findings"
STATUSES = ("addressed", "n/a")
MIN_REASON = 8

ITEM = re.compile(r"^-\s+([A-Za-z0-9][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
HEADING = re.compile(r"^##\s+(.*?)\s*#*\s*$")
SEPARATOR = re.compile(r"^[\s:|-]+$")
BULLET = re.compile(r"^\s*[-*+]\s+\S")


def parse_checklist(text: str) -> list[tuple[str, str]]:
    """(id, what to check) for every `- <id>: <text>` line, in file order; other lines are ignored."""
    out = []
    for line in text.splitlines():
        m = ITEM.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def sections(md: str) -> dict[str, str]:
    """Body of every `## <title>` section, keyed by the lower-cased trimmed title (deeper headings stay inside)."""
    out: dict[str, str] = {}
    title: str | None = None
    body: list[str] = []
    for line in md.splitlines():
        m = HEADING.match(line)
        if m:
            if title is not None:
                out[title] = "\n".join(body)
            title, body = m.group(1).strip().lower(), []
        elif title is not None:
            body.append(line)
    if title is not None:
        out[title] = "\n".join(body)
    return out


def parse_table(text: str) -> list[tuple[str, str, str]]:
    """(area, status, reason) per data row of a `| a | b | c |` table: cells trimmed, header and
    separator rows skipped, missing cells empty."""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|") or SEPARATOR.match(s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        cells += [""] * (3 - len(cells))
        if cells[0].lower() == "area" and cells[1].lower() == "status":
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def check(plan_md: str, checklist_md: str) -> list[str]:
    """Problems with the plan's self-audit (empty = good), in table order then by checklist order."""
    problems: list[str] = []
    secs = sections(plan_md)
    audit = secs.get(AUDIT_SECTION[3:].lower())
    if audit is None:
        problems.append(f"missing section: {AUDIT_SECTION}")
    rows = parse_table(audit or "")
    seen: set[str] = set()
    for area, status, reason in rows:
        key = area.lower()
        seen.add(key)
        if status.lower() not in STATUSES:
            problems.append(f"bad status for {key}: {status}")
        if len(reason) < MIN_REASON:
            problems.append(f"reason too short for {key}")
    for cid, _ in parse_checklist(checklist_md):
        if cid.lower() not in seen:
            problems.append(f"missing area: {cid.lower()}")
    findings = secs.get(FINDINGS_SECTION[3:].lower())
    if findings is None:
        problems.append(f"missing section: {FINDINGS_SECTION}")
    elif not any(BULLET.match(ln) for ln in findings.splitlines()):
        problems.append(f"empty section: {FINDINGS_SECTION}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m hands.planaudit", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="list the self-audit problems of a PLAN.md (exit 1 when there are any)")
    p.add_argument("plan", type=Path)
    p.add_argument("--checklist", type=Path, default=CHECKLIST_FILE, help="checklist file (default: prompts/)")
    a = ap.parse_args(argv)
    try:
        plan_md = a.plan.read_text(errors="replace")
        checklist_md = a.checklist.read_text(errors="replace")
    except OSError as e:
        print(f"cannot read {e.filename}: {e.strerror}", file=sys.stderr)
        return 2
    problems = check(plan_md, checklist_md)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
