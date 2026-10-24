import json
import tempfile
import unittest
from pathlib import Path

from hands import vendored

PIN = "source=https://example.com/x.git\nref=v1\nsha256=" + "a" * 64 + "\nreviewed_by=ops\ndate=2026-10-20\n"


def put(root: Path, rel: str, data: str = "x") -> Path:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(data)
    return f


def kit(root: Path, name="react-bits", version="1.0.0"):
    d = f"kits/{name}-{version}"
    put(root, f"{d}/inventory.json", json.dumps({"schema": 1, "name": name, "version": version, "components": []}))
    put(root, f"{d}/LICENSE")
    put(root, f"{d}/PIN", PIN)
    put(root, f"{d}/src/a.tsx")
    return root / d


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_absent_dirs_are_empty(self):
        self.assertEqual(vendored.scan(self.root), {"kits": [], "skills": []})

    def test_valid_kit(self):
        kit(self.root)
        k = vendored.scan(self.root)["kits"][0]
        self.assertEqual((k["name"], k["version"], k["ok"]), ("react-bits", "1.0.0", True))

    def test_kit_problems(self):
        d = kit(self.root)
        put(d, ".mcp.json")
        (d / "link").symlink_to(d / "src")
        (d / "PIN").write_text("source=x\n")
        (d / "LICENSE").unlink()
        text = " ".join(vendored.scan(self.root)["kits"][0]["problems"])
        for want in (".mcp.json", "symlink", "PIN incomplete", "LICENSE missing"):
            self.assertIn(want, text)

    def test_hooks_dir_forbidden(self):
        d = kit(self.root)
        put(d, "hooks/x.sh")
        self.assertIn("forbidden directory hooks", " ".join(vendored.scan(self.root)["kits"][0]["problems"]))

    def test_skill_pin_missing(self):
        put(self.root, "skills/s/SKILL.md")
        put(self.root, "skills/s/LICENSE")
        s = vendored.scan(self.root)["skills"][0]
        self.assertFalse(s["ok"])
        self.assertIn("PIN missing", s["problems"])


class MountsTest(unittest.TestCase):
    def setUp(self):
        self.root, self.state = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        kit(self.root)
        put(self.root, "skills/h/SKILL.md")
        put(self.root, "skills/h/LICENSE")
        put(self.root, "skills/h/PIN", PIN)
        self.base = Path(tempfile.mkdtemp())
        put(self.base, "tdd/SKILL.md")
        self.map = {"skills": {"h": ["coder"]}, "kits": ["react-bits"]}

    def dsts(self, role):
        return {m["dst"]: m for m in vendored.mounts(self.root, role, self.state, self.base, self.map)}

    def test_coder(self):
        m = self.dsts("coder")
        self.assertTrue(all(x["ro"] for x in m.values()))
        self.assertIn("/opt/kits/react-bits", m)
        s = Path(m["/home/node/.claude/skills"]["src"])
        self.assertTrue((s / "h" / "SKILL.md").is_file() and (s / "tdd" / "SKILL.md").is_file())

    def test_planner_gets_inventory_only(self):
        m = self.dsts("planner")
        self.assertIn("/opt/kits/react-bits/inventory.json", m)
        self.assertNotIn("/opt/kits/react-bits", m)
        self.assertFalse((Path(m["/home/node/.claude/skills"]["src"]) / "h").exists())

    def test_no_base_no_vendored(self):
        self.assertEqual(vendored.mounts(self.root / "none", "coder", self.state, None, {}), [])


class StepKeysTest(unittest.TestCase):
    def spec(self, **extra):
        from hands import spec as specmod
        root = Path(tempfile.mkdtemp())
        put(root, "acceptance/a.mjs")
        steps = [{"id": "S1", "title": "t", "goal": "g", "acceptance": ["x"], **extra.get("s1", {})},
                 {"id": "G1", "title": "t", "goal": "g", "type": "human", **extra.get("g1", {})}]
        put(root, "steps.json", json.dumps({"version": 1, "allowed_paths": ["src/**"], "steps": steps}))
        return specmod.load(root)

    def test_old_steps_still_load(self):
        s = self.spec()
        self.assertEqual((s.steps[0].skills, s.steps[0].screenshots, s.steps[0].gate), ([], [], ""))

    def test_gate_needs_earlier_screenshots(self):
        from hands import spec as specmod
        with self.assertRaises(specmod.SpecError):
            self.spec(g1={"gate": "tweaks"})
        self.assertEqual(self.spec(s1={"screenshots": ["/a"]}, g1={"gate": "tweaks"}).steps[1].gate, "tweaks")

    def test_toolusage_step_skills_are_required(self):
        from hands import toolusage
        self.assertEqual(toolusage.check({"skills": ["a"]}, "coder", toolusage.with_step_skills({"coder": ["a"]}, "coder", ["b"])), ["b"])
