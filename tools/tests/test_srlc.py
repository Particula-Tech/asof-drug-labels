"""SrLC fetching, parsing, quote rules, coverage and the deterministic sample.

Fixtures under srlc_fixtures/ are trimmed copies of real SrLC pages (one search result page, the start
page a rejected search redirects to, one detail page with two changes and several sections, one change
whose sections hold several subsection divs, and one whose content div nests <h4> tags). Nothing here
touches the network or build/us.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, timedelta
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import srlc  # noqa: E402

FIXTURES = Path(__file__).parent / "srlc_fixtures"
SEARCH_PAGE = (FIXTURES / "search-2024-01.html").read_text(encoding="utf-8")
START_PAGE = (FIXTURES / "start-page.html").read_text(encoding="utf-8")
DETAIL_PAGE = (FIXTURES / "detail-19.html").read_text(encoding="utf-8")
DETAIL_952 = (FIXTURES / "detail-952.html").read_text(encoding="utf-8")   # FORTAMET 2017-04-05: several divs per section
DETAIL_188 = (FIXTURES / "detail-188.html").read_text(encoding="utf-8")   # INDOMETHACIN 2021-04-28: <h4> nested in a div


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, url: str, status: int = 200):
        super().__init__(body)
        self.status, self._url = status, url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    """Serves canned pages per URL and records every request it sees."""

    def __init__(self, pages: dict, status: dict | None = None):
        self.pages, self.status, self.requests = pages, status or {}, []

    def open(self, req, timeout=None):
        url = req.full_url
        self.requests.append({"url": url, "method": req.get_method(), "headers": dict(req.header_items()),
                              "data": req.data})
        status = self.status.get(url, 200)
        if status != 200:
            raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(b""))
        return FakeResponse(self.pages[url], url)


def make_session(opener, min_gap_s=1.0):
    waits = []
    session = srlc.Session(opener=opener, log_path=None, sleep=waits.append, min_gap_s=min_gap_s)
    return session, waits


class SearchPageTest(unittest.TestCase):
    def test_rows_have_every_field(self):
        rows = srlc.parse_search_page(SEARCH_PAGE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"drug_id": "1358", "brand": "AURYXIA", "ingredient": "FERRIC CITRATE",
                                   "application": "NDA205874", "application_kind": "NDA", "application_number": "205874",
                                   "approval_date": "2024-01-19", "posted_date": "2024-01-23"})
        self.assertEqual(rows[1]["application"], "BLA021859")
        self.assertEqual(rows[1]["drug_id"], "19")

    def test_start_page_is_not_a_result_page(self):
        self.assertTrue(srlc.is_start_page(START_PAGE))
        self.assertFalse(srlc.is_results_page(START_PAGE))
        self.assertTrue(srlc.is_results_page(SEARCH_PAGE))
        self.assertFalse(srlc.is_start_page(SEARCH_PAGE))
        with self.assertRaises(srlc.SrlcError):
            srlc.parse_search_page(START_PAGE)

    def test_result_page_without_rows_is_not_the_start_page(self):
        empty = SEARCH_PAGE.rsplit("<tbody>", 1)[0] + "<tbody></tbody></table></body></html>"
        self.assertTrue(srlc.is_results_page(empty))
        self.assertEqual(srlc.parse_search_page(empty), [])

    def test_search_form_fields(self):
        form = srlc.search_form(2024, 2).decode()
        self.assertIn("date_range_from=02%2F01%2F2024", form)
        self.assertIn("date_range_to=02%2F29%2F2024", form)
        self.assertEqual(form.count("section="), 7)
        self.assertTrue(form.endswith("&Datesearch="))
        self.assertEqual(srlc.month_bounds(2025, 12), ("12/01/2025", "12/31/2025"))
        self.assertEqual(srlc.month_range("2025-11", "2026-02"), [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])

    def test_in_corpus_drugs_match_on_digits(self):
        rows = srlc.parse_search_page(SEARCH_PAGE)
        apps = {"NDA021859": [{"inn": "hyaluronidase", "setid": "s1", "status": "current"}]}
        self.assertEqual(srlc.in_corpus_drug_ids(rows, apps), {"19"})


class SessionTest(unittest.TestCase):
    def test_search_sends_cookies_referer_and_submit_field(self):
        pages = {srlc.SRLC_INDEX: START_PAGE.encode(), srlc.SRLC_SEARCH: SEARCH_PAGE.encode()}
        opener = FakeOpener(pages)
        session, waits = make_session(opener)
        with tempfile.TemporaryDirectory() as tmp:
            n = srlc.fetch_searches(session, [(2024, 1)], Path(tmp), log=lambda *_: None)
            self.assertEqual(n, 1)
            self.assertTrue((Path(tmp) / "search" / "2024-01.html").exists())
            # a second run reads the cache and makes no request
            before = len(opener.requests)
            self.assertEqual(srlc.fetch_searches(session, [(2024, 1)], Path(tmp), log=lambda *_: None), 0)
            self.assertEqual(len(opener.requests), before)
        self.assertEqual([r["url"] for r in opener.requests], [srlc.SRLC_INDEX, srlc.SRLC_SEARCH])
        post = opener.requests[1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["headers"]["User-agent"], "ParticulaTech-research/0.1 (drug-label history check)")
        self.assertEqual(post["headers"]["Referer"], srlc.SRLC_INDEX)
        self.assertIn(b"Datesearch=", post["data"])
        self.assertIn(b"section=BW", post["data"])
        self.assertEqual(session.requests, 2)

    def test_redirect_to_start_page_is_a_failure_not_no_results(self):
        pages = {srlc.SRLC_INDEX: START_PAGE.encode(), srlc.SRLC_SEARCH: START_PAGE.encode()}
        session, _ = make_session(FakeOpener(pages))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(srlc.SrlcError) as ctx:
                srlc.fetch_searches(session, [(2024, 1)], Path(tmp), log=lambda *_: None)
            self.assertIn("start page", str(ctx.exception))
            self.assertFalse((Path(tmp) / "search" / "2024-01.html").exists())

    def test_rate_limit_waits_between_requests(self):
        pages = {srlc.SRLC_INDEX: START_PAGE.encode(), srlc.SRLC_SEARCH: SEARCH_PAGE.encode()}
        session, waits = make_session(FakeOpener(pages))
        with tempfile.TemporaryDirectory() as tmp:
            srlc.fetch_searches(session, [(2024, 1)], Path(tmp), log=lambda *_: None)
        self.assertEqual(len(waits), 1)  # the second request waited for the one-second slot
        self.assertGreater(waits[0], 0.9)

    def test_403_stops_without_retry(self):
        url = srlc.SRLC_DETAIL.format(drug_id="19")
        opener = FakeOpener({srlc.SRLC_INDEX: START_PAGE.encode()}, status={url: 403})
        session, waits = make_session(opener)
        with self.assertRaises(srlc.SrlcBlocked):
            session.request(url)
        self.assertEqual(len([r for r in opener.requests if r["url"] == url]), 1)
        self.assertTrue(all(w <= srlc.MIN_GAP_S for w in waits), waits)   # rate-limit slots only, no retry back-off

    def test_challenge_page_stops(self):
        url = srlc.SRLC_DETAIL.format(drug_id="19")
        opener = FakeOpener({srlc.SRLC_INDEX: START_PAGE.encode(), url: b"<html><body>Pardon Our Interruption</body></html>"})
        session, _ = make_session(opener)
        with self.assertRaises(srlc.SrlcBlocked):
            session.request(url)

    def test_other_hosts_are_refused(self):
        session, _ = make_session(FakeOpener({}))
        with self.assertRaises(srlc.SrlcError):
            session.request("https://dailymed.nlm.nih.gov/x")
        self.assertEqual(session.requests, 0)

    def test_detail_fetch_is_cached_and_validated(self):
        url = srlc.SRLC_DETAIL.format(drug_id="19")
        opener = FakeOpener({srlc.SRLC_INDEX: START_PAGE.encode(), url: DETAIL_PAGE.encode(),
                             srlc.SRLC_DETAIL.format(drug_id="7"): START_PAGE.encode()})
        session, _ = make_session(opener)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(srlc.fetch_details(session, ["19"], Path(tmp), log=lambda *_: None), 1)
            self.assertEqual(srlc.fetch_details(session, ["19"], Path(tmp), log=lambda *_: None), 0)
            with self.assertRaises(srlc.SrlcError):
                srlc.fetch_details(session, ["7"], Path(tmp), log=lambda *_: None)
            self.assertFalse((Path(tmp) / "detail" / "7.html").exists())


class DetailPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drug = srlc.parse_detail_page(DETAIL_PAGE, "19")
        cls.blocks = {(b["approval_date"], b["section"]): b for b in cls.drug["blocks"]}

    def test_header(self):
        d = self.drug
        self.assertEqual((d["brand"], d["application_text"], d["application"], d["ingredient"]),
                         ("HYLENEX RECOMBINANT", "BLA-021859", "BLA021859", "HYALURONIDASE RECOMBINANT HUMAN"))

    def test_every_change_and_section_is_a_block(self):
        keys = [(b["approval_date"], b["supplement_text"], b["section"]) for b in self.drug["blocks"]]
        self.assertEqual(keys, [("2024-01-12", "SUPPL-31", "USP"), ("2024-01-12", "SUPPL-31", "WP"),
                                ("2024-01-12", "SUPPL-31", "C"), ("2024-01-12", "SUPPL-31", "PCI/PI/MG"),
                                ("2016-01-21", "SUPPL-23", "DI"), ("2016-01-21", "SUPPL-23", "BW")])

    def test_underlined_spans_merge_adjacent_and_drop_headings(self):
        b = self.blocks[("2024-01-12", "USP")]
        self.assertEqual(b["comment_type"], srlc.COMMENT_UNDERLINED)
        self.assertEqual(b["subsection_headings"], ["8.1 Pregnancy", "8.2 Lactation"])
        quote = b["quote_paragraphs"]
        self.assertIn("adequate and well-controlled studies of HYLENEX recombinant administration in pregnant women to "
                      "inform a drug-associated risk. Subcutaneous administration of HYLENEX recombinant to pregnant mice "
                      "throughout organogenesis did not produce teratogenic effects at clinically relevant doses.", quote)
        self.assertIn("There are", quote)        # the span ends where the plain word 'no' starts
        self.assertIn("9 mg/kg (approximately", quote)
        self.assertIn("USP units", quote)
        self.assertNotIn("8.1 Pregnancy", quote)
        self.assertFalse(any("Additions" in q for q in quote))
        kept = srlc.kept_sentences(quote)
        self.assertEqual(kept[0], "adequate and well-controlled studies of HYLENEX recombinant administration in pregnant "
                                  "women to inform a drug-associated risk.")
        self.assertNotIn("There are", kept)
        self.assertTrue(srlc.usable(kept))
        self.assertIn("<u>Risk Summary</u>", b["raw_html"])

    def test_newly_added_takes_the_whole_text_and_drops_deleted_text(self):
        b = self.blocks[("2024-01-12", "WP")]
        self.assertEqual(b["comment_type"], srlc.COMMENT_NEWLY_ADDED)
        self.assertEqual(b["subsection_headings"], ["5.3 Hypersensitivity Reactions"])
        self.assertEqual(b["quote_paragraphs"], ["Serious hypersensitivity reactions, including anaphylaxis, have been "
                                                 "reported with HYLENEX recombinant. Discontinue the infusion and treat "
                                                 "the reaction if it occurs."])
        self.assertEqual(len(srlc.kept_sentences(b["quote_paragraphs"])), 2)
        self.assertNotIn("deleted", " ".join(b["quote_paragraphs"]))

    def test_pllr_note_leaves_no_quote(self):
        b = self.blocks[("2024-01-12", "C")]
        self.assertEqual(b["comment_type"], srlc.COMMENT_REFER_TO_LABEL)
        self.assertEqual(b["quote_paragraphs"], [])
        self.assertFalse(srlc.usable(srlc.kept_sentences(b["quote_paragraphs"])))

    def test_patient_labeling_is_one_unsampled_section(self):
        b = self.blocks[("2024-01-12", "PCI/PI/MG")]
        self.assertNotIn(b["section"], srlc.SAMPLED_SECTIONS)
        self.assertIn(b["section"], srlc.UNSAMPLED_SECTIONS)

    def test_summary_without_markup_has_no_quote(self):
        b = self.blocks[("2016-01-21", "DI")]
        self.assertEqual(b["comment_type"], srlc.COMMENT_NONE)
        self.assertEqual(b["quote_paragraphs"], [])
        self.assertIn("sodium metabisulfite", b["text"])

    def test_plain_comment_variant_and_bold_heading(self):
        b = self.blocks[("2016-01-21", "BW")]
        self.assertEqual(b["comment_type"], srlc.COMMENT_UNDERLINED)
        self.assertEqual(b["subsection_headings"], ["WARNING: SEVERE REACTIONS"])
        self.assertEqual(len(b["quote_paragraphs"]), 1)

    def test_section_names(self):
        for heading, code in [("8 Use in Specific Populations", "USP"), ("Boxed Warning", "BW"), ("Warnings", "WP"),
                              ("Precautions", "WP"), ("5 Warnings and Precautions", "WP"), ("6 Adverse Reactions", "AR"),
                              ("7 Drug Interactions", "DI"), ("4 Contraindications", "C"),
                              ("17 PCI/PI/MG (Patient Counseling Information/Patient Information/Medication Guide)", "PCI/PI/MG"),
                              ("Medication Guide", "PCI/PI/MG"), ("Other", None), ("Fetal Toxicity", None)]:
            self.assertEqual(srlc.srlc_section_code(heading), code, heading)

    def test_comment_wording_families(self):
        for text, kind in [("Additions and/or revisions underlined:", srlc.COMMENT_UNDERLINED),
                           ("(Additions and/or revisions are underlined)", srlc.COMMENT_UNDERLINED),
                           ("Additions are underlined below:", srlc.COMMENT_UNDERLINED),
                           ("Newly added information:", srlc.COMMENT_NEWLY_ADDED),
                           ("(newly added subsection)", srlc.COMMENT_NEWLY_ADDED),
                           ("New subsection added:", srlc.COMMENT_NEWLY_ADDED),
                           ("Addition of the following to the bulleted line listing:", srlc.COMMENT_NEWLY_ADDED),
                           ("PLLR conversion; please refer to label for complete information.", srlc.COMMENT_REFER_TO_LABEL),
                           ("Table 3 has been revised; please refer to label.", srlc.COMMENT_REFER_TO_LABEL),
                           ("Risk Summary", None),
                           ("Serious reactions have been reported in patients.", None)]:
            self.assertEqual(srlc.comment_type(text, italic_only=True), kind, text)
        self.assertEqual(srlc.comment_type("Subsection title revised", italic_only=True), srlc.COMMENT_OTHER)
        self.assertIsNone(srlc.comment_type("The dose was revised in the trial.", italic_only=False))

    def test_other_comment_does_not_change_the_default_mode(self):
        html = ('<p><i>Subsection title revised</i></p><p>Plain text that is not a quote here.</p>'
                '<p><u>Underlined text that was added to the label in this version.</u></p>')
        q = srlc.section_quote(srlc.parse_paragraphs(html))
        self.assertEqual(q["comment_type"], srlc.COMMENT_OTHER)
        self.assertEqual(q["quote_paragraphs"], ["Underlined text that was added to the label in this version."])

    def test_inline_comment_keeps_the_text_after_it(self):
        html = "<p>New subsection created: Avoid use of the product in patients being treated with anesthetics.</p>"
        q = srlc.section_quote(srlc.parse_paragraphs(html))
        self.assertEqual(q["comment_type"], srlc.COMMENT_NEWLY_ADDED)
        self.assertEqual(q["quote_paragraphs"], ["Avoid use of the product in patients being treated with anesthetics."])

    def test_long_bold_heading_without_sentence_punctuation(self):
        html = ('<p><b>WARNING: ADDICTION, ABUSE, AND MISUSE; LIFE-THREATENING RESPIRATORY DEPRESSION; '
                '<u>and RISKS FROM CONCOMITANT USE WITH BENZODIAZEPINES OR OTHER CNS DEPRESSANTS</u></b><i> </i></p>'
                '<p><b><u>Risks From Concomitant Use With Benzodiazepines Or Other CNS Depressants</u></b></p>'
                '<p><i>Newly added:</i></p>'
                '<p><b>Concomitant use of opioids with benzodiazepines may result in profound sedation, respiratory depression, coma, and death.</b></p>'
                '<p><b>Table 3: Adverse Reactions Reported in Greater Than or Equal to 5% of Patients in Any Treatment Arm</b></p>')
        paras = srlc.parse_paragraphs(html)
        self.assertEqual([p["kind"] for p in paras], ["heading", "heading", "comment", "text", "heading"])
        q = srlc.section_quote(paras)
        self.assertEqual(q["quote_paragraphs"], ["Concomitant use of opioids with benzodiazepines may result in profound sedation, "
                                                 "respiratory depression, coma, and death."])
        self.assertTrue(q["subsection_headings"][0].startswith("WARNING: ADDICTION"))
        self.assertTrue(srlc.is_heading("8.1 Pregnancy", bold_only=True, heading_tag=False))
        self.assertTrue(srlc.is_heading("Anything in a heading tag. Even with sentences.", bold_only=False, heading_tag=True))
        self.assertFalse(srlc.is_heading("A bold sentence of more than eight tokens that ends properly.", bold_only=True, heading_tag=False))
        self.assertFalse(srlc.is_heading("Not bold and without punctuation at all here in this paragraph", bold_only=False, heading_tag=False))

    def test_supplement_and_item_id(self):
        self.assertEqual(srlc.supplement_number("SUPPL-31"), "31")
        self.assertEqual(srlc.supplement_number("SUPPL-31, SUPPL-32"), "31+32")
        self.assertEqual(srlc.item_id("NDA208194", "2019-10-08", "19", "WP"), "srlc-NDA208194-20191008-SUPPL19-WP")

    def test_application_normalisation(self):
        self.assertEqual(srlc.normalise_application("NDA  ", "205874"), "NDA205874")
        self.assertEqual(srlc.normalise_application("BLA", "21859"), "BLA021859")
        self.assertIsNone(srlc.normalise_application("ANDA", "076543"))
        self.assertEqual(srlc.application_from_text("BENDEKA (NDA-208194)"), "NDA208194")
        apps = {"NDA021859": []}
        self.assertEqual(srlc.crosswalk_form("BLA021859", apps), ("NDA021859", True))
        self.assertEqual(srlc.crosswalk_form("NDA021859", apps), ("NDA021859", True))
        self.assertEqual(srlc.crosswalk_form("NDA999999", apps), ("NDA999999", False))


class MultiDivSectionTest(unittest.TestCase):
    """A section with several subsections: one <strong> + content div per subsection (FORTAMET, 2017-04-05)."""

    @classmethod
    def setUpClass(cls):
        cls.drug = srlc.parse_detail_page(DETAIL_952, "952")
        cls.blocks = {b["section"]: b for b in cls.drug["blocks"]}

    def test_every_content_div_of_a_section_is_read(self):
        di = self.blocks["DI"]
        self.assertEqual(di["content_divs"], 3)
        self.assertEqual(di["subsection_headings"], ["Alcohol", "Carbonic Anhydrase Inhibitors", "Drugs that reduce metformin clearance"])
        self.assertEqual(di["comment_type"], srlc.COMMENT_MIXED)   # two '(new subsection added)' divs, one '(additions underlined)'
        q = di["quote_paragraphs"]
        self.assertEqual(q[0], "Alcohol is known to potentiate the effect of metformin on lactate metabolism. Warn patients against "
                               "excessive alcohol intake while receiving FORTAMET.")
        self.assertTrue(q[1].startswith("Topiramate or other carbonic anhydrase inhibitors"))
        self.assertTrue(q[2].startswith("Concomitant use of drugs that interfere with common renal tubular transport systems"))
        self.assertEqual(len(q), 5)
        self.assertEqual(len(srlc.kept_sentences(q)), 9)
        self.assertEqual(srlc.quote_tokens(srlc.kept_sentences(q)), 191)

    def test_chunks_equal_content_divs_and_sections_keep_their_order(self):
        self.assertEqual([b["section"] for b in self.drug["blocks"]], ["C", "WP", "AR", "DI", "USP", "PCI/PI/MG"])
        self.assertEqual(sum(b["content_divs"] for b in self.drug["blocks"]), DETAIL_952.count('<div style="background-color:#fff'))
        self.assertEqual(self.blocks["WP"]["content_divs"], 2)
        self.assertEqual(self.blocks["WP"]["subsection_headings"][0], "PRECAUTIONS General")
        self.assertEqual((self.drug["brand"], self.drug["application"]), ("FORTAMET", "NDA021574"))


class NestedHeadingTest(unittest.TestCase):
    """An <h4> inside a content div is a heading paragraph of that section, not a new section (INDOMETHACIN, 2021-04-28)."""

    @classmethod
    def setUpClass(cls):
        cls.drug = srlc.parse_detail_page(DETAIL_188, "188")

    def test_nested_h4_does_not_start_a_section(self):
        self.assertEqual([b["section"] for b in self.drug["blocks"]], ["WP", "USP", "PCI/PI/MG"])
        wp = self.drug["blocks"][0]
        self.assertEqual(wp["subsection_headings"], ["Warnings", "Drug Reaction with Eosinophilia and Systemic Symptoms (DRESS)", "Fetal Toxicity"])
        self.assertEqual(wp["comment_type"], srlc.COMMENT_NEWLY_ADDED)
        self.assertTrue(wp["quote_paragraphs"][0].startswith("Drug Reaction with Eosinophilia and Systemic Symptoms (DRESS) has been reported"))
        self.assertNotIn("Drug Reaction with Eosinophilia and Systemic Symptoms (DRESS)", wp["quote_paragraphs"])  # the heading itself
        self.assertTrue(any("ductus arteriosus" in q.lower() for q in wp["quote_paragraphs"]))   # text after the nested h4 stays in WP

    def test_block_walker(self):
        block = ('<h4>5 Warnings and Precautions</h4> <strong>A</strong> <div style="background-color:#fff;"><p>one</p><h4>Inner</h4>'
                 '<p>two</p></div> <strong>B</strong> <div style="background-color:#fff;"><p>three</p></div>'
                 '<h4>6 Adverse Reactions</h4><div style="background-color:#fff;"><p>four</p></div>')
        secs = srlc.block_sections(block)
        self.assertEqual([s["heading"] for s in secs], ["5 Warnings and Precautions", "6 Adverse Reactions"])
        self.assertEqual([c["subsection"] for c in secs[0]["chunks"]], ["A", "B"])
        self.assertEqual([c["subsection"] for c in secs[1]["chunks"]], [""])
        self.assertIn("<h4>Inner</h4>", secs[0]["chunks"][0]["inner"])
        self.assertEqual([p["kind"] for p in srlc.parse_paragraphs(secs[0]["chunks"][0]["inner"])], ["text", "heading", "text"])
        self.assertEqual(srlc.block_sections("<p>nothing here</p>"), [])


class NormalisationTest(unittest.TestCase):
    def test_abbreviations_do_not_end_a_sentence(self):
        self.assertEqual(srlc.split_sentences("Avoid strong CYP3A inhibitors (e.g. Ketoconazole) with the drug. Use with St. John's Wort is not advised."),
                         ["Avoid strong CYP3A inhibitors (e.g. Ketoconazole) with the drug.", "Use with St. John's Wort is not advised."])
        self.assertEqual(srlc.split_sentences("Doses above 0.5 mg/kg vs. 1 mg/kg were compared. See Fig. 3 for the curve."),
                         ["Doses above 0.5 mg/kg vs. 1 mg/kg were compared.", "See Fig. 3 for the curve."])
        self.assertEqual(srlc.split_sentences("Outcomes from women with MS. 797 pregnancies were exposed."),   # 'MS.' is not 'Ms.'
                         ["Outcomes from women with MS.", "797 pregnancies were exposed."])
        self.assertEqual(srlc.split_sentences("Contains FD&C Yellow No. 5. Do not use."), ["Contains FD&C Yellow No. 5.", "Do not use."])

    def test_normalise(self):
        self.assertEqual(srlc.normalise("  Mother’s   ‘need’ — (e.g., 0.5 mg/kg) ﬁne Ⅳ "), "mother s need e g 0 5 mg kg fine iv")
        self.assertEqual(srlc.tokens("Risk Summary:"), ["risk", "summary"])
        self.assertEqual(srlc.tokens(""), [])

    def test_sentences_and_usability(self):
        para = ("Reported in patients. Avoid use with CYP3A inhibitors (e.g. ketoconazole); monitor closely. "
                "Take 0.5 mg daily with food at night.")
        # splits after . ! ? ; : only before an upper-case, digit or bracket start: 'e.g. k' and '; monitor' stay
        self.assertEqual(srlc.split_sentences(para), ["Reported in patients.",
                                                      "Avoid use with CYP3A inhibitors (e.g. ketoconazole); monitor closely.",
                                                      "Take 0.5 mg daily with food at night."])
        kept = srlc.kept_sentences([para])
        self.assertEqual(kept, ["Avoid use with CYP3A inhibitors (e.g. ketoconazole); monitor closely.",
                                "Take 0.5 mg daily with food at night."])
        self.assertEqual(srlc.quote_tokens(kept), 10 + 9)
        self.assertTrue(srlc.usable(kept))
        self.assertFalse(srlc.usable(["one two three four five six", "seven eight nine ten eleven"]))
        self.assertTrue(srlc.usable(["one two three four five six", "seven eight nine ten eleven twelve"]))


class PresenceTest(unittest.TestCase):
    TEXT = ("## 5.3 Hepatotoxicity\n"
            "Serious hypersensitivity reactions, including anaphylaxis, have been\n"
            "reported with HYLENEX recombinant.\n"
            "Discontinue the infusion and treat the reaction, if it occurs, promptly.\n")

    def test_substring_match_ignores_case_and_punctuation(self):
        section = srlc.SectionText(self.TEXT)
        self.assertTrue(srlc.sentence_found("discontinue the INFUSION and treat the reaction", section, substring_only=True))
        self.assertFalse(srlc.sentence_found("discontinue the infusion and stop the reaction", section, substring_only=True))

    def test_coverage_over_a_pair_of_lines(self):
        section = srlc.SectionText(self.TEXT)
        sentence = "Serious hypersensitivity reactions, including anaphylaxis, have been reported with HYLENEX recombinant."
        self.assertEqual(len(srlc.tokens(sentence)), 11)
        self.assertEqual(srlc.token_coverage(srlc.tokens(sentence), section.lines[1]), 7 / 11)
        self.assertTrue(srlc.sentence_found(sentence, section, substring_only=True))  # the line break normalises to a space
        reworded = "Serious hypersensitivity reactions, including anaphylaxis, have been seen with HYLENEX recombinant."
        self.assertFalse(srlc.sentence_found(reworded, section, substring_only=True))
        self.assertTrue(srlc.sentence_found(reworded, section))               # 10 of 11 tokens >= 0.90 over the line pair
        self.assertFalse(srlc.sentence_found(reworded, section, line_coverage=1.0))
        self.assertEqual(srlc.token_coverage(srlc.tokens(reworded), section.pairs[1]), 10 / 11)

    def test_quote_presence_share(self):
        section = srlc.SectionText(self.TEXT)
        found = "Discontinue the infusion and treat the reaction, if it occurs, promptly."
        missing = "Report any new rash to the prescriber within one day of onset."
        r = srlc.quote_presence([found, missing], section)
        self.assertEqual((r["found"], r["sentences"]), (1, 2))
        self.assertFalse(r["present"])   # 11 of 23 tokens
        self.assertTrue(srlc.quote_presence([found, missing], section, quote_token_share=0.45)["present"])
        self.assertTrue(srlc.quote_presence([found], section)["present"])
        self.assertFalse(srlc.quote_presence([], section)["present"])


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sources = Path(self.tmp.name)
        self.approval = "2020-06-30"

    def tearDown(self):
        self.tmp.cleanup()

    def pin(self, setid: str, dates: list[str]) -> None:
        (self.sources / f"{setid}.json").write_text(json.dumps(
            {"setid": setid, "versions": [{"version": i + 1, "published_date": d} for i, d in enumerate(dates)]}))

    def cover(self, setid: str, status: str = "current") -> dict:
        return srlc.setid_coverage({"setid": setid, "inn": "x", "status": status}, self.approval, srlc.Pinned(self.sources))

    def test_boundaries_at_183_days(self):
        lo = (date(2020, 6, 30) - timedelta(days=183)).isoformat()
        hi = (date(2020, 6, 30) + timedelta(days=183)).isoformat()
        self.pin("a", [lo, "2021-01-01"])
        self.assertTrue(self.cover("a")["covered"])
        self.pin("b", [(date.fromisoformat(lo) + timedelta(days=1)).isoformat(), "2021-01-01"])
        self.assertFalse(self.cover("b")["covered"])
        self.assertEqual(self.cover("b")["reason"], "first version after approval - 183 days")
        self.pin("c", ["2010-01-01", hi])
        self.assertTrue(self.cover("c", "retired")["covered"])
        self.pin("d", ["2010-01-01", (date.fromisoformat(hi) - timedelta(days=1)).isoformat()])
        self.assertFalse(self.cover("d", "retired")["covered"])
        self.assertEqual(self.cover("d", "retired")["reason"], "retired before approval + 183 days")
        self.assertTrue(self.cover("d", "current")["covered"])   # a current setid needs no later version

    def test_missing_sources_file_is_not_covered(self):
        row = self.cover("nope")
        self.assertFalse(row["covered"])
        self.assertEqual(row["reason"], "no pinned versions in sources/us")
        self.assertEqual(row["versions"], 0)


class SampleTest(unittest.TestCase):
    @staticmethod
    def item(app: str, day: str, suppl: str, section: str) -> dict:
        return {"id": srlc.item_id(app, day, suppl, section), "application": app, "approval_date": day,
                "year": int(day[:4]), "section": section, "hash": srlc.sample_hash(app, day, suppl, section)}

    def test_hash_key(self):
        import hashlib
        expect = hashlib.sha256(b"asof-m4|NDA208194|2019-10-08|SUPPL19|WP").hexdigest()
        self.assertEqual(srlc.sample_hash("NDA208194", "2019-10-08", "19", "WP"), expect)

    def test_order_skips_and_shortfall(self):
        items = []
        for n in range(12):
            items.append(self.item(f"NDA{n % 4:06d}", f"2016-0{1 + n % 9}-10", str(n), "WP"))
        items.append(self.item("NDA000009", "2017-03-03", "1", "AR"))
        items.append(self.item("NDA000009", "2017-04-04", "2", "C"))
        items.append(self.item("NDA000009", "2018-05-05", "3", "C"))
        sample, per_year = srlc.select_sample(items)
        y2016 = [i for i in sample if i["year"] == 2016]
        self.assertEqual(len(y2016), 4)   # four applications only: one each
        self.assertEqual(len({i["application"] for i in y2016}), 4)
        ordered = sorted((i for i in items if i["year"] == 2016), key=lambda i: i["hash"])
        self.assertEqual(y2016[0]["id"], ordered[0]["id"])
        self.assertEqual(per_year["2016"]["shortfall"], 1)
        self.assertEqual(per_year["2017"]["taken"], 1)       # same application twice in one year: one taken
        self.assertEqual(per_year["2017"]["skipped"][0]["reason"], "application already taken this year")
        self.assertEqual(per_year["2018"]["taken"], 1)       # second overall is still allowed
        self.assertEqual(per_year["2019"], {"eligible": 0, "taken": 0, "shortfall": 5, "skipped": []})

    def test_two_overall_limit(self):
        items = [self.item("NDA000001", f"{y}-01-01", "1", "WP") for y in (2016, 2017, 2018)]
        sample, per_year = srlc.select_sample(items)
        self.assertEqual([i["year"] for i in sample], [2016, 2017])
        self.assertEqual(per_year["2018"]["skipped"][0]["reason"], "application already taken twice overall")

    def test_same_input_same_sample_and_sha(self):
        items = [self.item(f"NDA{n:06d}", "2016-02-02", "1", "WP") for n in range(8)]
        a, _ = srlc.select_sample(list(reversed(items)))
        b, _ = srlc.select_sample(items)
        self.assertEqual([i["id"] for i in a], [i["id"] for i in b])
        self.assertEqual(srlc.canonical_sha256(a), srlc.canonical_sha256(b))


class EndToEndTest(unittest.TestCase):
    """parse and select over the fixtures in a temporary raw dir, crosswalk and sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.raw, self.molecules, self.sources = root / "raw", root / "molecules", root / "sources"
        (self.raw / "search").mkdir(parents=True)
        (self.raw / "detail").mkdir()
        (self.raw / "search" / "2024-01.html").write_text(SEARCH_PAGE, encoding="utf-8")
        (self.raw / "detail" / "19.html").write_text(DETAIL_PAGE, encoding="utf-8")
        self.molecules.mkdir()
        (self.molecules / "hyaluronidase.json").write_text(json.dumps({"inn": "hyaluronidase", "us": [
            {"setid": "cur", "application_numbers": ["BLA021859"]},
            {"setid": "old", "application_numbers": ["BLA021859"], "status": "retired"},
            {"setid": "late", "application_numbers": ["BLA021859"]}]}))
        self.sources.mkdir()
        for setid, dates in {"cur": ["2012-05-05", "2023-09-09", "2024-03-01"], "old": ["2009-01-01", "2015-01-01"],
                             "late": ["2024-01-01"]}.items():
            (self.sources / f"{setid}.json").write_text(json.dumps(
                {"setid": setid, "versions": [{"version": i + 1, "published_date": d} for i, d in enumerate(dates)]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_then_select(self):
        items_path = self.raw / "items.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srlc.main(["parse", "--raw-dir", str(self.raw), "--molecules", str(self.molecules),
                                        "--out", str(items_path)]), 0)
        parsed = json.loads(items_path.read_text())
        self.assertEqual(parsed["srlc_drugs_seen"], 2)
        self.assertEqual(parsed["in_corpus_drugs"], 1)
        ids = [i["id"] for i in parsed["items"]]
        self.assertEqual(ids, ["srlc-BLA021859-20160121-SUPPL23-BW", "srlc-BLA021859-20160121-SUPPL23-DI",
                               "srlc-BLA021859-20240112-SUPPL31-C", "srlc-BLA021859-20240112-SUPPL31-PCI/PI/MG",
                               "srlc-BLA021859-20240112-SUPPL31-USP", "srlc-BLA021859-20240112-SUPPL31-WP"])
        usp = next(i for i in parsed["items"] if i["id"].endswith("-USP"))
        self.assertEqual(usp["posted_date"], "2024-01-17")
        self.assertIsNone(next(i for i in parsed["items"] if i["id"].endswith("-BW"))["posted_date"])
        self.assertEqual(usp["detail_urls"], [srlc.SRLC_DETAIL.format(drug_id="19")])
        pop, sample = self.raw / "pop.json", self.raw / "sample.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srlc.main(["select", "--items", str(items_path), "--molecules", str(self.molecules),
                                        "--sources", str(self.sources), "--population-out", str(pop),
                                        "--sample-out", str(sample)]), 0)
        population = json.loads(pop.read_text())
        self.assertEqual(population["items_2016_2025"], 6)
        self.assertEqual(population["funnel"], {"in_range": 6, "sampled_section": 5, "usable": 3,
                                                "with_covered_setid": 3, "eligible": 3})
        self.assertEqual(population["excluded"]["section_not_sampled"], {"PCI/PI/MG": 1})
        self.assertEqual(population["excluded"]["not_usable_by_comment_type"], {"none": 1, "refer_to_label": 1})
        doc = json.loads(sample.read_text())
        self.assertEqual(doc["rules"], srlc.RULES)
        self.assertEqual(doc["constants"]["WINDOW_DAYS"], 183)
        self.assertEqual(doc["constants"]["PARSE_VERSION"], 2)
        self.assertEqual(doc["constants"]["ABBREVIATIONS"], list(srlc.ABBREVIATIONS))
        self.assertEqual(doc["revisions"], srlc.REVISIONS)
        self.assertEqual(doc["generation"]["parse_version"], 2)
        self.assertEqual(doc["section_groups"]["USP"][:2], ["43684-0", "43685-7"])
        self.assertEqual(doc["per_year"]["2016"]["taken"], 1)
        self.assertEqual(doc["per_year"]["2024"]["taken"], 1)   # USP and WP share the application: one per year
        self.assertEqual(doc["per_year"]["2024"]["skipped"][0]["reason"], "application already taken this year")
        self.assertEqual(doc["sample_size"], 2)
        for it in doc["items"]:
            self.assertEqual([s["setid"] for s in it["covered_setids"]], ["cur"])
            self.assertEqual({s["setid"]: s["reason"] for s in it["uncovered_setids"]},
                             {"old": "retired before approval + 183 days", "late": "first version after approval - 183 days"})
            self.assertEqual(it["covered_setids"][0]["first_published"], "2012-05-05")
            self.assertIn("raw_html", it)
            self.assertTrue(it["kept_sentences"])
        self.assertEqual(doc["items_sha256"], srlc.canonical_sha256(doc["items"]))

    def test_select_refuses_stale_items(self):
        items_path = self.raw / "items.json"
        items_path.write_text(json.dumps({"parse_version": -1, "items": []}))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(srlc.main(["select", "--items", str(items_path), "--molecules", str(self.molecules),
                                        "--sources", str(self.sources), "--population-out", str(self.raw / "p.json"),
                                        "--sample-out", str(self.raw / "s.json")]), 2)


