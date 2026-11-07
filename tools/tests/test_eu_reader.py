"""The EU reader in labels.py, the MCP server and the CLI over a built EU repo.

The fixture EU repo is written in a temp dir by tools/eu.py's own build-plan functions (plan_commits and
write_repo, no PyMuPDF): Alpha (in force, four decisions: a same-date corrigendum published 60 days later
and an annex identical to the first one), Beta (current-only) and Gamma (in force, two decisions, the second
a corrigendum whose corrected procedure is not identified). A temp molecules dir carries us[] setids of the
US fixture of test_mcp and eu[] entries written by eu.crosswalk. The compare tests need that US fixture
(pinned raw zips) and skip without it; the other tests then run against a one-commit stub US repo.
No test may touch the network. FullEuCorpusTest runs only when ASOF_FULL_EU_REPO points at build/eu
(and ASOF_FULL_REPO at build/us for the compare checks).
"""
import atexit
import io
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli  # noqa: E402
import eu  # noqa: E402
import labels  # noqa: E402
import mcp_server  # noqa: E402
import notices  # noqa: E402
import test_mcp  # noqa: E402

HERE = Path(__file__).parent
MOLECULES = TOOLS.parent / "molecules"
IDENT = {"converter": "9", "pymupdf": "0.0"}
WORDING_START = "The Union Register carries this text as Annex I of Commission decision"
TOOL_NAMES = ["find_label", "read_label", "section_history", "blame", "diff", "changes_between", "search",
              "first_appearance", "compare_jurisdictions", "check_citations", "coverage"]

SYN = "1e11ad30-1041-4520-10b0-8f9d30d30fcc"          # Synthroid: primary US label of alphamab (brand ALPHA)
IPRA = "3fd850a2-af0f-4291-af5d-52c8d6094e64"         # second NDA candidate of alphamab
HCQ_ANDA = "6a9e2fc8-bedf-46d4-9973-328dbcb18061"     # ANDA: never a candidate
COMBO = "0bbee292-0fba-4606-9c8e-4723742682c0"        # combination: never a candidate
PLAQUENIL = "34496b43-05a2-45fb-a769-52b12e099341"    # gammavir
AG = "84b00366-96ef-41e1-bac6-5d24acdb9e1d"           # NDA AUTHORIZED GENERIC: not a candidate
DOXEPIN = "f350afed-120b-422d-9e6e-3aaa0f72a5fb"      # betanib
NEWHCQ = "7e09aeca-0cec-4a88-9dde-a2098a08b877"       # deltanib (no EU reference)
STUB = "aaaaaaaa-0000-4000-8000-000000000001"
ALPHA, BETA, GAMMA = "EMEA-H-C-000001", "EMEA-H-C-000002", "EMEA-H-C-000003"
US_SHARED = "SYNTHROID is contraindicated in patients with uncorrected adrenal insufficiency [see Warnings and Precautions (5.3)]."
# The same sentence as the EU fixture prints it: a list marker, a non-breaking space and no final full stop.
EU_SHARED = "- SYNTHROID is contraindicated in patients with uncorrected adrenal insufficiency [see Warnings and Precautions (5.3)]"


def _refuse(*args, **kwargs):
    raise AssertionError("network access attempted during an EU reader test")


def setUpModule():
    global _socket_backup
    _socket_backup = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)
    socket.socket.connect = _refuse
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse


def tearDownModule():
    socket.socket.connect, socket.create_connection, socket.getaddrinfo = _socket_backup


HAVE_US = test_mcp.raw_available()
skip_without_us = unittest.skipUnless(HAVE_US, "pinned raw zips not present; run tools/build.py fetch first")


# ---------------------------------------------------------------- fixture

def ref(dir_name, name, eu_number, comparable, note):
    return {"dir": dir_name, "name": name, "register_name": name.upper(), "eu_number": eu_number,
            "ema_product_number": dir_name.replace("-", "/"), "mah": f"{name} Holder Pharma", "generic": "No",
            "biosimilar": "No", "medicine_status": "Authorised", "marketing_authorisation_date": "2010-03-01",
            "active_substance": name.lower(), "h_page_url": "https://ec.europa.eu/health/documents/community-register/html/h1.htm",
            "medicine_url": f"https://www.ema.europa.eu/en/medicines/human/EPAR/{name.lower()}",
            "comparable_to_us": comparable, "comparability_note": note}


def annex(pid, date, sha, closed, kind="Centralised - Variation", status="fetched", reason=None):
    return {"procedure_id": pid, "decision_date": date, "decision_number": f"({date[:4]}) {pid}", "sha256": sha,
            "status": status, "reason": reason, "type": kind, "ema_number": f"EMEA/H/C/1/II/{pid}", "closed": closed,
            "url": f"https://ec.europa.eu/health/documents/community-register/x/anx_{pid}_en.pdf",
            "annex_languages": ["de", "en"]}


TITLES = {n: (stem, title) for n, (stem, title) in labels.EU_SECTIONS.items()}


def sections(**by_number) -> dict:
    """{'4.3': [lines]} as keyword arguments s4_3=[...] -> converter-result sections {stem: text}."""
    out = {}
    for key, lines in by_number.items():
        number = key[1:].replace("_", ".")
        stem, title = TITLES[number]
        out[stem] = f"# {number} {title}\n\n" + "\n".join(lines) + "\n"
    return out


def conversion(status="converted", secs=None):
    return {"status": status, "sections": secs or {}, "warnings": [], "smpcs": [{}] if secs else []}


ALPHA_V1 = sections(s4_1=["Hypothyroidism in adults."],
                    s4_3=["Hypersensitivity to the active substance or to any of the excipients."],
                    s4_4=["Thyroid function should be monitored."], s4_8=["Palpitations."])


def alpha_v2(typo: bool) -> dict:
    return sections(s4_1=["Hypothyroidism in adults.", "Thyroid cancer as adjunct therapy."],
                    s4_2=["One tablet daily, taken in the morning."],
                    s4_3=[EU_SHARED, "Hypersensitivity to the active substance or to any of the excipients.",
                          "Acute myocardial infarction " + ("withn" if typo else "within") + " the last month."],
                    s4_4=["Thyroid function should be monitored.", "Cardiac adverse reactions may occur in the elderly."],
                    s4_6=["Treatment should be continued during pregnancy."],
                    s4_8=["| System organ class | Reaction |", "| --- | --- |", "| Cardiac disorders | Palpitations |"],
                    s5_1=["Levothyroxine is a synthetic thyroid hormone."])


def fake_inputs():
    a = ref(ALPHA, "Alpha", "EU/1/10/001", "yes", "EU name matches the US NDA/BLA brand ALPHA")
    b = ref(BETA, "Beta", "EU/1/15/002", "unknown", "no US brand starts with Beta")
    g = ref(GAMMA, "Gamma", "EU/1/12/003", "no", "hand-checked: another formulation than the US product")
    alt = {"ema_product_number": "EMEA/H/C/000009", "name": "Alpha Generic", "generic": "Yes", "biosimilar": "No",
           "eu_number": "EU/1/20/009", "mah": "Other", "medicine_status": "Authorised",
           "marketing_authorisation_date": "2020-01-01", "medicine_url": "https://www.ema.europa.eu/x9"}
    blank = {"combinations": [], "near_misses_not_matched": [], "no_reference_reason": None}
    delta_alt = {**alt, "ema_product_number": "EMEA/H/C/000041", "name": "Deltanib Teva (previously Deltanib Actavis)",
                 "eu_number": "EU/1/21/041"}
    products = {"molecules": {
        "alphamab": {"name": "ALPHAMAB", "reference": a, "alternates": [alt], **blank},
        "betanib": {"name": "BETANIB", "reference": b, "alternates": [], **blank},
        "gammavir": {"name": "GAMMAVIR", "reference": g, "alternates": [], **blank},
        "deltanib": {"name": "DELTANIB", "reference": None, "alternates": [delta_alt], "combinations": [],
                     "near_misses_not_matched": [{"name": "Deltasalt", "ema_product_number": "EMEA/H/C/000040",
                                                  "kind": "salt", "inn_field": "deltanib sulfate", "generic": "No",
                                                  "biosimilar": "No", "marketing_authorisation_date": "2005-01-01"}],
                     "no_reference_reason": "no central product with this exact INN in the EMA medicines JSON; not "
                                            "matched by the v1 rule (exact INN): Deltasalt EMEA/H/C/000040 "
                                            "('deltanib sulfate', salt)"}}}
    h = {"fetched": "2026-11-01T10:00:00Z"}
    alpha = [annex("100", "2010-03-01", "a1", "2010-03-05"), annex("200", "2019-06-01", "a2", "2019-06-01"),
             annex("201", "2019-06-01", "a3", "2019-07-31", "Centralised - Corrigendum"),
             annex("300", "2021-01-10", "a1", "2021-01-15", status="duplicate",
                   reason="identical file to the annex of procedure 100")]
    gamma = [annex("400", "2012-01-01", "g1", "2012-01-03"),
             annex("500", "2020-05-05", "g2", "2020-09-01", "Centralised - Corrigendum")]
    records = [{"inn": "alphamab", "ref": a, "annexes": alpha},
               {"inn": "betanib", "ref": b, "annexes": [annex("600", "2015-02-02", "b0", "2015-02-02")]},
               {"inn": "gammavir", "ref": g, "annexes": gamma}]
    for rec in records:
        rec["src"] = {"h_page": h, "annexes": list(rec["annexes"]), "procedure_counts": {}}
    gamma_44 = ["Use with care in renal impairment.", "", "[Gamma 20 mg tablets]", "(all sentences already above)"]
    conversions = {
        "a1": conversion(secs=ALPHA_V1), "a2": conversion(secs=alpha_v2(True)), "a3": conversion(secs=alpha_v2(False)),
        "b0": conversion("no-text-layer"),
        "bpi": conversion(secs=sections(s4_1=["Betanib treats migraine."], s4_3=["Hypersensitivity."],
                                        s4_4=["Serotonin syndrome may occur."], s4_8=["Dizziness."])),
        "g1": conversion(secs=sections(s4_1=["Herpes zoster."], s4_3=["Hypersensitivity to gammavir."],
                                       s4_4=gamma_44, s4_8=["Headache."])),
        "g2": conversion(secs=sections(s4_1=["Herpes zoster."], s4_3=["Hypersensitivity to gammavir."],
                                       s4_4=gamma_44[:1] + ["Liver injury has been reported."] + gamma_44[1:],
                                       s4_8=["Headache."])),
    }
    captures = {BETA: ("2026-11-01", {"pi_sha256": "bpi", "last_updated_date": "2026-10-01",
                                      "pi_url": "https://www.ema.europa.eu/en/documents/product-information/beta_en.pdf"})}
    return products, records, conversions, captures


def us_entry(setid, brand, categories=("NDA",), combination=False):
    return {"setid": setid, "brand_names": [brand], "marketing_categories": list(categories),
            "combination": combination, "product_ndcs": [], "rxcui": []}


MOLECULE_US = {
    "alphamab": [us_entry(SYN, "ALPHA"), us_entry(IPRA, "OTHERBRAND"), us_entry(HCQ_ANDA, "HCQ", ("ANDA",)),
                 us_entry(COMBO, "COMBO", combination=True)],
    "betanib": [us_entry(DOXEPIN, "SILENOR")],
    "gammavir": [us_entry(PLAQUENIL, "ZETA"), us_entry(AG, "YOTA", ("NDA AUTHORIZED GENERIC",))],
    "deltanib": [us_entry(NEWHCQ, "DELTA")],
}


def stub_us_repo(path: Path) -> Path:
    """A one-commit US repo (one label) for the EU tests when the US fixture cannot be built."""
    folder = path / "levothyroxine" / STUB
    folder.mkdir(parents=True)
    (folder / "label.json").write_text(json.dumps({"setid": STUB, "labeler": "Stub"}) + "\n", encoding="utf-8")
    (folder / "34070-3-contraindications.md").write_text("# 4 CONTRAINDICATIONS\n\nNone.\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@labels.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@labels.invalid", "GIT_AUTHOR_DATE": "2009-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2009-01-01T00:00:00Z"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "core.hooksPath=/dev/null", "commit", "-q", "-m",
                  f"Stub — label version 1\n\nSource-Id: dailymed:{STUB}\nSource-Version: 1\nPublished-Date: 2009-01-01\n"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, env=env, capture_output=True)
    return path


