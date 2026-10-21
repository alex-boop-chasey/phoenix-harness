"""Merge tap and discard (0.5.8, B3): the ONE merge path for a change round, on the runner's app clone with
its deploy key (plain git, no GitHub API, never a force push).

Callers: the dashboard tap / `hands <p> merge` for ordinary projects, and the installer (Install update,
U3) for a `self` project (Phoenix building itself). `check_tap` decides who may press; `merge_round` is
the merge itself and does not know about the tap.

  merge_round:  origin/main must still be the round's base and origin/change/C<n> the shown head; squash
                locally, verify the merged tree is exactly the tested tree, tag hands/merged/C<n> at the
                unsquashed head, push main (a non-fast-forward is refused by git and rolled back locally),
                record {status: merged, merged_sha, head_sha, merged_tag}, delete change/C<n>.
                hands/pre-C<n> (state before the change) and hands/merged/C<n> (the unsquashed work) stay.
  discard_round: tag hands/discarded/C<n>-<utc> at the shown head, delete change/C<n> and hands/pre-C<n>,
                 put the round's steps back to pending and drop the record. The steps stay in the spec: the
                 next start rebuilds them in a fresh round C<n>. Discarding the work is not discarding the
                 change request (that needs a spec PR).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import gitops, rounds
from . import spec as specmod
from .config import Config
from .state import Lock, State

SHA = re.compile(r"^[0-9a-f]{40}$")
IDENT = ("-c", "user.name=hands", "-c", "user.email=hands@localhost")
MERGED_TAG = "hands/merged/C{round}"
DISCARD_TAG = "hands/discarded/C{round}-{ts}"
EVENTS = "rounds.jsonl"
SELF_WHY = "Phoenix updates itself: this round is merged by Install update"


class MergeError(Exception):
    """Refused; main on origin is untouched, the record keeps its state (plus `error`)."""


class LockedError(MergeError):
    """The runner lock is held: refused before the record was even read (nothing saved)."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def events(state_dir: Path) -> list[dict]:
    p = state_dir / EVENTS
    out = []
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x, dict) and isinstance(x.get("id"), int):
            out.append(x)
    return out


def append_event(st: State, event: str, rec: dict, **extra) -> None:
    keep = ("id", "branch", "base_sha", "pre_tag", "spec_commit", "request", "pr", "pr_url",
            "head_sha", "merged_sha", "merged_tag", "merged_at")
    st.append_jsonl(EVENTS, {"event": event, **{k: rec[k] for k in keep if k in rec}, **extra})


def round_steps(data: dict, step_ids: list[str] | None, rnd: int) -> tuple[list[str], list[str]]:
    """(passed, pending) ids of round `rnd` in spec order (unknown spec: from the state records)."""
    ids = list(step_ids) if step_ids is not None else sorted(data.get("steps") or {})
    mine = [s for s in ids if rounds.round_of(s) == rnd]           # by id: a failed step has no `round` record yet
    passed = [s for s in mine if (data.get("steps") or {}).get(s, {}).get("status") == "passed"]
    return passed, [s for s in mine if s not in passed]


def tap_state(cfg: Config, data: dict, step_ids: list[str] | None) -> dict:
    """What the Merge tap may do now; derived from files only (no network). `ready` = pressing it would start a
    merge; `why` says what is missing otherwise. For a `self` project the tap is never ready (the installer
    merges), the dashboard renders that instead of the tap."""
    rec = rounds.record(data)
    out = {"state": "none", "ready": False, "why": "", "branch_sha": "", "main_sha": "", "self": bool(cfg.is_self),
           "all_passed": False, "passed": 0, "total": 0, "pending": [], "error": ""}
    if not rec:
        return out
    out["state"] = str(rec.get("status") or "open")
    out["error"] = str(rec.get("error") or "")
    passed, pending = round_steps(data, step_ids, int(rec["id"]))
    out.update(passed=len(passed), total=len(passed) + len(pending), pending=pending, all_passed=not pending and bool(passed))
    if out["state"] != "open":
        out["why"] = {"merging": "merge in progress", "discarding": "discard in progress",
                      "merged": "merged"}.get(out["state"], out["state"])
        return out
    local = gitops.git(cfg.app_dir, "rev-parse", "-q", "--verify", f"refs/heads/{rec.get('branch')}", check=False).strip()
    out["branch_sha"], out["main_sha"] = local, str(rec.get("base_sha") or "")
    if cfg.is_self:
        out["why"] = SELF_WHY
    elif pending:
        out["why"] = f"round C{rec['id']} is not complete: {', '.join(pending[:5])} pending"
    elif not passed:
        out["why"] = f"round C{rec['id']} has no steps"
    elif not cfg.push:
        out["why"] = "push is disabled for this project: nothing on origin to merge"
    elif Lock.held(cfg.state_dir / "runner.lock"):
        out["why"] = "the build is running"
    elif not SHA.fullmatch(local):
        out["why"] = f"the clone has no {rec.get('branch')} branch; start the runner once, then merge"
    else:
        out["ready"] = True
    return out


