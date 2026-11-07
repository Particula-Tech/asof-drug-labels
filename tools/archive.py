#!/usr/bin/env python3
"""Find retired DailyMed setids: the older labels of the selected products.

DailyMed's /spls list and getFile.cfm cover current setids only. When a product
changes holder, or its company re-issues the label under a new setid, the old
setid is retired and its versions survive only in DailyMed's archive
(archives/index.cfm). This tool searches that archive by brand name, resolves
the rows that belong neither to a repackager nor to a setid we already have,
and keeps the retired setids that carry one of the molecule's own NDA or BLA
application numbers.

  discover  network, every page cached under raw/archive/ (a rerun makes no
            requests for pages already saved); writes reports/archive-discovery.json
  apply     offline: decide which retired setids to keep, pin the decisions in
            sources/retired-setids.json and merge the kept setids into
            molecules/<inn>.json, so build.py fetch-all fetches their versions
            through getArchivalFile.cfm
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import threading
import time
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import select_molecules as sm  # noqa: E402

ROOT = build.ROOT
ARCHIVE = build.RAW / "archive"
DRUGSFDA_URL = "https://download.open.fda.gov/drug/drugsfda/drug-drugsfda-0001-of-0001.json.zip"
DRUGSFDA_DIR = build.RAW / "m2" / "openfda" / "drugsfda"
DRUGSFDA = DRUGSFDA_DIR / "drug-drugsfda-0001-of-0001.json"
DISCOVERY = build.REPORTS / "archive-discovery.json"
RETIRED = ROOT / "sources" / "retired-setids.json"
ARCHIVE_SOURCE = "dailymed-archive"
PAGE_SIZE = 20                # the archive ignores anything larger
MAX_PAGES_PER_TERM = 150      # 3,000 rows; a term above this is logged as skipped, never silently cut
MAX_RESOLVES_PER_TERM = 60    # setid lookups per search term before its leftover rows are counted as unresolved
INNOVATOR_PREFIXES = ("NDA", "BLA")
DRUGSFDA_STATUSES = {"Prescription", "Discontinued"}
MIN_FIRST_WORD = 4            # brand searched by its first word when that word is at least this long

# Labelers that only repackage or relabel. Used for names openFDA's current NDC
# directory no longer knows (defunct repackagers such as Physicians Total Care).
REPACKAGER_MARKERS = tuple(sm.norm(s) for s in """
REPACK|RELABEL|PHYSICIANS TOTAL CARE|PD RX|DIRECT RX|DIRECT_RX|BRYANT RANCH|A S MEDICATION|PROFICIENT RX|NUCARE|
PREFERRED PHARMACEUTICALS|QUALITY CARE|AIDAREX|LAKE ERIE MEDICAL|STAT RX|REBEL DISTRIBUTORS|DISPENSING SOLUTIONS|
UNIT DOSE SERVICES|AMERICAN HEALTH PACKAGING|CARDINAL HEALTH|MAJOR PHARMACEUTICALS|MCKESSON|DENTON PHARMA|HJ HARKINS|
H J HARKINS|MEDSOURCE|MEDICAL PARK PHARMACY|BLENHEIM|NORTHWIND|QPHARMA|LIBERTY PHARMACEUTICALS|KELTMAN|
CLINICAL SOLUTIONS WHOLESALE|ALTURA|GOLDEN STATE MEDICAL SUPPLY|SAFECOR|CONTRACT PHARMACY SERVICES|APOTHECA|
MED HEALTH PHARMA|ASCLEMED|CARILION|READYMEDS|LEGACY PHARMACEUTICAL PACKAGING|AVKARE|AVPAK|PRECISION DOSE|
NCS HEALTHCARE|LIBERTY|COUPLER|HENRY SCHEIN|MEDVANTX|PACKAGING|DOH CENTRAL PHARMACY|RXCHANGE|NORTHSTAR RX|
ST MARY S|UNIT DOSE|PHARMPAK|SPECTRUM PHARMACY|PHARMEDIUM|PRASCO MEDICAL|REDPHARM|E5 PHARMA|ADVANCED RX|
NUCARE PHARMACEUTICALS|MEDVANTX|PROPACK|AUROHEALTH|CAMBER CONSUMER|PHYSICIAN PARTNER|DIRECT PHARMACEUTICALS
""".replace("\n", "").split("|") if s.strip())


class ArchiveError(build.BuildError):
    pass


# ---------------------------------------------------------------- parsing

@dataclass(frozen=True)
class Row:
    """One archived label version as the archive search lists it."""
    posted: str        # ISO date; equals the /history published date
    archive_id: int
    title: str
    packager: str
    version: int


SEARCH_ROW = re.compile(
    r'(?s)<div class="desktop">.*?<div class="date">\s*([A-Z][a-z]{2} \d{1,2}, \d{4})'
    r'.*?getArchivalFile\.cfm\?archive_id=(\d+)'
    r'.*?<div class="desc">\s*<a [^>]*>(.*?)</a>'
    r'.*?Packager:\s*<span>(.*?)</span>'
    r'.*?Version:\s*<span>(\d+)</span>')
SEARCH_TOTAL = re.compile(r"\((\d[\d,]*) results?\)")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_search(page: str) -> tuple[int, list[Row]]:
    """Total result count and the rows of one archive search page."""
    rows = [Row(build.parse_published(d), int(a), _text(t), _text(p), int(v))
            for d, a, t, p, v in SEARCH_ROW.findall(page)]
    m = SEARCH_TOTAL.search(page)
    if m:
        return int(m.group(1).replace(",", "")), rows
    if rows:
        raise ArchiveError("archive page has rows but no result count; the page layout changed")
    return 0, rows


def page_text(page: str) -> str:
    page = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", page)
    return _text(page)


@dataclass
class Info:
    """What an archived version's drug-info page says about its label."""
    archive_id: int
    setid: str
    version: int
    labeler: str
    applications: list[str]
    product_ndcs: list[str]
    marketing_end_dates: list[str]
    title: str
    source_ndcs: list[str]
    repackaged: bool