# ---------------------------------------------------------------- check (rules 4-8 and 11)

APPROVAL = "2020-06-30"


def seq(*pairs) -> list[tuple[str, bool]]:
    return [(d, bool(p)) for d, p in pairs]


class ClassifySequenceTest(unittest.TestCase):
    """Rule 7 on synthetic version sequences; lag = published - 2020-06-30."""

    def test_match_inside_the_window(self):
        r = srlc.classify_sequence(seq(("2018-01-01", 0), ("2020-08-01", 1), ("2021-01-01", 1)), APPROVAL)
        self.assertEqual(r["category"], "match")
        self.assertEqual(r["chosen"], {"index": 1, "published_date": "2020-08-01", "lag": 32})
        self.assertFalse(r["present_in_first_version"])

    def test_boundaries_at_plus_minus_183(self):
        lo = (date(2020, 6, 30) - timedelta(days=183)).isoformat()
        hi = (date(2020, 6, 30) + timedelta(days=183)).isoformat()
        self.assertEqual(srlc.classify_sequence(seq(("2018-01-01", 0), (hi, 1)), APPROVAL)["category"], "match")
        self.assertEqual(srlc.classify_sequence(seq(("2018-01-01", 0), (lo, 1)), APPROVAL)["category"], "match")
        day_late = (date.fromisoformat(hi) + timedelta(days=1)).isoformat()
        day_early = (date.fromisoformat(lo) - timedelta(days=1)).isoformat()
        self.assertEqual(srlc.classify_sequence(seq(("2018-01-01", 0), (day_late, 1)), APPROVAL)["category"], "late")
        self.assertEqual(srlc.classify_sequence(seq(("2018-01-01", 0), (day_early, 1)), APPROVAL)["category"], "early")
        self.assertEqual(srlc.classify_sequence(seq(("2018-01-01", 0), (day_late, 1)), APPROVAL, window_days=365)["category"], "match")

    def test_early_when_present_in_first_version(self):
        r = srlc.classify_sequence(seq(("2015-01-01", 1), ("2020-07-01", 1)), APPROVAL)
        self.assertEqual(r["category"], "early")
        self.assertTrue(r["present_in_first_version"])
        self.assertIsNone(r["chosen"])
        self.assertEqual(r["transitions"], [])

    def test_late(self):
        r = srlc.classify_sequence(seq(("2015-01-01", 0), ("2020-05-01", 0), ("2021-09-01", 1)), APPROVAL)
        self.assertEqual(r["category"], "late")
        self.assertEqual(r["chosen"]["lag"], 428)

    def test_absent(self):
        r = srlc.classify_sequence(seq(("2015-01-01", 0), ("2020-07-01", 0), ("2022-01-01", 0)), APPROVAL)
        self.assertEqual(r["category"], "absent")
        self.assertFalse(r["ever_present"])
        self.assertIsNone(r["chosen"])
        self.assertEqual(srlc.classify_sequence([], APPROVAL)["category"], "absent")

    def test_transition_choice_is_the_smallest_absolute_lag(self):
        # present, removed, re-added: two transitions, lags -1000ish and +40; the +40 one is chosen
        s = seq(("2010-01-01", 0), ("2017-01-01", 1), ("2019-01-01", 0), ("2020-08-09", 1), ("2021-01-01", 1))
        r = srlc.classify_sequence(s, APPROVAL)
        self.assertEqual([t["index"] for t in r["transitions"]], [1, 3])
        self.assertEqual(r["chosen"]["index"], 3)
        self.assertEqual(r["category"], "match")
        # the same text that never comes back after 2019 is early, by the first transition
        r = srlc.classify_sequence(seq(("2010-01-01", 0), ("2017-01-01", 1), ("2019-01-01", 0), ("2021-01-01", 0)), APPROVAL)
        self.assertEqual((r["category"], r["chosen"]["index"]), ("early", 1))

    def test_tie_prefers_the_earlier_transition(self):
        s = seq(("2010-01-01", 0), ("2020-06-20", 1), ("2020-06-25", 0), ("2020-07-10", 1))
        r = srlc.classify_sequence(s, APPROVAL)
        self.assertEqual([t["lag"] for t in r["transitions"]], [-10, 10])
        self.assertEqual(r["chosen"]["lag"], -10)

    def test_match_beats_early_when_quote_was_in_first_version(self):
        s = seq(("2010-01-01", 1), ("2015-01-01", 0), ("2020-07-15", 1))
        r = srlc.classify_sequence(s, APPROVAL)
        self.assertEqual(r["category"], "match")
        self.assertTrue(r["present_in_first_version"])

    def test_item_outcome_priority(self):
        self.assertEqual(srlc.item_outcome(["late", "match", "absent"]), "match")
        self.assertEqual(srlc.item_outcome(["absent", "late", "early"]), "late")
        self.assertEqual(srlc.item_outcome(["absent", "early"]), "early")
        self.assertEqual(srlc.item_outcome(["absent", "absent"]), "absent")
        self.assertEqual(srlc.item_outcome([]), "unchecked")


