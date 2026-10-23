"""hands <slug> reset: destroy what create recorded, then create the same project again with its saved
settings. Runs against the fake GitHub API and the local bare repos of tests/test_newproject.py."""
import contextlib
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmpdirs  # noqa: E402,F401  (removes every mkdtemp dir after each test)

from hands import config as cfgmod, newproject, nextproject, nextstate, projectenv, reset  # noqa: E402
from hands.state import Lock  # noqa: E402
from test_newproject import Base  # noqa: E402

DESC = "Booking app for salons"


def quiet(fn, *args, **kw):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = fn(*args, **kw)
    return rc, out.getvalue()


class ResetBase(Base):
    def setUp(self):
        super().setUp()
        self.after = []
        import hands.__main__ as cli
        orig = cli.after_create
        cli.after_create = lambda cfg, refs, framework, libs: self.after.append(
            (cfg.name, list(refs), framework, list(libs))) or 0
        self.addCleanup(setattr, cli, "after_create", orig)

    def make(self, slug: str = "shop", framework: int = 2, libs=("gsap",), refs=("https://a.example/page",),
             env=(("API_KEY", "s3cret", False), ("PUBLIC_URL", "https://x.example", True))) -> None:
        """A created project with the choices and variables the New project form would have stored."""
        self.assertEqual(quiet(newproject.run_create, slug, DESC)[0], 0)
        cfg = cfgmod.load(slug)
        nextproject.init(cfg, list(refs), framework, list(libs))
        if env:
            projectenv.Store(newproject.secrets_root(slug)).set(
                [{"key": k, "value": v, "public": p} for k, v, p in env])

    def env_of(self, slug: str) -> dict:
        s = projectenv.Store(newproject.secrets_root(slug))
        return {k: (v, s.meta[k]["public"]) for k, v in s.values.items()}

    def deletes(self) -> list[str]:
        return [c[1] for c in self.gh.calls if c[0] == "DELETE"]


class ResetReplay(ResetBase):
    def test_reset_recreates_the_project_with_the_saved_settings(self):
        self.make()
        old_ids = json.loads((self.home / "projects" / "shop" / "created.json").read_text())["repos"]
        self.assertEqual(self.env_of("shop")["API_KEY"], ("s3cret", False))
        rc, out = quiet(reset.reset, "shop", "shop")
        self.assertEqual(rc, 0, out)
        self.assertIn("DONE: shop reset", out)
        self.assertEqual(sorted(self.deletes()), ["/repos/acme/shop-app", "/repos/acme/shop-specs"])
        root = self.home / "projects" / "shop"
        self.assertEqual((root / "description.txt").read_text().strip(), DESC)
        self.assertEqual(self.after, [("shop", ["https://a.example/page"], 2, ["gsap"])])
        new_ids = json.loads((root / "created.json").read_text())["repos"]
        self.assertEqual(set(new_ids), set(old_ids))
        self.assertNotEqual(new_ids, old_ids, "the repos are new ones")
        self.assertEqual(self.env_of("shop"), {"API_KEY": ("s3cret", False),
                                               "PUBLIC_URL": ("https://x.example", True)})
        self.assertFalse(reset.stage_dir("shop").exists(), "the staged settings go when the reset is done")
        self.assertEqual(json.loads(newproject.status_path("shop").read_text())["state"], "done")
        self.assertTrue((root / "specs" / ".github" / "CODEOWNERS").is_file())
        self.assertEqual(nextstate.load(cfgmod.load("shop").state_dir / "next.json")["plan_session"]["status"], "none")

    def test_hand_set_config_keys_are_named_before_anything_is_deleted(self):
        self.make()
        toml = self.cfg_home / "projects" / "shop.toml"
        toml.write_text(toml.read_text() + 'cost_cap_usd = 5.0\nplanner_model = "claude-opus-5-5"\n')
        self.assertEqual(reset.hand_written_keys("shop"), ["cost_cap_usd", "planner_model"])
        rc, out = quiet(reset.reset, "shop", "shop")
        self.assertEqual(rc, 0, out)
        self.assertIn("NOT replayed: cost_cap_usd, planner_model", out)
        self.assertNotIn("cost_cap_usd", toml.read_text(), "create writes a fresh config")

    def test_wipe_env_starts_with_no_variables(self):
        self.make()
        rc, out = quiet(reset.reset, "shop", "shop", True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.env_of("shop"), {})
        self.assertIn("variables wiped", out)

    def test_a_project_without_next_json_replays_its_setup_record(self):
        self.assertEqual(quiet(newproject.run_create, "plain", DESC)[0], 0)
        rc, out = quiet(reset.reset, "plain", "plain")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.after, [("plain", [], 1, [])])

    def test_the_cli_needs_the_slug_as_confirmation(self):
        from hands.__main__ import main
        self.make()
        self.assertEqual(quiet(main, ["shop", "reset"])[0], 1)             # no --confirm
        self.assertEqual(self.deletes(), [])
        self.assertEqual(quiet(main, ["shop", "reset", "--confirm", "nope"])[0], 1)
        self.assertEqual(self.deletes(), [])
        rc, out = quiet(main, ["shop", "reset", "--confirm", "shop"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.deletes()), ["/repos/acme/shop-app", "/repos/acme/shop-specs"])


