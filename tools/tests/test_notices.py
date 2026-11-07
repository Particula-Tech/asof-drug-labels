"""The shared notice texts (tools/notices.py): no counts, no dates, the contact, the wording rules."""
import json
import re
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import notices  # noqa: E402

ROOT = TOOLS.parent
CONSTANTS = {name: value for name, value in vars(notices).items() if name.isupper() and isinstance(value, str)}
# Legal references that contain digit groups but are neither counts nor dates.
LEGAL_REFS = ("2011/833/EU", "2001/83/EC", "Article 21(3)", "1995-2026", "SHA-256", "4.0", "Apache-2.0",
              "CC0", "5 business days", "10 business days")


class ConstantsTest(unittest.TestCase):
    def test_no_counts_and_no_dates(self):
        counts = set()
        manifest = ROOT / "manifest.json"
        if manifest.exists():
            m = json.loads(manifest.read_text(encoding="utf-8"))
            for c in m["corpora"].values():
                counts |= {str(v) for v in c.values() if isinstance(v, int) and not isinstance(v, bool) and v > 9}
        for name, text in CONSTANTS.items():
            with self.subTest(name):
                self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", text), "a date")
                self.assertIsNone(re.search(r"\b\d{1,3}(,\d{3})+\b", text), "a thousands-separated count")
                plain = text
                for ref in LEGAL_REFS:
                    plain = plain.replace(ref, "")
                self.assertEqual(re.findall(r"(?<![\w./-])\d+(?![\w/-])", plain), [], "a bare number")
                for n in counts:
                    self.assertIsNone(re.search(rf"(?<![\w.]){n}(?![\w])", text), f"the corpus count {n}")
        self.assertIn("1995-2026", notices.EU_LICENCE)

    def test_scope_is_filled_at_run_time(self):
        self.assertEqual(notices.scope("us", labels=5393, molecules=502),
                         "Covers this record only: 5,393 US labels of 502 selected molecules, as DailyMed showed "
                         "them; not all of DailyMed and not every label of these molecules.")
        self.assertEqual(notices.scope("eu", labels=206, molecules=206),
                         "Covers this record only: the English Annex I of 206 EU reference products, one per "
                         "molecule; not every product authorised in the EU.")
        with self.assertRaises(ValueError):
            notices.scope("uk", labels=1, molecules=1)
        self.assertIn("'old-to-PLR'", notices.FORMAT_CONVERSION)

    def test_contact_where_expected(self):
        self.assertEqual(notices.CONTACT, "legal@particula.tech")
        self.assertIn(notices.CONTACT, notices.RIGHTS_HOLDERS)
        for corpus in notices.CORPORA:
            self.assertIn(notices.CONTACT, notices.corpus_notice(corpus))

    def test_wording_rules(self):
        texts = dict(CONSTANTS)
        for corpus in notices.CORPORA:
            texts[f"readme-{corpus}"] = notices.corpus_readme(corpus)
            texts[f"notice-{corpus}"] = notices.corpus_notice(corpus)
        for name, text in texts.items():
            with self.subTest(name):
                low = text.lower()
                self.assertNotIn("approved on", low)
                self.assertNotIn("fda-approved", low)
                self.assertNotIn("wording unchanged", low)
                self.assertNotIn("to be confirmed", low)
                self.assertNotIn("particula.tech/book", low)
                for banned in ("claude", "anthropic", "generated with", "co-authored"):
                    self.assertNotIn(banned, low)

    def test_corpus_readmes_carry_attribution_and_point_to_the_notice(self):
        leads = {"us": "Courtesy of the U.S. National Library of Medicine", "eu": "© European Union, 1995-2026"}
        attributions = {"us": notices.ATTRIBUTION_US, "eu": notices.ATTRIBUTION_EU}
        for corpus, lead in leads.items():
            with self.subTest(corpus):
                text = notices.corpus_readme(corpus)
                notice = notices.corpus_notice(corpus)
                self.assertIn(lead, text)
                self.assertIn(attributions[corpus], text)
                self.assertIn(notices.CODE_REPO, text)
                self.assertIn("See `NOTICE.md`", text)
                self.assertNotIn("particula.tech/", text)
                # the legal paragraphs live once, in NOTICE.md, not repeated in the README
                for paragraph in (notices.RIGHTS_HOLDERS, notices.INDEPENDENCE, notices.NO_WARRANTY):
                    self.assertNotIn(paragraph, text)
                    self.assertIn(paragraph, notice)
                self.assertIn("legal@particula.tech", notice)

    def test_eu_attribution_indicates_changes(self):
        for part in ("Commission Decision 2011/833/EU", "CC BY 4.0", "Changes: converted from PDF to Markdown"):
            self.assertIn(part, notices.ATTRIBUTION_EU)

    def test_changes_say_the_official_text_prevails(self):
        for text in (notices.US_CHANGES, notices.EU_CHANGES):
            self.assertIn("the official text prevails", text)

    def test_trailers_are_single_git_trailer_lines(self):
        for trailer in (notices.TRAILER_US, notices.TRAILER_EU):
            self.assertNotIn("\n", trailer)
            key, sep, value = trailer.partition(": ")
            self.assertEqual((key, sep), ("Text-Fidelity", ": "))
            self.assertIn("intended to be unchanged", value)

    def test_eu_licence_does_not_claim_cc_by_for_the_smpc_text(self):
        self.assertIn("not licensed by this project and stay with the holder", notices.EU_LICENCE)
        self.assertIn("The Commission's own content (decisions, register pages, open dataset)", notices.EU_LICENCE)
        self.assertIn("is not public domain, CC0 or openly licensed", notices.US_LICENCE)


