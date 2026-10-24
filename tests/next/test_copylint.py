import tempfile
import unittest
from pathlib import Path

from hands import copylint

INPUTS = {"banned_words": ["synergy", "best-in-class"], "proof": ["Used by 40 bakeries"]}


def lint(files: dict, copy_md=None, final=False, only=None):
    with tempfile.TemporaryDirectory() as d:
        for rel, text in files.items():
            p = Path(d) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return copylint.check(Path(d), INPUTS, copy_md, only, final)


def rules(problems):
    return [p["rule"] for p in problems]


class Rules(unittest.TestCase):
    def test_banned_whole_word_case_insensitive(self):
        self.assertEqual(rules(lint({"a.astro": "<p>Pure SYNERGY</p>"})), ["banned"])
        self.assertEqual(rules(lint({"a.astro": "<p>synergies</p>"})), [])

    def test_banned_in_alt_but_not_in_class_or_href(self):
        self.assertEqual(rules(lint({"a.html": '<img alt="best-in-class" class="synergy" src="synergy.png">'})), ["banned"])

    def test_frontmatter_and_script_and_style_are_code(self):
        src = '---\nconst x = "synergy";\n---\n<style>.synergy{}</style><script>var s="synergy"</script><p>ok</p>'
        self.assertEqual(lint({"a.astro": src}), [])

    def test_line_numbers_and_sorting(self):
        src = "<p>fine</p>\n<p>synergy</p>\n<p>Lorem ipsum</p>"
        got = lint({"b.html": src, "a.html": src})
        self.assertEqual([(p["file"], p["line"], p["rule"]) for p in got],
                         [("a.html", 2, "banned"), ("a.html", 3, "lorem"), ("b.html", 2, "banned"), ("b.html", 3, "lorem")])

    def test_lorem_ignored_on_placeholder_line(self):
        self.assertEqual(lint({"a.html": "<p>[NEEDS INPUT: lorem ipsum replacement]</p>"}), [])

    def test_invented_proof_vs_real(self):
        self.assertEqual(rules(lint({"a.html": "<p>98% of bakers</p><p>Rated 4.9/5</p><p>★★★★★</p><p>3x faster</p>"})),
                         ["invented-proof"])
        self.assertEqual(lint({"a.html": "<p>Used by 40 bakeries</p>"}), [])
        self.assertEqual(lint({"a.html": "<p>500+ customers</p>"}, copy_md="We have 500+ customers."), [])
        self.assertEqual(rules(lint({"a.html": "<p>500+ customers</p>"})), ["invented-proof"])

    def test_attribution_line(self):
        self.assertEqual(rules(lint({"a.html": '<p>"Great." — Jo Doe, CEO</p>'})), ["invented-proof"])
        self.assertEqual(lint({"a.html": '<p>"Great." — Jo Doe, CEO</p>'}, copy_md='"Great." — Jo Doe, CEO of X'), [])

    def test_placeholder_only_in_final_step(self):
        files = {"a.html": "<p>[NEEDS INPUT: hours]</p>"}
        self.assertEqual(lint(files), [])
        self.assertEqual(rules(lint(files, final=True)), ["placeholder-final"])

    def test_text_trimmed_to_200(self):
        got = lint({"a.html": "<p>synergy " + "x" * 400 + "</p>"})
        self.assertEqual(len(got[0]["text"]), 200)

    def test_skipped_files(self):
        bad = "<p>synergy</p>"
        files = {"node_modules/x/a.html": bad, "tests/a.html": bad, "docs/a.md": "synergy", "src/a.test.html": bad,
                 "src/a.css": "synergy", "src/a.ts": "synergy", "src/a.js": "synergy"}
        self.assertEqual(lint(files), [])
        self.assertEqual(lint(files, only=list(files)), [])

    def test_markdown_prose_skips_code_and_frontmatter(self):
        src = "---\ntitle: synergy\n---\nHello\n```\nsynergy\n```\nsynergy here\n"
        got = lint({"a.md": src})
        self.assertEqual([(p["line"], p["rule"]) for p in got], [(8, "banned")])

    def test_json_only_under_content(self):
        self.assertEqual(rules(lint({"src/content/a.json": '{"t": "synergy"}'})), ["banned"])
        self.assertEqual(lint({"src/data/a.json": '{"t": "synergy"}'}), [])

    def test_files_outside_root_ignored(self):
        self.assertEqual(lint({"a.html": "synergy"}, only=["../a.html"]), [])


if __name__ == "__main__":
    unittest.main()
