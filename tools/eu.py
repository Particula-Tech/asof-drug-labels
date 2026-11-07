#!/usr/bin/env python3
"""EU sources: product selection, Union Register annex fetch, coverage and EMA forward capture.

Subcommands (each writes to disk, so an interrupted run resumes where it stopped):
  discover   EMA medicines JSON + Union Register open dataset -> sources/eu-products.json
  fetch      every reference product's Union Register page and English annex PDFs
             -> sources/eu/<EMEA-H-C-nnnnnn>.json, PDFs in raw/eu/sha256/
  pages      page count and text-layer check of every fetched annex (runs .venv-eu/bin/python)
  coverage   counts, timings, requests, sizes and every logged problem -> reports/eu-fetch.json
  capture    EMA medicines JSON + current product-information PDF of every reference product
             -> sources/eu-capture/<YYYY-MM-DD>.json, with the changes since the previous capture
  build      convert every pinned English annex with tools/eu_qrd.py under .venv-eu/bin/python
             (results cached in raw/eu/converted/) and write build/eu with git fast-import;
             reports/eu-coverage.json and .md
  rebuild-check  build again from an empty cache into a temporary repo and compare every hash
  candidates molecules present in build/us and build/eu with version counts per side
             -> reports/eu-us-overlap.json
  crosswalk  eu and eu_selection of every molecules/<inn>.json from sources/eu-products.json, checked
             against build/eu product.json (offline; --check reports drift and exits 1)

Network goes only to ec.europa.eu and www.ema.europa.eu, at most 2 requests per second
shared by all workers, with a fixed User-Agent. Every response is cached under raw/eu/
(gitignored): pages and JSON under raw/eu/sha256/<sha256>.<ext> pinned by the sources files,
PDFs the same way. A rerun makes no request for anything already cached; HTTP 404s are
logged in the sources file and retried only with --retry-logged.

This module uses the standard library only. PDF page counts (pages) and the annex conversion
(build) use the pinned PyMuPDF in .venv-eu through subprocesses; fetch and capture do not need it.

Examples:
  python3 -I tools/eu.py discover
  python3 -I tools/eu.py fetch --workers 4
  python3 -I tools/eu.py pages
  python3 -I tools/eu.py coverage
  python3 -I tools/eu.py capture
  python3 -I tools/eu.py build --clean --fresh-cache
  python3 -I tools/eu.py rebuild-check
  python3 -I tools/eu.py crosswalk --check
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import queue
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402  (Fetcher, USER_AGENT, errors, the removal list)
import notices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MOLECULES = ROOT / "molecules"
PRODUCTS = ROOT / "sources" / "eu-products.json"
SOURCES_EU = ROOT / "sources" / "eu"
CAPTURES = ROOT / "sources" / "eu-capture"
RAW_EU = build.RAW / "eu"          # follows env ASOF_RAW, like build.RAW
REPORTS = ROOT / "reports"
EU_PYTHON_ENV = "ASOF_EU_PYTHON"   # interpreter with the pinned PyMuPDF (CI: python3 after pip install)


def eu_python() -> Path:
    """The interpreter that runs the converter: env ASOF_EU_PYTHON (a path or a command name on PATH),
    else .venv-eu/bin/python."""
    env = os.environ.get(EU_PYTHON_ENV)
    if env:
        found = shutil.which(env) if os.sep not in env else env
        return Path(found or env)
    return ROOT / ".venv-eu" / "bin" / "python"


VENV_PYTHON = eu_python()

EMA_MEDICINES_URL = "https://www.ema.europa.eu/en/documents/report/medicines-output-medicines_json-report_en.json"
EMA_EPAR_DOCUMENTS_URL = "https://www.ema.europa.eu/en/documents/report/documents-output-epar_documents_json-report_en.json"
REGISTER = "https://ec.europa.eu/health/documents/community-register"
REGISTER_ODS_URL = f"{REGISTER}/ods/ods_products.json"
REGISTER_INDEX_URL = f"{REGISTER}/html/index_en.htm"
ALLOWED_HOSTS = frozenset({"ec.europa.eu", "www.ema.europa.eu"})
ABORT_AFTER_FAILURES = 5     # failed URLs (after retries) among the last FAILURE_WINDOW before the run stops
FAILURE_WINDOW = 20
HISTORY_IN_FORCE = "in-force"
HISTORY_CURRENT_ONLY = "current-only"
KNOWN_PRODUCTS = {  # verified by hand in the pilot: EMA number -> (EU number, h-page)
    "EMEA/H/C/002148": ("EU/1/11/691", "h691"),     # Eliquis
    "EMEA/H/C/003820": ("EU/1/15/1024", "h1024"),   # Keytruda
    "EMEA/H/C/004174": ("EU/1/17/1251", "h1251"),   # Ozempic
}
SELECTION_RULE = (
    "EMA medicines JSON rows with category Human and medicine_status Authorised. The INN field "
    "(international_non_proprietary_name_common_name) is split on ' / ', ',' and ' and ' (and on ';', which the "
    "JSON uses between the substances of a combination; this changes no match) and each part is "
    "normalised (lowercase, non-alphanumerics to spaces, spaces collapsed). A product matches a molecule when "
    "its INN set is exactly {molecule inn} or {molecule name} after the same normalisation (single substance "
    "only in v1; products whose INN set has more than one part and includes the molecule are combinations, "
    "excluded and counted). Among matches with generic = No and biosimilar = No, the reference product is the "
    "one with the earliest marketing_authorisation_date (ties: lowest EMA product number). Every other "
    "matching authorised product is recorded as an alternate; only reference products are built. "
    "Version 2 (salt-alias-v2): when the exact-INN rule finds no reference, a single-substance authorised product "
    "whose INN is the molecule's INN plus salt or hydrate words (kind salt) or the INN of its US adopted name "
    "(kind alias), with generic = No and biosimilar = No, becomes the reference (earliest "
    "marketing_authorisation_date, ties: lowest EMA product number); the other salt or alias products are "
    "alternates. Overrides: REFERENCE_OVERRIDES in tools/eu.py names the reference by hand for a few molecules "
    "(duloxetine: Cymbalta, the depression and pain product, not the earlier urinary-incontinence Yentreve; "
    "umeclidinium: Incruse Ellipta, the 2014 originator, not its 2017 duplicate Rolufta Ellipta); the products "
    "they displace become alternates.")
SELECTION_RULE_NAME = "salt-alias-v2 + overrides"
REFERENCE_OVERRIDES = {   # molecule inn -> EMA product number of the hand-picked reference (see SELECTION_RULE)
    "duloxetine": "EMEA/H/C/000572",     # Cymbalta
    "umeclidinium": "EMEA/H/C/002809",   # Incruse Ellipta
}
EU_NUMBER_RULE = (
    "Union Register open dataset (ods_products.json, Creative Commons Attribution 4.0): rows with Type Human "
    "carry EUNumber, the product page URI (html/h{n}.htm) and EMALink, the EMA EPAR page. An EMA product is "
    "mapped through its medicine_url equal to EMALink (case-insensitive, trailing slash and scheme ignored); "
    "when no EMALink matches, through an exact normalised product name, ignoring a trailing '(previously ...)' "
    "note, among Human rows (preferring Active rows; recorded as a name match). The h-page of every reference product is then fetched and its EMA number (dataSet_proc "
    "ema_number) and EU number (dataSet_product_information) are checked against the mapping.")


class EUError(build.BuildError):
    """An EU source did not have the expected shape."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- fetch layer

