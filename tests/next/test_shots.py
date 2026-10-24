"""hands.shots (slug, hash, finish refusals) and the runner's shots phase around the evaluator."""
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hands import prompts, shots  # noqa: E402
import test_runner as tr  # noqa: E402


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(w=1, h=1, extra=b""):
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def raw_dir(files, listed):
    d = Path(tempfile.mkdtemp(prefix="shots-raw-"))
    for name, data in files.items():
        (d / name).write_bytes(data)
    (d / "shots.json").write_text(json.dumps(listed))
    return d


ENTRY = {"route": "/", "viewport": "desktop", "file": "home-desktop.png"}


class TestShots(unittest.TestCase):
    def test_slug(self):
        self.assertEqual([shots.slug(r) for r in ("/", "/directions/1", "/a_b/c.html")], ["home", "directions-1", "a-b-c-html"])

    def test_finish_rewrites_and_hash_is_stable(self):
        d = raw_dir({"home-desktop.png": png(extra=chunk(b"tEXt", b"k\0secret"))}, [ENTRY])
        dest = Path(tempfile.mkdtemp(prefix="shots-dest-")) / "out"
        res = shots.finish(d, dest, "S03", "c" * 40)
        self.assertNotIn(b"tEXt", (dest / "home-desktop.png").read_bytes())
        self.assertEqual(res["shots_sha"], shots.manifest_sha(json.loads((dest / "manifest.json").read_text())))
        self.assertEqual(res["shots_sha"], shots.finish(d, dest, "S03", "c" * 40)["shots_sha"])

    def test_finish_refusals(self):
        dest = Path(tempfile.mkdtemp(prefix="shots-dest-")) / "out"
        for files, listed in (
            ({"home-desktop.png": b"<html>"}, [ENTRY]),
            ({"home-desktop.png": png(3000, 1)}, [ENTRY]),
            ({"home-desktop.png": png(1, 12001)}, [ENTRY]),
            ({"home-desktop.png": png(), "x.html": b"x"}, [ENTRY]),
            ({"Home.png": png()}, [{**ENTRY, "file": "Home.png"}]),
            ({}, [ENTRY]),
        ):
            with self.assertRaises(shots.Refused, msg=str(listed)):
                shots.finish(raw_dir(files, listed), dest, "S03", "c" * 40)


class TestRunnerShots(unittest.TestCase):
    STEP = {**tr.STEP1, "screenshots": ["/"]}

    def run_step(self):
        e = tr.Env([self.STEP])
        cap = e.cfg.runner_home / "capture"
        if not (cap / "shots.mjs").is_file():
            self.skipTest("runner_home has no capture package")
        e.queue(tr.GOOD1, tr.EVAL_PASS)
        r = e.runner()
        self.assertEqual(r.run(), 0)
        return e, r

    def test_evaluator_gets_shots_mount_and_listing(self):
        e, r = self.run_step()
        ev = [x for x in e.prompt_records() if x["role"] == "eval"][0]
        self.assertIn("## Screenshots", ev["prompt"])
        self.assertIn("/shots/home-desktop.png", ev["prompt"])
        runs = [json.loads(x) for x in (e.tmp / "docker.jsonl").read_text().splitlines()]
        evals = [a for a in runs if "hands.role=eval" in a]
        self.assertTrue(any(f"dst=/shots,readonly" in " ".join(a) for a in evals))
        self.assertTrue((e.cfg.state_dir / "design" / "shots" / "S01" / "manifest.json").is_file())

    def test_failed_shots_do_not_fail_the_step(self):
        os.environ["FAKE_SHOTS_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_SHOTS_FAIL", None)
        e, r = self.run_step()
        ev = [x for x in e.prompt_records() if x["role"] == "eval"][0]
        self.assertIn("Screenshots failed: exit 3", ev["prompt"])
        self.assertFalse((e.cfg.state_dir / "design" / "shots" / "S01").exists())


class TestPrompt(unittest.TestCase):
    def test_section(self):
        self.assertEqual(prompts.screenshots_section("boom"), "## Screenshots\nScreenshots failed: boom")
        m = {"shots": [{"file": "a-mobile.png", "route": "/a", "viewport": "mobile", "width": 780, "height": 900}]}
        self.assertIn("`/shots/a-mobile.png`", prompts.screenshots_section(m))


if __name__ == "__main__":
    unittest.main()