def check_tap(cfg: Config, data: dict, step_ids: list[str] | None) -> dict:
    t = tap_state(cfg, data, step_ids)
    if not t["ready"]:
        raise MergeError(t["why"] or f"no open round to merge (state: {t['state']})")
    return t


def _check_open(cfg: Config, st: State, step_ids: list[str], allow_states=("open",)) -> dict:
    rec = rounds.record(st.data)
    if not rec or rec.get("status") not in allow_states:
        raise MergeError(f"no open round (state: {rec.get('status') if rec else 'none'})")
    if not cfg.push:
        raise MergeError("push is disabled for this project: nothing on origin to merge")
    passed, pending = round_steps(st.data, step_ids, int(rec["id"]))
    if pending:
        raise MergeError(f"round C{rec['id']} is not complete: {', '.join(pending[:5])} pending")
    if not passed:
        raise MergeError(f"round C{rec['id']} has no steps")
    return rec


def _spec_ids(cfg: Config) -> list[str]:
    try:
        return [s.id for s in specmod.load(cfg.spec_dir, gitops.head(cfg.spec_dir)).steps]
    except (specmod.SpecError, gitops.GitError, OSError) as e:
        raise MergeError(f"the spec clone is not loadable: {e}")


def step_ids(cfg: Config) -> list[str] | None:
    """Step ids straight from the spec clone's steps.json (pending steps are not in state.json yet); None when
    unreadable (callers then count from the state records only and the CLI's full check decides)."""
    try:
        raw = json.loads((cfg.spec_dir / "steps.json").read_text())
        return [x["id"] for x in raw.get("steps") or [] if isinstance(x, dict) and isinstance(x.get("id"), str)]
    except (OSError, ValueError, AttributeError):
        return None


def _lock(cfg: Config) -> Lock:
    """The runner lock for the whole merge/discard (the runner and `plan --amend` are excluded meanwhile). Not
    getting it is reported without touching state.json: a runner owns it now."""
    try:
        return Lock(cfg.state_dir / "runner.lock").__enter__()
    except SystemExit:
        raise LockedError("the build is running; stop it first")


def _fail(st: State, msg: str) -> None:
    rec = rounds.record(st.data)
    if rec and rec.get("status") in ("merging", "discarding"):
        rec["status"] = "open"
    if rec:
        rec["error"] = msg[:1000]
    st.save()


def _squash(app: Path, branch_sha: str, message: str) -> None:
    gitops.git(app, "merge", "-q", "--squash", branch_sha)
    gitops.git(app, *IDENT, "commit", "-q", "-m", message)


def merge_round(cfg: Config, st: State, log, send, branch_sha: str, main_sha: str) -> dict:
    """Sha-pinned squash merge of the open round into cfg.branch. Raises MergeError with nothing changed on origin
    (local main is put back). Returns the merged record."""
    if not (SHA.fullmatch(branch_sha or "") and SHA.fullmatch(main_sha or "")):
        raise MergeError("branch_sha and main_sha must be full 40-hex commit ids (the ones the page showed)")
    lk = _lock(cfg)
    try:
        try:
            return _merge(cfg, st, log, send, branch_sha, main_sha)
        except gitops.GitError as e:
            raise MergeError(f"git failed: {e}")
        except MergeError:
            raise
        except Exception as e:                          # never leave the record stuck in `merging`
            raise MergeError(f"merge failed: {type(e).__name__}: {e}")
    except MergeError as e:
        _fail(st, str(e))
        append_event(st, "merge_failed", rounds.record(st.data) or {}, error=str(e)[:500])
        log(f"merge refused: {e}")
        raise
    finally:
        lk.__exit__(None, None, None)


