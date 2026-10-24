#!/usr/bin/env python3
"""Stand-in for the docker CLI in tests. Container paths (/workspace, /out) are mapped to the
bind-mount sources on the host. Agent behaviour comes from a scenario queue (FAKE_SCENARIO json):
  [{"role": "coder", "files": {...}, "delete": [...], "symlinks": {...}, "git_config": "...",
    "result": {...}, "rc": 0}, {"role": "eval", "text": "VERDICT: {...}"}, ...]
Each call records its argv to FAKE_LOG (jsonl)."""
import json
import os
import subprocess
import sys
from pathlib import Path

argv = sys.argv[1:]
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\n")

if not argv:
    sys.exit(0)
if argv[0] in ("rm", "ps", "volume"):
    sys.exit(0)
if argv[:2] == ["image", "inspect"]:
    sys.exit(0)
if argv[0] != "run":
    sys.exit(0)

mounts, env, i = {}, {}, 1
readonly = set()                      # container dsts mounted readonly: writes under them fail like EROFS
flags_with_value = {"--name", "--label", "--user", "--memory", "--memory-swap", "--cpus", "--pids-limit",
                    "--security-opt", "-w", "--cap-add", "--network", "--env-file", "--entrypoint", "--shm-size"}
while i < len(argv):
    a = argv[i]
    if a == "--mount":
        kv = dict(x.split("=", 1) for x in argv[i + 1].split(",") if "=" in x)
        if kv["type"] == "bind":
            mounts[kv["dst"]] = kv["src"]
            if "readonly" in argv[i + 1].split(","):
                readonly.add(kv["dst"])
        i += 2
    elif a == "-v":
        src, dst, *opts = argv[i + 1].split(":")
        mounts[dst] = src
        if "ro" in opts:
            readonly.add(dst)
        i += 2
    elif a == "-e":
        k, _, v = argv[i + 1].partition("=")
        env[k] = v
        i += 2
    elif a in flags_with_value:
        i += 2
    elif a.startswith("-"):
        i += 1
    else:
        break
image, *cmd = argv[i:]
role = env.get("HANDS_ROLE")


def host(p: str) -> str:
    for dst in sorted(mounts, key=len, reverse=True):
        if p == dst or p.startswith(dst + "/"):
            return mounts[dst] + p[len(dst):]
    return p


def is_readonly(p: str) -> bool:
    for dst in sorted(mounts, key=len, reverse=True):
        if p == dst or p.startswith(dst + "/"):
            return dst in readonly
    return False


def mapped(script: str) -> str:
    return script.replace("/workspace", mounts["/workspace"]).replace("/out", mounts["/out"])


def pop(role: str) -> dict:
    path = os.environ["FAKE_SCENARIO"]
    q = json.loads(Path(path).read_text())
    for n, item in enumerate(q):
        if item["role"] == role:
            q.pop(n)
            Path(path).write_text(json.dumps(q))
            return item
    return {}


def result(text: str, *, is_error=False, subtype="success", cost=0.1, model="claude-fable-5-1", extra=None):
    r = {"type": "result", "subtype": subtype, "is_error": is_error, "result": text, "num_turns": 3,
         "total_cost_usd": cost, "session_id": "s", "modelUsage": {model: {}}}
    r.update(extra or {})
    return r


if role == "export":
    script = cmd[-1]
    p = subprocess.run(["bash", "-c", mapped(script)])
    sys.exit(p.returncode)

if role == "accept":
    ws = mounts["/workspace"]
    acc = mounts.get("/workspace/acceptance")
    if acc:
        os.makedirs(ws + "/acceptance", exist_ok=True)
        subprocess.run(["cp", "-r", acc + "/.", ws + "/acceptance/"])
    assert cmd[:2] == ["bash", "-c"], cmd
    p = subprocess.run(["bash", "-c", cmd[2]], cwd=ws)
    sys.exit(p.returncode)

