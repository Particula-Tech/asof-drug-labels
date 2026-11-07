"""Archive search: page parsing, search terms and the keep/reject rules for retired setids."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import archive  # noqa: E402
from test_build import archive_page  # noqa: E402

SETID = "c6e131fe-e7df-4876-83f7-9156fc4e8228"

INFO_2022 = f"""<html><head><title>LIPITOR- atorvastatin calcium tablet, film coated</title></head><body>
<td>Marketing Category</td><td>Application Number or Monograph Citation</td>
<td>NDA</td><td>NDA020702</td><td>12/17/1996</td><td>04/30/2025</td>
<td>Item Code (Source)</td><td>NDC:0071-0155</td>
<td>1</td><td>NDC:0071-0155-23</td><td>90 in 1 BOTTLE</td>
<p>Labeler - Parke-Davis Div of Pfizer Inc (829076962)</p>
<p>Revised: 12/2022 Document Id: 202f0996-09c4-4740-a68d-31a15536ce30 34391-3
Set id: {SETID} Version: 42 Effective Time: 20221201</p></body></html>"""

INFO_REPACKAGED = """<html><body>
<td>NDA</td><td>NDA020702</td><td>12/17/1996</td>
<td>Item Code (Source)</td><td>NDC:55154-2421(NDC:0071-0155)</td>
<p>Labeler - Cardinal Health (188557102)</p>
<p>Establishment Name Address ID/FEI Business Operations Cardinal Health 188557102 REPACK(55154-2421)</p>
<p>Set id: e480e538-4ea7-47e6-9e5d-dc0f8179e06c Version: 6 Effective Time: 20171201</p></body></html>"""

INFO_2012 = """<html><body><td>NDA</td><td>NDA020702</td><td>04/07/2000</td>
<p>Labeler - Parke-Davis Division of Pfizer Inc (829076962)</p><p>Revised: 02/2012</p></body></html>"""


class ParseTest(unittest.TestCase):
    def test_search_page_rows_and_total(self):
        total, rows = archive.parse_search(archive_page([("Nov 22, 2024", 847065, 16), ("Apr 26, 2024", 795872, 1)]))
        self.assertEqual(total, 2)
        self.assertEqual(rows[0], archive.Row("2024-11-22", 847065, "TEST (test) tablet", "Test Inc.", 16))

    def test_empty_search_page(self):
        self.assertEqual(archive.parse_search("<h2>Search results</h2><p>No results</p>"), (0, []))

    def test_rows_without_a_count_mean_the_layout_changed(self):
        page = archive_page([("Nov 22, 2024", 1, 1)]).replace("(1 results)", "")
        with self.assertRaises(archive.ArchiveError):
            archive.parse_search(page)

    def test_info_page_of_an_approval_holder(self):
        info = archive.parse_info(INFO_2022, 7)
        self.assertEqual((info.setid, info.version), (SETID, 42))
        self.assertEqual(info.labeler, "Parke-Davis Div of Pfizer Inc")
        self.assertEqual(info.applications, ["NDA020702"])
        self.assertEqual(info.product_ndcs, ["0071-0155"])          # the package NDC is not a product NDC
        self.assertEqual(info.marketing_end_dates, ["2025-04-30"])
        self.assertFalse(info.repackaged)

    def test_info_page_of_a_repackager(self):
        info = archive.parse_info(INFO_REPACKAGED, 8)
        self.assertEqual(info.product_ndcs, ["55154-2421"])
        self.assertEqual(info.source_ndcs, ["0071-0155"])
        self.assertTrue(info.repackaged)

    def test_old_info_page_has_no_setid(self):
        info = archive.parse_info(INFO_2012, 9)
        self.assertEqual((info.setid, info.version), ("", 0))
        self.assertEqual(info.applications, ["NDA020702"])


class TermTest(unittest.TestCase):
    def test_search_term_is_the_distinctive_first_word(self):
        self.assertEqual(archive.search_term("ADVAIR DISKUS"), "ADVAIR")
        self.assertEqual(archive.search_term("K-TAB"), "K-TAB")
        self.assertEqual(archive.search_term("ZESTRIL"), "ZESTRIL")

    def test_title_match_is_a_whole_brand_prefix(self):
        self.assertTrue(archive.title_matches("ZESTRIL (lisinopril) tablet", "ZESTRIL"))
        self.assertTrue(archive.title_matches("ADVAIR DISKUS (fluticasone and salmeterol) powder", "ADVAIR"))
        self.assertFalse(archive.title_matches("ZESTORETIC (lisinopril and hydrochlorothiazide) tablet", "ZESTRIL"))

    def test_terms_from_the_selection_and_drugsfda_skip_generic_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            mol = {"inn": "levofloxacin", "name": "LEVOFLOXACIN", "ingredient_names": ["LEVOFLOXACIN"],
                   "us": [{"setid": "x", "brand_names": ["LEVOFLOXACIN"], "marketing_categories": ["NDA"],
                           "application_numbers": ["NDA200001"]}]}
            (Path(tmp) / "levofloxacin.json").write_text(json.dumps(mol))
            drugsfda = [{"application_number": "NDA020634", "products": [
                {"brand_name": "LEVAQUIN", "marketing_status": "Discontinued",
                 "active_ingredients": [{"name": "LEVOFLOXACIN"}]}]},
                {"application_number": "ANDA076000", "products": [
                    {"brand_name": "LEVOFLOXACIN", "marketing_status": "Prescription",
                     "active_ingredients": [{"name": "LEVOFLOXACIN"}]}]}]
            terms = archive.search_terms(Path(tmp), drugsfda)
        self.assertEqual([t.term for t in terms], ["LEVAQUIN"])
        self.assertEqual(terms[0].applications, {"NDA020634"})
        self.assertEqual(terms[0].molecules, {"levofloxacin"})
        self.assertEqual(terms[0].by_molecule, {"levofloxacin": {"brands": {"LEVAQUIN"}, "applications": {"NDA020634"}}})


class DecideTest(unittest.TestCase):
    LABELERS = archive.LabelerIndex(holder_codes={"0071", "50458"}, repackager_codes={"55154"},
                                    holder_names={"PFIZER LABORATORIES"}, repackager_names={"CARDINAL HEALTH"})
    TERM = {"term": "LIPITOR", "molecules": ["atorvastatin"], "brands": ["LIPITOR"], "applications": ["NDA020702"],
            "by_molecule": {"atorvastatin": {"brands": ["LIPITOR"], "applications": ["NDA020702"]}}}
    LISTING = [{"archive_id": 1, "posted": "2024-05-02", "title": "LIPITOR (atorvastatin calcium) tablet",
                "packager": "Parke-Davis Div of Pfizer Inc", "version": 42}]

    def info(self, **kw):
        base = {"labeler": "Parke-Davis Div of Pfizer Inc", "applications": ["NDA020702"],
                "product_ndcs": ["0071-0155"], "repackaged": False, "source_ndcs": []}
        return {**base, **kw}

    def decide(self, info, current=(), listing=None):
        return archive.decide(SETID, info, self.LISTING if listing is None else listing, self.TERM, self.LABELERS,
                              set(current), {"atorvastatin": {"NDA020702"}}, {"atorvastatin": {"ATORVASTATIN"}})

    def test_keeps_a_retired_holder_setid_under_the_molecules_application(self):
        d = self.decide(self.info())
        self.assertEqual(d["keep_for"], ["atorvastatin"])
        self.assertNotIn("labeler_unverified", d)

    def test_rejects_a_current_setid(self):
        self.assertEqual(self.decide(self.info(), current={SETID})["keep_for"], [])

    def test_rejects_a_repackaged_label(self):
        d = self.decide(self.info(repackaged=True, source_ndcs=["0071-0155"], product_ndcs=["55154-2421"]))
        self.assertEqual(d["keep_for"], [])
        self.assertIn("repackager", d["reason"])

    def test_rejects_a_repackager_code_even_without_markers(self):
        self.assertEqual(self.decide(self.info(product_ndcs=["55154-1000"], labeler="Some Name"))["keep_for"], [])

    def test_rejects_another_application(self):
        d = self.decide(self.info(applications=["NDA999999"]))
        self.assertEqual(d["keep_for"], [])
        self.assertIn("not the molecule's", d["reason"])

    def test_keeps_the_brand_itself_when_no_application_is_stated(self):
        d = self.decide(self.info(applications=[], product_ndcs=["0045-0065"], labeler="OMP Division"))
        self.assertEqual(d["keep_for"], ["atorvastatin"])
        self.assertTrue(d["application_unstated"])
        self.assertTrue(d["labeler_unverified"])

    def test_a_shared_search_word_does_not_pool_applications_across_molecules(self):
        # CIPRO (ciprofloxacin) and CIPRO HC (ciprofloxacin + hydrocortisone) share the search word CIPRO.
        term = {"term": "CIPRO", "molecules": ["ciprofloxacin", "hydrocortisone"], "brands": ["CIPRO", "CIPRO HC"],
                "applications": ["NDA019537", "NDA020805"],
                "by_molecule": {"ciprofloxacin": {"brands": ["CIPRO", "CIPRO HC"],
                                                  "applications": ["NDA019537", "NDA020805"]},
                                "hydrocortisone": {"brands": ["CIPRO HC"], "applications": ["NDA020805"]}}}
        mol_apps = archive.molecule_applications(Path(tempfile.mkdtemp()), [term])
        names = {"ciprofloxacin": {"CIPROFLOXACIN"}, "hydrocortisone": {"HYDROCORTISONE"}}
        listing = [{**self.LISTING[0], "title": "CIPRO (ciprofloxacin hydrochloride) tablet"}]
        tablet = archive.decide(SETID, self.info(applications=["NDA019537"]), listing, term, self.LABELERS, set(), mol_apps, names)
        self.assertEqual(tablet["keep_for"], ["ciprofloxacin"])
        otic = archive.decide(SETID, self.info(applications=["NDA020805"]), listing, term, self.LABELERS, set(), mol_apps, names)
        self.assertEqual(otic["keep_for"], ["ciprofloxacin", "hydrocortisone"])
        unstated = archive.decide(SETID, self.info(applications=[]), listing, term, self.LABELERS, set(), mol_apps, names)
        self.assertEqual(unstated["keep_for"], ["ciprofloxacin"])     # brand CIPRO belongs to ciprofloxacin only

    def test_a_reused_brand_name_without_application_must_name_the_molecule(self):
        term = {"term": "LODINE", "molecules": ["etodolac"], "brands": ["LODINE"], "applications": ["NDA018922"],
                "by_molecule": {"etodolac": {"brands": ["LODINE"], "applications": ["NDA018922"]}}}
        names = {"etodolac": {"ETODOLAC"}}
        def keep(title, brands=("LODINE",)):
            t = {**term, "by_molecule": {"etodolac": {"brands": list(brands), "applications": ["NDA018922"]}}}
            listing = [{**self.LISTING[0], "title": title}]
            return archive.decide(SETID, self.info(applications=[]), listing, t, self.LABELERS, set(), {},
                                  names)["keep_for"]
        self.assertEqual(keep("lodine (Etodolac) capsule lodine (Etodolac) tablet"), ["etodolac"])
        self.assertEqual(keep("LODINE (povidone-iodine) swab"), [])
        self.assertEqual(keep("lodine (etodolac) tablet", brands=["LODINE XL"]), ["etodolac"])   # bare stem
        self.assertEqual(keep("LODINE COLD (etodolac, phenylephrine) tablet"), [])   # added words: not the brand
        self.assertEqual(keep("LODINE STOCK SERUM liquid"), [])                       # no generic name either
        self.assertEqual(keep("lodine (etodalac) tablet"), ["etodolac"])          # typo in the archived title

    def test_rejects_an_empty_listing(self):
        self.assertEqual(self.decide(self.info(), listing=[])["keep_for"], [])


class MergeDiscoveryTest(unittest.TestCase):
    def test_only_run_is_merged_into_the_full_report(self):
        old = {"generated": "2026-11-01T00:00:00Z", "requests": 100, "inputs": {"x": 1},
               "terms": [{"term": "LIPITOR", "setids": ["a"]}, {"term": "ZESTRIL", "setids": ["b"]}],
               "setids": {"a": {"v": 1}, "b": {"v": 2}}}
        new = {"generated": "2026-11-03T00:00:00Z", "requests": 7, "inputs": {"x": 2},
               "terms": [{"term": "ELIQUIS", "setids": ["c"]}, {"term": "zestril", "setids": []}],
               "setids": {"c": {"v": 3}}}
        got = archive.merge_discovery(old, new)
        self.assertEqual([t["term"] for t in got["terms"]], ["ELIQUIS", "LIPITOR", "zestril"])
        self.assertEqual(sorted(got["setids"]), ["a", "b", "c"])          # earlier setids are never dropped
        self.assertEqual((got["requests"], got["inputs"]), (107, {"x": 2}))
        self.assertEqual([r["terms"] for r in got["runs"]], ["all", ["ELIQUIS", "zestril"]])


if __name__ == "__main__":
    unittest.main()