class _SameHostsRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the allowed hosts; any other target fails the request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname not in ALLOWED_HOSTS:
            raise urllib.error.HTTPError(newurl, code, f"redirect to a host outside the allowed list: {newurl}",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_host(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname
    if host not in ALLOWED_HOSTS:
        raise EUError(f"refusing a request to {host}: only {sorted(ALLOWED_HOSTS)} are allowed")


def looks_like(kind: str, body: bytes) -> bool:
    """False for a challenge or error page served with HTTP 200 instead of the expected file."""
    if kind == "pdf":
        return body[:5] == b"%PDF-"
    if kind == "json":
        return body.lstrip()[:1] in (b"{", b"[")
    if kind == "hpage":
        return b"dataSet_proc" in body and b"dataSet_product_information" in body
    if kind == "html":
        return b"<html" in body[:2000].lower()
    return True


class EUFetcher(build.Fetcher):
    """build.Fetcher (shared 2 requests/s slot, fixed User-Agent, request log) with stricter rules for
    the EU hosts: HTTP 403 and 429 back off like 5xx errors, a 200 answer that is not the expected
    kind of file counts as a challenge page and backs off too, redirects stay on the allowed hosts.

    Back-off is shared: a failed answer moves the next free request slot of the whole fetcher forward,
    so every worker waits, not only the one that saw it. The run stops when ``abort_after`` of the last
    ``window`` URLs failed after their retries (a success in another worker does not reset the count).
    The request log gets a sixth column, the reserved start slot (UTC, milliseconds), so the 2 requests
    per second limit can be checked from the log; the first column stays the time the answer came."""

    def __init__(self, opener=None, abort_after: int = ABORT_AFTER_FAILURES, window: int = FAILURE_WINDOW, **kw):
        if opener is None:
            opener = urllib.request.build_opener(_SameHostsRedirect()).open
        super().__init__(opener=opener, abort_after=abort_after, **kw)
        self.recent: collections.deque[bool] = collections.deque(maxlen=max(window, abort_after))

    def _once(self, url: str) -> tuple[int, bytes, str]:
        with self._lock:  # reserve the next start slot; slots are min_gap_s apart across all threads
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.min_gap_s
            self.requests += 1
        slot_utc = datetime.now(timezone.utc).timestamp() + (slot - time.monotonic())
        wait = slot - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": build.USER_AGENT})
        status, body, note = 0, b"", ""
        try:
            with self.opener(req, timeout=120) as resp:
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as err:
            status = err.code
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            note = f"{type(err).__name__}: {err}"
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            started = datetime.fromtimestamp(slot_utc, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            with self._lock, self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t{status or 'ERR'}\t{len(body)}\t{url}\t"
                         f"{note}\t{started}\n")
        return status, body, note

    def hold(self, seconds: float) -> None:
        """Move the next free request slot at least ``seconds`` ahead, for every worker."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)

    def _outcome(self, failed: bool) -> int:
        """Record one URL's result; returns the failures among the recent URLs."""
        with self._lock:
            self.recent.append(failed)
            self.consecutive_failures = self.consecutive_failures + 1 if failed else 0
            return sum(self.recent)

    def fetch(self, url: str, kind: str = "") -> bytes:
        check_host(url)
        last = ""
        for attempt in range(len(self.backoff) + 1):
            status, body, note = self._once(url)
            if status == 200 and looks_like(kind, body):
                self._outcome(False)
                return body
            if status in (404, 410):
                self._outcome(False)
                raise build.NotAvailable(f"HTTP {status} for {url}")
            if status == 200:
                last = f"HTTP 200 but not a {kind} file (challenge or error page, {len(body)} bytes)"
            elif status:
                last = f"HTTP {status}"
            else:
                last = note
            if status and status not in build.TRANSIENT and status not in (200, 403):
                self._outcome(False)
                raise build.NotAvailable(f"{last} for {url}")
            if attempt < len(self.backoff):
                self.hold(self.backoff[attempt])   # the next _once of any worker waits for this
        failures = self._outcome(True)
        if failures >= self.abort_after:
            raise build.FetchAborted(f"{failures} of the last {len(self.recent)} URLs failed after retries; "
                                     f"last: {url} ({last})")
        raise build.BuildError(f"gave up after retries: {url} ({last})")

    get = fetch


def new_fetcher(raw_dir: Path = RAW_EU, **kw) -> EUFetcher:
    return EUFetcher(log_path=raw_dir / "requests.tsv", **kw)


def blob_path(digest: str, ext: str, raw_dir: Path = RAW_EU) -> Path:
    return raw_dir / "sha256" / f"{digest}.{ext}"


def store_blob(data: bytes, ext: str, raw_dir: Path = RAW_EU) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = blob_path(digest, ext, raw_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return digest


def pinned_blob(pin: dict | None, ext: str, raw_dir: Path = RAW_EU) -> bytes | None:
    """The cached bytes of a pinned input, or None when it is not pinned or not on disk."""
    if not pin or not pin.get("sha256"):
        return None
    path = blob_path(pin["sha256"], ext, raw_dir)
    if not path.exists():
        return None
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise EUError(f"{path} does not match its pinned SHA-256")
    return data


def get_pinned(url: str, kind: str, ext: str, pin: dict | None, fetcher: EUFetcher | None,
               raw_dir: Path = RAW_EU, refresh: bool = False) -> tuple[bytes, dict]:
    """A pinned input from the raw store, or a fresh download that is stored and pinned."""
    if not refresh:
        data = pinned_blob(pin, ext, raw_dir)
        if data is not None:
            return data, pin
    if fetcher is None:
        raise EUError(f"{url} is not cached and no network access was allowed")
    data = fetcher.fetch(url, kind)
    digest = store_blob(data, ext, raw_dir)
    return data, {"url": url, "sha256": digest, "size": len(data), "fetched": utc_now()}


# ---------------------------------------------------------------- EMA medicines JSON

def norm(text: str) -> str:
    """Lowercase, non-alphanumerics to spaces, spaces collapsed."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split())


def split_inn(field: str) -> frozenset[str]:
    """The normalised substances of an EMA INN field, split on ' / ', ',', ' and ' and ';'.
    The medicines JSON writes combinations as 'abacavir;lamivudine'; splitting on ';' too changes no match
    (a single substance has no separator) but counts those products as combinations."""
    parts = re.split(r" / |,| and |;", field or "")
    return frozenset(p for p in (norm(x) for x in parts) if p)


def parse_dmy(value: str) -> str | None:
    """'18/05/2011' -> '2011-05-18'; empty -> None."""
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%d/%m/%Y").strftime("%Y-%m-%d")


def ema_dir(ema_number: str) -> str:
    """'EMEA/H/C/002148' -> 'EMEA-H-C-002148' (the folder and sources file name)."""
    return canonical_ema(ema_number).replace("/", "-")


def canonical_ema(ema_number: str) -> str:
    """The product part of an EMA number, zero-padded: 'EMEA/H/C/2148/X/4/G' -> 'EMEA/H/C/002148'."""
    m = re.match(r"\s*EMEA/H/C/0*(\d+)", ema_number or "")
    if not m:
        raise EUError(f"not an EMA human product number: {ema_number!r}")
    return f"EMEA/H/C/{int(m.group(1)):06d}"


def load_medicines(data: bytes) -> list[dict]:
    doc = json.loads(data)
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows:
        raise EUError("EMA medicines JSON has no 'data' list")
    need = {"category", "name_of_medicine", "ema_product_number", "medicine_status", "generic", "biosimilar",
            "international_non_proprietary_name_common_name", "marketing_authorisation_date", "medicine_url"}
    missing = need - set(rows[0])
    if missing:
        raise EUError(f"EMA medicines JSON rows lack {sorted(missing)}")
    return rows


def load_molecules(molecules_dir: Path = MOLECULES) -> list[dict]:
    """inn, name and the brand names of the molecule's single-ingredient US NDA/BLA products (for the
    comparability flag)."""
    mols = []
    for path in sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json"):
        mol = read_json(path)
        brands = sorted({b for u in mol.get("us") or [] if not u.get("combination") and set(u.get("marketing_categories") or []) & {"NDA", "BLA"}
                         for b in u.get("brand_names") or [] if b})
        mols.append({"inn": mol.get("inn") or path.stem, "name": mol.get("name") or "", "us_brands": brands})
    if not mols:
        raise EUError(f"no molecule files in {molecules_dir}")
    return mols


def product_summary(row: dict) -> dict:
    return {
        "name": row["name_of_medicine"],
        "ema_product_number": row["ema_product_number"],
        "inn_field": row["international_non_proprietary_name_common_name"],
        "active_substance": row.get("active_substance", ""),
        "medicine_status": row["medicine_status"],
        "generic": row["generic"],
        "biosimilar": row["biosimilar"],
        "mah": row.get("marketing_authorisation_developer_applicant_holder", ""),
        "marketing_authorisation_date": parse_dmy(row["marketing_authorisation_date"]),
        "medicine_url": row["medicine_url"],
        "revision_number": row.get("revision_number", ""),
        "last_updated_date": parse_dmy(row.get("last_updated_date", "")),
    }


# Words that name a salt, ester-free counter-ion or hydrate in an EMA INN field ("atazanavir sulfate",
# "dasatinib (anhydrous)"). Used only to label near misses; the selection rule still needs the exact INN.
SALT_WORDS = frozenset("""sulfate sulphate hydrochloride dihydrochloride hydrobromide bromide chloride benzoate maleate
    anhydrous mesilate mesylate besilate besylate tosilate tosylate fumarate hemifumarate tartrate succinate citrate
    phosphate acetate lactate malate oxalate nitrate hyclate sodium disodium potassium dipotassium calcium magnesium
    monohydrate dihydrate trihydrate sesquihydrate hemihydrate hydrate as""".split())
# US adopted names that differ from the INN used by EMA. Used only to label near misses.
INN_ALIASES = {"cyclosporine": "ciclosporin", "hydroxyurea": "hydroxycarbamide", "leuprolide": "leuprorelin",
               "cysteamine": "mercaptamine", "acetaminophen": "paracetamol", "albuterol": "salbutamol",
               "glyburide": "glibenclamide", "rifampin": "rifampicin", "dextroamphetamine": "dexamfetamine",
               "amphetamine": "amfetamine", "meperidine": "pethidine", "norepinephrine": "noradrenaline",
               "epinephrine": "adrenaline", "levalbuterol": "levosalbutamol", "lisdexamfetamine": "lisdexamfetamine",
               "methimazole": "thiamazole", "levothyroxine": "levothyroxine sodium"}
# Reference products whose EU text does not cover the use of the US innovator label, or that are a
# duplicate marketing authorisation of the originator brand. Hand-checked facts from the selection review;
# they do not change the selection rule (the lead's), only flag the crosswalk for compare_jurisdictions.
REFERENCE_CAVEATS = {
    "EMEA/H/C/002618": ("no", "Nexium Control is the non-prescription heartburn product; the US innovator label is "
                              "prescription Nexium"),
    "EMEA/H/C/001013": ("no", "Pantozol Control is the non-prescription reflux product; the US innovator label is "
                              "prescription Protonix"),
    "EMEA/H/C/000545": ("no", "Yentreve is the stress urinary incontinence product; the depression and pain product "
                              "matching US Cymbalta is the alternate Cymbalta EMEA/H/C/000572"),
    "EMEA/H/C/000175": ("yes", "Iscover is a duplicate marketing authorisation of Plavix (EMEA/H/C/000174, one day "
                               "later), same holder group; text normally identical"),
    "EMEA/H/C/000310": ("yes", "Azomyr is a duplicate marketing authorisation of Aerius (EMEA/H/C/000313, same day); "
                               "text normally identical"),
    "EMEA/H/C/004654": ("yes", "Rolufta Ellipta is a duplicate marketing authorisation of Incruse Ellipta "
                               "(EMEA/H/C/002809, 2014, INN field 'umeclidinium bromide', not matched by the exact-INN rule); "
                               "the EU record starts in 2017, not 2014"),
    "EMEA/H/C/000374": ("no", "Protopic is the topical ointment; the US innovator label is oral Prograf"),
    "EMEA/H/C/004655": ("no", "Jorveza is the orodispersible tablet for eosinophilic oesophagitis"),
    "EMEA/H/C/002642": ("no", "Mirvaso is the rosacea gel; the US innovator label is the glaucoma eye drop"),
    "EMEA/H/C/000549": ("no", "Pedea is the intravenous product for patent ductus arteriosus in neonates"),
    "EMEA/H/C/002621": ("no", "Hemangiol is the oral solution for infantile haemangioma"),
    "EMEA/H/C/000089": ("no", "Caelyx is pegylated liposomal doxorubicin (US Doxil), not conventional doxorubicin"),
    "EMEA/H/C/000778": ("no", "Abraxane is albumin-bound paclitaxel, not conventional paclitaxel"),
    "EMEA/H/C/002789": ("no", "Quinsair is the nebulised solution for cystic fibrosis lung infection"),
    "EMEA/H/C/004587": ("no", "Nuceiva is the cosmetic (glabellar lines) product"),
    "EMEA/H/C/006617": ("no", "Bopediat is a paediatric formulation"),
    "EMEA/H/C/005406": ("no", "Okedi is a long-acting injectable; the US innovator label is oral Risperdal"),
    "EMEA/H/C/001140": ("no", "Ozurdex is the intravitreal implant"),
    "EMEA/H/C/005933": ("no", "Catiolanze is a preservative-free cationic emulsion; check against the US label used"),
    "EMEA/H/C/004786": ("no", "Inbrija is inhaled levodopa for off episodes, not oral levodopa"),
}
COMPARABILITY_RULE = (
    "comparable_to_us is 'no' or 'yes' with a hand-written note for the products in REFERENCE_CAVEATS (an OTC, "
    "other-indication or other-formulation product, or a duplicate marketing authorisation of the originator), "
    "'yes' when the first word of the EU product name equals the first word of a US NDA/BLA brand name of the "
    "molecule, and 'unknown' otherwise. It does not change the selection; compare_jurisdictions should show a "
    "caveat when it is not 'yes'.")


def _ref_key(p: dict) -> tuple:
    return (p["marketing_authorisation_date"] or "9999-99-99", canonical_ema(p["ema_product_number"]))


def select_products(rows: list[dict], molecules: list[dict],
                    overrides: dict[str, str] | None = None) -> dict[str, dict]:
    """Per molecule inn: reference product (with ``selected_by``: exact-inn, salt-alias-v2 or override),
    alternates, combinations and the no-reference reason. See SELECTION_RULE."""
    overrides = REFERENCE_OVERRIDES if overrides is None else overrides
    human = [r for r in rows if r.get("category") == "Human"]
    authorised = [r for r in human if r.get("medicine_status") == "Authorised"]
    sets = [(r, split_inn(r["international_non_proprietary_name_common_name"])) for r in authorised]
    other_sets = [(r, split_inn(r["international_non_proprietary_name_common_name"]))
                  for r in human if r.get("medicine_status") != "Authorised"]
    by_number = {canonical_ema(r["ema_product_number"]): r for r in authorised}
    out: dict[str, dict] = {}
    for mol in molecules:
        keys = {k for k in (norm(mol["inn"]), norm(mol["name"])) if k}
        aliases = {norm(INN_ALIASES[k]) for k in keys if k in INN_ALIASES} - keys
        matches = [product_summary(r) for r, s in sets if len(s) == 1 and s <= keys]
        combos = [product_summary(r) for r, s in sets if len(s) > 1 and s & keys]
        other = [product_summary(r) for r, s in other_sets if len(s) == 1 and s <= keys]
        near, near_rows = [], []
        for r, s in sets:
            if len(s) != 1 or s <= keys:
                continue
            kind = near_miss_kind(next(iter(s)), keys, aliases)
            if kind:
                near.append({"name": r["name_of_medicine"], "ema_product_number": r["ema_product_number"],
                             "inn_field": r["international_non_proprietary_name_common_name"], "kind": kind,
                             "generic": r["generic"], "biosimilar": r["biosimilar"],
                             "marketing_authorisation_date": parse_dmy(r["marketing_authorisation_date"])})
                if kind in ("salt", "alias"):
                    near_rows.append((r, kind))
        near.sort(key=lambda n: n["ema_product_number"])
        candidates = sorted((p for p in matches if p["generic"] == "No" and p["biosimilar"] == "No"), key=_ref_key)
        reference, pool, selected_by = (candidates[0] if candidates else None), list(matches), "exact-inn"
        if reference is None:
            salt_alias = [dict(product_summary(r), near_miss_kind=kind) for r, kind in near_rows]
            v2 = sorted((p for p in salt_alias if p["generic"] == "No" and p["biosimilar"] == "No"), key=_ref_key)
            if v2:
                reference, selected_by = v2[0], "salt-alias-v2"
                pool = matches + salt_alias
        forced = overrides.get(mol["inn"])
        if forced:
            row = by_number.get(canonical_ema(forced))
            if row is None:
                raise EUError(f"REFERENCE_OVERRIDES names {forced} for {mol['inn']}, which is not an authorised "
                              "human product in the EMA medicines JSON")
            reference, selected_by = product_summary(row), "override"
            pool = matches + [dict(product_summary(r), near_miss_kind=kind) for r, kind in near_rows]
        if reference is not None:
            reference["selected_by"] = selected_by
        ref_number = canonical_ema(reference["ema_product_number"]) if reference else None
        alternates = sorted((p for p in pool if canonical_ema(p["ema_product_number"]) != ref_number), key=_ref_key)
        reason = None
        if reference is None:
            if matches:
                reason = "only generic or biosimilar central products under this exact INN"
            elif combos:
                reason = "only combination central products under this exact INN (combinations are not built)"
            elif other:
                reason = "no authorised central product under this exact INN (only " + ", ".join(
                    sorted({p["medicine_status"].lower() for p in other})) + " ones)"
            else:
                reason = "no central product with this exact INN in the EMA medicines JSON"
        if reference is not None:
            reference.update(comparability(reference, mol.get("us_brands") or []))
        out[mol["inn"]] = {"name": mol["name"], "reference": reference, "alternates": alternates,
                           "combinations": sorted(combos, key=_ref_key), "not_authorised": len(other),
                           "near_misses_not_matched": [n for n in near if canonical_ema(n["ema_product_number"])
                                                       != ref_number and not any(
                                                           canonical_ema(n["ema_product_number"]) ==
                                                           canonical_ema(a["ema_product_number"]) for a in alternates)],
                           "no_reference_reason": reason}
    return out


def near_miss_kind(substance: str, keys: set[str], aliases: set[str]) -> str | None:
    """How a single-substance INN that is not the molecule's exact INN relates to it: "salt" (the INN plus
    salt or hydrate words), "alias" (the INN of a US adopted name, e.g. ciclosporin for cyclosporine,
    optionally with salt words), "variant" (the INN plus another word, e.g. an ester or conjugate), or None."""
    bare = " ".join(w for w in substance.split() if w not in SALT_WORDS)
    if bare in keys:
        return "salt"
    if substance in aliases or bare in aliases:
        return "alias"
    if any(substance.startswith(k + " ") or substance.endswith(" " + k) for k in keys):
        return "variant"
    return None


def comparability(ref: dict, us_brands: list[str]) -> dict:
    """comparable_to_us and comparability_note of a reference product (see COMPARABILITY_RULE)."""
    caveat = REFERENCE_CAVEATS.get(canonical_ema(ref["ema_product_number"]))
    if caveat:
        return {"comparable_to_us": caveat[0], "comparability_note": caveat[1]}
    first = (name_key(ref["name"]).split() or [""])[0]
    hits = sorted(b for b in us_brands if (norm(b).split() or [""])[0] == first)
    if first and hits:
        return {"comparable_to_us": "yes", "comparability_note": f"EU name matches the US NDA/BLA brand {hits[0]}"}
    return {"comparable_to_us": "unknown",
            "comparability_note": "EU name matches no US NDA/BLA brand of the molecule; check the indication and form"}


# ---------------------------------------------------------------- EU number source

def load_register_ods(data: bytes) -> list[dict]:
    doc = json.loads(data)
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not rows or "EUNumber" not in rows[0]:
        raise EUError("Union Register open dataset has no 'data' list with EUNumber")
    return rows


def _url_key(url: str) -> str:
    parts = urllib.parse.urlsplit((url or "").strip().lower())
    return (parts.netloc + parts.path).rstrip("/")


def _en_text(values) -> str:
    for v in values or []:
        if v.get("LanguageCode") == "EN":
            return v.get("Text") or ""
    return ""


def register_entry(row: dict) -> dict:
    uri = row.get("URI") or ""
    m = re.search(r"/html/(h\d+)\.htm$", uri)
    return {"eu_number": row.get("EUNumber"), "h_page": m.group(1) if m else None,
            "h_page_url": f"{REGISTER}/html/{m.group(1)}.htm" if m else None,
            "register_name": _en_text(row.get("Name")), "register_status": row.get("Status"),
            "register_inn": _en_text(row.get("ActiveSubstance")),
            "register_mah": (row.get("MAH") or {}).get("Name"),
            "authorisation_date": row.get("AuthorisationDate"),
            "last_decision_date": row.get("LastDecisionDate"),
            "total_decisions": row.get("TotalDecisions"),
            "current_annex_en": next((a["URI"] for a in row.get("CurrentAnnexLink") or []
                                      if a.get("LanguageCode") == "EN"), None)}


def register_index(rows: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Human rows of the open dataset keyed by EPAR URL and by normalised product name."""
    by_url: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("Type") != "Human" or not row.get("EUNumber"):
            continue
        entry = register_entry(row)
        if row.get("EMALink"):
            by_url.setdefault(_url_key(row["EMALink"]), []).append(entry)
        by_name.setdefault(name_key(entry["register_name"]), []).append(entry)
    return by_url, by_name


def map_eu_number(product: dict, index: tuple[dict, dict]) -> tuple[dict | None, str]:
    """(register entry, how it matched) for an EMA product summary; (None, reason) when unmapped."""
    by_url, by_name = index
    hits = by_url.get(_url_key(product["medicine_url"]), [])
    if len(hits) == 1:
        return hits[0], "epar-url"
    if len(hits) > 1:
        active = [h for h in hits if h["register_status"] == "Active"]
        if len(active) == 1:
            return active[0], "epar-url (only active row of several)"
        return None, f"EPAR URL matches {len(hits)} register rows: {[h['eu_number'] for h in hits]}"
    hits = by_name.get(name_key(product["name"]), [])
    active = [h for h in hits if h["register_status"] == "Active"] or hits
    if len(active) == 1:
        return active[0], "name"
    if active:
        return None, f"name matches {len(active)} register rows: {[h['eu_number'] for h in active]}"
    return None, "no Union Register row with this EPAR URL or product name"


def h_page_of(eu_number: str) -> str:
    """'EU/1/15/1024' -> 'h1024'; the digits are kept as written ('EU/1/96/005' -> 'h005')."""
    m = re.fullmatch(r"EU/1/\d{2}/(\d+)", eu_number or "")
    if not m:
        raise EUError(f"not a human EU number: {eu_number!r}")
    return f"h{m.group(1)}"


def name_key(name: str) -> str:
    """A product name without its '(previously ...)' note, normalised."""
    return norm(re.sub(r"\s*\(previously\b.*\)\s*$", "", name or "", flags=re.I))


# ---------------------------------------------------------------- discover

def cmd_discover(args: argparse.Namespace) -> int:
    old = read_json(PRODUCTS) if PRODUCTS.exists() else {}
    pins = old.get("inputs", {})
    fetcher = None if args.offline else new_fetcher()
    started = time.monotonic()
    med_bytes, med_pin = get_pinned(EMA_MEDICINES_URL, "json", "json", pins.get("ema_medicines_json"), fetcher,
                                    refresh=args.refresh)
    ods_bytes, ods_pin = get_pinned(REGISTER_ODS_URL, "json", "json", pins.get("union_register_ods"), fetcher,
                                    refresh=args.refresh)
    rows = load_medicines(med_bytes)
    index = register_index(load_register_ods(ods_bytes))
    selection = select_products(rows, load_molecules(Path(args.molecules)))
    problems: list[str] = []
    for inn, entry in selection.items():
        for p in [entry["reference"], *entry["alternates"]]:
            if p is None:
                continue
            reg, how = map_eu_number(p, index)
            p["dir"] = ema_dir(p["ema_product_number"])
            if reg is None:
                p.update({"eu_number": None, "h_page": None, "eu_number_match": None, "eu_number_problem": how})
                problems.append(f"{inn} {p['ema_product_number']} {p['name']}: {how}")
            else:
                p.update(reg)
                p["eu_number_match"] = how
                if reg["h_page"] != h_page_of(reg["eu_number"]):
                    problems.append(f"{inn} {p['ema_product_number']}: h-page {reg['h_page']} does not follow "
                                    f"EU number {reg['eu_number']}")
        ref = entry["reference"]
        if ref is not None:
            prev = ((old.get("molecules") or {}).get(inn) or {}).get("reference") or {}
            keep = prev.get("ema_product_number") == ref["ema_product_number"]
            for k in ("history_type", "record_from", "record_to", "history_note"):
                ref[k] = prev.get(k) if keep else None
            ref["history_type"] = ref["history_type"] or "pending"
        for p in entry["alternates"]:
            p["history_type"] = "not-built"
    verification = verify_known(rows, index)
    refs = [e["reference"] for e in selection.values() if e["reference"]]
    reasons: dict[str, int] = {}
    for e in selection.values():
        if e["no_reference_reason"]:
            base = e["no_reference_reason"].split("; ", 1)[0]
            reasons[base] = reasons.get(base, 0) + 1
    human_auth = [r for r in rows if r.get("category") == "Human" and r.get("medicine_status") == "Authorised"]
    doc = {
        "generated": utc_now(),
        "rule": SELECTION_RULE_NAME,
        "rule_text": SELECTION_RULE,
        "reference_overrides": dict(sorted(REFERENCE_OVERRIDES.items())),
        "eu_number_rule": EU_NUMBER_RULE,
        "comparability_rule": COMPARABILITY_RULE,
        "inputs": {"ema_medicines_json": med_pin, "union_register_ods": dict(ods_pin, licence="CC BY 4.0")},
        "counts": {
            "ema_rows": len(rows), "ema_human_authorised": len(human_auth),
            "ema_human_authorised_without_inn": sum(1 for r in human_auth
                                                    if not split_inn(r["international_non_proprietary_name_common_name"])),
            "molecules": len(selection), "molecules_with_reference": len(refs),
            "molecules_without_reference": len(selection) - len(refs), "no_reference_reasons": reasons,
            "alternates": sum(len(e["alternates"]) for e in selection.values()),
            "combination_products_excluded": len({c["ema_product_number"] for e in selection.values()
                                                  for c in e["combinations"]}),
            "molecules_with_combination_products": sum(1 for e in selection.values() if e["combinations"]),
            "molecules_with_near_misses_not_matched": sum(1 for e in selection.values()
                                                          if e["near_misses_not_matched"]),
            "references_by_rule": dict(sorted(collections.Counter(
                e["reference"]["selected_by"] for e in selection.values() if e["reference"]).items())),
            "references_comparable_to_us": {v: sum(1 for r in refs if r.get("comparable_to_us") == v)
                                            for v in ("yes", "no", "unknown")},
            "references_mapped_to_eu_number": sum(1 for r in refs if r.get("eu_number")),
            "references_mapped_by_name": sum(1 for r in refs if r.get("eu_number_match") == "name"),
        },
        "verification": verification,
        "problems": problems,
        "molecules": selection,
    }
    write_json(PRODUCTS, doc)
    c = doc["counts"]
    print(f"{c['molecules']} molecules: {c['molecules_with_reference']} with a reference product "
          f"({c['references_mapped_to_eu_number']} mapped to an EU number), {c['alternates']} alternates, "
          f"{c['combination_products_excluded']} combination products excluded; "
          f"{(fetcher.requests if fetcher else 0)} requests, {time.monotonic() - started:.1f} s")
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {reason}")
    for v in verification:
        print(f"  verify {v['ema_product_number']}: {v['result']}")
    for p in problems:
        print(f"  PROBLEM {p}")
    print(f"written {PRODUCTS}")
    return 0 if all(v["result"] == "ok" for v in verification) else 1


def verify_known(rows: list[dict], index: tuple[dict, dict]) -> list[dict]:
    """The EU-number mapping checked on the three products verified by hand in the pilot."""
    out = []
    by_number = {r["ema_product_number"]: r for r in rows}
    for ema, (eu, h) in sorted(KNOWN_PRODUCTS.items()):
        row = by_number.get(ema)
        if row is None:
            out.append({"ema_product_number": ema, "expected": eu, "result": "missing from EMA JSON"})
            continue
        reg, how = map_eu_number(product_summary(row), index)
        got = (reg or {}).get("eu_number"), (reg or {}).get("h_page")
        out.append({"ema_product_number": ema, "name": row["name_of_medicine"], "expected": [eu, h],
                    "got": list(got), "match": how, "result": "ok" if got == (eu, h) else "MISMATCH"})
    return out


# ---------------------------------------------------------------- Union Register product page

def js_array(text: str, var: str) -> list:
    """The JSON array assigned to ``var`` in the page script (control characters allowed in strings)."""
    m = re.search(r"\bvar\s+" + re.escape(var) + r"\s*=\s*", text)
    if not m:
        raise EUError(f"no '{var}' in the page")
    value, _ = json.JSONDecoder(strict=False).raw_decode(text, m.end())
    if not isinstance(value, list):
        raise EUError(f"'{var}' is not a JSON array")
    return value


def parse_hpage(data: bytes) -> dict:
    """Product facts and the procedure list of a Union Register product page."""
    text = data.decode("utf-8", errors="replace")
    info = js_array(text, "dataSet_product_information")
    procs = js_array(text, "dataSet_proc")
    facts = {i.get("type"): i for i in info if isinstance(i, dict)}
    name = facts.get("name") or {}
    regen = re.search(r"Regeneration time:\s*([0-9/: ]+)", text)
    return {"name": name.get("value"), "eu_number": (facts.get("eu_num") or {}).get("value"),
            "inn": (facts.get("inn") or {}).get("value"), "mah": (facts.get("mah") or {}).get("value"),
            "status": (name.get("meta") or {}).get("status_name"),
            "regenerated": regen.group(1).strip() if regen else None,
            "product_information": info, "procedures": procs}


def languages(files) -> list[str]:
    return sorted({f.get("code") for f in files or [] if isinstance(f, dict) and f.get("code")})


def annex_url(procedure_id: str, decision_date: str, lang: str = "en", kind: str = "anx") -> str:
    """community-register/{yyyy}/{yyyymmdd}{id}/{kind}_{id}_{lang}.pdf (kind 'anx' or 'dec')."""
    if not re.fullmatch(r"\d+", str(procedure_id)) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", decision_date or ""):
        raise EUError(f"cannot build a file URL from procedure {procedure_id!r} and date {decision_date!r}")
    ymd = decision_date.replace("-", "")
    return f"{REGISTER}/{ymd[:4]}/{ymd}{procedure_id}/{kind}_{procedure_id}_{lang}.pdf"


def plan_annexes(procedures: list[dict]) -> list[dict]:
    """One row per procedure that lists annex files, in decision-date order (ties: procedure id):
    the English annex URL to fetch, or the reason it is not fetched."""
    rows = []
    for p in procedures:
        langs = languages(p.get("files_anx"))
        if not langs:
            continue
        dec = p.get("decision") or {}
        row = {"procedure_id": str(p.get("id")), "type": p.get("type"), "ema_number": p.get("ema_number"),
               "decision_number": dec.get("number"), "decision_date": dec.get("date"), "closed": p.get("closed"),
               "annex_languages": langs, "decision_languages": languages(p.get("files_dec")),
               "remark": p.get("remark")}
        if "en" not in langs:
            corr = "corrigendum" in (p.get("type") or "").lower() or "corrig" in (p.get("remark") or "").lower()
            row.update(status="logged", reason=("corrigendum without an English annex file" if corr else
                                                "no English annex file") + f" (annex languages: {','.join(langs)})")
        elif not dec.get("date"):
            row.update(status="logged", reason="English annex listed, but the procedure has no decision date")
        else:
            row.update(status="pending", url=annex_url(row["procedure_id"], dec["date"]))
        rows.append(row)
    return sorted(rows, key=lambda r: (r["decision_date"] or "9999", int(r["procedure_id"])))


def page_ema_numbers(procedures: list[dict]) -> set[str]:
    """Product EMA numbers named by the procedures (PSUSA and referral numbers are skipped)."""
    return {canonical_ema(p["ema_number"]) for p in procedures
            if re.match(r"\s*EMEA/H/C/\d", p.get("ema_number") or "")}


def procedure_counts(procedures: list[dict]) -> dict:
    with_dec = [p for p in procedures if (p.get("decision") or {}).get("date")]
    carried = [p for p in procedures if not (p.get("decision") or {}).get("date")
               and (p.get("updated_by") or {}).get("date")]
    return {"procedures": len(procedures), "with_decision": len(with_dec),
            "carried_by_later_decision": len(carried),
            "with_annex_files": sum(1 for p in procedures if languages(p.get("files_anx"))),
            "decisions_without_annex_files": sum(1 for p in with_dec if not languages(p.get("files_anx")))}


# ---------------------------------------------------------------- fetch

def load_products(path: Path = PRODUCTS) -> dict:
    if not path.exists():
        raise EUError(f"{path} is missing; run tools/eu.py discover first")
    return read_json(path)


def reference_products(products: dict) -> list[tuple[str, dict]]:
    return sorted(((inn, e["reference"]) for inn, e in products["molecules"].items() if e.get("reference")),
                  key=lambda kv: kv[1]["dir"])


def active_reference_products(products: dict, removed: set[str] | None = None) -> list[tuple[str, dict]]:
    """reference_products without the product folders removed on request (default: the EU ids of
    sources/removed.json). Every build, append, fetch and capture uses this list, so a removed product is
    neither requested nor committed again."""
    removed = build.removed_ids("eu") if removed is None else set(removed)
    return [(inn, ref) for inn, ref in reference_products(products) if ref["dir"] not in removed]


FETCH_RESULT_FIELDS = ("status", "reason", "sha256", "size", "pages", "text_chars", "fetched", "fetched_url",
                       "pdf_check", "added", "build_skip", "first_failed")   # kept when the h-page is read again
RETRY_DAYS = 30   # the daily path retries an annex it logged as unavailable for this many days
PIN_CHANGED = "the annex downloaded again differs from its pinned SHA-256; the pin is kept"


def fetch_product(inn: str, ref: dict, fetcher: EUFetcher | None, sources_dir: Path = SOURCES_EU,
                  raw_dir: Path = RAW_EU, retry_logged: bool = False, refresh_page: bool = False,
                  added: str | None = None, trust_pins: bool = False) -> list[str]:
    """Fetch one reference product's h-page and English annexes not yet pinned. Returns new problems;
    every problem is also persisted in sources/eu/<dir>.json.

    ``added`` (the daily job's batch date) is written into every annex pinned by this call, so
    ``build --append`` commits it after HEAD. An annex the daily path cannot download (HTTP 404 or 410) gets
    status logged-http and first_failed (the run date, kept from the first failure); with ``retry_logged`` the
    daily path asks for it again on each run while first_failed is within RETRY_DAYS of the run date, and never
    for a logged-http annex without first_failed (logged by a full fetch). ``trust_pins`` (the daily job, which has no raw store): a
    pinned annex counts as fetched without its PDF on disk, and a re-read h-page whose procedures equal the
    pinned ones leaves the sources file as it is (the page changes daily by its regeneration time only).
    Without it, a pinned annex whose PDF is missing is downloaded again and kept only when its SHA-256 is
    the pinned one (the pin is in the committed history)."""
    path = sources_dir / f"{ref['dir']}.json"
    src = read_json(path) if path.exists() else {}
    if not ref.get("h_page_url"):
        src.update({"ema_product_number": ref["ema_product_number"], "inn": inn, "name": ref["name"],
                    "eu_number": None, "annexes": [], "problems": [f"not mapped to an EU number: "
                                                                    f"{ref.get('eu_number_problem')}"]})
        write_json(path, src)
        return src["problems"]
    page, pin = get_pinned(ref["h_page_url"], "hpage", "htm", src.get("h_page"), fetcher, raw_dir,
                           refresh=refresh_page)
    parsed = parse_hpage(page)
    if (trust_pins and src.get("annexes") is not None and parsed["procedures"] == src.get("procedures")
            and parsed["eu_number"] == ref["eu_number"]):
        old_problems = list(src.get("problems") or [])
        return fetch_annexes(ref, src, path, fetcher, raw_dir, old_problems, retry_logged, added, trust_pins,
                             write_first=False, report_from=len(old_problems))
    problems = []
    if parsed["eu_number"] != ref["eu_number"]:
        problems.append(f"h-page EU number {parsed['eu_number']} differs from the mapping {ref['eu_number']}")
    page_ema = page_ema_numbers(parsed["procedures"])
    want = canonical_ema(ref["ema_product_number"])
    if page_ema and want not in page_ema:
        problems.append(f"h-page procedures name EMA numbers {sorted(page_ema)}, not {want}")
    old = {a["procedure_id"]: a for a in src.get("annexes", [])}
    annexes = []
    for row in plan_annexes(parsed["procedures"]):
        prev = old.get(row["procedure_id"])
        if prev and prev.get("url") == row.get("url") and prev.get("status") in ("fetched", "duplicate",
                                                                                  "logged-http"):
            row.update({k: prev[k] for k in FETCH_RESULT_FIELDS if k in prev})
        annexes.append(row)
    src.update({"ema_product_number": ref["ema_product_number"], "dir": ref["dir"], "inn": inn,
                "name": ref["name"], "eu_number": ref["eu_number"], "h_page": pin,
                "register": {k: parsed[k] for k in ("name", "eu_number", "inn", "mah", "status", "regenerated")},
                "product_information": parsed["product_information"], "procedures": parsed["procedures"],
                "procedure_counts": procedure_counts(parsed["procedures"]), "annexes": annexes,
                "problems": problems})
    return fetch_annexes(ref, src, path, fetcher, raw_dir, problems, retry_logged, added, trust_pins)


def fetch_annexes(ref: dict, src: dict, path: Path, fetcher: EUFetcher | None, raw_dir: Path, problems: list[str],
                  retry_logged: bool, added: str | None, trust_pins: bool, write_first: bool = True,
                  report_from: int = 0) -> list[str]:
    """The annex loop of fetch_product over src["annexes"] (see there). ``problems`` is what the sources file
    keeps; the return value is problems[report_from:], the ones this call found."""
    annexes = src.get("annexes") or []
    if write_first:
        write_json(path, src)
    if fetcher is None:
        return problems[report_from:]
    seen_sha: dict[str, str] = {}
    for a in annexes:
        if a.get("status") in ("fetched", "duplicate") and a.get("sha256"):
            if trust_pins or blob_path(a["sha256"], "pdf", raw_dir).exists():
                seen_sha.setdefault(a["sha256"], a["procedure_id"])
                continue
            try:   # recovery of a missing raw file: same URL, same bytes, or the pin stays and a problem is logged
                data = fetcher.fetch(a.get("fetched_url") or a["url"], "pdf")
            except build.FetchAborted:
                raise
            except build.BuildError as err:
                problems.append(f"{ref['dir']} procedure {a['procedure_id']}: pinned annex not downloaded again: {err}")
                continue
            if hashlib.sha256(data).hexdigest() != a["sha256"]:
                problems.append(f"{ref['dir']} procedure {a['procedure_id']}: {PIN_CHANGED}")
            else:
                store_blob(data, "pdf", raw_dir)
                seen_sha.setdefault(a["sha256"], a["procedure_id"])
            continue
        if a["status"] == "logged" or (a["status"] == "logged-http" and not retry_now(a, retry_logged, added)):
            continue
        urls = [a["url"]]
        if a.get("closed") and a["closed"] != a["decision_date"]:
            urls.append(annex_url(a["procedure_id"], a["closed"]))
        data, got, errors, transient = None, None, [], False
        for url in urls:
            try:
                data, got = fetcher.fetch(url, "pdf"), url
                break
            except build.NotAvailable as err:
                errors.append(str(err))
            except build.FetchAborted:
                write_json(path, src)
                raise
            except build.BuildError as err:  # gave up after retries: stays pending for the next run
                errors.append(str(err))
                transient = True
                break
        if data is None and transient:
            a.update(status="pending", reason=f"not fetched yet: {'; '.join(errors)}")
            problems.append(f"{ref['dir']} procedure {a['procedure_id']}: {a['reason']}")
        elif data is None:
            a.update(status="logged-http", reason="; ".join(errors))
            if added:
                a.setdefault("first_failed", added.split(".")[0])
            msg = f"{ref['dir']} procedure {a['procedure_id']}: {a['reason']}"
            if msg not in problems:      # a daily retry that fails again is not a new problem
                problems.append(msg)
        else:
            digest = store_blob(data, "pdf", raw_dir)
            a.update(sha256=digest, size=len(data), fetched=utc_now(), fetched_url=got, reason=None)
            a.pop("first_failed", None)
            if added:
                a["added"] = added
            a.pop("pages", None)
            a.pop("text_chars", None)
            if digest in seen_sha:
                a.update(status="duplicate", reason=f"identical file to the annex of procedure {seen_sha[digest]}")
            else:
                a["status"] = "fetched"
                seen_sha[digest] = a["procedure_id"]
            if got != a["url"]:
                problems.append(f"{ref['dir']} procedure {a['procedure_id']}: annex found under the closed-date "
                                f"folder {got}, not {a['url']}")
        src["problems"] = problems
        write_json(path, src)  # after every file, so an interrupted run resumes
    return problems[report_from:]


def retry_now(annex: dict, retry_logged: bool, added: str | None) -> bool:
    """Whether a logged-http annex is requested again. Without ``added`` (eu.py fetch --retry-logged) every one
    is; on the daily path only one with first_failed within RETRY_DAYS of the run date."""
    if not retry_logged:
        return False
    if not added:
        return True
    first = annex.get("first_failed")
    if not first:
        return False
    run_day = date.fromisoformat(added.split(".")[0])
    return (run_day - date.fromisoformat(first)).days <= RETRY_DAYS


def record_run(kind: str, started: str, seconds: float, requests: int, extra: dict | None = None,
               raw_dir: Path = RAW_EU) -> None:
    """Append the finished line of a run to raw/eu/runs.jsonl (written from a finally block, so a run
    stopped by an error or Ctrl-C is recorded too, with "completed": false)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    with (raw_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"command": kind, "event": "finished", "started": started, "seconds": round(seconds, 1),
                             "requests": requests, **(extra or {})}, sort_keys=True) + "\n")


def record_start(kind: str, started: str, raw_dir: Path = RAW_EU) -> None:
    """Append the started line of a run to raw/eu/runs.jsonl, before its first request."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    with (raw_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"command": kind, "event": "started", "started": started}, sort_keys=True) + "\n")


def cmd_fetch(args: argparse.Namespace) -> int:
    products = load_products()
    refs = active_reference_products(products)
    if args.only:
        refs = [(i, r) for i, r in refs if r["dir"] in args.only or i in args.only]
    if args.limit:
        refs = refs[: args.limit]
    fetcher = None if args.offline else new_fetcher()
    stop = threading.Event()
    started_at, started, n_problems, done = utc_now(), time.monotonic(), 0, 0
    record_start("fetch", started_at)

    def work(inn: str, ref: dict) -> list[str]:
        if stop.is_set():
            return []
        try:
            return fetch_product(inn, ref, fetcher, retry_logged=args.retry_logged, refresh_page=args.refresh,
                                 added=args.append_batch, trust_pins=args.trust_pins)
        except build.FetchAborted:
            raise
        except (build.BuildError, ValueError) as err:
            path = SOURCES_EU / f"{ref['dir']}.json"
            src = read_json(path) if path.exists() else {"ema_product_number": ref["ema_product_number"],
                                                          "dir": ref["dir"], "inn": inn, "annexes": []}
            src["problems"] = [*src.get("problems", []), f"product page failed: {err}"]
            write_json(path, src)
            return [f"{ref['dir']}: product page failed: {err}"]

    code, completed = 0, False
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(work, inn, ref): (inn, ref) for inn, ref in refs}
            for fut in as_completed(futures):
                inn, ref = futures[fut]
                try:
                    problems = fut.result()
                except build.FetchAborted as err:
                    stop.set()
                    for other in futures:
                        other.cancel()
                    print(f"ABORTED: {err}. Rerun to resume.", file=sys.stderr, flush=True)
                    code = 3
                    break
                done += 1
                n_problems += len(problems)
                print(f"[{done}/{len(refs)}] {inn} {ref['dir']}: {len(problems)} problems; "
                      f"{fetcher.requests if fetcher else 0} requests, {(time.monotonic() - started) / 60:.1f} min",
                      flush=True)
                for p in problems:
                    print(f"  PROBLEM {p}", flush=True)
        completed = True
    finally:
        stop.set()
        record_run("fetch", started_at, time.monotonic() - started, fetcher.requests if fetcher else 0,
                   {"products": done, "problems": n_problems, "aborted": code == 3, "completed": completed})
    update_history_types()
    print(f"done: {done}/{len(refs)} products, {fetcher.requests if fetcher else 0} requests, "
          f"{n_problems} problems logged", flush=True)
    return code


