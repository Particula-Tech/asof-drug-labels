"""EU side of tools/eu.py: medicines JSON, product selection, EU-number source, Union Register
product pages, annex URLs, resume behaviour, forward-capture change detection, and the build/eu
commit model (planning, trailers, fast-import determinism, known readings).

Standard library only; runs under the system python3. No network: fetches go through a fake opener.
Build tests that convert real annexes (rebuild determinism on a pinned subset, known readings) call
.venv-eu/bin/python themselves through eu.convert_pdfs and skip when .venv-eu, PyMuPDF or the
raw/eu PDFs are absent.
Tests on the Keytruda and Eliquis pages saved in the pilot (m0-data/eu/, gitignored) skip when those files
are absent; the Ozempic page is copied into eu_fixtures/ so the parser is always tested.
"""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import build  # noqa: E402
import eu  # noqa: E402
import notices  # noqa: E402

FIX = Path(__file__).parent / "eu_fixtures"
M0 = TOOLS.parent / "m0-data" / "eu"


def row(name, number, inn, status="Authorised", generic="No", biosimilar="No", date="01/01/2010",
        category="Human"):
    return {"category": category, "name_of_medicine": name, "ema_product_number": number,
            "medicine_status": status, "international_non_proprietary_name_common_name": inn,
            "active_substance": inn, "generic": generic, "biosimilar": biosimilar,
            "marketing_authorisation_date": date, "medicine_url": f"https://www.ema.europa.eu/en/medicines/human/EPAR/{name.lower()}",
            "marketing_authorisation_developer_applicant_holder": "Holder", "revision_number": "3",
            "last_updated_date": "30/10/2026"}


class MedicinesJsonTest(unittest.TestCase):
    def test_load_sample_and_fields(self):
        rows = eu.load_medicines((FIX / "ema_medicines_sample.json").read_bytes())
        self.assertEqual({r["name_of_medicine"] for r in rows}, {"Eliquis", "Keytruda", "Ozempic"})
        p = eu.product_summary(next(r for r in rows if r["name_of_medicine"] == "Eliquis"))
        self.assertEqual(p["marketing_authorisation_date"], "2011-05-18")
        self.assertEqual(p["ema_product_number"], "EMEA/H/C/002148")

    def test_rejects_wrong_shape(self):
        with self.assertRaises(eu.EUError):
            eu.load_medicines(b'{"rows": []}')
        with self.assertRaises(eu.EUError):
            eu.load_medicines(b'{"data": [{"category": "Human"}]}')

    def test_norm_split_and_dates(self):
        self.assertEqual(eu.norm("Insulin  Human (rDNA)"), "insulin human rdna")
        self.assertEqual(eu.split_inn("emtricitabine / tenofovir disoproxil"),
                         {"emtricitabine", "tenofovir disoproxil"})
        self.assertEqual(eu.split_inn("abacavir;lamivudine"), {"abacavir", "lamivudine"})
        self.assertEqual(eu.split_inn("lopinavir and ritonavir, x"), {"lopinavir", "ritonavir", "x"})
        self.assertEqual(eu.split_inn("albumin-aggregated"), {"albumin aggregated"})
        self.assertEqual(eu.parse_dmy("18/05/2011"), "2011-05-18")
        self.assertIsNone(eu.parse_dmy(""))

    def test_ema_numbers(self):
        self.assertEqual(eu.canonical_ema("EMEA/H/C/2148/X/4/G"), "EMEA/H/C/002148")
        self.assertEqual(eu.ema_dir("EMEA/H/C/002148"), "EMEA-H-C-002148")
        with self.assertRaises(eu.EUError):
            eu.canonical_ema("EMEA/V/C/000001")


class SelectionTest(unittest.TestCase):
    MOLS = [{"inn": "albumin-aggregated", "name": "ALBUMIN AGGREGATED"}, {"inn": "drugx", "name": "DRUGX"},
            {"inn": "drugy", "name": "DRUGY"}, {"inn": "drugz", "name": "DRUGZ"}, {"inn": "none", "name": "NONE"},
            {"inn": "gone", "name": "GONE"}]

    def setUp(self):
        rows = [
            row("Later", "EMEA/H/C/000300", "drugx", date="01/01/2012"),
            row("Tie2", "EMEA/H/C/000200", "drugx", date="01/01/2008"),
            row("Tie1", "EMEA/H/C/000100", "DrugX", date="01/01/2008"),
            row("GenericX", "EMEA/H/C/000050", "drugx", generic="Yes", date="01/01/2001"),
            row("Combo", "EMEA/H/C/000400", "drugx / drugz"),
            row("Salt", "EMEA/H/C/000450", "drugx sodium"),
            row("OnlyBio", "EMEA/H/C/000500", "drugy", biosimilar="Yes"),
            row("ComboZ", "EMEA/H/C/000600", "drugz;other"),
            row("Withdrawn", "EMEA/H/C/000700", "gone", status="Withdrawn"),
            row("Vet", "EMEA/V/C/000800", "drugx", category="Veterinary"),
            row("Mixed", "EMEA/H/C/000900", "Albumin aggregated"),
        ]
        self.sel = eu.select_products(rows, self.MOLS)

    def test_reference_is_earliest_non_generic_with_lowest_number_on_ties(self):
        e = self.sel["drugx"]
        self.assertEqual(e["reference"]["name"], "Tie1")
        self.assertEqual([a["name"] for a in e["alternates"]], ["GenericX", "Tie2", "Later"])
        self.assertEqual([c["name"] for c in e["combinations"]], ["Combo"])
        self.assertEqual([n["name"] for n in e["near_misses_not_matched"]], ["Salt"])

    def test_inn_matched_after_normalisation(self):
        self.assertEqual(self.sel["albumin-aggregated"]["reference"]["name"], "Mixed")

    def test_reasons_without_reference(self):
        self.assertEqual(self.sel["drugy"]["no_reference_reason"],
                         "only generic or biosimilar central products under this exact INN")
        self.assertEqual(self.sel["drugy"]["alternates"][0]["name"], "OnlyBio")
        self.assertTrue(self.sel["drugz"]["no_reference_reason"].startswith("only combination"))
        self.assertEqual(self.sel["gone"]["no_reference_reason"],
                         "no authorised central product under this exact INN (only withdrawn ones)")
        self.assertEqual(self.sel["none"]["no_reference_reason"],
                         "no central product with this exact INN in the EMA medicines JSON")
        self.assertIsNone(self.sel["drugx"]["no_reference_reason"])

    def test_salt_alias_v2_reference_when_exact_inn_finds_none(self):
        rows = [row("Reyataz", "EMEA/H/C/000494", "atazanavir sulfate", date="02/03/2004"),
                row("Atazanavir Viatris", "EMEA/H/C/004048", "atazanavir", generic="Yes", date="22/08/2016"),
                row("Later salt", "EMEA/H/C/000600", "atazanavir sulfate", date="02/03/2010"),
                row("Generic salt", "EMEA/H/C/000300", "atazanavir sulfate", generic="Yes", date="02/03/2001"),
                row("Ikervis", "EMEA/H/C/002066", "ciclosporin", date="19/03/2015"),
                row("Pegdrug", "EMEA/H/C/000777", "drugq pegol"),
                row("Exact", "EMEA/H/C/000901", "drugw", date="01/01/2015"),
                row("Older salt", "EMEA/H/C/000902", "drugw hydrochloride", date="01/01/2005")]
        mols = [{"inn": "atazanavir", "name": "ATAZANAVIR"}, {"inn": "cyclosporine", "name": "CYCLOSPORINE"},
                {"inn": "drugq", "name": "DRUGQ"}, {"inn": "drugw", "name": "DRUGW"}]
        sel = eu.select_products(rows, mols, overrides={})
        ata = sel["atazanavir"]
        # the generic salt product is older but generic; the earliest non-generic salt product wins
        self.assertEqual((ata["reference"]["name"], ata["reference"]["selected_by"]), ("Reyataz", "salt-alias-v2"))
        self.assertIsNone(ata["no_reference_reason"])
        self.assertEqual([a["name"] for a in ata["alternates"]], ["Generic salt", "Later salt", "Atazanavir Viatris"])
        self.assertEqual(ata["near_misses_not_matched"], [])
        cyc = sel["cyclosporine"]["reference"]
        self.assertEqual((cyc["name"], cyc["selected_by"], cyc["near_miss_kind"]), ("Ikervis", "salt-alias-v2", "alias"))
        self.assertIsNone(sel["drugq"]["reference"])                         # a variant never becomes the reference
        self.assertEqual(sel["drugq"]["near_misses_not_matched"][0]["kind"], "variant")
        self.assertNotIn("not matched", sel["drugq"]["no_reference_reason"])
        w = sel["drugw"]                                                      # exact INN still comes first
        self.assertEqual((w["reference"]["name"], w["reference"]["selected_by"]), ("Exact", "exact-inn"))
        self.assertEqual(w["near_misses_not_matched"][0]["name"], "Older salt")
        self.assertEqual(self.sel["drugx"]["near_misses_not_matched"][0]["kind"], "salt")

    def test_reference_overrides(self):
        rows = [row("Yentreve", "EMEA/H/C/000545", "duloxetine", date="11/08/2004"),
                row("Cymbalta", "EMEA/H/C/000572", "duloxetine", date="17/12/2004"),
                row("Rolufta Ellipta", "EMEA/H/C/004654", "umeclidinium", date="20/03/2017"),
                row("Incruse Ellipta", "EMEA/H/C/002809", "umeclidinium bromide", date="28/04/2014")]
        mols = [{"inn": "duloxetine", "name": "DULOXETINE", "us_brands": ["CYMBALTA"]},
                {"inn": "umeclidinium", "name": "UMECLIDINIUM", "us_brands": ["INCRUSE ELLIPTA"]}]
        plain = eu.select_products(rows, mols, overrides={})
        self.assertEqual(plain["duloxetine"]["reference"]["name"], "Yentreve")
        self.assertEqual(plain["umeclidinium"]["reference"]["name"], "Rolufta Ellipta")
        sel = eu.select_products(rows, mols)                                  # REFERENCE_OVERRIDES
        dul, ume = sel["duloxetine"], sel["umeclidinium"]
        self.assertEqual((dul["reference"]["name"], dul["reference"]["selected_by"]), ("Cymbalta", "override"))
        self.assertEqual([a["name"] for a in dul["alternates"]], ["Yentreve"])
        self.assertEqual(dul["reference"]["comparable_to_us"], "yes")
        self.assertEqual((ume["reference"]["name"], ume["reference"]["selected_by"]), ("Incruse Ellipta", "override"))
        self.assertEqual([a["name"] for a in ume["alternates"]], ["Rolufta Ellipta"])
        self.assertEqual(ume["near_misses_not_matched"], [])
        with self.assertRaises(eu.EUError):
            eu.select_products(rows, mols, overrides={"duloxetine": "EMEA/H/C/009999"})

    def test_near_miss_kind(self):
        self.assertEqual(eu.near_miss_kind("dasatinib (anhydrous)".replace("(", "").replace(")", ""),
                                           {"dasatinib"}, set()), "salt")
        self.assertEqual(eu.near_miss_kind("hydroxycarbamide", {"hydroxyurea"}, {"hydroxycarbamide"}), "alias")
        self.assertEqual(eu.near_miss_kind("estradiol valerate", {"estradiol"}, set()), "variant")
        self.assertIsNone(eu.near_miss_kind("drugzz", {"drugz"}, set()))

    def test_comparability(self):
        rows = [row("Brandx", "EMEA/H/C/000123", "drugc"), row("Nexium Control", "EMEA/H/C/002618", "esomeprazole"),
                row("Otherx", "EMEA/H/C/000124", "druge")]
        mols = [{"inn": "drugc", "name": "DRUGC", "us_brands": ["BRANDX XR", "Brandx"]},
                {"inn": "esomeprazole", "name": "ESOMEPRAZOLE", "us_brands": ["Nexium"]},
                {"inn": "druge", "name": "DRUGE", "us_brands": ["Elsewise"]}]
        sel = eu.select_products(rows, mols)
        self.assertEqual(sel["drugc"]["reference"]["comparable_to_us"], "yes")
        self.assertIn("BRANDX XR", sel["drugc"]["reference"]["comparability_note"])
        nex = sel["esomeprazole"]["reference"]   # the hand caveat wins over the brand match
        self.assertEqual(nex["comparable_to_us"], "no")
        self.assertIn("non-prescription", nex["comparability_note"])
        self.assertEqual(sel["druge"]["reference"]["comparable_to_us"], "unknown")
        self.assertEqual(eu.comparability({"name": "Plavix", "ema_product_number": "EMEA/H/C/000175"}, [])
                         ["comparable_to_us"], "yes")   # Iscover: duplicate authorisation, flagged in the note

    def test_load_molecules_reads_single_ingredient_us_brands(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "drugc.json").write_text(json.dumps({"inn": "drugc", "name": "DRUGC", "us": [
                {"brand_names": ["Brandc"], "marketing_categories": ["NDA"], "combination": False},
                {"brand_names": ["Comboc"], "marketing_categories": ["NDA"], "combination": True},
                {"brand_names": ["drugc"], "marketing_categories": ["ANDA"], "combination": False}]}))
            Path(tmp, "selection.json").write_text("{}")
            self.assertEqual(eu.load_molecules(Path(tmp)), [{"inn": "drugc", "name": "DRUGC", "us_brands": ["Brandc"]}])