def parse_info(page: str, archive_id: int) -> Info:
    """Labeler, applications and NDCs always; setid and version only on pages from about 2012 on
    (older pages lack the 'Set id' line, and the caller reads them from the archived file instead)."""
    text = page_text(page)
    setid = re.search(rf"Set id:\s*({UUID})", text, re.I)
    version = re.search(r"Set id:\s*\S+\s+Version:\s*(\d+)", text, re.I)
    labeler = re.search(r"Labeler\s*-\s*(.+?)\s*\(\d{9}\)", text)
    marketing = re.findall(
        r"\b(NDA AUTHORIZED GENERIC|NDA|BLA|ANDA|BLA AUTHORIZED GENERIC)\s+((?:NDA|BLA|ANDA)\d{6})"
        r"\s+(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}/\d{2}/\d{4}))?", text)
    # A repackaged product shows its own NDC followed by the source NDC in brackets:
    # "NDC:55154-2421(NDC:0071-0155)"; its labeler's business operations read REPACK(...) or RELABEL(...).
    own = sorted(set(re.findall(r"(?<!\()NDC:\s*(\d{4,5}-\d{3,4})(?![\d-])", text)))
    source = sorted(set(re.findall(r"\(NDC:\s*(\d{4,5}-\d{3,4})\)", text)))
    title = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    return Info(archive_id=archive_id, setid=setid.group(1).lower() if setid else "",
                version=int(version.group(1)) if version else 0,
                labeler=labeler.group(1).strip() if labeler else "",
                applications=sorted({m[1] for m in marketing}),
                product_ndcs=own,
                marketing_end_dates=sorted({sm.iso_date(m[3]) for m in marketing if m[3]}),
                title=_text(title.group(1)) if title else "",
                source_ndcs=source,
                repackaged=bool(source) or bool(re.search(r"\b(?:REPACK|RELABEL)\(", text)))


# ---------------------------------------------------------------- inputs

def brand_part(title: str) -> str:
    """'ZESTRIL (lisinopril) tablet' -> 'ZESTRIL'; the normalised product name before the generic name."""
    return sm.norm(title.split("(")[0])


def title_matches(title: str, term: str) -> bool:
    brand, key = brand_part(title), sm.norm(term)
    return brand == key or brand.startswith(key + " ")


def search_term(brand: str) -> str:
    """The query for a brand: its first word when that word is distinctive enough, else the whole name."""
    words = brand.strip().upper().split()
    if not words:
        return ""
    first = words[0].rstrip(",")
    return first if len(sm.norm(first).replace(" ", "")) >= MIN_FIRST_WORD else " ".join(words)


def load_drugsfda(path: Path = DRUGSFDA) -> list[dict]:
    if not path.is_file():
        raise ArchiveError(f"Drugs@FDA file not found: {path}; run: archive.py discover (it downloads it once)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results")
    if not isinstance(results, list):
        raise ArchiveError(f"{path} has no 'results' list")
    return results