def _merge(cfg: Config, st: State, log, send, branch_sha: str, main_sha: str) -> dict:
    app, main = cfg.app_dir, cfg.branch
    ids = _spec_ids(cfg)
    rec = _check_open(cfg, st, ids, ("open", "merging"))
    rnd, branch = int(rec["id"]), str(rec["branch"])
    tag = MERGED_TAG.format(round=rnd)
    if main_sha != rec.get("base_sha"):
        raise MergeError(f"main moved since round C{rnd} opened (built on {str(rec.get('base_sha'))[:9]}, shown "
                         f"{main_sha[:9]}): the round was tested on the old base; discard it or merge by hand")
    gitops.git(app, "fetch", "-q", "origin", main)
    origin_main = gitops.git(app, "rev-parse", "FETCH_HEAD").strip()
    if origin_main != main_sha:
        raise MergeError(f"main moved since round C{rnd} opened (origin {origin_main[:9]}, built on {main_sha[:9]}): "
                         f"the round was tested on the old base; discard it or merge by hand")
    origin_head = gitops.remote_sha(app, f"refs/heads/{branch}")
    if origin_head != branch_sha:
        raise MergeError(f"head of {branch} moved since the page showed it ({origin_head[:9] or 'gone'} vs "
                         f"{branch_sha[:9]}); reload and look again")
    remote_tag = gitops.remote_sha(app, f"refs/tags/{tag}")
    if remote_tag and remote_tag != branch_sha:
        raise MergeError(f"tag {tag} already exists on origin at {remote_tag[:9]}; remove it by hand first")
    gitops.git(app, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    if not gitops.is_clean(app):
        gitops.git(app, "reset", "-q", "--hard", "HEAD")
        gitops.git(app, "clean", "-qfdx")
    passed, _ = round_steps(st.data, ids, rnd)
    # the page pins what it showed; what it showed must be the tested head = the last passed code step's commit
    # (human steps record "human"; a round of human steps only has no tested commit and skips this check)
    tested = [(sid, str((st.data.get("steps") or {}).get(sid, {}).get("commit") or "")) for sid in passed]
    tested = [(sid, c) for sid, c in tested if SHA.fullmatch(c)]
    if tested and tested[-1][1] != branch_sha:
        raise MergeError(f"{branch} ({branch_sha[:9]}) has commits after the last passed step {tested[-1][0]} "
                         f"({tested[-1][1][:9]}); only the tested head is merged. Remove them, or discard")
    first = str(rec.get("request") or "").strip().splitlines()[:1]
    title = f"Change C{rnd}: {first[0][:72]}" if first else f"Change C{rnd}"
    message = (f"{title}\n\nSquash of {branch} ({branch_sha}): steps {', '.join(passed)}.\n"
               f"Unsquashed work: tag {tag}. State before the change: tag {rec.get('pre_tag')}.")
    prev = gitops.git(app, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    gitops.git(app, "checkout", "-q", "-B", main, main_sha)
    try:
        _squash(app, branch_sha, message)
        if gitops.git(app, "rev-parse", "HEAD^{tree}").strip() != gitops.git(app, "rev-parse", f"{branch_sha}^{{tree}}").strip():
            raise MergeError("merged tree differs from the tested round head; nothing pushed")
        local_tag = gitops.git(app, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False).strip()
        if local_tag and local_tag != branch_sha:
            raise MergeError(f"tag {tag} exists locally at {local_tag[:9]}; remove it by hand first")
        try:
            gitops.push_ref(app, "HEAD", f"refs/heads/{main}")
        except gitops.GitError as e:
            raise MergeError(f"main moved between the check and the push (git: {str(e)[-200:]}); press Merge again")
    except Exception:
        gitops.git(app, "reset", "-q", "--hard", main_sha, check=False)   # local main back to the base (also mid-merge)
        gitops.git(app, "clean", "-qfdx", check=False)
        if prev and prev not in ("HEAD", main):
            gitops.git(app, "checkout", "-q", prev, check=False)      # and the clone back where it was
        raise
    # main is merged: from here on nothing is refused; the record says so first, then the tag and the branch delete
    merged = gitops.head(app)
    rec.update({"status": "merged", "head_sha": branch_sha, "merged_sha": merged, "merged_tag": tag,
                "merged_at": _now()})
    rec.pop("error", None)
    st.save()
    append_event(st, "merged", rec)
    log(f"merged round C{rnd}: {branch} ({branch_sha[:9]}) squashed into {main} as {merged[:9]}; tag {tag}")
    try:
        if not local_tag:
            gitops.git(app, "tag", tag, branch_sha)
        if not remote_tag:
            gitops.push_ref(app, f"refs/tags/{tag}", f"refs/tags/{tag}")       # the unsquashed work stays reachable
        now_head = gitops.remote_sha(app, f"refs/heads/{branch}")
        if now_head != branch_sha:                                          # someone pushed in the window: keep it
            log(f"WARN: {branch} moved to {now_head[:9] or 'gone'} after the merge check; left on origin, not deleted")
        else:
            gitops.push_ref(app, "", f"refs/heads/{branch}")
            gitops.git(app, "branch", "-q", "-D", branch, check=False)
            gitops.git(app, "update-ref", "-d", f"refs/remotes/origin/{branch}", check=False)
    except gitops.GitError as e:
        log(f"WARN: {branch} was merged but its tag/delete did not finish: {e}; the branch is left on origin")
    send(f"merged C{rnd} into {main} ({merged[:9]})" + (f": {first[0][:120]}" if first else ""))
    return rec


def discard_round(cfg: Config, st: State, log, send, branch_sha: str) -> None:
    """Throw the round's build away (work kept under a tag) and put its steps back to pending."""
    if not SHA.fullmatch(branch_sha or ""):
        raise MergeError("branch_sha must be the full 40-hex commit id the page showed")
    lk = _lock(cfg)
    try:
        try:
            _discard(cfg, st, log, send, branch_sha)
        except gitops.GitError as e:
            raise MergeError(f"git failed: {e}")
        except MergeError:
            raise
        except Exception as e:
            raise MergeError(f"discard failed: {type(e).__name__}: {e}")
    except MergeError as e:
        _fail(st, str(e))
        log(f"discard refused: {e}")
        raise
    finally:
        lk.__exit__(None, None, None)


def _discard(cfg: Config, st: State, log, send, branch_sha: str) -> None:
    app, main = cfg.app_dir, cfg.branch
    rec = rounds.record(st.data)
    if not rec or rec.get("status") not in ("open", "discarding"):
        raise MergeError(f"no open round (state: {rec.get('status') if rec else 'none'})")
    rnd, branch, pre = int(rec["id"]), str(rec["branch"]), str(rec.get("pre_tag") or "")
    keep = DISCARD_TAG.format(round=rnd, ts=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    head = (gitops.remote_sha(app, f"refs/heads/{branch}") if cfg.push
            else gitops.git(app, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False).strip())
    if head != branch_sha:
        raise MergeError(f"head of {branch} moved since the page showed it ({head[:9] or 'gone'} vs "
                         f"{branch_sha[:9]}); reload and look again")
    if cfg.push:
        gitops.git(app, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    gitops.git(app, "tag", keep, branch_sha)
    if cfg.push:
        gitops.push_ref(app, f"refs/tags/{keep}", f"refs/tags/{keep}")
        gitops.git(app, "fetch", "-q", "origin", main)
        gitops.git(app, "checkout", "-q", "-B", main, "FETCH_HEAD")
    else:
        gitops.git(app, "checkout", "-q", "-B", main, main)
    gitops.git(app, "clean", "-qfdx")
    if cfg.push:
        for ref in (f"refs/heads/{branch}", f"refs/tags/{pre}" if pre else ""):
            if ref:
                try:
                    gitops.push_ref(app, "", ref)
                except gitops.GitError as e:
                    log(f"WARN: could not delete {ref} on origin: {e}")
    gitops.git(app, "branch", "-q", "-D", branch, check=False)
    gitops.git(app, "update-ref", "-d", f"refs/remotes/origin/{branch}", check=False)
    if pre:
        gitops.git(app, "tag", "-d", pre, check=False)
    reset = []
    for sid, x in list((st.data.get("steps") or {}).items()):
        if rounds.round_of(sid) == rnd or int(x.get("round") or 0) == rnd:
            st.data["steps"][sid] = {"status": "pending", "attempts": 0, "reasons": []}
            reset.append(sid)
    append_event(st, "discarded", rec, discarded_tag=keep, head_sha=branch_sha, steps=sorted(reset))
    st.data.pop("round", None)
    st.data.pop("current", None)
    if st.status in ("done", "escalation"):
        st.data["status"], st.data["reason"] = "idle", f"round C{rnd} discarded"
    st.save()
    log(f"discarded round C{rnd}: {branch} ({branch_sha[:9]}) kept as tag {keep}; steps back to pending: {', '.join(sorted(reset))}")
    send(f"discarded round C{rnd} (work kept as {keep}); its steps are pending again, Start rebuilds them")


def reset_refusal(cfg: Config, data: dict, sid: str, x: dict) -> str:
    """Why `reset-step sid` is refused ('' = allowed). Passed steps of a merged or earlier round, and round-0 steps once
    a round exists, are never rebuilt: rebuilding them on main would bypass the round branch, the pre-tag and the
    regression rule. Only steps of the open round (or any step of a project without rounds) can be reset."""
    rec = rounds.record(data)
    if not rec:
        return ""
    mine, cur = int(x.get("round") or 0), int(rec["id"])
    if mine < cur or (mine == cur and rec.get("status") == "merged"):
        if rec.get("status") == "merged" and mine == cur:
            where = f"merged into {cfg.branch} as {str(rec.get('merged_sha', ''))[:9]}: merged work is never rebuilt"
        else:
            where = f"round C{cur} is {rec.get('status')} on {rec.get('branch')}: earlier-round work is never rebuilt"
        return f"{sid} passed in round C{mine} and {where}. Make a new change request (Amend plan) that describes what to change."
    return ""