_fixture: dict | None = None


def fixture() -> dict:
    """Build the EU repo, the molecules dir and the US index once per test process."""
    global _fixture
    if _fixture is None:
        tmp = tempfile.TemporaryDirectory()
        atexit.register(tmp.cleanup)
        root = Path(tmp.name)
        products, records, conversions, captures = fake_inputs()
        products_path = root / "eu-products.json"
        eu.write_json(products_path, products)
        commits, outcomes = eu.plan_commits(products, records, conversions, captures)
        repo = root / "eu"
        eu.prepare_out_eu(repo, clean=False)
        hashes = eu.write_repo(repo, commits, outcomes, conversions.__getitem__, IDENT)
        eu.sync_products(outcomes, products_path)
        mols = root / "molecules"
        mols.mkdir()
        for inn, entries in MOLECULE_US.items():
            (mols / f"{inn}.json").write_text(eu.molecule_text(
                {"inn": inn, "name": inn.upper(), "ingredient_names": [inn], "eu": [], "us": entries, "rank": 1}),
                encoding="utf-8")
        res = eu.crosswalk(mols, products_path, repo)
        assert not res["problems"], res["problems"]
        us_repo = test_mcp.fixture()["repo"] if HAVE_US else stub_us_repo(root / "us")
        _fixture = {"repo": repo, "hashes": hashes, "mols": mols, "us_repo": us_repo,
                    "us": labels.Index(us_repo, mols), "eu": labels.EuIndex(repo, mols)}
    return _fixture


def run_cli(*argv) -> tuple[int, str, str]:
    f = fixture()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["--repo", str(f["us_repo"]), "--molecules", str(f["mols"]), "--eu-repo", str(f["repo"]),
                         *argv])
    return code, out.getvalue(), err.getvalue()