class SectionGroupTest(unittest.TestCase):
    def test_group_files_by_loinc_code(self):
        files = [("x/s/34066-1-boxed-warning.md", "a"), ("x/s/34073-7-drug-interactions.md", "b"),
                 ("x/s/43685-7-warnings-and-precautions.md", "c"), ("x/s/42228-7-pregnancy.md", "d"),
                 ("x/s/34084-4-adverse-reactions.md", "e")]
        self.assertEqual([b for _, b in srlc.group_files(files, "DI")], ["b", "c"])
        self.assertEqual([b for _, b in srlc.group_files(files, "USP")], ["c", "d"])
        self.assertEqual([b for _, b in srlc.group_files(files, "BW")], ["a"])
        self.assertEqual(srlc.group_files(files, "C"), [])

    def test_located_lines_cite_file_and_line(self):
        wp = "# 5 WARNINGS\n\n## 5.1 Falls\n  - •Dizziness and somnolence have been reported.\n\nAdvise patients not to drive.\n"
        di = "# 7 DRUG INTERACTIONS\n\nAlcohol increases the sedative effect of this drug markedly.\n"
        section = srlc.SectionText(files=[("x/s/34073-7-drug-interactions.md", di), ("x/s/43685-7-warnings-and-precautions.md", wp)])
        hit = srlc.locate_sentence("Alcohol increases the sedative effect of this drug markedly.", section)
        self.assertEqual(hit["how"], "substring")
        self.assertEqual(hit["lines"], [{"file": "34073-7-drug-interactions.md", "line": 3,
                                         "text": "Alcohol increases the sedative effect of this drug markedly."}])
        # a substring that runs over a line break (and the blank line after it) cites both lines, verbatim with indentation
        hit = srlc.locate_sentence("have been reported. Advise patients not to drive", section)
        self.assertEqual([(l["file"], l["line"]) for l in hit["lines"]], [("43685-7-warnings-and-precautions.md", 4),
                                                                           ("43685-7-warnings-and-precautions.md", 6)])
        self.assertEqual(hit["lines"][0]["text"], "  - •Dizziness and somnolence have been reported.")
        # coverage over a pair of consecutive non-empty lines: 10 of 11 tokens, across the blank line 5
        hit = srlc.locate_sentence("Dizziness and somnolence have been seen. Advise patients not to drive.", section)
        self.assertEqual(hit["how"], "coverage")
        self.assertEqual(hit["coverage"], round(10 / 11, 4))
        self.assertEqual([l["line"] for l in hit["lines"]], [4, 6])
        self.assertIsNone(srlc.locate_sentence("Nothing of the sort is written here at all.", section))
        # the last line of one file pairs with the first line of the next file in the group
        self.assertEqual(section.pairs[1], ["alcohol", "increases", "the", "sedative", "effect", "of", "this", "drug", "markedly",
                                            "5", "warnings"])

    def test_best_coverage_and_score(self):
        section = srlc.SectionText("Serious skin reactions have been reported in patients taking this drug.\nStop at the first sign of rash.\n")
        exact = "Stop at the first sign of rash."
        close = "Serious skin reactions were reported in patients taking this drug."   # 9 of 10 tokens
        far = "Blood pressure should be measured before every dose increase."
        self.assertEqual(srlc.best_coverage(exact, section)["coverage"], 1.0)
        self.assertEqual(srlc.best_coverage(close, section)["coverage"], 0.9)
        self.assertEqual(srlc.best_coverage(close, section)["lines"][0]["line"], 1)
        self.assertLess(srlc.best_coverage(far, section)["coverage"], 0.3)
        score = srlc.quote_score([exact, close], section)
        self.assertEqual(score["score"], round((7 * 1.0 + 10 * 0.9) / 17, 4))
        self.assertEqual(srlc.quote_score([], section)["score"], 0.0)

    def test_quote_presence_threshold_and_variants(self):
        section = srlc.SectionText("Serious skin reactions have been reported in patients taking this drug.\nStop at the first sign of rash.\n")
        close = "Serious skin reactions were reported in patients taking this drug."   # coverage 0.9: found pre-registered, not strict
        far = "Blood pressure should be measured before every dose increase."
        r = srlc.quote_presence([close, far], section, **srlc.PRESENCE_VARIANTS["preregistered"])
        self.assertEqual((r["found"], r["share"]), (1, round(10 / 19, 4)))
        self.assertFalse(r["present"])                                                # 0.53 < 0.80
        self.assertEqual(r["matches"][0]["how"], "coverage")
        self.assertEqual(r["matches"][1]["how"], None)
        self.assertFalse(srlc.quote_presence([close], section, **srlc.PRESENCE_VARIANTS["strict"])["present"])  # substring only
        self.assertTrue(srlc.quote_presence([close], section, **srlc.PRESENCE_VARIANTS["preregistered"])["present"])
        self.assertFalse(srlc.quote_presence([close, far], section, **srlc.PRESENCE_VARIANTS["loose"])["present"])  # 0.53 < 0.60
        self.assertTrue(srlc.quote_presence([close, far, close], section, **srlc.PRESENCE_VARIANTS["loose"])["present"])  # 20/29