def molecule_files(molecules_dir: Path) -> list[Path]:
    return sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json")


@dataclass
class Term:
    term: str
    molecules: set
    brands: set
    applications: set   # NDA/BLA numbers behind the brands, from the selection and Drugs@FDA
    # inn -> {"brands", "applications"}: one search word can cover brands of different molecules
    # (CIPRO and CIPRO HC), so keep decisions use the molecule's own share, never the union.
    by_molecule: dict = field(default_factory=dict)


def search_terms(molecules_dir: Path, drugsfda: list[dict]) -> list[Term]:
    """Brand names to search, per molecule: the NDA/BLA brands in the selection plus every
    single-ingredient NDA/BLA brand Drugs@FDA lists for the molecule (discontinued ones included).
    A brand that is just the generic name is skipped: searching it returns every generic."""
    by_moiety: dict[str, list[tuple[str, str]]] = defaultdict(list)   # moiety -> (brand, application)
    for app in drugsfda:
        number = str(app.get("application_number", ""))
        if not number.startswith(INNOVATOR_PREFIXES):
            continue
        for product in app.get("products") or []:
            if product.get("marketing_status") not in DRUGSFDA_STATUSES:
                continue
            ingredients = product.get("active_ingredients") or []
            moieties = {sm.moiety(str(i.get("name", ""))) for i in ingredients} - {""}
            if len(moieties) == 1 and product.get("brand_name"):
                by_moiety[moieties.pop()].append((str(product["brand_name"]), number))

    terms: dict[str, Term] = {}
    for path in molecule_files(molecules_dir):
        mol = json.loads(path.read_text(encoding="utf-8"))
        generic = {sm.norm(n) for n in mol.get("ingredient_names", [])} | {sm.norm(mol.get("name", ""))}
        pairs = [(b, a) for e in mol.get("us", []) if e.get("source") != ARCHIVE_SOURCE
                 and set(e.get("marketing_categories", [])) & set(INNOVATOR_PREFIXES)
                 for b in e.get("brand_names", []) for a in e.get("application_numbers", [])
                 if a.startswith(INNOVATOR_PREFIXES)]
        pairs += by_moiety.get(sm.norm(mol.get("name", "")), [])
        for brand, application in pairs:
            key = sm.norm(brand)
            if not key or any(key == g or key.startswith(g + " ") for g in generic if g):
                continue
            q = search_term(brand)
            if not q or sm.norm(q) in generic:
                continue
            t = terms.setdefault(sm.norm(q), Term(q, set(), set(), set()))
            t.molecules.add(mol["inn"])
            t.brands.add(key)
            t.applications.add(application)
            own = t.by_molecule.setdefault(mol["inn"], {"brands": set(), "applications": set()})
            own["brands"].add(key)
            own["applications"].add(application)
    return sorted(terms.values(), key=lambda t: sm.norm(t.term))


@dataclass
class LabelerIndex:
    """Approval holders vs repackagers, from openFDA's current NDC directory."""
    holder_codes: set
    repackager_codes: set
    holder_names: set
    repackager_names: set

    def code_kind(self, code: str) -> str:
        if code in self.holder_codes:
            return "holder"
        if code in self.repackager_codes:
            return "repackager"
        return "unknown"

    def name_kind(self, name: str) -> str:
        key = company_key(name)
        if key in self.holder_names:
            return "holder"
        if key in self.repackager_names or any(m in sm.norm(name) for m in REPACKAGER_MARKERS):
            return "repackager"
        return "unknown"


COMPANY_SUFFIXES = set("INC LLC LP LTD CORP CORPORATION CO COMPANY USA US PLC GMBH AG SA LIMITED THE OF DIV".split())


def company_key(name: str) -> str:
    return " ".join(w for w in sm.norm(name).split() if w not in COMPANY_SUFFIXES)


def labeler_index(ndc_records: list[dict]) -> LabelerIndex:
    harmonised: Counter = Counter()
    flagged: Counter = Counter()
    names: dict[str, set] = defaultdict(set)
    for record in ndc_records:
        code = str(record.get("product_ndc", "")).split("-")[0]
        if not code:
            continue
        names[code].add(company_key(str(record.get("labeler_name", ""))))
        openfda = record.get("openfda") or {}
        if openfda.get("spl_set_id"):
            harmonised[code] += 1
            if openfda.get("is_original_packager"):
                flagged[code] += 1
    holder = {c for c in harmonised if flagged[c] / harmonised[c] >= sm.ORIGINAL_PACKAGER_SHARE}
    repack = set(harmonised) - holder
    holder_names = {n for c in holder for n in names[c]}
    repack_names = {n for c in repack for n in names[c]} - holder_names
    return LabelerIndex(holder, repack, holder_names, repack_names)


