"""0.5 step 15: create a project from an existing repo (imported, never deleted), planner code summary,
coder prompt note, allowed_paths coverage warning. Fake GitHub API + local bare repos as in test_newproject."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401
os.environ["HANDS_NO_NOTIFY"] = "1"
os.environ["HANDS_TEST_ALLOW_ROOT"] = "1"

from hands import codesummary, config as cfgmod, newproject, planner, prompts  # noqa: E402
import test_newproject as tn  # noqa: E402
import test_planner as tp  # noqa: E402

git = tn.git

LEGACY_FILES = {
    "package.json": json.dumps({"name": "legacy", "packageManager": "pnpm@12.11.2",
                                "scripts": {"test": "vitest run", "lint": "eslint .", "build": "next build"},
                                "dependencies": {"next": "15.3.0", "react": "19.1.0"},
                                "devDependencies": {"vitest": "3.2.0", "typescript": "5.8.0"}}),
    "README.md": "# Legacy\n\nAn existing Next.js app.\n",
    "src/app/page.tsx": "export default function Page() { return null }\n",
    "src/lib/db.ts": "export const db = 1\n",
    "lib/util.ts": "export const u = 1\n",
    "tests/util.test.ts": "test('x', () => {})\n",
    "vitest.config.ts": "export default {}\n",
    ".github/workflows/ci.yml": "name: ci\n",
    "LICENSE": "MIT\n",
}


class ImportGH(tp.GitData):
    """Existing repos (not created by hands) with archived/permissions flags and deploy-key ids."""

    def __init__(self, remote_root):
        super().__init__(remote_root)
        self.existing = {}          # name -> {"id", "archived", "default_branch", "permissions"}
        self.key_ids = {}           # repo name -> next key id
        self.deleted_keys = []
        fake = self
        H = self.srv.RequestHandlerClass
        base_do = H._do

        def _do(h):
            p = h.path.split("?")[0].split("/")
            if p[1] == "repos" and len(p) >= 4 and p[3] in fake.existing:
                n = int(h.headers.get("Content-Length") or 0)
                body = json.loads(h.rfile.read(n)) if n else None
                fake.calls.append((h.command, h.path, body, h.headers.get("Authorization")))
                meta = fake.existing[p[3]]
                if h.command == "GET" and len(p) == 4:
                    return h._reply(200, {"id": meta["id"], "archived": meta.get("archived", False),
                                          "default_branch": meta.get("default_branch", "main"),
                                          "permissions": meta.get("permissions", {"admin": True, "push": True})})
                if h.command == "GET" and p[4] == "branches":
                    bare = fake.root / f"{p[3]}.git"
                    ok = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", f"refs/heads/{p[5]}"],
                                        capture_output=True).returncode == 0
                    return h._reply(200, {"name": p[5]}) if ok else h._reply(404)
                if h.command == "POST" and p[4] == "keys":
                    fake.key_ids[p[3]] = fake.key_ids.get(p[3], 7000) + 1
                    return h._reply(201, {"id": fake.key_ids[p[3]]})
                if h.command == "DELETE" and p[4] == "keys":
                    fake.deleted_keys.append((p[3], int(p[5])))
                    return h._reply(204)
                if h.command == "DELETE" and len(p) == 4:
                    return h._reply(500, {"message": "imported repo must never be deleted"})
                if h.command == "POST" and p[4] == "rulesets":
                    return h._reply(201, {"id": 1})
                if h.command == "PUT" and p[4] == "contents":
                    return h._reply(500, {"message": "imported repo contents must not be written by create"})
            return base_do(h)
        H._do = _do
        H.do_GET = H.do_POST = H.do_PUT = H.do_DELETE = H.do_PATCH = _do

    def add_existing(self, name, files, *, rid=4242, branch="main", extra_branch=None, **meta):
        bare = self.root / f"{name}.git"
        git("init", "-q", "--bare", "-b", branch, str(bare))
        w = Path(tempfile.mkdtemp())
        git("clone", "-q", str(bare), str(w / "c"))
        for rel, text in files.items():
            f = w / "c" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        git("add", "-A", cwd=w / "c")
        git("-c", "user.name=h", "-c", "user.email=h@x", "commit", "-qm", "existing code", cwd=w / "c")
        git("push", "-q", "origin", branch, cwd=w / "c")
        if extra_branch:
            git("checkout", "-qb", extra_branch, cwd=w / "c")
            (w / "c" / "BRANCH").write_text(extra_branch)
            git("add", "-A", cwd=w / "c")
            git("-c", "user.name=h", "-c", "user.email=h@x", "commit", "-qm", "branch", cwd=w / "c")
            git("push", "-q", "origin", extra_branch, cwd=w / "c")
        self.ids[name] = rid
        self.existing[name] = {"id": rid, "default_branch": branch, **meta}


class Base(tn.Base):
    def setUp(self):
        super().setUp()
        self.gh.close()
        self.gh = ImportGH(self.remote)
        os.environ["HANDS_GH_API"] = self.gh.url
        os.environ["HANDS_NPM_REGISTRY"] = self.gh.url + "/npm"
        self.gh.add_existing("legacy", LEGACY_FILES)


class TestCreateFromRepo(Base):
    def test_happy_path(self):
        rc = newproject.run_create("shop", "Add a cart to the legacy app", from_repo="acme/legacy")
        self.assertEqual(rc, 0)
        root = self.home / "projects" / "shop"
        cfg = cfgmod.load("shop")
        self.assertTrue(cfg.managed)
        self.assertTrue(cfg.imported)
        self.assertEqual((cfg.repo_specs, cfg.repo_app, cfg.github_org), ("shop-specs", "acme/legacy", "acme"))
        self.assertEqual(cfg.branch, "main")
        self.assertTrue((root / "specs" / ".github" / "CODEOWNERS").is_file())
        self.assertTrue((root / "app" / "package.json").is_file(), "clone of the existing repo")
        self.assertTrue((root / "app" / "src" / "app" / "page.tsx").is_file())
        rec = json.loads((root / "created.json").read_text())
        self.assertEqual(set(rec["repos"]), {"shop-specs"}, "only the specs repo is recorded as created")
        self.assertEqual(rec["imported"]["repo"], "acme/legacy")
        self.assertEqual(rec["imported"]["id"], 4242)
        self.assertEqual(rec["imported"]["deploy_key_id"], 7001)
        self.assertEqual(rec["imported"]["branch"], "main")
        self.assertFalse((self.remote / "shop-app.git").exists(), "no app repo is created")
        posts = [c for c in self.gh.calls if c[0] == "POST" and c[1] == "/orgs/acme/repos"]
        self.assertEqual([c[2]["name"] for c in posts], ["shop-specs"])
        keys = {c[1]: c[2]["read_only"] for c in self.gh.calls if c[1].endswith("/keys")}
        self.assertEqual(keys, {"/repos/acme/shop-specs/keys": True, "/repos/acme/legacy/keys": False})
        rules = [c[1] for c in self.gh.calls if c[1].endswith("/rulesets")]
        self.assertEqual(rules, ["/repos/acme/shop-specs/rulesets"], "no ruleset on the imported repo unless asked")
        self.assertFalse(any(c[0] == "PUT" and "/legacy/" in c[1] for c in self.gh.calls), "nothing written to it")
        sshc = git("config", "core.sshCommand", cwd=root / "app").strip()
        self.assertIn("deploy_legacy", sshc)
        self.assertEqual(json.loads(newproject.status_path("shop").read_text())["state"], "done")
        self.assertIn("imported", (self.cfg_home / "projects" / "shop.toml").read_text())

    def test_ruleset_and_branch_when_asked(self):
        self.gh.add_existing("old", LEGACY_FILES, rid=11, extra_branch="develop")
        rc = newproject.run_create("two", "x", from_repo="acme/old", branch="develop", ruleset=True)
        self.assertEqual(rc, 0)
        cfg = cfgmod.load("two")
        self.assertEqual(cfg.branch, "develop")
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.home / "projects" / "two" / "app").strip(),
                         "develop")
        self.assertTrue((self.home / "projects" / "two" / "app" / "BRANCH").is_file())
        rules = {c[1]: c[2] for c in self.gh.calls if c[1].endswith("/rulesets")}
        self.assertIn("/repos/acme/old/rulesets", rules)
        self.assertNotIn("pull_request", [r["type"] for r in rules["/repos/acme/old/rulesets"]["rules"]])
        self.assertEqual(json.loads((self.home / "projects" / "two" / "created.json").read_text())["imported"]["branch"],
                         "develop")

    def test_missing_branch_refused_before_anything(self):
        self.assertEqual(newproject.run_create("bbb", "x", from_repo="acme/legacy", branch="nope"), 1)
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in self.gh.calls))
        self.assertFalse((self.cfg_home / "projects" / "bbb.toml").exists())
        for bad in ("-x", "a..b", "a b", "a\\b", "x@{1}", "a" * 130, ""):
            with self.assertRaises(newproject.CreateError, msg=bad):
                newproject.validate_branch(bad)

    def test_missing_repo(self):
        self.assertEqual(newproject.run_create("gone", "x", from_repo="acme/nothere"), 1)
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in self.gh.calls))
        self.assertFalse((self.cfg_home / "projects" / "gone.toml").exists())
        st = json.loads(newproject.status_path("gone").read_text())
        self.assertEqual(st["state"], "failed")
        self.assertIn("acme/nothere", st["error"])

    def test_archived_repo(self):
        self.gh.add_existing("museum", LEGACY_FILES, rid=12, archived=True)
        self.assertEqual(newproject.run_create("mus", "x", from_repo="acme/museum"), 1)
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in self.gh.calls))
        self.assertIn("archived", json.loads(newproject.status_path("mus").read_text())["error"])

    def test_no_admin_access(self):
        self.gh.add_existing("locked", LEGACY_FILES, rid=13, permissions={"admin": False, "push": True})
        self.assertEqual(newproject.run_create("lkk", "x", from_repo="acme/locked"), 1)
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in self.gh.calls))
        self.assertIn("admin", json.loads(newproject.status_path("lkk").read_text())["error"])

    def test_bad_repo_refs(self):
        for bad in ("legacy", "acme/", "/legacy", "acme/le gacy", "acme/../x", "a/b/c", "-acme/x", "acme/.", "acme/.."):
            with self.assertRaises(newproject.CreateError, msg=bad):
                newproject.parse_repo(bad)
        self.assertEqual(newproject.parse_repo("Acme-Org/my.repo_1"), ("Acme-Org", "my.repo_1"))

    def test_destroy_keeps_imported_repo_removes_key_and_specs(self):
        self.assertEqual(newproject.run_create("shop", "x", from_repo="acme/legacy"), 0)
        head_before = git("-C", str(self.remote / "legacy.git"), "rev-parse", "main").strip()
        self.assertEqual(newproject.destroy("shop", "shop"), 0)
        self.assertTrue((self.remote / "legacy.git").exists(), "imported repo must survive destroy")
        self.assertEqual(git("-C", str(self.remote / "legacy.git"), "rev-parse", "main").strip(), head_before)
        self.assertFalse((self.remote / "shop-specs.git").exists())
        self.assertEqual(self.gh.deleted_keys, [("legacy", 7001)])
        deletes = [c[1] for c in self.gh.calls if c[0] == "DELETE"]
        self.assertEqual(sorted(deletes), ["/repos/acme/legacy/keys/7001", "/repos/acme/shop-specs"])
        self.assertFalse((self.home / "projects" / "shop").exists())
        self.assertFalse((self.cfg_home / "secrets" / "shop").exists())
        self.assertFalse((self.cfg_home / "projects" / "shop.toml").exists())

    def test_destroy_ignores_tampered_record(self):
        self.assertEqual(newproject.run_create("shop", "x", from_repo="acme/legacy"), 0)
        root = self.home / "projects" / "shop"
        rec = json.loads((root / "created.json").read_text())
        rec["repos"]["legacy"] = 4242                     # pretend create made the imported repo
        rec["imported"]["repo"] = "acme/shop-specs"       # and point the key removal somewhere else
        (root / "created.json").write_text(json.dumps(rec))
        self.assertEqual(newproject.destroy("shop", "shop"), 0)
        self.assertTrue((self.remote / "legacy.git").exists())
        self.assertEqual(self.gh.deleted_keys, [])
        # fail closed: a record that names the imported repo as created, or the created repo as imported,
        # deletes nothing on GitHub; local folders and secrets still go
        self.assertEqual([c[1] for c in self.gh.calls if c[0] == "DELETE"], [])
        self.assertTrue((self.remote / "shop-specs.git").exists())
        self.assertFalse((self.home / "projects" / "shop").exists())
        self.assertFalse((self.cfg_home / "secrets" / "shop").exists())

    def test_failure_midway_then_destroy(self):
        self.gh.fail[("POST", "/repos/acme/shop-specs/rulesets")] = 422
        self.assertEqual(newproject.run_create("shop", "x", from_repo="acme/legacy"), 1)
        self.assertEqual(newproject.destroy("shop", "shop"), 0)
        self.assertTrue((self.remote / "legacy.git").exists())
        self.assertFalse((self.remote / "shop-specs.git").exists())
        self.assertEqual(self.gh.deleted_keys, [], "the key was never added, so nothing to remove")

    def test_cli_flags(self):
        from hands.__main__ import main
        import io
        old = sys.stdin
        sys.stdin = io.StringIO("Build on it\n")
        try:
            self.assertEqual(main(["cli", "create", "--from-repo", "acme/legacy", "--branch", "main"]), 0)
        finally:
            sys.stdin = old
        self.assertEqual(cfgmod.load("cli").repo_app, "acme/legacy")


class TestDashboardImport(Base):
    def setUp(self):
        super().setUp()
        from test_v04 import Dash
        os.environ["HANDS_GH_API_FOR_CHILD"] = self.gh.url
        self.d = Dash()
        cfgmod.CONFIG_HOME = self.cfg_home
        os.environ["HANDS_CONFIG_HOME"] = str(self.cfg_home)

    def tearDown(self):
        self.d.close()
        super().tearDown()

    def post(self, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=30)
        data = json.dumps(body).encode()
        c.request("POST", "/api/new", body=data, headers={
            "Host": f"127.0.0.1:{self.d.port}", "Origin": f"http://127.0.0.1:{self.d.port}", "X-Hands": "1",
            "Cookie": f"hands_dash={self.d.sid}", "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_form_fields_reach_create(self):
        import time
        self.assertEqual(self.post({"slug": "shop", "description": "x", "from_repo": "acme/le gacy"})[0], 400)
        self.assertEqual(self.post({"slug": "shop", "description": "x", "from_repo": "acme/legacy", "branch": "a..b"})[0], 400)
        st, body = self.post({"slug": "shop", "description": "Cart", "from_repo": "acme/legacy", "ruleset": True})
        self.assertEqual(st, 200, body)
        for _ in range(120):
            s = json.loads(newproject.status_path("shop").read_text())["state"]
            if s != "running":
                break
            time.sleep(0.5)
        self.assertEqual(s, "done", (newproject.creating_dir() / "shop.log").read_text())
        rec = json.loads((self.home / "projects" / "shop" / "created.json").read_text())
        self.assertEqual(rec["imported"]["repo"], "acme/legacy")
        self.assertIn("/repos/acme/legacy/rulesets", [c[1] for c in self.gh.calls])
        st, _, raw = self.d.req("GET", "/api/p/shop")
        d = json.loads(raw)
        self.assertEqual(d["setup"]["repo_app"], "acme/legacy")
        self.assertTrue(d["setup"]["imported"])


class TestCodeSummary(Base):
    def test_summary_contents_and_cap(self):
        self.assertEqual(newproject.run_create("shop", "x", from_repo="acme/legacy"), 0)
        app = self.home / "projects" / "shop" / "app"
        text = codesummary.summarize(app)
        self.assertIn("Next.js", text)
        self.assertIn("pnpm@12.11.2", text)
        self.assertIn("src/app/  page.tsx", text)
        self.assertIn("An existing Next.js app.", text)
        self.assertIn("vitest run", text)
        self.assertIn("vitest.config.ts", text)
        self.assertIn("ci.yml", text)
        # a big repo stays within the cap
        for i in range(600):
            f = app / "src" / f"m{i // 20}" / f"f{i}.ts"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x\n")
        (app / "README.md").write_text("# Big\n" + "lorem ipsum " * 5000)
        git("add", "-A", cwd=app)
        git("-c", "user.name=h", "-c", "user.email=h@x", "commit", "-qm", "big", cwd=app)
        text = codesummary.summarize(app, cap=20000)
        self.assertLessEqual(len(text), 20000)
        self.assertIn("truncated", text)
        self.assertIn("src/m3/", text)
        small = codesummary.summarize(app, cap=2000)
        self.assertLessEqual(len(small), 2000)
        self.assertTrue(small.endswith("(code summary truncated)"), small[-80:])

    def test_symlinks_and_binaries_not_read(self):
        self.assertEqual(newproject.run_create("shop", "x", from_repo="acme/legacy"), 0)
        app = self.home / "projects" / "shop" / "app"
        secret = self.tmp / "secret.txt"
        secret.write_text("TOPSECRET\n")
        (app / "README.md").unlink()
        os.symlink(secret, app / "README.md")
        (app / "package.json").write_bytes(b"\x00\xff not json")
        text = codesummary.summarize(app)
        self.assertNotIn("TOPSECRET", text)
        self.assertIn("package.json", text)

    def test_code_roots_and_coverage(self):
        self.assertEqual(newproject.run_create("shop", "x", from_repo="acme/legacy"), 0)
        app = self.home / "projects" / "shop" / "app"
        self.assertEqual(codesummary.code_roots(app), ["lib/", "package.json", "src/", "tests/", "vitest.config.ts"])
        self.assertEqual(codesummary.coverage_warnings(["src/**", "package.json", "pnpm-lock.yaml"], app),
                         ["allowed_paths does not cover existing code in: lib/, tests/, vitest.config.ts"])
        self.assertEqual(codesummary.coverage_warnings(["**"], app), [])
        self.assertEqual(codesummary.coverage_warnings(["src/**"], self.tmp / "missing"), [])


class TestPlannerImported(tp.TestPlanner):
    """The planner tests with an imported app repo: prompt carries the code summary, warnings surface."""

    def setUp(self):
        tn.Base.setUp(self)
        self.gh.close()
        self.gh = ImportGH(self.remote)
        os.environ["HANDS_GH_API"] = self.gh.url
        os.environ["HANDS_NPM_REGISTRY"] = self.gh.url + "/npm"
        self.gh.add_existing("legacy", LEGACY_FILES)
        self.assertEqual(newproject.run_create("site", "An Astro site for my agency", from_repo="acme/legacy"), 0)
        toml = self.cfg_home / "projects" / "site.toml"
        toml.write_text(toml.read_text() + f'docker_bin = "{tp.FAKE}"\nclaude_env = "{self.tmp / "claude.env"}"\n')
        (self.tmp / "claude.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
        self.scenario = self.tmp / "scenario.json"
        os.environ.update(FAKE_SCENARIO=str(self.scenario), FAKE_LOG=str(self.tmp / "docker.jsonl"),
                          FAKE_PROMPTS=str(self.tmp / "prompts.jsonl"))
        self.started = []
        planner.START = lambda cfg: self.started.append(cfg.name) or 0

    def test_prompt_has_code_summary_and_coverage_warning(self):
        self.queue({"role": "planner", "files": tp.GOOD, "text": "done\nPLANNED: 1 steps; a cart"})
        self.assertEqual(planner.plan(self.cfg()), 0, (planner.pdir(self.cfg()) / "plan.log").read_text())
        prompt = json.loads((self.tmp / "prompts.jsonl").read_text().splitlines()[0])["prompt"]
        self.assertIn("Existing codebase", prompt)
        self.assertIn("Next.js", prompt)
        self.assertIn("src/app/  page.tsx", prompt)
        self.assertIn("An Astro site for my agency", prompt)
        self.assertLess(len(prompt), 40000)
        st = planner.read_state(self.cfg())
        self.assertEqual(st["state"], "ready")
        self.assertEqual(st["warnings"], ["allowed_paths does not cover existing code in: lib/, tests/, vitest.config.ts"])
        pr = self.gh.pulls[1]
        self.assertIn("does not cover existing code", pr["body"])
        from hands import dashboard
        self.assertEqual(dashboard.plan_info(self.cfg())["warnings"], st["warnings"])

    def test_no_warning_when_covered(self):
        good = {**tp.GOOD, "steps.json": json.dumps({**json.loads(tp.GOOD["steps.json"]),
                                                      "allowed_paths": ["src/**", "lib/**", "tests/**", "package.json",
                                                                        "vitest.config.ts"]})}
        self.queue({"role": "planner", "files": good, "text": "done\nPLANNED: 1 steps; a cart"})
        self.assertEqual(planner.plan(self.cfg()), 0)
        self.assertEqual(planner.read_state(self.cfg())["warnings"], [])

    def test_prompt_without_summary_for_created_projects(self):
        cfg = self.cfg()
        text = planner.prompt(cfg, "idea", "", [], False)
        self.assertNotIn("Existing codebase", text)
        text = planner.prompt(cfg, "idea", "", [], False, code_summary="## Tree\nsrc/")
        self.assertIn("Existing codebase", text)
        self.assertIn("src/", text)


class TestCoderPrompt(unittest.TestCase):
    def test_imported_note(self):
        from hands import spec as specmod
        d = Path(tempfile.mkdtemp())
        (d / "steps.json").write_text(json.dumps({"version": 1, "project": "p", "allowed_paths": ["src/**"],
                                                  "steps": [{"id": "S01", "title": "t", "goal": "g",
                                                             "acceptance": ["true"]}]}))
        sp = specmod.load(d, "c" * 40)
        kw = dict(attempt=1, max_attempts=3, progress=[], git_log="", feedback=[], prev_diff="")
        plain = prompts.coder(sp, sp.steps[0], **kw)
        self.assertNotIn("existed before", plain)
        imp = prompts.coder(sp, sp.steps[0], imported=True, **kw)
        self.assertIn("existed before", imp)
        self.assertIn("conventions win", imp)


if __name__ == "__main__":
    unittest.main()