def setid_row(setid: str, category: str, lag: int | None = None, first: bool = False, sensitivity: dict | None = None,
              first_present: dict | None = None) -> dict:
    """A walked covered setid as summarise_item sees it."""
    chosen = {"version": 9, "published_date": (date(2020, 6, 30) + timedelta(days=lag)).isoformat(), "lag": lag} if lag is not None else None
    sens = sensitivity or {"strict": category, "loose": category, "window_365": category}
    return {"setid": setid, "category": category, "chosen_transition": chosen, "present_in_first_version": first,
            "first_present_version": first_present or chosen, "title": f"{setid} label", "srlc_brand_in_label": True,
            "sequence": [{"version": 1, "published_date": "2010-01-01", "lag": -3833, "present": first}],
            "sensitivity": {k: {"category": v} for k, v in sens.items()}}


def shown_core(shown: dict) -> dict:
    return {k: v for k, v in shown.items() if k in ("setid", "version", "published_date", "lag", "first_version")}


def sample_item(**over) -> dict:
    base = {"id": "srlc-NDA000001-20200630-SUPPL1-WP", "brand": "X", "ingredient": "x", "application": "NDA000001",
            "approval_date": APPROVAL, "year": 2020, "supplement": "1", "section": "WP", "section_group_codes": ["43685-7"],
            "section_headings": [], "subsection_headings": [], "comment_type": "newly_added", "quote_tokens": 12,
            "kept_sentences": ["one two three four five six seven eight nine ten eleven twelve"], "detail_urls": []}
    return {**base, **over}