if role == "shots":
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))
    out = Path(mounts["/shots"])
    assert "/opt/hands/capture" in readonly, "capture package must be read-only"
    if os.environ.get("FAKE_SHOTS_FAIL"):
        (out / "errors.json").write_text('[{"route": "*", "error": "server did not start"}]')
        sys.exit(3)
    (out / "home-desktop.png").write_bytes(png)
    (out / "shots.json").write_text('[{"route": "/", "viewport": "desktop", "file": "home-desktop.png"}]')
    sys.exit(0)

if role == "probe" and cmd and cmd[0] == "bash":
    # a locked-down container's answer to FIREWALL_PROBE (hands/__main__.py)
    print("blocked\nip-blocked\nanthropic=404\ncannot-flush\nno-sudo\nv6-blocked\ndns-allowed-ok\ndns-random-blocked\n"
          "dns-direct-blocked-1.1.1.1+udp\ndns-direct-blocked-1.1.1.1+tcp\ndns-direct-blocked-8.8.8.8+udp\n"
          "dns-direct-blocked-8.8.8.8+tcp\ndnsmasq-running\ndnsmasq-survived\ndnsconf-readonly\nssh-blocked\n"
          "ghip-blocked\ngh-dns-blocked\ngithub=000\nhost-blocked\n1000")
    sys.exit(0)

if role in ("coder", "eval", "probe", "planner"):
    prompt = sys.stdin.read()
    model = cmd[cmd.index("--model") + 1] if "--model" in cmd else "?"
    if role == "probe":
        item = pop("probe")
        if item.get("text") is not None:
            print(json.dumps(result(item["text"], is_error=item.get("is_error", False), model=model)))
            sys.exit(item.get("rc", 0))
        if "SKILLS=" in prompt:
            print(json.dumps(result("SKILLS=test-driven-development,systematic-debugging,verification-before-completion AGENTS=explorer,test-runner", model=model)))
        else:
            print(json.dumps(result("OK", model=model)))
        sys.exit(0)
    if role == "planner" and "SKILLS=" in prompt:
        skills = "brainstorming,writing-plans" if any(m.endswith("/.claude/skills") for m in mounts) else ""
        print(json.dumps(result(f"SKILLS={skills} TOOLS=Bash,Read,Write,WebSearch,Agent", model=model)))
        sys.exit(0)
    item = pop(role)
    rec = os.environ.get("FAKE_PROMPTS")
    if rec:
        with open(rec, "a") as f:
            f.write(json.dumps({"role": role, "model": model, "prompt": prompt, "cmd": cmd}) + "\n")
    ws = Path(mounts["/workspace"])
    refused = []
    for cpath, content in (item.get("write") or {}).items():      # absolute container paths, ro mounts enforced
        if is_readonly(cpath):
            refused.append(cpath)
            print(f"EROFS: Read-only file system: {cpath}", file=sys.stderr)
            continue
        hp = Path(host(cpath))
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(content)
    for rel, content in (item.get("files") or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for rel in item.get("delete") or []:
        (ws / rel).unlink()
    for rel, target in (item.get("symlinks") or {}).items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
    if item.get("git_config"):
        with open(ws / ".git" / "config", "a") as f:
            f.write(item["git_config"])
    if item.get("hook"):
        h = ws / ".git" / "hooks" / item["hook"]["name"]
        h.write_text(item["hook"]["body"])
        h.chmod(0o755)
    if "raw" in item:
        sys.stdout.write(item["raw"])
        sys.exit(item.get("rc", 0))
    for ev in item.get("stream") or []:
        print(json.dumps(ev), flush=True)
    text = item.get("text", "done\nSUMMARY: did the thing")
    if refused:
        text = "".join(f"write failed: EROFS: Read-only file system: {c}\n" for c in refused) + text
    if role == "eval" and "VERDICT:" in text and '"nonce"' not in text and not item.get("no_nonce"):
        import re
        m = re.search(r'"nonce": "([0-9a-f]+)"', prompt)
        if m:
            text = text.replace('VERDICT: {', 'VERDICT: {"nonce": "%s", ' % m.group(1))
    print(json.dumps(result(text, is_error=item.get("is_error", False), subtype=item.get("subtype", "success"),
                            model=model)))
    sys.exit(item.get("rc", 0))

sys.exit(0)
