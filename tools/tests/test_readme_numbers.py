"""Every number in README.md regenerates from committed JSON (tools/readme_numbers.py)."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import readme_numbers as rn  # noqa: E402

ROOT = TOOLS.parent


class RealReadmeTest(unittest.TestCase):
    def test_check_passes_on_the_real_readme(self):
        self.assertEqual(rn.main(["--check"]), 0)

    def test_every_key_resolves(self):
        vals = rn.values()
        self.assertEqual(set(vals), set(rn.KEYS))
        self.assertTrue(all(v != "" for v in vals.values()))

    def test_readme_rules(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        # The frozen SrLC validation text denies an approval date in these exact words; nothing else may say it.
        denial = "It does not show that the label in the repo was FDA-approved on any particular date."
        self.assertEqual(text.count(denial), 1)
        self.assertEqual(text.replace(denial, "").lower().count("approved on"), 0)
        self.assertIn("## What it does not show", text)
        self.assertIn("## Validation against FDA safety labeling changes", text)
        for setid in ("1e11ad30-1041-4520-10b0-8f9d30d30fcc", "f0ff4f27-5185-4881-a749-c6b7a0ca5696"):
            self.assertIn(setid, text)
        for cmd in ("tools/cli.py blame", "tools/cli.py changes",
                    "tools/cli.py compare <!-- n:q3_molecule -->bevacizumab<!-- /n --> <!-- n:q3_date -->"):
            self.assertIn(cmd, text)
        self.assertIn("git clone --recurse-submodules", text)
        self.assertIn("git submodule update --init --recursive", text)
        self.assertNotIn("to be confirmed", text)
        self.assertIn("legal@particula.tech", text)
        self.assertIn("## Particula Tech", text)
        self.assertNotIn("ema.europa.eu", text)
        self.assertNotIn("particula.tech/book", text)
        self.assertEqual(text.count("particula.tech/open-source/asof"), 1)
        self.assertGreater(text.index("particula.tech/open-source/asof"), text.index("## Particula Tech"))
        self.assertNotIn("mostly because the two labels phrase", text)

    def test_front_door_links_resolve_and_skip_internal_files(self):
        """Relative links (and anchors) in the public front-door docs point at files that exist, and none of them
        names a file that stays out of the public repo."""
        internal = ("asof-drug-labels-spec.md", "asof-conventions.md", "launch/", "internal/", "docs/response-playbook.md")
        link = re.compile(r"\]\(([^)\s]+)\)|<img [^>]*src=\"([^\"]+)\"")
        for name in ("README.md", "disclaimer.md", "visualisations/README.md"):
            path = ROOT / name
            text = path.read_text(encoding="utf-8")
            for banned in internal:
                self.assertNotIn(banned, text, f"{name} names internal file {banned}")
            for m in link.finditer(text):
                target = m.group(1) or m.group(2)
                if re.match(r"^(https?|mailto):", target):
                    continue
                file_part, _, anchor = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part else path
                self.assertTrue(dest.exists(), f"{name}: broken link {target}")
                if anchor:
                    slugs = {re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")
                             for h in re.findall(r"^#+ (.+)$", dest.read_text(encoding="utf-8"), re.M)}
                    self.assertIn(anchor, slugs, f"{name}: no heading for #{anchor} in {file_part or name}")

    def test_install_line_uses_the_clone_root(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("claude mcp add asof-drug-labels -- python3 -I <path>/tools/mcp_server.py", text)
        self.assertNotIn("<path>/asof-drug-labels/", text)
        self.assertNotIn("build/us", text)
        self.assertNotIn("build/eu", text)
        self.assertIn("Python 3.12 or newer", text)
        self.assertLess(text.index("git clone --recurse-submodules"), text.index("git -C us log"))

    def test_disclaimer_states_each_rule_once(self):
        text = (ROOT / "disclaimer.md").read_text(encoding="utf-8")
        self.assertIn("legal@particula.tech", text)
        self.assertEqual(text.count("the official text prevails"), 1)
        self.assertEqual(text.count("within 10 business days"), 1)
        self.assertEqual(text.count("Not intended for diagnosis"), 0)
        self.assertEqual(text.lower().count("approved on"), 0)

    def test_query3_command_and_answer_use_the_us_record_end(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        record_to = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["corpora"]["us"]["record_to"]
        dates = re.findall(r"<!-- n:q3_date -->(.*?)<!-- /n -->", text)
        self.assertGreaterEqual(len(dates), 2)                 # the command and the answer
        self.assertEqual(set(dates), {rn.values(keys=["q3_date"])["q3_date"]})
        q = json.loads((ROOT / "visualisations" / "data" / "01-example-queries.json").read_text(encoding="utf-8"))
        if q["inputs"]["us_record_to"] == record_to:
            self.assertEqual(dates[0], record_to)
        for banned in ("Generated with", "Co-Authored-By", "Anthropic"):
            self.assertNotIn(banned, text)

    def test_markdown_has_no_hard_wrapped_paragraphs(self):
        for name in ("README.md", "disclaimer.md", "reproduce.md", "visualisations/README.md",
                     "docs/daily-update.md", "docs/eu-format.md"):
            path = ROOT / name
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            in_code = False
            for prev, line in zip([""] + lines, lines):
                if line.startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    continue
                plain = prev.strip() and line.strip() and not re.match(r"^\s*([-*|>#]|\d+\.|<)", line) \
                    and not re.match(r"^\s*([-*|>#]|\d+\.|<)", prev) and not prev.endswith("  ")
                self.assertFalse(plain, f"{name}: hard-wrapped paragraph near {line[:60]!r}")


class MechanismTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "molecules").mkdir()
        for inn in ("a", "b", "c"):
            (self.root / "molecules" / f"{inn}.json").write_text("{}", encoding="utf-8")
        (self.root / "molecules" / "selection.json").write_text("{}", encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps(
            {"corpora": {"us": {"labels": 12345, "head": "abcdef0123456789"}}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        readme = self.root / "README.md"
        readme.write_text("Labels: <!-- n:us_labels -->1<!-- /n -->, molecules <!-- n:molecules -->0<!-- /n -->, "
                          "head <!-- n:us_head -->x<!-- /n -->.\n", encoding="utf-8")
        args = ["--readme", str(readme), "--root", str(self.root)]
        self.assertEqual(rn.main(["--check", *args]), 1)
        self.assertEqual(rn.main(["--write", *args]), 0)
        self.assertEqual(readme.read_text(encoding="utf-8"),
                         "Labels: <!-- n:us_labels -->12,345<!-- /n -->, molecules <!-- n:molecules -->3<!-- /n -->, "
                         "head <!-- n:us_head -->abcdef012345<!-- /n -->.\n")
        self.assertEqual(rn.main(["--check", *args]), 0)
        self.assertEqual(rn.main(["--write", *args]), 0)   # idempotent

    def test_number_outside_marker_fails(self):
        text = "Labels: <!-- n:us_labels -->12,345<!-- /n --> and 47,287 commits typed by hand.\n"
        problems = rn.check_text(text, self.root)
        self.assertTrue(any("47,287" in p for p in problems))

    def test_bare_number_outside_marker_fails_but_a_year_does_not(self):
        base = "Labels: <!-- n:us_labels -->12,345<!-- /n -->."
        self.assertTrue(any("12345" in p for p in rn.check_text(base + " We have 12345 labels.\n", self.root)))
        self.assertEqual(rn.check_text(base + " The busiest year was 2019.\n", self.root), [])
        ok = (base + " `git log 123456`, [x](https://e.org/12345), https://e.org/99999 and\n"
              "```sh\nrun 54321\n```\n<pre><code>run 77777</code></pre>\n")
        self.assertEqual(rn.check_text(ok, self.root), [])
        saved = rn.ALLOWLIST
        try:
            rn.ALLOWLIST = ("12345",)
            self.assertEqual(rn.check_text(base + " We have 12345 labels.\n", self.root), [])
        finally:
            rn.ALLOWLIST = saved

    def test_listing_numbers_stay_consistent_after_an_append(self):
        """After a daily append manifest.json moves commits and the listing counts together, so the README's
        'listed - logged == versions' holds without reports/coverage-us.json."""
        us = {"labels": 2, "head": "a" * 40, "commits": 47287, "listed_versions": 47728, "logged_versions": 441,
              "fetched_pct": 99.08}
        readme = self.root / "README.md"
        readme.write_text("<!-- n:us_versions -->0<!-- /n --> versions; DailyMed lists <!-- n:us_listed_versions -->0"
                          "<!-- /n -->; <!-- n:us_fetched_pct -->0<!-- /n -->% are in; <!-- n:us_logged_versions -->0"
                          "<!-- /n --> logged.\n", encoding="utf-8")
        for commits, listed in ((47287, 47728), (47310, 47751)):      # before and after 23 appended versions
            us.update(commits=commits, listed_versions=listed, fetched_pct=round(100 * commits / listed, 2))
            (self.root / "manifest.json").write_text(json.dumps({"corpora": {"us": us}}), encoding="utf-8")
            self.assertEqual(rn.main(["--write", "--readme", str(readme), "--root", str(self.root)]), 0)
            got = dict(rn.markers(readme.read_text(encoding="utf-8")))
            n = {k: int(got[k].replace(",", "")) for k in ("us_versions", "us_listed_versions", "us_logged_versions")}
            self.assertEqual(n["us_listed_versions"] - n["us_logged_versions"], n["us_versions"])
        self.assertEqual(rn.KEYS["us_listed_versions"][0], "manifest.json")

    def test_eu_no_reference_counts(self):
        mols = self.root / "molecules"
        (mols / "a.json").write_text(json.dumps({"eu": [{"role": "reference"}]}), encoding="utf-8")
        (mols / "b.json").write_text(json.dumps({"eu": [], "eu_selection": {
            "no_reference_reason": "no central product with this exact INN in the EMA medicines JSON"}}))
        (mols / "c.json").write_text(json.dumps({"eu": [{"role": "alternate"}], "eu_selection": {
            "no_reference_reason": "only combination central products under this exact INN (excluded in v1)"}}))
        got = rn.values(self.root, ["eu_no_reference", "eu_no_central", "eu_only_combination", "molecules"])
        self.assertEqual(got, {"eu_no_reference": "2", "eu_no_central": "1", "eu_only_combination": "1",
                               "molecules": "3"})

    def test_unknown_key_fails(self):
        problems = rn.check_text("<!-- n:no_such_key -->1<!-- /n -->", self.root)
        self.assertTrue(any("unknown key" in p for p in problems))

    def test_format(self):
        self.assertEqual(rn.fmt(47287), "47,287")
        self.assertEqual(rn.fmt(99.08), "99.08")
        self.assertEqual(rn.fmt("2026-10-30"), "2026-10-30")


if __name__ == "__main__":
    unittest.main()