def update_history_types(products_path: Path = PRODUCTS, sources_dir: Path = SOURCES_EU) -> None:
    """Set history_type, record_from and record_to of every reference product from its fetched annexes:
    in-force when at least one English annex is pinned (record from the first annex's decision date to the
    day the product page was read), current-only otherwise (dates set by the first capture). A product
    already built (build_versions set) is left alone: after a build, tools/eu.py build is the only writer
    of these fields, because it knows which annexes were committed."""
    products = load_products(products_path)
    for inn, entry in products["molecules"].items():
        ref = entry.get("reference")
        if not ref or ref.get("build_versions") is not None:
            continue   # the build wrote these fields from what it committed; fetch leaves them alone
        path = sources_dir / f"{ref['dir']}.json"
        if not path.exists():
            continue
        src = read_json(path)
        fetched = [a for a in src.get("annexes", []) if a.get("status") == "fetched"]
        if fetched:
            ref.update(history_type=HISTORY_IN_FORCE, record_from=fetched[0]["decision_date"],
                       record_to=(src.get("h_page") or {}).get("fetched", "")[:10] or None,
                       history_note=f"{len(fetched)} English annexes pinned from the Union Register")
        elif ref.get("history_type") != HISTORY_CURRENT_ONLY:
            ref.update(history_type=HISTORY_CURRENT_ONLY, record_from=None, record_to=None,
                       history_note="no English Union Register annex fetched; text from the EMA "
                                    "product-information PDF captured forward")
    write_json(products_path, products)


# ---------------------------------------------------------------- pages (PyMuPDF via .venv-eu)

PAGES_SCRIPT = r"""
import json, sys
import pymupdf
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)
out = {}
for path in json.load(sys.stdin):
    pymupdf.TOOLS.reset_mupdf_warnings()
    try:
        with pymupdf.open(path) as doc:
            chars = sum(len(doc[i].get_text().strip()) for i in range(min(3, doc.page_count)))
            out[path] = {"pages": doc.page_count, "text_chars": chars, "error": None}
    except Exception as err:
        out[path] = {"pages": None, "text_chars": None, "error": f"{type(err).__name__}: {err}"}
    out[path]["mupdf_messages"] = sorted(set(m for m in pymupdf.TOOLS.mupdf_warnings().splitlines() if m.strip()))
sys.stdout.write("\n" + RESULT_MARKER + json.dumps({"pymupdf": pymupdf.__version__, "results": out}) + "\n")
"""
PAGES_RESULT_MARKER = "EU-PAGES-RESULT "


