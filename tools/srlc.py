"""Validate the US label-history repo against FDA's Safety-related Labeling Changes (SrLC) database.

Subcommands: ``fetch`` (monthly SrLC date searches and the detail page of every drug whose application
is in the crosswalk, cached under raw/srlc/ so a rerun makes no request), ``parse`` (cached pages to
items, one per SrLC section change), ``select`` (the pinned 50-item sample and the population report)
and ``check`` (the pinned sample against build/us: match rate, sensitivity, per-item evidence and the
two validation reports; it refuses a sample whose item list is not the pinned one and never touches
the network).

The SrLC date search lists each drug once, at its latest safety labeling change, and a detail page
covers every year for that drug. Drugs whose latest change falls after 2025 would never show up in a
2016..2025 search, so the searches run from 2016-01 through the month of the fetch (SEARCH_MONTHS) and
the 2016..2025 items are taken from the detail pages.

Every rule and constant the sample depends on lives here and is copied verbatim into
sources/srlc-sample.json, so the sample can be regenerated and audited. Selection reads only SrLC pages,
the crosswalk and the pinned version dates in sources/us; it never opens label text.

Network: only www.accessdata.fda.gov, at most one request per second, the project User-Agent, a session
cookie jar, and a stop on HTTP 403, a challenge page or repeated failures. Standard library only.
"""
from __future__ import annotations

import argparse
import bisect
import difflib
import hashlib
import html
import http.cookiejar
import json
import re
import sys
import time
import statistics
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import USER_AGENT  # noqa: E402
import labels  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW_SRLC = ROOT / "raw" / "srlc"
MOLECULES = ROOT / "molecules"
SOURCES_US = ROOT / "sources" / "us"
REPORTS = ROOT / "reports"
SAMPLE_PATH = ROOT / "sources" / "srlc-sample.json"
MISS_NOTES_PATH = ROOT / "sources" / "srlc-miss-notes.json"
POPULATION_PATH = REPORTS / "srlc-population.json"
VALIDATION_JSON = REPORTS / "srlc-validation.json"
VALIDATION_MD = REPORTS / "srlc-validation.md"
# sha256 of the canonical JSON of the pinned sample's item list; check refuses any other sample.
SAMPLE_ITEMS_SHA256 = "a9aa9777351d7de969f64d55bf2ccb48070a72d55f1f010d67e30cb90e9ebc4d"
ITEMS_PATH = RAW_SRLC / "items.json"
REQUEST_LOG = RAW_SRLC / "requests.tsv"

SRLC_HOST = "www.accessdata.fda.gov"
SRLC_INDEX = f"https://{SRLC_HOST}/scripts/cder/safetylabelingchanges/index.cfm"
SRLC_SEARCH = SRLC_INDEX + "?event=searchResult.page"
SRLC_DETAIL = SRLC_INDEX + "?event=searchdetail.page&DrugNameID={drug_id}"
SEARCH_SECTIONS = ("BW", "C", "WP", "AR", "DI", "USP", "MG")
SEARCH_MONTHS = ("2016-01", "2026-11")  # through the fetch month: the search lists a drug only at its latest change
MIN_GAP_S = 1.0
RETRY_BACKOFF_S = (5, 30, 120)
TRANSIENT = {429, 500, 502, 503, 504}
ABORT_AFTER_FAILURES = 3
CHALLENGE_MARKERS = ("incapsula", "_incapsula_resource", "request unsuccessful", "access denied",
                     "captcha", "challenge-platform", "cf-chl", "pardon our interruption")

# ---------------------------------------------------------------- pre-registered rules (fixed before any result)

PARSE_VERSION = 2  # 2: every content div of a section is read, nested <h4> stay inside their div, long bold headings and abbreviations
HASH_PREFIX = "asof-m4"
YEARS = tuple(range(2016, 2026))
PER_YEAR = 5
MAX_PER_APPLICATION_PER_YEAR = 1
MAX_PER_APPLICATION_OVERALL = 2
SAMPLED_SECTIONS = ("BW", "C", "WP", "AR", "DI", "USP")
UNSAMPLED_SECTIONS = ("PCI/PI/MG",)  # SrLC files patient counseling, patient information and medication guide changes under one heading
MIN_SENTENCE_TOKENS = 6
MIN_QUOTE_TOKENS = 12
LINE_COVERAGE = 0.90
QUOTE_TOKEN_SHARE = 0.80
WINDOW_DAYS = 183
SENSITIVITY = {
    "strict": {"substring_only": True, "line_coverage": 1.0, "quote_token_share": 1.0, "window_days": WINDOW_DAYS},
    "loose": {"substring_only": False, "line_coverage": 0.80, "quote_token_share": 0.60, "window_days": WINDOW_DAYS},
    "window_365": {"substring_only": False, "line_coverage": LINE_COVERAGE, "quote_token_share": QUOTE_TOKEN_SHARE,
                   "window_days": 365},
}
# Top-level specific-population codes seen in build/us file names (pregnancy, nursing mothers, lactation,
# females and males of reproductive potential, pediatric use, geriatric use, renal and hepatic impairment).
# Labor and delivery (34079-4) and the OTC pregnancy box (53414-9) are not in the brief's list and are left out.
SPECIFIC_POPULATION_CODES = ("42228-7", "34080-2", "77290-5", "77291-3", "34081-0", "34082-8", "88828-9", "88829-7")
SECTION_GROUPS = {
    "BW": ("34066-1",),
    "C": ("34070-3",),
    "WP": ("43685-7",),
    "AR": ("34084-4",),
    "DI": ("34073-7", "43685-7"),
    "USP": ("43684-0", "43685-7") + SPECIFIC_POPULATION_CODES,
}
SRLC_REPORT_TITLE = ("Validation of the US label history against FDA's Safety-related Labeling Changes (SrLC): "
                     "pre-registered check")
SHORT_QUOTE_TOKENS = 15  # a usable quote this short is flagged in the report as weak evidence (information only)
WINDOW_RATIONALE = ("CBE-0 supplements let a company implement a safety change before FDA approves it, and "
                    "DailyMed publication can trail approval by months, so a transition up to 183 days either "
                    "side of the approval date counts as a match.")
# Every regeneration of the sample after the first pin, oldest first: what changed in the parse, why, and which
# items left or entered the sample under the unchanged selection rule. Filled in by the fix pass that made it.
REVISIONS: list[dict] = [
    {
        "parse_version": 2,
        "date": "2026-11-01",
        "previous_items_sha256": "0ecf8c8cff52f01b527446bb7fdb69b5316e85413bafa7185adb358b676821fb",
        "reason": ("Parse version 1 had four faults: it read only the first content div of each SrLC section (3,495 of "
                   "15,982 section regions in the cache have two or more divs), took an <h4> nested inside a content div "
                   "for a new section (21 phantom items), let all-bold headings longer than 8 tokens without sentence "
                   "punctuation (boxed-warning titles, table captions) into the quote, and split sentences after "
                   "abbreviations such as 'e.g.', 'vs.' and 'St.'. Parse version 2 fixes all four; the selection rules "
                   "(6, 9, 10), the hash key and every constant are unchanged. The corrected pool has 6,271 eligible items "
                   "(was 5,774); the unchanged hash order replaces three items, and 12 retained items carry longer or "
                   "shorter quotes than before (listed under retained_quote_changes)."),
        "changes": [
            {"year": 2016, "removed": "srlc-NDA021692-20161216-SUPPL14-BW", "added": "srlc-NDA021306-20161216-SUPPL24-AR",
             "why": "BUTRANS (hash 0166df19) was not usable under parse 1 because its 6.2 text sat in the second div; it now precedes "
                    "ULTRAM ER (hash 024843e5) in the 2016 hash order"},
            {"year": 2017, "removed": "srlc-NDA021924-20170425-SUPPL12-WP", "added": "srlc-NDA203094-20170505-SUPPL7-DI",
             "why": "TYBOST (hash 026afac4) became usable and precedes TIROSINT (hash 026e688a)"},
            {"year": 2020, "removed": "srlc-BLA125433-20200929-SUPPL31-WP", "added": "srlc-NDA202106-20200512-SUPPL7-WP",
             "why": "the MEROPENEM duplex container item (hash 006c5237) became usable and precedes SIMPONI ARIA (hash 01dbbe5d)"},
        ],
        "retained_quote_changes": [
            "srlc-NDA019892-20161216-SUPPL29-WP DILAUDID 98 -> 1472 tokens", "srlc-NDA207932-20161216-SUPPL2-BW BELBUCA 88 -> 67",
            "srlc-NDA203993-20161216-SUPPL5-DI ONFI 76 -> 147", "srlc-NDA021574-20170405-SUPPL20-DI FORTAMET 21 -> 191",
            "srlc-NDA022341-20170425-SUPPL28-AR VICTOZA 518 -> 527", "srlc-NDA006134-20180201-SUPPL45-BW DOLOPHINE 43 -> 33",
            "srlc-NDA050753-20181005-SUPPL22-AR TOBI 13 -> 100", "srlc-NDA210806-20190919-SUPPL2-AR PIFELTRO 310 -> 284",
            "srlc-NDA204150-20191029-SUPPL10-WP DESVENLAFAXINE 38 -> 77", "srlc-NDA020427-20200124-SUPPL21-USP SABRIL 124 -> 220",
            "srlc-NDA021419-20231013-SUPPL23-WP METHYLIN 136 -> 307", "srlc-NDA020825-20250122-SUPPL65-WP GEODON 250 -> 250 (one abbreviation join)",
        ],
    },
]
RULES = [
    "1. Item = one SrLC section change: (application, approval date, supplement, SrLC section). Item id: "
    "srlc-<APPLICATION>-<YYYYMMDD>-SUPPL<n>-<SECTION>. Application numbers are normalised to NDA or BLA plus 6 digits.",
    "2. Quote: for 'Additions and/or revisions underlined' text, the underlined spans (adjacent spans in the same "
    "paragraph merged); for 'Newly added information' text, the whole text. FDA comments, section and subsection "
    "headings and 'PLLR conversion; please refer to label' notes are dropped. Split into sentences; keep sentences "
    "with >= 6 normalised tokens. An item is usable if its kept sentences total >= 12 tokens.",
    "3. Normalisation: unicode NFKC, lowercase, every run of non-alphanumeric characters becomes one space, strip. "
    "Tokens = whitespace split.",
    "4. Section group = files of a version whose LOINC code is in the group: BW {34066-1}; C {34070-3}; WP {43685-7}; "
    "AR {34084-4}; DI {34073-7, 43685-7}; USP {43684-0, 43685-7} plus the top-level specific-population codes listed "
    "under section_groups. PCI, PI and MG items are counted but not sampled.",
    "5. Presence: a quote sentence is found in a version if its normalised form is a substring of the normalised "
    "concatenated section-group text, or if for some line or pair of consecutive lines the token coverage "
    "sum(matching block sizes of difflib.SequenceMatcher(None, sentence_tokens, line_tokens, autojunk=False)) / "
    "len(sentence_tokens) >= 0.90. The quote is present if found sentences hold >= 0.80 of all kept quote tokens.",
    "6. Setids for an item: every setid in molecules/*.json whose application_numbers include the item's application "
    "(current and retired). Covered setid: its first pinned version was published on or before approval date - 183 "
    "days, and it is current or its last pinned version was published on or after approval date + 183 days. Version "
    "dates come from sources/us/*.json only.",
    "7. Per covered setid, walk its versions in Published-Date order. Transition = a version where the quote is present "
    "and the previous version's is not. Choose the transition with the smallest |lag|, lag = transition published "
    "date - approval date in days. Category: match if a chosen transition has -183 <= lag <= 183; early if the quote "
    "is present in the setid's first version or the chosen transition has lag < -183; late if lag > 183; absent if "
    "the quote is never present.",
    "8. Item outcome: match if any covered setid is a match; otherwise the categories of its covered setids (all "
    "reported; headline category priority late, early, absent). Match rate = matches / 50.",
    "9. Eligibility (content-blind): approval date 2016-01-01..2025-12-31, section in BW/C/WP/AR/DI/USP, usable "
    "quote, and at least one covered setid.",
    "10. Sample: per calendar year 2016..2025, order eligible items by "
    "sha256('asof-m4|<APPLICATION>|<YYYY-MM-DD>|<SUPPL>|<SECTION>') hex ascending; take items in that order, skipping "
    "an application already taken that year or already taken twice overall, until 5 are taken. If a year has fewer "
    "than 5 eligible items, say so and do not top up from other years.",
    "11. Sensitivity (reported, never the headline): strict = substring-only and 1.00 of tokens; loose = coverage 0.80 "
    "and 0.60 of tokens; window +-365 days.",
]

