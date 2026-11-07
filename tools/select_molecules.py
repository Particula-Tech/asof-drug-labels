#!/usr/bin/env python3
"""Select the molecules and their US label setids from local openFDA and DailyMed files.

Standard library only, Python 3.10+, deterministic: the same inputs give
byte-identical outputs (no timestamps are written; inputs are pinned by SHA-256).

Inputs (defaults under raw/m2/):
  openFDA NDC directory JSON          product -> labeler, application, category, ingredients, openfda block
  DailyMed dm_spl_zip_files_meta_data every current SPL: setid, current version, upload date, title [labeler]
  DailyMed rxnorm_mappings            setid -> RxCUI (product level: SCD/SBD/GPCK/BPCK and their synonyms)

Outputs:
  molecules/<inn>.json   one crosswalk file per selected molecule
  molecules/selection.json  the rules in words, input hashes, counts, request estimate, ranked list

Selection rules
---------------
Label universe (setids).  A setid qualifies when the openFDA NDC directory lists a
HUMAN PRESCRIPTION DRUG product for it with marketing category NDA, BLA, ANDA or
NDA AUTHORIZED GENERIC and `openfda.is_original_packager` true (approval holders
and their own packagers; repackagers and relabelers carry no flag and are out).
About 8% of approved prescription products have no `openfda` block at all, which
hides innovator labels such as Eliquis or Wegovy.  For those products, when the
labeler code is an approval holder by openFDA's own flag (at least half of its
harmonised products flagged, or none harmonised at all), the setid is recovered
from DailyMed's current-SPL list by matching the product's brand (or generic) name
against the start of the SPL title and the labeler against the bracketed labeler
at its end.  Only setids present in DailyMed's current list are kept; others are
logged as not current.  Left out: medical gases (dosage form GAS), irrigants
(route IRRIGATION), allergenic extracts (pharmacologic class "... Allergenic
Extract"), products with more than four active ingredients (parenteral nutrition,
dialysis and bowel-prep solutions) and the water, dextrose, electrolyte, trace
element and nutrition-lipid moieties listed in EXCLUDED_MOIETIES.

Molecule.  A molecule is one active moiety named at the INN/USAN level: the openFDA
active-ingredient name with its salt, ester, hydrate, solvate and counter-ion
tokens stripped from the end (levothyroxine sodium -> levothyroxine; fluticasone
propionate and fluticasone furoate -> fluticasone; adalimumab-atto -> adalimumab).
A short alias table joins spellings that the stripping cannot (divalproex and
valproate -> valproic acid).  Prodrugs with their own INN (tenofovir alafenamide,
mycophenolate mofetil, aripiprazole lauroxil) stay separate.  A fixed-dose
combination label belongs to every one of its component molecules; it is fetched
once and its `home_inn` is the best-ranked selected component.

Ranking (two sentences).  Every molecule is ranked twice: by the number of distinct
approval holders with a current original-packager label containing it (breadth of
the generic market), and by the number of distinct NDA/BLA application numbers for
products containing it (breadth of innovator, 505(b)(2) and biosimilar products),
ties going first to biologics, then to the molecule carried by more repackagers,
then to the most recently updated label.  The selection takes molecules alternately
from the two rankings, skipping ones already taken, until it holds --molecules
(default 500).

Setids per molecule.  Within a molecule, setids fall into two classes, NDA/BLA and
generic (authorized generic or ANDA).  Each class is ordered single-ingredient
before combination labels, then authorized generic before ANDA, then the earliest
marketing start date (a proxy for history depth), then setid; and a setid whose
application numbers are all new comes before a labeler's second setid for the same
application.  The first --cap-nda NDA/BLA setids (default 10) and the first
--cap-generic generic setids (default 6) are kept; the rest are logged under
`us_excluded`.

Request estimate.  One /history request per setid plus one request per version,
assuming --versions-per-setid versions (default 12, the average measured in a pilot run).

Usage:
  python3 tools/select_molecules.py
  python3 tools/select_molecules.py --add apixaban dupilumab osimertinib   # manual additions, ranks 501-503
  python3 tools/select_molecules.py --ndc path.json --spl-meta path.txt --rxnorm path.txt --out molecules
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NDC = ROOT / "raw" / "m2" / "openfda" / "ndc" / "drug-ndc-0001-of-0001.json"
DEFAULT_SPL_META = ROOT / "raw" / "m2" / "mappings" / "dm_spl_zip_files_meta_data" / "dm_spl_zip_files_meta_data.txt"
DEFAULT_RXNORM = ROOT / "raw" / "m2" / "mappings" / "rxnorm_mappings" / "rxnorm_mappings.txt"
DEFAULT_OUT = ROOT / "molecules"

PRODUCT_TYPE = "HUMAN PRESCRIPTION DRUG"
APPROVED_CATEGORIES = ("NDA", "BLA", "ANDA", "NDA AUTHORIZED GENERIC")
INNOVATOR_CATEGORIES = ("NDA", "BLA")
GAS_DOSAGE_FORM = "GAS"
IRRIGATION_ROUTE = "IRRIGATION"
ALLERGEN_CLASS_MARKER = "ALLERGENIC EXTRACT"
# Products with more active ingredients than this are parenteral nutrition,
# dialysis, bowel-prep or multi-electrolyte solutions, not drug labels to track.
MAX_ACTIVE_INGREDIENTS = 4
# A labeler code counts as an approval holder when at least this share of its
# openFDA-harmonised products carry is_original_packager (manufacturers sit at
# 100%, repackagers at 0%); codes with no harmonised product at all are accepted.
ORIGINAL_PACKAGER_SHARE = 0.5

# Tokens stripped from the end of an active-ingredient name to reach the moiety.
# Salts, counter-ions, hydrates, solvates, esters and ester prodrug suffixes that
# WHO treats as modified INNs, plus openFDA form qualifiers such as "UNSPECIFIED FORM".
SALT_TOKENS = frozenset("""
HYDROCHLORIDE HCL DIHYDROCHLORIDE TRIHYDROCHLORIDE HYDROBROMIDE DIHYDROBROMIDE
SODIUM DISODIUM TRISODIUM POTASSIUM DIPOTASSIUM CALCIUM MAGNESIUM AMMONIUM STRONTIUM
CHLORIDE DICHLORIDE BROMIDE IODIDE
ACETATE DIACETATE SULFATE BISULFATE DISULFATE PHOSPHATE DIPHOSPHATE POLYPHOSPHATE
MESYLATE DIMESYLATE MESILATE ESYLATE EDISYLATE BESYLATE TOSYLATE NAPSYLATE CAMSYLATE XINAFOATE TRIFENATATE
CITRATE TARTRATE BITARTRATE FUMARATE MALEATE MALATE L-MALATE S-MALATE SUCCINATE HEMISUCCINATE
BENZOATE NITRATE GLUCONATE LACTATE OXALATE PAMOATE CARBONATE BICARBONATE SACCHARATE ASPARTATE DL-ASPARTATE
GLUTAMATE TANNATE ADIPATE PIDOLATE ETHANOLATE GLYCOLATE SOLVATE PROPYLENE GLYCOL PROPANEDIOL 2,3-BUTANEDIOL
TROMETHAMINE MEGLUMINE DIMEGLUMINE LYSINE ARGININE OLAMINE EPOLAMINE HYCLATE TRIFLUOROACETATE METHYLSULFATE
HIPPURATE MANDELATE
ANHYDROUS HYDRATE MONOHYDRATE DIHYDRATE TRIHYDRATE HEMIHYDRATE SESQUIHYDRATE HEMIPENTAHYDRATE HEMI-PENTAHYDRATE
PENTAHYDRATE HEPTAHYDRATE HEXAHYDRATE DECAHYDRATE
PROPIONATE DIPROPIONATE VALERATE FUROATE CYPIONATE ENANTHATE UNDECANOATE DECANOATE CAPROATE PALMITATE PIVALATE
BUTYRATE PROBUTATE ETHYLSUCCINATE ESTOLATE LACTOBIONATE GLUCEPTATE STEARATE
AXETIL PROXETIL PIVOXIL MEDOXOMIL KAMEDOXOMIL CILEXETIL ETABONATE BENZATHINE PROCAINE
MONOBASIC DIBASIC TRIBASIC MACROCRYSTALS MACROCRYSTALLINE MICROCRYSTALLINE UNSPECIFIED FORM
""".split())

# Electrolyte cations: stripping never leaves one of these on its own, so
# SODIUM PHOSPHATE stays SODIUM PHOSPHATE while LITHIUM CARBONATE becomes LITHIUM.
CATION_TOKENS = frozenset("SODIUM DISODIUM TRISODIUM POTASSIUM DIPOTASSIUM CALCIUM MAGNESIUM AMMONIUM STRONTIUM".split())

# Leading qualifiers stripped from the start of a name.
LEADING_TOKENS = frozenset({"ANHYDROUS", "MONOBASIC", "DIBASIC", "TRIBASIC"})

# Names whose anion is part of the INN itself: never stripped.
PROTECTED_NAMES = frozenset({
    "DIMETHYL FUMARATE", "DIROXIMEL FUMARATE", "MONOMETHYL FUMARATE", "FERRIC CITRATE", "ZINC ACETATE",
    "BARIUM SULFATE", "SODIUM TETRADECYL SULFATE", "SILVER NITRATE",
})

# Names the stripping cannot join, mapped to their INN-level moiety.
MOIETY_ALIASES = {
    "VALPROATE": "VALPROIC ACID",
    "DIVALPROEX": "VALPROIC ACID",
    "CLAVULANATE": "CLAVULANIC ACID",
    "MYCOPHENOLATE": "MYCOPHENOLIC ACID",
    "GLYCOPYRROLATE": "GLYCOPYRRONIUM",
    "EPOETIN": "EPOETIN ALFA",
    "PANCRELIPASE AMYLASE": "PANCRELIPASE",
    "PANCRELIPASE LIPASE": "PANCRELIPASE",
    "PANCRELIPASE PROTEASE": "PANCRELIPASE",
}

# Water, dextrose, inorganic electrolytes, trace elements and parenteral-nutrition
# lipids: fluid and nutrition components, not label histories anyone needs.
# Medical gases are excluded by dosage form, irrigants by route.
EXCLUDED_MOIETIES = frozenset("""
WATER|DEXTROSE|SODIUM CHLORIDE|POTASSIUM CHLORIDE|CALCIUM CHLORIDE|MAGNESIUM CHLORIDE|AMMONIUM CHLORIDE
SODIUM BICARBONATE|SODIUM ACETATE|POTASSIUM ACETATE|SODIUM LACTATE|SODIUM PHOSPHATE|POTASSIUM PHOSPHATE
SODIUM CITRATE|CITRIC ACID|SODIUM GLUCONATE|CALCIUM GLUCONATE|CALCIUM ACETATE
MAGNESIUM SULFATE|SODIUM SULFATE|POTASSIUM SULFATE
ZINC|COPPER|CUPRIC|SELENIOUS ACID|MANGANESE|CHROMIC
SOYBEAN OIL|FISH OIL|OLIVE OIL|SAFFLOWER OIL|MEDIUM-CHAIN TRIGLYCERIDES
""".replace("\n", "|").split("|")) - {""}

# FDA four-letter biologic suffix, e.g. ADALIMUMAB-ATTO.
BIOLOGIC_SUFFIX = re.compile(r"^([A-Z]+)-[A-Z]{4}$")
NON_ALNUM = re.compile(r"[^A-Z0-9]+")
PRODUCT_NAME_CONJUNCTION = ("AND ", "IN ", "WITH ", "/")


class SelectionError(RuntimeError):
    """An input is missing or malformed."""


# ------------------------------------------------------------------ helpers

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def norm(text: str) -> str:
    """Uppercase, punctuation collapsed to single spaces."""
    return NON_ALNUM.sub(" ", text.upper()).strip()


def moiety(name: str) -> str:
    """INN/USAN-level active moiety of an openFDA active-ingredient name."""
    tokens = [t.strip(",") for t in name.upper().split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return ""
    while len(tokens) > 1 and tokens[0] in LEADING_TOKENS:
        tokens.pop(0)
    match = BIOLOGIC_SUFFIX.match(tokens[-1])
    if match:
        tokens[-1] = match.group(1)
    # Strip salt/ester tokens from the end, but never reduce a name to bare
    # electrolyte cations (SODIUM PHOSPHATE must stay SODIUM PHOSPHATE, not SODIUM)
    # and never touch a name whose anion is part of the INN (DIMETHYL FUMARATE).
    while len(tokens) > 1 and tokens[-1] in SALT_TOKENS:
        if " ".join(tokens) in PROTECTED_NAMES or all(t in CATION_TOKENS for t in tokens[:-1]):
            break
        tokens.pop()
    key = " ".join(tokens)
    return MOIETY_ALIASES.get(key, key)


def inn_slug(key: str) -> str:
    slug = NON_ALNUM.sub("-", key.upper()).strip("-").lower()
    if not slug:
        raise SelectionError(f"cannot derive a file name from molecule key {key!r}")
    return slug


def iso_date(value: str | None) -> str | None:
    """openFDA YYYYMMDD or DailyMed MM/DD/YYYY to ISO; None when absent or odd."""
    if not value:
        return None
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value)
    if match:
        return f"{match.group(3)}-{match.group(1)}-{match.group(2)}"
    return None


# ------------------------------------------------------------------ loading

@dataclass
class SplMeta:
    setid: str
    spl_version: int | None
    upload_date: str | None
    title: str
    title_name: str      # normalised product part of the title
    title_labeler: str   # normalised bracketed labeler


def load_ndc(path: Path) -> list[dict]:
    if not path.is_file():
        raise SelectionError(f"openFDA NDC file not found: {path}")
    try:
        with path.open("rb") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        raise SelectionError(f"cannot parse openFDA NDC JSON {path}: {error}") from error
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SelectionError(f"openFDA NDC JSON {path} has no 'results' list")
    return results


def load_spl_meta(path: Path) -> dict[str, SplMeta]:
    if not path.is_file():
        raise SelectionError(f"DailyMed SPL metadata file not found: {path}")
    metas: dict[str, SplMeta] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        header = handle.readline().rstrip("\n").split("|")
        if header[:5] != ["SETID", "ZIP_FILE_NAME", "UPLOAD_DATE", "SPL_VERSION", "TITLE"]:
            raise SelectionError(f"unexpected header in {path}: {header}")
        for line in handle:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 5:
                continue
            setid, _zip, upload, version, title = parts[0], parts[1], parts[2], parts[3], "|".join(parts[4:])
            bracket = title.rfind("[")
            if bracket > 0 and title.endswith("]"):
                name, labeler = title[:bracket], title[bracket + 1:-1]
            else:
                name, labeler = title, ""
            metas[setid] = SplMeta(
                setid=setid,
                spl_version=int(version) if version.isdigit() else None,
                upload_date=iso_date(upload),
                title=title,
                title_name=norm(name),
                title_labeler=norm(labeler),
            )
    if not metas:
        raise SelectionError(f"no SPL rows read from {path}")
    return metas


def load_rxnorm(path: Path) -> dict[str, set[str]]:
    if not path.is_file():
        raise SelectionError(f"DailyMed RxNorm mapping file not found: {path}")
    rxcuis: dict[str, set[str]] = defaultdict(set)
    with path.open(encoding="utf-8", errors="replace") as handle:
        header = handle.readline().rstrip("\n").split("|")
        if header[:3] != ["SETID", "SPL_VERSION", "RXCUI"]:
            raise SelectionError(f"unexpected header in {path}: {header}")
        for line in handle:
            parts = line.rstrip("\n").split("|")
            if len(parts) >= 3 and parts[2]:
                rxcuis[parts[0]].add(parts[2])
    return rxcuis


# ------------------------------------------------------------------ setids

@dataclass
class SetidRecord:
    setid: str
    labeler: str
    labeler_key: str
    source: str                      # "openfda" or "dailymed-title-match"
    categories: set[str] = field(default_factory=set)
    applications: set[str] = field(default_factory=set)
    brand_names: set[str] = field(default_factory=set)
    ingredient_names: set[str] = field(default_factory=set)
    moieties: set[str] = field(default_factory=set)
    product_ndcs: set[str] = field(default_factory=set)
    rxcuis: set[str] = field(default_factory=set)
    marketing_start: str | None = None
    meta: SplMeta | None = None

    @property
    def combination(self) -> bool:
        return len(self.moieties) > 1

    def category_rank(self) -> int:
        if self.categories & set(INNOVATOR_CATEGORIES):
            return 0
        if "NDA AUTHORIZED GENERIC" in self.categories:
            return 1
        return 2


def is_allergen(record: dict) -> bool:
    classes = record.get("openfda", {}).get("pharm_class_epc", []) or []
    return any(ALLERGEN_CLASS_MARKER in str(c).upper() for c in classes)


def record_moieties(record: dict) -> tuple[set[str], set[str]]:
    names = {str(a.get("name", "")).strip().upper() for a in record.get("active_ingredients", []) or []}
    names.discard("")
    moieties = {moiety(n) for n in names} - EXCLUDED_MOIETIES
    moieties.discard("")
    return names, moieties


def is_irrigant(record: dict) -> bool:
    routes = [str(r).upper() for r in (record.get("route") or [])]
    return bool(routes) and all(r == IRRIGATION_ROUTE for r in routes)


def approved_rx(records: list[dict]) -> list[dict]:
    """Human prescription products under an approval, minus gases, irrigants, allergens and fluid cocktails."""
    return [
        r for r in records
        if r.get("product_type") == PRODUCT_TYPE
        and r.get("marketing_category") in APPROVED_CATEGORIES
        and r.get("dosage_form") != GAS_DOSAGE_FORM
        and not is_irrigant(r)
        and not is_allergen(r)
        and len(r.get("active_ingredients") or []) <= MAX_ACTIVE_INGREDIENTS
    ]


def original_packager_codes(products: list[dict]) -> set[str]:
    """Labeler codes that are approval holders by openFDA's own flag share, plus codes openFDA never harmonised."""
    harmonised: Counter = Counter()
    flagged: Counter = Counter()
    codes: set[str] = set()
    for record in products:
        code = str(record.get("product_ndc", "")).split("-")[0]
        if not code:
            continue
        codes.add(code)
        openfda = record.get("openfda", {}) or {}
        if openfda.get("spl_set_id"):
            harmonised[code] += 1
            if openfda.get("is_original_packager"):
                flagged[code] += 1
    return {c for c in codes if harmonised[c] == 0 or flagged[c] / harmonised[c] >= ORIGINAL_PACKAGER_SHARE}