def pdf_pages(paths: list[str], python: Path = VENV_PYTHON) -> tuple[str, dict]:
    """(PyMuPDF version, {path: {pages, text_chars (first 3 pages), error}}) from the pinned .venv-eu."""
    if not python.exists():
        raise EUError(f"{python} is missing; see tools/requirements-eu.txt")
    script = f"RESULT_MARKER = {PAGES_RESULT_MARKER!r}\n" + PAGES_SCRIPT
    proc = subprocess.run([str(python), "-I", "-c", script], input=json.dumps(paths),
                          capture_output=True, text=True, cwd=str(ROOT))
    if proc.returncode != 0:
        raise EUError(f"PyMuPDF page count failed: {proc.stderr.strip()[-500:]}")
    # MuPDF may print its own messages on stdout; the result is the line after the marker.
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(PAGES_RESULT_MARKER)), None)
    if line is None:
        raise EUError(f"PyMuPDF page count gave no result: {proc.stdout.strip()[-500:]}")
    out = json.loads(line[len(PAGES_RESULT_MARKER):])
    return out["pymupdf"], out["results"]


def cmd_pages(args: argparse.Namespace) -> int:
    todo: list[tuple[Path, dict, dict]] = []
    for path in sorted(SOURCES_EU.glob("*.json")):
        src = read_json(path)
        for a in src.get("annexes", []):
            if a.get("sha256") and (args.all or a.get("pages") is None):
                todo.append((path, src, a))
    srcs = {p: s for p, s, _ in todo}
    version = None
    for i in range(0, len(todo), 200):
        chunk = todo[i:i + 200]
        version, results = pdf_pages([str(blob_path(a["sha256"], "pdf")) for _, _, a in chunk])
        for _, _, a in chunk:
            r = results[str(blob_path(a["sha256"], "pdf"))]
            a["pages"], a["text_chars"] = r["pages"], r["text_chars"]
            a["pdf_check"] = {"tool": f"PyMuPDF {version}", "error": r["error"],
                              "mupdf_messages": r.get("mupdf_messages") or [],
                              "text_layer": bool(r["text_chars"] and r["text_chars"] >= 200)}
        print(f"{min(i + 200, len(todo))}/{len(todo)} PDFs checked", flush=True)
    for path, src in srcs.items():
        write_json(path, src)
    print(f"done: {len(todo)} PDFs, PyMuPDF {version}")
    return 0


# ---------------------------------------------------------------- coverage report

def request_stats(raw_dir: Path = RAW_EU) -> dict:
    log = raw_dir / "requests.tsv"
    stats = {"total": 0, "bytes": 0, "by_host": {}, "by_status": {}}
    if not log.exists():
        return stats
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        stats["total"] += 1
        stats["bytes"] += int(parts[2] or 0)
        host = urllib.parse.urlsplit(parts[3]).hostname or "?"
        stats["by_host"][host] = stats["by_host"].get(host, 0) + 1
        stats["by_status"][parts[1]] = stats["by_status"].get(parts[1], 0) + 1
    return stats


REUSE_TERMS = [
    {"source": "Union Register open dataset (ods_products.json), used for the EU numbers",
     "url": REGISTER_INDEX_URL,
     "quote": "Available datasets: ... Medicinal products dataset Licence: Creative Commons Attribution 4.0 "
              "International",
     "status": "read on the Union Register page (ec.europa.eu) during discover"},
    {"source": "Union Register pages, Commission decisions and annex PDFs (ec.europa.eu/health/documents/"
               "community-register)",
     "url": "https://commission.europa.eu/legal-notice_en",
     "quote": None,
     "status": "not read: every Union Register page links its legal notice to commission.europa.eu, a host "
               "outside the two allowed for this task (ec.europa.eu/info/legal-notice_en answers 301 to it), and "
               "community-register/html/legal_notice.htm answers 301 to health.ec.europa.eu, also outside the "
               "allowed hosts; neither redirect was followed. The Union Register FAQ "
               "(community-register/register_faq.pdf) says nothing about reuse. The Commission's reuse policy "
               "(Decision 2011/833/EU, normally summarised in that legal notice as reuse allowed with "
               "acknowledgement of the source) is expected to apply but was not verified here."},
    {"source": "EMA website: medicines JSON, EPAR documents JSON, product-information PDFs",
     "url": "https://www.ema.europa.eu/en/about-us/about-website/legal-notice",
     "quote": "Information and documents made available on EMA's webpages are public and may be reproduced and "
              "/ or distributed, totally or in part, irrespective of the means and / or the formats used, for "
              "non-commercial and commercial purposes, provided that EMA is always acknowledged as the source of "
              "the material. Such acknowledgement must be included in each copy of the material. Citations may "
              "be made from such material without prior permission, provided the source is always acknowledged. "
              "The above-mentioned permissions do not apply to content supplied by third parties. Therefore, for "
              "documents where the copyright vests in a third party, permission for reproduction must be "
              "obtained from this copyright holder.",
     "status": "read (www.ema.europa.eu/en/about-us/legal-notice, which answers 301 to the URL given)"},
]