# FDA comment types. The two the quote rule names, then the ones that carry no quotable text: the
# 'please refer to label' family (PLLR and PLR conversions, revised tables, extensive changes), any other
# italic FDA note, and sections with no comment at all (with or without underline markup).
COMMENT_UNDERLINED = "additions_underlined"
COMMENT_NEWLY_ADDED = "newly_added"
COMMENT_REFER_TO_LABEL = "refer_to_label"
COMMENT_OTHER = "other_comment"
COMMENT_NONE = "none"
COMMENT_NONE_UNDERLINED = "none_underlined"
COMMENT_MIXED = "mixed"

SECTION_NAME_TO_CODE = [
    (re.compile(r"PCI\s*/\s*PI\s*/\s*MG|patient counseling|medication guide|patient (package )?(insert|information)|"
                r"instructions for use|patient labeling", re.I), "PCI/PI/MG"),
    (re.compile(r"boxed? warning", re.I), "BW"),
    (re.compile(r"contraindication", re.I), "C"),
    (re.compile(r"warnings? and precautions|^warnings?$|^precautions?$|warnings?\b.*precautions?", re.I), "WP"),
    (re.compile(r"adverse reactions?", re.I), "AR"),
    (re.compile(r"drug interactions?", re.I), "DI"),
    (re.compile(r"use in specific populations?|specific populations?", re.I), "USP"),
]