def resolve_by_title(record: dict, by_labeler: dict[str, list[SplMeta]]) -> list[SplMeta]:
    """Current SPLs of the record's labeler whose title names the product.

    Tiers, first non-empty wins: the title equals the brand or generic name; it starts
    with that name (not followed by "AND"/"IN"/"WITH", which would be another product);
    or, for a true brand name only, a multi-product title lists it further in
    ("XELJANZ (...) TABLET XELJANZ XR (...)").
    """
    labeler = norm(record.get("labeler_name", ""))
    generic = norm(record.get("generic_name") or "")
    names = []
    for key in ("brand_name", "generic_name"):
        value = norm(record.get(key) or "")
        if value and value not in names:
            names.append(value)
    for name in names:
        strict, loose, inside = [], [], []
        for meta in by_labeler.get(labeler, []):
            title = meta.title_name
            if title == name:
                strict.append(meta)
            elif title.startswith(name + " "):
                rest = title[len(name) + 1:]
                if rest.startswith(PRODUCT_NAME_CONJUNCTION):
                    continue
                loose.append(meta)
            elif name != generic and f" {name} " in f" {title} ":
                inside.append(meta)
        for tier in (strict, loose, inside):
            if tier:
                return tier
    return []


def build_setids(records: list[dict], spl_meta: dict[str, SplMeta], rxnorm: dict[str, set[str]]):
    """All original-packager approval-holder setids with their products folded in."""
    diagnostics = {
        "approved_rx_products": 0,
        "flagged_original_packager_products": 0,
        "products_without_openfda_block": 0,
        "fallback_products_resolved": 0,
        "fallback_products_unresolved": 0,
        "setids_not_in_dailymed_current": [],
        "unii_by_ingredient_unresolved": [],
    }
    products = approved_rx(records)
    diagnostics["approved_rx_products"] = len(products)
    original_codes = original_packager_codes(products)
    by_labeler: dict[str, list[SplMeta]] = defaultdict(list)
    for meta in spl_meta.values():
        by_labeler[meta.title_labeler].append(meta)
    for metas in by_labeler.values():
        metas.sort(key=lambda m: m.setid)

    setids: dict[str, SetidRecord] = {}
    not_current: set[str] = set()

    def fold(setid: str, record: dict, source: str) -> None:
        meta = spl_meta.get(setid)
        if meta is None:
            not_current.add(setid)
            return
        rec = setids.get(setid)
        if rec is None:
            rec = SetidRecord(
                setid=setid,
                labeler=record.get("labeler_name", ""),
                labeler_key=norm(record.get("labeler_name", "")),
                source=source,
                meta=meta,
            )
            setids[setid] = rec
        rec.categories.add(record["marketing_category"])
        if record.get("application_number"):
            rec.applications.add(record["application_number"])
        if record.get("brand_name"):
            rec.brand_names.add(record["brand_name"])
        names, moieties = record_moieties(record)
        rec.ingredient_names |= names
        rec.moieties |= moieties
        if record.get("product_ndc"):
            rec.product_ndcs.add(record["product_ndc"])
        rec.rxcuis |= set(record.get("openfda", {}).get("rxcui", []) or [])
        rec.rxcuis |= rxnorm.get(setid, set())
        start = iso_date(record.get("marketing_start_date"))
        if start and (rec.marketing_start is None or start < rec.marketing_start):
            rec.marketing_start = start

    for record in products:
        openfda = record.get("openfda", {}) or {}
        spl_set_ids = openfda.get("spl_set_id") or []
        if spl_set_ids:
            if openfda.get("is_original_packager"):
                diagnostics["flagged_original_packager_products"] += 1
                for setid in spl_set_ids:
                    fold(setid, record, "openfda")
            continue
        diagnostics["products_without_openfda_block"] += 1
        if record.get("product_ndc", "").split("-")[0] not in original_codes:
            continue
        matches = resolve_by_title(record, by_labeler)
        if matches:
            diagnostics["fallback_products_resolved"] += 1
            for meta in matches:
                fold(meta.setid, record, "dailymed-title-match")
        else:
            diagnostics["fallback_products_unresolved"] += 1

    diagnostics["setids_not_in_dailymed_current"] = sorted(not_current)
    return setids, diagnostics