def coverage(products_path: Path = PRODUCTS, sources_dir: Path = SOURCES_EU, raw_dir: Path = RAW_EU) -> dict:
    products = load_products(products_path)
    refs = reference_products(products)
    t = {"reference_products": len(refs), "h_pages_fetched": 0, "products_not_started": 0,
         "products_with_problems": 0, "procedures": 0, "decisions": 0, "carried_by_later_decision": 0,
         "annexes_listed": 0, "annexes_en_listed": 0, "annexes_fetched": 0, "annexes_duplicate": 0,
         "annexes_logged": 0, "annexes_unaccounted": 0, "annex_bytes": 0, "annex_pages": 0,
         "annexes_page_counted": 0, "annexes_without_text_layer": 0, "annexes_unreadable": 0,
         "in_force_products": 0, "current_only_products": 0, "h_page_bytes": 0, "earliest_annex": None,
         "latest_annex": None, "products_first_annex_before_2005": 0, "products_first_annex_before_2011": 0}
    reasons: dict[str, int] = {}
    rows, problems = [], []
    for inn, ref in refs:
        path = sources_dir / f"{ref['dir']}.json"
        if not path.exists():
            t["products_not_started"] += 1
            rows.append({"inn": inn, "dir": ref["dir"], "status": "not-started"})
            continue
        src = read_json(path)
        if src.get("h_page"):
            t["h_pages_fetched"] += 1
            t["h_page_bytes"] += src["h_page"].get("size") or 0
        pc = src.get("procedure_counts") or {}
        t["procedures"] += pc.get("procedures", 0)
        t["decisions"] += pc.get("with_decision", 0)
        t["carried_by_later_decision"] += pc.get("carried_by_later_decision", 0)
        ann = src.get("annexes", [])
        counts = {"listed": len(ann), "en": sum(1 for a in ann if "en" in a.get("annex_languages", [])),
                  "fetched": 0, "duplicate": 0, "logged": 0, "unaccounted": 0}
        for a in ann:
            st = a.get("status")
            if st in ("fetched", "duplicate") and a.get("sha256") and blob_path(a["sha256"], "pdf", raw_dir).exists():
                counts[st] += 1
                if st == "fetched":
                    t["annex_bytes"] += a.get("size") or 0
                    if a.get("pages"):
                        t["annex_pages"] += a["pages"]
                        t["annexes_page_counted"] += 1
                    check = a.get("pdf_check") or {}
                    if check.get("error"):
                        t["annexes_unreadable"] += 1
                    elif check and not check.get("text_layer"):
                        t["annexes_without_text_layer"] += 1
                        problems.append(f"{ref['dir']} procedure {a['procedure_id']} ({a['decision_date']}): "
                                        f"no usable text layer on the first 3 pages ({a.get('text_chars')} chars)")
            elif st in ("logged", "logged-http"):
                counts["logged"] += 1
                key = re.sub(r"\s*\(annex languages:.*\)|https?://\S+", "", a.get("reason") or "").strip()
                key = re.sub(r"HTTP (\d+) for\s*", r"HTTP \1", key)
                reasons[key[:90]] = reasons.get(key[:90], 0) + 1
            else:
                counts["unaccounted"] += 1
        fetched_dates = [a["decision_date"] for a in ann if a.get("status") == "fetched"]
        if fetched_dates:
            t["earliest_annex"] = min(filter(None, [t["earliest_annex"], fetched_dates[0]]))
            t["latest_annex"] = max(filter(None, [t["latest_annex"], fetched_dates[-1]]))
            t["products_first_annex_before_2005"] += fetched_dates[0] < "2005-01-01"
            t["products_first_annex_before_2011"] += fetched_dates[0] < "2011-01-01"
        t["annexes_listed"] += counts["listed"]
        t["annexes_en_listed"] += counts["en"]
        t["annexes_fetched"] += counts["fetched"]
        t["annexes_duplicate"] += counts["duplicate"]
        t["annexes_logged"] += counts["logged"]
        t["annexes_unaccounted"] += counts["unaccounted"]
        if ref.get("history_type") == HISTORY_IN_FORCE:
            t["in_force_products"] += 1
        elif ref.get("history_type") == HISTORY_CURRENT_ONLY:
            t["current_only_products"] += 1
        if src.get("problems"):
            t["products_with_problems"] += 1
            problems.extend(f"{ref['dir']}: {p}" for p in src["problems"])
        problems.extend(f"{ref['dir']} procedure {a['procedure_id']} ({a.get('decision_date')}, {a.get('type')}): "
                        f"{a['reason']}" for a in ann if a.get("status") in ("logged", "logged-http", "duplicate"))
        rows.append({"inn": inn, "dir": ref["dir"], "name": ref["name"], "eu_number": ref.get("eu_number"),
                     "history_type": ref.get("history_type"), "record_from": ref.get("record_from"),
                     "record_to": ref.get("record_to"), "annexes": counts})
    runs = []
    if (raw_dir / "runs.jsonl").exists():
        runs = [json.loads(line) for line in (raw_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
    return {"generated": utc_now(), "totals": t, "logged_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "selection": products["counts"], "eu_number_source": {"rule": products["eu_number_rule"],
                                                                  "input": products["inputs"]["union_register_ods"],
                                                                  "verification": products["verification"]},
            "inputs": products["inputs"], "reuse_terms": REUSE_TERMS, "requests": request_stats(raw_dir),
            "runs": runs, "capture": capture_summary(), "discover_problems": products.get("problems", []),
            "problems": problems,
            "products": rows}


def capture_summary(captures_dir: Path = CAPTURES) -> dict | None:
    """The newest forward capture in brief: date, products pinned, problems and changes since the one before."""
    days = sorted(p for p in captures_dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    if not days:
        return None
    cap = read_json(days[-1])
    ch = cap.get("changes") or {}
    return {"file": str(days[-1].relative_to(ROOT)), "capture_date": cap.get("capture_date"),
            "captures": len(days), "products": len(cap.get("products", {})),
            "pdfs_pinned": sum(1 for p in cap.get("products", {}).values() if p.get("pi_sha256")),
            "pdf_bytes": sum(p.get("pi_size") or 0 for p in cap.get("products", {}).values()),
            "inputs": cap.get("inputs"), "problems": cap.get("problems", {}),
            "changes": {"previous": ch.get("previous"), "new": len(ch.get("new", [])),
                        "removed": ch.get("removed", []), "changed": ch.get("changed", {})}}


def cmd_coverage(args: argparse.Namespace) -> int:
    report = coverage()
    out = REPORTS / "eu-fetch.json"
    write_json(out, report)
    t = report["totals"]
    print(f"{t['reference_products']} reference products, {t['h_pages_fetched']} h-pages; annexes listed "
          f"{t['annexes_listed']}, English {t['annexes_en_listed']}, fetched {t['annexes_fetched']}, duplicate "
          f"{t['annexes_duplicate']}, logged {t['annexes_logged']}, unaccounted {t['annexes_unaccounted']}; "
          f"{t['annex_bytes'] / 1e9:.2f} GB, {t['annex_pages']} pages; in-force {t['in_force_products']}, "
          f"current-only {t['current_only_products']}; requests {report['requests']['total']}")
    for reason, n in report["logged_reasons"].items():
        print(f"  {n:>5}  {reason}")
    print(f"written {out}")
    return 0


# ---------------------------------------------------------------- forward capture

def epar_pi_documents(data: bytes) -> dict[str, dict]:
    """EMA product number -> its 'product-information' entry in the EPAR documents JSON."""
    doc = json.loads(data)
    rows = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise EUError("EPAR documents JSON has no 'data' list")
    out: dict[str, dict] = {}
    for r in rows:
        # Only the English product-information PDF: the type also covers a shared spreadsheet of member-state
        # URLs (product-information-urls-member-states_en.xls) filed under one product.
        if r.get("type") == "product-information" and re.match(r"\s*EMEA/H/C/\d", r.get("ema_product_number") or "") \
                and "product-information_en.pdf" in (r.get("document_url") or ""):
            key = canonical_ema(r["ema_product_number"])
            if key not in out or (r.get("last_updated_date") or "") > (out[key].get("last_updated_date") or ""):
                out[key] = r
    return out


def previous_capture(day: str, captures_dir: Path = CAPTURES) -> dict | None:
    older = sorted(p for p in captures_dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)
                   and p.stem < day)
    return read_json(older[-1]) if older else None


def capture_changes(prev: dict | None, products: dict[str, dict]) -> dict:
    """Products new, removed or changed (EMA revision_number / last_updated_date, PDF SHA-256) since prev.

    A product whose PDF download failed in this capture (``pi_fetch_error``) has no PDF hash to compare:
    it is listed under fetch_failed, and its PDF hash is not reported as a change."""
    if prev is None:
        return {"previous": None, "new": sorted(products), "removed": [], "changed": {},
                "fetch_failed": sorted(k for k, v in products.items() if v.get("pi_fetch_error"))}
    before = prev.get("products", {})
    changed, failed = {}, []
    for key, now in products.items():
        if now.get("pi_fetch_error"):
            failed.append(key)
        old = before.get(key)
        if old is None:
            continue
        fields = ["revision_number", "last_updated_date", "medicine_status"]
        if not now.get("pi_fetch_error"):
            fields.insert(2, "pi_sha256")
        what = [f for f in fields if old.get(f) != now.get(f)]
        if what:
            changed[key] = {f: [old.get(f), now.get(f)] for f in what}
    return {"previous": prev.get("capture_date"), "new": sorted(set(products) - set(before)),
            "removed": sorted(set(before) - set(products)), "changed": dict(sorted(changed.items())),
            "fetch_failed": sorted(failed)}


CAPTURE_RULE = ("Forward capture: the EMA medicines JSON, the EPAR documents JSON (for the product-information "
                "URL) and the current English product-information PDF of every reference product in "
                "sources/eu-products.json, pinned by SHA-256. A PDF whose URL and EPAR last_updated_date equal those "
                "of the newest earlier capture is not downloaded again; its pin is carried forward "
                "(pi_carried_from). Changes are reported against the newest earlier capture by EMA revision_number, "
                "last_updated_date and medicine_status and by PDF SHA-256. Nothing is committed to build/eu.")


def capture_day(day: str, products: dict, fetcher: EUFetcher | None, captures_dir: Path = CAPTURES,
                raw_dir: Path = RAW_EU, products_path: Path = PRODUCTS, trust_pins: bool = False,
                removed: set[str] | None = None) -> dict:
    """Write sources/eu-capture/<day>.json (resumable: an existing file of the same day is completed) and
    return it, with the change report against the newest earlier capture."""
    out = captures_dir / f"{day}.json"
    cap = read_json(out) if out.exists() else {"capture_date": day, "inputs": {}, "products": {}, "problems": {}}
    previous = previous_capture(day, captures_dir)
    capture_products(cap, out, products, fetcher, raw_dir, previous=previous, trust_pins=trust_pins,
                     removed=removed)
    cap["changes"] = capture_changes(previous, cap["products"])
    cap["rule"] = CAPTURE_RULE
    write_json(out, cap)
    set_current_only_dates(day, products_path, captures_dir)
    return cap


def cmd_capture(args: argparse.Namespace) -> int:
    day = args.date or today()
    out = CAPTURES / f"{day}.json"
    products = load_products()
    fetcher = new_fetcher()
    started_at, started = utc_now(), time.monotonic()
    record_start("capture", started_at)
    completed, cap = False, {"products": {}, "problems": {}}
    try:
        cap = capture_day(day, products, fetcher)
        completed = True
    finally:
        record_run("capture", started_at, time.monotonic() - started, fetcher.requests,
                   {"capture_date": day, "products": len(cap["products"]), "problems": len(cap["problems"]),
                    "completed": completed})
    ch = cap["changes"]
    print(f"capture {day}: {len(cap['products'])} products, {len(cap['problems'])} problems, "
          f"{fetcher.requests} requests; since {ch['previous']}: {len(ch['new'])} new, {len(ch['removed'])} "
          f"removed, {len(ch['changed'])} changed")
    print(f"written {out}")
    return 0


def capture_products(cap: dict, out: Path, products: dict, fetcher: EUFetcher | None,
                     raw_dir: Path = RAW_EU, previous: dict | None = None, trust_pins: bool = False,
                     removed: set[str] | None = None) -> None:
    """Pin today's EMA inputs and every reference product's product-information PDF into ``cap``.

    Problems are recomputed on every run, per product and per kind ({kind: message}): "not-listed" (not in
    today's EMA medicines JSON), "no-pi-document" (no product-information PDF in the EPAR documents JSON)
    and "pi-fetch-failed". A failed download clears the product's PDF pin for the day and sets
    pi_fetch_error, so the change report does not mistake it for a new PDF."""
    med, cap["inputs"]["ema_medicines_json"] = get_pinned(EMA_MEDICINES_URL, "json", "json",
                                                          cap["inputs"].get("ema_medicines_json"), fetcher, raw_dir)
    docs, cap["inputs"]["ema_epar_documents_json"] = get_pinned(EMA_EPAR_DOCUMENTS_URL, "json", "json",
                                                                cap["inputs"].get("ema_epar_documents_json"),
                                                                fetcher, raw_dir)
    write_json(out, cap)
    rows = {canonical_ema(r["ema_product_number"]): r for r in load_medicines(med)
            if r.get("category") == "Human" and r.get("ema_product_number", "").startswith("EMEA/H/C/")}
    pis = epar_pi_documents(docs)
    cap["problems"] = {}
    for inn, ref in active_reference_products(products, removed):
        key = canonical_ema(ref["ema_product_number"])
        entry = cap["products"].get(ref["dir"], {})
        row, pi = rows.get(key), pis.get(key)
        entry.update({"inn": inn, "name": ref["name"], "ema_product_number": key,
                      "medicine_status": row.get("medicine_status") if row else None,
                      "revision_number": row.get("revision_number") if row else None,
                      "last_updated_date": parse_dmy(row.get("last_updated_date", "")) if row else None,
                      "pi_url": pi.get("document_url") if pi else None,
                      "pi_last_updated": pi.get("last_updated_date") if pi else None})
        cap["products"][ref["dir"]] = entry
        problems: dict[str, str] = {}
        if row is None:
            problems["not-listed"] = "not in today's EMA medicines JSON"
        if not entry["pi_url"]:
            problems["no-pi-document"] = "no product-information document in the EPAR documents JSON"
        elif not (entry.get("pi_sha256") and entry.get("pi_fetched_url") == entry["pi_url"]
                  and (trust_pins or blob_path(entry["pi_sha256"], "pdf", raw_dir).exists())):
            before = ((previous or {}).get("products") or {}).get(ref["dir"]) or {}
            if (before.get("pi_sha256") and before.get("pi_fetched_url") == entry["pi_url"]
                    and entry["pi_last_updated"] and before.get("pi_last_updated") == entry["pi_last_updated"]):
                entry.update({k: before[k] for k in ("pi_sha256", "pi_size", "pi_fetched", "pi_fetched_url")
                              if k in before})
                entry["pi_carried_from"] = before.get("pi_carried_from") or previous.get("capture_date")
                entry.pop("pi_fetch_error", None)
            else:
                try:
                    if fetcher is None:
                        raise build.BuildError(f"{entry['pi_url']} is not cached and no network access was allowed")
                    data = fetcher.fetch(entry["pi_url"], "pdf")
                except build.FetchAborted:
                    write_json(out, cap)
                    raise
                except build.BuildError as err:
                    for k in ("pi_sha256", "pi_size", "pi_fetched", "pi_fetched_url", "pi_carried_from"):
                        entry.pop(k, None)
                    entry["pi_fetch_error"] = str(err)
                    problems["pi-fetch-failed"] = str(err)
                else:
                    entry.update(pi_sha256=store_blob(data, "pdf", raw_dir), pi_size=len(data),
                                 pi_fetched=utc_now(), pi_fetched_url=entry["pi_url"])
                    entry.pop("pi_fetch_error", None)
                    entry.pop("pi_carried_from", None)
                    print(f"{ref['dir']} {ref['name']}: {len(data)} bytes; {fetcher.requests} requests", flush=True)
        if problems:
            cap["problems"][ref["dir"]] = problems
        write_json(out, cap)


def set_current_only_dates(day: str, products_path: Path = PRODUCTS, captures_dir: Path = CAPTURES) -> None:
    """record_from of a current-only product is its first capture; record_to the latest one."""
    days = sorted(p.stem for p in captures_dir.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    products = load_products(products_path)
    for entry in products["molecules"].values():
        ref = entry.get("reference")
        if ref and ref.get("history_type") == HISTORY_CURRENT_ONLY:
            first = next((d for d in days if ref["dir"] in read_json(captures_dir / f"{d}.json").get("products", {})
                          and read_json(captures_dir / f"{d}.json")["products"][ref["dir"]].get("pi_sha256")), None)
            ref["record_from"] = first
            ref["record_to"] = days[-1] if first else None
    write_json(products_path, products)


# ---------------------------------------------------------------- build (build/eu via git fast-import)

BUILD_EU = ROOT / "build" / "eu"
CONVERTED = RAW_EU / "converted"
BUILD_MARKER = "asof-build-eu"
COMMITTER = build.COMMITTER
CONVERT_STATUSES = ("fetched", "duplicate")     # annex entries with a pinned English PDF
NO_CHANGE = ("empty-annex-i", "no-annex-i")       # the decision did not change Annex I
CONVERT_TIMEOUT_S = 1800                          # one annex; a worker past this is killed and the build fails
SCOPE = "Annex I (summary of product characteristics), English annexes only"
SKIP_REASONS = {
    "empty-annex-i": "Annex I title followed directly by Annex II (the decision did not change the SmPC)",
    "no-annex-i": "the annex holds only Annex II or III (the decision did not change the SmPC)",
    "no-smpc": "Annex I has no section 1 (for example the scientific conclusions of a referral)",
    "no-text-layer": "no usable text layer (no OCR is done)",
    "unusable-text-layer": "text layer without a usable character map (no OCR is done)",
    "unreadable-pdf": "PyMuPDF could not open the PDF",
    "converter-error": "the converter raised an error",
}

CONVERT_SCRIPT = r"""
import json, os, sys, traceback
sys.path.insert(0, TOOLS)
import eu_qrd
sys.stdout.write("\n" + READY + json.dumps({"converter": eu_qrd.EU_CONVERTER_VERSION,
                                             "pymupdf": eu_qrd.pymupdf_version()}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    job = json.loads(line)
    try:
        with open(job["path"], "rb") as fh:
            res = eu_qrd.convert(fh.read())
    except Exception as err:   # reported, never cached: the build fails and a rerun tries again
        sys.stderr.write("converter error on %s\n" % job["path"])
        traceback.print_exc()
        sys.stderr.flush()
        sys.stdout.write("\n" + DONE + json.dumps({"sha256": job["sha256"], "status": "converter-error",
                                                  "error": "%s: %s" % (type(err).__name__, err)}) + "\n")
        sys.stdout.flush()
        continue
    tmp = job["out"] + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, job["out"])
    sys.stdout.write("\n" + DONE + json.dumps({"sha256": job["sha256"], "status": res["status"]}) + "\n")
    sys.stdout.flush()
"""
CONVERT_READY = "EU-CONVERT-READY "
CONVERT_DONE = "EU-CONVERT-DONE "


def _start_converter(python: Path, log_path: Path | None = None) -> tuple[subprocess.Popen, dict]:
    """A converter process and its identity. Its stderr (Python tracebacks, MuPDF noise) is appended to
    ``log_path`` when given, so a worker failure can be diagnosed."""
    script = (f"TOOLS = {str(ROOT / 'tools')!r}\nREADY = {CONVERT_READY!r}\nDONE = {CONVERT_DONE!r}\n"
              + CONVERT_SCRIPT)
    err = log_path.open("a", encoding="utf-8") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen([str(python), "-I", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, text=True, encoding="utf-8", cwd=str(ROOT))
    finally:
        if log_path:
            err.close()   # the child keeps its own handle
    line = _read_marker(proc, CONVERT_READY)
    if line is None:
        proc.kill()
        proc.wait()
        proc.stdout.close()
        where = f"; see {log_path}" if log_path else ""
        raise EUError(f"the converter did not start under {python} (is PyMuPDF installed in .venv-eu?){where}")
    return proc, json.loads(line)


def _read_marker(proc: subprocess.Popen, marker: str) -> str | None:
    """The payload of the next stdout line that starts with marker (MuPDF may print other lines); None at EOF."""
    for line in proc.stdout:
        if line.startswith(marker):
            return line[len(marker):]
    return None


def converter_identity(python: Path = VENV_PYTHON) -> dict:
    """{"converter": EU_CONVERTER_VERSION, "pymupdf": version} of the converter the build will run."""
    if not python.exists():
        raise EUError(f"{python} is missing; the EU build needs the pinned PyMuPDF (tools/requirements-eu.txt)")
    proc, ident = _start_converter(python)
    proc.stdin.close()
    proc.wait()
    proc.stdout.close()
    return ident


def cache_dir_for(ident: dict, root: Path = CONVERTED) -> Path:
    return root / f"qrd{ident['converter']}-pymupdf{ident['pymupdf']}"


def convert_pdfs(jobs: dict[str, Path], cache: Path, workers: int, python: Path = VENV_PYTHON,
                 log=print) -> int:
    """Convert every {sha256: pdf path} not yet in cache into cache/<sha256>.json; returns how many were run.

    Each worker is one long-lived .venv-eu interpreter fed one PDF at a time, so the work balances itself.
    The result of a PDF depends only on its bytes, never on the worker or the order. Any failure fails the
    build and is never cached: a converter exception (logged with its traceback in
    cache/converter-<n>.log), a worker that dies or passes CONVERT_TIMEOUT_S, or an error in the worker
    thread itself (a broken pipe, a converter that does not restart)."""
    todo = sorted(((sha, p) for sha, p in jobs.items() if not (cache / f"{sha}.json").exists()),
                  key=lambda sp: (-sp[1].stat().st_size, sp[0]))
    if not todo:
        return 0
    cache.mkdir(parents=True, exist_ok=True)
    pending = queue.Queue()
    for item in todo:
        pending.put(item)
    lock = threading.Lock()
    state = {"done": 0, "started": time.monotonic()}
    failures: list[str] = []

    def work(n: int) -> None:
        log_path = cache / f"converter-{n}.log"
        proc, path = None, None
        try:
            proc, _ = _start_converter(python, log_path)
            while True:
                try:
                    sha, path = pending.get_nowait()
                except queue.Empty:
                    return
                proc.stdin.write(json.dumps({"sha256": sha, "path": str(path),
                                             "out": str(cache / f"{sha}.json")}) + "\n")
                proc.stdin.flush()
                timer = threading.Timer(CONVERT_TIMEOUT_S, proc.kill)
                timer.start()
                line = _read_marker(proc, CONVERT_DONE)
                timer.cancel()
                if line is None:
                    with lock:
                        failures.append(f"{path}: the converter process died or passed {CONVERT_TIMEOUT_S} s "
                                        f"(see {log_path})")
                    proc.wait()
                    proc.stdout.close()
                    proc = None
                    proc, _ = _start_converter(python, log_path)
                    continue
                done = json.loads(line)
                if done.get("status") == "converter-error":
                    with lock:
                        failures.append(f"{path}: converter error {done.get('error')} (traceback in {log_path})")
                    continue
                with lock:
                    state["done"] += 1
                    if state["done"] % 100 == 0 or state["done"] == len(todo):
                        log(f"converted {state['done']}/{len(todo)} PDFs "
                            f"({time.monotonic() - state['started']:.0f} s)", flush=True)
        except Exception as err:   # BrokenPipeError on stdin, a converter that does not restart, ...
            with lock:
                failures.append(f"{path or 'worker start'}: {type(err).__name__}: {err}")
        finally:
            if proc is not None:
                if proc.stdin and not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                proc.wait()
                if proc.stdout:
                    proc.stdout.close()

    threads = [threading.Thread(target=work, args=(n,), daemon=True)
               for n in range(max(1, min(workers, len(todo))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if failures:
        raise EUError("conversion failed for %d PDFs:\n  %s" % (len(failures), "\n  ".join(failures[:20])))
    return len(todo)


def load_conversion(cache: Path, sha: str) -> dict:
    """The cached converter result of the PDF with this SHA-256. The converter hashes the bytes it read;
    a result whose hash is not its cache key, or that records a converter error, fails the build."""
    path = cache / f"{sha}.json"
    if not path.exists():
        raise EUError(f"no conversion result for {sha} in {cache}")
    res = read_json(path)
    if res.get("sha256") != sha:
        raise EUError(f"{path}: the converter read a PDF with SHA-256 {res.get('sha256')}, not {sha}")
    if res.get("status") == "converter-error":
        raise EUError(f"{path}: cached converter error ({'; '.join(res.get('warnings', []))}); delete it and rebuild")
    return res


def slim_result(r: dict) -> dict:
    """What planning and the coverage report need from a converter result (not the section text)."""
    return {"status": r["status"], "warnings": r["warnings"], "smpcs": len(r.get("smpcs", [])),
            "sections": len(r["sections"])}


def converter_label(ident: dict) -> str:
    return f"eu_qrd {ident['converter']} (PyMuPDF {ident['pymupdf']})"


def latest_capture(dir_name: str, captures_dir: Path = CAPTURES) -> tuple[str, dict] | None:
    """(capture date, entry) of the newest forward capture that pinned this product's product-information PDF."""
    for path in sorted(captures_dir.glob("*.json"), reverse=True):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
            continue
        entry = read_json(path).get("products", {}).get(dir_name)
        if entry and entry.get("pi_sha256"):
            return path.stem, entry
    return None


def annex_key(a: dict) -> tuple:
    """Order of a product's annexes: batch (``added``, the daily run that pinned it; '' before the first
    daily run), then decision date and procedure id. Source-Version numbers follow this order, so an annex
    appended later (e.g. a corrigendum dated at its corrected decision) never renumbers older commits."""
    return (a.get("added") or "", a["decision_date"], int(a["procedure_id"]))


def build_inputs(products_path: Path = PRODUCTS, sources_dir: Path = SOURCES_EU, raw_dir: Path = RAW_EU,
                 removed: set[str] | None = None) -> tuple[dict, list[dict], dict[str, Path], list[str]]:
    """(products file, one record per reference product not removed on request, {sha256: pdf path} of every
    pinned annex, problems)."""
    products = load_products(products_path)
    records, jobs, problems = [], {}, []
    for inn, ref in active_reference_products(products, removed):
        path = sources_dir / f"{ref['dir']}.json"
        if not path.exists():
            problems.append(f"{ref['dir']} ({inn}): no sources file; run tools/eu.py fetch")
            records.append({"inn": inn, "ref": ref, "src": None, "annexes": []})
            continue
        src = read_json(path)
        annexes = []
        for a in src.get("annexes", []):
            if a.get("status") in CONVERT_STATUSES and a.get("sha256"):
                pdf = blob_path(a["sha256"], "pdf", raw_dir)
                if not pdf.exists():
                    raise EUError(f"{ref['dir']} procedure {a['procedure_id']}: pinned annex {pdf} is missing "
                                  "(run tools/eu.py fetch)")
                jobs[a["sha256"]] = pdf
                annexes.append(a)
        annexes.sort(key=annex_key)
        records.append({"inn": inn, "ref": ref, "src": src, "annexes": annexes})
    return products, records, jobs, problems


def plan_commits(products: dict, records: list[dict], conversions: dict[str, dict],
                 captures: dict[str, tuple[str, dict] | None]) -> tuple[list[dict], list[dict]]:
    """(commits in build order, one outcome row per product).

    An annex is committed when its conversion status is "converted". Products with no converted annex
    fall back to the newest captured product-information PDF (history current-only) when that converted.
    conversions maps a PDF's SHA-256 to its converter result; captures maps a product folder to
    latest_capture() (only needed for products without a converted annex)."""
    commits, outcomes = [], []
    for rec in records:
        inn, ref, src = rec["inn"], rec["ref"], rec["src"]
        mine, skipped = [], []
        for a in rec["annexes"]:
            res = conversions[a["sha256"]]
            if res["status"] == "converted":
                mine.append({"kind": "annex", "annex": a, "sha256": a["sha256"], "date": a["decision_date"]})
            else:
                skipped.append({"procedure_id": a["procedure_id"], "decision_date": a["decision_date"],
                                "status": res["status"], "reason": SKIP_REASONS.get(res["status"], res["status"])})
        history, record_to, fallback_problem = HISTORY_IN_FORCE, None, None
        if mine:
            record_to = ((src or {}).get("h_page") or {}).get("fetched", "")[:10] or None
        else:
            history = HISTORY_CURRENT_ONLY
            cap = captures.get(ref["dir"])
            if cap is None:
                fallback_problem = "no converted annex and no captured product-information PDF"
            else:
                day, entry = cap
                res = conversions.get(entry["pi_sha256"])
                if res is None or res["status"] != "converted":
                    status = res["status"] if res else "not converted"
                    fallback_problem = f"no converted annex; captured product-information PDF: {status}"
                else:
                    mine.append({"kind": "capture", "capture": entry, "sha256": entry["pi_sha256"], "date": day})
                    record_to = day
        for c in mine:
            if c["kind"] == "annex":
                c["corrigendum_of"] = corrigendum_of(c["annex"], (src or {}).get("annexes", []))
        for seq, c in enumerate(mine, start=1):
            c.update(inn=inn, ref=ref, seq=seq, order=commit_order(c, ref))
        built = bool(mine)
        outcomes.append({"inn": inn, "ref": ref, "src": src, "history_type": history if built else "not-built",
                         "record_from": mine[0]["date"] if built else None,
                         "record_to": record_to if built else None, "commits": mine, "skipped": skipped,
                         "problem": fallback_problem, "molecule": products["molecules"][inn]})
        commits.extend(mine)
    commits.sort(key=lambda c: c["order"])
    return commits, outcomes


def commit_order(c: dict, ref: dict) -> tuple:
    """Build order of one EU commit: (added or '', date, procedure id, product folder)."""
    if c["kind"] == "annex":
        return (c["annex"].get("added") or "", c["date"], int(c["annex"]["procedure_id"]), ref["dir"])
    return ("", c["date"], 0, ref["dir"])


def is_corrigendum(annex: dict) -> bool:
    return bool(re.search(r"corrigendum|rectificative", annex.get("type") or "", re.I))


def corrigendum_of(annex: dict, entries: list[dict]) -> str | None:
    """For a corrigendum (or rectificative decision), the procedure id of the procedure it corrects: the
    latest earlier procedure of the product with the same decision number, else with the same decision
    date; "not identified" when there is none. None for any other procedure."""
    if not is_corrigendum(annex):
        return None
    pid = int(annex["procedure_id"])
    for key in ("decision_number", "decision_date"):
        earlier = [e for e in entries if e.get(key) and e.get(key) == annex.get(key)
                   and int(e["procedure_id"]) < pid and not is_corrigendum(e)]
        if earlier:
            return max(earlier, key=lambda e: int(e["procedure_id"]))["procedure_id"]
    return "not identified"


PRODUCT_JSON_FIELDS = ("active_substance", "corpus", "ema_product_number", "eu_number", "history_type",
                       "marketing_authorisation_date", "medicine_url", "molecule", "molecule_name", "name",
                       "register_name", "scope", "source", "source_h_page_url")


def product_json(out: dict, ident: dict | None = None) -> dict:
    """The constants of a product, written with its first commit and never changed by a later one: nothing
    here depends on how many decisions exist or on the build date (holder, comparability, versions and the
    record window live in molecules/<inn>.json and in the commits)."""
    ref, mol = out["ref"], out["molecule"]
    in_force = out["history_type"] == HISTORY_IN_FORCE
    return {
        "corpus": "eu",
        "name": ref["name"],
        "register_name": ref.get("register_name"),
        "ema_product_number": ref["ema_product_number"],
        "eu_number": ref.get("eu_number"),
        "molecule": out["inn"],
        "molecule_name": mol.get("name"),
        "active_substance": ref.get("active_substance"),
        "marketing_authorisation_date": ref.get("marketing_authorisation_date"),
        "history_type": out["history_type"],
        "source": "ec-union-register" if in_force else "ema-product-information",
        "source_h_page_url": ref.get("h_page_url"),
        "medicine_url": ref.get("medicine_url"),
        "scope": SCOPE,
    }


def corpus_json(outcomes: list[dict] | None = None, ident: dict | None = None) -> dict:
    """corpus.json, written once in the first commit: constants only (counts, record window, history types
    and the converter come from the commits and manifest.json)."""
    return {
        "corpus": "eu",
        "scope": SCOPE,
        "layout": "<inn>/<EMEA-H-C-nnnnnn>/product.json and smpc-<section>-<slug>.md; one commit per Commission "
                  "decision whose English Annex I was converted, dated at the decision date; the Effective-Date "
                  "trailer is authoritative",
        "sources": ["European Commission, Union Register of medicinal products (ec.europa.eu/health/documents/"
                    "community-register)", "European Medicines Agency (www.ema.europa.eu)"],
    }


def eu_root_files() -> dict[str, bytes]:
    """corpus.json, README.md and NOTICE.md at the root of the EU corpus, written in the first commit."""
    corpus = (json.dumps(corpus_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    return {"corpus.json": corpus, **notices.root_files("eu")}


def subject_line(c: dict) -> str:
    if c["kind"] == "annex":
        return f"{c['ref']['name']} — Commission decision {c['annex']['decision_number']}"
    return f"{c['ref']['name']} — EMA product information captured {c['date']}"


def commit_message_eu(c: dict, ident: dict) -> str:
    ref = c["ref"]
    if c["kind"] == "annex":
        a = c["annex"]
        lines = [subject_line(c), "",
                 f"Source-Id: ec-union-register:{ref['eu_number']}:{a['procedure_id']}",
                 f"Source-Version: {c['seq']}",
                 f"Effective-Date: {a['decision_date']}",
                 f"Decision-Number: {a['decision_number']}",
                 f"Decision-Date: {a['decision_date']}",
                 f"EMA-Procedure: {a.get('ema_number') or 'not stated'}",
                 f"Procedure-Type: {a.get('type') or 'not stated'}",
                 f"Source-URL: {a.get('fetched_url') or a['url']}",
                 f"Source-SHA256: {a['sha256']}"]
        if a.get("closed"):
            lines.append(f"Procedure-Closed: {a['closed']}")
        if c.get("corrigendum_of"):
            lines.append(f"Corrigendum-Of: {c['corrigendum_of']}")
        if a.get("status") == "duplicate":
            m = re.search(r"procedure (\d+)", a.get("reason") or "")
            lines.append(f"Annex-Identical-To: {m.group(1) if m else 'an earlier procedure'}")
        lines += [f"Converter-Version: {converter_label(ident)}", f"History: {HISTORY_IN_FORCE}", notices.TRAILER_EU]
    else:
        e = c["capture"]
        lines = [subject_line(c), "",
                 f"Source-Id: ema-product-information:{ref['ema_product_number']}:{c['date']}",
                 f"Source-Version: {c['seq']}",
                 f"Effective-Date: {c['date']}",
                 f"Captured-Date: {c['date']}",
                 f"EMA-Last-Updated: {e.get('last_updated_date') or 'not stated'}",
                 f"Source-URL: {e.get('pi_fetched_url') or e.get('pi_url')}",
                 f"Source-SHA256: {e['pi_sha256']}",
                 f"Converter-Version: {converter_label(ident)}",
                 f"History: {HISTORY_CURRENT_ONLY}",
                 notices.TRAILER_EU]
    return "\n".join(lines) + "\n"


def author_line(c: dict) -> str:
    ref = c["ref"]
    if c["kind"] == "annex":
        pid = c["annex"]["procedure_id"]
        return f"Annex {ref['eu_number']} {pid} <{ref['dir']}.{pid}@labels.invalid>"
    return f"Product information {ref['ema_product_number']} {c['date']} <{ref['dir']}.pi-{c['date']}@labels.invalid>"


def prepare_out_eu(out: Path, clean: bool) -> None:
    if out.exists() and any(out.iterdir()):
        if not clean:
            raise EUError(f"{out} is not empty; pass --clean to rebuild it")
        if not (out / ".git" / BUILD_MARKER).exists():
            raise EUError(f"{out} was not created by tools/eu.py build (no .git/{BUILD_MARKER}); refusing to delete it")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


class _EuWriter:
    """Streams one fast-import commit per planned EU commit; ``state`` maps a product folder to its files'
    blob ids as of the previous commit (for an append: the product's tree at HEAD)."""

    def __init__(self, proc: subprocess.Popen, state: dict, ident: dict, parent: str | None,
                 root: dict[str, bytes] | None):
        self.proc, self.state, self.ident, self.parent, self.root = proc, state, ident, parent, root
        self.n = 0

    def write(self, c: dict, product: dict, res: dict) -> None:
        self.n += 1
        ref = c["ref"]
        prefix = f"{c['inn']}/{ref['dir']}/"
        files = {"product.json": json.dumps(product, indent=2, sort_keys=True, ensure_ascii=False) + "\n"}
        files.update({f"{stem}.md": text for stem, text in res["sections"].items()})
        encoded = {name: text.encode("utf-8") for name, text in files.items()}
        hashes = {name: build.blob_hash(b) for name, b in encoded.items()}
        before = self.state.get(prefix, {})
        stamp = f"{build.epoch(c['date'])} +0000"
        chunk = [b"commit refs/heads/main\n", b"mark :%d\n" % self.n,
                 f"author {author_line(c)} {stamp}\n".encode(),
                 f"committer {COMMITTER} {stamp}\n".encode(),
                 build._data(commit_message_eu(c, self.ident).encode("utf-8"))]
        if self.n > 1:
            chunk.append(b"from :%d\n" % (self.n - 1))
        elif self.parent:
            chunk.append(f"from {self.parent}\n".encode())
        if self.n == 1 and self.root:
            chunk += build.root_chunk(self.root)
        for name in sorted(set(before) - set(encoded)):
            chunk.append(f"D {prefix}{name}\n".encode())
        for name in sorted(encoded):
            if before.get(name) != hashes[name]:
                chunk.append(f"M 100644 inline {prefix}{name}\n".encode())
                chunk.append(build._data(encoded[name]))
        chunk.append(b"\n")
        self.proc.stdin.write(b"".join(chunk))
        self.state[prefix] = hashes


def _eu_fast_import(out: Path, marks: Path) -> subprocess.Popen:
    return subprocess.Popen(["git", "-C", str(out), "-c", "core.hooksPath=/dev/null", "fast-import", "--quiet",
                             "--date-format=raw", f"--export-marks={marks}"],
                            stdin=subprocess.PIPE, env=build.git_env(out))


def _eu_finish(proc: subprocess.Popen, out: Path, marks: Path, n: int) -> list[str]:
    proc.stdin.close()
    if proc.wait() != 0:
        raise EUError("git fast-import failed")
    if not n:
        return []
    build.git(out, "reset", "-q", "--hard", "main")
    by_mark = dict(line.split() for line in marks.read_text().splitlines())
    return [by_mark[f":{i}"] for i in range(1, n + 1)]


def write_repo(out: Path, commits: list[dict], outcomes: list[dict], conversions_of, ident: dict) -> list[str]:
    """git fast-import the planned commits into out; returns the commit hashes in order.

    conversions_of(sha256) returns the converter result. Trees, messages, authors and dates depend only on
    the inputs, so the same inputs always give the same hashes."""
    out = Path(out).resolve()   # git runs with -C out, so the marks path must not be relative
    build.git(out, "init", "-q", "-b", "main")
    (out / ".git" / BUILD_MARKER).write_text("created by tools/eu.py build\n", encoding="utf-8")
    marks = out / ".git" / "asof-marks"
    products = {o["ref"]["dir"]: product_json(o) for o in outcomes if o["commits"]}
    proc = _eu_fast_import(out, marks)
    writer = _EuWriter(proc, {}, ident, None, eu_root_files())
    try:
        for c in commits:
            writer.write(c, products[c["ref"]["dir"]], conversions_of(c["sha256"]))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return _eu_finish(proc, out, marks, writer.n)


# ---------------------------------------------------------------- append

def head_commits_eu(repo: Path) -> list[tuple[str, str]]:
    """(product folder, procedure id or 'pi-<date>') of every commit on main, oldest first, from the authors."""
    rows = []
    for email in build.git(repo, "log", "--reverse", "--format=%ae", "main").split():
        local = email.split("@", 1)[0]
        folder, _, key = local.partition(".")
        if not folder.startswith("EMEA-H-C-") or not key:
            raise EUError(f"{repo}: commit author {email} is not an EU annex or capture author")
        rows.append((folder, key))
    return rows


def plan_append_eu(repo: Path, products_path: Path = PRODUCTS, sources_dir: Path = SOURCES_EU,
                   raw_dir: Path = RAW_EU, removed: set[str] | None = None) -> tuple[list[dict], dict]:
    """(pending records, context) for an append. A pending annex is a pinned English annex that carries
    ``added``, is not committed and was not already skipped by an earlier append (``build_skip``); annexes
    without ``added`` were decided by the full build that made HEAD. Refuses with build.HISTORY_REFUSAL
    when a pending annex would sort before HEAD's newest commit in the full-build order, and when HEAD still
    holds a product removed on request (sources/removed.json; only a full rebuild takes it out)."""
    removed = build.removed_ids("eu") if removed is None else set(removed)
    committed = head_commits_eu(repo)
    present = sorted({folder for folder, _ in committed} & removed)
    if present:
        raise EUError(f"{len(present)} removed product(s) still in HEAD (first: {present[0]}); "
                      f"{build.HISTORY_REFUSAL}: run tools/purge.py rebuild --corpus eu")
    done = set(committed)
    per_product: dict[str, int] = collections.Counter(folder for folder, _ in committed)
    products = load_products(products_path)
    annex_of: dict[tuple[str, str], dict] = {}
    pending = []
    for inn, ref in active_reference_products(products, removed):
        path = sources_dir / f"{ref['dir']}.json"
        if not path.exists():
            continue
        src = read_json(path)
        mine = []
        for a in src.get("annexes", []):
            annex_of[(ref["dir"], str(a["procedure_id"]))] = a
            if (a.get("status") in CONVERT_STATUSES and a.get("sha256") and a.get("added")
                    and (ref["dir"], str(a["procedure_id"])) not in done and not a.get("build_skip")):
                pdf = blob_path(a["sha256"], "pdf", raw_dir)
                if not pdf.exists():
                    raise EUError(f"{ref['dir']} procedure {a['procedure_id']}: pinned annex {pdf} is missing "
                                  "(run tools/eu.py fetch)")
                mine.append(a)
        if mine:
            pending.append({"inn": inn, "ref": ref, "src": src, "path": path, "annexes": sorted(mine, key=annex_key)})
    last_key = None
    if committed:
        folder, key = committed[-1]
        last = annex_of.get((folder, key))
        if last is None:
            raise EUError(f"HEAD's newest commit ({folder} {key}) is not a pinned annex; {build.HISTORY_REFUSAL}")
        last_key = (last.get("added") or "", last["decision_date"], int(last["procedure_id"]), folder)
    for rec in pending:
        for a in rec["annexes"]:
            k = (a["added"], a["decision_date"], int(a["procedure_id"]), rec["ref"]["dir"])
            if last_key is not None and k <= last_key:
                raise EUError(f"{rec['ref']['dir']} procedure {a['procedure_id']} (added {a['added']}) sorts before "
                              f"HEAD's newest commit; {build.HISTORY_REFUSAL}")
    return pending, {"products": products, "per_product": per_product, "committed": committed}


def append_eu(out: Path = BUILD_EU, products_path: Path = PRODUCTS, sources_dir: Path = SOURCES_EU,
              raw_dir: Path = RAW_EU, cache_root: Path = CONVERTED, workers: int = 2, python: Path | None = None,
              log=print, removed: set[str] | None = None) -> dict:
    """Append the pending annexes (see plan_append_eu) of build/eu as new commits on main, converting only
    them. Annexes whose conversion shows no Annex I change are marked ``build_skip`` in their sources file
    (as the full build skips them). Returns {"hashes", "skipped", "commits"}; HEAD is untouched when nothing
    is pending or everything was skipped."""
    if not (out / ".git").exists():
        raise EUError(f"{out} is not a git repo; run tools/eu.py build first")
    pending, ctx = plan_append_eu(out, products_path, sources_dir, raw_dir, removed)
    if not pending:
        return {"hashes": [], "skipped": [], "commits": []}
    python = python or eu_python()
    ident = converter_identity(python)
    last_commit_msg = build.git(out, "log", "-1", "--format=%(trailers:key=Converter-Version,valueonly,separator=)",
                                "main").strip()
    if last_commit_msg and last_commit_msg != converter_label(ident):
        raise EUError(f"HEAD was converted by {last_commit_msg}, this run would use {converter_label(ident)}; "
                      f"{build.HISTORY_REFUSAL}")
    cache = cache_dir_for(ident, cache_root)
    jobs = {a["sha256"]: blob_path(a["sha256"], "pdf", raw_dir) for rec in pending for a in rec["annexes"]}
    convert_pdfs(jobs, cache, workers, python, log)
    commits, skipped = [], []
    for rec in pending:
        ref, src = rec["ref"], rec["src"]
        seq = ctx["per_product"].get(ref["dir"], 0)
        changed = False
        for a in rec["annexes"]:
            res = load_conversion(cache, a["sha256"])
            if res["status"] != "converted":
                entry = next(e for e in src["annexes"] if str(e["procedure_id"]) == str(a["procedure_id"]))
                entry["build_skip"] = res["status"]
                changed = True
                skipped.append({"dir": ref["dir"], "procedure_id": a["procedure_id"], "status": res["status"]})
                continue
            seq += 1
            c = {"kind": "annex", "annex": a, "sha256": a["sha256"], "date": a["decision_date"], "inn": rec["inn"],
                 "ref": ref, "seq": seq, "corrigendum_of": corrigendum_of(a, src.get("annexes", []))}
            c["order"] = commit_order(c, ref)
            commits.append(c)
        if changed:
            write_json(rec["path"], src)
    commits.sort(key=lambda c: c["order"])
    if not commits:
        return {"hashes": [], "skipped": skipped, "commits": []}
    state = {}
    for c in commits:
        prefix = f"{c['inn']}/{c['ref']['dir']}/"
        if prefix not in state:
            state[prefix] = build.label_tree(out, prefix)
    marks = Path(tempfile.mkdtemp(prefix="asof-eu-append-")) / "marks"
    proc = _eu_fast_import(out, marks)
    writer = _EuWriter(proc, state, ident, "refs/heads/main^0", None)
    try:
        for c in commits:
            out_row = {"ref": c["ref"], "inn": c["inn"], "history_type": HISTORY_IN_FORCE,
                       "molecule": ctx["products"]["molecules"][c["inn"]]}
            writer.write(c, product_json(out_row), load_conversion(cache, c["sha256"]))
    except BaseException:
        proc.kill()
        proc.wait()
        shutil.rmtree(marks.parent, ignore_errors=True)
        raise
    try:
        hashes = _eu_finish(proc, out, marks, writer.n)
    finally:
        shutil.rmtree(marks.parent, ignore_errors=True)
    for c, h in zip(commits, hashes):
        c["hash"] = h
    return {"hashes": hashes, "skipped": skipped, "commits": commits}


def build_eu(out: Path = BUILD_EU, products_path: Path = PRODUCTS, sources_dir: Path = SOURCES_EU,
             raw_dir: Path = RAW_EU, captures_dir: Path = CAPTURES, cache_root: Path = CONVERTED,
             clean: bool = False, workers: int = 4, python: Path = VENV_PYTHON, log=print,
             removed: set[str] | None = None) -> dict:
    """Convert every pinned English annex of the reference products not removed on request and write the EU
    repo. Returns hashes, outcomes and timings."""
    t0 = time.monotonic()
    ident = converter_identity(python)
    cache = cache_dir_for(ident, cache_root)
    products, records, jobs, problems = build_inputs(products_path, sources_dir, raw_dir, removed)
    log(f"{len(records)} reference products, {len(jobs)} unique English annex PDFs; converter "
        f"{converter_label(ident)}; cache {cache}", flush=True)
    ran = convert_pdfs(jobs, cache, workers, python, log)
    t_convert = time.monotonic() - t0
    slim = {sha: slim_result(load_conversion(cache, sha)) for sha in sorted(jobs)}   # one full result at a time
    # current-only fallback for products whose annexes all failed conversion
    captures, fallback_jobs = {}, {}
    for rec in records:
        if not any(slim[a["sha256"]]["status"] == "converted" for a in rec["annexes"]):
            cap = latest_capture(rec["ref"]["dir"], captures_dir)
            captures[rec["ref"]["dir"]] = cap
            if cap:
                pdf = blob_path(cap[1]["pi_sha256"], "pdf", raw_dir)
                if pdf.exists():
                    fallback_jobs[cap[1]["pi_sha256"]] = pdf
    if fallback_jobs:
        ran += convert_pdfs(fallback_jobs, cache, workers, python, log)
        for sha in fallback_jobs:
            slim[sha] = slim_result(load_conversion(cache, sha))
    commits, outcomes = plan_commits(products, records, slim, captures)
    prepare_out_eu(out, clean)
    t1 = time.monotonic()
    hashes = write_repo(out, commits, outcomes, lambda sha: load_conversion(cache, sha), ident)
    for c, h in zip(commits, hashes):
        c["hash"] = h
    t_end = time.monotonic()
    log(f"built {len(hashes)} commits into {out} in {t_end - t0:.1f} s "
        f"(convert {t_convert:.1f} s, {ran} PDFs converted this run; commit {t_end - t1:.1f} s)", flush=True)
    return {"ident": ident, "cache": cache, "hashes": hashes, "commits": commits, "outcomes": outcomes,
            "conversions": slim, "problems": problems, "converted_this_run": ran, "workers": workers,
            "seconds": {"total": round(t_end - t0, 1), "convert": round(t_convert, 1),
                        "commit": round(t_end - t1, 1)}}


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def peak_memory() -> dict:
    """Peak resident memory in bytes of this process and of the largest finished child (converter or git)."""
    scale = 1 if sys.platform == "darwin" else 1024     # ru_maxrss is bytes on macOS, KiB on Linux
    return {"build_process": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale,
            "largest_child": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale}


def sync_products(outcomes: list[dict], products_path: Path = PRODUCTS) -> None:
    """Write the built history type and record window back into sources/eu-products.json (the crosswalk)."""
    products = load_products(products_path)
    for o in outcomes:
        ref = products["molecules"][o["inn"]]["reference"]
        n = len(o["commits"])
        if o["history_type"] == HISTORY_IN_FORCE:
            note = f"{n} converted English annexes committed to build/eu from the Union Register"
        elif o["history_type"] == HISTORY_CURRENT_ONLY:
            note = "no converted Union Register annex; text from the captured EMA product-information PDF"
        else:
            note = f"not built: {o['problem']}"
        ref.update(history_type=o["history_type"], record_from=o["record_from"], record_to=o["record_to"],
                   history_note=note, build_versions=n)
    write_json(products_path, products)


def coverage_eu(result: dict) -> dict:
    """Per product and in total: decisions, English annexes listed, fetched, converted, skipped with reasons."""
    rows, reasons = [], {}
    t = {"reference_products": len(result["outcomes"]), "products_built": 0, "in_force_products": 0,
         "current_only_products": 0, "products_not_built": 0, "decisions_listed": 0, "annexes_en_listed": 0,
         "annexes_fetched": 0, "annexes_fetched_identical_file": 0, "annexes_converted": 0, "annexes_skipped": 0,
         "annexes_not_fetched": 0, "commits": len(result["hashes"]), "molecules_in_force": 0,
         "multi_smpc_annexes": 0, "annexes_with_warnings": 0, "corrigendum_commits": 0,
         "corrigendum_commits_published_after_decision_date": 0, "decisions_with_several_commits": 0,
         "commits_in_decisions_with_several_commits": 0}
    for o in result["outcomes"]:
        src = o["src"] or {}
        ann = src.get("annexes", [])
        pc = src.get("procedure_counts") or {}
        not_fetched = [a for a in ann if a.get("status") not in CONVERT_STATUSES]
        en_listed = sum(1 for a in ann if "en" in a.get("annex_languages", []))
        fetched = [a for a in ann if a.get("status") in CONVERT_STATUSES and a.get("sha256")]
        in_force = [c for c in o["commits"] if c["kind"] == "annex"]
        per_decision: dict[str, int] = {}
        for c in in_force:
            a = c["annex"]
            per_decision[a["decision_number"]] = per_decision.get(a["decision_number"], 0) + 1
            if c.get("corrigendum_of"):
                t["corrigendum_commits"] += 1
                t["corrigendum_commits_published_after_decision_date"] += bool(
                    a.get("closed") and a["closed"] > a["decision_date"])
        shared = [n for n in per_decision.values() if n > 1]
        t["decisions_with_several_commits"] += len(shared)
        t["commits_in_decisions_with_several_commits"] += sum(shared)
        skip: dict[str, int] = {}
        for s in o["skipped"]:
            skip[s["status"]] = skip.get(s["status"], 0) + 1
            reasons[s["status"]] = reasons.get(s["status"], 0) + 1
        for a in not_fetched:
            key = "not fetched: " + re.sub(r"\s*\(annex languages:.*\)", "", a.get("reason") or a.get("status"))
            skip[key] = skip.get(key, 0) + 1
            reasons[key] = reasons.get(key, 0) + 1
        for a in fetched:
            r = result["conversions"][a["sha256"]]
            t["multi_smpc_annexes"] += r["smpcs"] > 1
            t["annexes_with_warnings"] += bool(r["warnings"])
        t["decisions_listed"] += pc.get("with_decision", 0)
        t["annexes_en_listed"] += en_listed
        t["annexes_fetched"] += len(fetched)
        t["annexes_fetched_identical_file"] += sum(1 for a in fetched if a["status"] == "duplicate")
        t["annexes_converted"] += len(in_force)
        t["annexes_skipped"] += len(o["skipped"])
        t["annexes_not_fetched"] += len(not_fetched)
        built = bool(o["commits"])
        t["products_built"] += built
        t["products_not_built"] += not built
        t["in_force_products"] += o["history_type"] == HISTORY_IN_FORCE
        t["current_only_products"] += o["history_type"] == HISTORY_CURRENT_ONLY
        rows.append({"inn": o["inn"], "dir": o["ref"]["dir"], "name": o["ref"]["name"],
                     "eu_number": o["ref"].get("eu_number"), "history_type": o["history_type"],
                     "record_from": o["record_from"], "record_to": o["record_to"],
                     "decisions_listed": pc.get("with_decision", 0), "annex_entries": len(ann),
                     "annexes_en_listed": en_listed, "annexes_fetched": len(fetched),
                     "annexes_converted": len(in_force), "commits": len(o["commits"]),
                     "skipped": dict(sorted(skip.items())), "problem": o["problem"]})
    t["molecules_in_force"] = len({o["inn"] for o in result["outcomes"] if o["history_type"] == HISTORY_IN_FORCE})
    t["record_from"] = min((o["record_from"] for o in result["outcomes"] if o["record_from"]), default=None)
    # The newest date the product pages were read: not a record end (the record ends at the newest decision
    # date in the repo, which manifest.json carries as corpora.eu.record_to).
    t["register_read"] = max((o["record_to"] for o in result["outcomes"] if o["record_to"]), default=None)
    return {"totals": t, "skip_reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            "skip_reason_text": SKIP_REASONS, "products": rows}


def repo_stats(out: Path, repack: bool) -> dict:
    stats = {"git_dir_bytes_fast_import": dir_size(out / ".git"), "worktree_files": 0}
    stats["worktree_files"] = sum(1 for p in out.rglob("*") if p.is_file() and ".git" not in p.parts)
    if repack:
        t = time.monotonic()
        build.git(out, "repack", "-adf", "--depth=50", "--window=250")
        build.git(out, "prune-packed")
        stats["git_dir_bytes_repacked"] = dir_size(out / ".git")
        stats["repack_seconds"] = round(time.monotonic() - t, 1)
    return stats


def coverage_markdown(report: dict) -> str:
    t, b = report["totals"], report["build"]
    mb = lambda n: f"{n / 1e6:,.1f} MB" if n is not None else "not measured"  # noqa: E731
    lines = [
        "# EU corpus coverage", "",
        f"Generated {report['generated']} by `python3 -I tools/eu.py build`. Converter {b['converter']}. Scope: {SCOPE}.", "",
        "## Totals", "",
        "| Measure | Value |", "| --- | ---: |",
        f"| Reference products | {t['reference_products']} |",
        f"| Products built | {t['products_built']} |",
        f"| In-force products (Union Register annexes) | {t['in_force_products']} |",
        f"| Current-only products (captured EMA product information) | {t['current_only_products']} |",
        f"| Products not built | {t['products_not_built']} |",
        f"| Molecules with in-force history | {t['molecules_in_force']} |",
        f"| Commission decisions listed | {t['decisions_listed']:,} |",
        f"| English annexes listed | {t['annexes_en_listed']:,} |",
        f"| English annexes fetched | {t['annexes_fetched']:,} |",
        f"| of which byte-identical to an earlier procedure's annex | {t['annexes_fetched_identical_file']:,} |",
        f"| Annexes converted and committed | {t['annexes_converted']:,} |",
        f"| Fetched annexes skipped after conversion | {t['annexes_skipped']:,} |",
        f"| Annex entries not fetched (logged in the fetch step) | {t['annexes_not_fetched']:,} |",
        f"| Annexes with more than one SmPC | {t['multi_smpc_annexes']:,} |",
        f"| Commits | {t['commits']:,} |",
        f"| Corrigendum commits (trailer Corrigendum-Of) | {t['corrigendum_commits']:,} |",
        f"| of which published (Procedure-Closed) after their decision date | "
        f"{t['corrigendum_commits_published_after_decision_date']:,} |",
        f"| Decisions (EU number, decision number) with more than one commit | "
        f"{t['decisions_with_several_commits']:,} ({t['commits_in_decisions_with_several_commits']:,} commits) |",
        f"| Earliest decision date | {t['record_from']} |",
        f"| Product pages last read (not the record end, which is the newest decision date) | "
        f"{t.get('register_read') or t.get('record_to')} |", "",
        "Decisions listed counts procedures on the product pages that carry a Commission decision. English annexes listed counts annex entries whose language list includes English; corrigenda and annexes without an English file are counted under annex entries not fetched. An annex byte-identical to an earlier one is still committed for its own decision, because it restores that text on its decision date.", "",
        "## Skipped annexes", "",
        "| Reason | Annexes |", "| --- | ---: |"]
    for key, n in sorted(report["skip_reasons"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {SKIP_REASONS.get(key, key)} | {n:,} |")
    empty_cache = _from_empty_cache(b)
    converted = b.get("converted_this_run")
    run_label = ("from an empty conversion cache" if empty_cache else
                 "on a warm conversion cache" + (f", {converted:,} annexes converted" if converted is not None else ""))
    lines += ["", "## Build", "",
              "| Measure | Value |", "| --- | ---: |",
              f"| Build time this run ({run_label}) | {b['seconds']['total']} s |"]
    if not empty_cache and b.get("seconds_from_scratch") is not None:
        lines.append(f"| Build time from an empty conversion cache (earlier run, same HEAD) | {b['seconds_from_scratch']} s |")
    lines += [f"| Conversion workers | {b['workers']} |",
              f"| Commits | {b['commits']:,} |",
              f"| HEAD | `{b['head']}` |",
              f"| .git after fast-import | {mb(b['repo']['git_dir_bytes_fast_import'])} |",
              f"| .git after `git repack -adf --depth=50 --window=250` | {mb(b['repo'].get('git_dir_bytes_repacked'))} |",
              f"| Files in the working tree | {b['repo']['worktree_files']:,} |",
              f"| Peak memory, build process | {mb(b['peak_memory']['build_process'])} |",
              f"| Peak memory, largest child (converter or git) | {mb(b['peak_memory']['largest_child'])} |"]
    rb = report.get("rebuild")
    if rb:
        lines += [f"| Rebuild from scratch ({rb['workers']} workers, fresh cache) | "
                  f"{'identical hashes' if rb['identical'] else 'HASHES DIFFER'} ({rb['commits']:,} commits, "
                  f"{rb['seconds']} s) |"]
    if t["molecules_in_force"] < 100:
        lines += ["", f"Only {t['molecules_in_force']} molecules have in-force history, {100 - t['molecules_in_force']} short of the target of 100."]
    lines += ["", "## Products", "",
              f"One row per reference product ({len(report['products']):,}) is in `reports/eu-coverage.json` under `products`: "
              "molecule, product, EU number, history type, earliest decision date (`record_from`), the date its product "
              "page was read (`record_to`, not a record end), decisions and English annexes listed, annexes fetched and "
              "converted, commits, skip reasons and any problem."]
    problems = [r for r in report["products"] if r.get("problem") or not r.get("commits")]
    if problems:
        lines += ["", "Products with a problem or no commit:", "",
                  "| Molecule | Product | History | Commits | Problem |", "| --- | --- | --- | ---: | --- |"]
        lines += [f"| {r['inn']} | {r['name']} ({r['dir']}) | {r['history_type']} | {r['commits']} | {r.get('problem') or 'none'} |"
                  for r in problems]
    return "\n".join(lines) + "\n"


def _from_empty_cache(build_info: dict) -> bool:
    """Whether the build this report describes converted every annex itself (started from an empty conversion
    cache). Older reports carry no conversion_cache field: a run that converted nothing was on a warm cache."""
    if "conversion_cache" in build_info:
        return build_info["conversion_cache"] == "empty"
    return bool(build_info.get("converted_this_run")) and \
        build_info.get("seconds_from_scratch") == build_info["seconds"]["total"]


def clear_conversion_cache(cache_root: Path) -> list[Path]:
    """Delete the converter result folders (qrd<n>-pymupdf<version>) under cache_root and nothing else;
    returns what was deleted."""
    gone = []
    if cache_root.is_dir():
        for sub in sorted(cache_root.iterdir()):
            if sub.is_dir() and re.fullmatch(r"qrd[0-9A-Za-z.]+-pymupdf[0-9A-Za-z.]+", sub.name):
                shutil.rmtree(sub)
                gone.append(sub)
    return gone


def cmd_build(args: argparse.Namespace) -> int:
    out = Path(args.out)
    cache_root = Path(args.cache) if args.cache else CONVERTED
    if args.append:
        if args.clean or args.fresh_cache:
            raise EUError("--append excludes --clean and --fresh-cache")
        res = append_eu(out, cache_root=cache_root, workers=args.workers)
        head = build.git(out, "rev-parse", "main").strip()
        print(f"appended {len(res['hashes'])} commits ({len(res['skipped'])} annexes skipped: no Annex I change "
              f"or not convertible); HEAD {head}")
        return 0
    if args.fresh_cache:
        clear_conversion_cache(cache_root)
    result = build_eu(out, clean=args.clean, workers=args.workers, cache_root=cache_root)
    if not args.report:
        print(f"HEAD {result['hashes'][-1] if result['hashes'] else 'none'}")
        return 0
    cov = coverage_eu(result)
    build_info = {"converter": converter_label(result["ident"]), "workers": result["workers"],
                  "commits": len(result["hashes"]), "head": result["hashes"][-1] if result["hashes"] else None,
                  "seconds": result["seconds"], "converted_this_run": result["converted_this_run"],
                  "peak_memory": peak_memory(), "repo": repo_stats(out, repack=args.repack),
                  "problems": result["problems"]}
    from_scratch = result["converted_this_run"] == len(result["conversions"])
    build_info["conversion_cache"] = "empty" if from_scratch else "warm"
    if from_scratch:
        build_info["seconds_from_scratch"] = result["seconds"]["total"]
    report = {"generated": utc_now(), "build": build_info, **cov,
              "hashes_sha256": hashlib.sha256("\n".join(result["hashes"]).encode()).hexdigest()}
    old = REPORTS / "eu-coverage.json"
    if old.exists():
        prev = read_json(old)
        if prev.get("rebuild") and prev["rebuild"].get("head") == build_info["head"]:
            report["rebuild"] = prev["rebuild"]
        if "seconds_from_scratch" not in build_info and prev.get("build", {}).get("seconds_from_scratch") \
                and prev["build"].get("head") == build_info["head"]:
            build_info["seconds_from_scratch"] = prev["build"]["seconds_from_scratch"]
    write_json(REPORTS / "eu-coverage.json", report)
    (REPORTS / "eu-coverage.md").write_text(coverage_markdown(report), encoding="utf-8")
    sync_products(result["outcomes"])
    print(f"written reports/eu-coverage.json and .md; {cov['totals']['molecules_in_force']} molecules in force")
    return 0


def cmd_rebuild_check(args: argparse.Namespace) -> int:
    """Build again into a temporary repo from an empty conversion cache and compare every commit hash."""
    base = Path(args.out)
    want = build.git(base, "rev-list", "--reverse", "main").split()
    scratch = Path(tempfile.mkdtemp(prefix="eu-rebuild-", dir=args.tmp))
    try:
        result = build_eu(scratch / "repo", workers=args.workers, cache_root=scratch / "cache")
        got = result["hashes"]
        identical = got == want
        first_diff = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), None)
        check = {"checked": utc_now(), "identical": identical, "commits": len(got), "base_commits": len(want),
                 "head": got[-1] if got else None, "workers": args.workers, "seconds": result["seconds"]["total"],
                 "first_difference": first_diff, "peak_memory": peak_memory()}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    path = REPORTS / "eu-coverage.json"
    if path.exists():
        report = read_json(path)
        report["rebuild"] = check
        write_json(path, report)
        (REPORTS / "eu-coverage.md").write_text(coverage_markdown(report), encoding="utf-8")
    print(json.dumps(check, indent=2))
    return 0 if identical else 1


def overlap_candidates(eu_repo: Path = BUILD_EU, us_sources: Path = ROOT / "sources" / "us",
                       us_repo: Path = ROOT / "build" / "us", min_eu: int = 10, min_us: int = 10) -> dict:
    """Molecules in both build/us and build/eu, with version counts per side (candidates for a
    cross-jurisdiction comparison with a long history on both sides).

    US counts come from sources/us/*.json (pinned versions per setid) for molecules whose folder is in
    build/us HEAD; EU counts are the commits of the molecule's reference product in build/eu."""
    us_dirs = (set(build.git(us_repo, "ls-tree", "-d", "--name-only", "HEAD").split())
               if (us_repo / ".git").exists() else set())
    us: dict[str, dict] = {}
    for path in sorted(us_sources.glob("*.json")):
        src = read_json(path)
        inn = src.get("inn")
        if inn not in us_dirs:
            continue
        row = us.setdefault(inn, {"setids": 0, "versions": 0, "top_setid": None, "top_setid_versions": 0,
                                  "top_setid_title": None})
        n = src.get("versions") or 0
        n = n if isinstance(n, int) else len(n)
        row["setids"] += 1
        row["versions"] += n
        if n > row["top_setid_versions"]:
            row.update(top_setid=src["setid"], top_setid_versions=n, top_setid_title=src.get("history_title"))
    log = build.git(eu_repo, "log", "--format=tformat:@@C", "--name-only", "main")
    eu_counts: dict[str, set] = {}
    commit = 0
    for line in log.splitlines():
        if line == "@@C":
            commit += 1
        elif "/" in line:
            inn = line.split("/", 1)[0]
            eu_counts.setdefault(inn, set()).add(commit)
    rows = []
    for inn in sorted(set(us) & set(eu_counts)):
        meta = json.loads(build.git(eu_repo, "show", f"HEAD:{inn}/" + next(
            p for p in build.git(eu_repo, "ls-tree", "--name-only", f"HEAD:{inn}").split()) + "/product.json"))
        rows.append({"molecule": inn, "us_setids": us[inn]["setids"], "us_versions": us[inn]["versions"],
                     "us_top_setid": us[inn]["top_setid"], "us_top_setid_versions": us[inn]["top_setid_versions"],
                     "us_top_setid_title": us[inn]["top_setid_title"], "eu_product": meta["name"],
                     "eu_dir": meta["ema_product_number"], "eu_versions": len(eu_counts[inn]),
                     "eu_history_type": meta["history_type"],
                     "eu_record_from": product_windows(eu_repo)[meta["ema_product_number"].replace("/", "-")]["record_from"],
                     "rich_both": len(eu_counts[inn]) >= min_eu and us[inn]["top_setid_versions"] >= min_us})
    rows.sort(key=lambda r: (-min(r["eu_versions"], r["us_top_setid_versions"]), r["molecule"]))
    return {"generated": utc_now(), "rule": f"molecule folder present in build/us HEAD and build/eu HEAD; eu_versions "
            f"counts build/eu commits that changed at least one file of the molecule (a decision whose converted "
            f"text equals the previous one changes nothing); us_versions counts pinned versions over all setids "
            f"in sources/us; rich_both "
            f"when the EU reference product has at least {min_eu} versions and the US setid with the most versions "
            f"has at least {min_us}; sorted by the smaller of those two counts",
            "molecules_in_both": len(rows), "rich_both": sum(r["rich_both"] for r in rows), "molecules": rows}


def cmd_candidates(args: argparse.Namespace) -> int:
    report = overlap_candidates()
    write_json(REPORTS / "eu-us-overlap.json", report)
    print(f"{report['molecules_in_both']} molecules in both repos, {report['rich_both']} rich on both sides")
    for r in report["molecules"][:args.top]:
        print(f"  {r['molecule']}: EU {r['eu_product']} {r['eu_versions']} versions; US {r['us_setids']} setids, "
              f"{r['us_versions']} versions, top setid {r['us_top_setid_versions']}")
    return 0


# ---------------------------------------------------------------- crosswalk (molecules/<inn>.json)

CROSSWALK_AGREE = ("history_type", "record_from", "versions")   # build/eu (product.json and commits) vs sources
EU_ENTRY_FIELDS = ("label_id", "ema_product_number", "eu_number", "name", "mah", "role", "generic", "biosimilar",
                   "medicine_status", "marketing_authorisation_date", "medicine_url", "built", "history_type",
                   "record_from", "record_to", "versions", "comparable_to_us", "comparability_note")
BUILT_HISTORY_TYPES = (HISTORY_IN_FORCE, HISTORY_CURRENT_ONLY, "replayed")


def built_products(eu_repo: Path) -> dict[str, dict] | None:
    """{EMEA-H-C-nnnnnn: product.json at main plus ``versions`` and ``record_from`` from the product's commits}
    of an EU repo, or None when the repo is absent. Reads every product.json in one git cat-file --batch
    call; no PyMuPDF, no network."""
    if not (eu_repo / ".git").exists():
        return None
    paths = [p for p in build.git(eu_repo, "ls-tree", "-r", "--name-only", "main").splitlines()
             if p.endswith("/product.json")]
    proc = subprocess.run(["git", "-C", str(eu_repo), "cat-file", "--batch"], capture_output=True,
                          input="".join(f"main:{p}\n" for p in paths).encode(), env=build.git_env(eu_repo))
    if proc.returncode != 0:
        raise EUError(f"git cat-file --batch in {eu_repo} failed: {proc.stderr.decode(errors='replace').strip()}")
    out, data, pos = {}, proc.stdout, 0
    for path in paths:
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise EUError(f"{eu_repo}: main:{path} is not a blob ({data[pos:end].decode(errors='replace')})")
        size = int(header[2])
        meta = json.loads(data[end + 1:end + 1 + size])
        pos = end + 1 + size + 1
        inn, label_id = path.split("/")[:2]
        if label_id in out:
            raise EUError(f"{eu_repo}: product {label_id} appears under two molecule folders")
        out[label_id] = dict(meta, _inn_folder=inn)
    windows = product_windows(eu_repo)
    for label_id, meta in out.items():
        w = windows.get(label_id) or {}
        meta["versions"] = w.get("versions", 0)
        meta["record_from"] = w.get("record_from")
        meta["last_effective"] = w.get("last_effective")
    return out


def product_windows(eu_repo: Path) -> dict[str, dict]:
    """{product folder: {versions, record_from, last_effective}} from the commits on main (author e-mail and
    Effective-Date trailer)."""
    log = build.git(eu_repo, "log", "--reverse", "--format=%ae%x00%(trailers:key=Effective-Date,valueonly,separator=)",
                    "main")
    out: dict[str, dict] = {}
    for line in log.splitlines():
        if "\x00" not in line:
            continue
        email, day = line.split("\x00", 1)
        folder, day = email.split("@", 1)[0].split(".", 1)[0], day.strip()
        w = out.setdefault(folder, {"versions": 0, "record_from": day, "last_effective": day})
        w["versions"] += 1
        w["record_from"] = min(w["record_from"], day)
        w["last_effective"] = max(w["last_effective"], day)
    return out


def sync_after_append(eu_repo: Path = BUILD_EU, products_path: Path = PRODUCTS) -> list[str]:
    """After build --append: build_versions and record_from of every built reference product in
    sources/eu-products.json from the commits of eu_repo (what a full build's sync_products would write;
    record_to stays the product page's read date). Returns the product folders that changed; run crosswalk()
    next so molecules/<inn>.json agrees."""
    windows = product_windows(eu_repo)
    products = load_products(products_path)
    changed = []
    for entry in products["molecules"].values():
        ref = entry.get("reference")
        w = windows.get(ref["dir"]) if ref else None
        if not w or ref.get("build_versions") is None:
            continue
        if (ref.get("build_versions"), ref.get("record_from")) != (w["versions"], w["record_from"]):
            ref.update(build_versions=w["versions"], record_from=w["record_from"],
                       history_note=f"{w['versions']} converted English annexes committed to build/eu from the "
                                    "Union Register")
            changed.append(ref["dir"])
    if changed:
        write_json(products_path, products)
    return sorted(changed)


def _yes(value) -> bool:
    return str(value).strip().lower() == "yes" if not isinstance(value, bool) else value


def _eu_entry(p: dict, role: str) -> dict:
    ema = canonical_ema(p["ema_product_number"])
    return {"label_id": ema_dir(ema), "ema_product_number": ema, "eu_number": p.get("eu_number"),
            "name": p.get("name"), "mah": p.get("mah"), "role": role, "generic": _yes(p.get("generic")),
            "biosimilar": _yes(p.get("biosimilar")), "medicine_status": p.get("medicine_status"),
            "marketing_authorisation_date": p.get("marketing_authorisation_date"),
            "medicine_url": p.get("medicine_url"), "built": False, "history_type": "not-built",
            "record_from": None, "record_to": None, "versions": None, "comparable_to_us": None,
            "comparability_note": None}


def products_rule_name(rule: str | None) -> str:
    """The short selection-rule name recorded in eu_selection.rule (files from before rule names: exact-inn-v1)."""
    return rule if rule and len(rule) <= 60 else "exact-inn-v1"


def crosswalk_fields(inn: str, entry: dict, built: dict[str, dict] | None, products_rule: str | None = None,
                     removed: set[str] | None = None) -> tuple[list[dict], dict, list[str]]:
    """(eu list, eu_selection object, problems) of one molecule from its sources/eu-products.json entry.

    built is built_products() of build/eu, or None when the repo is absent; then the reference's build fields
    come from the products file (written back by tools/eu.py build). A problem is a disagreement between
    build/eu and the products file on CROSSWALK_AGREE, or a built product missing from build/eu. A reference
    removed on request (``removed``, default sources/removed.json) is written as built false, removed true."""
    removed = build.removed_ids("eu") if removed is None else set(removed)
    problems: list[str] = []
    eu_list: list[dict] = []
    ref = entry.get("reference")
    if ref:
        e = _eu_entry(ref, "reference")
        e["comparable_to_us"] = ref.get("comparable_to_us")
        e["comparability_note"] = ref.get("comparability_note")
        src_versions = ref.get("build_versions")
        src_built = ref.get("history_type") in BUILT_HISTORY_TYPES and bool(src_versions)
        if e["label_id"] in removed:
            e["removed"] = True     # built stays False; sources/eu-products.json keeps its last build fields
        elif built is None:
            if src_built:
                e.update(built=True, history_type=ref["history_type"], record_from=ref.get("record_from"),
                         record_to=ref.get("record_to"), versions=src_versions)
        else:
            meta = built.get(e["label_id"])
            if meta is None:
                if src_built:
                    problems.append(f"{inn} {e['label_id']}: sources/eu-products.json says built "
                                    f"({ref['history_type']}, {src_versions} versions) but build/eu has no product.json")
            else:
                src = {"history_type": ref.get("history_type"), "record_from": ref.get("record_from"),
                       "versions": src_versions}
                for k in CROSSWALK_AGREE:
                    if meta.get(k) != src[k]:
                        problems.append(f"{inn} {e['label_id']}: {k} is {meta.get(k)!r} in build/eu product.json "
                                        f"but {src[k]!r} in sources/eu-products.json")
                if meta.get("_inn_folder") != inn or meta.get("molecule") != inn:
                    problems.append(f"{inn} {e['label_id']}: build/eu files it under molecule "
                                    f"{meta.get('_inn_folder')!r} (product.json molecule {meta.get('molecule')!r})")
                e.update(built=True, history_type=meta.get("history_type"), record_from=meta.get("record_from"),
                         record_to=ref.get("record_to"), versions=meta.get("versions"))
        eu_list.append(e)
    eu_list += [_eu_entry(a, "alternate") for a in
                sorted(entry.get("alternates") or [], key=lambda a: canonical_ema(a["ema_product_number"]))]
    selection = {
        "rule": products_rule_name(products_rule),
        "reference_label_id": eu_list[0]["label_id"] if ref else None,
        "reference_selected_by": (ref or {}).get("selected_by", "exact-inn") if ref else None,
        "no_reference_reason": entry.get("no_reference_reason"),
        "combinations_excluded": len(entry.get("combinations") or []),
        "near_misses_not_matched": [{"name": n.get("name"), "ema_product_number": n.get("ema_product_number"),
                                     "kind": n.get("kind")} for n in entry.get("near_misses_not_matched") or []],
    }
    return eu_list, selection, problems


def molecule_text(mol: dict) -> str:
    """The molecules/<inn>.json writer's format (tools/select_molecules.py): indent 2, sorted keys, newline."""
    return json.dumps(mol, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def crosswalk(molecules_dir: Path = MOLECULES, products_path: Path = PRODUCTS, eu_repo: Path = BUILD_EU,
              write: bool = True) -> dict:
    """Derive eu and eu_selection of every molecules/<inn>.json from sources/eu-products.json (and build/eu
    product.json when the repo exists). Writes only the files whose text changes, and nothing at all when a
    problem is found or write is False. Returns counts, changed file names and problems."""
    products = load_products(products_path)
    selection = products.get("molecules") or {}
    built = built_products(eu_repo)
    removed = build.removed_ids("eu")
    files = sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json")
    if not files:
        raise EUError(f"no molecule files in {molecules_dir}")
    problems: list[str] = []
    planned: list[tuple[Path, str]] = []
    seen: set[str] = set()
    counts = collections.Counter()
    for path in files:
        raw = path.read_text(encoding="utf-8")
        mol = json.loads(raw)
        inn = mol.get("inn") or path.stem
        entry = selection.get(inn)
        if entry is None:
            problems.append(f"{path.name}: molecule {inn!r} is not in {products_path.name}; run tools/eu.py discover")
            continue
        seen.add(inn)
        eu_list, sel, probs = crosswalk_fields(inn, entry, built, products.get("rule"), removed)
        problems += probs
        counts["molecules"] += 1
        counts["references"] += sum(1 for e in eu_list if e["role"] == "reference")
        counts["references_built"] += sum(1 for e in eu_list if e["role"] == "reference" and e["built"])
        counts["alternates"] += sum(1 for e in eu_list if e["role"] == "alternate")
        if sel["no_reference_reason"]:
            counts["no_reference_reason"] += 1
            if sel["no_reference_reason"].startswith("only combination central products"):
                counts["no_reference_combination_only"] += 1
            if any(k in sel["no_reference_reason"] for k in ("not matched by the v1 rule", "not matched by the exact-INN rule")):
                counts["no_reference_salt_or_alias_originator"] += 1
        text = molecule_text(dict(mol, eu=eu_list, eu_selection=sel))
        if text != raw:
            planned.append((path, text))
    missing = sorted(set(selection) - seen)
    if missing:
        problems.append(f"{len(missing)} molecules of {products_path.name} have no file in {molecules_dir}: "
                        + ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""))
    if built is not None:
        refs = {ema_dir(e["reference"]["ema_product_number"]) for e in selection.values() if e.get("reference")}
        extra = sorted(set(built) - refs)
        if extra:
            problems.append(f"build/eu has products that are no reference in {products_path.name}: " + ", ".join(extra))
    if write and not problems:
        for path, text in planned:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
    return {"counts": dict(sorted(counts.items())), "changed": [p.name for p, _ in planned],
            "written": bool(write and not problems), "problems": problems, "eu_repo": built is not None,
            "built_products": len(built) if built is not None else None}


def cmd_crosswalk(args: argparse.Namespace) -> int:
    res = crosswalk(Path(args.molecules), Path(args.products), Path(args.eu_repo), write=not args.check)
    c = res["counts"]
    for p in res["problems"]:
        print(f"PROBLEM {p}", file=sys.stderr)
    if res["problems"]:
        print(f"error: {len(res['problems'])} problems; nothing written", file=sys.stderr)
        return 2
    source = (f"build/eu product.json of {res['built_products']} products" if res["eu_repo"]
              else "sources/eu-products.json only (no build/eu)")
    print(f"{c.get('molecules', 0)} molecules: {c.get('references', 0)} references "
          f"({c.get('references_built', 0)} built), {c.get('alternates', 0)} alternates, "
          f"{c.get('no_reference_reason', 0)} without a reference ({c.get('no_reference_combination_only', 0)} "
          f"combination-only, {c.get('no_reference_salt_or_alias_originator', 0)} with a salt or alias originator); "
          f"build fields from {source}")
    if args.check:
        for name in res["changed"]:
            print(f"would change {name}")
        print(f"{len(res['changed'])} files would change")
        return 1 if res["changed"] else 0
    print(f"{len(res['changed'])} files written")
    return 0


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover", help="select products and map EU numbers")
    p.add_argument("--molecules", default=str(MOLECULES))
    p.add_argument("--refresh", action="store_true", help="download the inputs again instead of the pinned copies")
    p.add_argument("--offline", action="store_true", help="use only pinned, cached inputs")
    p.set_defaults(func=cmd_discover)
    p = sub.add_parser("fetch", help="Union Register pages and English annexes of every reference product")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", nargs="*", help="EMEA-H-C-nnnnnn folders or molecule inns")
    p.add_argument("--retry-logged", action="store_true", help="retry annexes logged with an HTTP error")
    p.add_argument("--refresh", action="store_true", help="download the product pages again")
    p.add_argument("--offline", action="store_true", help="parse cached product pages only, no requests")
    p.add_argument("--append-batch", metavar="YYYY-MM-DD",
                   help="write this batch date as 'added' into every annex pinned now (then: build --append)")
    p.add_argument("--trust-pins", action="store_true",
                   help="count pinned annexes as fetched without their PDF on disk (the daily job, no raw store)")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("pages", help="page count and text-layer check of fetched annexes (.venv-eu PyMuPDF)")
    p.add_argument("--all", action="store_true", help="recheck annexes already counted")
    p.set_defaults(func=cmd_pages)
    p = sub.add_parser("coverage", help="write reports/eu-fetch.json")
    p.set_defaults(func=cmd_coverage)
    p = sub.add_parser("capture", help="EMA forward capture into sources/eu-capture/<date>.json")
    p.add_argument("--date", help="capture date (default: today, UTC)")
    p.set_defaults(func=cmd_capture)
    p = sub.add_parser("build", help="convert every pinned English annex (.venv-eu) and write build/eu with git fast-import")
    p.add_argument("--out", default=str(BUILD_EU))
    p.add_argument("--clean", action="store_true", help="delete and rebuild an existing build/eu")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                   help="converter processes (the output does not depend on this)")
    p.add_argument("--cache", help="conversion cache root (default raw/eu/converted)")
    p.add_argument("--fresh-cache", action="store_true", help="delete the conversion cache first (build from scratch)")
    p.add_argument("--no-report", dest="report", action="store_false",
                   help="do not write reports/eu-coverage.* or sync sources/eu-products.json")
    p.add_argument("--no-repack", dest="repack", action="store_false", help="skip the repack size measurement")
    p.add_argument("--append", action="store_true",
                   help="append only the pinned annexes HEAD lacks (each needs an 'added' batch date)")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("rebuild-check", help="rebuild from an empty cache into a temporary repo and compare hashes")
    p.add_argument("--out", default=str(BUILD_EU), help="the repo to compare against")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--tmp", help="parent directory for the temporary repo and cache")
    p.set_defaults(func=cmd_rebuild_check)
    p = sub.add_parser("candidates", help="molecules in both build/us and build/eu, with version counts per side -> reports/eu-us-overlap.json")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_candidates)
    p = sub.add_parser("crosswalk", help="write eu and eu_selection into every molecules/<inn>.json (offline)")
    p.add_argument("--molecules", default=str(MOLECULES))
    p.add_argument("--products", default=str(PRODUCTS))
    p.add_argument("--eu-repo", default=str(BUILD_EU), help="built EU repo whose product.json files are checked")
    p.add_argument("--check", action="store_true", help="print the files that would change; exit 1 if any, write nothing")
    p.set_defaults(func=cmd_crosswalk)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except build.BuildError as err:   # EUError and the git/fetch errors of build.py
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
