"""Selection rules on small synthetic inputs: salt grouping, repackager exclusion,
the DailyMed title fallback, exclusions, ranking, per-molecule caps, determinism."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import select_molecules as sm  # noqa: E402

SETIDS = {name: f"{i:08x}-0000-4000-8000-000000000000" for i, name in enumerate([
    "synthroid", "levo_inj", "levo_anda", "levo_repack", "eliquis", "eliquis_repack", "oxygen", "saline",
    "pollen", "tpn", "irrigant", "metf_nda", "metf_combo", "metf_anda1", "metf_anda2", "metf_anda3", "metf_ag",
    "metf_nda_dup", "keytruda", "stale", "bactrim", "trimethoprim_anda",
], start=1)}


def product(ndc, setid, name, ingredients, *, category="ANDA", app="ANDA000001", labeler="Acme Generics",
            brand=None, original=True, openfda=True, start="20100101", unii=None, rxcui=None, form="TABLET",
            route=("ORAL",), epc=None, product_type="HUMAN PRESCRIPTION DRUG"):
    record = {
        "product_ndc": ndc, "product_type": product_type, "marketing_category": category,
        "application_number": app, "labeler_name": labeler, "brand_name": brand or name, "generic_name": name,
        "dosage_form": form, "route": list(route), "marketing_start_date": start,
        "active_ingredients": [{"name": n, "strength": "1 mg/1"} for n in ingredients],
        "openfda": {},
    }
    if openfda:
        record["openfda"] = {"spl_set_id": [setid], "manufacturer_name": [labeler]}
        if original:
            record["openfda"]["is_original_packager"] = [True]
        if unii:
            record["openfda"]["unii"] = list(unii)
        if rxcui:
            record["openfda"]["rxcui"] = list(rxcui)
        if epc:
            record["openfda"]["pharm_class_epc"] = list(epc)
    return record


def spl_row(setid, title, labeler, upload="01/15/2024", version=3):
    return f"{setid}|20240115_{setid}.zip|{upload}|{version}|{title} [{labeler.upper()}]"


def write_inputs(folder: Path, records, spl_rows, rx_rows):
    ndc = folder / "ndc.json"
    ndc.write_text(json.dumps({"meta": {}, "results": records}), encoding="utf-8")
    meta = folder / "spl.txt"
    meta.write_text("SETID|ZIP_FILE_NAME|UPLOAD_DATE|SPL_VERSION|TITLE\n" + "".join(r + "\n" for r in spl_rows), encoding="utf-8")
    rx = folder / "rx.txt"
    rx.write_text("SETID|SPL_VERSION|RXCUI|RXSTRING|RXTTY\n" + "".join(r + "\n" for r in rx_rows), encoding="utf-8")
    return ndc, meta, rx


def fixture():
    """A small world: levothyroxine, apixaban, metformin, pembrolizumab, co-trimoxazole and things to exclude."""
    s = SETIDS
    records = [
        # levothyroxine: brand NDA, injection NDA (anhydrous salt), ANDA, and a repackager copy
        product("0074-1", s["synthroid"], "Levothyroxine Sodium", ["LEVOTHYROXINE SODIUM"], category="NDA",
                app="NDA021402", labeler="AbbVie Inc.", brand="SYNTHROID", start="20020224", unii=["9J765S329G"], rxcui=["966168"]),
        product("0075-1", s["levo_inj"], "Levothyroxine Sodium", ["LEVOTHYROXINE SODIUM ANHYDROUS"], category="NDA",
                app="NDA202231", labeler="Fresenius Kabi USA, LLC", form="INJECTION", route=("INTRAVENOUS",), start="20110624", unii=["054I36CPMN"]),
        product("0093-1", s["levo_anda"], "Levothyroxine Sodium", ["LEVOTHYROXINE SODIUM"], app="ANDA076187",
                labeler="Mylan Pharmaceuticals Inc.", start="20040101", unii=["9J765S329G"]),
        product("6362-1", s["levo_repack"], "Levothyroxine Sodium", ["LEVOTHYROXINE SODIUM"], app="ANDA076187",
                labeler="Bryant Ranch Prepack", original=False, unii=["9J765S329G"]),
        # apixaban: the NDA holder's record has no openfda block (BMS) and must come back through the title match;
        # a repackager record without openfda must not.
        product("0003-1", s["eliquis"], "Apixaban", ["APIXABAN"], category="NDA", app="NDA202155",
                labeler="E.R. Squibb & Sons, L.L.C.", brand="ELIQUIS", openfda=False, start="20121228"),
        product("0003-2", s["eliquis"], "Apixaban", ["APIXABAN"], category="NDA", app="NDA202155",
                labeler="E.R. Squibb & Sons, L.L.C.", brand="ELIQUIS", openfda=True, original=True, unii=["3Z9Y7UWC1J"]),
        # a line extension named further inside the same multi-product title
        product("0003-3", s["eliquis"], "Apixaban", ["APIXABAN"], category="NDA", app="NDA220073",
                labeler="E.R. Squibb & Sons, L.L.C.", brand="ELIQUIS SPRINKLE", openfda=False, form="PELLET"),
        product("5009-1", s["eliquis_repack"], "Apixaban", ["APIXABAN"], category="NDA", app="NDA202155",
                labeler="A-S Medication Solutions", brand="ELIQUIS", openfda=False),
        product("5009-2", s["eliquis_repack"], "Atorvastatin", ["ATORVASTATIN CALCIUM"], labeler="A-S Medication Solutions",
                original=False),
        # excluded worlds: a medical gas, saline, an allergen, a TPN cocktail, an irrigant
        product("1111-1", s["oxygen"], "Oxygen", ["OXYGEN"], category="NDA", app="NDA205000", labeler="Gas Co", form="GAS", route=("RESPIRATORY (INHALATION)",)),
        product("2222-1", s["saline"], "Sodium Chloride", ["SODIUM CHLORIDE"], category="NDA", app="NDA018000", labeler="Baxter"),
        product("3333-1", s["pollen"], "Timothy Pollen", ["PHLEUM PRATENSE POLLEN"], category="BLA", app="BLA101000",
                labeler="Greer", epc=["Standardized Pollen Allergenic Extract [EPC]"]),
        product("4444-1", s["tpn"], "Amino Acids", ["LEUCINE", "ISOLEUCINE", "VALINE", "LYSINE ACETATE", "METHIONINE"],
                category="NDA", app="NDA019000", labeler="Baxter"),
        product("5555-1", s["irrigant"], "Glycine", ["GLYCINE"], category="NDA", app="NDA017000", labeler="Baxter", route=("IRRIGATION",)),
        # metformin: one NDA (two setids for the same application), one NDA combination, AG, three ANDAs
        product("0087-1", s["metf_nda"], "Metformin Hydrochloride", ["METFORMIN HYDROCHLORIDE"], category="NDA",
                app="NDA020357", labeler="BMS", brand="GLUCOPHAGE", start="19950301", unii=["786Z46389E"]),
        product("0087-2", s["metf_nda_dup"], "Metformin Hydrochloride", ["METFORMIN HYDROCHLORIDE"], category="NDA",
                app="NDA020357", labeler="BMS", brand="GLUCOPHAGE XR", start="19960301", unii=["786Z46389E"]),
        product("0006-1", s["metf_combo"], "Sitagliptin and Metformin", ["SITAGLIPTIN PHOSPHATE", "METFORMIN HYDROCHLORIDE"],
                category="NDA", app="NDA022044", labeler="Merck", brand="JANUMET", start="20070401", unii=["TS63EW8X6F", "786Z46389E"]),
        product("6868-1", s["metf_ag"], "Metformin Hydrochloride", ["METFORMIN HYDROCHLORIDE"], category="NDA AUTHORIZED GENERIC",
                app="NDA020357", labeler="Prasco", start="20150101", unii=["786Z46389E"]),
        product("6818-1", s["metf_anda1"], "Metformin Hydrochloride", ["METFORMIN HYDROCHLORIDE"], app="ANDA075000",
                labeler="Lupin", start="20020124", unii=["786Z46389E"]),
        product("6555-1", s["metf_anda2"], "Metformin Hydrochloride", ["METFORMIN HYDROCHLORIDE"], app="ANDA076000",
                labeler="Zydus", start="20030101", unii=["786Z46389E"]),
        product("6560-1", s["metf_anda3"], "Metformin Hydrochloride", ["METFORMIN HYDROCHLORIDE"], app="ANDA077000",
                labeler="Aurobindo", start="20040101", unii=["786Z46389E"]),
        # pembrolizumab: single BLA, and a setid openFDA lists that DailyMed no longer has
        product("0006-2", s["keytruda"], "Pembrolizumab", ["PEMBROLIZUMAB"], category="BLA", app="BLA125514",
                labeler="Merck", brand="KEYTRUDA", unii=["DPT0O3T46P"], form="INJECTION", route=("INTRAVENOUS",)),
        product("0006-3", s["stale"], "Pembrolizumab", ["PEMBROLIZUMAB"], category="BLA", app="BLA125514",
                labeler="Merck", brand="KEYTRUDA OLD", unii=["DPT0O3T46P"]),
        # a combination whose second UNII is only resolvable by elimination
        product("0024-1", s["bactrim"], "Sulfamethoxazole and Trimethoprim", ["SULFAMETHOXAZOLE", "TRIMETHOPRIM"],
                category="NDA", app="NDA017377", labeler="Sun", brand="BACTRIM", unii=["JE42381TNV", "AN164J8Y0X"]),
        product("0024-2", s["trimethoprim_anda"], "Trimethoprim", ["TRIMETHOPRIM"], app="ANDA070000", labeler="Sun",
                unii=["AN164J8Y0X"]),
    ]
    spl_rows = [
        spl_row(s["synthroid"], "SYNTHROID (LEVOTHYROXINE SODIUM) TABLET", "AbbVie Inc.", upload="02/29/2024", version=1537),
        spl_row(s["levo_inj"], "LEVOTHYROXINE SODIUM INJECTION", "Fresenius Kabi USA, LLC"),
        spl_row(s["levo_anda"], "LEVOTHYROXINE SODIUM TABLET", "Mylan Pharmaceuticals Inc."),
        spl_row(s["levo_repack"], "LEVOTHYROXINE SODIUM TABLET", "Bryant Ranch Prepack"),
        spl_row(s["eliquis"], "ELIQUIS (APIXABAN) TABLET, FILM COATED ELIQUIS SPRINKLE (APIXABAN) PELLET", "E.R. Squibb & Sons, L.L.C."),
        spl_row(s["eliquis_repack"], "ELIQUIS (APIXABAN) TABLET, FILM COATED", "A-S Medication Solutions"),
        spl_row(s["oxygen"], "OXYGEN GAS", "Gas Co"),
        spl_row(s["saline"], "SODIUM CHLORIDE INJECTION", "Baxter"),
        spl_row(s["pollen"], "TIMOTHY POLLEN INJECTION", "Greer"),
        spl_row(s["tpn"], "AMINO ACIDS INJECTION", "Baxter"),
        spl_row(s["irrigant"], "GLYCINE IRRIGANT", "Baxter"),
        spl_row(s["metf_nda"], "GLUCOPHAGE (METFORMIN HYDROCHLORIDE) TABLET", "BMS"),
        spl_row(s["metf_nda_dup"], "GLUCOPHAGE XR (METFORMIN HYDROCHLORIDE) TABLET, EXTENDED RELEASE", "BMS"),
        spl_row(s["metf_combo"], "JANUMET (SITAGLIPTIN AND METFORMIN HYDROCHLORIDE) TABLET", "Merck"),
        spl_row(s["metf_ag"], "METFORMIN HYDROCHLORIDE TABLET", "Prasco"),
        spl_row(s["metf_anda1"], "METFORMIN HYDROCHLORIDE TABLET", "Lupin"),
        spl_row(s["metf_anda2"], "METFORMIN HYDROCHLORIDE TABLET", "Zydus"),
        spl_row(s["metf_anda3"], "METFORMIN HYDROCHLORIDE TABLET", "Aurobindo"),
        spl_row(s["keytruda"], "KEYTRUDA (PEMBROLIZUMAB) INJECTION", "Merck", upload="08/12/2026"),
        spl_row(s["bactrim"], "BACTRIM (SULFAMETHOXAZOLE AND TRIMETHOPRIM) TABLET", "Sun"),
        spl_row(s["trimethoprim_anda"], "TRIMETHOPRIM TABLET", "Sun"),
    ]
    rx_rows = [
        f"{s['synthroid']}|1537|966168|levothyroxine sodium 0.1 MG Oral Tablet [Synthroid]|SBD",
        f"{s['eliquis']}|3|1364447|apixaban 5 MG Oral Tablet [Eliquis]|SBD",
        f"{s['eliquis']}|3|1364447|Eliquis 5 MG Oral Tablet|SY",
    ]
    return records, spl_rows, rx_rows


def run_with_inputs(ndc: Path, meta: Path, rx: Path, out: Path, **overrides):
    argv = ["--ndc", str(ndc), "--spl-meta", str(meta), "--rxnorm", str(rx), "--out", str(out)]
    for key, value in overrides.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return sm.run(sm.parse_args(argv))


def run_selection(folder: Path, **overrides):
    records, spl_rows, rx_rows = fixture()
    ndc, meta, rx = write_inputs(folder, records, spl_rows, rx_rows)
    out = folder / "molecules"
    selection = run_with_inputs(ndc, meta, rx, out, **overrides)
    return out, selection


def load(out: Path, inn: str) -> dict:
    return json.loads((out / f"{inn}.json").read_text(encoding="utf-8"))


class MoietyTest(unittest.TestCase):
    def test_salts_esters_hydrates_and_suffixes_are_stripped(self):
        cases = {
            "LEVOTHYROXINE SODIUM": "LEVOTHYROXINE",
            "LEVOTHYROXINE SODIUM ANHYDROUS": "LEVOTHYROXINE",
            "METFORMIN HYDROCHLORIDE": "METFORMIN",
            "ATORVASTATIN CALCIUM TRIHYDRATE": "ATORVASTATIN",
            "ATORVASTATIN CALCIUM PROPYLENE GLYCOL SOLVATE": "ATORVASTATIN",
            "FLUTICASONE PROPIONATE": "FLUTICASONE",
            "FLUTICASONE FUROATE": "FLUTICASONE",
            "DEXAMETHASONE SODIUM PHOSPHATE": "DEXAMETHASONE",
            "ADALIMUMAB-ATTO": "ADALIMUMAB",
            "INSULIN GLARGINE-YFGN": "INSULIN GLARGINE",
            "TENOFOVIR DISOPROXIL FUMARATE": "TENOFOVIR DISOPROXIL",
            "VASOPRESSIN, UNSPECIFIED": "VASOPRESSIN",
            "DEXTROSE, UNSPECIFIED FORM": "DEXTROSE",
            "ANHYDROUS CITRIC ACID": "CITRIC ACID",
            "LITHIUM CARBONATE": "LITHIUM",
            "AMPHETAMINE DL-ASPARTATE MONOHYDRATE": "AMPHETAMINE",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(sm.moiety(name), expected)

    def test_electrolyte_cations_are_never_left_bare(self):
        self.assertEqual(sm.moiety("SODIUM PHOSPHATE, DIBASIC, ANHYDROUS"), "SODIUM PHOSPHATE")
        self.assertEqual(sm.moiety("MONOBASIC POTASSIUM PHOSPHATE"), "POTASSIUM PHOSPHATE")
        self.assertEqual(sm.moiety("MAGNESIUM SULFATE HEPTAHYDRATE"), "MAGNESIUM SULFATE")
        self.assertEqual(sm.moiety("SODIUM CHLORIDE"), "SODIUM CHLORIDE")

    def test_names_whose_anion_is_the_inn_are_protected(self):
        self.assertEqual(sm.moiety("DIMETHYL FUMARATE"), "DIMETHYL FUMARATE")
        self.assertEqual(sm.moiety("FERRIC CITRATE"), "FERRIC CITRATE")
        self.assertEqual(sm.moiety("BARIUM SULFATE"), "BARIUM SULFATE")
        self.assertEqual(sm.moiety("TENOFOVIR DISOPROXIL FUMARATE"), "TENOFOVIR DISOPROXIL")

    def test_aliases_and_distinct_prodrugs(self):
        self.assertEqual(sm.moiety("DIVALPROEX SODIUM"), "VALPROIC ACID")
        self.assertEqual(sm.moiety("VALPROATE SODIUM"), "VALPROIC ACID")
        self.assertEqual(sm.moiety("TENOFOVIR ALAFENAMIDE FUMARATE"), "TENOFOVIR ALAFENAMIDE")
        self.assertEqual(sm.moiety("MYCOPHENOLATE MOFETIL"), "MYCOPHENOLATE MOFETIL")
        self.assertEqual(sm.moiety("PEGINTERFERON ALFA-2A"), "PEGINTERFERON ALFA-2A")

    def test_slug(self):
        self.assertEqual(sm.inn_slug("TRIAMCINOLONE ACETONIDE"), "triamcinolone-acetonide")
        self.assertEqual(sm.inn_slug("HYALURONIDASE (HUMAN RECOMBINANT)"), "hyaluronidase-human-recombinant")


class SelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out, cls.selection = run_selection(Path(cls.tmp.name), molecules=10)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_salt_forms_group_into_one_molecule(self):
        levo = load(self.out, "levothyroxine")
        self.assertEqual(levo["name"], "LEVOTHYROXINE")
        self.assertEqual(levo["ingredient_names"], ["LEVOTHYROXINE SODIUM", "LEVOTHYROXINE SODIUM ANHYDROUS"])
        self.assertEqual({u["unii"] for u in levo["unii"]}, {"9J765S329G", "054I36CPMN"})
        self.assertEqual({u["setid"] for u in levo["us"]}, {SETIDS["synthroid"], SETIDS["levo_inj"], SETIDS["levo_anda"]})
        self.assertEqual(levo["eu"], [])
        self.assertIn("966168", levo["rxcui"])

    def test_repackagers_are_excluded_but_counted(self):
        levo = load(self.out, "levothyroxine")
        self.assertNotIn(SETIDS["levo_repack"], {u["setid"] for u in levo["us"]})
        self.assertEqual(levo["stats"]["repackagers"], 1)
        self.assertEqual(levo["stats"]["approval_holders"], 3)

    def test_innovator_without_openfda_block_is_recovered_from_dailymed_title(self):
        apix = load(self.out, "apixaban")
        setids = {u["setid"]: u for u in apix["us"]}
        self.assertIn(SETIDS["eliquis"], setids)
        self.assertNotIn(SETIDS["eliquis_repack"], setids, "a repackager code (0% flagged) must not be recovered")
        self.assertEqual(setids[SETIDS["eliquis"]]["application_numbers"], ["NDA202155", "NDA220073"])
        self.assertIn("1364447", setids[SETIDS["eliquis"]]["rxcui"])
        self.assertEqual(self.selection["diagnostics"]["fallback_products_resolved"], 2)
        self.assertEqual(apix["stats"]["nda_bla_applications"], 2)

    def test_gases_electrolytes_allergens_cocktails_and_irrigants_are_excluded(self):
        files = {p.stem for p in self.out.glob("*.json")}
        for name in ("oxygen", "sodium-chloride", "phleum-pratense-pollen", "leucine", "glycine"):
            self.assertNotIn(name, files)
        self.assertEqual(self.selection["counts"]["candidate_molecules"], 7)

    def test_not_current_setid_is_logged_not_selected(self):
        self.assertEqual(self.selection["diagnostics"]["setids_not_in_dailymed_current"], [SETIDS["stale"]])
        keytruda = load(self.out, "pembrolizumab")
        self.assertEqual([u["setid"] for u in keytruda["us"]], [SETIDS["keytruda"]])
        self.assertTrue(keytruda["biologic"])

    def test_combination_is_listed_under_each_component_and_homed_once(self):
        metf = load(self.out, "metformin")
        sita = load(self.out, "sitagliptin")
        combo_in_metf = next(u for u in metf["us"] if u["setid"] == SETIDS["metf_combo"])
        combo_in_sita = next(u for u in sita["us"] if u["setid"] == SETIDS["metf_combo"])
        self.assertTrue(combo_in_metf["combination"])
        self.assertEqual(combo_in_metf["moieties"], ["METFORMIN", "SITAGLIPTIN"])
        self.assertEqual(combo_in_metf["home_inn"], combo_in_sita["home_inn"])
        self.assertLess(metf["rank"], sita["rank"])
        self.assertEqual(combo_in_metf["home_inn"], "metformin")

    def test_unii_resolved_by_elimination_for_combination_only_ingredient(self):
        sulfa = load(self.out, "sulfamethoxazole")
        self.assertEqual([u["unii"] for u in sulfa["unii"]], ["JE42381TNV"])
        self.assertEqual(sulfa["unii_unresolved"], [])

    def test_ranking_interleaves_breadth_and_innovator(self):
        ranked = [(m["inn"], m["selected_by"]) for m in self.selection["molecules"]]
        self.assertEqual(ranked[0], ("metformin", "breadth"))      # most approval holders
        self.assertEqual(ranked[1][1], "innovator")
        self.assertEqual(ranked[1][0], "levothyroxine")             # most NDA/BLA applications after metformin
        self.assertEqual(len(ranked), 7)

    def test_selection_json_has_hashes_counts_and_estimate(self):
        inputs = {i["role"]: i for i in self.selection["inputs"]}
        self.assertEqual(set(inputs), {"openfda_ndc", "dailymed_spl_meta", "dailymed_rxnorm"})
        for role in inputs:
            self.assertRegex(inputs[role]["sha256"], r"^[0-9a-f]{64}$")
        counts = self.selection["counts"]
        self.assertEqual(counts["selected_molecules"], 7)
        self.assertEqual(counts["selected_setids"], sum(counts["selected_setids_by_category"].values()))
        est = self.selection["request_estimate"]
        self.assertEqual(est["requests"], counts["selected_setids"] * 13)


class CapTest(unittest.TestCase):
    def test_caps_keep_innovators_then_generics_and_log_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, selection = run_selection(Path(tmp), molecules=1, cap_nda=2, cap_generic=2)
            metf = load(out, "metformin")
            kept = [u["setid"] for u in metf["us"]]
            # NDA class: single-ingredient Glucophage first, then the combination (fresh application), the
            # second Glucophage setid (same NDA) drops behind both and is cut by the cap of 2.
            self.assertEqual(kept[:2], [SETIDS["metf_nda"], SETIDS["metf_combo"]])
            # generic class: authorized generic before ANDAs, then the oldest-marketed ANDA.
            self.assertEqual(kept[2:], [SETIDS["metf_ag"], SETIDS["metf_anda1"]])
            dropped = {e["setid"]: e["reason"] for e in metf["us_excluded"]}
            self.assertEqual(set(dropped), {SETIDS["metf_nda_dup"], SETIDS["metf_anda2"], SETIDS["metf_anda3"]})
            self.assertIn("NDA/BLA", dropped[SETIDS["metf_nda_dup"]])
            self.assertIn("ANDA", dropped[SETIDS["metf_anda2"]])
            self.assertEqual(selection["counts"]["selected_setids"], 4)
            self.assertEqual(selection["counts"]["setids_excluded_over_cap"], 3)
            self.assertEqual(selection["request_estimate"]["requests"], 4 * 13)

    def test_molecule_count_bounds_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, selection = run_selection(Path(tmp), molecules=3)
            self.assertEqual(len(list(out.glob("*.json"))), 4)  # 3 molecules + selection.json
            self.assertEqual(selection["counts"]["selected_molecules"], 3)


class ManualAddTest(unittest.TestCase):
    def test_add_writes_new_files_only_with_manual_ranks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out, selection = run_selection(tmp, molecules=2)
            ranked = [m["inn"] for m in selection["molecules"]]
            self.assertNotIn("apixaban", ranked)
            before = {p.name: p.read_text() for p in out.glob("*.json") if p.name != "selection.json"}
            first = sorted(before)[0]
            edited = json.loads(before[first])
            edited["eu"] = [{"label_id": "EMEA-H-C-000001"}]          # a later stage's field must survive
            (out / first).write_text(json.dumps(edited))
            argv = ["--ndc", str(tmp / "ndc.json"), "--spl-meta", str(tmp / "spl.txt"), "--rxnorm", str(tmp / "rx.txt"),
                    "--out", str(out), "--add", "apixaban", "pembrolizumab"]
            self.assertEqual(sm.main(argv), 0)
            api = load(out, "apixaban")
            self.assertEqual((api["rank"], api["selected_by"], api["eu"]), (3, "manual", []))
            self.assertEqual([u["setid"] for u in api["us"]][0], SETIDS["eliquis"])
            self.assertEqual(api["us"][0]["home_inn"], "apixaban")
            if "pembrolizumab" not in ranked:
                self.assertEqual(load(out, "pembrolizumab")["rank"], 4)
            self.assertEqual(json.loads((out / first).read_text())["eu"], [{"label_id": "EMEA-H-C-000001"}])
            for name, text in before.items():
                if name != first:
                    self.assertEqual((out / name).read_text(), text)    # existing files untouched
            manual = json.loads((out / "selection.json").read_text())["manual_additions"]
            self.assertEqual([m["inn"] for m in manual["molecules"]][0], "apixaban")
            self.assertIn("selected_by 'manual'", manual["rule"])
            again = (out / "apixaban.json").read_text()
            self.assertEqual(sm.main(argv), 0)                          # idempotent
            self.assertEqual((out / "apixaban.json").read_text(), again)
            self.assertEqual(sm.main([*argv[:-3], "--add", ranked[0]]), 1)   # already ranked: refused
            self.assertEqual(sm.main([*argv[:-3], "--add", "unobtainium"]), 1)


class DeterminismTest(unittest.TestCase):
    def test_two_runs_on_the_same_inputs_give_identical_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, spl_rows, rx_rows = fixture()
            ndc, meta, rx = write_inputs(Path(tmp), records, spl_rows, rx_rows)
            digests = []
            for run in ("a", "b"):
                out = Path(tmp) / run
                run_with_inputs(ndc, meta, rx, out)
                digests.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.glob("*.json"))})
        self.assertEqual(digests[0], digests[1])
        self.assertIn("selection.json", digests[0])
        self.assertGreater(len(digests[0]), 1)


class ErrorTest(unittest.TestCase):
    def test_missing_input_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = sm.parse_args(["--ndc", str(Path(tmp) / "missing.json"), "--spl-meta", str(Path(tmp) / "x"),
                                  "--rxnorm", str(Path(tmp) / "y"), "--out", str(Path(tmp) / "out")])
            with self.assertRaises(sm.SelectionError) as ctx:
                sm.run(args)
            self.assertIn("missing.json", str(ctx.exception))
            self.assertEqual(sm.main(["--ndc", str(Path(tmp) / "missing.json")]), 1)

    def test_malformed_ndc_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"meta": {}}', encoding="utf-8")
            with self.assertRaises(sm.SelectionError):
                sm.load_ndc(bad)


if __name__ == "__main__":
    unittest.main()