NON_ALNUM = re.compile(r"[\W_]+")  # Unicode-aware: letters and digits of any script stay, everything else is a separator
SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+(?=[\(\[\"'“‘A-Z0-9•\-–])")
# A sentence does not end after one of these (matched as written, so 'MS.' the disease still ends a sentence).
ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "et al.", "cf.", "approx.", "Fig.", "Figs.", "St.", "No.", "U.S.", "Inc.", "Ltd.",
                 "Co.", "Dr.", "Mr.", "Mrs.", "spp.")
ABBREVIATION_END = re.compile(r"(?:^|[\s(\[])(?:" + "|".join(re.escape(a) for a in ABBREVIATIONS) + r")$")
APP_NUMBER = re.compile(r"\b(NDA|BLA|ANDA)[\s\-]*0*(\d{1,6})\b", re.I)


class SrlcError(RuntimeError):
    """A problem the operator must see: a blocked request, a bad page or a missing cache."""


class SrlcBlocked(SrlcError):
    """HTTP 403, a bot-challenge page or repeated failures: stop, never work around it."""


# ---------------------------------------------------------------- normalisation and quotes (rules 2-3)

def normalise(text: str) -> str:
    """Rule 3: NFKC, lowercase, runs of non-alphanumerics to one space, stripped."""
    text = unicodedata.normalize("NFKC", text).lower()
    return NON_ALNUM.sub(" ", text).strip()


def tokens(text: str) -> list[str]:
    return normalise(text).split()


def split_sentences(text: str) -> list[str]:
    """Sentences of one paragraph: split after . ! ? ; : followed by whitespace and an upper-case, digit or
    bracket start, so decimals ('0.5 mg') and lower-case abbreviations stay together; a piece that ends
    with one of ABBREVIATIONS ('e.g.', 'vs.', 'St.') is joined to the next piece."""
    out: list[str] = []
    for part in SENTENCE_END.split(text.strip()):
        part = part.strip()
        if not part:
            continue
        if out and ABBREVIATION_END.search(out[-1]):
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def kept_sentences(paragraphs: list[str]) -> list[str]:
    """Rule 2: the sentences of the quote paragraphs with >= MIN_SENTENCE_TOKENS normalised tokens."""
    out = []
    for para in paragraphs:
        for sentence in split_sentences(para):
            if len(tokens(sentence)) >= MIN_SENTENCE_TOKENS:
                out.append(sentence)
    return out


def quote_tokens(sentences: list[str]) -> int:
    return sum(len(tokens(s)) for s in sentences)


def usable(sentences: list[str]) -> bool:
    return quote_tokens(sentences) >= MIN_QUOTE_TOKENS


# ---------------------------------------------------------------- application numbers

def normalise_application(kind: str, number: str) -> str | None:
    """'NDA ', '205874' -> 'NDA205874'; 'BLA', '125514' -> 'BLA125514'; anything else -> None."""
    kind = (kind or "").strip().upper()
    digits = re.sub(r"\D", "", number or "")
    if kind not in ("NDA", "BLA") or not digits or len(digits) > 6:
        return None
    return f"{kind}{int(digits):06d}"


def application_from_text(text: str) -> str | None:
    """'BENDEKA (NDA-208194)' -> 'NDA208194'."""
    m = APP_NUMBER.search(text or "")
    if not m:
        return None
    return normalise_application(m.group(1), m.group(2))


# ---------------------------------------------------------------- HTML helpers

class _TableParser(HTMLParser):
    """Rows of every <table> as lists of cells; a cell keeps its text, the first link's href and title,
    and its data-order attribute. Enough for the SrLC search result table."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "href": None, "title": None, "data_order": a.get("data-order")}
        elif tag == "a" and self._cell is not None and self._cell["href"] is None:
            self._cell["href"] = a.get("href")
            self._cell["title"] = a.get("title")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


RESULTS_MARKER = "Search Results for:"
DETAIL_MARKER = "event=downloadDetails.page&DrugNameID="


def is_results_page(page: str) -> bool:
    """A search result page, with or without rows. A rejected POST (no cookies or no Datesearch field)
    redirects to the start page, which carries the search form and no results marker."""
    return RESULTS_MARKER in page and 'name="date_range_from"' not in page


def is_start_page(page: str) -> bool:
    return 'name="date_range_from"' in page or RESULTS_MARKER not in page and DETAIL_MARKER not in page


def is_detail_page(page: str) -> bool:
    return DETAIL_MARKER in page and 'name="date_range_from"' not in page


def is_challenge_page(page: str) -> bool:
    low = page.lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def parse_search_page(page: str) -> list[dict]:
    """Rows of a search result page: drug_id, brand, ingredient, application, kind, approval_date, posted_date.

    Raises SrlcError on the start page (a rejected search), which must not pass as 'no results'."""
    if not is_results_page(page):
        raise SrlcError("search returned the SrLC start page, not a result page (session cookie or Datesearch field missing)")
    parser = _TableParser()
    parser.feed(page)
    rows = []
    for cells in parser.rows:
        link = cells[0]
        if not link["href"] or "DrugNameID=" not in link["href"] or len(cells) < 6:
            continue
        drug_id = re.search(r"DrugNameID=(\d+)", link["href"]).group(1)
        kind, number = cells[3]["text"], cells[2]["text"]
        rows.append({
            "drug_id": drug_id,
            "brand": (link["title"] or link["text"]).strip(),
            "ingredient": cells[1]["text"],
            "application": normalise_application(kind, number),
            "application_kind": kind.strip(),
            "application_number": number.strip(),
            "approval_date": _iso_date(cells[4]["data_order"] or cells[4]["text"]),
            "posted_date": _iso_date(cells[5]["data_order"] or cells[5]["text"]),
        })
    return rows


def _iso_date(value: str | None) -> str | None:
    """'2024/01/19' or '01/19/2024' -> '2024-01-19'."""
    if not value:
        return None
    value = value.strip()
    m = re.fullmatch(r"(\d{4})/(\d{2})/(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", value)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    return None


# ---------------------------------------------------------------- detail pages (rules 1-2)

ACCORDION_START = re.compile(r'<div id="accordion"', re.I)
ACCORDION_HEADER = re.compile(
    r'<h3 class="background_text accordion-header[^"]*">\s*(\d{2}/\d{2}/\d{4})\s*(?:</a>)?\s*<small>\((.*?)\)</small>', re.S)
H4 = re.compile(r"<h4>(.*?)</h4>", re.S)
STRONG = re.compile(r"<strong>(.*?)</strong>", re.S)
CONTENT_DIV = re.compile(r'<div style="background-color:#fff[^"]*">', re.I)
DIV_TAG = re.compile(r"<(/?)div\b", re.I)
HEADER_H3 = re.compile(r"<h3>\s*([^<]*?)\s*<small>\(([^)<]*)\)</small>\s*</h3>")
INGREDIENT_H5 = re.compile(r"<h5>\s*\(\s*(.*?)\s*\)\s*</h5>", re.S)
TAGS = re.compile(r"<[^>]+>")
NUMBERED_HEADING = re.compile(r"^\d+(\.\d+)*\s+\S")
SENTENCE_PUNCTUATION = re.compile(r"[.!?]")
MAX_HEADING_TOKENS = 8
# A heading is a paragraph in a heading tag, or an all-bold paragraph that is numbered ('8.1 Pregnancy'), has at
# most MAX_HEADING_TOKENS tokens, or has no sentence punctuation at all (a boxed-warning title, a table caption).
HEADING_RULE = ("heading tag, or all-bold and (numbered, or <= 8 tokens, or no '.', '!' or '?' anywhere); "
                "struck-through text dropped first")
# The wording of the two quote-bearing comments varies ('Additions and/or revisions are underlined',
# 'Additions are underlined below', 'newly added subsection', 'New subsection added', 'Addition of the
# following:'); each family is matched as one type. Patterns run on normalised text.
COMMENT_PATTERNS = (
    (re.compile(r"(additions?|revisions?|changes?)( and ?or (revisions?|additions?))? (are |is )?underlined"), COMMENT_UNDERLINED),
    (re.compile(r"newly added|new (sub)?sections? (added|created)|(sub)?sections? (added|created)|added (sub)?sections?|"
                r"addition of the following|the following( \w+){0,3} (is|was|has been|have been) added|added the following"),
     COMMENT_NEWLY_ADDED),
    (re.compile(r"refer to (the )?(label|labeling|pi\b|prescribing information)|pllr conversion|plr conversion|"
                r"labeling rule"), COMMENT_REFER_TO_LABEL),
)
# An all-italic paragraph with one of these words is an FDA note of some other kind, not label text.
OTHER_COMMENT_WORDS = re.compile(r"\b(revised|revision|updated|update|added|addition|deleted|deletion|removed|changes?|"
                                 r"conversion|as below|as follows|reformatted|relocated|moved|underlined)\b")


def strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(TAGS.sub(" ", fragment)).split())


def collapse_ws(fragment: str) -> str:
    return " ".join(fragment.split())


class _ParagraphParser(HTMLParser):
    """Paragraphs of a content div as segment lists; a segment is text with its underline, italic,
    bold, strike-through and heading-tag state. Block-level tags start a new paragraph."""
    BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "br",
             "blockquote", "dd", "dt", "hr"}
    STATE = {"u": "u", "ins": "u", "i": "i", "em": "i", "b": "b", "strong": "b", "s": "s", "strike": "s", "del": "s",
             "h1": "h", "h2": "h", "h3": "h", "h4": "h", "h5": "h", "h6": "h"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[list[dict]] = []
        self._segs: list[dict] = []
        self._depth = {"u": 0, "i": 0, "b": 0, "s": 0, "h": 0}

    @classmethod
    def _key(cls, tag: str) -> str | None:
        return cls.STATE.get(tag)

    def _flush(self):
        if any(seg["text"].strip() for seg in self._segs):
            self.paragraphs.append(self._segs)
        self._segs = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self._flush()
        key = self._key(tag)
        if key:
            self._depth[key] += 1

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self._flush()
        key = self._key(tag)
        if key:
            self._depth[key] = max(0, self._depth[key] - 1)

    def handle_data(self, data):
        d = self._depth
        self._segs.append({"text": data, "u": d["u"] > 0, "i": d["i"] > 0, "b": d["b"] > 0, "s": d["s"] > 0, "h": d["h"] > 0})

    def close(self):
        super().close()
        self._flush()


def underlined_spans(segs: list[dict]) -> list[str]:
    """Underlined text of one paragraph, adjacent spans (separated by nothing or whitespace) merged."""
    spans, current, gap = [], "", ""
    for seg in segs:
        if seg["s"]:
            continue
        if seg["u"]:
            current += gap + seg["text"]
            gap = ""
        elif seg["text"].strip() == "":
            gap += seg["text"]
        else:
            if current.strip():
                spans.append(collapse_ws(current))
            current, gap = "", ""
    if current.strip():
        spans.append(collapse_ws(current))
    return spans


def comment_type(text: str, italic_only: bool = False) -> str | None:
    """The FDA comment a paragraph carries: one of the known phrases at its start, or anywhere when the
    paragraph is all italics; an all-italic note with an editorial word is 'other_comment'."""
    n = normalise(text)
    for pattern, kind in COMMENT_PATTERNS:
        m = pattern.search(n)
        if m and (m.start() == 0 or italic_only):
            return kind
    if italic_only and OTHER_COMMENT_WORDS.search(n):
        return COMMENT_OTHER
    return None


def is_heading(text: str, bold_only: bool, heading_tag: bool) -> bool:
    """HEADING_RULE: a heading tag, or an all-bold paragraph that is numbered, short, or has no sentence
    punctuation (a boxed-warning title or a table caption, however long)."""
    if heading_tag:
        return True
    if not bold_only:
        return False
    return bool(NUMBERED_HEADING.match(text)) or len(tokens(text)) <= MAX_HEADING_TOKENS or not SENTENCE_PUNCTUATION.search(text)


def analyse_paragraph(segs: list[dict]) -> dict:
    """Classify one paragraph: an FDA comment, a heading, or label text with its underlined spans.
    Struck-through (deleted) text is dropped everywhere."""
    live = [s for s in segs if not s["s"]]
    text = collapse_ws("".join(s["text"] for s in live))
    filled = [s for s in live if s["text"].strip()]
    italic_only = bool(filled) and all(s["i"] for s in filled)
    bold_only = bool(filled) and all(s["b"] for s in filled)
    heading_tag = bool(filled) and all(s.get("h") for s in filled)
    kind = comment_type(text, italic_only)
    if kind:
        # The comment is the italic lead of the paragraph; whatever follows it is label text. Without
        # italics the comment runs to the first colon.
        lead = []
        for s in live:
            if s["i"] or not s["text"].strip():
                lead.append(s)
            else:
                break
        if lead and comment_type("".join(s["text"] for s in lead), True):
            rest = live[len(lead):]
        else:
            _, sep, tail = text.partition(":")
            rest = [{"text": tail.strip(), "u": False, "i": False, "b": False, "s": False, "h": False}] if sep and tail.strip() else []
        rest_para = analyse_paragraph(rest) if any(s["text"].strip() for s in rest) else None
        return {"kind": "comment", "comment": kind, "text": text, "rest": rest_para}
    if is_heading(text, bold_only, heading_tag):
        return {"kind": "heading", "text": text}
    return {"kind": "text", "text": text, "spans": underlined_spans(live), "underlined": any(s["u"] for s in filled)}


def parse_paragraphs(inner_html: str) -> list[dict]:
    parser = _ParagraphParser()
    parser.feed(inner_html)
    parser.close()
    return [analyse_paragraph(segs) for segs in parser.paragraphs]


def srlc_section_code(heading: str) -> str | None:
    """'8 Use in Specific Populations' -> 'USP'; unknown headings -> None."""
    name = re.sub(r"^\s*\d+(\.\d+)*\s*", "", collapse_ws(heading)).strip()
    for pattern, code in SECTION_NAME_TO_CODE:
        if pattern.search(name):
            return code
    return None


def _div_inner(fragment: str, start: int) -> tuple[str, int]:
    """Inner HTML of the <div> whose start tag begins at ``start``, found by nesting depth."""
    depth = 0
    for m in DIV_TAG.finditer(fragment, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            open_end = fragment.index(">", start) + 1
            return fragment[open_end:m.start()], m.end()
    return fragment[fragment.index(">", start) + 1:], len(fragment)


def section_quote(paragraphs: list[dict]) -> dict:
    """Rule 2 over one section's paragraphs: the quote paragraphs, the FDA comment type, the headings."""
    headings, quote, comments = [], [], []
    has_underline = any(p.get("underlined") for p in paragraphs) or any(
        p.get("rest") and p["rest"].get("underlined") for p in paragraphs)
    # Without a quote-bearing comment, underline markup still means added or revised text (the site's legend).
    default = COMMENT_UNDERLINED if has_underline else COMMENT_NONE
    mode = default
    queue = list(paragraphs)
    while queue:
        p = queue.pop(0)
        if p["kind"] == "comment":
            comments.append(p["comment"])
            mode = p["comment"] if p["comment"] in (COMMENT_UNDERLINED, COMMENT_NEWLY_ADDED) else default
            if p.get("rest"):
                queue.insert(0, p["rest"])
            continue
        if p["kind"] == "heading":
            headings.append(p["text"])
            continue
        if mode == COMMENT_UNDERLINED:
            quote.extend(p["spans"])
        elif mode == COMMENT_NEWLY_ADDED and p["text"]:
            quote.append(p["text"])
    return {"quote_paragraphs": quote, "comment_type": overall_comment_type(comments, has_underline), "comments": comments,
            "subsection_headings": headings, "has_underline": has_underline}


def overall_comment_type(comments: list[str], has_underline: bool) -> str:
    """One type for a section: the single FDA comment kind seen, 'mixed' for several, and without any
    comment 'none_underlined' or 'none' by the underline markup."""
    kinds = []
    for c in comments:
        if c not in kinds:
            kinds.append(c)
    if not kinds:
        return COMMENT_NONE_UNDERLINED if has_underline else COMMENT_NONE
    return kinds[0] if len(kinds) == 1 else COMMENT_MIXED


def block_sections(block: str) -> list[dict]:
    """The sections of one accordion block in page order. Every <h4> outside a content div starts a
    section; every content div is a chunk of the current section, with the <strong> just before it as
    the chunk's subsection heading. A div's inner HTML is stepped over whole, so an <h4> nested inside it
    is a heading paragraph of that section, not a new section."""
    sections: list[dict] = []
    pos = 0
    while True:
        h4 = H4.search(block, pos)
        cd = CONTENT_DIV.search(block, pos)
        if not h4 and not cd:
            return sections
        if h4 and (not cd or h4.start() < cd.start()):
            sections.append({"heading": collapse_ws(strip_tags(h4.group(1))), "chunks": []})
            pos = h4.end()
            continue
        inner, end = _div_inner(block, cd.start())
        strongs = STRONG.findall(block[pos:cd.start()])
        if not sections:
            sections.append({"heading": "", "chunks": []})
        sections[-1]["chunks"].append({"subsection": strip_tags(strongs[-1]) if strongs else "", "inner": inner})
        pos = end


def section_block(section: dict) -> dict:
    """Rule 2 over every content div of one section: section_quote per div (the FDA comment mode starts
    afresh in each subsection), the results concatenated in page order."""
    chunks = [{**c, "paragraphs": parse_paragraphs(c["inner"])} for c in section["chunks"]]
    quote, comments, headings, has_underline = [], [], [], False
    for c in chunks:
        q = section_quote(c["paragraphs"])
        if c["subsection"]:
            headings.append(c["subsection"])
        headings.extend(q["subsection_headings"])
        quote.extend(q["quote_paragraphs"])
        comments.extend(q["comments"])
        has_underline = has_underline or q["has_underline"]
    return {
        "section_heading": section["heading"],
        "section": srlc_section_code(section["heading"]),
        "content_divs": len(chunks),
        "raw_html": collapse_ws(" ".join(c["inner"] for c in chunks)),
        "text": " ".join(p["text"] for c in chunks for p in c["paragraphs"] if p["kind"] == "text"),
        "quote_paragraphs": quote,
        "comment_type": overall_comment_type(comments, has_underline),
        "comments": comments,
        "subsection_headings": headings,
    }


def parse_detail_page(page: str, drug_id: str) -> dict:
    """One detail page -> drug header plus every (approval date, supplement, section) block.

    Each block holds the raw inner HTML of its content divs (underline markup preserved), the parsed
    text, the SrLC section code, headings, FDA comment type and the quote paragraphs."""
    if not is_detail_page(page):
        raise SrlcError(f"page for DrugNameID={drug_id} is not an SrLC detail page")
    header = HEADER_H3.search(page)
    if not header:
        raise SrlcError(f"detail page {drug_id}: no drug header")
    brand = strip_tags(header.group(1))
    application_text = strip_tags(header.group(2))
    ingredient_m = INGREDIENT_H5.search(page, header.end())
    ingredient = strip_tags(ingredient_m.group(1)) if ingredient_m else ""
    acc = ACCORDION_START.search(page)
    body = page[acc.start():] if acc else page[header.end():]
    heads = list(ACCORDION_HEADER.finditer(body))
    blocks = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        for section in block_sections(body[h.end():end]):
            blocks.append({"approval_date": _iso_date(h.group(1)), "supplement_text": strip_tags(h.group(2)),
                           **section_block(section)})
    return {"drug_id": drug_id, "brand": brand, "application_text": application_text,
            "application": application_from_text(application_text), "ingredient": ingredient, "blocks": blocks}


def supplement_number(text: str) -> str:
    """'SUPPL-31' -> '31'; several numbers are joined with '+' ('SUPPL-31, SUPPL-32' -> '31+32')."""
    nums = re.findall(r"\d+", text)
    return "+".join(nums) if nums else re.sub(r"[^A-Za-z0-9]+", "", text).upper() or "NA"


def item_id(application: str, approval_date: str, supplement: str, section: str) -> str:
    return f"srlc-{application}-{approval_date.replace('-', '')}-SUPPL{supplement}-{section}"


def sample_hash(application: str, approval_date: str, supplement: str, section: str) -> str:
    key = f"{HASH_PREFIX}|{application}|{approval_date}|SUPPL{supplement}|{section}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- fetching

class Session:
    """One SrLC browsing session: cookie jar, fixed User-Agent, one request per second, a request log,
    retries on transient errors and a hard stop on 403, challenge pages or repeated failures.
    ``opener`` and ``sleep`` are injectable for tests."""

    def __init__(self, opener=None, log_path: Path | None = REQUEST_LOG, sleep=time.sleep, min_gap_s: float = MIN_GAP_S,
                 index_cache: Path | None = None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.log_path, self.sleep, self.min_gap_s = log_path, sleep, min_gap_s
        self.index_cache = index_cache
        self._next = 0.0
        self.requests = 0
        self.failures = 0
        self.started = False

    def _once(self, url: str, data: bytes | None, referer: str | None) -> tuple[int, bytes, str]:
        wait = self._next - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._next = time.monotonic() + self.min_gap_s
        self.requests += 1
        headers = {"User-Agent": USER_AGENT}
        if referer:
            headers["Referer"] = referer
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        status, body, note = 0, b"", ""
        try:
            with self.opener.open(req, timeout=120) as resp:
                status, body = resp.status, resp.read()
                if resp.geturl() != url:
                    note = f"redirected to {resp.geturl()}"
        except urllib.error.HTTPError as err:
            status = err.code
            body = err.read() or b""
            err.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            note = f"{type(err).__name__}: {err}"
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                method = "POST" if data is not None else "GET"
                fh.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t{status or 'ERR'}\t{len(body)}\t{method}\t{url}\t{note}\n")
        return status, body, note

    def request(self, url: str, data: bytes | None = None, referer: str | None = None) -> str:
        if urllib.parse.urlsplit(url).netloc != SRLC_HOST:
            raise SrlcError(f"refusing to fetch outside {SRLC_HOST}: {url}")
        if not self.started:
            self.started = True
            status, body, note = self._once(SRLC_INDEX, None, None)  # start page: sets the session cookies
            self._check(status, body, SRLC_INDEX)
            if status != 200:
                raise SrlcError(f"start page gave HTTP {status or note}; no session")
            if self.index_cache:
                self.index_cache.parent.mkdir(parents=True, exist_ok=True)
                self.index_cache.write_bytes(body)
            if url == SRLC_INDEX and data is None:
                return body.decode("utf-8", "replace")
        for attempt in range(len(RETRY_BACKOFF_S) + 1):
            status, body, note = self._once(url, data, referer)
            if status == 200:
                page = body.decode("utf-8", "replace")
                self._check(status, body, url)
                self.failures = 0
                return page
            self._check(status, body, url)
            if status and status not in TRANSIENT:
                raise SrlcError(f"HTTP {status} for {url}")
            if attempt < len(RETRY_BACKOFF_S):
                self.sleep(RETRY_BACKOFF_S[attempt])
        self.failures += 1
        if self.failures >= ABORT_AFTER_FAILURES:
            raise SrlcBlocked(f"{self.failures} requests in a row failed; last: {url} ({status or note})")
        raise SrlcError(f"gave up after retries: {url} ({status or note})")

    def _check(self, status: int, body: bytes, url: str) -> None:
        if status == 403:
            raise SrlcBlocked(f"HTTP 403 for {url}: stopping, not working around it")
        if status == 200 and is_challenge_page(body.decode("utf-8", "replace")[:20000]):
            raise SrlcBlocked(f"challenge page for {url}: stopping, not working around it")


def month_range(first: str, last: str) -> list[tuple[int, int]]:
    """('2016-01', '2016-03') -> [(2016, 1), (2016, 2), (2016, 3)]."""
    y0, m0 = (int(x) for x in first.split("-"))
    y1, m1 = (int(x) for x in last.split("-"))
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_bounds(year: int, month: int) -> tuple[str, str]:
    first = date(year, month, 1)
    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return first.strftime("%m/%d/%Y"), last.strftime("%m/%d/%Y")


def search_form(year: int, month: int) -> bytes:
    lo, hi = month_bounds(year, month)
    fields = [("date_range_from", lo), ("date_range_to", hi)] + [("section", s) for s in SEARCH_SECTIONS] + [("Datesearch", "")]
    return urllib.parse.urlencode(fields).encode()


def search_cache_path(year: int, month: int, raw_dir: Path = RAW_SRLC) -> Path:
    return raw_dir / "search" / f"{year:04d}-{month:02d}.html"


def detail_cache_path(drug_id: str, raw_dir: Path = RAW_SRLC) -> Path:
    return raw_dir / "detail" / f"{drug_id}.html"


def crosswalk_applications(molecules_dir: Path = MOLECULES) -> dict[str, list[dict]]:
    """application -> [{inn, setid, status}] for every NDA/BLA in the crosswalk (current and retired setids)."""
    out: dict[str, list[dict]] = {}
    for path in sorted(molecules_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "us" not in data:
            continue
        for entry in data["us"]:
            for app in entry.get("application_numbers", []):
                if re.fullmatch(r"(NDA|BLA)\d{6}", app):
                    row = {"inn": data["inn"], "setid": entry["setid"], "status": entry.get("status", "current"),
                           "title": entry.get("title") or "", "brand_names": list(entry.get("brand_names") or [])}
                    if row not in out.setdefault(app, []):
                        out[app].append(row)
    return out


def srlc_brand_in_label(item: dict, entry: dict) -> bool:
    """Whether one of the item's SrLC brand names appears in the crosswalk title or brand names of a
    setid. Information only: rule 6 maps items to setids by application number, so an authorized
    generic or a sibling product under the same application is a covered setid without its brand."""
    hay = f" {normalise(' '.join([entry.get('title') or ''] + list(entry.get('brand_names') or [])))} "
    needles = []
    for brand in [item.get("brand")] + list(item.get("brands") or []):
        toks = tokens(brand or "")
        if toks:
            needles.append(" ".join(toks))
            if len(toks[0]) >= 3 and not toks[0].isdigit():
                needles.append(toks[0])  # the brand word alone: SrLC writes 'DOLOPHINE HYDROCHLORIDE', 'OMNIPAQUE 140'
    return any(f" {n} " in hay for n in needles)


def application_digits(app: str | None) -> str | None:
    return re.sub(r"\D", "", app) if app else None


def in_corpus_drug_ids(rows: list[dict], apps: dict[str, list[dict]]) -> set[str]:
    """Drugs whose application number is in the crosswalk. Matched on the six digits, so a product
    that moved from NDA to BLA (the March 2020 biologics transition) is found under either prefix."""
    digits = {application_digits(a) for a in apps}
    return {r["drug_id"] for r in rows if r["application"] and application_digits(r["application"]) in digits}


def fetch_searches(session: Session, months: list[tuple[int, int]], raw_dir: Path = RAW_SRLC, log=print) -> int:
    """Cache every monthly search page not yet cached; returns the number fetched."""
    fetched = 0
    for year, month in months:
        path = search_cache_path(year, month, raw_dir)
        if path.exists() and is_results_page(path.read_text(encoding="utf-8", errors="replace")):
            continue
        page = session.request(SRLC_SEARCH, data=search_form(year, month), referer=SRLC_INDEX)
        if not is_results_page(page):
            raise SrlcError(f"search {year}-{month:02d} came back as the start page ({len(page)} bytes); "
                            "the session or the Datesearch field was not accepted")
        rows = parse_search_page(page)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")
        fetched += 1
        log(f"search {year}-{month:02d}: {len(rows)} drugs ({len(page)} bytes)")
    return fetched


def cached_search_rows(raw_dir: Path = RAW_SRLC) -> list[dict]:
    rows = []
    for path in sorted((raw_dir / "search").glob("*.html")) if (raw_dir / "search").exists() else []:
        for row in parse_search_page(path.read_text(encoding="utf-8", errors="replace")):
            row["search_month"] = path.stem
            rows.append(row)
    return rows


def fetch_details(session: Session, drug_ids: list[str], raw_dir: Path = RAW_SRLC, log=print, limit: int | None = None) -> int:
    fetched = 0
    for drug_id in drug_ids:
        if limit is not None and fetched >= limit:
            break
        path = detail_cache_path(drug_id, raw_dir)
        if path.exists():
            continue
        page = session.request(SRLC_DETAIL.format(drug_id=drug_id), referer=SRLC_SEARCH)
        if not is_detail_page(page):
            raise SrlcError(f"detail page {drug_id} does not look like a detail page ({len(page)} bytes)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page, encoding="utf-8")
        fetched += 1
        log(f"detail {drug_id}: {len(page)} bytes")
    return fetched


def cmd_fetch(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir)
    session = Session(log_path=raw_dir / "requests.tsv", index_cache=raw_dir / "index.html")
    months = month_range(args.months_from, args.months_to)
    try:
        n_search = fetch_searches(session, months, raw_dir)
        rows = cached_search_rows(raw_dir)
        wanted = sorted(in_corpus_drug_ids(rows, crosswalk_applications(Path(args.molecules))), key=int)
        print(f"{len(rows)} search rows, {len({r['drug_id'] for r in rows})} drugs, {len(wanted)} with an application in the crosswalk")
        n_detail = 0 if args.no_details else fetch_details(session, wanted, raw_dir, limit=args.max_details)
    except SrlcBlocked as err:
        print(f"STOPPED: {err}", file=sys.stderr)
        print(f"requests this run: {session.requests}", file=sys.stderr)
        return 3
    except SrlcError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        print(f"requests this run: {session.requests}", file=sys.stderr)
        return 2
    print(f"fetched {n_search} search pages and {n_detail} detail pages; requests this run: {session.requests}")
    return 0


# ---------------------------------------------------------------- items (rule 1)

def crosswalk_form(application: str | None, apps: dict[str, list[dict]]) -> tuple[str | None, bool]:
    """The crosswalk's spelling of an application: itself when present, else the one crosswalk application
    with the same six digits (a product that moved from NDA to BLA). Returns (application, in_corpus)."""
    if not application:
        return None, False
    if application in apps:
        return application, True
    same = [a for a in apps if application_digits(a) == application_digits(application)]
    if len(same) == 1:
        return same[0], True
    return application, False


def build_items(drugs: list[dict], search_rows: list[dict], apps: dict[str, list[dict]]) -> list[dict]:
    """Items from parsed detail pages: one per (application, approval date, supplement, SrLC section).
    Blocks of one page that map to the same section are merged, and so are the pages of two brands that
    share one application; duplicated text is kept once."""
    posted = {(r["drug_id"], r["approval_date"]): r["posted_date"] for r in search_rows}
    items: dict[str, dict] = {}
    for d in drugs:
        app, in_corpus = crosswalk_form(d["application"], apps)
        for b in d["blocks"]:
            if not b["approval_date"]:
                continue
            section = b["section"] or "other"
            supplement = supplement_number(b["supplement_text"])
            key = item_id(app or "NONE", b["approval_date"], supplement, section)
            it = items.get(key)
            if it is None:
                it = items[key] = {
                    "id": key, "application": app, "application_srlc": d["application"], "in_corpus": in_corpus,
                    "brand": d["brand"], "brands": [], "ingredient": d["ingredient"], "drug_ids": [],
                    "detail_urls": [], "approval_date": b["approval_date"], "year": int(b["approval_date"][:4]),
                    "posted_date": None, "supplement": supplement, "supplement_text": b["supplement_text"],
                    "section": section, "section_headings": [], "subsection_headings": [], "comment_types": [],
                    "raw_html": [], "text": [], "quote_paragraphs": [],
                }
            if d["drug_id"] not in it["drug_ids"]:
                it["drug_ids"].append(d["drug_id"])
                it["detail_urls"].append(SRLC_DETAIL.format(drug_id=d["drug_id"]))
                it["brands"].append(d["brand"])
            it["posted_date"] = it["posted_date"] or posted.get((d["drug_id"], b["approval_date"]))
            _add_unique(it["section_headings"], [b["section_heading"]])
            _add_unique(it["subsection_headings"], b["subsection_headings"])
            _add_unique(it["comment_types"], [b["comment_type"]])
            if b["raw_html"] not in it["raw_html"]:
                it["raw_html"].append(b["raw_html"])
                it["text"].append(b["text"])
                _add_unique(it["quote_paragraphs"], b["quote_paragraphs"])
    out = []
    for it in items.values():
        kinds = it.pop("comment_types")
        it["comment_type"] = kinds[0] if len(kinds) == 1 else COMMENT_MIXED
        it["raw_html"] = "\n".join(it["raw_html"])
        it["text"] = "\n".join(it["text"])
        it["kept_sentences"] = kept_sentences(it["quote_paragraphs"])
        it["quote_tokens"] = quote_tokens(it["kept_sentences"])
        it["usable"] = usable(it["kept_sentences"])
        it["hash"] = sample_hash(it["application"] or "NONE", it["approval_date"], it["supplement"], it["section"])
        out.append(it)
    out.sort(key=lambda i: (i["approval_date"], i["id"]))
    return out


def _add_unique(target: list, values: list) -> None:
    for v in values:
        if v and v not in target:
            target.append(v)


# ---------------------------------------------------------------- presence (rule 5; used by the check task)

class SectionText:
    """Normalised forms of one version's section-group text (one file, or several concatenated in file
    order): the flat text for substring checks, and the token lists of every line and of every pair of
    consecutive lines for the coverage check. Each line remembers its file, line number and raw text
    (verbatim, indentation included) so a match can be cited. Blank lines and lines without an
    alphanumeric character are not lines here, so 'consecutive lines' means consecutive non-empty lines:
    a pair may span a blank line between two paragraphs, and the last line of one file pairs with the
    first line of the next file in the group."""

    def __init__(self, text: str | None = None, files: list[tuple[str, str]] | None = None):
        parts = files if files is not None else [("", text or "")]
        self.origins: list[tuple[str, int, str]] = []
        self.lines: list[list[str]] = []
        for path, body in parts:
            for n, raw in enumerate(body.splitlines(), start=1):
                norm = normalise(raw)
                if norm:
                    self.origins.append((path, n, raw))
                    self.lines.append(norm.split())
        self.line_sets = [set(line) for line in self.lines]
        self.pairs = [a + b for a, b in zip(self.lines, self.lines[1:])]
        self.pair_sets = [a | b for a, b in zip(self.line_sets, self.line_sets[1:])]
        self.starts: list[int] = []
        pieces, pos = [], 0
        for line in self.lines:
            self.starts.append(pos)
            piece = " ".join(line)
            pieces.append(piece)
            pos += len(piece) + 1
        self.flat = " ".join(pieces)

    def cite(self, indexes: list[int]) -> list[dict]:
        return [{"file": Path(self.origins[i][0]).name if self.origins[i][0] else "", "line": self.origins[i][1],
                 "text": self.origins[i][2]} for i in indexes]

    def lines_at(self, start: int, end: int) -> list[dict]:
        """The lines whose flat-text spans overlap [start, end)."""
        first = bisect.bisect_right(self.starts, start) - 1
        last = bisect.bisect_right(self.starts, max(start, end - 1)) - 1
        return self.cite(list(range(max(first, 0), max(last, 0) + 1)))

    def lines_of(self, candidate: int) -> list[dict]:
        """The line (index < len(lines)) or pair (index - len(lines)) a candidate index names."""
        n = len(self.lines)
        return self.cite([candidate] if candidate < n else [candidate - n, candidate - n + 1])

    def candidates(self):
        """Every line, then every pair, each with its token list and token set."""
        yield from zip(self.lines, self.line_sets)
        yield from zip(self.pairs, self.pair_sets)


def token_coverage(sentence_tokens: list[str], line_tokens: list[str]) -> float:
    if not sentence_tokens:
        return 0.0
    sm = difflib.SequenceMatcher(None, sentence_tokens, line_tokens, autojunk=False)
    return sum(block.size for block in sm.get_matching_blocks()) / len(sentence_tokens)


def locate_sentence(sentence: str, section: SectionText, line_coverage: float = LINE_COVERAGE,
                    substring_only: bool = False) -> dict | None:
    """Rule 5 for one sentence: how it was found (substring of the flat text, or token coverage over one
    line or a pair of consecutive lines) and the label lines that matched; None when it is not found."""
    toks = tokens(sentence)
    if not toks:
        return None
    needle = " ".join(toks)
    pos = section.flat.find(needle)
    if pos >= 0:
        return {"how": "substring", "coverage": 1.0, "lines": section.lines_at(pos, pos + len(needle))}
    if substring_only:
        return None
    need = len(toks) * line_coverage
    for idx, (line, line_set) in enumerate(section.candidates()):
        if sum(1 for t in toks if t in line_set) < need:  # matching blocks cannot exceed the shared tokens
            continue
        cov = token_coverage(toks, line)
        if cov >= line_coverage:
            return {"how": "coverage", "coverage": round(cov, 4), "lines": section.lines_of(idx)}
    return None


def sentence_found(sentence: str, section: SectionText, line_coverage: float = LINE_COVERAGE,
                   substring_only: bool = False) -> bool:
    return locate_sentence(sentence, section, line_coverage, substring_only) is not None


def best_coverage(sentence: str, section: SectionText) -> dict:
    """The closest line or pair for one sentence: 1.0 when it is a substring, else the highest token
    coverage over every line and pair (lines whose shared tokens cannot beat the best so far are skipped)."""
    toks = tokens(sentence)
    if not toks:
        return {"coverage": 0.0, "lines": []}
    needle = " ".join(toks)
    pos = section.flat.find(needle)
    if pos >= 0:
        return {"coverage": 1.0, "lines": section.lines_at(pos, pos + len(needle))}
    best, best_idx = 0.0, None
    for idx, (line, line_set) in enumerate(section.candidates()):
        if sum(1 for t in toks if t in line_set) / len(toks) <= best:
            continue
        cov = token_coverage(toks, line)
        if cov > best:
            best, best_idx = cov, idx
    return {"coverage": round(best, 4), "lines": section.lines_of(best_idx) if best_idx is not None else []}


def quote_presence(sentences: list[str], section: SectionText, line_coverage: float = LINE_COVERAGE,
                   quote_token_share: float = QUOTE_TOKEN_SHARE, substring_only: bool = False) -> dict:
    """Rule 5 for one version: which kept sentences are found (with the lines that matched), and whether
    the found sentences hold enough of the quote's tokens."""
    total = quote_tokens(sentences)
    matches = []
    held = 0
    for s in sentences:
        hit = locate_sentence(s, section, line_coverage, substring_only)
        if hit:
            held += len(tokens(s))
        matches.append({"sentence": s, "tokens": len(tokens(s)), **(hit or {"how": None, "coverage": None, "lines": []})})
    share = held / total if total else 0.0
    return {"present": total > 0 and share >= quote_token_share, "share": round(share, 4),
            "found": sum(1 for m in matches if m["how"]), "sentences": len(sentences), "matches": matches}


def quote_score(sentences: list[str], section: SectionText) -> dict:
    """Token-weighted mean of each kept sentence's best coverage in the section group: 1.0 means every
    sentence is there verbatim, 0.0 means no token of any sentence lines up."""
    total = quote_tokens(sentences)
    per = []
    weighted = 0.0
    for s in sentences:
        b = best_coverage(s, section)
        n = len(tokens(s))
        weighted += n * b["coverage"]
        per.append({"sentence": s, "tokens": n, "coverage": b["coverage"], "lines": b["lines"]})
    return {"score": round(weighted / total, 4) if total else 0.0, "per_sentence": per}


# ---------------------------------------------------------------- covered setids (rule 6)

class Pinned:
    """Published dates of the pinned versions per setid, from sources/us/*.json only."""

    def __init__(self, sources_dir: Path = SOURCES_US):
        self.dir = Path(sources_dir)
        self._dates: dict[str, list[str] | None] = {}

    def dates(self, setid: str) -> list[str] | None:
        if setid not in self._dates:
            path = self.dir / f"{setid}.json"
            if not path.exists():
                self._dates[setid] = None
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._dates[setid] = sorted(v["published_date"] for v in data.get("versions", []))
        return self._dates[setid]


def setid_coverage(entry: dict, approval_date: str, pinned: Pinned, window_days: int = WINDOW_DAYS) -> dict:
    """Rule 6 for one crosswalk setid: covered when its first pinned version is on or before
    approval - window and it is current or its last pinned version is on or after approval + window."""
    dates = pinned.dates(entry["setid"])
    row = {"setid": entry["setid"], "inn": entry["inn"], "status": entry["status"], "title": entry.get("title") or "",
           "first_published": dates[0] if dates else None, "last_published": dates[-1] if dates else None,
           "versions": len(dates) if dates else 0, "covered": False, "reason": None}
    if not dates:
        row["reason"] = "no pinned versions in sources/us"
        return row
    approval = date.fromisoformat(approval_date)
    before = row["first_published"] <= (approval - timedelta(days=window_days)).isoformat()
    after = entry["status"] != "retired" or row["last_published"] >= (approval + timedelta(days=window_days)).isoformat()
    reasons = []
    if not before:
        reasons.append(f"first version after approval - {window_days} days")
    if not after:
        reasons.append(f"retired before approval + {window_days} days")
    row["covered"] = not reasons
    row["reason"] = "; ".join(reasons) or None
    return row


def item_setids(item: dict, apps: dict[str, list[dict]], pinned: Pinned) -> list[dict]:
    seen, rows = set(), []
    for entry in apps.get(item["application"] or "", []):
        if entry["setid"] in seen:
            continue
        seen.add(entry["setid"])
        rows.append({**setid_coverage(entry, item["approval_date"], pinned), "srlc_brand_in_label": srlc_brand_in_label(item, entry)})
    return rows


# ---------------------------------------------------------------- eligibility and sample (rules 9-10)

def classify(items: list[dict], apps: dict[str, list[dict]], pinned: Pinned) -> None:
    """Annotate every item with its eligibility (rule 9) and the reasons it falls short."""
    for it in items:
        reasons = []
        if not (f"{YEARS[0]}-01-01" <= it["approval_date"] <= f"{YEARS[-1]}-12-31"):
            reasons.append("outside 2016-2025")
        if it["section"] not in SAMPLED_SECTIONS:
            reasons.append("section not sampled")
        if not it["usable"]:
            reasons.append("quote not usable")
        it["setids"] = item_setids(it, apps, pinned) if it["in_corpus"] else []
        it["covered_setids"] = [s for s in it["setids"] if s["covered"]]
        if not it["in_corpus"]:
            reasons.append("application not in crosswalk")
        elif not it["covered_setids"]:
            reasons.append("no covered setid")
        it["ineligible_reasons"] = reasons
        it["eligible"] = not reasons


def select_sample(eligible: list[dict]) -> tuple[list[dict], dict]:
    """Rule 10: per year, hash order, at most one item per application per year and two overall."""
    taken_overall: dict[str, int] = {}
    sample, per_year = [], {}
    for year in YEARS:
        candidates = sorted((i for i in eligible if i["year"] == year), key=lambda i: i["hash"])
        taken_year: set[str] = set()
        picked, skipped = [], []
        for it in candidates:
            if len(picked) >= PER_YEAR:
                break
            app = it["application"]
            if app in taken_year:
                skipped.append({"id": it["id"], "reason": "application already taken this year"})
                continue
            if taken_overall.get(app, 0) >= MAX_PER_APPLICATION_OVERALL:
                skipped.append({"id": it["id"], "reason": "application already taken twice overall"})
                continue
            picked.append(it)
            taken_year.add(app)
            taken_overall[app] = taken_overall.get(app, 0) + 1
        sample.extend(picked)
        per_year[str(year)] = {"eligible": len(candidates), "taken": len(picked), "shortfall": PER_YEAR - len(picked),
                               "skipped": skipped}
    return sample, per_year


def _count(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))


def year_section_mix(items: list[dict]) -> dict:
    mix: dict[str, dict[str, int]] = {}
    for it in items:
        row = mix.setdefault(str(it["year"]), {})
        row[it["section"]] = row.get(it["section"], 0) + 1
    return {y: dict(sorted(row.items())) for y, row in sorted(mix.items())}


def canonical_sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def constants() -> dict:
    return {
        "PARSE_VERSION": PARSE_VERSION, "HASH_PREFIX": HASH_PREFIX, "YEARS": list(YEARS), "PER_YEAR": PER_YEAR,
        "MAX_PER_APPLICATION_PER_YEAR": MAX_PER_APPLICATION_PER_YEAR,
        "MAX_PER_APPLICATION_OVERALL": MAX_PER_APPLICATION_OVERALL, "SAMPLED_SECTIONS": list(SAMPLED_SECTIONS),
        "UNSAMPLED_SECTIONS": list(UNSAMPLED_SECTIONS), "MIN_SENTENCE_TOKENS": MIN_SENTENCE_TOKENS,
        "MIN_QUOTE_TOKENS": MIN_QUOTE_TOKENS, "LINE_COVERAGE": LINE_COVERAGE, "QUOTE_TOKEN_SHARE": QUOTE_TOKEN_SHARE,
        "WINDOW_DAYS": WINDOW_DAYS, "SENSITIVITY": SENSITIVITY, "SPECIFIC_POPULATION_CODES": list(SPECIFIC_POPULATION_CODES),
        "SECTION_GROUPS": {k: list(v) for k, v in SECTION_GROUPS.items()}, "SEARCH_MONTHS": list(SEARCH_MONTHS),
        "SEARCH_SECTIONS": list(SEARCH_SECTIONS), "MAX_HEADING_TOKENS": MAX_HEADING_TOKENS, "HEADING_RULE": HEADING_RULE,
        "ABBREVIATIONS": list(ABBREVIATIONS), "SHORT_QUOTE_TOKENS": SHORT_QUOTE_TOKENS,
        "COMMENT_TYPES": [COMMENT_UNDERLINED, COMMENT_NEWLY_ADDED, COMMENT_REFER_TO_LABEL, COMMENT_OTHER, COMMENT_NONE,
                          COMMENT_NONE_UNDERLINED, COMMENT_MIXED],
        "HASH_KEY": f"sha256('{HASH_PREFIX}|<APPLICATION>|<YYYY-MM-DD>|SUPPL<n>|<SECTION>') as lower-case hex",
    }


def cache_facts(raw_dir: Path) -> dict:
    search = sorted((raw_dir / "search").glob("*.html")) if (raw_dir / "search").exists() else []
    detail = sorted((raw_dir / "detail").glob("*.html")) if (raw_dir / "detail").exists() else []
    log = raw_dir / "requests.tsv"
    return {"search_pages": len(search), "detail_pages": len(detail),
            "request_log_lines": len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0}


def parse_cache(raw_dir: Path = RAW_SRLC, molecules_dir: Path = MOLECULES) -> dict:
    rows = cached_search_rows(raw_dir)
    apps = crosswalk_applications(molecules_dir)
    drugs = []
    for path in sorted((raw_dir / "detail").glob("*.html"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        drugs.append(parse_detail_page(path.read_text(encoding="utf-8", errors="replace"), path.stem))
    items = build_items(drugs, rows, apps)
    return {
        "parse_version": PARSE_VERSION,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "cache": cache_facts(raw_dir),
        "search_rows": len(rows),
        "srlc_drugs_seen": len({r["drug_id"] for r in rows}),
        "in_corpus_drugs": len(in_corpus_drug_ids(rows, apps)),
        "drugs": [{k: v for k, v in d.items() if k != "blocks"} | {"blocks": len(d["blocks"])} for d in drugs],
        "items": items,
    }


def population_report(parsed: dict, items: list[dict]) -> dict:
    in_range = [i for i in items if f"{YEARS[0]}-01-01" <= i["approval_date"] <= f"{YEARS[-1]}-12-31" and i["in_corpus"]]
    sampled = [i for i in in_range if i["section"] in SAMPLED_SECTIONS]
    usable_items = [i for i in sampled if i["usable"]]
    covered = [i for i in usable_items if i["covered_setids"]]
    eligible = [i for i in covered if i["eligible"]]
    not_usable = [i for i in sampled if not i["usable"]]
    no_cover = [i for i in usable_items if not i["covered_setids"]]
    no_cover_reasons: dict[str, int] = {}
    for i in no_cover:
        reasons = {part for s in i["setids"] if s["reason"] for part in s["reason"].split("; ")}
        key = "; ".join(sorted(reasons)) if i["setids"] else "no setid in crosswalk"
        no_cover_reasons[key] = no_cover_reasons.get(key, 0) + 1
    return {
        "generated": parsed["generated"], "parse_version": parsed["parse_version"], "search_months": list(SEARCH_MONTHS),
        "cache": parsed["cache"], "search_rows": parsed["search_rows"], "srlc_drugs_seen": parsed["srlc_drugs_seen"],
        "in_corpus_drugs": parsed["in_corpus_drugs"], "detail_pages_parsed": len(parsed["drugs"]),
        "items_all_years": len(items), "items_not_in_corpus": sum(1 for i in items if not i["in_corpus"]),
        "items_2016_2025": len(in_range),
        "by_year": _count(i["year"] for i in in_range),
        "by_section": _count(i["section"] for i in in_range),
        "by_comment_type": _count(i["comment_type"] for i in in_range),
        "by_year_and_section": year_section_mix(in_range),
        "funnel": {"in_range": len(in_range), "sampled_section": len(sampled), "usable": len(usable_items),
                   "with_covered_setid": len(covered), "eligible": len(eligible)},
        "excluded": {
            "section_not_sampled": _count(i["section"] for i in in_range if i["section"] not in SAMPLED_SECTIONS),
            "not_usable_by_comment_type": _count(i["comment_type"] for i in not_usable),
            "not_usable_by_reason": _count(
                ("no quote text" if not i["quote_paragraphs"] else
                 "no sentence with >= 6 tokens" if not i["kept_sentences"] else "kept sentences under 12 tokens")
                for i in not_usable),
            "no_covered_setid": dict(sorted(no_cover_reasons.items())),
        },
        "usable_by_comment_type": _count(i["comment_type"] for i in usable_items),
        "eligible_by_year": _count(i["year"] for i in eligible),
        "eligible_by_section": _count(i["section"] for i in eligible),
        "eligible_by_year_and_section": year_section_mix(eligible),
        "eligible_applications": len({i["application"] for i in eligible}),
    }


def sample_item(it: dict) -> dict:
    keep = ("id", "application", "application_srlc", "brand", "brands", "ingredient", "drug_ids", "detail_urls",
            "approval_date", "year", "posted_date", "supplement", "supplement_text", "section", "section_headings",
            "subsection_headings", "comment_type", "raw_html", "text", "quote_paragraphs", "kept_sentences",
            "quote_tokens", "hash")
    out = {k: it[k] for k in keep}
    out["section_group_codes"] = list(SECTION_GROUPS[it["section"]])
    out["covered_setids"] = it["covered_setids"]
    out["uncovered_setids"] = [s for s in it["setids"] if not s["covered"]]
    return out


def sample_document(parsed: dict, items: list[dict], population: dict) -> dict:
    eligible = [i for i in items if i["eligible"]]
    sample, per_year = select_sample(eligible)
    rows = [sample_item(i) for i in sample]
    return {
        "title": "SrLC validation sample: 50 safety labeling changes, 2016-2025, fixed before any matching",
        "generated": parsed["generated"],
        "rules": RULES,
        "window_rationale": WINDOW_RATIONALE,
        "constants": constants(),
        "section_groups": {k: list(v) for k, v in SECTION_GROUPS.items()},
        "generation": {"parse_version": parsed["parse_version"], "search_months": list(SEARCH_MONTHS),
                       "cache": parsed["cache"], "srlc_drugs_seen": parsed["srlc_drugs_seen"],
                       "in_corpus_drugs": parsed["in_corpus_drugs"], "items_2016_2025": population["items_2016_2025"],
                       "eligible_items": len(eligible), "eligible_applications": population["eligible_applications"]},
        "per_year": per_year,
        "sample_size": len(rows),
        "mix": {"by_year_and_section": year_section_mix(sample), "by_section": _count(i["section"] for i in sample),
                "by_comment_type": _count(i["comment_type"] for i in sample),
                "applications_taken_twice": sorted(a for a, n in _count(i["application"] for i in sample).items() if n > 1)},
        "items": rows,
        "items_sha256": canonical_sha256(rows),
        "revisions": REVISIONS,
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_parse(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir)
    parsed = parse_cache(raw_dir, Path(args.molecules))
    write_json(Path(args.out), parsed)
    items = parsed["items"]
    in_range = [i for i in items if f"{YEARS[0]}-01-01" <= i["approval_date"] <= f"{YEARS[-1]}-12-31"]
    print(f"{len(parsed['drugs'])} detail pages, {len(items)} items, {len(in_range)} in 2016-2025, "
          f"{sum(1 for i in in_range if i['usable'])} usable; written to {args.out}")
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    items_path = Path(args.items)
    if not items_path.exists():
        print(f"ERROR: {items_path} missing; run: python3 -I tools/srlc.py parse", file=sys.stderr)
        return 2
    parsed = json.loads(items_path.read_text(encoding="utf-8"))
    if parsed.get("parse_version") != PARSE_VERSION:
        print(f"ERROR: {items_path} has parse_version {parsed.get('parse_version')}, code has {PARSE_VERSION}; rerun parse",
              file=sys.stderr)
        return 2
    apps = crosswalk_applications(Path(args.molecules))
    items = parsed["items"]
    classify(items, apps, Pinned(Path(args.sources)))
    population = population_report(parsed, items)
    sample = sample_document(parsed, items, population)
    write_json(Path(args.population_out), population)
    write_json(Path(args.sample_out), sample)
    short = {y: v["shortfall"] for y, v in sample["per_year"].items() if v["shortfall"]}
    print(f"items 2016-2025: {population['items_2016_2025']}; eligible: {population['funnel']['eligible']}; "
          f"sample: {sample['sample_size']}; shortfalls: {short or 'none'}; sha256 {sample['items_sha256']}")
    print(f"written {args.population_out} and {args.sample_out}")
    return 0


# ---------------------------------------------------------------- check (rules 4-8 and 11; reads build/us, no network)

CATEGORIES = ("match", "late", "early", "absent")
HEADLINE_PRIORITY = ("late", "early", "absent")  # rule 8: an item's category when no covered setid matches
PRESENCE_VARIANTS = {
    "preregistered": {"substring_only": False, "line_coverage": LINE_COVERAGE, "quote_token_share": QUOTE_TOKEN_SHARE},
    "strict": {k: SENSITIVITY["strict"][k] for k in ("substring_only", "line_coverage", "quote_token_share")},
    "loose": {k: SENSITIVITY["loose"][k] for k in ("substring_only", "line_coverage", "quote_token_share")},
}
# Each reported variant = (presence parameters, match window). window_365 reuses the pre-registered
# presence. Covered setids stay as pinned in the sample under every variant: rule 6 fixed the sample
# and is not re-opened by the sensitivity runs.
VARIANTS = {
    "preregistered": ("preregistered", WINDOW_DAYS),
    "strict": ("strict", SENSITIVITY["strict"]["window_days"]),
    "loose": ("loose", SENSITIVITY["loose"]["window_days"]),
    "window_365": ("preregistered", SENSITIVITY["window_365"]["window_days"]),
}
VARIANT_WORDING = {
    "preregistered": "substring or line coverage >= 0.90; found sentences hold >= 0.80 of the tokens",
    "strict": "substring only; found sentences hold 1.00 of the tokens",
    "loose": "substring or line coverage >= 0.80; found sentences hold >= 0.60 of the tokens",
    "window_365": "pre-registered presence",
}


def days_between(later: str, earlier: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def classify_sequence(sequence: list[tuple[str, bool]], approval_date: str, window_days: int = WINDOW_DAYS) -> dict:
    """Rule 7 over one setid. ``sequence`` is [(published date, quote present)] in Published-Date order.
    A transition is a version where the quote is present and the previous version's is not; the one with
    the smallest |lag| is chosen (the earlier one on a tie). Categories are tested in the order match,
    early, late, absent, so a quote already in the first version that is removed and re-added in the
    window still counts as a match."""
    transitions = []
    for i, (published, present) in enumerate(sequence):
        if i and present and not sequence[i - 1][1]:
            transitions.append({"index": i, "published_date": published, "lag": days_between(published, approval_date)})
    chosen = min(transitions, key=lambda t: (abs(t["lag"]), t["lag"])) if transitions else None
    present_first = bool(sequence) and sequence[0][1]
    if chosen and -window_days <= chosen["lag"] <= window_days:
        category = "match"
    elif present_first or (chosen and chosen["lag"] < -window_days):
        category = "early"
    elif chosen and chosen["lag"] > window_days:
        category = "late"
    else:
        category = "absent"
    return {"category": category, "transitions": transitions, "chosen": chosen,
            "present_in_first_version": present_first, "ever_present": any(p for _, p in sequence)}


def item_outcome(categories: list[str]) -> str:
    """Rule 8: match if any covered setid matches, else the first of late, early, absent that occurs."""
    if "match" in categories:
        return "match"
    for c in HEADLINE_PRIORITY:
        if c in categories:
            return c
    return "unchecked"


def verify_sample(sample: dict) -> str:
    """The sample's item list must hash to both its own items_sha256 and the constant pinned here."""
    actual = canonical_sha256(sample.get("items", []))
    recorded = sample.get("items_sha256")
    if actual != recorded:
        raise SrlcError(f"sample item list hashes to {actual}, but the file records {recorded}; the sample was edited")
    if actual != SAMPLE_ITEMS_SHA256:
        raise SrlcError(f"sample item list hashes to {actual}, not the pinned {SAMPLE_ITEMS_SHA256}; refusing to check "
                        "a different sample")
    return actual


class LabelReader:
    """Every version of a label with its section files and blob ids, read through labels.Index. A blob's
    text is fetched once however many versions share it."""
    LS_TREE = re.compile(r"^\d{6} blob ([0-9a-f]{40})\t(.+)$")

    def __init__(self, index: labels.Index):
        self.index = index
        self._texts: dict[str, str] = {}
        self._files: dict[tuple[str, str], list[tuple[str, str]]] = {}

    def has(self, setid: str) -> bool:
        return setid in self.index.by_setid

    def versions(self, setid: str) -> list[labels.Version]:
        return self.index.label_versions(setid)

    def files(self, v: labels.Version) -> list[tuple[str, str]]:
        """(path, blob) of the section files of one version, in file-name order."""
        key = (v.commit, v.setid)
        if key not in self._files:
            folder = self.index.folder(v.setid)
            rows = []
            for line in self.index.git("ls-tree", v.commit, f"{folder}/").splitlines():
                m = self.LS_TREE.match(line)
                if m and m.group(2).endswith(".md"):
                    rows.append((m.group(2), m.group(1)))
            self._files[key] = sorted(rows)
        return self._files[key]

    def load(self, blobs: set[str]) -> None:
        missing = sorted(b for b in blobs if b not in self._texts)
        if missing:
            for blob, text in self.index.texts(missing).items():
                self._texts[blob] = text or ""

    def text(self, blob: str) -> str:
        self.load({blob})
        return self._texts[blob]


def group_files(files: list[tuple[str, str]], section: str) -> list[tuple[str, str]]:
    """Rule 4: the files of one version whose LOINC code is in the section group."""
    codes = SECTION_GROUPS[section]
    return [(p, b) for p, b in files if labels.section_code(p) in codes]


class PresenceCache:
    """Rule 5 results per distinct set of blobs and per presence variant, so a version whose section
    group did not change is not checked again."""

    def __init__(self, reader: LabelReader, sentences: list[str]):
        self.reader, self.sentences = reader, sentences
        self._sections: dict[tuple, SectionText] = {}
        self._presence: dict[tuple, dict] = {}
        self._score: dict[tuple, dict] = {}

    @staticmethod
    def key(files: list[tuple[str, str]]) -> tuple:
        return tuple(b for _, b in files)

    def section(self, files: list[tuple[str, str]]) -> SectionText:
        k = self.key(files)
        if k not in self._sections:
            self._sections[k] = SectionText(files=[(p, self.reader.text(b)) for p, b in files])
        return self._sections[k]

    def presence(self, files: list[tuple[str, str]], variant: str) -> dict:
        k = (self.key(files), variant)
        if k not in self._presence:
            self._presence[k] = quote_presence(self.sentences, self.section(files), **PRESENCE_VARIANTS[variant])
        return self._presence[k]

    def score(self, files: list[tuple[str, str]]) -> dict:
        k = self.key(files)
        if k not in self._score:
            self._score[k] = quote_score(self.sentences, self.section(files))
        return self._score[k]


def _version_ref(v: labels.Version, approval_date: str) -> dict:
    return {"version": v.version, "published_date": v.published, "lag": days_between(v.published, approval_date)}


def walk_setid(reader: LabelReader, entry: dict, section: str, sentences: list[str], approval_date: str,
               diagnose: bool = True) -> dict:
    """Rules 5 and 7 over every version of one setid, under the pre-registered presence and each
    sensitivity variant; with ``diagnose``, the extra evidence for a non-match."""
    setid = entry["setid"]
    row = {"setid": setid, "inn": entry.get("inn"), "status": entry.get("status"), "title": entry.get("title"),
           "srlc_brand_in_label": entry.get("srlc_brand_in_label"), "versions_pinned": entry.get("versions"),
           "covered": entry.get("covered", True), "coverage_reason": entry.get("reason")}
    if not reader.has(setid):
        row["error"] = "setid not in build/us"
        return row
    lv = reader.versions(setid)
    files = [reader.files(v) for v in lv]
    reader.load({b for fl in files for _, b in fl})
    cache = PresenceCache(reader, sentences)
    groups = [group_files(fl, section) for fl in files]
    present = {name: [] for name in PRESENCE_VARIANTS}
    sequence = []
    for v, g in zip(lv, groups):
        pres = {name: cache.presence(g, name) for name in PRESENCE_VARIANTS}
        p = pres["preregistered"]
        sequence.append({**_version_ref(v, approval_date), "present": p["present"], "share": p["share"], "found": p["found"],
                         "sentences": p["sentences"], "group_files": [Path(path).name for path, _ in g],
                         "strict": pres["strict"]["present"], "loose": pres["loose"]["present"]})
        for name in PRESENCE_VARIANTS:
            present[name].append(pres[name]["present"])
    results = {name: classify_sequence([(v.published, p) for v, p in zip(lv, present[pv])], approval_date, window)
               for name, (pv, window) in VARIANTS.items()}
    main = results["preregistered"]
    transitions = []
    for t in main["transitions"]:
        i = t["index"]
        pres = cache.presence(groups[i], "preregistered")
        matched = [m for m in pres["matches"] if m["how"]]
        transitions.append({**_version_ref(lv[i], approval_date), "previous_version": lv[i - 1].version,
                            "previous_published_date": lv[i - 1].published, "share": pres["share"],
                            "files": sorted({ln["file"] for m in matched for ln in m["lines"]}), "matched": matched})
    chosen = transitions[main["transitions"].index(main["chosen"])] if main["chosen"] else None
    first_present = next((i for i, p in enumerate(present["preregistered"]) if p), None)
    if main["chosen"]:
        ci, why = main["chosen"]["index"], "chosen transition"
    elif main["present_in_first_version"]:
        ci, why = 0, "first version (quote already present)"
    else:
        hi = (date.fromisoformat(approval_date) + timedelta(days=WINDOW_DAYS)).isoformat()
        shown = [i for i, v in enumerate(lv) if v.published <= hi]
        ci, why = (shown[-1], f"version in force at approval + {WINDOW_DAYS} days") if shown else (0, "first version")
    row.update({
        "versions_in_repo": len(lv), "first_published": lv[0].published, "last_published": lv[-1].published,
        "sequence": sequence, "transitions": transitions, "chosen_transition": chosen,
        "first_present_version": _version_ref(lv[first_present], approval_date) if first_present is not None else None,
        "present_in_first_version": main["present_in_first_version"], "ever_present": main["ever_present"],
        "category": main["category"],
        "chosen_version": {**_version_ref(lv[ci], approval_date), "why": why},
        "score_at_chosen_version": cache.score(groups[ci]),
        "sensitivity": {name: {"category": r["category"],
                               "chosen_transition": _version_ref(lv[r["chosen"]["index"]], approval_date) if r["chosen"] else None,
                               "present_in_first_version": r["present_in_first_version"]}
                        for name, r in results.items() if name != "preregistered"},
    })
    if diagnose and main["category"] != "match":
        row["diagnostics"] = _diagnose(reader, cache, lv, files, groups, sentences, approval_date, main)
    return row


def _diagnose(reader: LabelReader, cache: PresenceCache, lv: list, files: list, groups: list, sentences: list[str],
              approval_date: str, main: dict) -> dict:
    """Evidence for a non-match: versions published in the window, the version in force at approval, the
    nearest appearance of the quote outside the window, the best coverage score over every version of the
    section group, and every other section of the label where the quote is present (first such version)."""
    approval = date.fromisoformat(approval_date)
    lo, hi = (approval - timedelta(days=WINDOW_DAYS)).isoformat(), (approval + timedelta(days=WINDOW_DAYS)).isoformat()
    in_force = [v for v in lv if v.published <= approval_date]
    nearest = None
    if main["chosen"]:
        nearest = {"kind": "transition", **_version_ref(lv[main["chosen"]["index"]], approval_date)}
    elif main["present_in_first_version"]:
        nearest = {"kind": "first_version", **_version_ref(lv[0], approval_date)}
    best = None
    for v, g in zip(lv, groups):
        s = cache.score(g)["score"]
        if best is None or s > best["score"]:
            best = {**_version_ref(v, approval_date), "score": s}
    elsewhere: dict[str, dict] = {}
    checked: dict[str, dict] = {}
    for v, fl, g in zip(lv, files, groups):
        in_group = {b for _, b in g}
        for path, blob in fl:
            if blob in in_group:
                continue
            if blob not in checked:
                checked[blob] = quote_presence(sentences, SectionText(files=[(path, reader.text(blob))]),
                                               **PRESENCE_VARIANTS["preregistered"])
            p = checked[blob]
            code = labels.section_code(path)
            if p["present"] and code not in elsewhere:
                elsewhere[code] = {"section": code, "file": Path(path).name, **_version_ref(v, approval_date),
                                   "share": p["share"]}
    return {
        "versions_in_window": [_version_ref(v, approval_date) for v in lv if lo <= v.published <= hi],
        "version_in_force_at_approval": _version_ref(in_force[-1], approval_date) if in_force else None,
        "nearest_appearance": nearest,
        "best_score_any_version": best,
        "elsewhere_in_label": sorted(elsewhere.values(), key=lambda e: (e["published_date"], e["section"])),
    }


def summarise_item(item: dict, covered: list[dict], others: list[dict]) -> dict:
    """Rule 8 for one item from its walked setids: outcome, the version the repo shows, the sensitivity
    outcomes, and the evidence rows."""
    walked = [r for r in covered if "error" not in r]
    categories = {r["setid"]: r["category"] for r in walked}
    outcome = item_outcome(list(categories.values()))
    matching = sorted((r for r in walked if r["category"] == "match"), key=lambda r: abs(r["chosen_transition"]["lag"]))
    shown = None
    if matching:
        r = matching[0]
        shown = {"setid": r["setid"], **{k: r["chosen_transition"][k] for k in ("version", "published_date", "lag")}}
        first = r.get("first_present_version")
        if first and first["published_date"] < shown["published_date"]:
            # the quote was carried earlier, removed, and re-added at the chosen (nearest) transition
            shown["earlier_appearance"] = first
    else:
        for r in walked:
            if r["category"] != outcome:
                continue
            if r["chosen_transition"]:
                shown = {"setid": r["setid"], **{k: r["chosen_transition"][k] for k in ("version", "published_date", "lag")}}
            elif r["present_in_first_version"]:
                shown = {"setid": r["setid"], **{k: r["sequence"][0][k] for k in ("version", "published_date", "lag")},
                         "first_version": True}
            break
    sensitivity = {name: item_outcome([r["sensitivity"][name]["category"] for r in walked])
                   for name in VARIANTS if name != "preregistered"}
    if shown:
        row = next(r for r in walked if r["setid"] == shown["setid"])
        shown["srlc_brand_in_label"] = row.get("srlc_brand_in_label")
        shown["title"] = row.get("title")
    return {
        "id": item["id"], "brand": item["brand"], "ingredient": item["ingredient"], "application": item["application"],
        "approval_date": item["approval_date"], "year": item["year"], "supplement": item["supplement"],
        "section": item["section"], "section_group_codes": item["section_group_codes"],
        "section_headings": item["section_headings"], "subsection_headings": item["subsection_headings"],
        "comment_type": item["comment_type"], "quote_tokens": item["quote_tokens"], "kept_sentences": item["kept_sentences"],
        "short_quote": item["quote_tokens"] <= SHORT_QUOTE_TOKENS,
        "detail_urls": item["detail_urls"], "outcome": outcome, "categories": categories, "shown": shown,
        "match_lag": shown["lag"] if matching else None, "sensitivity": sensitivity,
        "covered_setids": covered, "other_setids": others,
    }


def _num(x: float) -> int | float:
    return int(x) if float(x).is_integer() else round(x, 1)


def lag_stats(lags: list[int]) -> dict:
    if not lags:
        return {"n": 0}
    out = {"n": len(lags), "min": min(lags), "max": max(lags), "median": _num(statistics.median(lags)),
           "mean": round(statistics.fmean(lags), 1)}
    if len(lags) >= 2:
        q1, _, q3 = statistics.quantiles(lags, n=4, method="inclusive")
        out.update({"q1": _num(q1), "q3": _num(q3)})
    else:
        out.update({"q1": lags[0], "q3": lags[0]})
    return out


def _by(items: list[dict], key) -> dict:
    out: dict[str, dict] = {}
    for it in items:
        row = out.setdefault(str(key(it)), {"items": 0, **{c: 0 for c in CATEGORIES}})
        row["items"] += 1
        row[it["outcome"]] = row.get(it["outcome"], 0) + 1
    return dict(sorted(out.items()))


def validation_report(sample: dict, items: list[dict], notes: dict, index: labels.Index, partial: bool = False,
                      build_note: str | None = None) -> dict:
    n = len(items)
    matches = [it for it in items if it["outcome"] == "match"]
    sensitivity = {}
    for name in VARIANTS:
        outcomes = [it["outcome"] if name == "preregistered" else it["sensitivity"][name] for it in items]
        m = sum(1 for o in outcomes if o == "match")
        sensitivity[name] = {"presence": VARIANT_WORDING[name], "window_days": VARIANTS[name][1], "matches": m,
                             "match_rate": round(m / n, 4) if n else None, "by_category": _count(outcomes)}
    misses, missing_notes = [], []
    for it in items:
        if it["outcome"] == "match":
            continue
        note = notes.get(it["id"])
        if note:
            misses.append({"id": it["id"], "brand": it["brand"], "approval_date": it["approval_date"], "section": it["section"],
                           "category": it["outcome"], **{k: note.get(k) for k in ("category", "explanation", "evidence")}})
            if note.get("category") != it["outcome"]:
                misses[-1]["note_category_differs"] = True
        else:
            missing_notes.append(it["id"])
    covered_rows = [r for it in items for r in it["covered_setids"]]
    other_rows = [r for it in items for r in it["other_setids"]]
    setid_counts = {"covered_entries": len(covered_rows), "covered_distinct": len({r["setid"] for r in covered_rows}),
                    "other_entries": len(other_rows), "other_distinct": len({r["setid"] for r in other_rows}),
                    "missing_from_repo": sum(1 for r in covered_rows + other_rows if "error" in r)}
    caveats = {
        "short_quote_items": [{"id": it["id"], "brand": it["brand"], "quote_tokens": it["quote_tokens"], "outcome": it["outcome"],
                               "kept_sentences": it["kept_sentences"]} for it in items if it["short_quote"]],
        "shown_in_label_without_srlc_brand": [
            {"id": it["id"], "brand": it["brand"], "outcome": it["outcome"], "setid": it["shown"]["setid"], "title": it["shown"].get("title")}
            for it in items if it["shown"] and it["shown"].get("srlc_brand_in_label") is False],
        "earlier_appearance": [{"id": it["id"], "brand": it["brand"], "shown": it["shown"]}
                               for it in items if it["shown"] and it["shown"].get("earlier_appearance")],
    }
    return {
        "title": SRLC_REPORT_TITLE,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "partial": partial,
        "sample": {"path": str(SAMPLE_PATH.relative_to(ROOT)), "items_sha256": sample["items_sha256"],
                   "generated": sample["generated"], "size": sample["sample_size"],
                   "parse_version": sample.get("generation", {}).get("parse_version"), "revisions": sample.get("revisions", [])},
        "setids": setid_counts,
        "caveats": caveats,
        "repo": {"path": str(index.repo.relative_to(ROOT)) if index.repo.is_relative_to(ROOT) else str(index.repo),
                 "head": index.head, "versions": len(index.versions), "labels": len(index.by_setid),
                 "record_from": index.record_from, "record_to": index.record_to, "build_date": index.build_date,
                 **({"note": build_note} if build_note else {})},
        "rules": sample["rules"], "window_rationale": sample["window_rationale"], "constants": sample["constants"],
        "section_groups": sample["section_groups"],
        "headline": {"matches": len(matches), "items": n, "match_rate": round(len(matches) / n, 4) if n else None,
                     "rule": "An item matches when some covered setid has a transition (quote present, previous version "
                             f"not) published within {WINDOW_DAYS} days either side of the SrLC approval date"},
        "by_category": _count(it["outcome"] for it in items),
        "by_year": _by(items, lambda it: it["year"]),
        "by_section": _by(items, lambda it: it["section"]),
        "lag_stats_matches": lag_stats([it["match_lag"] for it in matches]),
        "sensitivity": sensitivity,
        "items": items,
        "misses": misses,
        "misses_without_note": missing_notes,
    }


def _md_table(header: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _items(n: int) -> str:
    return f"{n} item" if n == 1 else f"{n} items"


def _times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


def _pct(rate) -> str:
    return "n/a" if rate is None else f"{100 * rate:.0f}%"


def render_markdown(report: dict) -> str:
    h, s, r = report["headline"], report["sample"], report["repo"]
    lines = [f"# {report['title']}", ""]
    sc = report.get("setids", {})
    corpus = (f"the US corpus build of {r['build_date']} ({r['note']})" if r.get("note")
              else f"the US corpus build of {r['build_date']} (`{r['path']}` at `{r['head'][:12]}`)")
    lines.append(f"Generated {report['generated']}. Sample: `{s['path']}` ({_items(s['size'])}, item list sha256 `{s['items_sha256']}`, "
                 f"fixed {s['generated']} before any matching" + (f", revised {_times(len(s['revisions']))} after results as described below" if s.get("revisions") else "") + f", parse version {s.get('parse_version')}). "
                 f"Run on {corpus}: {r['versions']:,} label versions of {r['labels']:,} setids, record {r['record_from']} to {r['record_to']}. "
                 f"Setids walked: {sc.get('covered_entries', 0)} covered setid entries ({sc.get('covered_distinct', 0)} distinct) and "
                 f"{sc.get('other_entries', 0)} uncovered setids of the same applications for diagnostics ({sc.get('other_distinct', 0)} distinct); "
                 f"{sc.get('missing_from_repo', 0)} missing from the repo. "
                 "The rules, constants and section groups are those recorded in the sample file; this report applies them without change.")
    for rev in s.get("revisions") or []:
        removed = ", ".join(f"`{c['removed']}` -> `{c['added']}` ({c['year']})" for c in rev.get("changes", []))
        lines.append("")
        lines.append(f"Sample revision (parse version {rev['parse_version']}, {rev['date']}). {rev['reason']} Items replaced by rerunning the "
                     f"unchanged selection rule on the corrected pool: {removed or 'none'}. Previous item list sha256 `{rev['previous_items_sha256']}`.")
    if report.get("partial"):
        lines.append("")
        lines.append("**Partial run**: only some sample items were checked; the counts below are not the headline result.")
    lines += ["", "## Headline", ""]
    lines.append(f"**{h['matches']} of {h['items']} sampled SrLC changes match ({_pct(h['match_rate'])}) under the pre-registered rule.** "
                 f"{h['rule']}. {report['window_rationale']}")
    lines += ["", "## Counts", "", "By category:", ""]
    lines += _md_table(["Category", "Items"], [[c, report["by_category"].get(c, 0)] for c in CATEGORIES])
    lines += ["", "By year:", ""]
    lines += _md_table(["Year", "Items", "Match", "Late", "Early", "Absent"],
                       [[y, v["items"], v["match"], v["late"], v["early"], v["absent"]] for y, v in report["by_year"].items()])
    lines += ["", "By SrLC section:", ""]
    lines += _md_table(["Section", "Items", "Match", "Late", "Early", "Absent"],
                       [[sec, v["items"], v["match"], v["late"], v["early"], v["absent"]] for sec, v in report["by_section"].items()])
    st = report["lag_stats_matches"]
    lines += ["", "## Lag of the matches", ""]
    if st["n"]:
        lines.append(f"Lag = published date of the repo version that first carries the quote minus the SrLC approval date, in days, "
                     f"over the {st['n']} matches (the closest matching setid per item). Min {st['min']}, first quartile {st['q1']}, "
                     f"median {st['median']}, third quartile {st['q3']}, max {st['max']}, mean {st['mean']}. "
                     "A negative lag means DailyMed carried the text before the SrLC approval date. The transition reported is the one "
                     "nearest the approval date (rule 7); when the setid carried the quote in an earlier version, lost it and regained it, "
                     "the earlier appearance is listed below and in the JSON as first_present_version.")
        earlier = report.get("caveats", {}).get("earlier_appearance", [])
        if earlier:
            lines.append("")
            lines.append("Matches whose setid carried the quote before the chosen transition: " + "; ".join(
                f"`{e['id']}` ({e['brand']}) first present in version {e['shown']['earlier_appearance']['version']} "
                f"({e['shown']['earlier_appearance']['published_date']}, lag {e['shown']['earlier_appearance']['lag']:+d}), "
                f"chosen transition version {e['shown']['version']} (lag {e['shown']['lag']:+d})" for e in earlier) + ".")
    else:
        lines.append("No matches, so no lag statistics.")
    lines += ["", "## Sensitivity", "",
              "Reported, never the headline. Covered setids stay as pinned in the sample; only the presence rule or the window changes.", ""]
    rows = []
    for name, v in report["sensitivity"].items():
        bc = v["by_category"]
        rows.append([name, v["presence"], f"+-{v['window_days']}", v["matches"], _pct(v["match_rate"]),
                     bc.get("late", 0), bc.get("early", 0), bc.get("absent", 0)])
    lines += _md_table(["Variant", "Presence", "Window (days)", "Matches", "Rate", "Late", "Early", "Absent"], rows)
    lines += ["", "## Items", "",
              "Version (setid): the DailyMed version number and the first 8 characters of its setid; for a match, the version "
              "that first carries the quote, with its Published-Date; for a late or early item, the nearest version that carries it "
              "(marked `first` when the quote is already in the setid's first version); blank for absent. Lag in days from the SrLC "
              "approval date.", ""]
    rows = []
    for i, it in enumerate(report["items"], start=1):
        sh = it["shown"]
        ver = f"{sh['version']} ({sh['setid'][:8]}){' first' if sh.get('first_version') else ''}" if sh else ""
        rows.append([i, f"`{it['id']}`", it["brand"], it["approval_date"], it["section"], it["outcome"], ver,
                     sh["published_date"] if sh else "", sh["lag"] if sh else ""])
    lines += _md_table(["#", "Item", "Drug", "SrLC date", "Section", "Outcome", "Version (setid)", "Published", "Lag"], rows)
    cav = report.get("caveats", {})
    lines += ["", "## Caveats on individual items", ""]
    short = cav.get("short_quote_items", [])
    lines.append(f"- Short quotes ({_items(len(short))} with at most {SHORT_QUOTE_TOKENS} kept tokens; valid under rule 2 but weak evidence, "
                 "since a cross-reference or a lead-in sentence can match any edit of the section): " +
                 ("; ".join(f"`{e['id']}` ({e['brand']}, {e['quote_tokens']} tokens, {e['outcome']}): " +
                            " / ".join(f"'{k}'" for k in e["kept_sentences"]) for e in short) if short else "none") + ".")
    sib = cav.get("shown_in_label_without_srlc_brand", [])
    lines.append(f"- Labels without the SrLC brand ({_items(len(sib))}): rule 6 maps an item to every setid under its application number, "
                 "so the version shown can belong to an authorized generic or a sibling product of the same sponsor. " +
                 ("; ".join(f"`{e['id']}` (SrLC brand {e['brand']}) shown in `{e['setid'][:8]}` '{e['title']}'" for e in sib) if sib else "none") + ".")
    lines += ["", "## Misses explained", ""]
    if not report["misses"] and not report["misses_without_note"]:
        lines.append("Every item matched.")
    for m in report["misses"]:
        ev = f" Evidence: {m['evidence']}" if m.get("evidence") else ""
        lines.append(f"- `{m['id']}` ({m['brand']}, {m['approval_date']}, {m['section']}), {m['category']}: {m['explanation']}{ev}")
    for mid in report["misses_without_note"]:
        lines.append(f"- `{mid}`: no note in sources/srlc-miss-notes.json yet.")
    lines += ["", "## Method notes", ""]
    groups = "; ".join(f"{k} {{{', '.join(v)}}}" for k, v in report["section_groups"].items())
    lines.append(f"- Section groups (rule 4), by LOINC code of the section file: {groups}. PCI, PI and MG items were counted but not sampled.")
    lines.append("- Presence (rule 5): a kept quote sentence is found when its normalised form is a substring of the normalised section-group "
                 "text, or when one line or a pair of consecutive lines covers at least 0.90 of its tokens (difflib matching blocks); the "
                 "quote is present when found sentences hold at least 0.80 of the kept tokens. Section files hold one sentence per line. "
                 "Lines are the non-empty lines of the section-group files, so a pair of consecutive lines may span a blank line between two "
                 "paragraphs, and the last line of one file pairs with the first line of the next file of the group. Cited line text is verbatim, "
                 "indentation included.")
    lines.append("- Quote (rule 2) as parsed from the SrLC detail pages: every content div of a section is read (one per subsection); headings are "
                 f"paragraphs in a heading tag or all-bold paragraphs that are numbered, have at most {MAX_HEADING_TOKENS} tokens, or carry no "
                 "sentence punctuation (boxed-warning titles, table captions); sentences do not end after " +
                 ", ".join(f"'{a}'" for a in ABBREVIATIONS) + ". After a 'Newly added' or 'Addition of the following' note every paragraph up to "
                 "the next FDA note is taken as the addition, which can include context the site did not mark; SrLC does not delimit the scope "
                 "of such a note, and the alternative reading (stop at the first underlined paragraph) would drop the body of a new subsection "
                 "wherever its heading is underlined, so the wider reading is kept and recorded here.")
    lines.append("- Transition (rule 7): a version where the quote is present and the previous version's is not; the transition with the "
                 "smallest |lag| is chosen, the earlier one on a tie. Categories are tested in the order match, early, late, absent.")
    lines.append("- Dates are Published-Date trailers of the US corpus (DailyMed publication), never commit dates and never label text.")
    lines.append("- The full evidence (every version walked, every transition with the matched lines, scores and diagnostics) is in "
                 "reports/srlc-validation.json; the explanations come from sources/srlc-miss-notes.json.")
    return "\n".join(lines) + "\n"


def run_check(sample: dict, index: labels.Index, notes: dict, only: set[str] | None = None, log=print,
              build_note: str | None = None) -> dict:
    reader = LabelReader(index)
    out = []
    for n, item in enumerate(sample["items"], start=1):
        if only and item["id"] not in only:
            continue
        covered = [walk_setid(reader, e, item["section"], item["kept_sentences"], item["approval_date"]) for e in item["covered_setids"]]
        others = [walk_setid(reader, {**e, "covered": False}, item["section"], item["kept_sentences"], item["approval_date"],
                             diagnose=False) for e in item["uncovered_setids"]]
        summary = summarise_item(item, covered, others)
        out.append(summary)
        sh = summary["shown"]
        where = f"v{sh['version']} {sh['published_date']} lag {sh['lag']:+d}" if sh else "-"
        log(f"[{n:2d}/{len(sample['items'])}] {item['id']}: {summary['outcome']} ({where}) "
            f"setids {', '.join(f'{k[:8]}={v}' for k, v in summary['categories'].items())}")
    return validation_report(sample, out, notes, index, partial=bool(only), build_note=build_note)


def load_notes(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def cmd_check(args: argparse.Namespace) -> int:
    sample_path = Path(args.sample)
    if not sample_path.exists():
        print(f"ERROR: {sample_path} missing; run: python3 -I tools/srlc.py select", file=sys.stderr)
        return 2
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    try:
        verify_sample(sample)
        index = labels.Index(args.repo, MOLECULES)
    except (SrlcError, labels.LabelError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    report = run_check(sample, index, load_notes(Path(args.notes)), only, build_note=args.build_note)
    write_json(Path(args.out_json), report)
    Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_md).write_text(render_markdown(report), encoding="utf-8")
    h = report["headline"]
    print(f"match rate {h['matches']}/{h['items']} ({_pct(h['match_rate'])}); by category {report['by_category']}; "
          f"sensitivity {{{', '.join(f'{k}: {v['matches']}' for k, v in report['sensitivity'].items())}}}")
    if report["misses_without_note"]:
        print(f"{len(report['misses_without_note'])} misses without a note in {args.notes}: {', '.join(report['misses_without_note'])}")
    print(f"written {args.out_json} and {args.out_md}")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="cache monthly searches and in-corpus detail pages under raw/srlc/")
    f.add_argument("--months-from", default=SEARCH_MONTHS[0])
    f.add_argument("--months-to", default=SEARCH_MONTHS[1])
    f.add_argument("--no-details", action="store_true")
    f.add_argument("--max-details", type=int, default=None)
    f.add_argument("--raw-dir", default=str(RAW_SRLC))
    f.add_argument("--molecules", default=str(MOLECULES))
    f.set_defaults(func=cmd_fetch)
    p = sub.add_parser("parse", help="cached pages -> raw/srlc/items.json (one item per SrLC section change)")
    p.add_argument("--raw-dir", default=str(RAW_SRLC))
    p.add_argument("--molecules", default=str(MOLECULES))
    p.add_argument("--out", default=str(ITEMS_PATH))
    p.set_defaults(func=cmd_parse)
    s = sub.add_parser("select", help="items -> reports/srlc-population.json and sources/srlc-sample.json")
    s.add_argument("--items", default=str(ITEMS_PATH))
    s.add_argument("--molecules", default=str(MOLECULES))
    s.add_argument("--sources", default=str(SOURCES_US))
    s.add_argument("--population-out", default=str(POPULATION_PATH))
    s.add_argument("--sample-out", default=str(SAMPLE_PATH))
    s.set_defaults(func=cmd_select)
    c = sub.add_parser("check", help="pinned sample x build/us -> reports/srlc-validation.json and .md (no network)")
    c.add_argument("--sample", default=str(SAMPLE_PATH))
    c.add_argument("--repo", default=str(labels.DEFAULT_REPO))
    c.add_argument("--notes", default=str(MISS_NOTES_PATH))
    c.add_argument("--only", default=None, help="comma-separated item ids for a partial run")
    c.add_argument("--out-json", default=str(VALIDATION_JSON))
    c.add_argument("--out-md", default=str(VALIDATION_MD))
    c.add_argument("--build-note", default=None,
                   help="a note on the US build checked, shown instead of its path and HEAD (e.g. that it is a pre-release "
                        "build whose hashes are not in the published history)")
    c.set_defaults(func=cmd_check)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