def unii_by_ingredient(records: list[dict]) -> dict[str, set[str]]:
    """Ingredient name -> UNII.

    Read from single-ingredient products with one UNII, then extended by elimination
    over combination products whose other ingredients are already resolved (openFDA
    does not state the order of its `unii` list, so no positional guess is made).
    """
    mapping: dict[str, set[str]] = defaultdict(set)
    combos: list[tuple[list[str], set[str]]] = []
    for record in records:
        names = [str(a.get("name", "")).strip().upper() for a in record.get("active_ingredients") or []]
        names = [n for n in names if n]
        uniis = set(record.get("openfda", {}).get("unii") or [])
        if len(names) == 1 and len(uniis) == 1:
            mapping[names[0]].add(next(iter(uniis)))
        elif len(names) >= 2 and len(set(names)) == len(names) and len(uniis) == len(names):
            combos.append((names, uniis))
    for _pass in range(2):
        for names, uniis in combos:
            unresolved = [n for n in names if n not in mapping]
            if len(unresolved) != 1:
                continue
            known = set()
            for n in names:
                if n in mapping:
                    known |= mapping[n]
            leftover = uniis - known
            if len(leftover) == 1 and len(known & uniis) == len(names) - 1:
                mapping[unresolved[0]].add(next(iter(leftover)))
    return mapping