@dataclass
class KnownVersions:
    """Archive rows that belong to setids we already fetch, matched on (version, date, labeler)."""
    by_version_date: dict

    def owns(self, row: Row) -> bool:
        for labeler in self.by_version_date.get((row.version, row.posted), ()):
            if company_key(row.packager).split()[:1] == labeler.split()[:1]:
                return True
        return False


def known_versions(sources_dir: Path) -> tuple[KnownVersions, set]:
    index: dict = defaultdict(set)
    current: set = set()
    for path in sources_dir.glob("*.json"):
        src = build.load_sources(path)
        title = src.get("history_title") or ""
        labeler = company_key(title[title.rfind("[") + 1:-1]) if title.endswith("]") else ""
        if src.get("status") == "current":
            current.add(src["setid"])
        for v in src.get("listed_versions", []):
            index[(v["version"], v["published_date"])].add(labeler)
    return KnownVersions(dict(index)), current


# ---------------------------------------------------------------- network

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "empty"


def cached_get(fetcher: build.Fetcher, url: str, path: Path) -> bytes:
    """GET once; later calls read the saved copy."""
    if path.exists():
        return path.read_bytes()
    data = fetcher.get(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return data


def search_url(query: str, page: int) -> str:
    return f"{build.DAILYMED}/archives/index.cfm?query={urllib.parse.quote(query)}&pagesize={PAGE_SIZE}&page={page}"


def info_url(archive_id: int) -> str:
    return f"{build.DAILYMED}/archives/fdaDrugInfo.cfm?archiveid={archive_id}"


def archival_url(archive_id: int) -> str:
    return f"{build.DAILYMED}/getArchivalFile.cfm?archive_id={archive_id}"


def search_all(fetcher: build.Fetcher, query: str, folder: Path) -> tuple[int, list[Row]]:
    """Every row the archive lists for a query (all pages)."""
    first = cached_get(fetcher, search_url(query, 1), folder / "p1.html").decode("utf-8", "replace")
    total, rows = parse_search(first)
    pages = -(-total // PAGE_SIZE)
    if pages > MAX_PAGES_PER_TERM:
        raise ArchiveError(f"{total} results ({pages} pages) exceeds the {MAX_PAGES_PER_TERM}-page limit")
    for page in range(2, pages + 1):
        body = cached_get(fetcher, search_url(query, page), folder / f"p{page}.html").decode("utf-8", "replace")
        rows.extend(parse_search(body)[1])
    return total, rows


def setid_listing(fetcher: build.Fetcher, setid: str, archive_dir: Path = ARCHIVE) -> list[Row]:
    """Every archived version of one setid, from the archive searched by setid."""
    _, rows = search_all(fetcher, setid, archive_dir / "setid" / setid)
    return rows


def archived_zip(fetcher: build.Fetcher, archive_id: int, archive_dir: Path = ARCHIVE,
                 raw_dir: Path = build.RAW) -> bytes:
    """One archived version's zip, kept in the raw store; raw/archive/zips/<id>.sha256 maps the id to it."""
    pointer = archive_dir / "zips" / f"{archive_id}.sha256"
    if pointer.exists():
        path = build.raw_path(pointer.read_text().strip(), raw_dir)
        if path.exists():
            return path.read_bytes()
    data = fetcher.get(archival_url(archive_id))
    digest = build.store(data, raw_dir)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(digest + "\n")
    return data


def resolve(fetcher: build.Fetcher, archive_id: int, archive_dir: Path = ARCHIVE) -> Info:
    """The label an archive row belongs to: from its drug-info page, or from the archived file
    itself when the page is too old to name the setid."""
    body = cached_get(fetcher, info_url(archive_id), archive_dir / "info" / f"{archive_id}.html")
    info = parse_info(body.decode("utf-8", "replace"), archive_id)
    if not info.setid:
        label = build.spl.convert(build.spl_xml(archived_zip(fetcher, archive_id, archive_dir)))
        info.setid, info.version = label.setid.lower(), label.version
        meta = label.metadata
        info.labeler = info.labeler or meta.get("labeler", "")
        info.applications = info.applications or sorted(meta.get("application_numbers", []))
        info.product_ndcs = info.product_ndcs or sorted({"-".join(n.split("-")[:2])
                                                         for n in meta.get("package_ndcs", [])})
    return info


def ensure_drugsfda(fetcher: build.Fetcher) -> Path:
    if DRUGSFDA.exists():
        return DRUGSFDA
    zpath = DRUGSFDA_DIR / "drug-drugsfda-0001-of-0001.json.zip"
    cached_get(fetcher, DRUGSFDA_URL, zpath)
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if n.endswith(".json") and "/" not in n]
        if names != ["drug-drugsfda-0001-of-0001.json"]:
            raise ArchiveError(f"unexpected members in {zpath}: {zf.namelist()}")
        DRUGSFDA.write_bytes(zf.read(names[0]))
    return DRUGSFDA


# ---------------------------------------------------------------- discover

@dataclass
class TermResult:
    term: str
    molecules: list
    total: int = 0
    rows_matching_title: int = 0
    rows_repackager: int = 0
    rows_known: int = 0
    rows_resolved: int = 0
    rows_left: int = 0
    setids: list = None
    problem: str = ""


class Resolved:
    """Setids resolved so far, shared by all search threads: a row whose archive_id is in a known
    setid's listing needs no request."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_archive_id: dict[int, str] = {}
        self.infos: dict[str, Info] = {}
        self.listings: dict[str, list[Row]] = {}

    def setid_of(self, archive_id: int) -> str | None:
        with self.lock:
            return self.by_archive_id.get(archive_id)

    def listing(self, setid: str) -> list[Row] | None:
        with self.lock:
            return self.listings.get(setid)

    def add(self, info: Info, listing: list[Row]) -> None:
        with self.lock:
            self.infos.setdefault(info.setid, info)
            self.listings.setdefault(info.setid, listing)
            self.by_archive_id[info.archive_id] = info.setid
            for r in listing:
                self.by_archive_id.setdefault(r.archive_id, info.setid)


def discover_term(term: Term, fetcher: build.Fetcher, labelers: LabelerIndex, known: KnownVersions,
                  resolved: Resolved, archive_dir: Path = ARCHIVE) -> TermResult:
    """Search one term and resolve its rows, newest first, until every row belongs to a known setid
    (or the per-term lookup limit is reached; leftovers are counted, never dropped silently)."""
    res = TermResult(term.term, sorted(term.molecules), setids=[])
    try:
        res.total, rows = search_all(fetcher, term.term, archive_dir / "search" / slug(term.term))
    except build.NotAvailable as err:
        res.problem = str(err)
        return res
    except ArchiveError as err:
        res.problem = f"skipped: {err}"
        return res
    rows = [r for r in rows if title_matches(r.title, term.term)]
    res.rows_matching_title = len(rows)
    pending = []
    for r in rows:
        if labelers.name_kind(r.packager) == "repackager":
            res.rows_repackager += 1
        elif known.owns(r):
            res.rows_known += 1
        else:
            pending.append(r)

    found: set = set()
    lookups = 0
    for r in sorted(pending, key=lambda r: (r.posted, r.version, r.archive_id), reverse=True):
        setid = resolved.setid_of(r.archive_id)
        if setid:
            found.add(setid)
            res.rows_resolved += 1
            continue
        if lookups >= MAX_RESOLVES_PER_TERM:
            res.rows_left += 1
            continue
        lookups += 1
        try:
            info = resolve(fetcher, r.archive_id, archive_dir)
            listing = resolved.listing(info.setid)
            if listing is None:
                listing = setid_listing(fetcher, info.setid, archive_dir)
        except (build.NotAvailable, ArchiveError, build.spl.SplError, zipfile.BadZipFile) as err:
            res.problem += f"; archive_id {r.archive_id}: {err}"
            res.rows_left += 1
            continue
        resolved.add(info, listing)
        found.add(info.setid)
        res.rows_resolved += 1
    res.setids = sorted(found)
    return res


def cmd_discover(args: argparse.Namespace) -> int:
    fetcher = build.Fetcher(log_path=build.RAW / "requests.tsv")
    drugsfda_path = ensure_drugsfda(fetcher)
    terms = search_terms(Path(args.molecules), load_drugsfda(drugsfda_path))
    if args.only:
        wanted = {sm.norm(t) for t in args.only}
        terms = [t for t in terms if sm.norm(t.term) in wanted]
    if args.limit:
        terms = terms[: args.limit]
    labelers = labeler_index(sm.load_ndc(sm.DEFAULT_NDC))
    known, _current = known_versions(build.SOURCES)
    print(f"{len(terms)} search terms", flush=True)

    resolved = Resolved()
    results: list[TermResult] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(discover_term, t, fetcher, labelers, known, resolved): t for t in terms}
        for n, fut in enumerate(as_completed(futures), start=1):
            try:
                r = fut.result()
            except build.FetchAborted as err:
                for other in futures:
                    other.cancel()
                print(f"ABORTED: {err}. Rerun to resume (saved pages are not fetched again).", file=sys.stderr)
                return 3
            results.append(r)
            print(f"[{n}/{len(terms)}] {r.term}: {r.total} rows, {r.rows_matching_title} titled, "
                  f"{r.rows_repackager} repackager, {r.rows_known} known, {len(r.setids)} setids, "
                  f"{r.rows_left} left{'; ' + r.problem.strip('; ') if r.problem else ''}; "
                  f"{fetcher.requests} requests, {(time.monotonic() - started) / 60:.1f} min", flush=True)

    term_by_key = {sm.norm(t.term): t for t in terms}
    report = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {"drugsfda": {"path": str(drugsfda_path.relative_to(ROOT)), "sha256": sm.sha256_file(drugsfda_path)},
                   "ndc": {"path": str(sm.DEFAULT_NDC.relative_to(ROOT)), "sha256": sm.sha256_file(sm.DEFAULT_NDC)}},
        "requests": fetcher.requests,
        "terms": [{**asdict(r), **term_record(term_by_key[sm.norm(r.term)])}
                  for r in sorted(results, key=lambda r: sm.norm(r.term))],
        "setids": {s: {**asdict(resolved.infos[s]),
                       "listing": [asdict(r) for r in sorted(resolved.listings[s], key=lambda r: r.version)]}
                   for s in sorted(resolved.infos)},
    }
    if args.only and DISCOVERY.exists():
        report = merge_discovery(json.loads(DISCOVERY.read_text(encoding="utf-8")), report)
    build.REPORTS.mkdir(exist_ok=True)
    DISCOVERY.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    skipped = [r for r in results if r.problem]
    print(f"done: {len(results)} terms, {len(resolved.infos)} setids resolved, {len(skipped)} terms with problems, "
          f"{fetcher.requests} requests; written {DISCOVERY}", flush=True)
    return 0


def merge_discovery(old: dict, new: dict) -> dict:
    """An --only run folded into the existing report: its terms replace those of the same name, every other
    term and resolved setid of the old report is kept (apply decides from the whole report, so a partial
    report would drop the retired setids found before). Requests add up; the run is listed under runs."""
    keys = {sm.norm(t["term"]) for t in new["terms"]}
    terms = [t for t in old.get("terms", []) if sm.norm(t["term"]) not in keys] + new["terms"]
    runs = old.get("runs") or [{"generated": old.get("generated"), "requests": old.get("requests"), "terms": "all"}]
    runs = [*runs, {"generated": new["generated"], "requests": new["requests"],
                    "terms": sorted(t["term"] for t in new["terms"])}]
    return {**old, "generated": new["generated"], "inputs": new["inputs"],
            "requests": (old.get("requests") or 0) + new["requests"], "runs": runs,
            "terms": sorted(terms, key=lambda t: sm.norm(t["term"])),
            "setids": dict(sorted({**old.get("setids", {}), **new["setids"]}.items()))}


# ---------------------------------------------------------------- apply

def term_record(t: Term) -> dict:
    """The JSON form of a term's brand and application sets, overall and per molecule."""
    return {"brands": sorted(t.brands), "applications": sorted(t.applications),
            "by_molecule": {inn: {"brands": sorted(o["brands"]), "applications": sorted(o["applications"])}
                            for inn, o in sorted(t.by_molecule.items())}}


def generic_part(title: str) -> str:
    """'LODINE (povidone-iodine) swab' -> 'POVIDONE IODINE'; the normalised text of the first parentheses."""
    m = re.search(r"\(([^)]*)\)", title)
    return sm.norm(m.group(1)) if m else ""


def molecule_names(molecules_dir: Path) -> dict[str, set]:
    """First words of each molecule's ingredient names (USAN/USP spelling), to recognise it in a label title."""
    names = {}
    for path in molecule_files(molecules_dir):
        mol = json.loads(path.read_text(encoding="utf-8"))
        words = {sm.norm(n).split()[0] for n in [*mol.get("ingredient_names", []), mol.get("name", "")] if sm.norm(n)}
        names[mol["inn"]] = words
    return names


def names_molecule(title: str, words: set) -> bool:
    """True when the title's generic name mentions the molecule, or states no generic name to check.
    A near match counts: archived titles carry typos ('olemesartan medoxomil')."""
    generic = generic_part(title)
    if not generic:
        return True
    tokens = generic.split()
    return any(w in generic or any(SequenceMatcher(None, w, t).ratio() >= 0.85 for t in tokens) for w in words)


def related_brand(brand: str, brands) -> bool:
    """The label's brand is one of the molecule's brands, or the bare stem of one: a 'DEXEDRINE' label
    belongs to the brand 'DEXEDRINE SPANSULE'. A label that adds words ('TYLENOL COLD MAX', 'BOTOX STOCK
    SOLUTION ANTI-AGING SERUM') can be a different product and needs an exact brand match."""
    return any(b == brand or b.startswith(brand + " ") for b in brands)


def decide(setid: str, info: dict, listing: list, term: dict, labelers: LabelerIndex, current: set,
           mol_apps: dict, mol_names: dict) -> dict:
    """Keep or reject one resolved setid for the molecules its search term came from."""
    decision = {"setid": setid, "term": term["term"], "labeler": info["labeler"],
                "applications": info["applications"], "keep_for": [], "reason": ""}
    if setid in current:
        decision["reason"] = "current setid (fetched already or outside the selection)"
        return decision
    if not listing:
        decision["reason"] = "the archive lists no versions for this setid"
        return decision
    if info.get("repackaged"):
        decision["reason"] = f"repackager or relabeler (source NDCs {info.get('source_ndcs') or 'n/a'}, REPACK/RELABEL)"
        return decision
    codes = {n.split("-")[0] for n in info["product_ndcs"]}
    kinds = {labelers.code_kind(c) for c in codes} or {"unknown"}
    if "holder" not in kinds:
        if "repackager" in kinds or labelers.name_kind(info["labeler"]) == "repackager":
            decision["reason"] = f"repackager or relabeler (labeler codes {sorted(codes)})"
            return decision
        decision["labeler_unverified"] = True
    apps = set(info["applications"])
    if not apps:
        # Early SPLs (2006-2008) often state no application number. Accept those only when the
        # product is the searched brand itself and the labeler is not a repackager (an unknown,
        # defunct labeler stays flagged labeler_unverified for spot checks).
        brand = brand_part(listing[-1]["title"]) if listing else ""
        # A brand name can be reused for an unrelated product (LODINE etodolac vs a LODINE
        # povidone-iodine swab), so the title's generic name must also name the molecule.
        owners = [inn for inn in sorted(term["molecules"]) if related_brand(brand, term["by_molecule"][inn]["brands"])
                  and names_molecule(listing[-1]["title"], mol_names.get(inn, set()))]
        if owners:
            decision["keep_for"] = owners
            decision["application_unstated"] = True
            decision["reason"] = "retired setid of the brand itself; the label states no application number"
            return decision
    for inn in sorted(term["molecules"]):
        if apps & mol_apps.get(inn, set()):
            decision["keep_for"].append(inn)
    if not decision["keep_for"]:
        decision["reason"] = (f"application numbers {sorted(apps) or 'none'} are not the molecule's NDA/BLA "
                              f"({', '.join(term['molecules'])})")
        return decision
    decision["reason"] = "retired setid under the molecule's own application"
    return decision


def molecule_applications(molecules_dir: Path, terms: list[dict]) -> dict[str, set]:
    """NDA/BLA numbers per molecule: from its selected labels and from the Drugs@FDA brands it was searched by."""
    apps: dict[str, set] = defaultdict(set)
    for path in molecule_files(molecules_dir):
        mol = json.loads(path.read_text(encoding="utf-8"))
        for e in mol.get("us", []):
            if e.get("source") != ARCHIVE_SOURCE:
                apps[mol["inn"]].update(a for a in e.get("application_numbers", []) if a.startswith(INNOVATOR_PREFIXES))
    for t in terms:
        for inn, own in t["by_molecule"].items():
            apps[inn].update(own["applications"])
    return apps


def archive_entry(setid: str, info: dict, listing: list, decision: dict, successors: list[str], brand: str) -> dict:
    posted = sorted(r["posted"] for r in listing)
    newest = max(listing, key=lambda r: (r["posted"], r["version"]))
    return {"setid": setid, "status": "retired", "source": ARCHIVE_SOURCE, "title": newest["title"],
            "labeler": info["labeler"], "application_numbers": info["applications"],
            "marketing_categories": sorted({a[:3] if a.startswith("BLA") else a.rstrip("0123456789")
                                            for a in info["applications"]}),
            "brand_names": [brand], "product_ndcs": info["product_ndcs"], "rxcui": [],
            "first_posted": posted[0], "last_posted": posted[-1], "archived_versions": len(listing),
            "marketing_end_dates": info["marketing_end_dates"], "successor_setids": successors,
            "found_by": decision["term"], "labeler_unverified": bool(decision.get("labeler_unverified"))}


def cmd_apply(args: argparse.Namespace) -> int:
    molecules_dir = Path(args.molecules)
    report = json.loads(DISCOVERY.read_text(encoding="utf-8"))
    labelers = labeler_index(sm.load_ndc(sm.DEFAULT_NDC))
    spl_meta = sm.load_spl_meta(sm.DEFAULT_SPL_META)
    _known, current_fetched = known_versions(build.SOURCES)
    current = set(spl_meta) | current_fetched
    # Per-molecule brand and application shares come from the inputs, not the report, so a report
    # written before they were recorded is still decided correctly. Archive entries are ignored
    # when terms are rebuilt, so this gives the same terms before and after an apply.
    fresh = {sm.norm(t.term): term_record(t) for t in search_terms(molecules_dir, load_drugsfda())}
    for term in report["terms"]:
        term.update(fresh[sm.norm(term["term"])])
    mol_apps = molecule_applications(molecules_dir, report["terms"])
    mol_names = molecule_names(molecules_dir)

    decisions, keep = [], defaultdict(list)     # inn -> [(setid, decision, brand)]
    for term in report["terms"]:
        for setid in term["setids"]:
            info = report["setids"][setid]
            d = decide(setid, info, info["listing"], term, labelers, current, mol_apps, mol_names)
            decisions.append(d)
            for inn in d["keep_for"]:
                keep[inn].append((setid, d, term["term"]))

    changed = 0
    for path in molecule_files(molecules_dir):
        mol = json.loads(path.read_text(encoding="utf-8"))
        inn = mol["inn"]
        base = [e for e in mol.get("us", []) if e.get("source") != ARCHIVE_SOURCE]
        added, seen = [], {e["setid"] for e in base}
        for setid, d, brand in sorted(keep.get(inn, []), key=lambda k: k[0]):
            if setid in seen:
                continue
            seen.add(setid)
            info = report["setids"][setid]
            successors = sorted(e["setid"] for e in base
                                if set(e.get("application_numbers", [])) & set(info["applications"]))
            added.append(archive_entry(setid, info, info["listing"], d, successors, sm.norm(brand)))
        new_us = base + added
        if new_us != mol.get("us", []):
            changed += 1
            if not args.dry_run:
                mol["us"] = new_us
                sm.dump_json(path, mol)

    kept = sorted({d["setid"] for d in decisions if d["keep_for"]})
    pinned = {"generated_from": str(DISCOVERY.relative_to(ROOT)), "discovery_generated": report["generated"],
              "rules": {"keep": "retired (not current in DailyMed) setid whose labeler is not a repackager and whose "
                                "application numbers include one of the molecule's own NDA/BLA numbers",
                        "repackager": "every product labeler code is a repackager code by openFDA's "
                                      "is_original_packager share, or the labeler name is a known repackager"},
              "kept_setids": kept,
              "decisions": sorted(decisions, key=lambda d: (d["setid"], d["term"]))}
    out = RETIRED.with_name("retired-setids.dry-run.json") if args.dry_run else RETIRED
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    reasons = Counter(d["reason"].split(" (")[0] for d in decisions)
    print(f"{len(decisions)} decisions, {len(kept)} retired setids kept, {changed} molecule files "
          f"{'would change (dry run)' if args.dry_run else 'changed'}; written {out}")
    for reason, n in reasons.most_common():
        print(f"  {n:>5}  {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="search the archive by brand and resolve candidate setids (network, cached)")
    d.add_argument("--molecules", default=str(build.MOLECULES))
    d.add_argument("--workers", type=int, default=4, help="parallel searches; 2 requests/s shared by all")
    d.add_argument("--limit", type=int, help="only the first N terms (trial run)")
    d.add_argument("--only", nargs="+",
                   help="only these search terms; their results are merged into the existing report")
    d.set_defaults(func=cmd_discover)
    a = sub.add_parser("apply", help="decide, pin and merge retired setids into molecules/*.json (offline)")
    a.add_argument("--molecules", default=str(build.MOLECULES))
    a.add_argument("--dry-run", action="store_true", help="write the decisions only; leave molecules/*.json as they are")
    a.set_defaults(func=cmd_apply)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (build.BuildError, sm.SelectionError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