def eu_words(obj) -> str:
    """The EU wording fields of an answer (never the label text itself), lower-cased, for the banned-word check."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("wording", "corrigendum_note", "record_note", "notice", "criteria", "reason", "note"):
                    found.append(str(v))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return " ".join(found).lower()


class _StubIndex:
    """Just enough of labels.Index for the molecule-resolution and US-label-pick helpers."""

    def __init__(self, molecules=None, labels_=None):
        self._molecules = molecules or {}
        self._labels = labels_ or {}
        self.by_setid = {s: None for s in self._labels}

    def molecules(self):
        return self._molecules

    def resolve_molecule(self, query):
        if query.strip().lower() in self._molecules:
            return query.strip().lower(), self._molecules[query.strip().lower()]
        raise labels.LabelError(f"no molecule matches {query!r}")

    def label_versions(self, setid):
        first, count, _ = self._labels[setid]
        return [labels.Version("c", setid, n + 1, first, first, "plr") for n in range(count)]

    def status(self, setid):
        state = self._labels[setid][2]
        if state == "current":
            return {"status": "current"}
        return {"status": "retired", "last_published": state[1], "successor_setids": []}

    def label_title(self, setid):
        return setid


# ---------------------------------------------------------------- helpers without a repo

class EuHelpersTest(unittest.TestCase):
    def test_ids(self):
        numbers = {(1, 15, 1024): "EMEA-H-C-003820"}
        for form in ("EMEA/H/C/003820", "EMEA-H-C-003820", "emea-h-c-3820", "eu:EMEA/H/C/3820", "EU:emea-h-c-003820",
                     "EU/1/15/1024", "eu:eu/1/15/1024"):
            with self.subTest(form=form):
                self.assertTrue(labels.is_eu_id(form))
                self.assertEqual(labels.canonical_eu_id(form, numbers), "EMEA-H-C-003820")
        for us_id in ("1e11ad30", "dailymed:1e11ad30-1041-4520-10b0-8f9d30d30fcc", "", None, 5):
            self.assertFalse(labels.is_eu_id(us_id))
        self.assertIsNone(labels.canonical_eu_id("EU/1/99/999", numbers))
        self.assertIsNone(labels.canonical_eu_id("EMEA/V/C/000001", numbers))

    def test_section_keys(self):
        files = [f"x/{ALPHA}/{stem}.md" for stem in ("smpc-4-clinical-particulars", "smpc-4.3-contraindications",
                                                      "smpc-4.4-special-warnings-and-precautions-for-use",
                                                      "smpc-4.9-overdose", "smpc-0-additional-monitoring")]
        for key, stem in (("4.3", "smpc-4.3-contraindications"), ("smpc-4.3", "smpc-4.3-contraindications"),
                          ("smpc-4.3-contraindications", "smpc-4.3-contraindications"),
                          ("contraindications", "smpc-4.3-contraindications"), ("overdose", "smpc-4.9-overdose"),
                          ("4", "smpc-4-clinical-particulars"), ("0", "smpc-0-additional-monitoring"),
                          ("special warnings", "smpc-4.4-special-warnings-and-precautions-for-use")):
            with self.subTest(key=key):
                self.assertTrue(labels.pick_eu_section(files, key).endswith(f"/{stem}.md"))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_eu_section(files[1:], "4")      # '4' never names 4.1 .. 4.9
        self.assertIn("4.3 (smpc-4.3-contraindications)", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_eu_section(files, "34070-3")
        self.assertIn("US LOINC code", str(ctx.exception))
        self.assertIn("available:", str(ctx.exception))
        self.assertEqual(labels.eu_section_entry(files[1]),
                         {"code": "4.3", "title": "Contraindications", "file": "smpc-4.3-contraindications.md"})
        self.assertEqual(labels.eu_section_entry(files[4])["title"], "Additional monitoring")

    def test_section_pair_words_and_whole_slug_words(self):
        stems = [stem for n, (stem, _) in labels.EU_SECTIONS.items() if n.startswith(("4", "6"))]
        files = [f"x/{ALPHA}/{stem}.md" for stem in stems]
        for key, number in (("indications", "4.1"), ("dosage", "4.2"), ("posology", "4.2"),
                            ("contraindications", "4.3"), ("precautions", "4.4"), ("warnings", "4.4"),
                            ("special warnings", "4.4"), ("special-warnings-and-precautions-for-use", "4.4"),
                            ("interactions", "4.5"), ("adverse", "4.8"), ("adverse reactions", "4.8"),
                            ("overdosage", "4.9"), ("overdose", "4.9"), ("excipients", "6.1"),
                            ("shelf life", "6.3"), ("storage", "6.4"), ("disposal", "6.6")):
            with self.subTest(key=key):
                self.assertEqual(labels.eu_section_code(labels.pick_eu_section(files, key)), number)
        for key in ("indica", "dications", "contra"):                    # parts of a word never match
            with self.subTest(key=key), self.assertRaises(labels.LabelError) as ctx:
                labels.pick_eu_section(files, key)
            self.assertIn("is not in this version", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_eu_section(files, "special-precautions")         # 6.4 and 6.6
        self.assertIn("is ambiguous", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_eu_section([f for f in files if "4.5" not in f], "interactions")
        self.assertIn("is not in this version", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_eu_section(files, "34073-7")
        self.assertIn("US LOINC code", str(ctx.exception))

    def test_eu_name_forms_and_resolution(self):
        self.assertEqual(labels.eu_name_forms("Alpha  Ellipta (previously Alpha)"),
                         {"alpha ellipta (previously alpha)", "alpha ellipta", "alpha"})
        self.assertEqual(labels.eu_name_forms("Nexium Control"), {"nexium control"})
        stub = _StubIndex(molecules={
            "alphamab": {"eu": [{"name": "Alpha Ellipta (previously Alpha)", "role": "reference"}]},
            "betanib": {"eu": [{"name": "Shared Name", "role": "alternate"}]},
            "gammavir": {"eu": [{"name": "Shared Name (previously Gamma)", "role": "alternate"}]}})
        for query in ("Alpha Ellipta", "alpha  ellipta", "Alpha", "ALPHA ELLIPTA (previously Alpha)"):
            with self.subTest(query=query):
                self.assertEqual(labels.resolve_molecule_any(stub, query)[0], "alphamab")
        with self.assertRaises(labels.LabelError) as ctx:
            labels.resolve_molecule_any(stub, "Shared Name")
        self.assertIn("matches several molecules: betanib, gammavir", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.resolve_molecule_any(stub, "Ellipta")
        self.assertIn("no molecule matches", str(ctx.exception))

    def test_pick_primary_order_and_pairing_caveat(self):
        stub = _StubIndex(labels_={
            "s-invega": ("2007-01-01", 10, "current"), "s-sustenna": ("2009-01-01", 40, "current"),
            "s-rapamune": ("2000-01-01", 50, ("retired", "2026-10-26")), "s-fyarro": ("2021-11-01", 12, "current"),
            "s-late": ("2027-06-01", 1, "current")})
        cands = [("s-invega", {"brand_names": ["INVEGA"]}), ("s-sustenna", {"brand_names": ["INVEGA SUSTENNA"]})]
        note = "EU name matches the US NDA/BLA brand INVEGA"
        self.assertEqual(labels._pick_primary(stub, cands, "Invega", note),
                         (cands[0], "brand named in comparability_note"))
        self.assertEqual(labels._pick_primary(stub, cands, "Invega", None),       # the whole-brand tie rule
                         (cands[0], "brand equals the EU product name"))
        self.assertEqual(labels._pick_primary(stub, cands, "Invega Depot", None),
                         (cands[0], "brand first word equals the EU product name's first word"))
        self.assertEqual(labels._pick_primary(stub, cands, "Other", None)[0], cands[1])   # most versions
        self.assertEqual(labels._pick_primary(stub, cands, "Invega (previously Xeplion)", None)[1],
                         "brand equals the EU product name")
        mol = {"us": [{"setid": "s-rapamune", "brand_names": ["RAPAMUNE"], "marketing_categories": ["NDA"]},
                      {"setid": "s-fyarro", "brand_names": ["FYARRO"], "marketing_categories": ["NDA"]}]}
        in_force, _ = labels._us_candidates(stub, mol, "2026-11-01")
        self.assertEqual([s for s, _ in in_force], ["s-fyarro"])            # the retired note brand drops out
        rnote = "EU name matches the US NDA/BLA brand RAPAMUNE"
        chosen, how = labels._pick_primary(stub, in_force, "Rapamune", rnote)
        self.assertEqual((chosen[0], how), ("s-fyarro", "most versions (ties: earliest first version, then setid)"))
        self.assertEqual(labels.pairing_caveat(stub, mol, chosen, rnote, "2026-11-01"),
                         "the US label named in the comparability note (RAPAMUNE, setid s-rapamune) is retired; "
                         "DailyMed last showed it on 2026-10-26 (its last published version), so it is not a "
                         "candidate on 2026-11-01; compared FYARRO (setid s-fyarro) instead, a different US product")
        self.assertIsNone(labels.pairing_caveat(stub, mol, chosen, rnote.replace("RAPAMUNE", "FYARRO"), "2026-11-01"))
        self.assertIsNone(labels.pairing_caveat(stub, mol, chosen, "no US brand starts with Rapamune", "2026-11-01"))
        self.assertIsNone(labels.pairing_caveat(stub, mol, None, rnote, "2026-11-01"))
        late = {"us": mol["us"] + [{"setid": "s-late", "brand_names": ["LATER"], "marketing_categories": ["NDA"]}]}
        self.assertIn("(LATER, setid s-late) is not yet published on 2026-11-01 (record starts 2027-06-01)",
                      labels.pairing_caveat(stub, late, chosen, rnote.replace("RAPAMUNE", "LATER"), "2026-11-01"))
        anda = {"us": mol["us"] + [{"setid": "s-late", "brand_names": ["GEN"], "marketing_categories": ["ANDA"]}]}
        self.assertIn("(GEN, setid s-late) is not an NDA/BLA single-ingredient label",
                      labels.pairing_caveat(stub, anda, chosen, rnote.replace("RAPAMUNE", "GEN"), "2026-11-01"))
        self.assertIn("(NOPE) is not in molecules us[]",
                      labels.pairing_caveat(stub, mol, chosen, rnote.replace("RAPAMUNE", "NOPE"), "2026-11-01"))
        self.assertEqual(labels.us_retired_candidates(stub, mol, "2026-11-01"),
                         [{"id": "s-rapamune", "brand": "RAPAMUNE", "last_published": "2026-10-26"}])
        self.assertEqual(labels.us_retired_candidates(stub, mol, "2026-10-26"), [])

    def test_compare_key(self):
        same = ["The patient’s “dose” — 5 mg.", "- the patient's \"dose\" - 5 mg", "1. The  patient's \"dose\" – 5 mg;",
                "* THE PATIENT'S \"DOSE\" − 5 MG:"]
        self.assertEqual({labels.compare_key(s) for s in same}, {"the patient's \"dose\" - 5 mg"})
        self.assertEqual(labels.compare_key("Dose ≤ 10 mg."), "dose ≤ 10 mg")       # no symbol-to-word mapping
        self.assertEqual(labels.compare_key("café"), labels.compare_key("café"))  # NFC
        self.assertEqual(labels.compare_key(EU_SHARED), labels.compare_key(US_SHARED))
        text = "# 4.4 Warnings\n\nA line.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n[Gamma 20 mg]\n(all sentences already above)\n"
        self.assertEqual([k for _, k in labels.compare_lines(text, eu_markers=True)], ["a line", "| a | b |", "| 1 | 2 |"])
        self.assertIn("[gamma 20 mg]", [k for _, k in labels.compare_lines(text)])

    def test_pairs(self):
        self.assertEqual([(p.eu, p.us, p.name) for p in labels.PAIRS][:4],
                         [("4.1", "34067-9", "indications"), ("4.2", "34068-7", "dosage"),
                          ("4.3", "34070-3", "contraindications"), ("4.4", "43685-7", "warnings")])
        self.assertEqual([p.eu for p in labels.PAIRS], ["4.1", "4.2", "4.3", "4.4", "4.5", "4.6", "4.8", "4.9"])
        for key, name in (("43685-7", "warnings"), ("4.4", "warnings"), ("smpc-4.3", "contraindications"),
                          ("Contraindications", "contraindications"), ("42228-7", "pregnancy-and-lactation"),
                          ("overdose", "overdosage")):
            self.assertEqual(labels.pair_for(key).name, name)
        self.assertIsNone(labels.pair_for("34089-3"))
        self.assertIsNone(labels.pair_for("5.1"))
        self.assertEqual(labels._translate_section("43685-7"), ("43685-7", "4.4", None))
        self.assertEqual(labels._translate_section("4.3"), ("34070-3", "4.3", None))
        self.assertIn("no SmPC counterpart", labels._translate_section("34089-3")[2])
        self.assertIn("no US counterpart", labels._translate_section("5.1")[2])
        with self.assertRaises(labels.LabelError):
            labels._translate_section("warnings")

    def test_a_clone_without_the_build_marker_opens(self):
        # CI and the public submodule layout have plain clones: no .git/asof-build-eu, corpus.json decides
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "eu"
            subprocess.run(["git", "clone", "-q", str(fixture()["repo"]), str(clone)], check=True)
            self.assertFalse((clone / ".git" / labels.EU_BUILD_MARKER).exists())
            self.assertTrue(labels.is_eu_repo(clone))
            self.assertEqual(labels.open_eu(clone, fixture()["mols"]).head, fixture()["eu"].head)
            us_clone = Path(tmp) / "us"
            subprocess.run(["git", "clone", "-q", str(fixture()["us_repo"]), str(us_clone)], check=True)
            self.assertFalse(labels.is_eu_repo(us_clone))
            saved = (labels.SUBMODULE_EU_REPO, labels.SUBMODULE_US_REPO)
            env = os.environ.pop(labels.EU_REPO_ENV, None), os.environ.pop(labels.US_REPO_ENV, None)
            try:
                labels.SUBMODULE_EU_REPO, labels.SUBMODULE_US_REPO = clone, us_clone
                self.assertEqual(labels.resolve_eu_repo(None), clone)          # eu/ before build/eu
                self.assertEqual(labels.default_us_repo(), us_clone)           # us/ before build/us
                self.assertEqual(labels.resolve_us_repo(), us_clone)
                self.assertEqual(labels.resolve_us_repo("/x/us"), Path("/x/us"))
                os.environ[labels.US_REPO_ENV] = "/env/us"
                self.assertEqual(labels.resolve_us_repo(), Path("/env/us"))
                labels.SUBMODULE_US_REPO = Path(tmp) / "absent"
                del os.environ[labels.US_REPO_ENV]
                self.assertEqual(labels.default_us_repo(), labels.BUILD_US_REPO)
            finally:
                labels.SUBMODULE_EU_REPO, labels.SUBMODULE_US_REPO = saved
                os.environ.pop(labels.US_REPO_ENV, None)
                for key, value in zip((labels.EU_REPO_ENV, labels.US_REPO_ENV), env):
                    if value is not None:
                        os.environ[key] = value

    def test_build_date_comes_from_the_manifest_when_its_head_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            saved = labels.MANIFEST
            try:
                labels.MANIFEST = manifest
                manifest.write_text(json.dumps({"corpora": {"eu": {"head": fixture()["eu"].head, "build_date": "2030-01-02"}}}))
                ix = labels.EuIndex(fixture()["repo"], fixture()["mols"])
                self.assertEqual(ix.build_date, "2030-01-02")
                self.assertIn("2030-01-02", labels.coverage(fixture()["us"], ix)["corpora"][1]["build_date"])
                manifest.write_text(json.dumps({"corpora": {"eu": {"head": "0" * 40, "build_date": "2030-01-02"}}}))
                self.assertNotEqual(labels.EuIndex(fixture()["repo"], fixture()["mols"]).build_date, "2030-01-02")
            finally:
                labels.MANIFEST = saved

    def test_open_eu_and_repo_resolution(self):
        self.assertIsNone(labels.open_eu(None))
        self.assertIsNone(labels.open_eu(""))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(labels.open_eu(tmp))                       # no .git/asof-build-eu marker
            subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
            (Path(tmp) / ".git" / labels.EU_BUILD_MARKER).write_text("x\n")
            with self.assertRaises(labels.LabelError):                   # marked but holds no commits
                labels.open_eu(tmp)
        saved = os.environ.get(labels.EU_REPO_ENV)
        try:
            os.environ[labels.EU_REPO_ENV] = ""
            self.assertIsNone(labels.resolve_eu_repo(None))
            os.environ[labels.EU_REPO_ENV] = "/x/eu"
            self.assertEqual(labels.resolve_eu_repo(None), Path("/x/eu"))
            self.assertEqual(labels.resolve_eu_repo("/y"), Path("/y"))
            self.assertIsNone(labels.resolve_eu_repo(""))
            del os.environ[labels.EU_REPO_ENV]
            expect = next((r for r in (labels.SUBMODULE_EU_REPO, labels.BUILD_EU_REPO) if labels.is_eu_repo(r)), None)
            self.assertEqual(labels.resolve_eu_repo(None), expect)
        finally:
            if saved is None:
                os.environ.pop(labels.EU_REPO_ENV, None)
            else:
                os.environ[labels.EU_REPO_ENV] = saved


# ---------------------------------------------------------------- EU reader on the fixture repo

class EuReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f = fixture()
        cls.ix, cls.eu = f["us"], f["eu"]

    def read(self, label_id, day, section=None):
        return labels.read_label(self.ix, label_id, day, section, eu=self.eu)

    def test_index_shape(self):
        self.assertEqual(len(self.eu.versions), 7)
        self.assertEqual([v.commit for v in self.eu.versions], fixture()["hashes"])
        self.assertEqual(sorted(self.eu.by_setid), [ALPHA, BETA, GAMMA])
        self.assertEqual((self.eu.record_from, self.eu.record_to), ("2010-03-01", "2026-11-01"))
        self.assertEqual((self.eu.date_key, self.eu.jurisdiction, self.eu.BUILD_MARKER),
                         ("effective_date", "eu", "asof-build-eu"))
        alpha = self.eu.label_versions(ALPHA)
        self.assertEqual([(v.version, v.published, v.procedure_id) for v in alpha],
                         [(1, "2010-03-01", "100"), (2, "2019-06-01", "200"), (3, "2019-06-01", "201"),
                          (4, "2021-01-10", "300")])
        self.assertEqual((alpha[2].corrigendum_of, alpha[2].procedure_closed), ("200", "2019-07-31"))
        self.assertEqual(alpha[3].annex_identical_to, "100")
        self.assertEqual(alpha[0].eu_number, "EU/1/10/001")
        self.assertEqual(self.eu.label_versions(BETA)[0].history, "current-only")
        self.assertIn("Union Register", self.eu.notice())
        self.assertIn("Record ends 2026-11-01 (built ", self.eu.notice())

    def test_every_id_form_resolves(self):
        for form in ("EMEA-H-C-000001", "EMEA/H/C/000001", "emea-h-c-1", "eu:EMEA/H/C/1", "EU/1/10/001", "eu:EU/1/10/001"):
            with self.subTest(form=form):
                self.assertEqual(self.read(form, "2020-01-01", "4.3")["label_id"], ALPHA)
        with self.assertRaises(labels.LabelError) as ctx:
            self.read("EMEA/H/C/000009", "2020-01-01")
        self.assertIn("not built", str(ctx.exception))
        self.assertIn("alternate of EMEA-H-C-000001", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            self.read("EMEA/H/C/777777", "2020-01-01")
        self.assertIn("find_label", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            self.read("EU/1/99/999", "2020-01-01")
        self.assertIn("no EU label matches", str(ctx.exception))

    def test_as_of_rule_and_same_date_corrigendum(self):
        out = self.read(ALPHA, "2019-06-01", "4.3")
        self.assertEqual((out["version"], out["procedure_id"], out["corrigendum_of"]), (3, "201", "200"))
        self.assertIn("within the last month", out["text"])
        self.assertEqual(out["corrigendum_note"],
                         "the register published this corrected annex on 2019-07-31; until then it showed the annex "
                         "of procedure 200 (version 2 of this product)")
        self.assertTrue(out["wording"].startswith(WORDING_START + " (2019) 201 of 2019-06-01 (version 3 of Alpha, "
                                                  "EMEA/H/C/000001), in force from 2019-06-01 until 2021-01-10"))
        self.assertIn("; this is the corrected annex the register published on 2019-07-31 (corrigendum of procedure "
                      "200)", out["wording"])
        self.assertEqual(out["published_date"], "2019-07-31")
        self.assertEqual(out["effective_window"], {"from": "2019-06-01", "to": "2021-01-10"})
        later = self.read(ALPHA, "2019-07-31", "4.3")
        self.assertEqual(later["version"], 3)
        self.assertNotIn("corrigendum_note", later)
        self.assertEqual(self.read(ALPHA, "2019-05-31", "4.3")["version"], 1)
        for key in ("jurisdiction", "label_id", "title", "ema_product_number", "eu_number", "molecule", "version",
                    "effective_date", "decision_number", "decision_date", "procedure_id", "ema_procedure",
                    "procedure_type", "published_date", "history_type", "record_from", "record_to",
                    "product_record_from", "product_record_to", "effective_window", "source_url", "commit", "wording",
                    "text_changed"):
            self.assertIn(key, out)
        self.assertEqual((out["jurisdiction"], out["molecule"], out["history_type"], out["record_from"],
                          out["product_record_from"]), ("eu", "alphamab", "in-force", "2010-03-01", "2010-03-01"))

    def test_superseded_same_day_and_identical_annex(self):
        v2 = self.eu.label_versions(ALPHA)[1]
        head = fixture()["eu"].header(v2, v2.published)
        self.assertTrue(head["superseded_same_day"])
        self.assertIn("version 2 was replaced the same day by version 3; no date returns it", head["wording"])
        self.assertTrue(head["text_changed"])
        out = self.read(ALPHA, "2022-01-01", "4.3")
        self.assertEqual((out["version"], out["annex_identical_to"]), (4, "100"))
        self.assertIn("its annex is byte-identical to that of procedure 100", out["wording"])
        self.assertIn("until the record end on 2026-11-01", out["wording"])
        self.assertNotIn("Acute myocardial", out["text"])

    def test_not_identified_corrigendum_and_current_only(self):
        out = self.read(GAMMA, "2020-06-01", "4.4")
        self.assertEqual(out["corrigendum_of"], "not identified")
        self.assertEqual(out["corrigendum_note"], "the register published this corrected annex on 2020-09-01; until "
                                                  "then it showed the previous annex, version 1")
        beta = self.read("EU/1/15/002", "2026-11-01", "4.1")
        self.assertEqual(beta["wording"], "EMA product information for Beta (EMEA/H/C/000002) captured on 2026-11-01 "
                                          "carries this text; no dated history before the capture")
        self.assertEqual((beta["history_type"], beta["captured_date"], beta["decision_number"]),
                         ("current-only", "2026-11-01", None))
        with self.assertRaises(labels.LabelError) as ctx:
            self.read(BETA, "2026-10-31")
        self.assertIn("EMA product information captured on 2026-11-01", str(ctx.exception))

    def test_dates_outside_the_record(self):
        with self.assertRaises(labels.LabelError) as ctx:
            self.read(ALPHA, "2010-02-28", "4.3")
        self.assertEqual(str(ctx.exception), "no EU text for Alpha (EMEA-H-C-000001) on 2010-02-28; the record for this "
                                             "product starts 2010-03-01 with Commission decision (2010) 100")
        after = self.read(ALPHA, "2027-01-01", "4.3")
        self.assertEqual(after["version"], 4)
        self.assertEqual(after["record_note"], "as_of 2027-01-01 is after the record end (2026-11-01, the newest "
                                               "decision in the repo; the Union Register was read on "
                                               f"{self.eu.build_date}); later decisions may exist")
        self.assertIsNone(after["effective_window"]["to"])
        with self.assertRaises(labels.LabelError) as ctx:
            labels.changes_between(self.ix, "2000-01-01", "2001-01-01", "eu", eu=self.eu)
        self.assertIn("2010-03-01", str(ctx.exception))
        with self.assertRaises(labels.LabelError):
            labels.search(self.ix, "hypersensitivity", "2009-01-01", "eu", eu=self.eu)

    def test_section_errors(self):
        with self.assertRaises(labels.LabelError) as ctx:
            self.read(ALPHA, "2020-01-01", "34070-3")
        self.assertIn("US LOINC code", str(ctx.exception))
        self.assertIn("4.3 (smpc-4.3-contraindications)", str(ctx.exception))
        with self.assertRaises(labels.LabelError):
            self.read(ALPHA, "2020-01-01", "4")
        whole = self.read(ALPHA, "2020-01-01")
        self.assertEqual([s["code"] for s in whole["sections"]], ["4.1", "4.2", "4.3", "4.4", "4.6", "4.8", "5.1"])
        self.assertEqual(whole["sections"][2]["title"], "Contraindications")
        self.assertIn("=== smpc-4.3-contraindications.md", whole["text"])
        if HAVE_US:
            with self.assertRaises(labels.LabelError) as ctx:
                labels.read_label(self.ix, SYN, "2020-01-01", "4.3", eu=self.eu)
            self.assertIn("available:", str(ctx.exception))

    def test_blame_credits_the_decision_that_wrote_each_line(self):
        out = labels.blame(self.ix, ALPHA, "2020-01-01", "4.3", eu=self.eu)
        credit = {l["text"]: (l["version"], l["procedure_id"], l["effective_date"]) for l in out["lines"]}
        self.assertEqual(credit["Hypersensitivity to the active substance or to any of the excipients."],
                         (1, "100", "2010-03-01"))
        self.assertEqual(credit[EU_SHARED], (2, "200", "2019-06-01"))
        self.assertEqual(credit["Acute myocardial infarction within the last month."], (3, "201", "2019-06-01"))
        self.assertTrue(out["wording"].startswith(WORDING_START))
        self.assertEqual([c["procedure_id"] for c in out["contributing_versions"]], ["100", "200", "201"])
        self.assertNotIn("published_date", out["lines"][0])
        words = labels.blame(self.ix, ALPHA, "2020-01-01", "4.3", words=True, eu=self.eu)
        line = next(l for l in words["lines"] if l["text"].startswith("Acute"))
        self.assertEqual({w["version"] for w in line["words"]}, {2, 3})   # only the corrected word is version 3
        self.assertEqual(next(w for w in line["words"] if w["text"] == "within")["version"], 3)

    def test_diff_and_section_history(self):
        out = labels.diff(self.ix, ALPHA, "2015-01-01", "2020-01-01", "4.3", eu=self.eu)
        self.assertEqual([(r["version"], r["procedure_id"]) for r in out["versions_between"]], [(2, "200"), (3, "201")])
        self.assertEqual(out["jurisdiction"], "eu")
        self.assertEqual(out["sections"][0]["code"], "4.3")
        self.assertIn("{+Acute myocardial", out["sections"][0]["word_diff"])
        self.assertTrue(out["to"]["wording"].startswith(WORDING_START))
        hist = labels.section_history(self.ix, "EU/1/10/001", "contraindications", eu=self.eu)
        self.assertEqual([(c["version"], c["status"], c["procedure_id"]) for c in hist["changes"]],
                         [(1, "created", "100"), (2, "changed", "200"), (3, "changed", "201"), (4, "changed", "300")])
        self.assertEqual((hist["section"]["code"], hist["history_type"], hist["jurisdiction"]), ("4.3", "in-force", "eu"))
        self.assertIn("effective_date", hist["changes"][0])
        h46 = labels.section_history(self.ix, ALPHA, "4.6", eu=self.eu)
        self.assertEqual([c["status"] for c in h46["changes"]], ["created", "deleted"])

    def test_changes_search_first_appearance(self):
        ch = labels.changes_between(self.ix, "2019-01-01", "2021-12-31", "eu", "4.3", eu=self.eu)
        self.assertEqual((ch["jurisdiction"], ch["eu_available"]), ("eu", True))
        self.assertEqual([(r["label_id"], r["version"], r["effective_date"]) for r in ch["changes"]],
                         [(ALPHA, 2, "2019-06-01"), (ALPHA, 3, "2019-06-01"), (ALPHA, 4, "2021-01-10")])
        self.assertEqual(ch["changes"][0]["sections"][0]["code"], "4.3")
        self.assertEqual(ch["changes"][0]["decision_number"], "(2019) 200")
        self.assertIn("Union Register", ch["notice"])
        q = labels.changes_between(self.ix, "2019-01-01", "2021-12-31", "eu", query="myocardial", eu=self.eu)
        self.assertEqual([(c["version"], c["change"]) for c in q["changes"]], [(2, "added"), (3, "reworded"),
                                                                              (4, "removed")])
        removed = next(c for c in q["changes"] if c["change"] == "removed")
        self.assertTrue(any("myocardial" in l.lower() for l in removed["lines_before"]))
        self.assertEqual(removed["lines_after"], [])
        self.assertNotIn("format_conversion", q["note"])   # a US-only field
        self.assertEqual(q["scope"], f"Covers this record only: the English Annex I of {len(self.eu.by_setid)} EU "
                                     "reference products, one per molecule; not every product authorised in the EU.")
        both = labels.changes_between(self.ix, "2019-01-01", "2021-12-31", "all", "43685-7", eu=self.eu)
        self.assertEqual((both["section"], both["eu"]["section"]), ("43685-7", "4.4"))
        self.assertIn("US labels", both["scope"])
        self.assertEqual(both["eu"]["scope"], self.eu.scope())
        self.assertIn("Courtesy of the U.S. National Library of Medicine", both["notice"])
        self.assertIn("Union Register", both["eu"]["notice"])
        s = labels.search(self.ix, "hypersensitivity", "2020-01-01", "eu", eu=self.eu)
        self.assertEqual([(m["label_id"], m["version"]) for m in s["matches"]], [(ALPHA, 3), (GAMMA, 1)])
        self.assertTrue(all(m["wording"].startswith(WORDING_START) for m in s["matches"]))
        self.assertEqual(s["matches"][0]["sections"][0]["code"], "4.3")
        self.assertEqual(s["scope"], self.eu.scope())
        s_all = labels.search(self.ix, "hypersensitivity", "2020-01-01", "all", "4.3", eu=self.eu)
        self.assertEqual(s_all["eu"]["count"], 2)
        self.assertEqual((s_all["scope"], s_all["eu"]["scope"]), (self.ix.scope(), self.eu.scope()))
        self.assertEqual((s_all["section"], s_all["eu"]["section"]), ("34070-3", "4.3"))
        note = labels.search(self.ix, "hypersensitivity", "2020-01-01", "all", "5.1", eu=self.eu)
        self.assertIn("no US counterpart", note["section_note"])
        fa = labels.first_appearance(self.ix, "liver injury", jurisdiction="eu", eu=self.eu)
        self.assertEqual([(e["label_id"], e["first_version"], e["first_effective"], e["first_procedure_id"])
                          for e in fa["episodes"]], [(GAMMA, 2, "2020-05-05", "500")])
        fa1 = labels.first_appearance(self.ix, "myocardial", ALPHA, eu=self.eu)
        self.assertEqual([(e["first_version"], e["left_version"], e["left_effective"]) for e in fa1["episodes"]],
                         [(2, 4, "2021-01-10")])
        self.assertEqual(fa["scope"], self.eu.scope())
        self.assertNotIn("scope", fa1)
        fa_all = labels.first_appearance(self.ix, "liver injury", jurisdiction="all", eu=self.eu)
        self.assertEqual(fa_all["eu"]["count"], 1)
        self.assertEqual(fa_all["eu"]["scope"], self.eu.scope())
        with self.assertRaises(labels.LabelError) as ctx:
            labels.first_appearance(self.ix, "myocardial", ALPHA, jurisdiction="us", eu=self.eu)
        self.assertIn("is a EU label", str(ctx.exception))

    def test_check_citations_mixed(self):
        cites = [{"doc_id": "EMEA/H/C/1", "version": 3, "section": "4.3",
                  "quote": "acute myocardial infarction within the last month.", "as_of": "2019-06-01"},
                 {"doc_id": "EU/1/10/001", "version": 2, "section": "4.3",
                  "quote": "Acute myocardial infarction withn the last month.", "as_of": "2019-06-01"},
                 {"doc_id": ALPHA, "version": 3, "section": "34070-3", "quote": "x", "as_of": "2020-01-01"},
                 {"doc_id": ALPHA, "version": 1, "section": "4.3", "quote": "Hypersensitivity", "as_of": "2009-01-01"}]
        if HAVE_US:
            cites.append({"doc_id": SYN, "version": 1526, "section": "34070-3", "quote": US_SHARED, "as_of": "2020-01-01"})
        out = labels.check_citations(self.ix, cites, eu=self.eu)
        flags = [(r["exists"], r["quote_found"], r["in_force_on_as_of"]) for r in out["results"]]
        expect = [(True, True, True), (True, True, False), (True, False, True), (True, True, False)]
        self.assertEqual(flags, expect + ([(True, True, True)] if HAVE_US else []))
        self.assertEqual(out["results"][0]["jurisdiction"], "eu")
        self.assertTrue(out["results"][0]["cited"]["wording"].startswith(WORDING_START))
        self.assertTrue(out["results"][0]["in_force"]["corrigendum_note"].startswith("the register published this corrected"))
        self.assertEqual(out["results"][1]["in_force"]["version"], 3)
        self.assertIn("US LOINC code", out["results"][2]["problems"][0])
        self.assertIn("starts 2010-03-01", out["results"][3]["problems"][0])
        self.assertIn("EU: the Annex I of the Commission decision in force", out["criteria"])
        if HAVE_US:
            self.assertEqual(out["results"][4]["jurisdiction"], "us")

    def test_find_label(self):
        all_ = labels.find_label(self.ix, "alpha", eu=self.eu)
        self.assertEqual(all_["jurisdiction"], "all")
        eu_rows = [m for m in all_["matches"] if m["jurisdiction"] == "eu"]
        self.assertEqual([(m["id"], m["built"]) for m in eu_rows], [(ALPHA, True), ("EMEA-H-C-000009", False)])
        row = eu_rows[0]
        for key in ("id", "jurisdiction", "title", "holder", "ema_product_number", "eu_number", "inn", "molecule_name",
                    "history_type", "record_from", "record_to", "versions", "comparable_to_us", "comparability_note",
                    "built", "matched_on"):
            self.assertIn(key, row)
        self.assertEqual((row["versions"], row["comparable_to_us"], row["inn"]), (4, "yes", ["alphamab"]))
        self.assertEqual(eu_rows[1]["note"], "not built; alternate of EMEA-H-C-000001")
        self.assertTrue(all_["eu_available"])
        us_rows = [m for m in all_["matches"] if m["jurisdiction"] == "us"]
        self.assertEqual(all_["matches"][:len(us_rows)], us_rows)          # US first
        for query, expect in (("EU/1/12/003", GAMMA), ("emea/h/c/3", GAMMA), ("gammavir", GAMMA),
                              ("Beta Holder", BETA), ("BETA", BETA)):
            with self.subTest(query=query):
                ids = [m["id"] for m in labels.find_label(self.ix, query, "eu", eu=self.eu)["matches"]]
                self.assertEqual(ids, [expect])
        none = labels.find_label(self.ix, "zzzz-nothing", "eu", eu=self.eu)
        self.assertEqual(none["count"], 0)
        self.assertIn("EU number", none["hint"])

    def test_section_words_in_every_per_label_tool(self):
        for key, code in (("indications", "4.1"), ("dosage", "4.2"), ("posology", "4.2"), ("precautions", "4.4"),
                          ("contraindications", "4.3"), ("adverse", "4.8"), ("pregnancy", "4.6")):
            with self.subTest(key=key):
                self.assertEqual(self.read(ALPHA, "2020-01-01", key)["section"]["code"], code)
        self.assertEqual(labels.section_history(self.ix, ALPHA, "indications", eu=self.eu)["section"]["code"], "4.1")
        self.assertEqual(labels.blame(self.ix, ALPHA, "2020-01-01", "precautions", eu=self.eu)["section"]["code"], "4.4")
        cite = labels.check_citations(self.ix, [{"doc_id": ALPHA, "version": 3, "section": "indications",
                                                 "quote": "Thyroid cancer as adjunct therapy.", "as_of": "2020-01-01"}],
                                      eu=self.eu)
        self.assertEqual(cite["passed"], 1)
        for key in ("interactions", "overdosage"):                      # Alpha has no 4.5 or 4.9
            with self.subTest(key=key), self.assertRaises(labels.LabelError) as ctx:
                self.read(ALPHA, "2020-01-01", key)
            self.assertIn("is not in this version", str(ctx.exception))
        with self.assertRaises(labels.LabelError) as ctx:
            self.read(ALPHA, "2020-01-01", "43685-7")
        self.assertIn("US LOINC code", str(ctx.exception))

    def test_top_level_record_window_is_the_corpus_envelope(self):
        corpus = ("2010-03-01", "2026-11-01")
        gamma = self.read(GAMMA, "2020-06-01", "4.4")
        self.assertEqual((gamma["record_from"], gamma["record_to"]), corpus)
        self.assertEqual((gamma["product_record_from"], gamma["product_record_to"]), ("2012-01-01", "2026-11-01"))
        alpha = self.read(ALPHA, "2020-01-01", "4.3")
        self.assertEqual((alpha["record_from"], alpha["product_record_from"]), ("2010-03-01", "2010-03-01"))
        blame = labels.blame(self.ix, GAMMA, "2020-06-01", "4.4", eu=self.eu)
        diff = labels.diff(self.ix, GAMMA, "2013-01-01", "2021-01-01", eu=self.eu)
        hist = labels.section_history(self.ix, GAMMA, "4.4", eu=self.eu)
        search = labels.search(self.ix, "gammavir", "2020-06-01", "eu", eu=self.eu)
        changes = labels.changes_between(self.ix, "2020-01-01", "2020-12-31", "eu", eu=self.eu)
        for answer in (blame, diff, hist, search, changes):
            self.assertEqual((answer["record_from"], answer["record_to"]), corpus)
        self.assertEqual(blame["product_record_from"], "2012-01-01")
        self.assertEqual(hist["product_record_from"], "2012-01-01")
        for side in ("from", "to"):
            self.assertEqual(diff[side]["product_record_from"], gamma["product_record_from"])
            self.assertNotIn("record_from", diff[side])
        self.assertEqual({m["product_record_from"] for m in search["matches"]}, {"2012-01-01"})
        self.assertTrue(all("record_from" not in m for m in search["matches"]))
        self.assertEqual({c["product_record_from"] for c in changes["changes"]}, {"2012-01-01"})
        cite = labels.check_citations(self.ix, [{"doc_id": GAMMA, "version": 2, "section": "4.4",
                                                 "quote": "Liver injury has been reported.", "as_of": "2020-06-01"}],
                                      eu=self.eu)["results"][0]
        self.assertEqual((cite["cited"]["product_record_from"], cite["in_force"]["product_record_from"]),
                         ("2012-01-01", "2012-01-01"))
        self.assertTrue(labels.eu_header_line(gamma).endswith(
            "Product record 2012-01-01 to 2026-11-01. Note: the register published this corrected annex on "
            "2020-09-01; until then it showed the previous annex, version 1."))
        row = labels.find_label(self.ix, "gammavir", "eu", eu=self.eu)["matches"][0]   # rows keep record_from
        self.assertEqual((row["record_from"], row["record_to"]), ("2012-01-01", "2026-11-01"))

    def test_find_label_without_an_eu_reference(self):
        rows = labels.find_label(self.ix, "Deltanib Teva", "eu", eu=self.eu)["matches"]
        self.assertEqual([(r["id"], r["built"]) for r in rows], [("EMEA-H-C-000041", False)])
        self.assertTrue(rows[0]["note"].startswith("not built; no EU reference for deltanib: no central product with "
                                                   "this exact INN"))
        self.assertNotIn("no built reference", rows[0]["note"])
        near = labels.find_label(self.ix, "deltasalt", "eu", eu=self.eu)
        self.assertEqual(near["count"], 1)
        row = near["matches"][0]
        self.assertEqual({k: row[k] for k in ("id", "jurisdiction", "title", "ema_product_number", "inn",
                                              "history_type", "built", "kind", "matched_on", "holder", "eu_number",
                                              "record_from", "versions")},
                         {"id": "EMEA-H-C-000040", "jurisdiction": "eu", "title": "Deltasalt",
                          "ema_product_number": "EMEA/H/C/000040", "inn": ["deltanib"], "history_type": "not-built",
                          "built": False, "kind": "salt", "matched_on": ["near_miss"], "holder": None,
                          "eu_number": None, "record_from": None, "versions": None})
        self.assertEqual(row["note"], "central product not matched by the exact-INN rule for deltanib (salt near "
                                      "miss): no central product with this exact INN in the EMA medicines JSON; not "
                                      "matched by the v1 rule (exact INN): Deltasalt EMEA/H/C/000040 "
                                      "('deltanib sulfate', salt)")
        self.assertEqual(labels.find_label(self.ix, "de", "eu", eu=self.eu)["count"], 0)   # under 3 characters

    def test_coverage_history_types(self):
        cov = labels.coverage(self.ix, self.eu)
        self.assertTrue(cov["eu_available"])
        us, e = cov["corpora"]
        self.assertEqual(us["history_type"], "in-force")
        self.assertEqual(e["history_types"], {"in-force": 2, "current-only": 1, "replayed": 0})
        self.assertEqual(e["products_by_history_type"], {"in-force": [ALPHA, GAMMA], "current-only": [BETA],
                                                         "replayed": []})
        self.assertEqual((e["products"], e["molecules"], e["versions"], e["corrigenda"]), (3, 3, 7, 2))
        self.assertEqual((e["record_from"], e["record_to"], e["head_commit"]), ("2010-03-01", "2026-11-01",
                                                                               fixture()["hashes"][-1]))
        self.assertNotIn("history_types_problem", e)
        self.assertIn("Effective-Date", e["date_rule"])
        self.assertEqual([p["label_id"] for p in e["products_list"]], [ALPHA, BETA, GAMMA])

    def test_molecules_agree_with_product_json(self):
        for setid, meta in self.eu.all_metadata().items():
            inn = meta["molecule"]
            ref = json.loads((fixture()["mols"] / f"{inn}.json").read_text())["eu"][0]
            with self.subTest(product=setid):
                self.assertEqual(ref["label_id"], setid)
                st = self.eu.status(setid)
                self.assertEqual((ref["history_type"], ref["record_from"], ref["versions"], ref["comparable_to_us"]),
                                 (st["history_type"], st["record_from"], len(self.eu.label_versions(setid)),
                                  st["comparable_to_us"]))
                self.assertEqual(ref["mah"], meta["mah"])        # merged from the crosswalk, not product.json
                self.assertEqual(meta["history_type"], {v.history for v in self.eu.label_versions(setid)}.pop())

    def test_no_eu_wording_says_approved(self):
        answers = [self.read(ALPHA, "2019-06-01"), self.read(BETA, "2026-11-01"), self.read(GAMMA, "2027-01-01", "4.4"),
                   labels.blame(self.ix, ALPHA, "2020-01-01", "4.3", eu=self.eu),
                   labels.coverage(self.ix, self.eu)["corpora"][1],
                   labels.search(self.ix, "hypersensitivity", "2020-01-01", "eu", eu=self.eu)]
        for a in answers:
            words = eu_words(a)
            for banned in ("approved", "fda", "authorised on"):
                self.assertNotIn(banned, words)


# ---------------------------------------------------------------- compare_jurisdictions (needs the US fixture)

@skip_without_us
class CompareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f = fixture()
        cls.ix, cls.eu = f["us"], f["eu"]

    def compare(self, *args, **kw):
        return labels.compare_jurisdictions(self.ix, *args, eu=self.eu, **kw)

    def test_pairs_without_section(self):
        out = self.compare("alphamab", "2020-01-01")
        self.assertEqual(out["comparability"], {"comparable_to_us": "yes",
                                                "note": "EU name matches the US NDA/BLA brand ALPHA"})
        label = out["us"]["label"]
        self.assertEqual((label["label_id"], label["version"], label["chosen_by"]),
                         (SYN, 1526, "brand named in comparability_note"))
        self.assertNotIn("pairing_caveat", out["comparability"])
        self.assertEqual([o["id"] for o in out["us"]["other_labels"]], [IPRA])   # ANDA and combination left out
        self.assertEqual(out["eu"]["product"]["version"], 3)
        self.assertEqual(out["eu"]["product"]["history_type"], "in-force")
        self.assertIn("Union Register", out["eu"]["notice"])
        self.assertEqual([p["pair"] for p in out["pairs"]], [p.name for p in labels.PAIRS])
        contra = next(p for p in out["pairs"] if p["pair"] == "contraindications")
        self.assertEqual((contra["us"]["code"], contra["eu"]["code"], contra["shared_lines"]), ("34070-3", "4.3", 1))
        self.assertEqual((contra["only_in_us_lines"], contra["only_in_eu_lines"]), (0, 2))
        both = [p["pair"] for p in out["pairs"] if p["us"] and p["eu"]]
        self.assertEqual(both, ["indications", "dosage", "contraindications", "warnings", "pregnancy-and-lactation",
                                "adverse-reactions"])
        self.assertIn("34066-1", out["us_only_sections"])
        self.assertEqual(out["section_notes"], {"34066-1": "boxed warning; no SmPC counterpart"})
        self.assertEqual(out["eu_only_sections"], ["5.1"])
        self.assertNotIn("text", contra["us"])

    def test_section_lines_round_trip_through_check_citations(self):
        for section in ("contraindications", "4.4", "34084-4"):
            with self.subTest(section=section):
                out = self.compare("Alpha", "2020-01-01", section)
                self.assertEqual(len(out["pairs"]), 1)
                pair = out["pairs"][0]
                self.assertIn("text", pair["us"])
                self.assertIn("text", pair["eu"])
                items = []
                for side, key in (("us", "only_in_us"), ("eu", "only_in_eu")):
                    for line in pair[key]["lines"]:
                        self.assertIn(line, pair[side]["text"].splitlines())    # verbatim, never invented
                        items.append({**pair[side]["citation"], "quote": line})
                self.assertTrue(items)
                res = labels.check_citations(self.ix, items, eu=self.eu)
                self.assertEqual(res["passed"], len(items))
        contra = self.compare("alphamab", "2020-01-01", "contraindications")["pairs"][0]
        self.assertEqual(contra["only_in_eu"]["lines"], ["Hypersensitivity to the active substance or to any of the "
                                                         "excipients.", "Acute myocardial infarction within the last month."])
        self.assertEqual(contra["only_in_us"], {"count": 0, "lines": [], "truncated": False})
        self.assertEqual(contra["eu"]["citation"], {"doc_id": ALPHA, "version": 3, "section": "4.3", "as_of": "2020-01-01"})
        self.assertEqual(contra["us"]["citation"], {"doc_id": SYN, "version": 1526, "section": "34070-3",
                                                    "as_of": "2020-01-01"})

    def test_us_label_override_and_no_counterpart(self):
        out = self.compare("alphamab", "2020-01-01", "warnings", us_label_id=IPRA[:8])
        self.assertEqual((out["us"]["label"]["label_id"], out["us"]["label"]["chosen_by"]), (IPRA, "us_label_id"))
        with self.assertRaises(labels.LabelError):
            self.compare("alphamab", "2020-01-01", us_label_id=PLAQUENIL)
        eu_only = self.compare("alphamab", "2020-01-01", "5.1")
        self.assertEqual((eu_only["pairs"][0]["pair"], eu_only["pairs"][0]["us"]), (None, None))
        self.assertIn("synthetic thyroid hormone", eu_only["pairs"][0]["eu"]["text"])
        self.assertIn("no counterpart in the pairing table", eu_only["note"])
        us_only = self.compare("alphamab", "2020-01-01", "34089-3")
        self.assertEqual((us_only["pairs"][0]["pair"], us_only["pairs"][0]["eu"]), (None, None))
        self.assertEqual(us_only["pairs"][0]["us"]["citation"]["section"], "34089-3")

    def test_no_reference_not_yet_and_caveats(self):
        out = self.compare("deltanib", "2025-01-01")
        self.assertIsNone(out["eu"]["product"])
        self.assertIn("not matched by the v1 rule", out["eu"]["reason"])
        self.assertEqual(out["pairs"], [])
        self.assertEqual(out["us"]["label"]["label_id"], NEWHCQ)
        self.assertIn("comparable_to_us", out["comparability"])
        early = self.compare("alphamab", "2009-06-01")
        self.assertEqual({k: early["eu"]["product"][k] for k in ("label_id", "name", "not_yet", "product_record_from")},
                         {"label_id": ALPHA, "name": "Alpha", "not_yet": True, "product_record_from": "2010-03-01"})
        self.assertNotIn("record_from", early["eu"]["product"])
        self.assertEqual(early["pairs"], [])
        self.assertEqual(early["us"]["label"]["label_id"], SYN)
        gamma = self.compare("Gamma", "2024-06-01")                     # an EU product name resolves the molecule
        self.assertEqual(gamma["molecule"], "gammavir")
        self.assertEqual(gamma["comparability"]["comparable_to_us"], "no")
        self.assertIn("another formulation", gamma["note"])
        self.assertEqual(gamma["us"]["label"]["label_id"], PLAQUENIL)    # the authorized generic is not a candidate
        self.assertEqual(gamma["us"]["label"]["chosen_by"], "most versions (ties: earliest first version, then setid)")
        warn = self.compare("gammavir", "2024-06-01", "warnings")["pairs"][0]
        self.assertNotIn("[Gamma 20 mg tablets]", warn["only_in_eu"]["lines"])   # multi-SmPC marker lines skipped
        beta = self.compare("betanib", "2026-11-01")
        self.assertEqual(beta["eu"]["product"]["history_type"], "current-only")
        self.assertTrue(beta["eu"]["product"]["wording"].startswith("EMA product information for Beta"))
        self.assertNotIn("approved", eu_words(beta))

    def test_name_with_previously_suffix_resolves(self):
        for query in ("Deltanib Teva", "deltanib actavis", "Deltanib Teva (previously Deltanib Actavis)"):
            with self.subTest(query=query):
                out = self.compare(query, "2025-01-01")
                self.assertEqual(out["molecule"], "deltanib")
                self.assertIsNone(out["eu"]["product"])
        self.assertEqual(self.compare("ALPHA", "2020-01-01")["molecule"], "alphamab")   # register_name

    def test_notes_only_describe_a_comparison_that_was_made(self):
        both = self.compare("alphamab", "2020-01-01", "contraindications")
        self.assertIn("only_in counts are distinct lines after compare_key normalisation; a line repeated within a "
                      "section is listed and counted once", both["note"])
        none = self.compare("deltanib", "2025-01-01")
        self.assertNotIn("compare_key", none["note"])
        early = self.compare("alphamab", "2009-06-01")
        self.assertNotIn("compare_key", early["note"])
        first = self.ix.label_versions(NEWHCQ)[0].published
        day = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
        no_us = self.compare("deltanib", day)
        self.assertIsNone(no_us["us"]["label"])
        self.assertIn(f"DailyMed showed no single-ingredient NDA/BLA label of deltanib on {day} in the US corpus (a retired "
                      "label counts until its last published version)", no_us["note"])
        self.assertNotIn("in force on", no_us["note"])
        self.assertNotIn("compare_key", no_us["note"])

    def test_line_cap(self):
        full = self.compare("alphamab", "2020-01-01", "contraindications")["pairs"][0]["only_in_eu"]
        self.assertEqual((full["count"], len(full["lines"]), full["truncated"]), (2, 2, False))
        one = self.compare("alphamab", "2020-01-01", "contraindications", line_cap=1)["pairs"][0]["only_in_eu"]
        self.assertEqual((one["count"], one["lines"], one["truncated"]), (2, full["lines"][:1], True))
        every = self.compare("alphamab", "2020-01-01", "contraindications", line_cap=None)["pairs"][0]["only_in_eu"]
        self.assertEqual(every, full)

    def test_compare_check_on_the_fixture(self):
        report = cli.compare_check(self.ix, self.eu, ["2020-01-01", "2009-06-01"])
        rows = {r["molecule"]: r for r in report["molecules"]}
        self.assertEqual(sorted(rows), ["alphamab", "betanib", "gammavir"])
        alpha = rows["alphamab"]["dates"]["2020-01-01"]
        self.assertTrue(alpha["works"], alpha["reasons"])
        self.assertGreater(alpha["citation_lines_checked"], 0)
        self.assertEqual(alpha["citation_failures"], [])
        self.assertFalse(rows["alphamab"]["dates"]["2009-06-01"]["works"])
        self.assertEqual(rows["alphamab"]["dates"]["2009-06-01"]["reasons"], ["EU record starts 2010-03-01"])
        s = report["summary"]["2020-01-01"]
        self.assertEqual(s["citation_failures"], 0)
        self.assertEqual(s["by_comparable_to_us"]["yes"], {"molecules": 1, "works": 1})
        md = cli.compare_check_markdown(report)
        self.assertIn("One entry per molecule (3) is in `compare-check.json` under `molecules`", md)
        self.assertNotIn("| alphamab |", md)          # per-molecule rows live in the JSON only
        self.assertEqual((report["only_in_lines_listed_per_side_in_tool_answers"], report["lines_round_tripped"]),
                         (200, "all"))
        self.assertNotIn("only_in_lines_listed_per_side", report)
        self.assertIn("every line of only_in_us and only_in_eu", report["criteria"])
        self.assertNotIn("every listed line", report["criteria"])
        self.assertIn("a line repeated within a section is listed and counted once", report["criteria"])
        self.assertEqual((s["works_with_pairing_caveat"], s["pairing_caveats"]), (0, {}))
        self.assertIsNone(alpha["pairing_caveat"])
        self.assertIn("## Pairing caveats on 2020-01-01", cli.compare_check_markdown(report))
        self.assertEqual(cli._reason_group("PURIXAN 9fd27952: 3 of 8 pairs have text on both sides"),
                         "fewer than 6 of 8 pairs have text on both sides")
        self.assertTrue(cli._reason_group("DailyMed showed no single-ingredient NDA/BLA label of x on 2026-11-01 in "
                                          "the US corpus; X 1 retired, last published 2025-01-01").startswith("DailyMed"))

    def test_compare_check_caveat_rows(self):
        retired = ("the US label named in the comparability note (CUBICIN, setid 34c1faa3-a3d8-4fe6-8ca1-7016c17339d2) is "
                   "retired; DailyMed last showed it on 2023-04-24 (its last published version), so it is not a "
                   "candidate on 2026-10-30; compared Daptomycin in Sodium Chloride (setid "
                   "7ed4d986-2aa9-43cd-9a8a-fbd11cb5ecef) instead, a different US product")
        self.assertEqual(cli._caveat_row("daptomycin", True, retired),
                         ["daptomycin (works)", "CUBICIN `34c1faa3`", "retired; last published 2023-04-24",
                          "Daptomycin in Sodium Chloride `7ed4d986`"])
        not_yet = ("the US label named in the comparability note (BAQSIMI, setid f1f5df9b-872f-44e5-a18f-f0b68e7e9254) is "
                   "not yet published on 2015-01-01 (record starts 2024-02-02); compared GLUCAGEN (setid "
                   "f09feb8e-6651-4708-a811-bb5264712059) instead, a different US product")
        self.assertEqual(cli._caveat_row("glucagon", False, not_yet),
                         ["glucagon (does not work)", "BAQSIMI `f1f5df9b`", "not yet published (first version 2024-02-02)",
                          "GLUCAGEN `f09feb8e`"])
        no_setid = ("the US label named in the comparability note (X) is not in molecules us[]; compared Y (setid "
                    "abcdef12-0000) instead, a different US product")
        self.assertEqual(cli._caveat_row("x", True, no_setid), ["x (works)", "X", "is not in molecules us[]", "Y `abcdef12`"])
        self.assertEqual(cli._caveat_row("x", True, "something else"), ["x (works)", "something else", "", ""])


# ---------------------------------------------------------------- server and CLI

class RemovedEuProductTest(unittest.TestCase):
    """A reference product listed in sources/removed.json and absent from the EU repo: answers say 'removed'."""
    GONE = "EMEA-H-C-000099"

    @classmethod
    def setUpClass(cls):
        f = fixture()
        cls.tmp = tempfile.TemporaryDirectory()
        mols = Path(cls.tmp.name) / "molecules"
        mols.mkdir()
        for path in f["mols"].glob("*.json"):
            mol = json.loads(path.read_text(encoding="utf-8"))
            if path.stem == "gammavir":
                ref_entry = next(e for e in mol["eu"] if e["role"] == "reference")
                ref_entry.update({"label_id": cls.GONE, "ema_product_number": "EMEA/H/C/000099", "built": False,
                                  "removed": True})
                mol["eu_selection"]["reference_label_id"] = cls.GONE
            (mols / path.name).write_text(eu.molecule_text(mol), encoding="utf-8")
        cls.ix = labels.Index(f["us_repo"], mols)
        cls.eu = labels.EuIndex(f["repo"], mols)
        assert cls.GONE not in cls.eu.by_setid

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_compare_reports_removal(self):
        out = labels.compare_jurisdictions(self.ix, "gammavir", "2020-01-01", eu=self.eu)
        self.assertFalse(out["eu_available"])
        self.assertIsNone(out["eu"]["product"])
        self.assertIn("removed from the corpus on request", out["eu"]["reason"])
        self.assertIn("sources/removed.json", out["eu"]["reason"])
        self.assertNotIn("not built", out["eu"]["reason"])
        self.assertEqual(out["pairs"], [])
        self.assertEqual(out["comparability"]["comparable_to_us"], "no")
        if HAVE_US:  # the US side needs the pinned raw zips, which a fresh clone lacks
            self.assertIsNotNone(out["us"]["label"])
        kept = labels.compare_jurisdictions(self.ix, "alphamab", "2020-01-01", eu=self.eu)
        self.assertTrue(kept["eu_available"])

    def test_read_label_and_show_say_removed(self):
        for label_id in (self.GONE, "EMEA/H/C/000099"):
            with self.subTest(label_id=label_id):
                with self.assertRaises(labels.LabelError) as cm:
                    labels.read_label(self.ix, label_id, "2020-01-01", eu=self.eu)
                self.assertIn("removed from the corpus on request", str(cm.exception))
                self.assertNotIn("no EU label matches", str(cm.exception))

    def test_find_label_marks_removed(self):
        out = labels.find_label(self.ix, self.GONE, "eu", eu=self.eu)
        rows = [r for r in out.get("matches") or out.get("labels") or out.get("results") or [] if r["id"] == self.GONE]
        self.assertEqual(len(rows), 1, out)
        self.assertEqual(rows[0]["history_type"], "removed")
        self.assertIn("removed from the corpus on request", rows[0]["note"])


class ServerEuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f = fixture()
        cls.server = mcp_server.Server(f["us_repo"], f["mols"], f["repo"])
        cls.server._index, cls.server._eu, cls.server._eu_loaded = f["us"], f["eu"], True

    def call(self, method, params=None):
        return self.server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_tools_list_and_schemas(self):
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], TOOL_NAMES)
        by = {t["name"]: t for t in tools}
        self.assertIn("us_label_id", by["compare_jurisdictions"]["inputSchema"]["properties"])
        self.assertEqual(by["first_appearance"]["inputSchema"]["properties"]["jurisdiction"]["enum"], ["us", "eu", "all"])
        self.assertIn("all (default", by["find_label"]["inputSchema"]["properties"]["jurisdiction"]["description"])
        self.assertIn("EMEA-H-C-nnnnnn", by["read_label"]["inputSchema"]["properties"]["label_id"]["description"])
        init = self.call("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(init["serverInfo"]["version"], "0.6.0")
        self.assertIn("provided 'as is'", init["instructions"])
        self.assertIn("Independent project by Particula Tech", init["instructions"])
        self.assertIn("provided as is", by["read_label"]["description"])
        self.assertIn("provided as is", by["compare_jurisdictions"]["description"])
        self.assertIn("Union Register", init["instructions"])
        self.assertIn("never as approved", init["instructions"])

    def test_eu_read_label_through_tools_call(self):
        res = self.call("tools/call", {"name": "read_label", "arguments": {"label_id": "EMEA/H/C/000001",
                                                                           "date": "2020-01-01", "section": "4.3"}})
        out = res["result"]["structuredContent"]
        self.assertNotIn("isError", res["result"])
        self.assertTrue(out["wording"].startswith(WORDING_START))
        self.assertIn("Union Register", out["notice"])
        res = self.call("tools/call", {"name": "first_appearance", "arguments": {"text": "liver injury",
                                                                                 "jurisdiction": "eu"}})
        self.assertEqual(res["result"]["structuredContent"]["count"], 1)
        res = self.call("tools/call", {"name": "coverage", "arguments": {}})
        self.assertEqual(res["result"]["structuredContent"]["corpora"][1]["history_types"]["in-force"], 2)

    def test_eu_resource(self):
        uri = "label://eu/EMEA-H-C-000001/2019-06-01/4.3"
        text = self.call("resources/read", {"uri": uri})["result"]["contents"][0]["text"]
        head = text.splitlines()[0]
        self.assertTrue(head.startswith(WORDING_START + " (2019) 201 of 2019-06-01"))
        self.assertIn("(label EMEA-H-C-000001, version 3, decision (2019) 201 of 2019-06-01). Product record 2010-03-01 to "
                      "2026-11-01.", head)
        self.assertIn("Note: the register published this corrected annex on 2019-07-31", head)
        self.assertIn("Acute myocardial infarction within the last month.", text)
        self.assertTrue(text.rstrip().endswith(notices.CONTACT + "."))
        self.assertIn("Not medical advice", text.rstrip().splitlines()[-1])
        self.assertIn("Union Register", text.rstrip().splitlines()[-1])
        same = labels.read_label(fixture()["us"], ALPHA, "2019-06-01", "4.3", eu=fixture()["eu"])["text"]
        self.assertIn(same, text)
        for bad in ("label://us/EMEA-H-C-000001/2020-01-01/4.3", f"label://eu/{STUB}/2020-01-01/34070-3",
                    "label://eu/EMEA-H-C-000001/2009-01-01/4.3", "label://eu/EMEA-H-C-000001/2020-01-01/34070-3"):
            with self.subTest(uri=bad):
                self.assertEqual(self.call("resources/read", {"uri": bad})["error"]["code"], -32002)

    def test_prompts_name_both_sources(self):
        res = self.call("prompts/get", {"name": "check-document-claims", "arguments": {"text": "x", "as_of": "2020-01-01"}})
        text = res["result"]["messages"][0]["content"]["text"]
        self.assertIn("DailyMed showed", text)
        self.assertIn("the Union Register's Annex I of Commission decision", text)
        res = self.call("prompts/get", {"name": "label-change-brief",
                                        "arguments": {"drug": "Alpha", "from": "2015-01-01", "to": "2020-01-01"}})
        self.assertIn("EMEA-H-C-nnnnnn", res["result"]["messages"][0]["content"]["text"])

    def test_descriptions_are_jurisdiction_neutral(self):
        prompts = {p["name"]: p for p in self.call("prompts/list")["result"]["prompts"]}
        self.assertEqual(prompts["check-document-claims"]["description"],
                         "Check every label claim in a pasted text against the label text in force on a date (US: what "
                         "DailyMed showed; EU: the Annex I of the Commission decision in force).")
        tools = {t["name"]: t for t in self.call("tools/list")["result"]["tools"]}
        self.assertTrue(tools["section_history"]["description"].startswith(
            "Every version that created, changed or deleted one section, with the version date (US: published date; "
            "EU: Commission decision date), lines added and removed, and, for US labels, the old-format-to-PLR "
            "conversion flag."))
        self.assertTrue(tools["changes_between"]["description"].startswith(
            "Every label version dated in [from, to] (US: Published-Date, what DailyMed showed; EU: the Commission "
            "decision date of the Annex I) with the sections it changed; "))
        for t in tools.values():
            self.assertNotIn("approved", t["description"].lower())

    def test_server_without_eu_repo_says_why(self):
        server = mcp_server.Server(fixture()["us_repo"], fixture()["mols"], None)
        server._index = fixture()["us"]
        res = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "read_label", "arguments": {"label_id": ALPHA, "date": "2020-01-01"}}})
        self.assertTrue(res["result"]["isError"])
        self.assertEqual(res["result"]["structuredContent"]["error"], labels.EU_REASON)
        res = server.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                             "params": {"uri": f"label://eu/{ALPHA}/2020-01-01/4.3"}})
        self.assertEqual(res["error"]["code"], -32002)

    def test_stdio_session_with_both_repos(self):
        f = fixture()
        us_id, us_section = (SYN, "43685-7") if HAVE_US else (STUB, "34070-3")
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_label", "arguments": {
                "label_id": us_id, "date": "2023-01-01", "section": us_section}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_label", "arguments": {
                "label_id": "EU/1/10/001", "date": "2023-01-01", "section": "4.3"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "prompts/get", "params": {"name": "label-change-brief", "arguments": {
                "drug": "Alpha", "from": "2015-01-01", "to": "2023-01-01"}}},
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
        ]
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([sys.executable, "-I", "-c", test_mcp.StdioEndToEndTest.BOOTSTRAP,
                               str(TOOLS / "mcp_server.py"), "--repo", str(f["us_repo"]), "--molecules", str(f["mols"]),
                               "--eu-repo", str(f["repo"])], input=stdin, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = {m["id"]: m for m in map(json.loads, proc.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5, 6])
        self.assertEqual([t["name"] for t in replies[2]["result"]["tools"]], TOOL_NAMES)
        self.assertTrue(replies[3]["result"]["structuredContent"]["wording"].startswith("DailyMed showed"))
        eu_out = replies[4]["result"]["structuredContent"]
        self.assertEqual((eu_out["label_id"], eu_out["version"]), (ALPHA, 4))
        self.assertTrue(eu_out["wording"].startswith(WORDING_START))
        self.assertIn("find_label", replies[5]["result"]["messages"][0]["content"]["text"])
        self.assertEqual(replies[6]["result"], {})
        self.assertNotIn("network access attempted", proc.stderr)
        self.assertIn("indexed 7 EU versions of 3 products", proc.stderr)


class CliEuTest(unittest.TestCase):
    def test_show_blame_diff(self):
        code, out, _ = run_cli("show", "2019-06-01", "EMEA/H/C/000001", "--section", "4.3")
        self.assertEqual(code, 0)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith(WORDING_START + " (2019) 201 of 2019-06-01"))
        self.assertIn("Note: the register published this corrected annex on 2019-07-31", first)
        self.assertIn("=== smpc-4.3-contraindications.md", out)
        self.assertIn("Union Register", out.rstrip().splitlines()[-1])
        code, out, _ = run_cli("blame", "2020-01-01", "EU/1/10/001", "--section", "contraindications")
        self.assertEqual(code, 0)
        self.assertIn("v3    2019-06-01 (2019) 201", out)
        self.assertIn("Commission decision that first wrote it", out)
        code, out, _ = run_cli("diff", "2015-01-01", "2020-01-01", ALPHA, "--section", "4.3")
        self.assertEqual(code, 0)
        self.assertIn("v2 ((2019) 200, 2019-06-01), v3 ((2019) 201, 2019-06-01)", out)
        self.assertIn("{+Acute", out)
        code, _, err = run_cli("show", "2009-01-01", ALPHA)
        self.assertEqual(code, 2)
        self.assertIn("record for this product starts 2010-03-01", err)

    def test_json_commands(self):
        code, out, _ = run_cli("coverage")
        self.assertEqual(code, 0)
        corpora = json.loads(out)["corpora"]
        self.assertEqual([c["jurisdiction"] for c in corpora], ["us", "eu"])
        self.assertEqual(corpora[1]["history_types"], {"in-force": 2, "current-only": 1, "replayed": 0})
        code, out, _ = run_cli("find", "gammavir")
        self.assertEqual(json.loads(out)["jurisdiction"], "all")
        self.assertIn(GAMMA, [m["id"] for m in json.loads(out)["matches"]])
        code, out, _ = run_cli("first", "liver injury", "--jurisdiction", "eu")
        self.assertEqual(json.loads(out)["count"], 1)
        code, out, _ = run_cli("search", "hypersensitivity", "2020-01-01", "--jurisdiction", "eu", "--section", "4.3")
        self.assertEqual(json.loads(out)["count"], 2)
        code, out, _ = run_cli("cite", json.dumps([{"doc_id": ALPHA, "version": 4, "section": "4.3",
                                                     "quote": "hypersensitivity", "as_of": "2022-01-01"}]))
        self.assertEqual(json.loads(out)["passed"], 1)
        if HAVE_US:
            code, out, _ = run_cli("compare", "alphamab", "2020-01-01", "--section", "4.3", "--us-label", IPRA)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["us"]["label"]["label_id"], IPRA)

    @skip_without_us
    def test_compare_check_writes_both_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cc.json"
            code, out, _ = run_cli("compare-check", "--date", "2020-01-01", "--info-date", "2009-06-01",
                                   "--out", str(target))
            self.assertEqual(code, 0)
            report = json.loads(target.read_text())
            self.assertEqual(report["dates"], ["2020-01-01", "2009-06-01"])
            self.assertTrue(target.with_suffix(".md").read_text().startswith("# compare_jurisdictions check"))
            self.assertIn("2020-01-01: ", out)


class EuAbsentTest(unittest.TestCase):
    """With no EU index every answer is the US-only answer apart from the EU_REASON text."""

    @classmethod
    def setUpClass(cls):
        cls.ix = fixture()["us"]

    def test_eu_reason_text(self):
        self.assertEqual(labels.EU_REASON, "EU repo not loaded; build it with tools/eu.py build or pass --eu-repo "
                                           "(env ASOF_EU_REPO)")

    def test_answers_carry_no_eu_fields(self):
        q = "synthroid" if HAVE_US else STUB[:8]
        out = labels.find_label(self.ix, q)
        self.assertEqual(set(out), {"notice", "record_from", "record_to", "build_date", "query", "matches", "count"})
        self.assertTrue(out["matches"])
        self.assertTrue(all("jurisdiction" not in m for m in out["matches"]))
        for jurisdiction in ("eu", "all"):
            out = labels.find_label(self.ix, q, jurisdiction)
            self.assertEqual((out["eu_available"], out["eu_reason"]), (False, labels.EU_REASON))
        self.assertEqual(labels.find_label(self.ix, "EU/1/15/1024")["matches"], [])
        cov = labels.coverage(self.ix)
        self.assertEqual(cov["corpora"][1], {"jurisdiction": "eu", "built": False, "reason": labels.EU_REASON})
        self.assertFalse(cov["eu_available"])
        day = "2024-08-01" if HAVE_US else "2009-06-01"
        ch = labels.changes_between(self.ix, day, day, "eu")
        self.assertEqual((ch["eu_available"], ch["changes"], ch["count"]), (False, [], 0))
        se = labels.search(self.ix, "contraindicated", day, "eu")
        self.assertEqual((se["eu_available"], se["matches"]), (False, []))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.read_label(self.ix, ALPHA, "2020-01-01")
        self.assertEqual(str(ctx.exception), labels.EU_REASON)
        cites = labels.check_citations(self.ix, [{"doc_id": ALPHA, "version": 1, "quote": "x", "as_of": "2020-01-01"}])
        self.assertEqual(cites["results"][0]["problems"], [labels.EU_REASON])
        self.assertNotIn("jurisdiction", cites["results"][0])

    @skip_without_us
    def test_compare_without_eu_is_m3(self):
        out = labels.compare_jurisdictions(self.ix, "alphamab", "2020-01-01", "34070-3")
        self.assertEqual(out["eu"], {"available": False, "reason": labels.EU_REASON, "labels": []})
        self.assertEqual(set(out), {"notice", "record_from", "record_to", "build_date", "molecule", "as_of", "section",
                                    "us", "eu", "eu_available", "eu_reason", "note"})
        listed = [r["label_id"] for r in out["us"]["labels"]] + [r["label_id"] for r in out["us"]["not_yet_published"]]
        self.assertEqual(sorted(listed), sorted([SYN, IPRA, HCQ_ANDA, COMBO]))   # every US label, any category
        self.assertTrue(all("jurisdiction" not in r for r in out["us"]["labels"]))


# ---------------------------------------------------------------- full corpus

FULL_EU = os.environ.get("ASOF_FULL_EU_REPO")
FULL_US = os.environ.get("ASOF_FULL_REPO")


@unittest.skipUnless(FULL_EU, "set ASOF_FULL_EU_REPO to build/eu to run the full EU corpus checks")
class FullEuCorpusTest(unittest.TestCase):
    READINGS = json.loads((HERE / "eu_known_readings.json").read_text(encoding="utf-8"))["readings"]

    @classmethod
    def setUpClass(cls):
        started = time.perf_counter()
        cls.eu = labels.EuIndex(Path(FULL_EU), MOLECULES)
        cls.eu.all_metadata()
        cls.load_seconds = time.perf_counter() - started
        if FULL_US:
            cls.ix = labels.Index(Path(FULL_US), MOLECULES)
        else:
            cls.ix = labels.Index(stub_us_repo(Path(tempfile.mkdtemp()) / "us"), MOLECULES)

    def test_load_time(self):
        self.assertLess(self.load_seconds, 2.0, f"EuIndex load took {self.load_seconds:.2f} s")

    def test_known_readings(self):
        for r in self.READINGS:
            with self.subTest(product=r["product"]):
                section = labels.eu_section_code(r["section"] + ".md")
                out = labels.read_label(self.ix, r["product"], r["reading_date"], section, eu=self.eu)
                self.assertIn(r["quote"], out["text"].splitlines())
                self.assertTrue(out["wording"].startswith(WORDING_START))
                blame = labels.blame(self.ix, r["product"], r["reading_date"], section, eu=self.eu)
                line = next(l for l in blame["lines"] if l["text"] == r["quote"])
                self.assertEqual((line["procedure_id"], line["decision_number"], line["effective_date"]),
                                 (r["first_decision"]["procedure_id"], r["first_decision"]["decision_number"],
                                  r["first_decision"]["decision_date"]))
                before = labels.read_label(self.ix, r["product"], _day_before(r["reading_date"]), section, eu=self.eu)
                self.assertNotIn(r["quote"], before["text"].splitlines())

    def test_corrigendum_notes(self):
        corrigenda = [v for v in self.eu.versions if v.corrigendum_of]
        self.assertEqual(len(corrigenda), 59)
        self.assertEqual(sum(v.corrigendum_of == "not identified" for v in corrigenda), 13)
        notes, not_identified = 0, 0
        for v in corrigenda:
            with self.subTest(product=v.setid, version=v.version):
                self.assertTrue(v.procedure_closed)
                self.assertLess(v.published, v.procedure_closed)
                out = labels.read_label(self.ix, v.setid, v.published, eu=self.eu)
                shown = next(x for x in self.eu.label_versions(v.setid) if x.version == out["version"])
                self.assertEqual(shown.published, v.published)
                head = self.eu.header(v, v.published)       # the corrigendum itself, queried on its decision date
                self.assertIn("corrigendum_note", head)
                self.assertIn(f"the corrected annex the register published on {v.procedure_closed}", head["wording"])
                notes += 1
                not_identified += "showed the previous annex" in head["corrigendum_note"]
                self.assertNotIn("corrigendum_note", self.eu.header(v, v.procedure_closed))
        self.assertEqual((notes, not_identified), (59, 13))

    def test_history_type_invariants(self):
        metas = self.eu.all_metadata()
        self.assertEqual(len(metas), 206)
        self.assertNotIn("history_types", self.eu.corpus)          # counts come from the commits, not corpus.json
        for setid, meta in metas.items():
            self.assertIn(meta["history_type"], labels.HISTORY_TYPES)
            self.assertEqual({v.history for v in self.eu.label_versions(setid)}, {meta["history_type"]}, setid)
        cov = labels.coverage(self.ix, self.eu)["corpora"][1]
        self.assertEqual(cov["history_types"], {"in-force": 206, "current-only": 0, "replayed": 0})
        self.assertEqual((cov["products"], cov["versions"], cov["corrigenda"]), (206, 4402, 59))
        self.assertNotIn("history_types_problem", cov)
        self.assertEqual(self.eu.head, self.eu.git("rev-parse", "main").strip())

    def test_crosswalk_agreement(self):
        metas = self.eu.all_metadata()
        refs = 0
        for path in sorted(MOLECULES.glob("*.json")):
            if path.name == "selection.json":
                continue
            mol = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("eu_selection", mol, path.name)
            for e in mol["eu"]:
                if not e["built"]:
                    continue
                refs += 1
                # the crosswalk's window and version count come from the commits; record_to is the register
                # read date of the sources, which the repo does not carry
                status = self.eu.status(e["label_id"])
                repo = {"history_type": status["history_type"], "record_from": status["record_from"],
                        "versions": len(self.eu.label_versions(e["label_id"])),
                        "comparable_to_us": metas[e["label_id"]]["comparable_to_us"]}
                for key, value in repo.items():
                    self.assertEqual(e[key], value, f"{path.name} {key}")
        self.assertEqual(refs, len(metas))

    def test_section_words_and_envelope_on_keytruda(self):
        for key, code in (("indications", "4.1"), ("dosage", "4.2"), ("precautions", "4.4"), ("interactions", "4.5"),
                          ("adverse", "4.8"), ("overdosage", "4.9")):
            with self.subTest(key=key):
                out = labels.read_label(self.ix, "EMEA-H-C-003820", "2024-06-01", key, eu=self.eu)
                self.assertEqual(out["section"]["code"], code)
        out = labels.read_label(self.ix, "EMEA-H-C-003820", "2024-06-01", "4.3", eu=self.eu)
        self.assertEqual((out["record_from"], out["record_to"]), ("1996-02-14", "2026-10-19"))
        self.assertEqual((out["product_record_from"], out["product_record_to"]), ("2015-07-17", "2026-10-19"))
        diff = labels.diff(self.ix, "EMEA-H-C-003820", "2016-01-01", "2024-06-01", eu=self.eu)
        self.assertEqual((diff["record_from"], diff["to"]["product_record_from"]), ("1996-02-14", "2015-07-17"))

    def test_find_label_names_the_salt_originator(self):
        # salt-alias-v2: Reyataz ('atazanavir sulfate') is the atazanavir reference; the generic is its alternate
        out = labels.find_label(self.ix, "Reyataz", "eu", eu=self.eu)
        self.assertEqual(out["count"], 1)
        self.assertEqual((out["matches"][0]["id"], out["matches"][0]["built"]), ("EMEA-H-C-000494", True))
        alt = labels.find_label(self.ix, "Atazanavir Viatris", "eu", eu=self.eu)["matches"][0]
        self.assertEqual((alt["built"], alt["note"]), (False, "not built; alternate of EMEA-H-C-000494"))
        mol = json.loads((MOLECULES / "atazanavir.json").read_text(encoding="utf-8"))["eu_selection"]
        self.assertEqual((mol["reference_label_id"], mol["reference_selected_by"]), ("EMEA-H-C-000494", "salt-alias-v2"))

    @unittest.skipUnless(FULL_US, "set ASOF_FULL_REPO to build/us for the compare checks")
    def test_previously_names_and_pairing_caveats(self):
        out = labels.compare_jurisdictions(self.ix, "Rolufta Ellipta", "2024-06-01", eu=self.eu)
        # REFERENCE_OVERRIDES: Incruse Ellipta is the umeclidinium reference, Rolufta Ellipta an alternate
        self.assertEqual((out["molecule"], out["eu"]["product"]["label_id"]), ("umeclidinium", "EMEA-H-C-002809"))
        out = labels.compare_jurisdictions(self.ix, "Atazanavir Viatris", "2024-06-01", eu=self.eu)
        self.assertEqual((out["molecule"], out["eu"]["product"]["label_id"]), ("atazanavir", "EMEA-H-C-000494"))
        caveats = {}
        for meta in self.eu.all_metadata().values():
            res = labels.compare_jurisdictions(self.ix, meta["molecule"], "2026-11-01", eu=self.eu)
            if res["comparability"].get("pairing_caveat"):
                caveats[meta["molecule"]] = res["comparability"]["pairing_caveat"]
            if meta["molecule"] == "paliperidone":
                self.assertTrue(res["us"]["label"]["label_id"].startswith("7b8e5b26"))
        self.assertEqual(sorted(caveats), ["daptomycin", "docetaxel", "duloxetine", "glycopyrronium", "mannitol",
                                           "palonosetron", "ranolazine", "sevelamer", "sirolimus", "travoprost",
                                           "zoledronic-acid"])
        self.assertTrue(caveats["duloxetine"].startswith(
            "the US label named in the comparability note (CYMBALTA, setid 2f7d4d67-10c1-4bf4-a7f2-c185fbad64ba) is "
            "retired"))
        self.assertTrue(caveats["sirolimus"].startswith(
            "the US label named in the comparability note (RAPAMUNE, setid 3275b824-3f82-4151-2ab2-0036a9ba0acc) is "
            "retired; DailyMed last showed it on 2026-10-26"))

    @unittest.skipUnless(FULL_US, "set ASOF_FULL_REPO to build/us for the compare checks")
    def test_example_query_3(self):
        out = labels.compare_jurisdictions(self.ix, "pembrolizumab", "2024-06-01", "contraindications", eu=self.eu)
        product, label = out["eu"]["product"], out["us"]["label"]
        self.assertEqual((product["label_id"], out["comparability"]["comparable_to_us"]), ("EMEA-H-C-003820", "yes"))
        self.assertTrue(label["label_id"].startswith("9333c79b"))
        self.assertTrue(product["wording"].startswith(WORDING_START))
        pair = out["pairs"][0]
        self.assertTrue(pair["us"]["text"] and pair["eu"]["text"])
        items = [{**pair[s]["citation"], "quote": line} for s, k in (("us", "only_in_us"), ("eu", "only_in_eu"))
                 for line in pair[k]["lines"]]
        self.assertTrue(items)
        self.assertEqual(labels.check_citations(self.ix, items, eu=self.eu)["passed"], len(items))
        server = mcp_server.Server(Path(FULL_US), MOLECULES, Path(FULL_EU))
        server._index, server._eu, server._eu_loaded = self.ix, self.eu, True
        res = server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                             "params": {"uri": "label://eu/EMEA-H-C-003820/2024-06-01/4.3"}})
        self.assertIn(pair["eu"]["text"], res["result"]["contents"][0]["text"])

    @unittest.skipUnless(FULL_US, "set ASOF_FULL_REPO to build/us for the compare checks")
    def test_compare_check_and_timings(self):
        report = cli.compare_check(self.ix, self.eu, [cli.CHECK_DATE])
        s = report["summary"][cli.CHECK_DATE]
        self.assertGreaterEqual(s["works"], 100)
        self.assertEqual(s["citation_failures"], 0)
        seconds = [r["dates"][cli.CHECK_DATE]["compare_seconds"] / 3 for r in report["molecules"]]
        self.assertLess(max(seconds), 3.0)
        self.assertLess(statistics.median(seconds), 3.0)


def _day_before(day: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


if __name__ == "__main__":
    unittest.main()