# ------------------------------------------------------------------ molecules

@dataclass
class Molecule:
    key: str
    setids: set[str] = field(default_factory=set)
    holders: set[str] = field(default_factory=set)
    applications: set[str] = field(default_factory=set)
    repackagers: set[str] = field(default_factory=set)
    ingredient_names: set[str] = field(default_factory=set)
    earliest_start: str | None = None
    latest_update: str | None = None
    biologic: bool = False


def group_molecules(setids: dict[str, SetidRecord], records: list[dict]) -> dict[str, Molecule]:
    molecules: dict[str, Molecule] = {}
    for rec in setids.values():
        for key in rec.moieties:
            mol = molecules.get(key)
            if mol is None:
                mol = molecules[key] = Molecule(key=key)
            mol.setids.add(rec.setid)
            mol.holders.add(rec.labeler_key)
            mol.ingredient_names |= {n for n in rec.ingredient_names if moiety(n) == key}
            if rec.categories & set(INNOVATOR_CATEGORIES):
                mol.applications |= {a for a in rec.applications if a.startswith(("NDA", "BLA"))}
            if "BLA" in rec.categories:
                mol.biologic = True
            if rec.marketing_start and (mol.earliest_start is None or rec.marketing_start < mol.earliest_start):
                mol.earliest_start = rec.marketing_start
            upload = rec.meta.upload_date if rec.meta else None
            if upload and (mol.latest_update is None or upload > mol.latest_update):
                mol.latest_update = upload
    # Repackagers: products openFDA harmonised to an SPL but did not flag as original packager.
    for record in approved_rx(records):
        openfda = record.get("openfda", {}) or {}
        if openfda.get("is_original_packager") or not openfda.get("spl_set_id"):
            continue
        _names, moieties = record_moieties(record)
        for key in moieties:
            if key in molecules:
                molecules[key].repackagers.add(norm(record.get("labeler_name", "")))
    return molecules