class ResetRefusals(ResetBase):
    def toml(self, slug: str = "shop") -> Path:
        return self.cfg_home / "projects" / f"{slug}.toml"

    def refuse(self, why: str) -> None:
        with self.assertRaises(newproject.CreateError) as cm:
            quiet(reset.reset, "shop", "shop")
        self.assertIn(why, str(cm.exception))
        self.assertEqual(self.deletes(), [], "nothing is deleted when a reset is refused")
        self.assertFalse(reset.stage_dir("shop").exists(), "nothing is staged when a reset is refused")

    def test_imported_and_self_projects_are_refused(self):
        self.make()
        text = self.toml().read_text()
        self.toml().write_text(text + "imported = true\n")
        self.refuse("imported")
        self.toml().write_text(text + "self = true\n")
        self.refuse("Phoenix itself")

    def test_an_unmanaged_project_is_refused(self):
        self.make()
        self.toml().write_text(self.toml().read_text().replace("managed = true", "managed = false"))
        self.refuse("not created by hands")

    def test_refused_while_the_runner_the_scaffold_or_the_planner_runs(self):
        self.make()
        cfg = cfgmod.load("shop")
        with Lock(cfg.state_dir / "runner.lock"):                  # `hands <p> setup` holds the same lock
            self.refuse("the build (or the scaffold) is running")
        (cfg.state_dir / "plan").mkdir(parents=True, exist_ok=True)
        with Lock(cfg.state_dir / "plan" / "plan.lock"):
            self.refuse("the planner is running")
        with Lock(nextproject.refs_lock(cfg)):
            self.refuse("style refs are being processed")
        newproject.creating_dir().mkdir(parents=True, exist_ok=True)
        with Lock(newproject.lock_path("shop")):
            self.refuse("being created right now")
        with Lock(reset.lock_path("shop")):
            self.refuse("already being reset")

    def test_an_unknown_project_is_refused(self):
        with self.assertRaisesRegex(newproject.CreateError, "nothing to reset"):
            quiet(reset.reset, "ghost", "ghost")

    def test_a_missing_description_is_refused(self):
        self.make()
        (self.home / "projects" / "shop" / "description.txt").unlink()
        self.refuse("description")


class ResetHalfway(ResetBase):
    def test_only_recorded_repo_ids_are_deleted(self):
        """A repo that was replaced since the create is left alone -- and then the create half cannot run,
        so the reset says what exists instead of pretending it worked."""
        self.make()
        self.gh.ids["shop-app"] = 5555
        self.gh.fail[("POST", "/orgs/acme/repos")] = 422
        rc, out = quiet(reset.reset, "shop", "shop")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.deletes(), ["/repos/acme/shop-specs"])
        self.assertTrue((self.remote / "shop-app.git").exists(), "a different repo is never deleted")
        self.assertIn("NOT deleted", out)
        self.assertIn("destroyed but not recreated", out)
        self.assertIn("hands shop reset --confirm shop", out)
        self.assertIn("staged settings", out)
        self.assertTrue(reset.stage_file("shop").exists())
        self.assertFalse(self.toml_exists())

    def toml_exists(self) -> bool:
        return (self.cfg_home / "projects" / "shop.toml").exists()

    def test_running_it_again_finishes_the_recreate_from_the_staged_settings(self):
        self.make()
        self.gh.fail[("POST", "/orgs/acme/repos")] = 422
        self.assertEqual(quiet(reset.reset, "shop", "shop")[0], 1)
        staged = json.loads(reset.stage_file("shop").read_text())
        self.assertEqual((staged["description"], staged["framework"], staged["libs"], staged["style_refs"],
                          staged["env_kept"]), (DESC, 2, ["gsap"], ["https://a.example/page"], True))
        self.assertEqual(os.stat(reset.stage_dir("shop")).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(reset.stage_dir("shop") / projectenv.ENV_FILE).st_mode & 0o777, 0o600)
        self.gh.fail.clear()
        for repo in ("shop-specs", "shop-app"):                    # the destroy half really did delete them
            shutil.rmtree(self.remote / f"{repo}.git", ignore_errors=True)
        rc, out = quiet(reset.reset, "shop", "shop")
        self.assertEqual(rc, 0, out)
        self.assertIn("using the staged settings", out)
        self.assertEqual(self.after, [("shop", ["https://a.example/page"], 2, ["gsap"])])
        self.assertEqual(self.env_of("shop")["API_KEY"], ("s3cret", False))
        self.assertFalse(reset.stage_dir("shop").exists())
        self.assertTrue(self.toml_exists())

    def test_a_destroy_that_fails_leaves_the_project_alone(self):
        self.make()
        self.gh.fail[("DELETE", "/repos/acme/shop-specs")] = 500
        rc, out = quiet(reset.reset, "shop", "shop")
        self.assertEqual(rc, 1, out)
        self.assertIn("destroy failed", out)
        self.assertIn("Nothing was recreated", out)
        self.assertTrue(self.toml_exists())
        self.assertTrue((self.home / "projects" / "shop" / "specs").is_dir())
        self.assertTrue(reset.stage_file("shop").exists())
        self.assertEqual(json.loads(newproject.status_path("shop").read_text())["state"], "failed")
        self.assertEqual(self.after, [])

    def test_variables_that_cannot_be_restored_are_reported_by_name_and_kept(self):
        self.make(env=())
        sec = newproject.secrets_root("shop")
        (sec / projectenv.ENV_FILE).write_text("HANDS_RESERVED=x\n")     # hand-written, not settable
        rc, out = quiet(reset.reset, "shop", "shop")
        self.assertEqual(rc, 0, out)
        self.assertIn("variables were not restored", out)
        self.assertIn(str(reset.stage_dir("shop")), out)
        self.assertTrue((reset.stage_dir("shop") / projectenv.ENV_FILE).is_file())
        self.assertFalse(reset.stage_file("shop").exists(), "no later reset may replay these settings")
        self.assertNotIn("HANDS_RESERVED=x", out)


if __name__ == "__main__":
    unittest.main()
