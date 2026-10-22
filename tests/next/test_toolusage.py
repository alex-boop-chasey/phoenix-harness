"""hands.toolusage: skill, plugin and tool usage summary of a stream-json session transcript."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hands import dashboard, projectenv, toolusage

REPO = Path(__file__).resolve().parents[2]
REQUIRED = {"coder": ["test-driven-development", "verification-before-completion"],
            "evaluator": ["verification-before-completion"],
            "planner": ["brainstorming", "writing-plans"], "analyst": []}


def assistant(*blocks):
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}})


def use(name, **inp):
    return {"type": "tool_use", "id": "toolu_x", "name": name, "input": inp}


INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                   "tools": ["Read", "Edit", "Write", "Bash", "Skill", "Task", "mcp__github__get_issue"],
                   "mcp_servers": [{"name": "github", "status": "connected"}],
                   "plugins": [{"name": "superpowers", "version": "1.0.0"}], "model": "claude-opus-5-5"})
TRANSCRIPT = [
    INIT,
    assistant({"type": "text", "text": "Let me look."}, use("Read", file_path="/w/a.py")),
    json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"}]}}),
    assistant(use("Read", file_path="/w/b.py"), use("Edit", file_path="/w/b.py", old_string="a", new_string="b")),
    "this line is not json",
    assistant(use("Skill", skill="brainstorming")),
    assistant(use("Skill", skill="superpowers:writing-plans")),
    assistant(use("mcp__github__get_issue", number=1)),
    assistant(use("Task", subagent_type="Explore", prompt="find x", description="search")),
    assistant(use("Bash", command="ls"))[:60],          # truncated: the container was killed mid-write
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 9, "result": "done"}),
]
EXPECTED_TOOLS = {"Read": 2, "Edit": 1, "Skill": 2, "mcp__github__get_issue": 1, "Task": 1}


class Summarize(unittest.TestCase):
    def test_counts_everything(self):
        s = toolusage.summarize(TRANSCRIPT)
        self.assertEqual(s["tools"], EXPECTED_TOOLS)
        self.assertEqual(s["skills"], ["brainstorming", "superpowers:writing-plans"])
        self.assertEqual(s["plugins"], ["superpowers"])
        self.assertEqual(s["mcp"], {"github": 1})
        self.assertEqual(s["subagents"], ["Explore"])
        self.assertEqual(s["available"]["tools"], ["Read", "Edit", "Write", "Bash", "Skill", "Task", "mcp__github__get_issue"])
        self.assertEqual(s["available"]["mcp_servers"], ["github"])
        self.assertEqual(s["skipped_lines"], 2)

    def test_accepts_bytes_with_newlines_and_blank_lines(self):
        raw = ("\n".join(TRANSCRIPT) + "\n\n").encode()
        s = toolusage.summarize(io.BytesIO(raw))
        self.assertEqual(s["tools"], EXPECTED_TOOLS)
        self.assertEqual(s["skipped_lines"], 2)

    def test_empty_and_garbage(self):
        empty = toolusage.summarize([])
        self.assertEqual(empty, {"tools": {}, "skills": [], "plugins": [], "mcp": {}, "subagents": [],
                                 "available": {"tools": [], "mcp_servers": []}, "skipped_lines": 0})
        weird = toolusage.summarize(["[1,2]", "42", '"str"', '{"type":"assistant","message":"nope"}',
                                     '{"type":"assistant","message":{"content":[{"type":"tool_use","name":7}, 3]}}',
                                     '{"type":"system","subtype":"init","tools":"Read","mcp_servers":[5],"plugins":[{"x":1}]}'])
        self.assertEqual(weird["tools"], {})
        self.assertEqual(weird["available"], {"tools": [], "mcp_servers": []})
        self.assertEqual(weird["skipped_lines"], 0)        # valid JSON of the wrong shape is ignored, not skipped

    def test_skill_order_is_first_use_and_agent_tool_counts_as_subagent(self):
        s = toolusage.summarize([assistant(use("Skill", skill="b")), assistant(use("Skill", skill="a")),
                                 assistant(use("Skill", skill="b")), assistant(use("Agent", subagent_type="test-runner")),
                                 assistant(use("Agent", subagent_type="test-runner")), assistant(use("Agent"))])
        self.assertEqual(s["skills"], ["b", "a"])
        self.assertEqual(s["tools"], {"Skill": 3, "Agent": 3})
        self.assertEqual(s["subagents"], ["test-runner"])

    def test_plugin_names_from_init_and_skill_prefix_deduplicated(self):
        s = toolusage.summarize([INIT, assistant(use("Skill", skill="superpowers:brainstorming")),
                                 assistant(use("Skill", skill="other:thing")), assistant(use("Skill", skill=":bad")),
                                 assistant(use("Skill", skill="bad:"))])
        self.assertEqual(s["plugins"], ["superpowers", "other"])
        self.assertEqual(s["skills"], ["superpowers:brainstorming", "other:thing", ":bad", "bad:"])

    def test_mcp_server_requires_tool_part(self):
        s = toolusage.summarize([assistant(use("mcp__a__b"), use("mcp__a__c"), use("mcp__x"), use("mcp____t"))])
        self.assertEqual(s["mcp"], {"a": 2})

    def test_names_are_bounded_plain_strings(self):
        s = toolusage.summarize([assistant(use("x" * 1000), use("Skill", skill="a\nb\t c"))])
        self.assertEqual(set(s["tools"]), {"x" * toolusage.MAX_NAME, "Skill"})
        self.assertEqual(s["skills"], ["a b c"])


class Check(unittest.TestCase):
    def test_missing_required_skills(self):
        s = toolusage.summarize(TRANSCRIPT)
        self.assertEqual(toolusage.check(s, "coder", REQUIRED), ["test-driven-development", "verification-before-completion"])
        self.assertEqual(toolusage.check(s, "planner", REQUIRED), [])     # writing-plans used via superpowers:
        self.assertEqual(toolusage.check(s, "analyst", REQUIRED), [])
        self.assertEqual(toolusage.check(s, "unknown-role", REQUIRED), [])
        self.assertEqual(toolusage.check({}, "coder", REQUIRED), REQUIRED["coder"])
        self.assertEqual(toolusage.check(s, "coder", {"coder": "nope"}), [])

    def test_load_required(self):
        req = toolusage.load_required(REPO / "prompts" / "required-skills.json")
        self.assertEqual(req, REQUIRED)
        self.assertEqual(toolusage.load_required(), REQUIRED)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(toolusage.load_required(Path(tmp) / "missing.json"), {})
            p = Path(tmp) / "bad.json"
            p.write_text("[1]")
            self.assertEqual(toolusage.load_required(p), {})
            p.write_text(json.dumps({"coder": ["a", 1], "x": "y"}))
            self.assertEqual(toolusage.load_required(p), {"coder": ["a"]})


class WriteToolsJson(unittest.TestCase):
    def test_writes_summary_plus_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "coder.json"
            t.write_text("\n".join(TRANSCRIPT) + "\n")
            dest = Path(tmp) / "tools.json"
            out = toolusage.write_tools_json(t, "coder", dest, REQUIRED)
            self.assertEqual(json.loads(dest.read_text()), out)
            self.assertEqual(out["tools"], EXPECTED_TOOLS)
            self.assertEqual(out["role"], "coder")
            self.assertEqual(out["missing"], ["test-driven-development", "verification-before-completion"])
            self.assertEqual(out["skipped_lines"], 2)
            self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o644")
            self.assertEqual(sorted(os.listdir(tmp)), ["coder.json", "tools.json"])

    def test_missing_transcript_is_an_empty_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "tools.json"
            out = toolusage.write_tools_json(Path(tmp) / "absent.json", "evaluator", dest, REQUIRED)
            self.assertEqual(out["tools"], {})
            self.assertEqual(out["missing"], ["verification-before-completion"])
            self.assertTrue(dest.is_file())

    def test_default_required_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "planner.json"
            t.write_text("\n".join(TRANSCRIPT) + "\n")
            out = toolusage.write_tools_json(t, "planner", Path(tmp) / "planner-tools.json")
            self.assertEqual(out["missing"], [])


class DashboardAttemptJson(unittest.TestCase):
    """The attempt list serves tools.json as tool_usage (redacted again at read time, like every attempt file)."""

    def test_tool_usage_reads_tools_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "tools.json"
            self.assertIsNone(dashboard.tool_usage(dest, projectenv.Redactor({})))
            t = Path(tmp) / "coder.json"
            t.write_text("\n".join(TRANSCRIPT) + "\n")
            toolusage.write_tools_json(t, "coder", dest, REQUIRED)
            got = dashboard.tool_usage(dest, projectenv.Redactor({}))
            self.assertEqual(got["tools"], EXPECTED_TOOLS)
            self.assertEqual(got["missing"], ["test-driven-development", "verification-before-completion"])
            dest.write_text("[1, 2]")
            self.assertIsNone(dashboard.tool_usage(dest, projectenv.Redactor({})))
            dest.write_text("{not json")
            self.assertIsNone(dashboard.tool_usage(dest, projectenv.Redactor({})))
            dest.unlink()
            dest.symlink_to(t)
            self.assertIsNone(dashboard.tool_usage(dest, projectenv.Redactor({})))

    def test_served_names_are_allowed_attempt_files(self):
        for name in ("tools.json", "eval-1-tools.json"):
            self.assertTrue(dashboard.ATTEMPT_FILE.match(name), name)
        for name in ("coder.json", "eval-1.json", "planner-tools.json", ".tools.json.1.tmp"):
            self.assertFalse(dashboard.ATTEMPT_FILE.match(name), name)


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "hands.toolusage", *args], cwd=REPO, capture_output=True, text=True)

    def test_summarize_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "t.jsonl"
            t.write_text("\n".join(TRANSCRIPT) + "\n")
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps(REQUIRED))
            r = self.run_cli("summarize", str(t), "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            s = json.loads(r.stdout)
            self.assertEqual(s["tools"], EXPECTED_TOOLS)
            self.assertEqual(s["skipped_lines"], 2)
            r = self.run_cli("summarize", str(t))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("skills: brainstorming, superpowers:writing-plans", r.stdout)
            self.assertIn("Read×2", r.stdout)
            r = self.run_cli("check", str(t), "--role", "coder", "--required", str(req), "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout), {"missing": ["test-driven-development", "verification-before-completion"]})
            r = self.run_cli("check", str(t), "--role", "planner", "--required", str(req), "--json")
            self.assertEqual(json.loads(r.stdout), {"missing": []})
            r = self.run_cli("check", str(t), "--role", "coder", "--required", str(req))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("test-driven-development", r.stdout)
            r = self.run_cli("check", str(t), "--role", "planner", "--json")      # default prompts/required-skills.json
            self.assertEqual(json.loads(r.stdout), {"missing": []})

    def test_unreadable_inputs_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_cli("summarize", str(Path(tmp) / "nope.jsonl"), "--json")
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stdout, "")
            t = Path(tmp) / "t.jsonl"
            t.write_text("")
            r = self.run_cli("check", str(t), "--role", "coder", "--required", str(Path(tmp) / "nope.json"), "--json")
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