def breadth_key(mol: Molecule):
    return (-len(mol.holders), -len(mol.applications), mol.key)


def innovator_key(mol: Molecule):
    """More NDA/BLA applications first; ties to biologics, then more repackagers, then the latest label update."""
    return (-len(mol.applications), not mol.biologic, -len(mol.repackagers), _desc_date(mol.latest_update), mol.key)


def rank_and_select(molecules: dict[str, Molecule], limit: int) -> list[tuple[str, str, int]]:
    """Alternate between the breadth and innovator rankings until `limit` molecules are chosen.

    Returns (key, selected_by, overall_rank) in selection order.
    """
    by_breadth = sorted(molecules.values(), key=breadth_key)
    by_innovator = sorted(molecules.values(), key=innovator_key)
    chosen: list[tuple[str, str, int]] = []
    taken: set[str] = set()
    queues = [("breadth", iter(by_breadth)), ("innovator", iter(by_innovator))]
    exhausted = 0
    while len(chosen) < limit and exhausted < len(queues):
        exhausted = 0
        for label, queue in queues:
            if len(chosen) >= limit:
                break
            for mol in queue:
                if mol.key not in taken:
                    taken.add(mol.key)
                    chosen.append((mol.key, label, len(chosen) + 1))
                    break
            else:
                exhausted += 1
    return chosen


def _desc_date(value: str | None) -> str:
    """Sort key that puts the latest ISO date first; missing dates last."""
    if not value:
        return "9999-99-99"
    return "".join(chr(ord("9") - (ord(c) - ord("0"))) if c.isdigit() else c for c in value)


def setid_order_key(rec: SetidRecord):
    """Innovator class before generic class; single-ingredient before combination; AG before ANDA; oldest first."""
    return (rec.category_rank() != 0, rec.combination, rec.category_rank(), rec.marketing_start or "9999-12-31", rec.setid)


def application_diverse(ordered: list[SetidRecord]) -> list[SetidRecord]:
    """Stable reorder: a setid whose application numbers are all new comes before repeats.

    One labeler often has several setids for one application (vial sizes, kits); this
    lets every application get a label before any application gets a second one.
    """
    seen: set[str] = set()
    fresh, repeats = [], []
    for rec in ordered:
        if rec.applications and rec.applications.isdisjoint(seen):
            fresh.append(rec)
        else:
            repeats.append(rec)
        seen |= rec.applications
    return fresh + repeats