class SummariseItemTest(unittest.TestCase):
    def test_one_match_among_several_setids(self):
        rows = [setid_row("a", "late", 400), setid_row("b", "match", 20), setid_row("c", "match", -5), setid_row("d", "absent")]
        s = srlc.summarise_item(sample_item(), rows, [])
        self.assertEqual(s["outcome"], "match")
        self.assertEqual(shown_core(s["shown"]), {"setid": "c", "version": 9, "published_date": "2020-06-25", "lag": -5})   # smallest |lag|
        self.assertEqual((s["shown"]["title"], s["shown"]["srlc_brand_in_label"]), ("c label", True))
        self.assertEqual(s["match_lag"], -5)
        self.assertEqual(s["categories"], {"a": "late", "b": "match", "c": "match", "d": "absent"})
        self.assertTrue(s["short_quote"])      # 12 kept tokens <= SHORT_QUOTE_TOKENS
        self.assertFalse(srlc.summarise_item(sample_item(quote_tokens=16), rows, [])["short_quote"])

    def test_headline_priority_without_a_match(self):
        s = srlc.summarise_item(sample_item(), [setid_row("a", "absent"), setid_row("b", "late", 400)], [])
        self.assertEqual(s["outcome"], "late")
        self.assertEqual(s["shown"]["lag"], 400)
        self.assertIsNone(s["match_lag"])
        s = srlc.summarise_item(sample_item(), [setid_row("a", "early", first=True), setid_row("b", "absent")], [])
        self.assertEqual(s["outcome"], "early")
        self.assertEqual(shown_core(s["shown"]), {"setid": "a", "version": 1, "published_date": "2010-01-01", "lag": -3833, "first_version": True})
        s = srlc.summarise_item(sample_item(), [setid_row("a", "absent")], [])
        self.assertEqual((s["outcome"], s["shown"]), ("absent", None))

    def test_earlier_appearance_is_reported_next_to_the_chosen_transition(self):
        early = {"version": 3, "published_date": "2019-12-01", "lag": -212}
        s = srlc.summarise_item(sample_item(), [setid_row("a", "match", 20, first_present=early)], [])
        self.assertEqual(s["shown"]["earlier_appearance"], early)   # present, lost, regained: rule 7 reports the nearest transition
        self.assertEqual(s["match_lag"], 20)
        s = srlc.summarise_item(sample_item(), [setid_row("a", "match", 20)], [])
        self.assertNotIn("earlier_appearance", s["shown"])

    def test_sensitivity_outcomes_follow_the_same_priority(self):
        rows = [setid_row("a", "late", 300, sensitivity={"strict": "absent", "loose": "late", "window_365": "match"}),
                setid_row("b", "absent", sensitivity={"strict": "absent", "loose": "match", "window_365": "absent"})]
        s = srlc.summarise_item(sample_item(), rows, [])
        self.assertEqual(s["outcome"], "late")
        self.assertEqual(s["sensitivity"], {"strict": "absent", "loose": "match", "window_365": "match"})

    def test_setid_missing_from_repo_is_skipped(self):
        rows = [{"setid": "gone", "error": "setid not in build/us"}]
        self.assertEqual(srlc.summarise_item(sample_item(), rows, [])["outcome"], "unchecked")

    def test_lag_stats(self):
        self.assertEqual(srlc.lag_stats([]), {"n": 0})
        self.assertEqual(srlc.lag_stats([7]), {"n": 1, "min": 7, "max": 7, "median": 7, "mean": 7.0, "q1": 7, "q3": 7})
        st = srlc.lag_stats([-30, 5, 7, 12, 160])
        self.assertEqual((st["min"], st["median"], st["max"]), (-30, 7, 160))
        self.assertEqual((st["q1"], st["q3"]), (5.0, 12.0))