class RootFilesTest(unittest.TestCase):
    def test_byte_stable_and_utf8_with_trailing_newline(self):
        for corpus in notices.CORPORA:
            a, b = notices.root_files(corpus), notices.root_files(corpus)
            self.assertEqual(a, b)
            self.assertEqual(sorted(a), ["NOTICE.md", "README.md"])
            for name, data in a.items():
                text = data.decode("utf-8")
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"), name)

    def test_readme_content(self):
        us, eu = notices.corpus_readme("us"), notices.corpus_readme("eu")
        self.assertEqual(us.split("\n\n")[0], "# As-of Drug Labels: US corpus")
        self.assertEqual(eu.split("\n\n")[0], "# As-of Drug Labels: EU corpus")
        self.assertIn(notices.FIDELITY_US, us)
        self.assertIn(notices.FIDELITY_EU, eu)
        self.assertIn("never FDA approval dates", us)
        self.assertIn("never an approval or Official Journal date", eu)
        for trailer in ("Source-Id", "Source-Version", "Published-Date", "Source-URL", "Source-SHA256", "Text-Fidelity"):
            self.assertIn(f"`{trailer}`", us)
        for trailer in ("Effective-Date", "Decision-Number"):
            self.assertIn(f"`{trailer}`", eu)
        for text in (us, eu):
            self.assertTrue(text.rstrip().endswith("Not medical advice."))
            self.assertLess(len(text.split()), 300)

    def test_notice_content(self):
        us, eu = notices.corpus_notice("us"), notices.corpus_notice("eu")
        self.assertEqual(us.split("\n\n"), ["# Notice", notices.US_LICENCE, notices.RIGHTS_HOLDERS,
                                            notices.INDEPENDENCE, notices.NO_WARRANTY + "\n"])
        self.assertEqual(eu.split("\n\n"), ["# Notice", notices.EU_LICENCE, notices.EU_SOURCES_READ,
                                            notices.RIGHTS_HOLDERS, notices.INDEPENDENCE, notices.NO_WARRANTY + "\n"])

    def test_unknown_corpus(self):
        with self.assertRaises(ValueError):
            notices.root_files("uk")

    def test_no_hard_wrapped_paragraphs(self):
        for corpus in notices.CORPORA:
            for text in (notices.corpus_readme(corpus), notices.corpus_notice(corpus)):
                for para in text.strip().split("\n\n"):
                    self.assertNotIn("\n", para)


if __name__ == "__main__":
    unittest.main()