class EuNumberSourceTest(unittest.TestCase):
    def setUp(self):
        self.rows = eu.load_medicines((FIX / "ema_medicines_sample.json").read_bytes())
        self.index = eu.register_index(eu.load_register_ods((FIX / "register_ods_sample.json").read_bytes()))

    def test_three_known_products_map_by_epar_url(self):
        results = eu.verify_known(self.rows, self.index)
        self.assertEqual([r["result"] for r in results], ["ok", "ok", "ok"])
        self.assertTrue(all(r["match"] == "epar-url" for r in results))

    def test_name_fallback_ignores_previously_note(self):
        p = eu.product_summary(next(r for r in self.rows if r["name_of_medicine"] == "Ozempic"))
        p["medicine_url"] = "https://www.ema.europa.eu/en/medicines/human/EPAR/other-slug"
        p["name"] = "Ozempic (previously Semaglutide Novo)"
        reg, how = eu.map_eu_number(p, self.index)
        self.assertEqual((reg["eu_number"], how), ("EU/1/17/1251", "name"))
        p["name"] = "Unknown"
        reg, how = eu.map_eu_number(p, self.index)
        self.assertIsNone(reg)
        self.assertIn("no Union Register row", how)

    def test_h_page_keeps_digits_as_written(self):
        self.assertEqual(eu.h_page_of("EU/1/15/1024"), "h1024")
        self.assertEqual(eu.h_page_of("EU/1/96/005"), "h005")
        entry = self.index[0]["www.ema.europa.eu/en/medicines/human/epar/keytruda"][0]
        self.assertEqual(entry["h_page_url"], f"{eu.REGISTER}/html/h1024.htm")


class HPageTest(unittest.TestCase):
    def check(self, path, eu_number, ema, annexes, first, logged):
        p = eu.parse_hpage(path.read_bytes())
        self.assertEqual(p["eu_number"], eu_number)
        self.assertTrue(all("id" in x and "decision" in x and "updated_by" in x for x in p["procedures"]))
        plan = eu.plan_annexes(p["procedures"])
        pending = [a for a in plan if a["status"] == "pending"]
        self.assertEqual(len(pending), annexes)
        self.assertEqual(pending[0]["url"], first)
        self.assertEqual(sorted(a["procedure_id"] for a in plan if a["status"] == "logged"), logged)
        self.assertEqual([a["decision_date"] for a in plan], sorted(a["decision_date"] for a in plan))
        self.assertIn(ema, eu.page_ema_numbers(p["procedures"]))
        return p, plan

    def test_ozempic_fixture(self):
        p, plan = self.check(FIX / "h1251.htm", "EU/1/17/1251", "EMEA/H/C/004174", 12,
                             f"{eu.REGISTER}/2018/20180208139833/anx_139833_en.pdf", ["155288", "162413"])
        self.assertTrue(all("corrigendum without an English annex" in a["reason"]
                            for a in plan if a["status"] == "logged"))
        self.assertEqual(eu.procedure_counts(p["procedures"])["procedures"], 34)

    @unittest.skipUnless((M0 / "keytruda" / "h1024.htm").exists(), "m0-data/eu not present")
    def test_keytruda_m0_page(self):
        self.check(M0 / "keytruda" / "h1024.htm", "EU/1/15/1024", "EMEA/H/C/003820", 51,
                   f"{eu.REGISTER}/2015/20150717132284/anx_132284_en.pdf", [])

    @unittest.skipUnless((M0 / "eliquis" / "h691.htm").exists(), "m0-data/eu not present")
    def test_eliquis_m0_page(self):
        p, plan = self.check(M0 / "eliquis" / "h691.htm", "EU/1/11/691", "EMEA/H/C/002148", 20,
                             f"{eu.REGISTER}/2011/20110518102349/anx_102349_en.pdf", ["166182"])
        counts = eu.procedure_counts(p["procedures"])
        self.assertEqual(counts["carried_by_later_decision"], 35)

    def test_missing_dataset_is_an_error(self):
        with self.assertRaises(eu.EUError):
            eu.parse_hpage(b"<html><script>var dataSet_product_information = [];</script></html>")


