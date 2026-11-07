"""Converter rules on small synthetic SPL documents."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import spl  # noqa: E402


def section(code, title, inner="", sub=""):
    code_el = f'<code code="{code}" codeSystem="2.16.840.1.113883.6.1"/>' if code else "<code/>"
    title_el = f"<title>{title}</title>" if title else ""
    return f"<component><section>{code_el}{title_el}<text>{inner}</text>{sub}</section></component>"


def document(*sections, version=3):
    return f"""<?xml version="1.0"?>
<document xmlns="urn:hl7-org:v3">
  <setId root="AAAAAAAA-0000-0000-0000-000000000001"/>
  <versionNumber value="{version}"/>
  <effectiveTime value="20200102"/>
  <title>Test label</title>
  <component><structuredBody>{''.join(sections)}</structuredBody></component>
</document>""".encode()


class SentenceSplitTest(unittest.TestCase):
    def test_splits_on_sentence_ends(self):
        self.assertEqual(spl.split_sentences("One thing. Two things! Three?"), ["One thing.", "Two things!", "Three?"])

    def test_keeps_abbreviations_and_decimals(self):
        text = "Use e.g. water in the U.S. for 0.5 mg doses. Then stop."
        self.assertEqual(spl.split_sentences(text), ["Use e.g. water in the U.S. for 0.5 mg doses.", "Then stop."])

    def test_keeps_closing_bracket_with_sentence(self):
        self.assertEqual(spl.split_sentences("See below (Table 1.) Next one."), ["See below (Table 1.)", "Next one."])

    def test_does_not_split_on_initials(self):
        self.assertEqual(spl.split_sentences("Made by J. Smith Labs. Done."), ["Made by J. Smith Labs.", "Done."])


class ConvertTest(unittest.TestCase):
    def test_reads_identity(self):
        label = spl.convert(document(section("34067-9", "INDICATIONS", "<paragraph>Use it.</paragraph>")))
        self.assertEqual((label.setid, label.version, label.effective_time),
                         ("aaaaaaaa-0000-0000-0000-000000000001", 3, "20200102"))

    def test_old_warnings_and_precautions_map_to_43685_7_in_order(self):
        label = spl.convert(document(
            section("34071-1", "WARNINGS", "<paragraph>Warn one.</paragraph>"),
            section("42232-9", "PRECAUTIONS", "<paragraph>Take care.</paragraph>")))
        self.assertEqual(list(label.files), ["43685-7-warnings-and-precautions.md"])
        text = label.files["43685-7-warnings-and-precautions.md"]
        self.assertLess(text.index("# WARNINGS"), text.index("# PRECAUTIONS"))
        self.assertEqual(label.label_format, "old")

    def test_nested_boxed_warning_is_lifted_out(self):
        boxed = section("34066-1", "Boxed Warning", "<paragraph>Do not use for weight loss.</paragraph>")
        label = spl.convert(document(section("34071-1", "WARNINGS", "<paragraph>Other warning.</paragraph>", boxed)))
        self.assertIn("Do not use for weight loss.", label.files["34066-1-boxed-warning.md"])
        self.assertNotIn("weight loss", label.files["43685-7-warnings-and-precautions.md"])

    def test_repeated_and_uncoded_sections_join_and_product_data_is_skipped(self):
        label = spl.convert(document(
            section("48780-1", "", "<paragraph>PRODUCT KEYWORDS</paragraph>"),
            section("51945-4", "Panel 25 mcg", "<paragraph>Carton A.</paragraph>"),
            section("51945-4", "Panel 50 mcg", "<paragraph>Carton B.</paragraph>"),
            section("", "", "<paragraph>Made by Example Inc.</paragraph>"),
            section("43685-7", "5 WARNINGS AND PRECAUTIONS", "<paragraph>Modern.</paragraph>")))
        self.assertEqual(sorted(label.files), ["42229-5-unclassified.md", "43685-7-warnings-and-precautions.md",
                                               "51945-4-package-label.md"])
        pkg = label.files["51945-4-package-label.md"]
        self.assertLess(pkg.index("Carton A."), pkg.index("Carton B."))
        self.assertNotIn("PRODUCT KEYWORDS", "".join(label.files.values()))
        self.assertEqual(label.label_format, "PLR")

    def test_one_sentence_per_line_and_markup_spacing(self):
        inner = ('<paragraph>First sentence <content styleCode="bold">here</content>. '
                 '(see <linkHtml href="#x">CONTRAINDICATIONS</linkHtml> ) Second.</paragraph>')
        text = spl.convert(document(section("34070-3", "CONTRAINDICATIONS", inner))).files["34070-3-contraindications.md"]
        self.assertEqual(text, "# CONTRAINDICATIONS\n\nFirst sentence here.\n(see CONTRAINDICATIONS) Second.\n")

    def test_table_becomes_pipe_table_with_colspan(self):
        inner = ("<table><caption>Table 1 Doses</caption><thead><tr><th>Age</th><th>Dose</th><th>Note</th></tr></thead>"
                 "<tbody><tr><td>Adult</td><td colspan=\"2\">1.6 mcg/kg | daily</td></tr></tbody></table>")
        text = spl.convert(document(section("34068-7", "DOSAGE", inner))).files["34068-7-dosage-and-administration.md"]
        self.assertIn("Table 1 Doses\n\n| Age | Dose | Note |\n| --- | --- | --- |\n| Adult | 1.6 mcg/kg \\| daily |  |", text)

    def test_lists_and_nested_subsections(self):
        inner = ('<list listType="ordered"><item>Take it. With water.</item><item>Store cold.'
                 '<list><item>Below 25 C</item></list></item></list>')
        sub = section("", "2.1 Adults", "<paragraph>Adult dose.</paragraph>")
        text = spl.convert(document(section("34068-7", "2 DOSAGE", inner, sub))).files["34068-7-dosage-and-administration.md"]
        self.assertIn("1. Take it.\n   With water.\n2. Store cold.\n   - Below 25 C", text)
        self.assertIn("## 2.1 Adults\n\nAdult dose.", text)

    def test_joins_section_numbers_split_across_markup(self):
        sub = section("42229-5", '<content styleCode="bold">5.</content>\n  <content styleCode="bold">3</content> Myxedema', "")
        text = spl.convert(document(section("43685-7", "5 W&amp;P", "", sub))).files["43685-7-warnings-and-precautions.md"]
        self.assertIn("## 5.3 Myxedema", text)

    def test_pretty_print_whitespace_between_runs_is_dropped_but_spaces_kept(self):
        inner = ('<table><thead><tr><td><content styleCode="bold">Ag</content>\n      <content styleCode="bold">e</content>'
                 '</td><td>Dose</td></tr></thead><tbody><tr><td><content>Adult</content> <content>men</content></td>'
                 '<td>1</td></tr></tbody></table>')
        text = spl.convert(document(section("34068-7", "DOSAGE", inner))).files["34068-7-dosage-and-administration.md"]
        self.assertIn("| Age | Dose |", text)
        self.assertIn("| Adult men | 1 |", text)

    def test_rejects_non_spl(self):
        with self.assertRaises(spl.SplError):
            spl.convert(b"<html/>")
        with self.assertRaises(spl.SplError):
            spl.convert(b"not xml")

    def test_output_is_deterministic(self):
        doc = document(section("34067-9", "INDICATIONS", "<paragraph>Use it. Twice.</paragraph>"))
        self.assertEqual(spl.convert(doc).files, spl.convert(doc).files)


class CopyrightNoticeTest(unittest.TestCase):
    """Company copyright notices are copyright management information (17 U.S.C. 1202(b)): the converter must
    keep them character for character. Real examples from build/us are in known_notices.json."""

    NOTICES = ("© 2024 Viatris Inc. All rights reserved.",
               "Copyright © 2025 Biocon Biologics Inc. All rights reserved.",
               "©2023 GSK group of companies or its licensor.")

    def test_copyright_notices_preserved(self):
        paras = "".join(f"<paragraph>{n}</paragraph>" for n in self.NOTICES)
        table = (f"<table><tbody><tr><td>Carton</td><td>{self.NOTICES[0]}</td></tr></tbody></table>")
        files = spl.convert(document(section("34076-0", "17 PATIENT COUNSELING INFORMATION", paras),
                                     section("51945-4", "PACKAGE LABEL", table))).files
        counseling = next(t for n, t in files.items() if n.startswith("34076-0-"))
        package = next(t for n, t in files.items() if n.startswith("51945-4-"))
        for name, text, notices in (("34076-0", counseling, self.NOTICES), ("51945-4", package, self.NOTICES[:1])):
            joined = " ".join(text.split())       # line splitting is allowed; no character may be lost
            for notice in notices:
                with self.subTest(section=name, notice=notice):
                    self.assertIn(notice, joined)
            self.assertIn("©", text)
            self.assertNotIn("(c)", text.lower())
        self.assertIn("©2023 GSK", counseling)    # the whitespace rules only remove spaces, never insert one


if __name__ == "__main__":
    unittest.main()
