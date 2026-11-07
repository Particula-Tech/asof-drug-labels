"""EU annex converter (tools/eu_qrd.py).

The rule tests build visual lines directly and need only the standard library.
The PDF tests need PyMuPDF: they run under .venv-eu/bin/python and skip
elsewhere, except that ``VenvPdfTests`` re-runs them under .venv-eu/bin/python
when the current interpreter lacks PyMuPDF and the venv exists, so the plain
``cd tools && python3 -I -m unittest discover -s tests`` covers them too.
"""
import collections
import importlib.util
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
FIXTURES = Path(__file__).resolve().parent / "eu_fixtures"
VENV_PYTHON = ROOT / ".venv-eu" / "bin" / "python"
M0 = ROOT / "m0-data" / "eu"
sys.path.insert(0, str(TOOLS))
import eu_qrd  # noqa: E402
from eu_qrd import Line, Table  # noqa: E402

HAVE_PYMUPDF = importlib.util.find_spec("pymupdf") is not None

LEFT, FULL_RIGHT, PITCH = 70.0, 520.0, 12.6


def make_lines(spec: str, page_height: float = 842.0) -> list:
    """Visual lines from a small text layout language.

    One entry per line. A blank line is a paragraph gap, ``\\f`` (a line holding
    only ``---page---``) starts a new page. Prefixes: ``B:`` bold, ``I:``
    italic, ``*:`` bullet (``**:`` nested bullet), ``>:`` continuation of a bullet's
    text, ``@foot:`` a footer line at
    the page bottom, ``@head:`` a header line at the page top. A line ending in
    `` ~`` filled the column (it wrapped); other lines end where their text
    ends (5 points per character)."""
    items, page, y = [], 0, 57.0
    for raw in spec.strip("\n").split("\n"):
        if raw.strip() == "---page---":
            page, y = page + 1, 57.0
            continue
        if not raw.strip():
            y += PITCH
            continue
        if raw.startswith("@foot:"):
            text = raw[6:]
            items.append(Line(page, 290, 796, 300, 806, text, page_height))
            continue
        if raw.startswith("@head:"):
            text = raw[6:]
            items.append(Line(page, LEFT, 30, LEFT + 5 * len(text), 40, text, page_height))
            continue
        bold = italic = 0.0
        bullet, marker_x, x0 = False, 0.0, LEFT
        while True:
            if raw.startswith("B:"):
                bold, raw = 1.0, raw[2:]
            elif raw.startswith("I:"):
                italic, raw = 1.0, raw[2:]
            elif raw.startswith("**:"):
                bullet, marker_x, x0, raw = True, LEFT + 36, LEFT + 54, raw[3:]
            elif raw.startswith("*:"):
                bullet, marker_x, x0, raw = True, LEFT + 18, LEFT + 36, raw[2:]
            elif raw.startswith(">:"):
                x0, raw = LEFT + 36, raw[2:]
            else:
                break
        full = raw.endswith(" ~")
        text = raw[:-2] if full else raw
        x1 = FULL_RIGHT if full else x0 + 5 * len(text)
        items.append(Line(page, x0, y, x1, y + 12.2, text, page_height, bold=bold, italic=italic,
                          bullet=bullet, marker_x=marker_x))
        y += PITCH
    return items


def smpc(name: str, posology: list[str], contra: str = "Hypersensitivity to the active substance.") -> str:
    body = "\n".join(posology)
    return f"""
B:1. NAME OF THE MEDICINAL PRODUCT

{name}

B:2. QUALITATIVE AND QUANTITATIVE COMPOSITION

Each tablet contains the active substance.

B:3. PHARMACEUTICAL FORM

Tablet.

B:4. CLINICAL PARTICULARS

B:4.1 Therapeutic indications

Treatment of a disease.

B:4.2 Posology and method of administration

{body}

B:4.3 Contraindications

{contra}
"""


def convert_text(spec: str) -> dict:
    if "ANNEX I\n" in spec and "ANNEX II" not in spec and "ANNEX III" not in spec:
        spec += "\n---page---\nANNEX II\n"
    return eu_qrd.convert_items(make_lines(spec))