def choose_setids(mol: Molecule, setids: dict[str, SetidRecord], cap_nda: int, cap_generic: int):
    ordered = sorted((setids[s] for s in mol.setids), key=setid_order_key)
    innovator = application_diverse([r for r in ordered if r.category_rank() == 0])
    generic = application_diverse([r for r in ordered if r.category_rank() != 0])
    kept = innovator[:cap_nda] + generic[:cap_generic]
    dropped = [(r, f"over cap of {cap_nda} NDA/BLA setids") for r in innovator[cap_nda:]]
    dropped += [(r, f"over cap of {cap_generic} ANDA/authorized-generic setids") for r in generic[cap_generic:]]
    return kept, dropped


# ------------------------------------------------------------------ output

def setid_json(rec: SetidRecord, home_inn: str | None) -> dict:
    meta = rec.meta
    return {
        "setid": rec.setid,
        "labeler": rec.labeler,
        "application_numbers": sorted(rec.applications),
        "marketing_categories": sorted(rec.categories),
        "title": meta.title if meta else None,
        "spl_version": meta.spl_version if meta else None,
        "upload_date": meta.upload_date if meta else None,
        "brand_names": sorted(rec.brand_names),
        "ingredients": sorted(rec.ingredient_names),
        "moieties": sorted(rec.moieties),
        "combination": rec.combination,
        "home_inn": home_inn,
        "marketing_start": rec.marketing_start,
        "product_ndcs": sorted(rec.product_ndcs),
        "rxcui": sorted(rec.rxcuis, key=lambda s: (len(s), s)),
        "source": rec.source,
    }


def molecule_payload(slug: str, mol: Molecule, kept: list[SetidRecord], dropped: list[tuple[SetidRecord, str]],
                     uniis: dict[str, set[str]], rank: int, selected_by: str, home_for) -> dict:
    """molecules/<inn>.json of one selected molecule (eu is filled later by tools/eu.py crosswalk)."""
    unii_entries = []
    for name in sorted(mol.ingredient_names):
        unii_entries.extend({"unii": u, "ingredient": name} for u in sorted(uniis.get(name) or ()))
    return {
        "inn": slug,
        "name": mol.key,
        "ingredient_names": sorted(mol.ingredient_names),
        "unii": unii_entries,
        "unii_unresolved": sorted(n for n in mol.ingredient_names if not uniis.get(n)),
        "rxcui": sorted({c for rec in kept for c in rec.rxcuis}, key=lambda s: (len(s), s)),
        "rxcui_level": "product (SCD/SBD/GPCK/BPCK) from openFDA NDC and DailyMed rxnorm_mappings; ingredient-level RxCUI is not derivable offline",
        "rank": rank,
        "selected_by": selected_by,
        "biologic": mol.biologic,
        "stats": {
            "approval_holders": len(mol.holders),
            "approval_holder_setids": len(mol.setids),
            "nda_bla_applications": len(mol.applications),
            "repackagers": len(mol.repackagers),
            "earliest_marketing_start": mol.earliest_start,
            "latest_label_update": mol.latest_update,
        },
        "us": [setid_json(rec, home_for(rec)) for rec in kept],
        "us_excluded": [
            {"setid": rec.setid, "labeler": rec.labeler, "marketing_categories": sorted(rec.categories),
             "combination": rec.combination, "reason": reason}
            for rec, reason in dropped
        ],
        "eu": [],
    }


MANUAL_RULE = ("Molecules added by hand with --add after the ranked selection (selected_by 'manual'): the same "
               "label universe, molecule key and per-molecule setid rules and caps as the ranked molecules; ranks "
               "continue after the highest existing rank in the order given. Existing molecule files are not "
               "rewritten; a setid already listed by an existing molecule keeps that molecule's home_inn.")