class BrandFlagTest(unittest.TestCase):
    def test_srlc_brand_in_label(self):
        climara = {"title": "CLIMARA (ESTRADIOL) PATCH [BAYER HEALTHCARE PHARMACEUTICALS INC.]", "brand_names": ["Climara"]}
        self.assertFalse(srlc.srlc_brand_in_label({"brand": "MENOSTAR", "brands": ["MENOSTAR"], "ingredient": "ESTRADIOL"}, climara))
        self.assertTrue(srlc.srlc_brand_in_label({"brand": "CLIMARA", "brands": ["CLIMARA", "MENOSTAR"]}, climara))
        dolophine = {"title": "DOLOPHINE (methadone hydrochloride) tablet", "brand_names": ["Dolophine"]}
        self.assertTrue(srlc.srlc_brand_in_label({"brand": "DOLOPHINE HYDROCHLORIDE", "brands": []}, dolophine))   # the brand word alone
        rhodes = {"title": "HYDROMORPHONE HYDROCHLORIDE (hydromorphone) solution [Rhodes]", "brand_names": []}
        self.assertFalse(srlc.srlc_brand_in_label({"brand": "DILAUDID", "brands": []}, rhodes))                     # authorized generic
        self.assertFalse(srlc.srlc_brand_in_label({"brand": "8-MOP", "brands": []}, {"title": "A product with 8 mg", "brand_names": []}))

    def test_crosswalk_rows_carry_title_and_brand_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.json").write_text(json.dumps({"inn": "x", "us": [
                {"setid": "s1", "application_numbers": ["NDA000001"], "title": "X (x) tablet [Maker]", "brand_names": ["X"]},
                {"setid": "s2", "application_numbers": ["NDA000001", "ANDA000002"], "status": "retired"}]}))
            apps = srlc.crosswalk_applications(Path(tmp))
        self.assertEqual(apps["NDA000001"][0], {"inn": "x", "setid": "s1", "status": "current", "title": "X (x) tablet [Maker]", "brand_names": ["X"]})
        self.assertEqual((apps["NDA000001"][1]["title"], apps["NDA000001"][1]["brand_names"], apps["NDA000001"][1]["status"]), ("", [], "retired"))
        self.assertNotIn("ANDA000002", apps)