class HeadingTest(unittest.TestCase):
    def heading(self, text):
        found = eu_qrd.match_heading(text)
        return found[0].slug if found else None

    def test_template_variants_share_one_slug(self):
        cases = {
            "4.6 Pregnancy and lactation": "smpc-4.6-fertility-pregnancy-and-lactation",
            "4.6 Fertility, pregnancy and lactation": "smpc-4.6-fertility-pregnancy-and-lactation",
            "4.6. Use during pregnancy and lactation": "smpc-4.6-fertility-pregnancy-and-lactation",
            "4.4 Special warnings and special precautions for use": "smpc-4.4-special-warnings-and-precautions-for-use",
            "4.4 Special warnings and precautions for use": "smpc-4.4-special-warnings-and-precautions-for-use",
            "6.6 Instructions for use and handling, and disposal":
                "smpc-6.6-special-precautions-for-disposal-and-other-handling",
            "6.6 Special precautions for disposal": "smpc-6.6-special-precautions-for-disposal-and-other-handling",
            "6.6 Special precautions for disposal and other handling":
                "smpc-6.6-special-precautions-for-disposal-and-other-handling",
            "6.5 Nature and contents of container <and special equipment for use, administration or implantation>":
                "smpc-6.5-nature-and-contents-of-container",
            "8. MARKETING AUTHORISATION NUMBER(S)": "smpc-8-marketing-authorisation-numbers",
            "9. DATE OF FIRST AUTHORISATION/RENEWAL OF THE AUTHORISATION":
                "smpc-9-date-of-first-authorisation-renewal-of-the-authorisation",
            "9. DATE OF FIRST AUTHORISATION / RENEWAL OF AUTHORISATION":
                "smpc-9-date-of-first-authorisation-renewal-of-the-authorisation",
            "10. DATE OF REVISION OF THE TEXT": "smpc-10-date-of-revision-of-the-text",
            "1. NAME OF THE MEDICINAL PRODUCT": "smpc-1-name-of-the-medicinal-product",
            "1.NAME OF THE MEDICINAL PRODUCT": None,  # QRD always has a space or tab after the number
            "4.3 Contra-indications": "smpc-4.3-contraindications",
            "4.8 Undesirable effects": "smpc-4.8-undesirable-effects",
            "4.7 Effects on the ability to drive and use machines":
                "smpc-4.7-effects-on-ability-to-drive-and-use-machines",
        }
        for text, slug in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.heading(text), slug)

    def test_body_text_is_not_a_heading(self):
        for text in ["4.4 and 5.2).", "4.3 mg twice daily", "11 patients discontinued", "2.5 mg film-coated tablets",
                     "6.1 List of side effects is below", "5.2 Pharmacology studies were short"]:
            with self.subTest(text=text):
                self.assertIsNone(self.heading(text))

    def test_printed_heading_kept_with_normalised_number(self):
        out = convert_text("ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet."]).replace(
            "B:4.3 Contraindications", "B:4.3. Contraindications"))
        self.assertTrue(out["sections"]["smpc-4.3-contraindications"].startswith("# 4.3 Contraindications\n"))
        self.assertTrue(out["sections"]["smpc-1-name-of-the-medicinal-product"].startswith(
            "# 1. NAME OF THE MEDICINAL PRODUCT\n"))

    def test_unnumbered_heading_for_the_next_section(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet."]).replace(
            "B:4.3 Contraindications", "B:Contraindications")
        out = convert_text(spec)
        self.assertEqual(out["sections"]["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity to the active substance.\n")
        self.assertIn("unnumbered-heading: SmPC 1 section 4.3", " ".join(out["warnings"]))

    def test_unnumbered_title_is_body_text_when_not_bold_or_not_next(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet.", "", "Contraindications", "",
                                                         "B:Overdose", "", "None known."])
        out = convert_text(spec)
        text = out["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertIn("\n\nContraindications\n", text)   # plain: not a heading
        self.assertIn("\n\nOverdose\n", text)            # bold, but 4.9 is not the next section after 4.2
        self.assertNotIn("unnumbered-heading", " ".join(out["warnings"]))

    def test_heading_glued_to_the_previous_line_is_split(self):
        items = make_lines("ANNEX I\n" + smpc("Drug 5 mg tablets", [
            "Avoid strong inhibitors such as azole antifungals, ritonavir and some macrolides ~",
            "(clarithromycin).4.3 Contraindications",
        ], contra="Hypersensitivity.").replace("B:4.3 Contraindications\n", ""))
        glued = next(it for it in items if it.text.startswith("(clarithromycin)"))
        glued.bold = 0.5
        out = eu_qrd.convert_items(items)
        self.assertTrue(out["sections"]["smpc-4.2-posology-and-method-of-administration"].endswith(
            "some macrolides (clarithromycin).\n"))
        self.assertEqual(out["sections"]["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity.\n")
        self.assertIn("heading-inside-line", " ".join(out["warnings"]))

    def test_older_template_headings(self):
        for text, slug in {
            "1. Name of Proprietary Medicinal Product": "smpc-1-name-of-the-medicinal-product",
            "4.1.1 Therapeutic indications": "smpc-4.1-therapeutic-indications",
            "4.6 Use in Pregnancy and Lactation": "smpc-4.6-fertility-pregnancy-and-lactation",
            "8. NUMBERS IN THE COMMUNITY REGISTER OF MEDICINAL PRODUCTS": "smpc-8-marketing-authorisation-numbers",
            "6.6 Instructions for handling of dosing device":
                "smpc-6.6-special-precautions-for-disposal-and-other-handling",
            "7. MARKETING A UTHORISATION HOLDER": "smpc-7-marketing-authorisation-holder",
            "4.1.2 Further information on clinical trials": None,
            "4.8 Undesirable effects) compared with adults, temporary dose reduction": None,
        }.items():
            with self.subTest(text=text):
                found = eu_qrd.match_heading(text)
                self.assertEqual(found[0].slug if found else None, slug)

    def test_auto_number_prefix_is_dropped(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet."]).replace(
            "B:4.3 Contraindications", "B:1.3.\nB:1.3.1. 4.3 Contraindications")
        out = convert_text(spec)
        self.assertEqual(out["sections"]["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity to the active substance.\n")
        self.assertIn("auto-number-prefix: '1.3.1.'", " ".join(out["warnings"]))

    def test_kerning_space_in_heading_is_repaired(self):
        self.assertEqual(eu_qrd.match_heading("8. MARKETING A UTHORISATION NUMBER")[1], "MARKETING AUTHORISATION NUMBER")
        self.assertEqual(eu_qrd.match_heading("4.3 Contraindications")[1], "Contraindications")

    def test_plain_heading_candidate_is_body_text_when_headings_are_bold(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["Reduce the dose in children (see section", "",
                                                         "4.8 Undesirable effects for details.", "", "One tablet."])
        out = convert_text(spec)
        self.assertIn("4.8 Undesirable effects for details.",
                      out["sections"]["smpc-4.2-posology-and-method-of-administration"])
        self.assertNotIn("smpc-4.8-undesirable-effects", out["sections"])
        self.assertEqual(out["warnings"], [])

    def test_heading_out_of_order_stays_body_text(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet.", "", "4.2 Posology and method of use"])
        out = convert_text(spec)
        self.assertIn("4.2 Posology and method of use", out["sections"]["smpc-4.2-posology-and-method-of-administration"])


class CanonicalSlugTest(unittest.TestCase):
    def test_slugs_are_fixed(self):
        expected = [
            "smpc-1-name-of-the-medicinal-product", "smpc-2-qualitative-and-quantitative-composition",
            "smpc-3-pharmaceutical-form", "smpc-4-clinical-particulars", "smpc-4.1-therapeutic-indications",
            "smpc-4.2-posology-and-method-of-administration", "smpc-4.3-contraindications",
            "smpc-4.4-special-warnings-and-precautions-for-use",
            "smpc-4.5-interaction-with-other-medicinal-products-and-other-forms-of-interaction",
            "smpc-4.6-fertility-pregnancy-and-lactation", "smpc-4.7-effects-on-ability-to-drive-and-use-machines",
            "smpc-4.8-undesirable-effects", "smpc-4.9-overdose", "smpc-5-pharmacological-properties",
            "smpc-5.1-pharmacodynamic-properties", "smpc-5.2-pharmacokinetic-properties",
            "smpc-5.3-preclinical-safety-data", "smpc-6-pharmaceutical-particulars", "smpc-6.1-list-of-excipients",
            "smpc-6.2-incompatibilities", "smpc-6.3-shelf-life", "smpc-6.4-special-precautions-for-storage",
            "smpc-6.5-nature-and-contents-of-container", "smpc-6.6-special-precautions-for-disposal-and-other-handling",
            "smpc-7-marketing-authorisation-holder", "smpc-8-marketing-authorisation-numbers",
            "smpc-9-date-of-first-authorisation-renewal-of-the-authorisation", "smpc-10-date-of-revision-of-the-text",
            "smpc-11-dosimetry", "smpc-12-instructions-for-preparation-of-radiopharmaceuticals",
        ]
        self.assertEqual([s.slug for s in eu_qrd.SECTIONS], expected)
        self.assertEqual(eu_qrd.ADDITIONAL_MONITORING, "smpc-0-additional-monitoring")


class AnnexBoundsTest(unittest.TestCase):
    ANNEX_III = """
ANNEX III

B:1. NAME OF THE MEDICINAL PRODUCT

Drug 5 mg film-coated tablets
"""

    def test_stops_before_annex_ii(self):
        spec = ("SUMMARY OF PRODUCT CHARACTERISTICS\nANNEX I\nSUMMARY OF PRODUCT CHARACTERISTICS\n"
                + smpc("Drug 5 mg tablets", ["Take one tablet."])
                + "\nANNEX II\n\nB:A. MANUFACTURER RESPONSIBLE FOR BATCH RELEASE\n" + self.ANNEX_III)
        out = convert_text(spec)
        self.assertEqual(len(out["smpcs"]), 1)
        self.assertEqual(out["warnings"], [])
        everything = "\n".join(out["sections"].values())
        self.assertNotIn("MANUFACTURER RESPONSIBLE", everything)
        self.assertNotIn("SUMMARY OF PRODUCT CHARACTERISTICS", everything)
        self.assertEqual(out["sections"]["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity to the active substance.\n")

    def test_annex_iii_ends_annex_i_when_annex_ii_is_missing(self):
        out = convert_text("ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet."]) + self.ANNEX_III)
        self.assertEqual(len(out["smpcs"]), 1)

    def test_footnoted_and_repeated_annex_i_titles(self):
        spec = ("ANNEX I\n\n\nANNEX I*\nSUMMARIES OF PRODUCT CHARACTERISTICS\n"
                + smpc("Drug 5 mg tablets", ["Take one tablet."]) + "\n---page---\nANNEX II*\nB:A. MANUFACTURER\n")
        out = convert_text(spec)
        self.assertEqual(out["status"], "converted")
        self.assertEqual(out["warnings"], [])
        self.assertNotIn("smpc-0-front-matter", out["sections"])
        self.assertNotIn("MANUFACTURER", "\n".join(out["sections"].values()))

    def test_empty_annex_i(self):
        out = convert_text("ANNEX I\nSUMMARY OF PRODUCT CHARACTERISTICS\n\n\nANNEX II\n\nB:A. MANUFACTURER\n"
                           "Some text.\n\nANNEX III\nLABELLING AND PACKAGE LEAFLET")
        self.assertEqual(out["status"], "empty-annex-i")
        self.assertEqual(out["sections"], {})
        self.assertTrue(out["warnings"][0].startswith("empty-annex-i"))

    def test_document_with_only_later_annexes(self):
        out = convert_text("ANNEX\n\n\nANNEX II\n\nB:A. MANUFACTURER\nSome text.")
        self.assertEqual(out["status"], "no-annex-i")
        self.assertEqual(out["warnings"], ["no-annex-i: no 'ANNEX I' line and no SmPC section 1 before any later "
                                           "annex (the document starts with 'ANNEX II')"])

    def test_labelling_section_1_does_not_start_annex_i(self):
        out = convert_text("ANNEX III\nLABELLING AND PACKAGE LEAFLET\n\nB:A. LABELLING\n\n"
                           "B:1. NAME OF THE MEDICINAL PRODUCT\n\nDrug 5 mg tablets\n\n"
                           "B:2. STATEMENT OF ACTIVE SUBSTANCE\n\nEach tablet contains 5 mg.")
        self.assertEqual(out["status"], "no-annex-i")
        self.assertEqual(out["sections"], {})

    def test_annex_i_without_an_smpc(self):
        out = convert_text("ANNEX I\n\nSCIENTIFIC CONCLUSIONS AND GROUNDS FOR THE RECOMMENDATION\n\n"
                           "The Agency was made aware of a problem.\n\n---page---\nANNEX II\n")
        self.assertEqual(out["status"], "no-smpc")
        self.assertEqual(out["sections"], {})
        self.assertIn("which starts with 'SCIENTIFIC CONCLUSIONS", out["warnings"][0])

    def test_missing_markers_are_reported(self):
        out = eu_qrd.convert_items(make_lines(smpc("Drug 5 mg tablets", ["Take one tablet."])))
        self.assertIn("annex-i-marker-missing", " ".join(out["warnings"]))
        self.assertIn("annex-ii-marker-missing", " ".join(out["warnings"]))
        self.assertEqual(len(out["smpcs"]), 1)
        out = convert_text("Some unrelated document\n\nWith text.")
        self.assertEqual(out["sections"], {})
        self.assertTrue(out["warnings"][0].startswith("no-annex-i"))


class MarginTest(unittest.TestCase):
    def test_page_numbers_and_running_headers_dropped(self):
        pages = []
        for n in range(4):
            pages.append(f"@head:Medicinal product no longer authorised\nBody line {n} mentions 12 patients.\n"
                         f"@foot:{n + 2}")
        items = make_lines("\n---page---\n".join(pages))
        kept = eu_qrd.remove_margins(items)
        self.assertEqual([l.text for l in kept], [f"Body line {n} mentions 12 patients." for n in range(4)])

    def test_single_margin_line_kept_unless_page_number(self):
        items = make_lines("@head:Only once at the top\nBody.\n@foot:Page 3 of 9")
        self.assertEqual([l.text for l in eu_qrd.remove_margins(items)], ["Only once at the top", "Body."])

    def test_page_break_inside_paragraph_joins(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", [
            "The recommended dose is one tablet taken orally twice daily, with or without food and ~",
            "@foot:2", "---page---", "@head:Running header", "water.",
        ]) + "\n---page---\n@head:Running header\nmore\n---page---\n@head:Running header\nend"
        out = convert_text(spec)
        text = out["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertIn("twice daily, with or without food and water.\n", text)
        self.assertNotIn("Running header", text)


class CopyrightNoticeTest(unittest.TestCase):
    """A copyright notice in the body text survives conversion character for character (eu_qrd normalises
    text only for matching, never the output). A notice printed only in a running page footer does not: page
    furniture is stripped (reproduce.md, 'Converter')."""

    NOTICE = "© 2024 Example Holder Ltd. All rights reserved."
    DIARY = "Patients completed the Psoriasis Symptom Diary©."

    def test_copyright_notices_preserved(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["One tablet daily."]) + f"""
B:5. PHARMACOLOGICAL PROPERTIES

B:5.1 Pharmacodynamic properties

{self.DIARY}
@foot:© 2024 Example Holder Ltd.
---page---
B:10. DATE OF REVISION OF THE TEXT

{self.NOTICE}
@foot:© 2024 Example Holder Ltd.
---page---
More text.
@foot:© 2024 Example Holder Ltd.
"""
        sections = convert_text(spec)["sections"]
        pd = sections["smpc-5.1-pharmacodynamic-properties"]
        revision = sections["smpc-10-date-of-revision-of-the-text"]
        self.assertIn(self.DIARY, pd)
        self.assertIn(self.NOTICE, " ".join(revision.split()))
        for text in (pd, revision):
            self.assertNotIn("(c)", text.lower())
        # Known limitation: the footer line repeats on every page, so it is removed as page furniture; only the
        # body-text notice in section 10 remains (one occurrence of the holder name, not three).
        self.assertEqual(" ".join(sections.values()).count("Example Holder Ltd."), 1)
        self.assertNotIn("© 2024 Example Holder Ltd.\n", pd)


class JoinTest(unittest.TestCase):
    def test_hyphenation(self):
        vocab = ({"treatment", "line"}, {"first-line"})
        self.assertEqual(eu_qrd._join("start of treat-", "ment here", vocab), "start of treatment here")
        self.assertEqual(eu_qrd._join("for first-", "line use", vocab), "for first-line use")
        self.assertEqual(eu_qrd._join("non-", "small cell", vocab), "non-small cell")
        self.assertEqual(eu_qrd._join("the Kaplan-", "Meier curve", vocab), "the Kaplan-Meier curve")
        self.assertEqual(eu_qrd._join("haemo­", "globin", vocab), "haemoglobin")
        self.assertEqual(eu_qrd._join("ends here.", "Next", vocab), "ends here. Next")
        self.assertEqual(eu_qrd._join("range 5 -", "10 mg", vocab), "range 5 - 10 mg")

    def test_vocabulary_decides_hyphen_from_the_annex(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", [
            "Treatment should continue. The posttreatment visit and the first-line setting matter here, and post- ~",
            "treatment follow-up and first- ~",
            "line therapy apply.",
        ])
        text = convert_text(spec)["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertIn("and posttreatment follow-up and first-line therapy apply.", text)


class SentenceAndParagraphTest(unittest.TestCase):
    def test_one_sentence_per_line_with_spl_rules(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", [
            "Inducers (e.g. rifampicin or St. John's Wort) reduce exposure by approx. 50%. Avoid them. A dose of ~",
            "2.5 mg is used. Ask Dr. Smith.",
        ])
        text = convert_text(spec)["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertEqual(text.split("\n\n", 1)[1],
                         "Inducers (e.g. rifampicin or St. John's Wort) reduce exposure by approx. 50%.\n"
                         "Avoid them.\nA dose of 2.5 mg is used.\nAsk Dr. Smith.\n")

    def test_short_line_is_a_line_break_and_gap_is_a_paragraph(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", [
            "I:Renal impairment",
            "No dose adjustment is necessary in patients with mild or moderate renal impairment, and the ~",
            "dose is the same.",
            "",
            "Posology",
            "One tablet.",
        ])
        text = convert_text(spec)["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertEqual(text, "# 4.2 Posology and method of administration\n\nRenal impairment\n"
                               "No dose adjustment is necessary in patients with mild or moderate renal impairment, "
                               "and the dose is the same.\n\nPosology\nOne tablet.\n")

    def test_bullets_keep_markers_and_nesting(self):
        spec = "ANNEX I\n" + smpc("Drug 5 mg tablets", ["Take one tablet."], contra="\n".join([
            "*:Hypersensitivity to the active substance or to any of the excipients listed in section ~",
            ">:6.1. Also to related substances.",
            "*:Active bleeding, such as:",
            "**:gastrointestinal ulcer",
            "*:Hepatic disease.",
        ]))
        text = convert_text(spec)["sections"]["smpc-4.3-contraindications"]
        self.assertEqual(text, "# 4.3 Contraindications\n\n"
                               "- Hypersensitivity to the active substance or to any of the excipients listed in "
                               "section 6.1.\n  Also to related substances.\n"
                               "- Active bleeding, such as:\n  - gastrointestinal ulcer\n- Hepatic disease.\n")

    def test_tabular_and_verbatim_lines_are_line_text(self):
        items = make_lines("ANNEX I\n" + smpc("Drug 5 mg tablets", ["Common  Headache", "Rare  Rash"]))
        for it in items:
            if it.text in ("Common  Headache", "Rare  Rash"):
                it.tabular = True
        text = eu_qrd.convert_items(items)["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertIn("\n\nCommon Headache\nRare Rash\n", text)


class TableTest(unittest.TestCase):
    def test_pipe_table_and_page_continuation(self):
        items = make_lines("ANNEX I\n" + smpc("Drug 5 mg tablets", ["Table 1: Adverse reactions"]))
        idx = next(i for i, it in enumerate(items) if it.text == "Table 1: Adverse reactions") + 1
        header = ["Frequency", "Reaction"]
        first = Table(0, 70, 600, 520, 790, [header, ["Common", "Haemorrhage (including vaginal and"]])
        second = Table(1, 70, 57, 520, 120, [header, ["", "urethral haemorrhage)"], ["Rare", "Rash | itch"]])
        after = Line(1, 70, 140, 300, 152, "Bleeding may occur.")
        items[idx:idx] = [first, second, after]
        text = eu_qrd.convert_items(items)["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertIn("Table 1: Adverse reactions\n\n| Frequency | Reaction |\n| --- | --- |\n"
                      "| Common | Haemorrhage (including vaginal and urethral haemorrhage) |\n"
                      "| Rare | Rash \\| itch |\n\nBleeding may occur.\n", text)


    def test_ruled_frame_with_a_qrd_heading_is_not_a_table(self):
        heading = Line(0, LEFT, 100, 300, 112, "4.3 Contraindications", bold=1.0)
        body = Line(0, LEFT, 120, FULL_RIGHT, 132, "Hypersensitivity to the active substance.")
        self.assertTrue(eu_qrd.is_frame([body, heading]))
        self.assertFalse(eu_qrd.is_frame([body, Line(0, LEFT, 100, 300, 112, "4.3 Contraindications")]))
        self.assertFalse(eu_qrd.is_frame([Line(0, LEFT, 100, 300, 112, "Organ class", bold=1.0), body]))

    def test_table_with_an_empty_column_is_ambiguous(self):
        self.assertTrue(eu_qrd.has_empty_column([["Organ class", "Common", "", "Rare"],
                                                 ["Cardiac", "Arrhythmia", "", "Cardiac failure"]]))
        self.assertTrue(eu_qrd.has_empty_column([["A", "B", ""], ["x", "y"]]))
        self.assertFalse(eu_qrd.has_empty_column([["Organ class", "Common", "Rare"],
                                                  ["Cardiac", "", "Cardiac failure"], ["Skin", "Rash", ""]]))


class MultiSmpcTest(unittest.TestCase):
    def test_later_smpcs_add_only_new_sentences(self):
        spec = ("ANNEX I\n"
                + smpc("Drug 2.5 mg film-coated tablets", ["The dose is 2.5 mg twice daily. Take with water."])
                + "\n---page---\n"
                + smpc("Drug 5 mg film-coated tablets",
                       ["The dose is 5 mg twice daily.  Take with  water.", "", "Do not crush the tablet."])
                + "\n---page---\n"
                + smpc("Drug 10 mg film-coated tablets", ["The dose is 5 MG twice daily.  Take with water."]))
        out = convert_text(spec)
        self.assertEqual([s["name"] for s in out["smpcs"]], [
            "Drug 2.5 mg film-coated tablets", "Drug 5 mg film-coated tablets", "Drug 10 mg film-coated tablets"])
        sec = out["sections"]
        self.assertEqual(sec["smpc-4.2-posology-and-method-of-administration"],
                         "# 4.2 Posology and method of administration\n\n"
                         "The dose is 2.5 mg twice daily.\nTake with water.\n\n"
                         "[Drug 5 mg film-coated tablets]\n\nThe dose is 5 mg twice daily.\n\n"
                         "Do not crush the tablet.\n\n"
                         "[Drug 10 mg film-coated tablets]\n\n(all sentences already above)\n")
        # Identical in every SmPC: no product marker at all.
        self.assertEqual(sec["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity to the active substance.\n")
        self.assertEqual(sec["smpc-1-name-of-the-medicinal-product"],
                         "# 1. NAME OF THE MEDICINAL PRODUCT\n\nDrug 2.5 mg film-coated tablets\n\n"
                         "[Drug 5 mg film-coated tablets]\n\nDrug 5 mg film-coated tablets\n\n"
                         "[Drug 10 mg film-coated tablets]\n\nDrug 10 mg film-coated tablets\n")
        self.assertIn("multi-smpc: 3 SmPCs merged", " ".join(out["warnings"]))


class AdditionalMonitoringTest(unittest.TestCase):
    def test_statement_gets_its_own_file(self):
        statement = ("▼This medicinal product is subject to additional monitoring. This will allow quick "
                     "identification of new safety information. Healthcare professionals are asked to report ~\n"
                     "any suspected adverse reactions. See section 4.8 for how to report adverse reactions.")
        spec = "ANNEX I\n\n" + statement + "\n" + smpc("Drug 5 mg tablets", ["Take one tablet."])
        out = convert_text(spec)
        self.assertEqual(out["sections"]["smpc-0-additional-monitoring"],
                         "# 0 Additional monitoring\n\n"
                         "This medicinal product is subject to additional monitoring.\n"
                         "This will allow quick identification of new safety information.\n"
                         "Healthcare professionals are asked to report any suspected adverse reactions.\n"
                         "See section 4.8 for how to report adverse reactions.\n")
        self.assertNotIn("smpc-0-front-matter", out["sections"])
        self.assertEqual(out["warnings"], [])
        self.assertEqual(list(out["sections"])[0], "smpc-0-additional-monitoring")

    def test_other_front_matter_is_kept_with_a_warning(self):
        out = convert_text("ANNEX I\n\nAn unexpected note.\n" + smpc("Drug 5 mg tablets", ["Take one tablet."]))
        self.assertEqual(out["sections"]["smpc-0-front-matter"], "# 0 Front matter\n\nAn unexpected note.\n")
        self.assertIn("front-matter", out["warnings"][0])


class Converter4RulesTest(unittest.TestCase):
    """Rules added in converter 4 (review of the converter 3 build); standard library only."""

    def test_line_end_hyphen_after_a_letter_or_digit_joins_without_a_space(self):
        vocab = ({"start"}, set(), set())
        self.assertEqual(eu_qrd._join("in KEYNOTE-", "811 patients", vocab), "in KEYNOTE-811 patients")
        self.assertEqual(eu_qrd._join("(range: 0.25-", "20.6)", vocab), "(range: 0.25-20.6)")
        self.assertEqual(eu_qrd._join("was a 2-", "year study", vocab), "was a 2-year study")
        self.assertEqual(eu_qrd._join("interferon beta-", "1a", vocab), "interferon beta-1a")
        self.assertEqual(eu_qrd._join("range 5 -", "10 mg", vocab), "range 5 - 10 mg")
        # The annex prints "HER2- advanced" inside a line elsewhere: the space is kept.
        self.assertEqual(eu_qrd._join("HR+, HER2-", "advanced", ({"advanced"}, set(), {"her2-"})),
                         "HR+, HER2- advanced")

    def test_vocabulary_collects_hyphen_final_words_inside_lines(self):
        vocab = eu_qrd._vocabulary([Line(0, LEFT, 0, FULL_RIGHT, 12, "with HR+/HER2- advanced cancer and a 2-"),
                                    Line(0, LEFT, 13, 300, 25, "year study")])
        self.assertIn("hr+/her2-", vocab[2])
        self.assertNotIn("2-", vocab[2])

    def test_no_sentence_or_line_break_after_an_abbreviation_or_number_hyphen(self):
        self.assertEqual(eu_qrd.split_eu_sentences(
            "This combination (i.e. EMEND, ondansetron and dexamethasone) decreased the AUC. Next one."),
            ["This combination (i.e. EMEND, ondansetron and dexamethasone) decreased the AUC.", "Next one."])
        self.assertEqual(eu_qrd.split_eu_sentences("Use it (e.g. Paracetamol) daily. Then stop."),
                         ["Use it (e.g. Paracetamol) daily.", "Then stop."])
        text = convert_text("ANNEX I\n" + smpc("Drug 5 mg tablets", [
            "Randomisation was stratified by histology (squamous vs.", "non-squamous) and region.", "",
            "The total body and renal clearance are 157-", "176 and 3-3.5 ml/min, respectively."]))
        sec = text["sections"]["smpc-4.2-posology-and-method-of-administration"]
        self.assertIn("Randomisation was stratified by histology (squamous vs. non-squamous) and region.\n", sec)
        self.assertIn("The total body and renal clearance are 157-176 and 3-3.5 ml/min, respectively.\n", sec)

    def test_sentence_key_ignores_final_period_and_spaces_around_hyphens(self):
        self.assertEqual(eu_qrd._sentence_key("Not all pack sizes may be marketed"),
                         eu_qrd._sentence_key("Not all pack sizes may be marketed."))
        self.assertEqual(eu_qrd._sentence_key("The clearance is 157 - 176 and 3 - 3.5 ml/min."),
                         eu_qrd._sentence_key("The clearance is 157-176 and 3-3.5 ml/min."))
        self.assertNotEqual(eu_qrd._sentence_key("The dose is 5 mg."), eu_qrd._sentence_key("The dose is 10 mg."))

    def test_duplicate_smpc_names_are_numbered_and_every_later_smpc_is_accounted_for(self):
        spec = ("ANNEX I\n" + smpc("Insulin 100 IU/ml cartridge", ["Pack of 5 cartridges."])
                + "\n---page---\n" + smpc("Insulin 100 IU/ml cartridge", ["Pack of 10 cartridges."])
                + "\n---page---\n" + smpc("Insulin 100 IU/ml pen", ["Pack of 5 cartridges."]))
        out = convert_text(spec)
        self.assertEqual([s["name"] for s in out["smpcs"]], [
            "Insulin 100 IU/ml cartridge", "Insulin 100 IU/ml cartridge (SmPC 2)", "Insulin 100 IU/ml pen"])
        self.assertEqual(out["sections"]["smpc-4.2-posology-and-method-of-administration"],
                         "# 4.2 Posology and method of administration\n\nPack of 5 cartridges.\n\n"
                         "[Insulin 100 IU/ml cartridge (SmPC 2)]\n\nPack of 10 cartridges.\n\n"
                         "[Insulin 100 IU/ml pen]\n\n(all sentences already above)\n")
        self.assertEqual(out["sections"]["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity to the active substance.\n")

    def test_order_chars_puts_glyphs_in_visual_order(self):
        # Lyrica 2015 CLcr formula: the content stream has ".1" then "23"; the page shows "1.23".
        Char = eu_qrd.Char
        stream = [Char(179.9, 315.6, 182.4, 326.7, "."), Char(174.9, 315.6, 179.9, 326.7, "1"),
                  Char(182.4, 315.6, 187.4, 326.7, "2"), Char(187.4, 315.6, 192.5, 326.7, "3")]
        self.assertEqual("".join(c.c for c in eu_qrd.order_chars(stream)), "1.23")
        bold_twice = [Char(10, 0, 15, 10, "A"), Char(10.2, 0, 15.2, 10, "A"), Char(15, 0, 20, 10, "B")]
        self.assertEqual("".join(c.c for c in eu_qrd.order_chars(bold_twice)), "AB")

    def test_subscripts_stay_on_their_line_in_table_cells(self):
        # Prezista 2018 p20: "telaprevir C" + subscript "min" + " ↓ 32%", then the next line.
        Char = eu_qrd.Char

        def word(x, y0, y1, text, size=10.0, seg=0, w=3.95):
            return [Char(x + i * w, y0, x + (i + 1) * w, y1, ch, size, seg=seg) for i, ch in enumerate(text)]

        cell = (word(198.2, 602.6, 613.6, "telaprevir C", seg=1) + word(245.8, 607.3, 614.4, "min", 6.5, 2, 3.3)
                + word(255.7, 602.6, 615.2, " ↓ 32%", seg=3) + word(198.2, 614.1, 625.2, "telaprevir C", seg=4)
                + word(245.8, 618.8, 626.0, "max", 6.5, 5, 3.6) + word(256.8, 614.1, 626.7, " ↓ 36%", seg=6))
        lines = eu_qrd.char_lines(cell)
        self.assertEqual([t for _, _, t in lines], ["telaprevir Cmin ↓ 32%", "telaprevir Cmax ↓ 36%"])
        # A gap between two PDF text lines is a space; inside one text line the PDF's own spaces count.
        self.assertEqual(eu_qrd.chars_text(word(0, 0, 10, "Median", seg=1) + word(33, 0, 10, "PFS", seg=2), 10),
                         "Median PFS")
        self.assertEqual(eu_qrd.chars_text(word(0, 0, 10, "≥") + word(7.8, 0, 10, "15"), 10), "≥15")

    def test_stacked_frequencies_in_one_ruled_cell_become_sub_rows(self):
        # Humira 2012 p12: "Ear and labyrinth disorders" | "Common" / "Uncommon" | "vertigo" / "deafness, tinnitus".
        cells = [[(601.1, 613.2, "Ear and labyrinth"), (613.8, 626.0, "disorders")],
                 [(601.1, 613.2, "Common"), (630.0, 642.2, "Uncommon")],
                 [(601.1, 613.2, "vertigo"), (630.0, 642.2, "deafness,"), (642.7, 654.9, "tinnitus")]]
        self.assertEqual(eu_qrd.split_row(cells), [["Ear and labyrinth disorders", "Common", "vertigo"],
                                                   ["", "Uncommon", "deafness, tinnitus"]])
        # One blank band in one cell only: no split.
        cells = [[(0, 12, "Psychiatric disorders")], [(0, 12, "Common")],
                 [(0, 12, "mood alterations,"), (12.5, 24.5, "anxiety"), (40, 52, "insomnia")]]
        self.assertEqual(eu_qrd.split_row(cells), [["Psychiatric disorders", "Common", "mood alterations, anxiety insomnia"]])
        # A header whose wrapped cell runs across the band of its neighbours is not stacked.
        cells = [[(0, 12, "Parameter")], [(0, 12, "Herceptin1"), (30, 42, "N=172")],
                 [(0, 12, "Herceptin plus"), (13, 25, "paclitaxel2"), (26, 38, "N=68")],
                 [(0, 12, "Paclitaxel2"), (30, 42, "N=77")]]
        self.assertEqual(eu_qrd.split_row(cells), [["Parameter", "Herceptin1 N=172", "Herceptin plus paclitaxel2 N=68",
                                                    "Paclitaxel2 N=77"]])

    def test_sparse_adr_table_with_a_full_header_is_clear(self):
        # Xeljanz 2022 p16: Rare and Very rare are empty in every data row.
        rows = [["System organ class", "Common", "Uncommon", "Rare", "Very rare", "Not known"],
                ["Cardiac disorders", "", "Myocardial infarction", "", "", ""],
                ["Skin and subcutaneous tissue disorders", "Rash", "Erythema Pruritus", "", "", ""]]
        self.assertTrue(eu_qrd.table_is_clear(rows))
        self.assertFalse(eu_qrd.table_is_clear([["", "Common", "Uncommon", "Rare"], ["Cardiac", "", "x", ""],
                                                ["Skin", "", "", "y"], ["Eye", "", "", ""]]))  # header gap: unclear
        self.assertFalse(eu_qrd.table_is_clear([["A", "B", ""], ["x", "y", ""]]))  # a column empty throughout

    def test_rule_columns_from_white_strips(self):
        spans = [(93, 160), (93, 150), (210, 300), (210, 290), (330, 480), (330, 470)]
        cols, strips = eu_qrd.rule_columns(spans, 92.7, 519.4, 8.0)
        self.assertEqual(len(cols), 3)
        self.assertEqual(strips, [(160, 210), (300, 330)])
        self.assertEqual(eu_qrd.rule_columns([(93, 300), (303, 480)], 92.7, 519.4, 8.0), ([], []))  # word spaces

    def test_fixed_headings_for_unprinted_sections(self):
        self.assertEqual(eu_qrd.FIXED_HEADINGS, {"smpc-0-additional-monitoring": "# 0 Additional monitoring",
                                                 "smpc-0-front-matter": "# 0 Front matter"})


def _pdf_tests_or_skip():
    return unittest.skipUnless(HAVE_PYMUPDF, "PyMuPDF not installed in this interpreter (use .venv-eu/bin/python)")


@_pdf_tests_or_skip()
class PdfTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pymupdf
        cls.pm = pymupdf

    def synthetic_pdf(self, pages: list[list[tuple[str, bool]]]) -> bytes:
        """A PDF with Times text, one (text, bold) pair per line, page numbers at the bottom."""
        doc = self.pm.open()
        for n, lines in enumerate(pages, start=1):
            page = doc.new_page(width=595, height=842)
            y = 70.0
            for text, bold in lines:
                if text:
                    page.insert_text((70.9, y), text, fontname="tibo" if bold else "tiro", fontsize=11)
                y += 12.6 if text else 12.6
            page.insert_text((293, 805), str(n), fontname="helv", fontsize=8)
        data = doc.tobytes(garbage=4, deflate=True, no_new_id=True)
        doc.close()
        return data

    def test_trimmed_eliquis_2011(self):
        result = eu_qrd.convert((FIXTURES / "eliquis-2011-trim.pdf").read_bytes())
        sec = result["sections"]
        self.assertEqual(result["status"], "converted")
        self.assertEqual(result["converter_version"], eu_qrd.EU_CONVERTER_VERSION)
        self.assertRegex(result["pymupdf_version"], r"^\d+\.\d+")
        self.assertEqual([s["name"] for s in result["smpcs"]], ["ELIQUIS 2.5 mg film-coated tablets"])
        self.assertTrue(sec["smpc-4.2-posology-and-method-of-administration"].startswith(
            "# 4.2 Posology and method of administration\n\nPosology\n"
            "The recommended dose of ELIQUIS is 2.5 mg taken orally twice daily.\n"
            "The initial dose should be taken 12 to 24 hours after surgery.\n\n"
            "Physicians may consider the potential benefits of earlier anticoagulation for VTE prophylaxis as well as "
            "the risks of post-surgical bleeding in deciding on the time of administration within this time window.\n"))
        self.assertEqual(sec["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\n"
                         "- Hypersensitivity to the active substance or to any of the excipients.\n"
                         "- Clinically significant active bleeding.\n"
                         "- Hepatic disease associated with coagulopathy and clinically relevant bleeding risk "
                         "(see section 5.2)\n")
        self.assertEqual(sec["smpc-7-marketing-authorisation-holder"],
                         "# 7. MARKETING AUTHORISATION HOLDER\n\nBristol-Myers Squibb/Pfizer EEIG, Bristol-Myers "
                         "Squibb House,\nUxbridge Business Park, Sanderson Road, Uxbridge, Middlesex\nUB8 1DH\n"
                         "United Kingdom\n")
        self.assertEqual(sec["smpc-8-marketing-authorisation-numbers"], "# 8. MARKETING AUTHORISATION NUMBER(S)\n")
        everything = "\n".join(sec.values())
        self.assertNotRegex(everything, r"(?m)^\d+$")           # no page numbers
        self.assertNotIn("ANNEX II", everything)
        self.assertNotIn("MANUFACTURING AUTHORISATION HOLDER", everything)
        self.assertNotIn("", everything)                  # Symbol bullets became "- "
        self.assertEqual(result, eu_qrd.convert((FIXTURES / "eliquis-2011-trim.pdf").read_bytes()))

    def test_synthetic_multi_smpc_pdf(self):
        def one(name, dose, extra):
            return [("ANNEX I", True)] * 0 + [
                ("1.    NAME OF THE MEDICINAL PRODUCT", True), ("", False), (name, False), ("", False),
                ("4.2   Posology and method of administration", True), ("", False),
                (f"The recommended dose is {dose} twice daily.", False), (extra, False), ("", False),
                ("4.3   Contraindications", True), ("", False),
                ("Hypersensitivity to the active substance.", False)]
        pages = [[("ANNEX I", True), ("SUMMARY OF PRODUCT CHARACTERISTICS", True)],
                 one("Testdrug 2.5 mg tablets", "2.5 mg", "Swallow whole."),
                 one("Testdrug 5 mg tablets", "5 mg", "Swallow whole."),
                 [("ANNEX II", True)]]
        result = eu_qrd.convert(self.synthetic_pdf(pages))
        self.assertEqual(len(result["smpcs"]), 2)
        self.assertEqual(result["sections"]["smpc-4.2-posology-and-method-of-administration"],
                         "# 4.2 Posology and method of administration\n\n"
                         "The recommended dose is 2.5 mg twice daily.\nSwallow whole.\n\n"
                         "[Testdrug 5 mg tablets]\n\nThe recommended dose is 5 mg twice daily.\n")
        self.assertEqual(result["sections"]["smpc-4.3-contraindications"],
                         "# 4.3 Contraindications\n\nHypersensitivity to the active substance.\n")

    def page_items(self, name: str) -> tuple[list, list]:
        items, _, _ = eu_qrd.extract_items((FIXTURES / name).read_bytes())
        return [it for it in items if isinstance(it, Table)], [it for it in items if isinstance(it, Line)]

    def test_prezista_2018_interaction_table_subscripts(self):
        tables, _ = self.page_items("prezista-2018-p20.pdf")
        text = "\n".join(" | ".join(r) for t in tables for r in t.rows)
        self.assertIn("telaprevir Cmin ↓ 32% telaprevir Cmax ↓ 36% darunavir AUC12 ↓ 40% darunavir Cmin ↓ 42%", text)
        self.assertNotRegex(text, r"C [↑↓↔][^|]*\b(min|max)\b")

    def test_xeljanz_2022_sparse_adr_table_is_a_pipe_table(self):
        tables, _ = self.page_items("xeljanz-2022-p16.pdf")
        rows = {r[0]: r for t in tables for r in t.rows}
        self.assertEqual(rows["Skin and subcutaneous tissue disorders"][1:3], ["Rash", "Erythema Pruritus"])
        self.assertEqual(rows["Cardiac disorders"][1:3], ["", "Myocardial infarction"])
        # The PDF wraps the footnote mark of "Hypersensitivity*" onto its own line in the cell.
        self.assertEqual(rows["Immune system disorders"][5], "Hypersensitivity * Angioedema* Urticaria*")

    def test_opdivo_2020_horizontally_ruled_table(self):
        tables, lines = self.page_items("opdivo-2020-p5-6.pdf")
        rows = [r for t in tables for r in t.rows]
        self.assertIn(["Immune-related adverse reaction", "Severity", "Treatment modification"], rows)
        self.assertIn(["Immune-related pneumonitis", "Grade 2 pneumonitis", "Withhold dose(s) until symptoms "
                       "resolve, radiographic abnormalities improve, and management with corticosteroids is complete"],
                      rows)
        self.assertIn(["", "Grade 3 or 4 pneumonitis", "Permanently discontinue treatment"], rows)
        # The row cut by the page break (no closing rule) is still a row.
        self.assertIn(["Immune-related skin adverse reactions", "Grade 3 rash", "Withhold dose(s) until symptoms "
                       "resolve and management with corticosteroids is complete"], rows)
        self.assertFalse([l.text for l in lines if "resolve and management with" in l.text])

    def test_humira_2012_stacked_frequencies(self):
        tables, _ = self.page_items("humira-2012-p12.pdf")
        rows = [r for t in tables for r in t.rows]
        self.assertIn(["Ear and labyrinth disorders", "Common", "vertigo"], rows)
        self.assertIn(["", "Uncommon", "deafness, tinnitus"], rows)

    def test_lyrica_2015_formula_digits_in_order(self):
        _, lines = self.page_items("lyrica-2015-p3.pdf")
        text = "\n".join(l.text for l in lines)
        self.assertIn("1.23 140 - age (years) x weight (kg)", text)
        self.assertIn("( x 0.85 for female patients)", text)
        self.assertNotIn(".123", text)

    def test_no_text_layer(self):
        doc = self.pm.open()
        for _ in range(3):
            page = doc.new_page(width=595, height=842)
            pix = self.pm.Pixmap(self.pm.csGRAY, self.pm.IRect(0, 0, 60, 80), 0)
            pix.clear_with(200)
            page.insert_image(page.rect, pixmap=pix)
        data = doc.tobytes(no_new_id=True)
        result = eu_qrd.convert(data)
        self.assertEqual(result["sections"], {})
        self.assertEqual(result["status"], "no-text-layer")
        self.assertTrue(result["warnings"][0].startswith("no-text-layer"))

    def test_unreadable_pdf(self):
        result = eu_qrd.convert(b"not a pdf at all")
        self.assertEqual(result["sections"], {})
        self.assertEqual(result["status"], "unreadable-pdf")
        self.assertTrue(result["warnings"][0].startswith("unreadable-pdf"))


def _tokens(text: str) -> list[str]:
    """Comparison tokens: split on white space and hyphens, keep letters and
    digits only (so Symbol-font glyphs, bullets and Markdown syntax drop out)."""
    out = []
    for raw in re.split(r"[\s\-\u2010\u2011]+", text):
        tok = re.sub(r"[^0-9A-Za-z\u00c0-\u024f\u0400-\u04ff]", "", raw)  # Greek too: Symbol "m" vs "μ"
        if tok:
            out.append(tok)
    return out


@_pdf_tests_or_skip()
@unittest.skipUnless((M0 / "eliquis").exists(), "m0-data/eu not present")
class M0AnnexTest(unittest.TestCase):
    """Full pilot annexes, page by page: every word on the Annex I pages of the
    PDF must be in the output, except the documented drops."""

    def compare(self, pdf: Path) -> tuple[collections.Counter, collections.Counter, dict]:
        import pymupdf
        data = pdf.read_bytes()
        result = eu_qrd.convert(data)
        first, last = result["stats"]["annex_pages"]
        ref, rotated = collections.Counter(), collections.Counter()
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for p in range(first - 1, last):
                page = doc[p]
                ref.update(_tokens(page.get_text("text")))
                ref[str(p + 1)] -= 1  # the page number
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        if abs(line["dir"][1]) > 0.01:
                            rotated.update(_tokens("".join(s["text"] for s in line["spans"])))
        got = collections.Counter()
        for text in result["sections"].values():
            got.update(_tokens(text))
        lost = ref - got - rotated
        added = got - ref
        return lost, added, result

    def test_eliquis_2011_page_by_page(self):
        lost, added, result = self.compare(M0 / "eliquis" / "20110518102349" / "anx_102349_en.pdf")
        group_headings = collections.Counter(_tokens("4. CLINICAL PARTICULARS 5. PHARMACOLOGICAL PROPERTIES "
                                                     "6. PHARMACEUTICAL PARTICULARS"))
        repeated_header = collections.Counter(_tokens("Common (≥1/100 to < 1/10) Uncommon (≥1/1,000 to < 1/100) "
                                                      "Rare (≥1/10,000 to < 1/1,000)"))
        # Converter 4 keeps footnote digits apart ("Period 1"), so only the group headings and the header row
        # repeated where the ADR table continues on the next page are lost.
        self.assertEqual(lost, group_headings + repeated_header)
        self.assertEqual(added, collections.Counter())
        self.assertEqual(result["warnings"], [])

    def test_keytruda_2024_page_by_page(self):
        lost, added, result = self.compare(M0 / "keytruda" / "20240829163752" / "anx_163752_en.pdf")
        self.assertEqual(len(result["smpcs"]), 1)
        self.assertEqual(result["warnings"], [])
        group_headings = collections.Counter(_tokens("4. CLINICAL PARTICULARS 5. PHARMACOLOGICAL PROPERTIES "
                                                     "6. PHARMACEUTICAL PARTICULARS"))
        lost -= group_headings
        # What remains is repeated header rows of tables continued on a new
        # page (dropped on purpose) and glued superscripts; measured at 59 of
        # about 42,000 words when this test was written.
        self.assertLessEqual(sum(lost.values()), 70, lost)
        self.assertLessEqual(sum(added.values()), 20, added)


@unittest.skipIf(HAVE_PYMUPDF, "PyMuPDF available here; the PDF tests run directly")
@unittest.skipUnless(VENV_PYTHON.exists(), ".venv-eu not present")
class VenvPdfTests(unittest.TestCase):
    def test_pdf_tests_pass_under_venv(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        proc = subprocess.run([str(VENV_PYTHON), "-I", "-m", "unittest", "discover", "-s", "tests",
                               "-p", "test_eu_qrd.py", "-k", "PdfTest", "-k", "M0AnnexTest"],
                              cwd=TOOLS, capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertRegex(proc.stderr, r"Ran [1-9]\d* tests?")


if __name__ == "__main__":
    unittest.main()