def add_molecules(args: argparse.Namespace, names: list[str]) -> list[dict]:
    """Write molecules/<inn>.json for each name in ``names`` (INN or slug) without touching the existing
    files, and record the additions under manual_additions in selection.json. A name that already has a
    file with selected_by 'manual' is written again with its rank (idempotent); any other existing file,
    or a name that is not a candidate molecule, is an error."""
    out = Path(args.out)
    records = load_ndc(Path(args.ndc))
    setids, _ = build_setids(records, load_spl_meta(Path(args.spl_meta)), load_rxnorm(Path(args.rxnorm)))
    uniis = unii_by_ingredient(records)
    molecules = group_molecules(setids, records)
    existing: dict[str, dict] = {}
    for path in sorted(out.glob("*.json")):
        if path.name != "selection.json":
            existing[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    homes: dict[str, str] = {}
    for slug, mol in existing.items():
        if mol.get("selected_by") == "manual" and slug in {inn_slug(moiety(n)) for n in names}:
            continue
        for entry in mol.get("us") or []:
            homes.setdefault(entry["setid"], entry.get("home_inn") or slug)
    next_rank = max((m.get("rank") or 0 for slug, m in existing.items() if m.get("selected_by") != "manual"),
                    default=0) + 1
    added = []
    for name in names:
        key = moiety(name.replace("-", " "))
        slug = inn_slug(key)
        mol = molecules.get(key)
        if mol is None:
            raise SelectionError(f"{name!r} is not a candidate molecule in the inputs (no current approval-holder "
                                 "prescription label)")
        prior = existing.get(slug)
        if prior is not None and prior.get("selected_by") != "manual":
            raise SelectionError(f"{slug} is already selected (selected_by {prior.get('selected_by')!r})")
        rank = next_rank
        next_rank += 1
        kept, dropped = choose_setids(mol, setids, args.cap_nda, args.cap_generic)
        payload = molecule_payload(slug, mol, kept, dropped, uniis, rank, "manual",
                                   lambda rec, slug=slug: homes.get(rec.setid, slug))
        if prior is not None:   # keep what later stages wrote (eu, eu_selection, retired us entries)
            retired = [e for e in prior.get("us") or [] if e.get("status") == "retired"]
            current = {e["setid"] for e in payload["us"]}
            payload["us"] += [e for e in retired if e["setid"] not in current]
            for k in ("eu", "eu_selection"):
                if k in prior:
                    payload[k] = prior[k]
        dump_json(out / f"{slug}.json", payload)
        added.append({"rank": rank, "inn": slug, "selected_by": "manual", "setids": len(kept),
                      "setids_excluded": len(dropped), "approval_holders": len(mol.holders),
                      "nda_bla_applications": len(mol.applications), "biologic": mol.biologic})
    sel_path = out / "selection.json"
    selection = json.loads(sel_path.read_text(encoding="utf-8")) if sel_path.exists() else {}
    prior_added = [a for a in selection.get("manual_additions", {}).get("molecules", [])
                   if a["inn"] not in {x["inn"] for x in added}]
    selection["manual_additions"] = {"rule": MANUAL_RULE,
                                     "molecules": sorted(prior_added + added, key=lambda a: a["rank"])}
    dump_json(sel_path, selection)
    return added


def dump_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict:
    ndc_path, meta_path, rx_path, out = Path(args.ndc), Path(args.spl_meta), Path(args.rxnorm), Path(args.out)
    records = load_ndc(ndc_path)
    spl_meta = load_spl_meta(meta_path)
    rxnorm = load_rxnorm(rx_path)

    setids, diagnostics = build_setids(records, spl_meta, rxnorm)
    uniis = unii_by_ingredient(records)
    molecules = group_molecules(setids, records)
    if not molecules:
        raise SelectionError("no candidate molecules found; check the input files")
    chosen = rank_and_select(molecules, args.molecules)

    rank_of = {key: rank for key, _by, rank in chosen}
    slugs: dict[str, str] = {}
    for key, _by, _rank in chosen:
        slug = inn_slug(key)
        if slug in slugs.values():
            raise SelectionError(f"two molecules map to the same file name {slug!r}")
        slugs[key] = slug

    # Which setids each selected molecule keeps.
    kept_by_mol: dict[str, list[SetidRecord]] = {}
    dropped_by_mol: dict[str, list[tuple[SetidRecord, str]]] = {}
    for key, _by, _rank in chosen:
        kept_by_mol[key], dropped_by_mol[key] = choose_setids(molecules[key], setids, args.cap_nda, args.cap_generic)
    kept_setids_by_mol = {key: {rec.setid for rec in kept} for key, kept in kept_by_mol.items()}
    selected_setids: set[str] = set().union(*kept_setids_by_mol.values()) if kept_setids_by_mol else set()

    def home_for(rec: SetidRecord) -> str | None:
        """The best-ranked selected molecule that keeps this setid: where the build stores it."""
        homes = [(rank_of[m], slugs[m]) for m in rec.moieties if m in rank_of and rec.setid in kept_setids_by_mol[m]]
        return min(homes)[1] if homes else None

    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.json"):
        stale.unlink()

    unresolved_unii: set[str] = set()
    ranked_summary = []
    for key, selected_by, rank in chosen:
        mol = molecules[key]
        kept, dropped = kept_by_mol[key], dropped_by_mol[key]
        payload = molecule_payload(slugs[key], mol, kept, dropped, uniis, rank, selected_by, home_for)
        unresolved_unii.update(payload["unii_unresolved"])
        dump_json(out / f"{slugs[key]}.json", payload)
        ranked_summary.append({
            "rank": rank, "inn": slugs[key], "selected_by": selected_by,
            "setids": len(kept), "setids_excluded": len(dropped),
            "approval_holders": len(mol.holders), "nda_bla_applications": len(mol.applications),
            "repackagers": len(mol.repackagers), "biologic": mol.biologic,
        })

    per_mol = [len(kept_by_mol[key]) for key, _by, _rank in chosen]
    histogram = Counter(per_mol)
    category_counts = Counter()
    source_counts = Counter()
    for setid in selected_setids:
        rec = setids[setid]
        category_counts["NDA/BLA" if rec.category_rank() == 0 else ("NDA AUTHORIZED GENERIC" if rec.category_rank() == 1 else "ANDA")] += 1
        source_counts[rec.source] += 1
    n_setids = len(selected_setids)
    requests = n_setids * (1 + args.versions_per_setid)
    selection = {
        "generated_by": "tools/select_molecules.py",
        "rules": {
            "label_universe": (
                "Current DailyMed setids whose openFDA NDC products are HUMAN PRESCRIPTION DRUG under NDA, BLA, ANDA or "
                "NDA AUTHORIZED GENERIC with openfda.is_original_packager true (approval holders only; repackagers and "
                "relabelers excluded). Products with no openfda block are matched to a current DailyMed SPL by brand/generic "
                "name and labeler when their labeler code is an approval holder by openFDA's flag share. Medical gases, "
                "irrigants, allergenic extracts, products with more than four active ingredients, and water/dextrose/"
                "electrolyte/trace-element/nutrition-lipid moieties are excluded. Retired setids are logged, not fetched."
            ),
            "molecule": (
                "One active moiety at INN/USAN level: the openFDA active-ingredient name with salt, ester, hydrate, "
                "solvate and counter-ion tokens stripped from the end (levothyroxine sodium -> levothyroxine), plus a "
                "short alias table; prodrugs with their own INN stay separate. A fixed-dose combination label is listed "
                "under each selected component molecule, fetched once, and homed under the best-ranked component."
            ),
            "ranking": (
                "Every molecule is ranked twice: by the number of distinct approval holders with a current "
                "original-packager label containing it, and by the number of distinct NDA/BLA application numbers for "
                "products containing it (ties: biologics first, then more repackagers, then most recently updated label). "
                "Molecules are taken alternately from the two rankings, skipping repeats, until the list holds the target count."
            ),
            "setids_per_molecule": (
                f"Two classes per molecule, NDA/BLA and generic (authorized generic or ANDA); each ordered single-ingredient "
                f"before combination, authorized generic before ANDA, earliest marketing start first, and one setid per "
                f"application number before repeats. The first {args.cap_nda} NDA/BLA and the first {args.cap_generic} "
                f"generic setids are kept; the rest are listed under us_excluded."
            ),
        },
        "parameters": {
            "molecules": args.molecules, "cap_nda": args.cap_nda, "cap_generic": args.cap_generic,
            "versions_per_setid": args.versions_per_setid, "requests_per_second": args.rate,
        },
        "inputs": [
            {"role": "openfda_ndc", "path": _rel(ndc_path), "sha256": sha256_file(ndc_path), "records": len(records)},
            {"role": "dailymed_spl_meta", "path": _rel(meta_path), "sha256": sha256_file(meta_path), "records": len(spl_meta)},
            {"role": "dailymed_rxnorm", "path": _rel(rx_path), "sha256": sha256_file(rx_path), "setids": len(rxnorm)},
        ],
        "exclusions": {
            "medical_gas_dosage_form": GAS_DOSAGE_FORM,
            "allergenic_extract_class_marker": ALLERGEN_CLASS_MARKER,
            "excluded_moieties": sorted(EXCLUDED_MOIETIES),
            "moiety_aliases": dict(sorted(MOIETY_ALIASES.items())),
        },
        "diagnostics": diagnostics | {"unii_by_ingredient_unresolved": sorted(unresolved_unii)},
        "counts": {
            "candidate_setids": len(setids),
            "candidate_molecules": len(molecules),
            "selected_molecules": len(chosen),
            "selected_by": dict(Counter(by for _k, by, _r in chosen)),
            "selected_setids": n_setids,
            "selected_setids_by_category": dict(sorted(category_counts.items())),
            "selected_setids_by_source": dict(sorted(source_counts.items())),
            "setids_excluded_over_cap": sum(len(d) for d in dropped_by_mol.values()),
            "setids_per_molecule": {
                "min": min(per_mol), "max": max(per_mol),
                "mean": round(statistics.fmean(per_mol), 2), "median": statistics.median(per_mol),
                "histogram": {str(k): v for k, v in sorted(histogram.items())},
            },
        },
        "request_estimate": {
            "assumption": f"1 history request per setid + {args.versions_per_setid} version requests per setid (average measured in a pilot run), at {args.rate} requests/s",
            "requests": requests,
            "hours_at_rate": round(requests / args.rate / 3600, 1),
        },
        "molecules": ranked_summary,
    }
    dump_json(out / "selection.json", selection)
    return selection


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def print_summary(selection: dict) -> None:
    counts, est = selection["counts"], selection["request_estimate"]
    print(f"candidates: {counts['candidate_molecules']} molecules, {counts['candidate_setids']} setids")
    print(f"selected:   {counts['selected_molecules']} molecules ({counts['selected_by']}), {counts['selected_setids']} setids")
    print(f"            by category {counts['selected_setids_by_category']}, by source {counts['selected_setids_by_source']}")
    spm = counts["setids_per_molecule"]
    print(f"            setids per molecule min {spm['min']} median {spm['median']} mean {spm['mean']} max {spm['max']}; "
          f"{counts['setids_excluded_over_cap']} setids over cap")
    diag = selection["diagnostics"]
    print(f"diagnostics: {diag['products_without_openfda_block']} approved Rx products lack an openfda block; "
          f"{diag['fallback_products_resolved']} resolved by DailyMed title, {diag['fallback_products_unresolved']} not; "
          f"{len(diag['setids_not_in_dailymed_current'])} setids not current; "
          f"{len(diag['unii_by_ingredient_unresolved'])} ingredient names without UNII")
    print(f"requests:   {est['requests']} ({est['assumption']}) = {est['hours_at_rate']} h")
    print("top 20:")
    for row in selection["molecules"][:20]:
        print(f"  {row['rank']:3d} {row['inn']:28s} {row['selected_by']:9s} setids={row['setids']:2d} "
              f"holders={row['approval_holders']:3d} apps={row['nda_bla_applications']:2d} repack={row['repackagers']:3d}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ndc", default=str(DEFAULT_NDC), help="openFDA NDC directory JSON")
    parser.add_argument("--spl-meta", default=str(DEFAULT_SPL_META), help="DailyMed dm_spl_zip_files_meta_data.txt")
    parser.add_argument("--rxnorm", default=str(DEFAULT_RXNORM), help="DailyMed rxnorm_mappings.txt")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory for molecules/*.json")
    parser.add_argument("--molecules", type=int, default=500, help="number of molecules to select")
    parser.add_argument("--cap-nda", type=int, default=10, help="max NDA/BLA setids per molecule")
    parser.add_argument("--cap-generic", type=int, default=6, help="max ANDA/authorized-generic setids per molecule")
    parser.add_argument("--versions-per-setid", type=int, default=12, help="assumed versions per setid for the request estimate")
    parser.add_argument("--rate", type=float, default=2.0, help="requests per second for the time estimate")
    parser.add_argument("--add", nargs="+", metavar="INN",
                        help="add these molecules by hand (selected_by 'manual', ranks after the existing ones) "
                             "without rewriting the existing molecule files")
    args = parser.parse_args(argv)
    for name in ("molecules", "cap_nda", "cap_generic", "versions_per_setid"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be >= 0")
    if args.molecules == 0:
        parser.error("--molecules must be >= 1")
    if args.rate <= 0:
        parser.error("--rate must be > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.add:
        try:
            added = add_molecules(args, args.add)
        except SelectionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        for a in added:
            print(f"added {a['inn']} rank {a['rank']}: {a['setids']} setids ({a['setids_excluded']} over the caps)")
        return 0
    try:
        selection = run(args)
    except SelectionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print_summary(selection)
    return 0


if __name__ == "__main__":
    sys.exit(main())