class VerifySampleTest(unittest.TestCase):
    def test_pinned_sample_verifies_and_edits_are_refused(self):
        sample = json.loads(srlc.SAMPLE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(srlc.verify_sample(sample), srlc.SAMPLE_ITEMS_SHA256)
        edited = json.loads(json.dumps(sample))
        edited["items"][0]["approval_date"] = "2016-12-17"
        with self.assertRaises(srlc.SrlcError):
            srlc.verify_sample(edited)
        edited["items_sha256"] = srlc.canonical_sha256(edited["items"])    # a consistent but different sample
        with self.assertRaises(srlc.SrlcError):
            srlc.verify_sample(edited)

    def test_check_command_refuses_an_edited_sample(self):
        sample = json.loads(srlc.SAMPLE_PATH.read_text(encoding="utf-8"))
        del sample["items"][-1]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.json"
            path.write_text(json.dumps(sample))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = srlc.main(["check", "--sample", str(path), "--out-json", f"{tmp}/v.json", "--out-md", f"{tmp}/v.md"])
            self.assertEqual(code, 2)
            self.assertIn("the sample was edited", err.getvalue())
            self.assertFalse((Path(tmp) / "v.json").exists())


def make_repo(root: Path, labels_spec: dict[str, list[tuple[str, dict[str, str]]]]) -> None:
    """A tiny built repo: labels_spec = {setid: [(published date, {file name: text}), ...]}, committed in
    Published-Date order with the trailers labels.Index reads. Files not named in a version are removed."""
    import subprocess

    def git(*args):
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    events = sorted((published, setid, n + 1, files) for setid, versions in labels_spec.items()
                    for n, (published, files) in enumerate(versions))
    for published, setid, number, files in events:
        folder = root / "inn" / setid
        if folder.exists():
            for old in folder.iterdir():
                old.unlink()
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8")
        (folder / "label.json").write_text(json.dumps({"setid": setid}))
        git("add", "-A")
        msg = (f"Drug ({setid}) — label version {number}\n\nSource-Id: dailymed:{setid}\nSource-Version: {number}\n"
               f"Published-Date: {published}\nEffective-Time: {published}\nLabel-Format: PLR\n")
        git("commit", "-q", "--allow-empty", "-m", msg)


class WalkSetidTest(unittest.TestCase):
    """walk_setid over a tiny real git repo read through labels.Index."""
    QUOTE = ["Dizziness and somnolence have been reported with this drug.", "Advise patients not to drive until they know how it affects them."]
    OLD_WP = "# 5 WARNINGS AND PRECAUTIONS\n\n## 5.1 Rash\nSerious rash has been reported.\n"
    NEW_WP = OLD_WP + "\n## 5.2 Driving\nDizziness and somnolence have been reported with this drug.\nAdvise patients not to drive until they know how it affects them.\n"
    AR = "# 6 ADVERSE REACTIONS\n\nDizziness and somnolence have been reported with this drug.\nAdvise patients not to drive until they know how it affects them.\n"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "repo"
        make_repo(root, {
            # match: the quote arrives in the version of 2020-07-20 (lag +20); it also sits in Adverse Reactions from the start
            "aaaa": [("2015-01-01", {"43685-7-warnings-and-precautions.md": cls.OLD_WP, "34084-4-adverse-reactions.md": cls.AR}),
                     ("2020-07-20", {"43685-7-warnings-and-precautions.md": cls.NEW_WP, "34084-4-adverse-reactions.md": cls.AR}),
                     ("2021-01-01", {"43685-7-warnings-and-precautions.md": cls.NEW_WP, "34084-4-adverse-reactions.md": cls.AR})],
            # late: the quote arrives 2021-09-01 (lag +428)
            "bbbb": [("2015-01-01", {"43685-7-warnings-and-precautions.md": cls.OLD_WP}),
                     ("2021-09-01", {"43685-7-warnings-and-precautions.md": cls.NEW_WP})],
            # absent: never carries it
            "cccc": [("2015-01-01", {"43685-7-warnings-and-precautions.md": cls.OLD_WP}),
                     ("2020-07-01", {"43685-7-warnings-and-precautions.md": cls.OLD_WP + "Another unrelated line was added here.\n"})],
        })
        import labels
        cls.reader = srlc.LabelReader(labels.Index(root, Path(cls.tmp.name) / "no-molecules"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def walk(self, setid: str) -> dict:
        return srlc.walk_setid(self.reader, {"setid": setid, "inn": "inn", "status": "current", "versions": 3}, "WP",
                               self.QUOTE, APPROVAL)

    def test_match_with_cited_lines(self):
        r = self.walk("aaaa")
        self.assertEqual(r["category"], "match")
        self.assertEqual([(s["version"], s["present"]) for s in r["sequence"]], [(1, False), (2, True), (3, True)])
        self.assertEqual(r["sequence"][0]["group_files"], ["43685-7-warnings-and-precautions.md"])
        t = r["chosen_transition"]
        self.assertEqual((t["version"], t["published_date"], t["lag"], t["previous_version"]), (2, "2020-07-20", 20, 1))
        self.assertEqual(t["files"], ["43685-7-warnings-and-precautions.md"])
        self.assertEqual([m["lines"][0]["line"] for m in t["matched"]], [7, 8])
        self.assertEqual(r["score_at_chosen_version"]["score"], 1.0)
        self.assertNotIn("diagnostics", r)
        self.assertEqual(r["sensitivity"]["strict"]["category"], "match")
        self.assertEqual(r["sensitivity"]["window_365"]["chosen_transition"]["lag"], 20)
        self.assertEqual(r["first_present_version"], {"version": 2, "published_date": "2020-07-20", "lag": 20})
        self.assertIsNone(self.walk("cccc")["first_present_version"])

    def test_late_with_diagnostics(self):
        r = self.walk("bbbb")
        self.assertEqual(r["category"], "late")
        self.assertEqual(r["chosen_transition"]["lag"], 428)
        d = r["diagnostics"]
        self.assertEqual(d["versions_in_window"], [])
        self.assertEqual(d["version_in_force_at_approval"]["version"], 1)
        self.assertEqual(d["nearest_appearance"], {"kind": "transition", "version": 2, "published_date": "2021-09-01", "lag": 428})
        self.assertEqual(d["best_score_any_version"]["score"], 1.0)
        self.assertEqual(d["elsewhere_in_label"], [])
        self.assertEqual(r["sensitivity"]["window_365"]["category"], "late")
        self.assertEqual(r["chosen_version"]["why"], "chosen transition")

    def test_absent_finds_the_text_in_another_section(self):
        r = srlc.walk_setid(self.reader, {"setid": "aaaa", "inn": "inn", "status": "current", "versions": 3}, "C", self.QUOTE, APPROVAL)
        self.assertEqual(r["category"], "absent")          # no contraindications file at all
        self.assertEqual(r["sequence"][0]["group_files"], [])
        self.assertEqual([(e["section"], e["version"]) for e in r["diagnostics"]["elsewhere_in_label"]],
                         [("34084-4", 1), ("43685-7", 2)])
        self.assertEqual(r["chosen_version"]["why"], "version in force at approval + 183 days")
        r = self.walk("cccc")
        self.assertEqual(r["category"], "absent")
        self.assertFalse(r["ever_present"])
        self.assertLess(r["diagnostics"]["best_score_any_version"]["score"], 0.5)

    def test_missing_setid_is_reported_not_guessed(self):
        r = self.walk("zzzz")
        self.assertEqual(r["error"], "setid not in build/us")

    def test_item_summary_and_report_over_the_repo(self):
        item = sample_item(kept_sentences=self.QUOTE, quote_tokens=srlc.quote_tokens(self.QUOTE),
                           covered_setids=[{"setid": "bbbb", "inn": "inn", "status": "current", "versions": 2, "covered": True, "reason": None},
                                           {"setid": "cccc", "inn": "inn", "status": "current", "versions": 2, "covered": True, "reason": None}],
                           uncovered_setids=[{"setid": "aaaa", "inn": "inn", "status": "current", "versions": 3, "covered": False,
                                              "reason": "first version after approval - 183 days"}])
        sample = {"items": [item], "items_sha256": srlc.canonical_sha256([item]), "generated": "2026-01-01", "sample_size": 1,
                  "rules": srlc.RULES, "window_rationale": srlc.WINDOW_RATIONALE, "constants": srlc.constants(),
                  "section_groups": {k: list(v) for k, v in srlc.SECTION_GROUPS.items()}}
        notes = {item["id"]: {"category": "late", "explanation": "The text arrived 428 days late.", "evidence": "v2 2021-09-01"}}
        report = srlc.run_check(sample, self.reader.index, notes, log=lambda *_: None)
        it = report["items"][0]
        self.assertEqual(it["outcome"], "late")
        self.assertEqual(it["categories"], {"bbbb": "late", "cccc": "absent"})
        self.assertEqual([o["category"] for o in it["other_setids"]], ["match"])   # the uncovered setid did carry it in the window
        self.assertFalse(it["other_setids"][0]["covered"])
        self.assertEqual(report["headline"], {**report["headline"], "matches": 0, "items": 1, "match_rate": 0.0})
        self.assertEqual(report["by_category"], {"late": 1})
        self.assertEqual(report["sensitivity"]["window_365"]["matches"], 0)
        self.assertEqual(report["misses"][0]["explanation"], "The text arrived 428 days late.")
        self.assertEqual(report["misses_without_note"], [])
        self.assertEqual(report["setids"], {"covered_entries": 2, "covered_distinct": 2, "other_entries": 1, "other_distinct": 1,
                                            "missing_from_repo": 0})
        self.assertEqual(report["caveats"], {"short_quote_items": [], "shown_in_label_without_srlc_brand": [], "earlier_appearance": []})
        self.assertEqual(report["sample"]["revisions"], [])
        md = srlc.render_markdown(report)
        self.assertIn("**0 of 1 sampled SrLC changes match (0%)", md)
        self.assertIn("2 covered setid entries (2 distinct) and 1 uncovered setids", md)
        self.assertIn("## Caveats on individual items", md)
        self.assertIn("## Misses explained", md)
        self.assertIn("The text arrived 428 days late. Evidence: v2 2021-09-01", md)
        for line in md.splitlines():
            self.assertFalse(line.startswith("  "), "markdown must not be hard-wrapped")

    def test_report_caveats_flag_short_quotes_and_sibling_labels(self):
        short = ["Advise patients not to drive until they know how it affects them."]   # 12 tokens
        item = sample_item(kept_sentences=short, quote_tokens=srlc.quote_tokens(short),
                           covered_setids=[{"setid": "aaaa", "inn": "inn", "status": "current", "versions": 3, "covered": True, "reason": None,
                                            "title": "OTHER BRAND (drug) tablet", "srlc_brand_in_label": False}],
                           uncovered_setids=[])
        sample = {"items": [item], "items_sha256": srlc.canonical_sha256([item]), "generated": "2026-01-01", "sample_size": 1,
                  "rules": srlc.RULES, "window_rationale": srlc.WINDOW_RATIONALE, "constants": srlc.constants(),
                  "section_groups": {k: list(v) for k, v in srlc.SECTION_GROUPS.items()},
                  "revisions": [{"parse_version": 2, "date": "2026-11-01", "previous_items_sha256": "abc", "reason": "A parse fix.",
                                 "changes": [{"year": 2016, "removed": "srlc-old", "added": "srlc-new", "why": "hash order"}]}]}
        report = srlc.run_check(sample, self.reader.index, {}, log=lambda *_: None)
        it = report["items"][0]
        self.assertEqual(it["outcome"], "match")
        self.assertEqual((it["shown"]["srlc_brand_in_label"], it["shown"]["title"]), (False, "OTHER BRAND (drug) tablet"))
        self.assertEqual([e["id"] for e in report["caveats"]["short_quote_items"]], [item["id"]])
        self.assertEqual([e["setid"] for e in report["caveats"]["shown_in_label_without_srlc_brand"]], ["aaaa"])
        md = srlc.render_markdown(report)
        self.assertIn("Short quotes (1 item with", md)
        self.assertIn("shown in `aaaa` 'OTHER BRAND (drug) tablet'", md)
        self.assertIn("Sample revision (parse version 2, 2026-11-01). A parse fix. Items replaced", md)
        self.assertIn("`srlc-old` -> `srlc-new` (2016)", md)
        self.assertIn("revised once after results", md)
        self.assertIn(f"(`{report['repo']['path']}` at `{report['repo']['head'][:12]}`)", md)
        self.assertNotIn("M4", md)
        self.assertIn("| Version (setid) |", md)

    def test_build_note_replaces_path_and_head(self):
        sample = {"items": [], "rules": {}, "window_rationale": "", "constants": {}, "section_groups": {},
                  "items_sha256": "x", "generated": "2026-01-01", "sample_size": 0}
        report = srlc.run_check(sample, self.reader.index, {}, log=lambda *_: None, build_note="pre-release")
        self.assertEqual(report["repo"]["note"], "pre-release")
        md = srlc.render_markdown(report)
        self.assertIn(f"Run on the US corpus build of {report['repo']['build_date']} (pre-release):", md)
        self.assertNotIn(report["repo"]["head"][:12], md)


if __name__ == "__main__":
    unittest.main()
