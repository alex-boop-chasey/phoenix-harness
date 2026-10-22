"""hands.nextstate: next.json (style refs, setup choices, planning chat) and the planning gate."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hands import nextstate
from hands.nextstate import InvalidInput, TooManyRefs, UnknownRef

REPO = Path(__file__).resolve().parents[2]
UUID = "7d7e5f1a-1234-4abc-9def-0123456789ab"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-nextstate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.f = self.tmp / "next.json"


class State(Base):
    def test_init_sets_setup_none_for_framework_1(self):
        s = nextstate.init(self.f, ["https://a.example", "https://b.example/x#frag"], 1, [])
        self.assertEqual([r["index"] for r in s["style_refs"]], [1, 2])
        self.assertEqual([r["url"] for r in s["style_refs"]], ["https://a.example/", "https://b.example/x"])
        self.assertTrue(all(r["status"] == "queued" and r["reason"] is None and r["added"] for r in s["style_refs"]))
        self.assertEqual(s["setup"], {"framework": 1, "libs": [], "status": "none", "record": None})
        self.assertEqual(s["plan_session"]["status"], "none")
        self.assertIsNone(s["plan_session"]["session_id"])
        self.assertEqual(nextstate.load(self.f), s)
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(InvalidInput):              # never overwritten: the index counter would reset
            nextstate.init(self.f, [], 1, [])

    def test_init_sets_pending_for_framework_2(self):
        s = nextstate.init(self.f, [], 2, ["gsap", "shadcn"])
        self.assertEqual(s["setup"]["status"], "pending")
        self.assertEqual(s["setup"]["libs"], ["shadcn", "gsap"])
        g = self.tmp / "g.json"
        self.assertEqual(nextstate.init(g, [], 1, ["gsap"])["setup"]["status"], "pending")

    def test_init_rejects_bad_input_and_writes_nothing(self):
        for urls, fw, libs in ((["https://u:p@a.example/"], 1, []), (["ftp://a.example/"], 1, []), ([], 2, ["jquery"]),
                               ([], 9, []), ([], True, []), (["https://a.example/", "https://a.example"], 1, []),
                               ([f"https://n{i}.example/" for i in range(11)], 1, [])):
            with self.assertRaises(InvalidInput, msg=(urls, fw, libs)):
                nextstate.init(self.f, urls, fw, libs)
        self.assertFalse(self.f.exists())

    def test_add_ref_never_reuses_index(self):
        nextstate.init(self.f, ["https://a.example/"], 1, [])
        nextstate.remove_ref(self.f, 1)
        self.assertEqual(nextstate.add_ref(self.f, "https://c.example/"), 2)
        self.assertEqual([r["index"] for r in nextstate.load(self.f)["style_refs"]], [2])
        nextstate.remove_ref(self.f, 2)
        self.assertEqual(nextstate.add_ref(self.f, "https://c.example/"), 3)

    def test_add_ref_rejects_bad_url_and_limit(self):
        nextstate.init(self.f, [], 1, [])
        for bad in ("javascript:1", "https://u:p@a.example/", "", "http://"):
            with self.assertRaises(InvalidInput):
                nextstate.add_ref(self.f, bad)
        for i in range(10):
            self.assertEqual(nextstate.add_ref(self.f, f"https://n{i}.example/"), i + 1)
        with self.assertRaises(TooManyRefs) as cm:
            nextstate.add_ref(self.f, "https://over.example/")
        self.assertIn("10", str(cm.exception))
        nextstate.remove_ref(self.f, 4)
        with self.assertRaises(InvalidInput):
            nextstate.add_ref(self.f, "https://n0.example")       # duplicate of ref 01
        self.assertEqual(nextstate.add_ref(self.f, "https://new.example/"), 11)

    def test_set_ref_setup_and_plan_session(self):
        nextstate.init(self.f, ["https://a.example/"], 2, [])
        nextstate.set_ref(self.f, 1, "failed", "navigation\nfailed")
        self.assertEqual(nextstate.load(self.f)["style_refs"][0]["reason"], "navigation failed")
        nextstate.set_ref(self.f, 1, "capturing")
        self.assertIsNone(nextstate.load(self.f)["style_refs"][0]["reason"])
        with self.assertRaises(InvalidInput):
            nextstate.set_ref(self.f, 1, "bogus")
        with self.assertRaises(UnknownRef):
            nextstate.set_ref(self.f, 7, "frozen")
        with self.assertRaises(UnknownRef):
            nextstate.remove_ref(self.f, 7)
        nextstate.set_setup(self.f, "done", {"schema": 1, "ok": True})
        self.assertEqual(nextstate.load(self.f)["setup"]["record"], {"schema": 1, "ok": True})
        with self.assertRaises(InvalidInput):
            nextstate.set_setup(self.f, "finished")
        nextstate.set_plan_session(self.f, "open", session_id=UUID)
        nextstate.set_plan_session(self.f, "brief_ready", sha="a" * 64)
        p = nextstate.load(self.f)["plan_session"]
        self.assertEqual((p["status"], p["session_id"], p["brief_sha256"]), ("brief_ready", UUID, "a" * 64))
        for kw in ({"status": "done"}, {"status": "open", "session_id": "not-a-uuid"}, {"status": "open", "sha": "abc"}):
            with self.assertRaises(InvalidInput):
                nextstate.set_plan_session(self.f, **kw)

    def test_gate_reasons(self):
        nextstate.init(self.f, ["https://a.example/", "https://b.example/", "https://c.example/"], 2, [])
        nextstate.set_ref(self.f, 1, "failed", "analyst token missing")
        nextstate.set_ref(self.f, 2, "capturing")
        nextstate.set_ref(self.f, 3, "frozen")
        self.assertEqual(nextstate.gate(nextstate.load(self.f)),
                         ["style ref 01 failed: analyst token missing", "style ref 02 capturing", "setup pending"])
        for status in ("running", "failed"):
            nextstate.set_setup(self.f, status)
            self.assertIn(f"setup {status}", nextstate.gate(nextstate.load(self.f)))
        nextstate.set_ref(self.f, 1, "frozen")
        nextstate.remove_ref(self.f, 2)
        nextstate.set_setup(self.f, "done")
        self.assertEqual(nextstate.gate(nextstate.load(self.f)), [])
        self.assertEqual(nextstate.gate(nextstate.empty()), [])
        self.assertEqual(nextstate.gate(nextstate.load(self.tmp / "missing.json")), [])

    def test_load_rejects_unreadable_state(self):
        self.f.write_text("{not json")
        with self.assertRaises(InvalidInput):
            nextstate.load(self.f)
        self.f.write_text("[]")
        with self.assertRaises(InvalidInput):
            nextstate.load(self.f)


class Cli(Base):
    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "hands.nextstate", *args, "--file", str(self.f)], cwd=REPO,
                              capture_output=True, text=True, timeout=60)

    def test_cli_round_trip(self):
        self.assertEqual(self.cli("init", "--style-urls", "https://a.example/", "--framework", "4", "--libs", "gsap").returncode, 0)
        self.assertEqual(self.cli("add-ref", "--url", "https://b.example/").stdout.strip(), "2")
        self.assertEqual(self.cli("add-ref", "--url", "javascript:1").returncode, 1)
        self.assertEqual(self.cli("set-ref", "--index", "1", "--status", "failed", "--reason", "capture failed (exit 1)").returncode, 0)
        gate = self.cli("gate")
        self.assertEqual(gate.returncode, 1)
        self.assertEqual(gate.stdout.splitlines(), ["style ref 01 failed: capture failed (exit 1)", "style ref 02 queued",
                                                    "setup pending"])
        self.assertEqual(self.cli("remove-ref", "--index", "9").returncode, 1)
        rec = self.tmp / "rec.json"
        rec.write_text(json.dumps({"schema": 1, "ok": True}))
        self.assertEqual(self.cli("set-setup", "--status", "done", "--record", str(rec)).returncode, 0)
        self.assertEqual(self.cli("set-plan-session", "--status", "open", "--session-id", UUID).returncode, 0)
        s = json.loads(self.cli("show", "--json").stdout)
        self.assertEqual((s["setup"]["status"], s["setup"]["record"]["ok"], s["plan_session"]["session_id"]), ("done", True, UUID))
        self.assertIn("planning blocked: style ref 01 failed", self.cli("show").stdout)
        for i in ("1", "2"):
            self.cli("set-ref", "--index", i, "--status", "frozen")
        self.assertEqual(self.cli("gate").returncode, 0)

    def test_imports_alone(self):
        p = subprocess.run([sys.executable, "-c", "import hands.nextstate, sys; "
                            "sys.exit(any(m in sys.modules for m in ('hands.dashboard', 'hands.loop', 'hands.planner')))"],
                           cwd=REPO, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)})
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
