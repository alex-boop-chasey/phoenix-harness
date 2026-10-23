"""hands.stylerefs: URL validation, capture/analyst launch requests, sanitiser, validators, freeze/remove."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hands import stylerefs
from hands.stylerefs import AnalystTokenMissing, FreezeError, InvalidUrl

REPO = Path(__file__).resolve().parents[2]

GOOD_STYLE = {
    "schema": 1,
    "sources": ["https://example.com/"],
    "palette": [{"name": "accent", "hex": "#ff5722", "role": "accent"}],
    "fonts": [{"family": "Georgia", "role": "body", "weights": [400]}],
    "typeScale": [{"name": "body", "px": 20, "lineHeight": 1.5}],
    "spacingScale": [8, 16, 24],
    "radii": [12],
    "shadows": ["0 2px 8px rgba(0,0,0,0.12)"],
    "layout": {"maxWidth": 1200, "grid": "3 columns", "sections": ["header", "hero"]},
    "motion": {"smoothScroll": {"library": "lenis", "lerp": 0.08},
               "animations": [{"name": "fade-up", "trigger": "load", "duration": 900}],
               "libraries": ["gsap", "lenis"]},
    "notes": ["hover lifts the CTA by 2px"],
}

GOOD_REPORT = "\n".join([
    "# Style reference 02: example.com", "", "## Sources", "- https://example.com/", "",
    "## Layout and structure", "x", "## Colour", "x", "## Typography", "x", "## Spacing and shape", "x",
    "## Graphics and icons", "x", "## Motion", "x", "## Frames", "x", "## How to apply", "x", ""])


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class TmpDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hands-stylerefs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def mkdir(self, name: str) -> Path:
        d = self.tmp / name
        d.mkdir()
        return d


class ValidateUrl(unittest.TestCase):
    def test_accepts_http_and_https_and_normalises(self):
        self.assertEqual(stylerefs.validate_url("https://example.com/path?q=1"), "https://example.com/path?q=1")
        self.assertEqual(stylerefs.validate_url("HTTP://Example.COM"), "http://example.com/")
        self.assertEqual(stylerefs.validate_url(" https://example.com/a#frag "), "https://example.com/a")

    def test_validate_url_rejects_credentials_and_schemes(self):
        for bad in ["https://u:p@x.io", "https://user@x.io/", "javascript:1", "file:///x", "http://", "ftp://example.com",
                    "example.com", "", "https://example.com/" + "a" * 2050, "https://exa mple.com/", "https://x.io/a\nb"]:
            with self.subTest(url=bad):
                with self.assertRaises(InvalidUrl):
                    stylerefs.validate_url(bad)

    def test_non_string_is_invalid(self):
        with self.assertRaises(InvalidUrl):
            stylerefs.validate_url(None)


class PlanCapture(TmpDirs):
    def test_plan_capture_has_no_token_and_open_network(self):
        out = self.mkdir("out")
        req = stylerefs.plan_capture("https://example.com/", out, REPO / "capture")
        argv = req.argv
        self.assertEqual(argv[:2], ["docker", "run"])
        self.assertEqual(req.timeout_seconds, 600)
        for flag, value in [("--network", "bridge"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                            ("--entrypoint", "node"), ("--tmpfs", "/tmp"), ("--memory", "2g"), ("--pids-limit", "512")]:
            i = argv.index(flag)
            self.assertEqual(argv[i + 1], value, flag)
        self.assertIn("--read-only", argv)
        self.assertIn("--rm", argv)
        self.assertIn(f"{REPO / 'capture'}:/capture:ro", argv)
        self.assertIn(f"{out}:/out:rw", argv)
        env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertEqual(sorted(env), ["HOME=/tmp", "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"])
        self.assertEqual(argv[-5:], ["/capture/capture.mjs", "--url", "https://example.com/", "--out", "/out"])
        self.assertEqual(argv[argv.index("--entrypoint") + 2], "hands")
        joined = " ".join(argv)
        for forbidden in ["ANTHROPIC", "TOKEN", "project.env", "/run/hands", "--env-file", "--cap-add", "--privileged"]:
            self.assertNotIn(forbidden, joined)

    def test_plan_capture_image_and_normalised_url(self):
        req = stylerefs.plan_capture("HTTPS://Example.com", self.tmp, REPO / "capture", image="hands:test")
        self.assertIn("hands:test", req.argv)
        self.assertEqual(req.argv[-3], "https://example.com/")
        self.assertEqual(req.url, "https://example.com/")

    def test_plan_capture_rejects_bad_url(self):
        with self.assertRaises(InvalidUrl):
            stylerefs.plan_capture("file:///etc/passwd", self.tmp, REPO / "capture")


class Sanitize(unittest.TestCase):
    def dirty(self) -> dict:
        return {
            "schema": 1, "url": "https://example.com/", "capturedAt": "2026-10-22T00:00:00Z", "injected": "x",
            "viewports": {
                "desktop": {
                    "size": {"w": 1440, "h": 900, "extra": 1}, "pageHeight": 4000, "html": "<html>",
                    "structure": [
                        {"tag": "header", "role": "banner", "depth": 1, "children": 1, "text": "secret",
                         "box": {"x": 0, "y": 0, "w": 1440, "h": 64}, "path": "header.site-header:nth-child(1)"},
                        {"tag": "div", "path": "div<script>alert(1)</script>", "depth": 1, "children": 0},
                        {"tag": "div", "path": "", "depth": 1, "children": 0},
                        {"tag": "div", "depth": 1, "children": 0},
                    ],
                    "colors": [{"value": f"rgb({i}, 0, 0)", "uses": 50 - i, "properties": ["color"]} for i in range(50)],
                    "fonts": {"families": [{"family": "A" * 300, "stack": "x", "uses": 3,
                                            "weights": [{"weight": "400", "uses": 3}]}],
                              "sizes": [{"px": 64, "uses": 1}], "lineHeights": [{"value": "normal", "ratio": None, "uses": 1}]},
                    "keyframes": [{"name": "fade-up", "frames": [{"offset": "0%", "properties": {
                        "opacity": "0", "Bad Key!": "x", "transform": "t" * 300}}]}],
                    "webAnimations": [{"type": "CSSAnimation", "name": "fade", "target": "h1:nth-child(1)", "timeline": "scroll",
                                       "currentTime": "0%", "playState": "running",
                                       "keyframes": [{"offset": None, "easing": "linear", "opacity": "0", "computedOffset": 0}],
                                       "timing": {"duration": None, "iterations": "infinite", "progress": float("nan"), "bogus": 1}}
                                      for _ in range(250)],
                    "steps": [{"index": i, "scrollY": i * 900, "frame": f"frames/desktop/step-{i:02d}.png", "animations": [],
                               "changed": [{"path": "div.orb:nth-child(4)", "changed": {"transform": {"from": "a", "to": "b"}}}]}
                              for i in range(30)],
                    "interactions": [{"kind": "hover", "tag": "a", "target": "a.nav:nth-child(1)", "diff": {}, "started": [],
                                      "frame": "frames/desktop/hover-00.png"} for _ in range(25)],
                    "gsap": {"present": True, "version": "3.13.0", "scrollTriggers": [{"trigger": "section:nth-child(2)",
                             "start": 1200, "end": 2100, "scrub": 0.5, "pin": True, "toggleActions": "play none",
                             "animationDuration": 1.2, "animationTargets": 1}],
                             "timelines": [{"kind": "tween", "duration": 1.2, "targets": 3, "children": 0, "varsKeys": ["opacity"]}]},
                    "smoothScroll": {"lenis": {"lerp": 0.08}, "locomotive": None, "scrollBehavior": {"html": "smooth", "body": "auto"},
                                     "cssScrollBehavior": "smooth", "transformScroller": False},
                    "graphics": {"svg": 0, "img": 1, "video": 0, "canvas": 0, "backgroundImages": 0,
                                 "sizes": [{"kind": "img", "w": 120, "h": 40, "src": "logo.png"}]},
                },
                "evil": {"structure": []},
            },
        }

    def test_sanitize_drops_unknown_keys_and_long_strings(self):
        s = stylerefs.sanitize(self.dirty())
        self.assertEqual(s["schema"], 1)
        self.assertEqual(s["url"], "https://example.com/")
        self.assertNotIn("injected", s)
        self.assertEqual(list(s["viewports"]), ["desktop"])
        d = s["viewports"]["desktop"]
        self.assertNotIn("html", d)
        self.assertEqual(d["size"], {"w": 1440, "h": 900})
        self.assertEqual(len(d["structure"]), 1)
        self.assertEqual(d["structure"][0]["path"], "header.site-header:nth-child(1)")
        self.assertNotIn("text", d["structure"][0])
        self.assertEqual(len(d["colors"]), 40)
        self.assertEqual(d["colors"][0], {"value": "rgb(0, 0, 0)", "uses": 50, "properties": ["color"]})
        self.assertEqual(len(d["fonts"]["families"][0]["family"]), 120)
        self.assertEqual(d["keyframes"][0]["frames"][0]["properties"], {"opacity": "0", "transform": "t" * 120})
        self.assertEqual(len(d["webAnimations"]), 200)
        self.assertNotIn("progress", d["webAnimations"][0]["timing"])
        self.assertNotIn("bogus", d["webAnimations"][0]["timing"])
        self.assertEqual(d["webAnimations"][0]["timing"]["iterations"], "infinite")
        self.assertEqual(len(d["steps"]), 24)
        self.assertEqual(d["steps"][1]["changed"], [{"path": "div.orb:nth-child(4)", "changed": {"transform": {"from": "a", "to": "b"}}}])
        self.assertEqual(len(d["interactions"]), 20)
        self.assertEqual(d["gsap"]["scrollTriggers"][0]["scrub"], 0.5)
        self.assertEqual(d["smoothScroll"]["lenis"], {"lerp": 0.08})
        self.assertIsNone(d["smoothScroll"]["locomotive"])
        self.assertEqual(d["graphics"]["sizes"], [{"kind": "img", "w": 120, "h": 40}])
        self.assertNotIn("secret", json.dumps(s))
        self.assertNotIn("<", json.dumps(s))

    def test_sanitize_tolerates_garbage(self):
        self.assertEqual(stylerefs.sanitize({"schema": "1", "url": "javascript:1", "viewports": []}), {})
        self.assertEqual(stylerefs.sanitize([]), {})
        s = stylerefs.sanitize({"schema": 1, "error": "x" * 500, "viewports": {"mobile": {"error": "nav failed", "pageHeight": float("inf")}}})
        self.assertEqual(len(s["error"]), 120)
        self.assertEqual(s["viewports"]["mobile"], {"error": "nav failed"})

    def test_sanitize_keeps_the_schema_example(self):
        example = json.loads((REPO / "capture" / "schema.example.json").read_text())
        s = stylerefs.sanitize(example)
        self.assertEqual(s, example, "every field the capture script writes survives the sanitiser")

    def test_sanitize_strips_control_characters(self):
        s = stylerefs.sanitize({"schema": 1, "capturedAt": "a\x00b\x1fc\nd"})
        self.assertEqual(s["capturedAt"], "abcd")


class PlanAnalyst(TmpDirs):
    def write_secrets(self, text: str) -> Path:
        p = self.tmp / "secrets.env"
        p.write_text(text)
        return p

    def test_plan_analyst_env_file_has_only_the_analyst_key(self):
        cap, out, env_dir = self.mkdir("cap"), self.mkdir("out"), self.mkdir("env")
        secrets = self.write_secrets("ANTHROPIC_API_KEY=sk-main-0\n  ANALYST_ANTHROPIC_API_KEY=k1=x\n# c\nGITHUB_TOKEN=ghp_x\n")
        req = stylerefs.plan_analyst(cap, out, secrets, env_dir, REPO / "prompts" / "analyst.md")
        self.assertEqual(req.role, "analyst")
        self.assertEqual(req.network, "firewalled")
        self.assertEqual(req["role"], "analyst")
        env_file = Path(req.env_file)
        self.assertEqual(env_file.parent, env_dir)
        self.assertEqual(env_file.read_text(), "ANTHROPIC_API_KEY=k1=x\n")
        self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)
        d = req.to_dict()
        self.assertEqual(d["env_file"], str(env_file))
        self.assertIn({"host": str(cap), "container": "/capture", "mode": "ro"}, d["mounts"])
        self.assertIn({"host": str(out), "container": "/out", "mode": "rw"}, d["mounts"])
        self.assertTrue(d["prompt_file"].endswith("prompts/analyst.md"))
        self.assertEqual(d["command"][:2], ["claude", "-p"])
        self.assertEqual(d["image"], "hands")
        text = json.dumps(d)
        for forbidden in ["sk-main-0", "ghp_x", "project.env", "/run/hands"]:
            self.assertNotIn(forbidden, text)
        self.assertEqual(json.loads(json.dumps(d)), d)

    def test_plan_analyst_without_key_raises(self):
        cap, out, env_dir = self.mkdir("cap"), self.mkdir("out"), self.mkdir("env")
        for text in ["ANTHROPIC_API_KEY=sk-main-0\n", "ANALYST_ANTHROPIC_API_KEY=\n", ""]:
            with self.subTest(text=text):
                with self.assertRaisesRegex(AnalystTokenMissing, "analyst token missing"):
                    stylerefs.plan_analyst(cap, out, self.write_secrets(text), env_dir, REPO / "prompts" / "analyst.md")
        with self.assertRaises(AnalystTokenMissing):
            stylerefs.plan_analyst(cap, out, self.tmp / "nope.env", env_dir, REPO / "prompts" / "analyst.md")
        self.assertEqual(os.listdir(env_dir), [])

    def test_plan_analyst_names_index_and_host_in_the_task(self):
        cap, out, env_dir = self.mkdir("cap"), self.mkdir("out"), self.mkdir("env")
        secrets = self.write_secrets("ANALYST_ANTHROPIC_API_KEY=k1\n")
        req = stylerefs.plan_analyst(cap, out, secrets, env_dir, REPO / "prompts" / "analyst.md", index=3, host="example.com")
        self.assertIn("Style reference 03: example.com", req.command[-1])


class Validators(unittest.TestCase):
    def test_validate_style_accepts_good(self):
        self.assertEqual(stylerefs.validate_style(GOOD_STYLE), [])

    def test_validate_style_lists_missing_sections(self):
        errors = stylerefs.validate_style({"schema": 1, "palette": "red", "fonts": [], "motion": {"animations": "none"}})
        text = "\n".join(errors)
        self.assertIn("palette", text)
        self.assertIn("motion.animations", text)
        self.assertIn("motion.libraries", text)
        for key in ["typeScale", "spacingScale", "radii", "shadows", "layout", "notes"]:
            self.assertIn(f"{key}: missing", text)
        self.assertNotIn("fonts", text)

    def test_validate_style_checks_entries(self):
        bad = dict(GOOD_STYLE, schema=2, palette=[{"hex": "red"}], fonts=[{"role": "body"}], spacingScale=["8px"],
                   typeScale=[{"name": "x"}], shadows=[1], notes="n")
        text = "\n".join(stylerefs.validate_style(bad))
        for frag in ["schema", "palette[0].hex", "fonts[0].family", "spacingScale[0]", "typeScale[0].px", "shadows[0]", "notes"]:
            self.assertIn(frag, text)
        self.assertEqual(stylerefs.validate_style([]), ["style.json: must be an object"])

    def test_validate_report(self):
        self.assertEqual(stylerefs.validate_report(GOOD_REPORT), [])
        errors = stylerefs.validate_report(GOOD_REPORT.replace("## Motion", "## Movement"))
        self.assertEqual(errors, ["report.md: missing heading '## Motion'"])
        errors = stylerefs.validate_report("hello\n## Motion\n")
        self.assertTrue(any("# Style reference" in e for e in errors))
        self.assertEqual(stylerefs.validate_report(""), ["report.md: empty"])


class FreezeRemove(TmpDirs):
    def make_spec(self) -> Path:
        spec = self.mkdir("spec")
        subprocess.run(["git", "-C", str(spec), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(spec), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(spec), "config", "user.name", "tester"], check=True)
        (spec / "README.md").write_text("spec\n")
        subprocess.run(["git", "-C", str(spec), "add", "."], check=True)
        subprocess.run(["git", "-C", str(spec), "commit", "-q", "-m", "init"], check=True)
        return spec

    def make_from(self, frames: int = 1) -> Path:
        src = self.mkdir("from")
        (src / "report.md").write_text(GOOD_REPORT)
        (src / "style.json").write_text(json.dumps(GOOD_STYLE))
        (src / "extra.txt").write_text("not frozen\n")
        (src / "frames" / "desktop").mkdir(parents=True)
        for i in range(frames):
            (src / "frames" / "desktop" / f"step-{i:02d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (src / "frames" / "notes.txt").write_text("not a frame\n")
        os.symlink("/etc/hostname", src / "frames" / "link.png")
        return src

    def test_freeze_commits_without_a_git_identity_in_the_clone(self):
        """Same as the brief approval: the runner's git ignores the global config, so the freeze commit
        carries gitops.IDENT itself."""
        spec, src = self.make_spec(), self.make_from()
        for key in ("user.email", "user.name"):
            subprocess.run(["git", "-C", str(spec), "config", "--unset", key], check=True)
        stylerefs.freeze(src, spec, 3, "example.com")
        author = subprocess.run(["git", "-C", str(spec), "log", "-1", "--format=%an <%ae>"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(author, "hands-runner <runner@hands.local>")

    def test_freeze_copies_and_commits(self):
        spec, src = self.make_spec(), self.make_from()
        dest = stylerefs.freeze(src, spec, 2, "example.com")
        self.assertEqual(dest, spec / "refs" / "style-refs" / "02")
        self.assertTrue((dest / "report.md").is_file())
        self.assertTrue((dest / "style.json").is_file())
        self.assertTrue((dest / "frames" / "desktop" / "step-00.png").is_file())
        self.assertFalse((dest / "extra.txt").exists())
        self.assertFalse((dest / "frames" / "notes.txt").exists())
        self.assertFalse((dest / "frames" / "link.png").exists())
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "style refs: 02 example.com")
        self.assertEqual(git(spec, "status", "--porcelain"), "")

    def test_freeze_replaces_an_existing_ref(self):
        spec, src = self.make_spec(), self.make_from()
        stylerefs.freeze(src, spec, 2, "example.com")
        (spec / "refs" / "style-refs" / "02" / "frames" / "old.png").write_bytes(b"x")
        subprocess.run(["git", "-C", str(spec), "add", "."], check=True)
        subprocess.run(["git", "-C", str(spec), "commit", "-q", "-m", "stray"], check=True)
        (src / "report.md").write_text(GOOD_REPORT + "\nmore\n")
        stylerefs.freeze(src, spec, 2, "example.com")
        self.assertFalse((spec / "refs" / "style-refs" / "02" / "frames" / "old.png").exists())
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "style refs: 02 example.com")
        self.assertEqual(git(spec, "status", "--porcelain"), "")
        before = git(spec, "rev-parse", "HEAD")
        stylerefs.freeze(src, spec, 2, "example.com")
        self.assertEqual(git(spec, "rev-parse", "HEAD"), before, "identical re-freeze commits nothing")

    def test_freeze_rejects_bad_input(self):
        spec, src = self.make_spec(), self.make_from()
        for index in [0, 100, -1]:
            with self.assertRaisesRegex(FreezeError, "index"):
                stylerefs.freeze(src, spec, index, "example.com")
        with self.assertRaisesRegex(FreezeError, "host"):
            stylerefs.freeze(src, spec, 1, "bad host")
        for i in range(1, 9):
            (src / "frames" / "desktop" / f"step-{i:02d}.png").write_bytes(b"x")
        with self.assertRaisesRegex(FreezeError, "frames"):
            stylerefs.freeze(src, spec, 1, "example.com")
        (src / "style.json").unlink()
        with self.assertRaisesRegex(FreezeError, "style.json"):
            stylerefs.freeze(src, spec, 1, "example.com")
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "init")
        self.assertFalse((spec / "refs").exists())

    def test_remove_deletes_and_commits(self):
        spec, src = self.make_spec(), self.make_from()
        stylerefs.freeze(src, spec, 2, "example.com")
        stylerefs.remove(spec, 2)
        self.assertFalse((spec / "refs" / "style-refs" / "02").exists())
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "style refs: remove 02")
        self.assertEqual(git(spec, "status", "--porcelain"), "")
        stylerefs.remove(spec, 2)
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "style refs: remove 02", "removing a missing ref is a no-op")


class Cli(TmpDirs):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "hands.stylerefs", *args], cwd=REPO, capture_output=True, text=True, timeout=120)

    def test_validate_url_exit_codes(self):
        ok = self.run_cli("validate-url", "https://example.com")
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(ok.stdout.strip(), "https://example.com/")
        bad = self.run_cli("validate-url", "https://u:p@example.com/")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("invalid url", bad.stderr)

    def test_plan_capture_json(self):
        r = self.run_cli("plan-capture", "--url", "https://example.com/", "--out", str(self.tmp), "--capture-dir",
                         str(REPO / "capture"), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        req = json.loads(r.stdout)
        self.assertEqual(req["argv"][:2], ["docker", "run"])
        self.assertEqual(req["timeout_seconds"], 600)
        plain = self.run_cli("plan-capture", "--url", "https://example.com/", "--out", str(self.tmp), "--capture-dir", str(REPO / "capture"))
        self.assertEqual(plain.returncode, 0)
        self.assertTrue(plain.stdout.startswith("docker run "))
        self.assertEqual(self.run_cli("plan-capture", "--url", "ftp://x", "--out", str(self.tmp), "--capture-dir", "capture").returncode, 1)

    def test_sanitize_cli(self):
        src = self.tmp / "capture.json"
        src.write_text(json.dumps({"schema": 1, "url": "https://example.com/", "nope": 1}))
        r = self.run_cli("sanitize", str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"schema": 1, "url": "https://example.com/"})
        out = self.tmp / "clean.json"
        self.assertEqual(self.run_cli("sanitize", str(src), "--out", str(out)).returncode, 0)
        self.assertEqual(json.loads(out.read_text()), {"schema": 1, "url": "https://example.com/"})
        src.write_text("{not json")
        self.assertEqual(self.run_cli("sanitize", str(src)).returncode, 1)
        self.assertEqual(self.run_cli("sanitize", str(self.tmp / "missing.json")).returncode, 1)

    def test_plan_analyst_cli(self):
        secrets = self.tmp / "s.env"
        secrets.write_text("ANALYST_ANTHROPIC_API_KEY=k1\n")
        env_dir = self.mkdir("env")
        r = self.run_cli("plan-analyst", "--capture-dir", str(self.tmp), "--out", str(self.tmp), "--secrets", str(secrets),
                         "--env-dir", str(env_dir), "--prompt", "prompts/analyst.md", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        req = json.loads(r.stdout)
        self.assertEqual(req["role"], "analyst")
        self.assertEqual(Path(req["env_file"]).read_text(), "ANTHROPIC_API_KEY=k1\n")
        secrets.write_text("ANTHROPIC_API_KEY=sk-main-0\n")
        r = self.run_cli("plan-analyst", "--capture-dir", str(self.tmp), "--out", str(self.tmp), "--secrets", str(secrets),
                         "--env-dir", str(env_dir), "--prompt", "prompts/analyst.md", "--json")
        self.assertEqual(r.returncode, 4)
        self.assertIn("analyst token missing", r.stderr)

    def test_validate_style_cli(self):
        style, report = self.tmp / "style.json", self.tmp / "report.md"
        style.write_text(json.dumps(GOOD_STYLE))
        report.write_text(GOOD_REPORT)
        self.assertEqual(self.run_cli("validate-style", str(style), "--report", str(report)).returncode, 0)
        report.write_text(GOOD_REPORT.replace("## Motion", "## Movement"))
        r = self.run_cli("validate-style", str(style), "--report", str(report))
        self.assertEqual(r.returncode, 1)
        self.assertIn("## Motion", r.stdout + r.stderr)
        style.write_text(json.dumps({"schema": 1, "palette": "red"}))
        r = self.run_cli("validate-style", str(style))
        self.assertEqual(r.returncode, 1)
        self.assertIn("palette", r.stdout + r.stderr)
        style.write_text("[")
        self.assertEqual(self.run_cli("validate-style", str(style)).returncode, 1)

    def test_freeze_and_remove_cli(self):
        spec = self.mkdir("spec")
        subprocess.run(["git", "-C", str(spec), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(spec), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(spec), "config", "user.name", "tester"], check=True)
        src = self.mkdir("from")
        (src / "report.md").write_text(GOOD_REPORT)
        (src / "style.json").write_text(json.dumps(GOOD_STYLE))
        r = self.run_cli("freeze", "--from", str(src), "--spec-dir", str(spec), "--index", "7", "--host", "example.com")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(spec / "refs" / "style-refs" / "07"))
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "style refs: 07 example.com")
        self.assertEqual(self.run_cli("freeze", "--from", str(src), "--spec-dir", str(spec), "--index", "0", "--host", "x").returncode, 1)
        r = self.run_cli("remove", "--spec-dir", str(spec), "--index", "7")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(git(spec, "log", "-1", "--format=%s"), "style refs: remove 07")

    def test_prompt_states_the_rules(self):
        text = (REPO / "prompts" / "analyst.md").read_text()
        for heading in stylerefs.REPORT_HEADINGS:
            self.assertIn(heading, text)
        for phrase in ["style.json", "report.md", "/out", "/capture/capture.json", "never", "logos", "data", "at most 8"]:
            self.assertIn(phrase, text)
        self.assertRegex(text, r"never (an )?instruction")


if __name__ == "__main__":
    unittest.main()