class AnnexUrlTest(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(eu.annex_url("163752", "2024-08-29"),
                         f"{eu.REGISTER}/2024/20240829163752/anx_163752_en.pdf")
        self.assertEqual(eu.annex_url("9031", "2004-12-22", kind="dec", lang="de"),
                         f"{eu.REGISTER}/2004/200412229031/dec_9031_de.pdf")
        with self.assertRaises(eu.EUError):
            eu.annex_url("12a", "2024-08-29")
        with self.assertRaises(eu.EUError):
            eu.annex_url("1", None)

    def test_plan_logs_missing_english_and_missing_date(self):
        procs = [
            {"id": "2", "type": "Centralised - Variation", "decision": {"number": "(2020) 2", "date": "2020-02-01"},
             "files_anx": [{"code": "de"}, {"code": "fr"}], "files_dec": [{"code": "de"}], "updated_by": {}},
            {"id": "1", "type": "Centralised - Authorisation", "decision": {"number": "(2020) 1", "date": "2020-01-01"},
             "files_anx": [{"code": "en"}], "files_dec": [{"code": "en"}], "updated_by": {}},
            {"id": "3", "type": "Centralised - Transfer", "decision": {"number": None, "date": None},
             "files_anx": [{"code": "en"}], "updated_by": {}},
            {"id": "4", "type": "Centralised - Notification", "decision": {}, "files_anx": None,
             "updated_by": {"number": "(2020) 2", "date": "2020-02-01"}},
        ]
        plan = eu.plan_annexes(procs)
        self.assertEqual([a["procedure_id"] for a in plan], ["1", "2", "3"])
        self.assertEqual(plan[0]["status"], "pending")
        self.assertEqual(plan[1]["reason"], "no English annex file (annex languages: de,fr)")
        self.assertIn("no decision date", plan[2]["reason"])
        self.assertEqual(eu.procedure_counts(procs)["carried_by_later_decision"], 1)


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Serves bytes by URL; a URL mapped to an int answers with that HTTP status."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        assert req.get_header("User-agent") == build.USER_AGENT
        body = self.pages.get(url, 404)
        if isinstance(body, int):
            raise urllib.error.HTTPError(url, body, "x", {}, io.BytesIO(b""))
        return FakeResponse(body)


def fetcher(opener, tmp):
    return eu.EUFetcher(opener=opener, min_gap_s=0, backoff=(0, 0), sleep=lambda s: None,
                        log_path=Path(tmp) / "requests.tsv")


class FetcherTest(unittest.TestCase):
    def test_challenge_page_and_403_back_off_then_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            op = FakeOpener({"https://ec.europa.eu/a.pdf": b"<html>challenge</html>",
                             "https://ec.europa.eu/b.pdf": 403})
            f = eu.EUFetcher(opener=op, min_gap_s=0, backoff=(0, 0), sleep=lambda s: None, abort_after=2)
            with self.assertRaises(build.BuildError) as ctx:
                f.fetch("https://ec.europa.eu/a.pdf", "pdf")
            self.assertNotIsInstance(ctx.exception, build.FetchAborted)
            self.assertEqual(len(op.calls), 3)
            with self.assertRaises(build.FetchAborted):
                f.fetch("https://ec.europa.eu/b.pdf", "pdf")

    def test_404_is_not_available_and_other_hosts_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = fetcher(FakeOpener({}), tmp)
            with self.assertRaises(build.NotAvailable):
                f.fetch("https://ec.europa.eu/missing.pdf", "pdf")
            with self.assertRaises(eu.EUError):
                f.fetch("https://commission.europa.eu/legal-notice_en", "html")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.sources, self.raw = base / "sources", base / "raw"
        page = (FIX / "h1251.htm").read_bytes()
        self.ref = {"dir": "EMEA-H-C-004174", "ema_product_number": "EMEA/H/C/004174", "name": "Ozempic",
                    "eu_number": "EU/1/17/1251", "h_page_url": f"{eu.REGISTER}/html/h1251.htm"}
        plan = [a for a in eu.plan_annexes(eu.parse_hpage(page)["procedures"]) if a["status"] == "pending"]
        self.pages = {self.ref["h_page_url"]: page}
        for i, a in enumerate(plan):
            self.pages[a["url"]] = b"%PDF-1.4 annex " + str(i % 11).encode()  # last one repeats the first
        self.missing = plan[3]["url"]
        self.pages[self.missing] = 404
        self.plan = plan

    def tearDown(self):
        self.tmp.cleanup()

    def run_fetch(self, opener, **kw):
        return eu.fetch_product("semaglutide", self.ref, fetcher(opener, self.tmp.name), self.sources, self.raw, **kw)

    def test_rerun_makes_no_requests_and_logs_persist(self):
        op = FakeOpener(self.pages)
        problems = self.run_fetch(op)
        src = json.loads((self.sources / "EMEA-H-C-004174.json").read_text())
        status = {a["procedure_id"]: a["status"] for a in src["annexes"]}
        self.assertEqual(list(status.values()).count("fetched"), 10)
        self.assertEqual(list(status.values()).count("duplicate"), 1)
        self.assertEqual(list(status.values()).count("logged-http"), 1)
        self.assertEqual(list(status.values()).count("logged"), 2)
        self.assertEqual(len(problems), 1)
        self.assertEqual(src["h_page"]["sha256"], eu.hashlib.sha256(self.pages[self.ref["h_page_url"]]).hexdigest())
        first = len(op.calls)
        self.assertGreater(first, 12)

        again = FakeOpener(self.pages)
        self.run_fetch(again)
        self.assertEqual(again.calls, [])
        retry = FakeOpener(self.pages)
        self.run_fetch(retry, retry_logged=True)
        # the 404 is retried, then under the folder named by the closed date (2020-10-01, not 2020-09-30)
        self.assertEqual(retry.calls, [self.missing, self.missing.replace("20200930", "20201001")])

    def test_interrupted_run_resumes_where_it_stopped(self):
        pages = dict(self.pages)
        for a in self.plan[5:]:
            pages[a["url"]] = 503
        op = FakeOpener(pages)
        f = eu.EUFetcher(opener=op, min_gap_s=0, backoff=(), sleep=lambda s: None, abort_after=2)
        with self.assertRaises(build.FetchAborted):
            eu.fetch_product("semaglutide", self.ref, f, self.sources, self.raw)
        src = json.loads((self.sources / "EMEA-H-C-004174.json").read_text())
        done = {a["procedure_id"] for a in src["annexes"] if a["status"] == "fetched"}
        self.assertEqual(len(done), 4)
        op2 = FakeOpener(self.pages)
        self.run_fetch(op2)
        self.assertNotIn(self.plan[0]["url"], op2.calls)
        self.assertNotIn(self.ref["h_page_url"], op2.calls)
        self.assertIn(self.plan[5]["url"], op2.calls)


class CaptureChangesTest(unittest.TestCase):
    def test_changes(self):
        prev = {"capture_date": "2026-10-31", "products": {
            "A": {"revision_number": "1", "last_updated_date": "2026-10-01", "pi_sha256": "x", "medicine_status": "Authorised"},
            "B": {"revision_number": "1", "last_updated_date": "2026-10-01", "pi_sha256": "y", "medicine_status": "Authorised"},
            "C": {}}}
        now = {"A": {"revision_number": "1", "last_updated_date": "2026-10-01", "pi_sha256": "x", "medicine_status": "Authorised"},
               "B": {"revision_number": "2", "last_updated_date": "2026-11-01", "pi_sha256": "z", "medicine_status": "Authorised"},
               "D": {}}
        ch = eu.capture_changes(prev, now)
        self.assertEqual(ch["previous"], "2026-10-31")
        self.assertEqual(ch["new"], ["D"])
        self.assertEqual(ch["removed"], ["C"])
        self.assertEqual(sorted(ch["changed"]["B"]), ["last_updated_date", "pi_sha256", "revision_number"])
        self.assertNotIn("A", ch["changed"])
        self.assertEqual(eu.capture_changes(None, now)["new"], ["A", "B", "D"])

    def test_epar_pi_documents(self):
        data = json.dumps({"data": [
            {"type": "product-information", "ema_product_number": "EMEA/H/C/002148", "last_updated_date": "2026-01-01",
             "document_url": "old"},
            {"type": "product-information", "ema_product_number": "EMEA/H/C/002148", "last_updated_date": "2026-04-10",
             "document_url": "https://www.ema.europa.eu/en/documents/product-information/eliquis-epar-product-information_en.pdf"},
            {"type": "overview", "ema_product_number": "EMEA/H/C/002148", "document_url": "x"}]}).encode()
        pis = eu.epar_pi_documents(data)
        self.assertTrue(pis["EMEA/H/C/002148"]["document_url"].endswith("eliquis-epar-product-information_en.pdf"))

    def test_epar_pi_documents_skips_spreadsheet_and_veterinary_rows(self):
        base = "https://www.ema.europa.eu/en/documents/product-information/"
        data = json.dumps({"data": [
            {"type": "product-information", "ema_product_number": "EMEA/H/C/000471", "last_updated_date": "2026-01-12",
             "document_url": base + "abilify-epar-product-information_en.pdf"},
            {"type": "product-information", "ema_product_number": "EMEA/H/C/000471", "last_updated_date": "2026-10-27",
             "document_url": base + "product-information-urls-member-states_en.xls"},
            {"type": "product-information", "ema_product_number": "EMEA/V/C/000133", "last_updated_date": "2026-10-27",
             "document_url": base + "vet-epar-product-information_en.pdf"}]}).encode()
        pis = eu.epar_pi_documents(data)
        self.assertEqual(list(pis), ["EMEA/H/C/000471"])
        self.assertTrue(pis["EMEA/H/C/000471"]["document_url"].endswith("abilify-epar-product-information_en.pdf"))


class PdfPagesTest(unittest.TestCase):
    """pdf_pages reads its result after a marker line, because MuPDF prints its own messages on stdout.
    A stand-in interpreter is used, so this runs without .venv-eu or PyMuPDF."""

    def test_result_read_after_mupdf_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "python"
            result = {"pymupdf": "9.9", "results": {"a.pdf": {"pages": 3, "text_chars": 900, "error": None,
                                                                "mupdf_messages": []}}}
            fake.write_text("#!/bin/sh\ncat > /dev/null\necho 'MuPDF error: format error: No default Layer config'\n"
                            f"echo '{eu.PAGES_RESULT_MARKER}{json.dumps(result)}'\n", encoding="utf-8")
            fake.chmod(0o755)
            version, results = eu.pdf_pages(["a.pdf"], python=fake)
            self.assertEqual(version, "9.9")
            self.assertEqual(results["a.pdf"]["pages"], 3)
            fake.write_text("#!/bin/sh\ncat > /dev/null\necho 'MuPDF error only'\n", encoding="utf-8")
            with self.assertRaises(eu.EUError):
                eu.pdf_pages(["a.pdf"], python=fake)

    @unittest.skipUnless(eu.VENV_PYTHON.exists(), ".venv-eu not present (PyMuPDF check runs only with it)")
    def test_real_pymupdf_on_m0_annex(self):
        pdf = M0 / "eliquis" / "20110518102349" / "anx_102349_en.pdf"
        if not pdf.exists():
            self.skipTest("m0-data/eu Eliquis annex not present")
        version, results = eu.pdf_pages([str(pdf)])
        self.assertEqual(version, "1.27.2.2")
        self.assertEqual(results[str(pdf)]["pages"], 29)
        self.assertGreater(results[str(pdf)]["text_chars"], 200)


class ProductsFileTest(unittest.TestCase):
    """The committed sources/eu-products.json: shape and the three hand-verified products."""

    @unittest.skipUnless(eu.PRODUCTS.exists(), "sources/eu-products.json not written yet")
    def test_products_file(self):
        doc = json.loads(eu.PRODUCTS.read_text(encoding="utf-8"))
        self.assertEqual([v["result"] for v in doc["verification"]], ["ok", "ok", "ok"])
        refs = [e["reference"] for e in doc["molecules"].values() if e["reference"]]
        self.assertGreaterEqual(len(refs), 100)
        for r in refs:
            self.assertIn(r["history_type"], {"in-force", "current-only", "pending", "not-built"})
            self.assertRegex(r["dir"], r"^EMEA-H-C-\d{6}$")
        for e in doc["molecules"].values():
            self.assertEqual(e["reference"] is None, e["no_reference_reason"] is not None)
        for k in ("ema_medicines_json", "union_register_ods"):
            self.assertRegex(doc["inputs"][k]["sha256"], r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------- build/eu

IDENT = {"converter": "9", "pymupdf": "0.0"}
KNOWN_READINGS = Path(__file__).parent / "eu_known_readings.json"
EU_REPO = Path(eu.os.environ.get("ASOF_EU_REPO", str(eu.BUILD_EU)))


def ref(dir_name, name, eu_number):
    return {"dir": dir_name, "name": name, "eu_number": eu_number, "ema_product_number": dir_name.replace("-", "/"),
            "mah": "Holder", "h_page_url": "https://ec.europa.eu/health/documents/community-register/html/h1.htm",
            "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/x"}


def annex(pid, date, sha, status="fetched", reason=None):
    return {"procedure_id": pid, "decision_date": date, "decision_number": f"(2020) {pid}", "sha256": sha,
            "status": status, "reason": reason, "type": "Centralised - Variation", "ema_number": "EMEA/H/C/1/II/1",
            "url": f"https://ec.europa.eu/health/documents/community-register/x/anx_{pid}_en.pdf",
            "annex_languages": ["de", "en"]}


def conversion(status="converted", **sections):
    stems = {"c43": "smpc-4.3-contraindications", "c48": "smpc-4.8-undesirable-effects",
             "c41": "smpc-4.1-therapeutic-indications"}
    return {"status": status, "sections": {stems[k]: v for k, v in sections.items()},
            "warnings": [], "smpcs": [{}] if sections else []}


def fake_inputs():
    """Two products: A has three converted annexes (one an identical file) and one empty Annex I;
    B has only a failed annex and falls back to its captured product information."""
    a, b = ref("EMEA-H-C-000001", "Alpha", "EU/1/01/001"), ref("EMEA-H-C-000002", "Beta", "EU/1/02/002")
    products = {"molecules": {"alphamab": {"name": "ALPHAMAB", "reference": a, "alternates": [
        {"ema_product_number": "EMEA/H/C/000009", "name": "Alpha Generic", "generic": "Yes", "biosimilar": "No"}]},
        "betanib": {"name": "BETANIB", "reference": b, "alternates": []}}}
    h = {"fetched": "2026-11-01T10:00:00Z"}
    records = [
        {"inn": "alphamab", "ref": a, "src": {"h_page": h, "annexes": [], "procedure_counts": {"with_decision": 4}},
         "annexes": [annex("10", "2001-01-01", "s1"), annex("12", "2003-01-01", "s2"),
                     annex("11", "2003-01-01", "s3"),
                     annex("13", "2004-01-01", "s1", "duplicate", "identical file to the annex of procedure 10")]},
        {"inn": "betanib", "ref": b, "src": {"h_page": h, "annexes": [], "procedure_counts": {"with_decision": 1}},
         "annexes": [annex("20", "2002-06-01", "s4")]},
    ]
    for rec in records:
        rec["src"]["annexes"] = list(rec["annexes"])
    conversions = {
        "s1": conversion(c43="# 4.3 Contraindications\n\nHypersensitivity.\n"),
        "s2": conversion("empty-annex-i"),
        "s3": conversion(c43="# 4.3 Contraindications\n\nHypersensitivity.\nPregnancy.\n",
                         c48="# 4.8 Undesirable effects\n\nNausea.\n"),
        "s4": conversion("no-text-layer"),
        "pi": conversion(c41="# 4.1 Therapeutic indications\n\nCancer.\n"),
    }
    captures = {"EMEA-H-C-000002": ("2026-11-01", {"pi_sha256": "pi", "last_updated_date": "2026-10-01",
                                                    "pi_url": "https://www.ema.europa.eu/en/documents/product-information/beta_en.pdf"})}
    return products, records, conversions, captures


class AppendTest(unittest.TestCase):
    """eu.py build --append on a fake corpus: the converter is replaced by a pre-filled conversion cache, so no
    PyMuPDF is needed. Appending then rebuilding from the same sources must give the same commits."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.raw, self.cache_root, self.sources = self.tmp / "raw", self.tmp / "cache", self.tmp / "sources"
        self.sources.mkdir()
        self.cache = eu.cache_dir_for(IDENT, self.cache_root)
        self.cache.mkdir(parents=True)
        self.saved = eu.converter_identity
        eu.converter_identity = lambda python=None: dict(IDENT)
        a = ref("EMEA-H-C-000001", "Alpha", "EU/1/01/001")
        b = ref("EMEA-H-C-000002", "Beta", "EU/1/02/002")
        self.products = {"molecules": {"alphamab": {"name": "ALPHAMAB", "reference": a, "alternates": []},
                                       "betanib": {"name": "BETANIB", "reference": b, "alternates": []}}}
        self.products_path = self.tmp / "products.json"
        eu.write_json(self.products_path, self.products)
        self.alpha = [annex("10", "2001-01-01", "a10"), annex("11", "2003-01-01", "a11")]
        self.beta = [annex("20", "2002-06-01", "b20")]
        self.texts = {"a10": "Hypersensitivity.", "a11": "Hypersensitivity.\nPregnancy.", "b20": "Children.",
                      "a14": "Hypersensitivity.\nPregnancy.\nLactation.", "a15": "Hypersensitivity (corrected).",
                      "b21": "Children under 2 years."}
        for sha, text in self.texts.items():
            self.convertible(sha, text)
        self.convertible("a16", None)
        self.save()

    def tearDown(self):
        eu.converter_identity = self.saved
        self.tmp_dir.cleanup()

    def convertible(self, sha, text):
        eu.store_blob(b"%PDF fake " + sha.encode(), "pdf", self.raw)
        digest = eu.blob_path(eu.build.sha256_of(b"%PDF fake " + sha.encode()), "pdf", self.raw)
        target = eu.blob_path(sha, "pdf", self.raw)
        digest.rename(target)
        res = conversion(c43=f"# 4.3 Contraindications\n\n{text}\n") if text else conversion("no-annex-i")
        (self.cache / f"{sha}.json").write_text(json.dumps(dict(res, sha256=sha)))

    def save(self):
        for d, annexes in (("EMEA-H-C-000001", self.alpha), ("EMEA-H-C-000002", self.beta)):
            eu.write_json(self.sources / f"{d}.json", {"dir": d, "annexes": annexes,
                                                       "h_page": {"fetched": "2026-11-01T10:00:00Z"}})

    def full(self, name):
        out = self.tmp / name
        res = eu.build_eu(out, self.products_path, self.sources, self.raw, self.tmp / "captures", self.cache_root,
                          workers=1, python=Path("unused"), log=lambda *a, **k: None)
        return out, res["hashes"]

    def append(self, out):
        return eu.append_eu(out, self.products_path, self.sources, self.raw, self.cache_root, workers=1,
                            python=Path("unused"), log=lambda *a, **k: None)

    def test_append_then_rebuild_gives_the_same_history(self):
        out, before = self.full("live")
        self.assertEqual(len(before), 3)
        self.assertEqual(self.append(out)["hashes"], [])                      # nothing pending: HEAD untouched
        self.assertEqual(build.git(out, "rev-parse", "main").strip(), before[-1])
        corr = dict(annex("15", "2001-01-01", "a15"), type="Centralised - Corrigendum", decision_number="(2020) 10")
        self.alpha += [dict(annex("14", "2005-01-01", "a14"), added="2026-11-05"), dict(corr, added="2026-11-05"),
                       dict(annex("16", "2005-02-01", "a16"), added="2026-11-05")]
        self.beta += [dict(annex("21", "2026-11-04", "b21"), added="2026-11-05")]
        self.save()
        res = self.append(out)
        self.assertEqual(len(res["hashes"]), 3)
        self.assertEqual(res["skipped"], [{"dir": "EMEA-H-C-000001", "procedure_id": "16", "status": "no-annex-i"}])
        after = build.git(out, "rev-list", "--reverse", "main").split()
        self.assertEqual(after[:3], before)                                   # old hashes unchanged
        self.assertEqual(build.git(out, "rev-parse", f"{res['hashes'][0]}^").strip(), before[-1])
        src = eu.read_json(self.sources / "EMEA-H-C-000001.json")
        self.assertEqual({a["procedure_id"]: a.get("build_skip") for a in src["annexes"]}["16"], "no-annex-i")
        # within a batch, the build order is by date: the corrigendum keeps the date of the decision it corrects
        msg = build.git(out, "log", "-1", "--format=%B", res["hashes"][0])
        self.assertIn("Effective-Date: 2001-01-01\n", msg)
        self.assertIn("Source-Version: 3\n", msg)
        self.assertIn("Corrigendum-Of: 10\n", msg)
        self.assertIn("Source-Version: 4\n", build.git(out, "log", "-1", "--format=%B", res["hashes"][1]))
        import labels
        ix = labels.EuIndex(out, self.tmp / "no-molecules")
        self.assertEqual(ix.out_of_order_setids, {"EMEA-H-C-000001"})        # appended below the running maximum
        self.assertEqual(ix.as_of("EMEA-H-C-000001", "2002-01-01")[0].procedure_id, "15")
        self.assertEqual(ix.record_to, "2026-11-04")
        hit = labels.search(ix, "corrected", "2002-01-01")
        self.assertEqual([m["label_id"] for m in hit["matches"]], ["EMEA-H-C-000001"])
        self.assertEqual(labels.search(ix, "Lactation", "2004-01-01")["count"], 0)
        rebuilt, hashes = self.full("rebuilt")
        self.assertEqual(hashes, after)                                       # append == full rebuild
        self.assertEqual(self.append(out)["hashes"], [])                      # idempotent
        self.assertEqual(build.git(out, "rev-list", "--count", "main").strip(), "6")

    def test_removed_product_is_not_built_and_blocks_an_append(self):
        removed = {"EMEA-H-C-000002"}
        _, records, jobs, _ = eu.build_inputs(self.products_path, self.sources, self.raw, removed=removed)
        self.assertEqual([r["ref"]["dir"] for r in records], ["EMEA-H-C-000001"])
        self.assertNotIn("b20", jobs)
        out, _ = self.full("live")                        # built with Beta (the committed list is empty)
        with self.assertRaises(eu.EUError) as ctx:
            eu.plan_append_eu(out, self.products_path, self.sources, self.raw, removed=removed)
        self.assertIn(build.HISTORY_REFUSAL, str(ctx.exception))
        self.assertIn("tools/purge.py rebuild --corpus eu", str(ctx.exception))
        res = eu.build_eu(self.tmp / "without", self.products_path, self.sources, self.raw, self.tmp / "captures",
                          self.cache_root, workers=1, python=Path("unused"), log=lambda *a, **k: None,
                          removed=removed)
        self.assertEqual(len(res["hashes"]), 2)
        self.assertEqual(eu.head_commits_eu(self.tmp / "without"), [("EMEA-H-C-000001", "10"),
                                                                    ("EMEA-H-C-000001", "11")])
        self.assertEqual(eu.plan_append_eu(self.tmp / "without", self.products_path, self.sources, self.raw,
                                           removed=removed)[0], [])
        refs = eu.active_reference_products(self.products, removed)
        self.assertEqual([r["dir"] for _, r in refs], ["EMEA-H-C-000001"])

    def test_append_refuses_to_change_history(self):
        out, _ = self.full("live")
        self.beta.append(dict(annex("21", "2026-11-04", "b21"), added="2026-11-05"))
        self.save()
        self.append(out)
        self.alpha.append(dict(annex("14", "2005-01-01", "a14"), added="2026-11-04"))   # an older batch
        self.save()
        with self.assertRaises(eu.EUError) as ctx:
            self.append(out)
        self.assertIn(build.HISTORY_REFUSAL, str(ctx.exception))


class BuildPlanTest(unittest.TestCase):
    def test_order_versions_skips_and_fallback(self):
        products, records, conversions, captures = fake_inputs()
        commits, outcomes = eu.plan_commits(products, records, conversions, captures)
        self.assertEqual([(c["ref"]["name"], c["date"], c.get("annex", {}).get("procedure_id"), c["seq"])
                          for c in commits],
                         [("Alpha", "2001-01-01", "10", 1), ("Alpha", "2003-01-01", "11", 2),
                          ("Alpha", "2004-01-01", "13", 3), ("Beta", "2026-11-01", None, 1)])
        alpha, beta = outcomes
        self.assertEqual((alpha["history_type"], alpha["record_from"], alpha["record_to"]),
                         ("in-force", "2001-01-01", "2026-11-01"))
        self.assertEqual([(s["procedure_id"], s["status"]) for s in alpha["skipped"]], [("12", "empty-annex-i")])
        self.assertEqual((beta["history_type"], beta["record_from"], beta["record_to"]),
                         ("current-only", "2026-11-01", "2026-11-01"))
        self.assertEqual(beta["skipped"][0]["status"], "no-text-layer")

    def test_product_without_any_text_is_not_built(self):
        products, records, conversions, _ = fake_inputs()
        commits, outcomes = eu.plan_commits(products, records, conversions, {"EMEA-H-C-000002": None})
        self.assertEqual(outcomes[1]["history_type"], "not-built")
        self.assertIn("no captured product-information PDF", outcomes[1]["problem"])
        self.assertEqual(len(commits), 3)

    def test_message_author_and_trailers(self):
        products, records, conversions, captures = fake_inputs()
        commits, _ = eu.plan_commits(products, records, conversions, captures)
        msg = eu.commit_message_eu(commits[2], IDENT)
        self.assertTrue(msg.startswith("Alpha — Commission decision (2020) 13\n\n"))
        for line in ("Source-Id: ec-union-register:EU/1/01/001:13", "Source-Version: 3", "Effective-Date: 2004-01-01",
                     "Decision-Number: (2020) 13", "Decision-Date: 2004-01-01", "EMA-Procedure: EMEA/H/C/1/II/1",
                     "Source-SHA256: s1", "Annex-Identical-To: 10", "Converter-Version: eu_qrd 9 (PyMuPDF 0.0)",
                     "History: in-force", "Source-URL: https://ec.europa.eu/health/documents/community-register/x/anx_13_en.pdf"):
            self.assertIn(line + "\n", msg)
        self.assertTrue(msg.endswith("History: in-force\n" + notices.TRAILER_EU + "\n"))
        self.assertEqual(eu.author_line(commits[0]), "Annex EU/1/01/001 10 <EMEA-H-C-000001.10@labels.invalid>")
        pi = eu.commit_message_eu(commits[3], IDENT)
        self.assertIn("Source-Id: ema-product-information:EMEA/H/C/000002:2026-11-01\n", pi)
        self.assertIn("History: current-only\n" + notices.TRAILER_EU + "\n", pi)

    def test_repo_tree_trailers_and_identical_hashes(self):
        products, records, conversions, captures = fake_inputs()
        runs = []
        with tempfile.TemporaryDirectory() as tmp:
            for n in (1, 2):
                commits, outcomes = eu.plan_commits(products, records, conversions, captures)
                out = Path(tmp) / f"r{n}"
                eu.prepare_out_eu(out, clean=False)
                runs.append(eu.write_repo(out, commits, outcomes, conversions.__getitem__, IDENT))
            self.assertEqual(runs[0], runs[1])
            self.assertEqual(len(runs[0]), 4)
            out = Path(tmp) / "r1"
            pa = "alphamab/EMEA-H-C-000001"
            self.assertEqual(sorted(build.git(out, "ls-tree", "--name-only", "HEAD", pa + "/").split()),
                             [f"{pa}/product.json", f"{pa}/smpc-4.3-contraindications.md"])
            # the identical-file annex of procedure 13 restores the text of procedure 10 and deletes 4.8
            self.assertNotIn("Pregnancy", build.git(out, "show", f"HEAD:{pa}/smpc-4.3-contraindications.md"))
            self.assertIn("Pregnancy", build.git(out, "show", f"{runs[0][1]}:{pa}/smpc-4.3-contraindications.md"))
            meta = json.loads(build.git(out, "show", f"HEAD:{pa}/product.json"))
            self.assertEqual(sorted(meta), sorted(eu.PRODUCT_JSON_FIELDS))   # constants only
            self.assertEqual(meta["history_type"], "in-force")
            for key in ("record_from", "record_to", "versions", "alternates", "mah", "converter", "comparable_to_us"):
                self.assertNotIn(key, meta)
            # product.json is written once, with the product's first commit
            self.assertEqual(build.git(out, "log", "--format=%H", "--", f"{pa}/product.json").split(), [runs[0][0]])
            corpus = json.loads(build.git(out, "show", "HEAD:corpus.json"))
            self.assertEqual(sorted(corpus), ["corpus", "layout", "scope", "sources"])
            # README.md, NOTICE.md and corpus.json are written once, in the first commit; no LICENSE file
            self.assertEqual(build.git(out, "log", "--format=%H", "--", "NOTICE.md", "README.md", "corpus.json").split(),
                             [runs[0][0]])
            root = build.git(out, "ls-tree", "--name-only", "main").split()
            self.assertTrue({"NOTICE.md", "README.md", "corpus.json"} <= set(root))
            self.assertFalse([n for n in root if n.upper().startswith(("LICENSE", "COPYING"))])
            self.assertEqual(build.git(out, "show", "main:NOTICE.md").encode(), notices.root_files("eu")["NOTICE.md"])
            fidelity = build.git(out, "log", "--format=%(trailers:key=Text-Fidelity,valueonly,separator=)%x1e", "main")
            values = [r.strip() for r in fidelity.split("\x1e")][:-1]
            self.assertEqual(set(values), {notices.TRAILER_EU.split(": ", 1)[1]})
            self.assertEqual(len(values), 4)
            log = build.git(out, "log", "--format=%an|%ad|%cn", "--date=format-local:%Y-%m-%d %H:%M %z")
            self.assertIn("Annex EU/1/01/001 10|2001-01-01 00:00 +0000|asof-drug-labels build", log)
            with self.assertRaises(eu.EUError):
                eu.prepare_out_eu(out, clean=False)
            eu.prepare_out_eu(out, clean=True)

    def test_coverage_counts(self):
        products, records, conversions, captures = fake_inputs()
        commits, outcomes = eu.plan_commits(products, records, conversions, captures)
        slim = {k: {"status": v["status"], "warnings": v["warnings"], "smpcs": len(v["smpcs"])} for k, v in conversions.items()}
        cov = eu.coverage_eu({"outcomes": outcomes, "hashes": ["x"] * len(commits), "conversions": slim})
        t = cov["totals"]
        self.assertEqual((t["annexes_fetched"], t["annexes_converted"], t["annexes_skipped"], t["commits"]),
                         (5, 3, 2, 4))
        self.assertEqual((t["in_force_products"], t["current_only_products"], t["molecules_in_force"]), (1, 1, 1))
        self.assertEqual(cov["skip_reasons"], {"empty-annex-i": 1, "no-text-layer": 1})
        build_info = {"converter": "c", "workers": 1, "commits": 4, "head": "h", "seconds": {"total": 1},
                      "repo": {"git_dir_bytes_fast_import": 1, "worktree_files": 2}, "peak_memory": {
                          "build_process": 1, "largest_child": 1}}
        md = eu.coverage_markdown({"generated": "now", "build": build_info, **cov})
        self.assertIn(f"One row per reference product ({len(cov['products'])}) is in `reports/eu-coverage.json`", md)
        self.assertNotIn("Record window", md)
        self.assertNotIn("| alphamab |", md)          # per-product rows live in the JSON only
        self.assertIn("| Build time this run (on a warm conversion cache) | 1 s |", md)
        self.assertNotIn("empty conversion cache", md)

    def test_coverage_build_time_names_the_cache_state(self):
        products, records, conversions, captures = fake_inputs()
        commits, outcomes = eu.plan_commits(products, records, conversions, captures)
        slim = {k: {"status": v["status"], "warnings": v["warnings"], "smpcs": len(v["smpcs"])} for k, v in conversions.items()}
        cov = eu.coverage_eu({"outcomes": outcomes, "hashes": ["x"] * len(commits), "conversions": slim})
        base = {"converter": "c", "workers": 1, "commits": 4, "head": "h", "seconds": {"total": 6.4},
                "repo": {"git_dir_bytes_fast_import": 1, "worktree_files": 2},
                "peak_memory": {"build_process": 1, "largest_child": 1}}
        cases = [
            ({"converted_this_run": 0}, "| Build time this run (on a warm conversion cache, 0 annexes converted) | 6.4 s |"),
            ({"conversion_cache": "warm", "converted_this_run": 2},
             "| Build time this run (on a warm conversion cache, 2 annexes converted) | 6.4 s |"),
            ({"conversion_cache": "empty", "converted_this_run": 5, "seconds_from_scratch": 6.4},
             "| Build time this run (from an empty conversion cache) | 6.4 s |"),
            ({"converted_this_run": 5, "seconds_from_scratch": 6.4},
             "| Build time this run (from an empty conversion cache) | 6.4 s |"),
        ]
        for extra, row in cases:
            with self.subTest(extra):
                self.assertIn(row, eu.coverage_markdown({"generated": "now", "build": {**base, **extra}, **cov}))
        carried = eu.coverage_markdown({"generated": "now", "build": {
            **base, "conversion_cache": "warm", "converted_this_run": 0, "seconds_from_scratch": 90.0}, **cov})
        self.assertIn("| Build time from an empty conversion cache (earlier run, same HEAD) | 90.0 s |", carried)
        self.assertIn("on a warm conversion cache, 0 annexes converted) | 6.4 s |", carried)


def _venv_ok() -> bool:
    if not eu.VENV_PYTHON.exists():
        return False
    try:
        eu.converter_identity()
        return True
    except eu.EUError:
        return False


VENV_OK = _venv_ok()


@unittest.skipUnless(VENV_OK, ".venv-eu with PyMuPDF not present")
class RebuildDeterminismTest(unittest.TestCase):
    """Rebuild on a small pinned subset (the first three Ozempic annexes) with real conversion: two builds
    from empty caches with different worker counts give identical commit hashes."""

    DIR, INN, N = "EMEA-H-C-004174", "semaglutide", 3

    def test_two_builds_identical(self):
        src_path = eu.SOURCES_EU / f"{self.DIR}.json"
        if not (src_path.exists() and eu.PRODUCTS.exists()):
            self.skipTest("sources/eu not present")
        src = json.loads(src_path.read_text(encoding="utf-8"))
        keep = [a for a in src["annexes"] if a.get("status") in eu.CONVERT_STATUSES][:self.N]
        if len(keep) < self.N or not all(eu.blob_path(a["sha256"], "pdf").exists() for a in keep):
            self.skipTest("raw/eu annex PDFs not present")
        src["annexes"] = keep
        products = json.loads(eu.PRODUCTS.read_text(encoding="utf-8"))
        products["molecules"] = {self.INN: products["molecules"][self.INN]}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "sources").mkdir()
            (tmp / "sources" / f"{self.DIR}.json").write_text(json.dumps(src), encoding="utf-8")
            (tmp / "products.json").write_text(json.dumps(products), encoding="utf-8")
            hashes = []
            for n, workers in ((1, 1), (2, 3)):
                r = eu.build_eu(tmp / f"repo{n}", products_path=tmp / "products.json", sources_dir=tmp / "sources",
                                captures_dir=tmp / "no-captures", cache_root=tmp / f"cache{n}", workers=workers,
                                log=lambda *a, **k: None)
                self.assertEqual(r["converted_this_run"], self.N)
                hashes.append(r["hashes"])
            self.assertEqual(hashes[0], hashes[1])
            self.assertEqual(len(hashes[0]), self.N)
            body = build.git(tmp / "repo1", "log", "-1", "--format=%B", hashes[0][0])
            self.assertIn(f"Source-Id: ec-union-register:EU/1/17/1251:{keep[0]['procedure_id']}\n", body)
            self.assertIn(f"Effective-Date: {keep[0]['decision_date']}\n", body)


PDF_TEXT_SCRIPT = r"""
import json, sys, pymupdf
pymupdf.TOOLS.mupdf_display_errors(False)
out = {}
for path in json.load(sys.stdin):
    with pymupdf.open(path) as doc:
        out[path] = " ".join(" ".join(doc[i].get_text().split()) for i in range(doc.page_count))
sys.stdout.write("\nTEXT " + json.dumps(out) + "\n")
"""


def pdf_texts(paths: list[str]) -> dict[str, str]:
    proc = eu.subprocess.run([str(eu.VENV_PYTHON), "-I", "-c", PDF_TEXT_SCRIPT], input=json.dumps(paths),
                             capture_output=True, text=True)
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("TEXT "))
    return json.loads(line[5:])


def squash(text: str) -> str:
    return " ".join(text.split())


class KnownReadingsTest(unittest.TestCase):
    """tools/tests/eu_known_readings.json: five readings, each a line that first appears in a given
    Commission decision's English annex. Checked against the PDF text itself (PyMuPDF page text, no
    converter) and, when build/eu exists, against the repo and the "on date X" rule."""

    @classmethod
    def setUpClass(cls):
        cls.readings = json.loads(KNOWN_READINGS.read_text(encoding="utf-8"))["readings"]

    def test_file_shape(self):
        self.assertEqual(len(self.readings), 5)
        for r in self.readings:
            for k in ("product", "inn", "name", "section", "quote", "first_decision", "absent_before",
                      "reading_date"):
                self.assertIn(k, r)
            self.assertLess(r["absent_before"]["decision_date"], r["first_decision"]["decision_date"])
            self.assertLessEqual(r["first_decision"]["decision_date"], r["reading_date"])

    @unittest.skipUnless(VENV_OK, ".venv-eu with PyMuPDF not present")
    def test_quotes_against_pdf_text(self):
        paths = {}
        for r in self.readings:
            for k in ("first_decision", "absent_before"):
                p = eu.blob_path(r[k]["sha256"], "pdf")
                if not p.exists():
                    self.skipTest("raw/eu annex PDFs not present")
                paths[(r["quote"], k)] = str(p)
        texts = pdf_texts(sorted(set(paths.values())))
        for r in self.readings:
            q = squash(r["pdf_quote"] if r.get("pdf_quote") else r["quote"])
            self.assertIn(q, texts[paths[(r["quote"], "first_decision")]], r["quote"])
            self.assertNotIn(q, texts[paths[(r["quote"], "absent_before")]], r["quote"])

    @unittest.skipUnless((EU_REPO / ".git" / eu.BUILD_MARKER).exists(), "build/eu not built")
    def test_readings_in_build_repo(self):
        for r in self.readings:
            path = f"{r['inn']}/{r['product']}/{r['section']}.md"
            # "on date X": the newest commit touching the product whose Effective-Date <= X
            log = build.git(EU_REPO, "log", "--format=%H %(trailers:key=Effective-Date,valueonly,separator=)",
                            "main", "--", f"{r['inn']}/{r['product']}/")
            on = [line.split() for line in log.splitlines() if line.strip()]
            at = next(h for h, d in on if d <= r["reading_date"])
            before = next(h for h, d in on if d <= r["absent_before"]["decision_date"])
            self.assertIn(r["quote"] + "\n", build.git(EU_REPO, "show", f"{at}:{path}"))
            try:
                old = build.git(EU_REPO, "show", f"{before}:{path}")
            except build.BuildError:
                old = ""
            self.assertNotIn(r["quote"], old)
            first = build.git(EU_REPO, "log", "--reverse", "--format=%(trailers:key=Source-Id,valueonly)",
                              "-S", r["quote"], "main", "--", path).split()[0]
            self.assertEqual(first, f"ec-union-register:{r['eu_number']}:{r['first_decision']['procedure_id']}")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- review fixes (pipeline)

class SharedBackoffTest(unittest.TestCase):
    def test_hold_delays_every_worker(self):
        waits = []
        f = eu.EUFetcher(opener=FakeOpener({"https://ec.europa.eu/a.pdf": b"%PDF-1.4 x"}), min_gap_s=0,
                         backoff=(), sleep=waits.append)
        f.hold(30)
        f.fetch("https://ec.europa.eu/a.pdf", "pdf")
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 29)

    def test_failed_answer_holds_the_shared_slot(self):
        waits = []
        op = FakeOpener({"https://ec.europa.eu/a.pdf": 503, "https://ec.europa.eu/b.pdf": b"%PDF-1.4 x"})
        f = eu.EUFetcher(opener=op, min_gap_s=0, backoff=(40,), sleep=waits.append, abort_after=5)
        with self.assertRaises(build.BuildError):
            f.fetch("https://ec.europa.eu/a.pdf", "pdf")
        self.assertGreater(max(waits), 39)          # the retry waited for the hold

    def test_abort_counts_failures_in_a_window_not_only_consecutive(self):
        op = FakeOpener({"https://ec.europa.eu/bad1.pdf": 503, "https://ec.europa.eu/bad2.pdf": 503,
                         "https://ec.europa.eu/ok.pdf": b"%PDF-1.4 x"})
        f = eu.EUFetcher(opener=op, min_gap_s=0, backoff=(), sleep=lambda s: None, abort_after=2, window=5)
        with self.assertRaises(build.BuildError):
            f.fetch("https://ec.europa.eu/bad1.pdf", "pdf")
        f.fetch("https://ec.europa.eu/ok.pdf", "pdf")   # another worker's success does not reset the count
        with self.assertRaises(build.FetchAborted):
            f.fetch("https://ec.europa.eu/bad2.pdf", "pdf")


class RunLogTest(unittest.TestCase):
    def test_started_and_finished_lines_and_slot_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            eu.record_start("fetch", "2026-11-01T00:00:00Z", raw_dir=raw)
            eu.record_run("fetch", "2026-11-01T00:00:00Z", 1.26, 3, {"completed": False}, raw_dir=raw)
            lines = [json.loads(x) for x in (raw / "runs.jsonl").read_text().splitlines()]
            self.assertEqual([x["event"] for x in lines], ["started", "finished"])
            self.assertEqual((lines[1]["requests"], lines[1]["seconds"], lines[1]["completed"]), (3, 1.3, False))
            f = fetcher(FakeOpener({"https://ec.europa.eu/a.pdf": b"%PDF-1.4 x"}), tmp)
            f.fetch("https://ec.europa.eu/a.pdf", "pdf")
            cols = (raw / "requests.tsv").read_text().rstrip("\n").split("\t")
            self.assertEqual(len(cols), 6)
            self.assertEqual(cols[1:4], ["200", "10", "https://ec.europa.eu/a.pdf"])
            self.assertRegex(cols[5], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class ConverterFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.pdf = self.base / "a.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 x")
        self.cache = self.base / "cache"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_interpreter_fails_and_caches_nothing(self):
        with self.assertRaises(eu.EUError) as ctx:
            eu.convert_pdfs({"abc": self.pdf}, self.cache, 1, python=self.base / "no-such-python", log=lambda *a, **k: None)
        self.assertIn("conversion failed for 1", str(ctx.exception))
        self.assertFalse((self.cache / "abc.json").exists())

    def test_broken_pipe_in_worker_is_reported(self):
        class Pipe:
            closed = False

            def write(self, _):
                raise BrokenPipeError(32, "Broken pipe")

            def close(self):
                self.closed = True

        class Proc:
            stdin, stdout = Pipe(), io.StringIO("")

            def wait(self):
                return 0

        real = eu._start_converter
        eu._start_converter = lambda python, log_path=None: (Proc(), {})
        try:
            with self.assertRaises(eu.EUError) as ctx:
                eu.convert_pdfs({"abc": self.pdf}, self.cache, 1, log=lambda *a, **k: None)
        finally:
            eu._start_converter = real
        self.assertIn("BrokenPipeError", str(ctx.exception))
        self.assertFalse((self.cache / "abc.json").exists())

    def test_converter_error_fails_the_build_and_is_not_cached(self):
        class Pipe:
            closed = False

            def write(self, _):
                pass

            def flush(self):
                pass

            def close(self):
                self.closed = True

        class Proc:
            stdin = Pipe()
            stdout = io.StringIO(eu.CONVERT_DONE + json.dumps({"sha256": "abc", "status": "converter-error",
                                                                "error": "ValueError: bad table"}) + "\n")

            def wait(self):
                return 0

            def kill(self):
                pass

        real = eu._start_converter
        eu._start_converter = lambda python, log_path=None: (Proc(), {})
        try:
            with self.assertRaises(eu.EUError) as ctx:
                eu.convert_pdfs({"abc": self.pdf}, self.cache, 1, log=lambda *a, **k: None)
        finally:
            eu._start_converter = real
        self.assertIn("converter error ValueError: bad table", str(ctx.exception))
        self.assertFalse((self.cache / "abc.json").exists())

    def test_load_conversion_checks_hash_and_error_status(self):
        self.cache.mkdir()
        (self.cache / "abc.json").write_text(json.dumps({"sha256": "def", "status": "converted"}))
        with self.assertRaises(eu.EUError):
            eu.load_conversion(self.cache, "abc")
        (self.cache / "abc.json").write_text(json.dumps({"sha256": "abc", "status": "converter-error", "warnings": []}))
        with self.assertRaises(eu.EUError):
            eu.load_conversion(self.cache, "abc")
        (self.cache / "abc.json").write_text(json.dumps({"sha256": "abc", "status": "converted"}))
        self.assertEqual(eu.load_conversion(self.cache, "abc")["status"], "converted")

    def test_clear_conversion_cache_deletes_only_converter_folders(self):
        for name in ("qrd3-pymupdf1.27.2.2", "qrd4-pymupdf1.27.2.2", "keep", "qrd-notes"):
            (self.cache / name).mkdir(parents=True)
        (self.cache / "qrd5-pymupdf1.0.txt").write_text("a file, not a folder")
        gone = eu.clear_conversion_cache(self.cache)
        self.assertEqual([g.name for g in gone], ["qrd3-pymupdf1.27.2.2", "qrd4-pymupdf1.27.2.2"])
        self.assertEqual(sorted(x.name for x in self.cache.iterdir()), ["keep", "qrd-notes", "qrd5-pymupdf1.0.txt"])
        self.assertEqual(eu.clear_conversion_cache(self.base / "absent"), [])


class HistoryTypesTest(unittest.TestCase):
    def test_built_products_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            built = dict(ref("EMEA-H-C-000001", "Alpha", "EU/1/01/001"), history_type="in-force",
                         record_from="2001-01-01", record_to="2026-11-01", build_versions=3)
            fresh = ref("EMEA-H-C-000002", "Beta", "EU/1/02/002")
            eu.write_json(base / "p.json", {"molecules": {"a": {"reference": built}, "b": {"reference": fresh}}})
            src = {"h_page": {"fetched": "2026-10-30T00:00:00Z"},
                   "annexes": [{"status": "fetched", "decision_date": "1999-01-01"}]}
            for d in ("EMEA-H-C-000001", "EMEA-H-C-000002"):
                eu.write_json(base / f"{d}.json", src)
            eu.update_history_types(base / "p.json", base)
            got = eu.read_json(base / "p.json")["molecules"]
            self.assertEqual((got["a"]["reference"]["record_from"], got["a"]["reference"]["build_versions"]),
                             ("2001-01-01", 3))
            self.assertEqual((got["b"]["reference"]["history_type"], got["b"]["reference"]["record_from"]),
                             ("in-force", "1999-01-01"))


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls, self.requests = pages, [], 0

    def fetch(self, url, kind=""):
        self.calls.append(url)
        self.requests += 1
        body = self.pages[url]
        if isinstance(body, Exception):
            raise body
        return body


class CaptureProblemsTest(unittest.TestCase):
    def test_fetch_failed_is_not_a_pdf_change(self):
        prev = {"capture_date": "2026-10-31", "products": {"A": {"pi_sha256": "x", "revision_number": "1"}}}
        now = {"A": {"revision_number": "1", "pi_fetch_error": "HTTP 503"}}
        ch = eu.capture_changes(prev, now)
        self.assertEqual((ch["fetch_failed"], ch["changed"]), (["A"], {}))
        self.assertEqual(eu.capture_changes(None, now)["fetch_failed"], ["A"])

    def test_problems_recomputed_per_product_and_kind(self):
        base = "https://www.ema.europa.eu/en/documents/product-information/"
        a, b, c = (ref(f"EMEA-H-C-00000{i}", n, f"EU/1/0{i}/00{i}") for i, n in ((1, "Alpha"), (2, "Beta"), (3, "Gamma")))
        products = {"molecules": {"a": {"reference": a}, "b": {"reference": b}, "c": {"reference": c}}}
        med = json.dumps({"data": [row("Alpha", "EMEA/H/C/000001", "a"), row("Beta", "EMEA/H/C/000002", "b")]}).encode()
        docs = json.dumps({"data": [
            {"type": "product-information", "ema_product_number": "EMEA/H/C/000001", "last_updated_date": "2026-10-01",
             "document_url": base + "alpha-epar-product-information_en.pdf"},
            {"type": "product-information", "ema_product_number": "EMEA/H/C/000003", "last_updated_date": "2026-10-01",
             "document_url": base + "gamma-epar-product-information_en.pdf"}]}).encode()
        pages = {eu.EMA_MEDICINES_URL: med, eu.EMA_EPAR_DOCUMENTS_URL: docs, base + "alpha-epar-product-information_en.pdf": b"%PDF-1.4 a",
                 base + "gamma-epar-product-information_en.pdf": build.BuildError("gave up after retries")}
        with tempfile.TemporaryDirectory() as tmp:
            raw, out = Path(tmp) / "raw", Path(tmp) / "cap.json"
            cap = {"capture_date": "2026-11-01", "inputs": {}, "products": {
                "EMEA-H-C-000003": {"pi_sha256": "old", "pi_url": base + "old.pdf", "pi_fetched_url": base + "old.pdf"}},
                "problems": {"EMEA-H-C-000001": ["stale problem from an earlier run"]}}
            eu.capture_products(cap, out, products, FakeFetcher(pages), raw)
            self.assertEqual(sorted(cap["problems"]), ["EMEA-H-C-000002", "EMEA-H-C-000003"], cap["problems"])
            self.assertEqual(sorted(cap["problems"]["EMEA-H-C-000002"]), ["no-pi-document"])
            self.assertEqual(sorted(cap["problems"]["EMEA-H-C-000003"]), ["not-listed", "pi-fetch-failed"])
            gamma = cap["products"]["EMEA-H-C-000003"]
            self.assertNotIn("pi_sha256", gamma)
            self.assertIn("gave up", gamma["pi_fetch_error"])
            self.assertRegex(cap["products"]["EMEA-H-C-000001"]["pi_sha256"], r"^[0-9a-f]{64}$")
            # a rerun with every input cached makes no requests and clears nothing that succeeded
            again = FakeFetcher({base + "gamma-epar-product-information_en.pdf": b"%PDF-1.4 g"})
            eu.capture_products(cap, out, products, again, raw)
            self.assertEqual(again.calls, [base + "gamma-epar-product-information_en.pdf"])
            self.assertNotIn("pi_fetch_error", cap["products"]["EMEA-H-C-000003"])
            self.assertEqual(sorted(cap["problems"]["EMEA-H-C-000003"]), ["not-listed"])


class CorrigendumTest(unittest.TestCase):
    def test_corrigendum_of(self):
        entries = [annex("10", "2020-01-01", "s1"), annex("11", "2020-03-01", "s2"),
                   dict(annex("12", "2020-03-01", "s3"), decision_number="(2020) 99")]
        cor = dict(annex("13", "2020-03-01", "s4"), type="Centralised - Corrigendum", decision_number="(2020) 11")
        self.assertEqual(eu.corrigendum_of(cor, entries + [cor]), "11")        # same decision number
        cor2 = dict(cor, decision_number="(2020) 50")
        self.assertEqual(eu.corrigendum_of(cor2, entries + [cor2]), "12")      # else latest on the same date
        cor3 = dict(cor, decision_number="(2020) 50", decision_date="2021-01-01")
        self.assertEqual(eu.corrigendum_of(cor3, entries), "not identified")
        self.assertIsNone(eu.corrigendum_of(entries[1], entries))
        self.assertTrue(eu.is_corrigendum({"type": "Centralised - Rectificative decision"}))

    def test_trailers(self):
        products, records, conversions, captures = fake_inputs()
        rec = records[0]
        rec["annexes"][2] = dict(rec["annexes"][2], type="Centralised - Corrigendum", decision_number="(2020) 10",
                                 closed="2003-02-01")
        rec["src"]["annexes"] = list(rec["annexes"])
        commits, _ = eu.plan_commits(products, records, conversions, captures)
        c = next(c for c in commits if c.get("annex", {}).get("procedure_id") == "11")
        self.assertEqual(c["corrigendum_of"], "10")
        msg = eu.commit_message_eu(c, IDENT)
        self.assertIn("Procedure-Closed: 2003-02-01\nCorrigendum-Of: 10\n", msg)
        plain = eu.commit_message_eu(commits[0], IDENT)
        self.assertNotIn("Corrigendum-Of", plain)
        self.assertNotIn("Procedure-Closed", plain)


    def test_coverage_counts_corrigenda_and_shared_decisions(self):
        products, records, conversions, captures = fake_inputs()
        rec = records[0]
        rec["annexes"][2] = dict(rec["annexes"][2], type="Centralised - Corrigendum", decision_number="(2020) 10",
                                 closed="2003-02-01")
        rec["src"]["annexes"] = list(rec["annexes"])
        commits, outcomes = eu.plan_commits(products, records, conversions, captures)
        slim = {k: {"status": v["status"], "warnings": v["warnings"], "smpcs": len(v["smpcs"])} for k, v in conversions.items()}
        t = eu.coverage_eu({"outcomes": outcomes, "hashes": ["x"] * len(commits), "conversions": slim})["totals"]
        self.assertEqual((t["corrigendum_commits"], t["corrigendum_commits_published_after_decision_date"],
                          t["decisions_with_several_commits"], t["commits_in_decisions_with_several_commits"]),
                         (1, 1, 1, 2))
        self.assertNotIn("record_to", t)            # the read date is not a record end
        self.assertEqual(t["register_read"], max(o["record_to"] for o in outcomes if o["record_to"]))


class CrosswalkTest(unittest.TestCase):
    """eu.py crosswalk: sources/eu-products.json (+ build/eu product.json) -> molecules/<inn>.json eu fields."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        products, records, conversions, captures = fake_inputs()
        products["molecules"]["alphamab"]["reference"].update(
            generic="No", biosimilar="No", medicine_status="Authorised", marketing_authorisation_date="2001-01-01",
            comparable_to_us="yes", comparability_note="EU name matches the US NDA/BLA brand ALPHA")
        products["molecules"]["alphamab"]["alternates"] = [
            {"ema_product_number": "EMEA/H/C/000009", "name": "Alpha Generic", "generic": "Yes", "biosimilar": "No",
             "eu_number": "EU/1/09/009", "mah": "Other", "medicine_status": "Authorised",
             "marketing_authorisation_date": "2015-01-01", "medicine_url": "https://www.ema.europa.eu/x9"},
            {"ema_product_number": "EMEA/H/C/000005", "name": "Alpha Bio", "generic": "No", "biosimilar": "Yes",
             "eu_number": "EU/1/05/005", "mah": "Other", "medicine_status": "Authorised",
             "marketing_authorisation_date": "2016-01-01", "medicine_url": "https://www.ema.europa.eu/x5"}]
        products["molecules"]["betanib"]["reference"].update(comparable_to_us="unknown", comparability_note="check")
        for e in products["molecules"].values():
            e.update(combinations=[], near_misses_not_matched=[], no_reference_reason=None)
        products["molecules"]["gammavir"] = {
            "name": "GAMMAVIR", "reference": None, "alternates": [], "near_misses_not_matched": [],
            "combinations": [{"ema_product_number": "EMEA/H/C/000030", "name": "Gamma Duo"},
                             {"ema_product_number": "EMEA/H/C/000031", "name": "Gamma Trio"}],
            "no_reference_reason": "only combination central products under this exact INN (combinations are not built)"}
        products["molecules"]["deltanib"] = {
            "name": "DELTANIB", "reference": None, "alternates": [], "combinations": [],
            "near_misses_not_matched": [
                {"name": "Deltasalt", "ema_product_number": "EMEA/H/C/000040", "kind": "salt",
                 "inn_field": "deltanib sulfate", "generic": "No", "biosimilar": "No",
                 "marketing_authorisation_date": "2005-01-01"},
                {"name": "Deltavar", "ema_product_number": "EMEA/H/C/000041", "kind": "variant",
                 "inn_field": "deltanib pegol", "generic": "No", "biosimilar": "No",
                 "marketing_authorisation_date": "2006-01-01"}],
            "no_reference_reason": "no central product with this exact INN in the EMA medicines JSON; not matched "
                                   "by the v1 rule (exact INN): Deltasalt EMEA/H/C/000040 ('deltanib sulfate', salt)"}
        self.products_path = self.tmp / "eu-products.json"
        eu.write_json(self.products_path, products)
        commits, outcomes = eu.plan_commits(products, records, conversions, captures)
        self.repo = self.tmp / "eu"
        eu.prepare_out_eu(self.repo, clean=False)
        eu.write_repo(self.repo, commits, outcomes, conversions.__getitem__, IDENT)
        eu.sync_products(outcomes, self.products_path)   # what eu.py build writes back
        self.mols = self.tmp / "molecules"
        self.mols.mkdir()
        for inn in ("alphamab", "betanib", "gammavir", "deltanib"):
            (self.mols / f"{inn}.json").write_text(eu.molecule_text(
                {"inn": inn, "name": inn.upper(), "eu": [], "us": [{"setid": "x"}], "rank": 1}), encoding="utf-8")
        (self.mols / "selection.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["crosswalk", "--molecules", str(self.mols), "--products", str(self.products_path),
                "--eu-repo", str(self.repo), *extra]
        with redirect_stdout(out), redirect_stderr(err):
            code = eu.main(argv)
        return code, out.getvalue(), err.getvalue()

    def mol(self, inn):
        return json.loads((self.mols / f"{inn}.json").read_text(encoding="utf-8"))

    def test_fields_written(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertIn("4 files written", out)
        alpha = self.mol("alphamab")
        self.assertEqual((alpha["rank"], alpha["us"]), (1, [{"setid": "x"}]))   # other keys untouched
        self.assertEqual([(e["label_id"], e["role"]) for e in alpha["eu"]],
                         [("EMEA-H-C-000001", "reference"), ("EMEA-H-C-000005", "alternate"),
                          ("EMEA-H-C-000009", "alternate")])
        ref = alpha["eu"][0]
        self.assertEqual(set(ref), set(eu.EU_ENTRY_FIELDS))
        self.assertEqual((ref["ema_product_number"], ref["eu_number"], ref["built"], ref["history_type"],
                          ref["record_from"], ref["record_to"], ref["versions"], ref["comparable_to_us"],
                          ref["generic"], ref["biosimilar"]),
                         ("EMEA/H/C/000001", "EU/1/01/001", True, "in-force", "2001-01-01", "2026-11-01", 3, "yes",
                          False, False))
        alt = alpha["eu"][2]
        self.assertEqual((alt["built"], alt["history_type"], alt["record_from"], alt["versions"],
                          alt["comparable_to_us"], alt["comparability_note"], alt["generic"], alt["mah"]),
                         (False, "not-built", None, None, None, None, True, "Other"))
        self.assertEqual(alpha["eu_selection"], {"rule": "exact-inn-v1", "reference_label_id": "EMEA-H-C-000001",
                                                 "reference_selected_by": "exact-inn",
                                                 "no_reference_reason": None, "combinations_excluded": 0,
                                                 "near_misses_not_matched": []})
        beta = self.mol("betanib")["eu"][0]
        self.assertEqual((beta["history_type"], beta["versions"], beta["comparable_to_us"]),
                         ("current-only", 1, "unknown"))
        gamma = self.mol("gammavir")
        self.assertEqual(gamma["eu"], [])
        self.assertEqual((gamma["eu_selection"]["reference_label_id"], gamma["eu_selection"]["combinations_excluded"]),
                         (None, 2))
        self.assertTrue(gamma["eu_selection"]["no_reference_reason"].startswith("only combination"))
        delta = self.mol("deltanib")["eu_selection"]
        self.assertIn("Deltasalt EMEA/H/C/000040", delta["no_reference_reason"])
        self.assertEqual(delta["near_misses_not_matched"],
                         [{"name": "Deltasalt", "ema_product_number": "EMEA/H/C/000040", "kind": "salt"},
                          {"name": "Deltavar", "ema_product_number": "EMEA/H/C/000041", "kind": "variant"}])
        self.assertEqual((self.mols / "selection.json").read_text(), "{}\n")
        text = (self.mols / "alphamab.json").read_text(encoding="utf-8")
        self.assertEqual(text, eu.molecule_text(alpha))
        res = eu.crosswalk(self.mols, self.products_path, self.repo, write=False)
        self.assertEqual(res["counts"], {"alternates": 2, "molecules": 4, "no_reference_combination_only": 1,
                                         "no_reference_reason": 2, "no_reference_salt_or_alias_originator": 1,
                                         "references": 2, "references_built": 2})

    def test_removed_reference_is_flagged_without_a_problem(self):
        products = eu.load_products(self.products_path)
        entry = products["molecules"]["alphamab"]
        built = eu.built_products(self.repo)
        _, _, probs = eu.crosswalk_fields("alphamab", entry, {k: v for k, v in built.items() if k != "EMEA-H-C-000001"},
                                          removed=set())
        self.assertTrue(probs)                                # built per the products file, absent from the repo
        eu_list, _, probs = eu.crosswalk_fields("alphamab", entry, {}, removed={"EMEA-H-C-000001"})
        self.assertEqual(probs, [])
        self.assertEqual((eu_list[0]["label_id"], eu_list[0]["built"], eu_list[0]["removed"]),
                         ("EMEA-H-C-000001", False, True))
        self.assertNotIn("removed", eu_list[1])               # alternates are untouched

    def test_idempotent_and_check_exit_codes(self):
        code, out, _ = self.run_cli("--check")
        self.assertEqual(code, 1)
        self.assertIn("would change alphamab.json", out)
        self.assertEqual(self.mol("alphamab")["eu"], [])   # --check writes nothing
        self.assertNotIn("eu_selection", self.mol("alphamab"))
        self.assertEqual(self.run_cli()[0], 0)
        before = {p.name: p.read_text() for p in self.mols.iterdir()}
        code, out, _ = self.run_cli()
        self.assertEqual((code, "0 files written" in out), (0, True))
        self.assertEqual({p.name: p.read_text() for p in self.mols.iterdir()}, before)
        self.assertEqual(self.run_cli("--check")[0], 0)

    def test_without_build_repo_uses_products_file(self):
        res = eu.crosswalk(self.mols, self.products_path, self.tmp / "absent")
        self.assertEqual((res["problems"], res["eu_repo"]), ([], False))
        with_repo = self.mol("alphamab")["eu"]
        eu.crosswalk(self.mols, self.products_path, self.repo)
        self.assertEqual(self.mol("alphamab")["eu"], with_repo)

    def test_mismatch_is_an_error_and_writes_nothing(self):
        products = eu.read_json(self.products_path)
        products["molecules"]["alphamab"]["reference"]["record_from"] = "2001-02-01"
        products["molecules"]["betanib"]["reference"]["build_versions"] = 7
        eu.write_json(self.products_path, products)
        code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("alphamab EMEA-H-C-000001: record_from is '2001-01-01' in build/eu product.json but "
                      "'2001-02-01' in sources/eu-products.json", err)
        self.assertIn("betanib EMEA-H-C-000002: versions", err)
        self.assertIn("nothing written", err)
        self.assertEqual(self.mol("alphamab")["eu"], [])
        self.assertEqual(self.run_cli("--check")[0], 2)

    def test_missing_product_or_molecule_is_an_error(self):
        products = eu.read_json(self.products_path)
        del products["molecules"]["deltanib"]
        eu.write_json(self.products_path, products)
        res = eu.crosswalk(self.mols, self.products_path, self.repo)
        self.assertFalse(res["written"])
        self.assertIn("deltanib.json: molecule 'deltanib' is not in eu-products.json", res["problems"][0])
        (self.mols / "deltanib.json").unlink()
        (self.mols / "betanib.json").unlink()
        res = eu.crosswalk(self.mols, self.products_path, self.repo)
        self.assertEqual(res["problems"], ["1 molecules of eu-products.json have no file in "
                                           f"{self.mols}: betanib"])
