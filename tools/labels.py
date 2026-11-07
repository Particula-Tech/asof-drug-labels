"""Read-only library over the built label-history repos (US: DailyMed, EU: Union Register).

Shared by cli.py and mcp_server.py. Everything here is standard library plus
git subprocesses; nothing writes to a repo and nothing touches the network.

US: "on date X" means the newest version whose Published-Date trailer is on or
before X: what DailyMed showed that day, which is not necessarily the text FDA
approved. EU: the newest Annex I whose Effective-Date (the Commission decision
date) is on or before X. Dates always come from commit trailers, never from
commit dates.
"""
from __future__ import annotations

import bisect
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILD_US_REPO = ROOT / "build" / "us"
BUILD_EU_REPO = ROOT / "build" / "eu"
SUBMODULE_US_REPO = ROOT / "us"       # the public layout: corpus repos as submodules of the top repo
SUBMODULE_EU_REPO = ROOT / "eu"
DEFAULT_MOLECULES = ROOT / "molecules"
MANIFEST = ROOT / "manifest.json"
BUILD_MARKER = "asof-build"
EU_BUILD_MARKER = "asof-build-eu"
US_REPO_ENV = "ASOF_US_REPO"
EU_REPO_ENV = "ASOF_EU_REPO"


def default_us_repo() -> Path:
    """The US repo when none is named: us/ when it holds a git repo (a .git directory or a submodule's
    gitlink file), else build/us."""
    return SUBMODULE_US_REPO if (SUBMODULE_US_REPO / ".git").exists() else BUILD_US_REPO


def not_built_message(repo: Path, build_hint: str) -> str:
    """The error for a repo path without .git. An existing directory is most often a submodule that was not
    checked out (a clone without --recurse-submodules), so that case names the submodule command first."""
    name = Path(repo).name
    if Path(repo).is_dir():
        return f"repo {name!r} is empty; run: {SUBMODULE_HINT} (or build it with {build_hint})"
    return f"repo {name!r} is not a built repo (no .git); run: {build_hint}"


def resolve_us_repo(arg: str | Path | None = None) -> Path:
    """The US repo to load: an explicit --repo, else env ASOF_US_REPO, else us/ (a git repo), else build/us."""
    if arg is not None and str(arg):
        return Path(arg)
    env = os.environ.get(US_REPO_ENV)
    return Path(env) if env else default_us_repo()


DEFAULT_REPO = default_us_repo()
DEFAULT_EU_REPO = SUBMODULE_EU_REPO if (SUBMODULE_EU_REPO / ".git").exists() else BUILD_EU_REPO
# -M credits lines moved within a section, -C lines moved from another section changed in the
# same version, to the version that first wrote them rather than the one that moved them.
BLAME_OPTS = ("-M", "-C")
NOTICE = ("Courtesy of the U.S. National Library of Medicine (DailyMed); NLM does not endorse this project. Record ends "
          "{record_to} (built {build_date}); later versions may exist on DailyMed. Converted text, provided as is; "
          "where it differs from the DailyMed document, the official text prevails. Not medical advice; not for "
          f"prescribing or treatment decisions. Corrections and rights holders: {notices.CONTACT}.")
EU_NOTICE = ("Source: European Commission, Union Register of medicinal products, and the European Medicines Agency "
             "(EMA); neither endorses this project. Record ends {record_to} (built {build_date}); check the EMA product "
             "information for the latest text. Converted text, provided as is; where it differs from the Annex I PDF "
             "in the Union Register, the official text prevails. Not medical advice; not for prescribing or treatment "
             f"decisions. Corrections and rights holders: {notices.CONTACT}.")
SUBMODULE_HINT = "git submodule update --init --recursive"
REMOVED_NOTE = "sources/removed.json"
EU_REASON = "EU repo not loaded; build it with tools/eu.py build or pass --eu-repo (env ASOF_EU_REPO)"
EU_SOURCE = "European Commission Union Register (Annex I of Commission decisions) and EMA product information"
EU_SOURCE_PREFIXES = ("ec-union-register:", "ema-product-information:")
HISTORY_TYPES = ("in-force", "current-only", "replayed")
READ_CAP_CHARS = 60_000
DEFAULT_LIMIT = 500
DEFAULT_LIMIT_ALL = 100       # rows for changes_between without a query, whose rows are large
WORD_MATCH_CUTOFF = 0.6
PICKAXE_CHUNKS = 8
PICKAXE_PARALLEL_MIN = 2_000  # commits; below this a single git process is faster than fan-out
RANGE_MIN = 300               # consecutive commits; below this an explicit commit list beats a range walk
ARGV_COMMITS = 1_000          # explicit commits per git call
TEXT_CHUNK_COMMITS = 100      # commits whose before/after section texts are held in memory at once
ONLY_IN_MAX_LINES = 200       # verbatim lines listed per side in compare_jurisdictions with a section
SETID_CHARS = re.compile(r"^[0-9a-f-]+$")
LOINC = re.compile(r"^\d{4,5}-\d$")
QRD_NUMBER = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")
EMEA_ID = re.compile(r"^EMEA[/-]H[/-]C[/-](\d{1,6})$", re.I)
EU_NUMBER = re.compile(r"^EU/(\d+)/(\d+)/(\d+)$", re.I)

WORD_BLAME_METHOD = (
    "Each line is credited by git blame -M -C to the version that first wrote it, following moved text. "
    "For words, that line is compared with the closest line (difflib ratio >= 0.6) in the previous version's "
    "text of the same section; words both lines share inherit that older line's word credits, applied "
    "recursively, and the remaining words are credited to the line's version. A word is therefore credited "
    "to the version that first wrote it in that sentence, and is never credited to a version later than its line.")

# The fixed QRD section table of tools/eu_qrd.py (SECTIONS), copied so this module never imports the
# converter: number -> (file stem, title). The two smpc-0 files have no number of their own.
EU_SECTIONS: dict[str, tuple[str, str]] = {
    "1": ("smpc-1-name-of-the-medicinal-product", "Name of the medicinal product"),
    "2": ("smpc-2-qualitative-and-quantitative-composition", "Qualitative and quantitative composition"),
    "3": ("smpc-3-pharmaceutical-form", "Pharmaceutical form"),
    "4": ("smpc-4-clinical-particulars", "Clinical particulars"),
    "4.1": ("smpc-4.1-therapeutic-indications", "Therapeutic indications"),
    "4.2": ("smpc-4.2-posology-and-method-of-administration", "Posology and method of administration"),
    "4.3": ("smpc-4.3-contraindications", "Contraindications"),
    "4.4": ("smpc-4.4-special-warnings-and-precautions-for-use", "Special warnings and precautions for use"),
    "4.5": ("smpc-4.5-interaction-with-other-medicinal-products-and-other-forms-of-interaction",
            "Interaction with other medicinal products and other forms of interaction"),
    "4.6": ("smpc-4.6-fertility-pregnancy-and-lactation", "Fertility, pregnancy and lactation"),
    "4.7": ("smpc-4.7-effects-on-ability-to-drive-and-use-machines", "Effects on ability to drive and use machines"),
    "4.8": ("smpc-4.8-undesirable-effects", "Undesirable effects"),
    "4.9": ("smpc-4.9-overdose", "Overdose"),
    "5": ("smpc-5-pharmacological-properties", "Pharmacological properties"),
    "5.1": ("smpc-5.1-pharmacodynamic-properties", "Pharmacodynamic properties"),
    "5.2": ("smpc-5.2-pharmacokinetic-properties", "Pharmacokinetic properties"),
    "5.3": ("smpc-5.3-preclinical-safety-data", "Preclinical safety data"),
    "6": ("smpc-6-pharmaceutical-particulars", "Pharmaceutical particulars"),
    "6.1": ("smpc-6.1-list-of-excipients", "List of excipients"),
    "6.2": ("smpc-6.2-incompatibilities", "Incompatibilities"),
    "6.3": ("smpc-6.3-shelf-life", "Shelf life"),
    "6.4": ("smpc-6.4-special-precautions-for-storage", "Special precautions for storage"),
    "6.5": ("smpc-6.5-nature-and-contents-of-container", "Nature and contents of container"),
    "6.6": ("smpc-6.6-special-precautions-for-disposal-and-other-handling",
            "Special precautions for disposal and other handling"),
    "7": ("smpc-7-marketing-authorisation-holder", "Marketing authorisation holder"),
    "8": ("smpc-8-marketing-authorisation-numbers", "Marketing authorisation numbers"),
    "9": ("smpc-9-date-of-first-authorisation-renewal-of-the-authorisation",
          "Date of first authorisation/renewal of the authorisation"),
    "10": ("smpc-10-date-of-revision-of-the-text", "Date of revision of the text"),
    "11": ("smpc-11-dosimetry", "Dosimetry"),
    "12": ("smpc-12-instructions-for-preparation-of-radiopharmaceuticals",
           "Instructions for preparation of radiopharmaceuticals"),
}
# smpc-0 files: stem -> (code, title). Codes stay distinct so one version never has two files per code.
EU_ZERO_SECTIONS = {"smpc-0-additional-monitoring": ("0", "Additional monitoring"),
                    "smpc-0-front-matter": ("0-front-matter", "Front matter")}


@dataclass(frozen=True)
class Pair:
    """One topical pair of compare_jurisdictions: an SmPC section and its US LOINC counterpart."""
    name: str
    eu: str
    us: str
    words: tuple[str, ...]
    us_fallbacks: tuple[tuple[str, ...], ...] = ()   # for old-format US labels, tried in order


PAIRS: tuple[Pair, ...] = (
    Pair("indications", "4.1", "34067-9", ("indications", "therapeutic-indications", "indications-and-usage")),
    Pair("dosage", "4.2", "34068-7", ("dosage", "posology", "dosage-and-administration", "dose")),
    Pair("contraindications", "4.3", "34070-3", ("contraindications", "contraindication")),
    Pair("warnings", "4.4", "43685-7", ("warnings", "warnings-and-precautions", "special-warnings", "precautions")),
    Pair("interactions", "4.5", "34073-7", ("interactions", "drug-interactions", "interaction")),
    Pair("pregnancy-and-lactation", "4.6", "43684-0", ("pregnancy-and-lactation", "pregnancy", "lactation",
                                                        "fertility", "use-in-specific-populations"),
         (("42228-7", "34080-2"), ("77290-5",))),
    Pair("adverse-reactions", "4.8", "34084-4", ("adverse-reactions", "undesirable-effects", "adverse",
                                                  "side-effects")),
    Pair("overdosage", "4.9", "34088-5", ("overdosage", "overdose")),
)
US_ONLY_NOTES = {"34066-1": "boxed warning; no SmPC counterpart"}


class LabelError(RuntimeError):
    """A user-facing problem with the request or the repo."""


@dataclass(frozen=True)
class Version:
    commit: str
    setid: str
    version: int
    published: str
    effective: str
    label_format: str
    source_url: str = ""
    sha256: str = ""
    application_numbers: tuple[str, ...] = ()
    title: str = ""
    format_conversion: str = ""
    jurisdiction: str = "us"
    name: str = ""
    eu_number: str = ""
    decision_number: str = ""
    decision_date: str = ""
    procedure_id: str = ""
    ema_procedure: str = ""
    procedure_type: str = ""
    procedure_closed: str = ""
    corrigendum_of: str = ""
    annex_identical_to: str = ""
    history: str = "in-force"
    captured_date: str = ""
    converter: str = ""


# ---------------------------------------------------------------- helpers

def check_date(value: str, name: str = "date") -> str:
    """Validate an ISO date and return it unchanged."""
    if not isinstance(value, str):
        raise LabelError(f"{name} must be an ISO date string (YYYY-MM-DD), got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as err:
        raise LabelError(f"{name} {value!r} is not an ISO date (YYYY-MM-DD): {err}") from err
    return value


def normalise(text: str) -> str:
    """Case-insensitive, whitespace-normalised form used for every phrase match."""
    return " ".join(text.split()).lower()


def ere_pattern(text: str) -> str:
    """A POSIX ERE that matches ``text`` with any run of whitespace between its words."""
    def escape(word: str) -> str:
        out = []
        for ch in word:
            if ch.isalnum() or ch == "_":
                out.append(ch)
            elif ch in "\\[]^":
                out.append("\\" + ch)
            else:
                out.append(f"[{ch}]")
        return "".join(out)
    words = text.split()
    if not words:
        raise LabelError("the search text is empty")
    return "[[:space:]]+".join(escape(w) for w in words)


def ndc_digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def section_code(path: str) -> str:
    """'inn/setid/43685-7-warnings-and-precautions.md' -> '43685-7'."""
    name = Path(path).name
    m = re.match(r"(\d{4,5}-\d)", name)
    return m.group(1) if m else name.removesuffix(".md")


def section_title(path: str) -> str:
    name = Path(path).name.removesuffix(".md")
    slug = re.sub(r"^\d{4,5}-\d-?", "", name)
    return slug.replace("-", " ").strip().capitalize() or name


def section_entry(path: str) -> dict:
    return {"code": section_code(path), "title": section_title(path), "file": Path(path).name}


def pick_section_group(files: list[str], section: str) -> list[str]:
    """The files in ``files`` of the one section ``section`` names: a LOINC code (prefix of
    the file name), the file name itself, or a word from the slug such as 'warnings' or
    'boxed'. Several files share one LOINC code only when the slug changed across a label's
    history (``files`` spanning versions), so a match is unique when it names one code."""
    if not isinstance(section, str) or not section.strip():
        raise LabelError("section must be a LOINC code (e.g. 43685-7) or a word from the section name")
    want = section.strip().lower().replace(" ", "-").removesuffix(".md")
    by_code = [f for f in files if Path(f).name.lower().startswith(want)]
    matches = by_code or [f for f in files if want in Path(f).name.lower()]
    if len({section_code(f) for f in matches}) == 1:
        return sorted(matches)
    names = ", ".join(sorted(Path(f).name.removesuffix(".md") for f in files)) or "none"
    problem = "is ambiguous" if matches else "is not in this version"
    raise LabelError(f"section {section!r} {problem}; available: {names}")


def pick_section(files: list[str], section: str) -> str:
    """The one file of a single version that ``section`` names (see pick_section_group)."""
    group = pick_section_group(files, section)
    if len(group) > 1:
        names = ", ".join(Path(f).name for f in group)
        raise LabelError(f"section {section!r} has several file names across versions ({names}); "
                         "resolve it per version or use pick_section_group")
    return group[0]


def by_code(files: list[str]) -> dict[str, str]:
    """LOINC code -> path for the section files of one version."""
    return {section_code(f): f for f in files}


# ---- EU sections and ids

def _stem(path: str) -> str:
    return Path(path).name.removesuffix(".md")


def eu_section_code(path: str) -> str:
    """'inn/EMEA-H-C-003820/smpc-4.3-contraindications.md' -> '4.3'."""
    stem = _stem(path)
    if stem in EU_ZERO_SECTIONS:
        return EU_ZERO_SECTIONS[stem][0]
    m = re.match(r"smpc-(\d{1,2}(?:\.\d{1,2})?)-", stem)
    return m.group(1) if m else stem


def eu_section_title(path: str) -> str:
    stem = _stem(path)
    if stem in EU_ZERO_SECTIONS:
        return EU_ZERO_SECTIONS[stem][1]
    entry = EU_SECTIONS.get(eu_section_code(path))
    if entry and entry[0] == stem:
        return entry[1]
    slug = re.sub(r"^smpc-[\d.]+-?", "", stem)
    return slug.replace("-", " ").strip().capitalize() or stem


def eu_section_entry(path: str) -> dict:
    return {"code": eu_section_code(path), "title": eu_section_title(path), "file": Path(path).name}


def _eu_available(files: list[str]) -> str:
    return ", ".join(f"{eu_section_code(f)} ({_stem(f)})" for f in sorted(files, key=_eu_order)) or "none"


def _eu_order(path: str) -> tuple:
    code = eu_section_code(path)
    parts = re.findall(r"\d+", code)
    return tuple(int(p) for p in parts) + ((1,) if code == "0-front-matter" else ())


def pick_eu_section(files: list[str], key: str) -> str:
    """The one SmPC file of a version that ``key`` names: a QRD number ('4.3', equal to the stem's
    number exactly, so '4' names only smpc-4-clinical-particulars), 'smpc-4.3', the full stem, a pair word of
    PAIRS ('indications' -> 4.1, 'interactions' -> 4.5, 'overdosage' -> 4.9), or whole consecutive words of the
    slug ('special warnings', 'excipients'; never a substring of a word). A US LOINC code is an error."""
    if not isinstance(key, str) or not key.strip():
        raise LabelError("section must be a QRD number (e.g. 4.3), an smpc- file stem or a word from the section name")
    want = key.strip().lower().replace(" ", "-").removesuffix(".md")
    if LOINC.match(want):
        raise LabelError(f"section {key!r} is a US LOINC code; EU labels use QRD section numbers; "
                         f"available: {_eu_available(files)}")
    number = re.fullmatch(r"(?:smpc-)?(\d{1,2}(?:\.\d{1,2})?)", want)
    paired = next((p for p in PAIRS if want == p.name or want in p.words), None)
    if number:
        matches = [f for f in files if eu_section_code(f) == number.group(1)]
    elif want.startswith("smpc-"):
        matches = [f for f in files if _stem(f) == want] or [f for f in files if _stem(f).startswith(want)]
    elif paired is not None:
        # A pair word of compare_jurisdictions ('indications', 'dosage', 'interactions', 'adverse', ...) names the
        # paired QRD section, so every tool accepts the same words.
        matches = [f for f in files if eu_section_code(f) == paired.eu]
    else:
        matches = [f for f in files if _slug_run(want.split("-"), re.sub(r"^smpc-[\d.]+-?", "", _stem(f)).split("-"))]
    if len(matches) == 1:
        return matches[0]
    problem = "is ambiguous" if matches else "is not in this version"
    raise LabelError(f"section {key!r} {problem}; available: {_eu_available(files)}")


def _slug_run(want: list[str], words: list[str]) -> bool:
    """True when ``want`` is the whole slug or a consecutive run of its hyphen-separated words."""
    n = len(want)
    return n > 0 and all(want) and any(words[i:i + n] == want for i in range(len(words) - n + 1))


def eu_section_paths(code: str | None, prefix: str = "*") -> list[str]:
    """Pathspecs selecting one SmPC section's files (by QRD number) across products, or all sections."""
    if not code:
        return [f"{prefix}/*.md"]
    want = code.strip().lower().removeprefix("smpc-")
    if not QRD_NUMBER.match(want):
        raise LabelError(f"a corpus-wide EU section filter must be a QRD number such as 4.4, not {code!r}")
    return [f"{prefix}/smpc-{want}-*.md"]


def _strip_prefix(label_id: str) -> str:
    key = label_id.strip()
    low = key.lower()
    for prefix in ("eu:", "dailymed:"):
        if low.startswith(prefix):
            return key[len(prefix):].strip()
    return key


def is_eu_id(label_id) -> bool:
    """True when ``label_id`` names an EU product (EMEA... or EU/... after an optional eu:/dailymed: prefix)."""
    if not isinstance(label_id, str):
        return False
    key = _strip_prefix(label_id).upper()
    return key.startswith("EMEA") or key.startswith("EU/")


def eu_number_key(value: str) -> tuple[int, int, int] | None:
    m = EU_NUMBER.match(value.strip()) if isinstance(value, str) else None
    return tuple(int(g) for g in m.groups()) if m else None


def canonical_eu_id(label_id: str, eu_numbers: dict[tuple, str] | None = None) -> str | None:
    """The canonical EU label id (the repo folder name, EMEA-H-C-nnnnnn) for any accepted form:
    EMEA/H/C/003820, EMEA-H-C-003820, EMEA-H-C-3820, each with an optional 'eu:' prefix, case-insensitive;
    an EU number (EU/1/15/1024) is looked up in ``eu_numbers`` (eu_number_key -> label id). None otherwise."""
    if not isinstance(label_id, str) or not label_id.strip():
        return None
    key = _strip_prefix(label_id)
    m = EMEA_ID.match(key)
    if m:
        return f"EMEA-H-C-{int(m.group(1)):06d}"
    number = eu_number_key(key)
    if number and eu_numbers is not None:
        return eu_numbers.get(number)
    return None


# ---- compare normalisation

_SPACES = re.compile(r"[   -   　\t\f\v]")
_PUNCT = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                        "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
                        "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", "‒": "-"})
_LIST_MARKER = re.compile(r"^(?:[-*]\s+|\d+\.\s+)")
_TABLE_RULE = re.compile(r"^\|?[\s:|-]+\|?$")


def compare_key(line: str) -> str:
    """The form two label lines are compared in: NFC, lowercase, Unicode spaces to a space, curly quotes
    to ASCII, en/em dashes and minus to '-', a leading list marker dropped, white space collapsed and
    trailing '.', ';', ':' and ',' dropped. No symbol is spelled out ('≤' stays '≤')."""
    s = unicodedata.normalize("NFC", line).lower()
    s = _SPACES.sub(" ", s).translate(_PUNCT).strip()
    s = _LIST_MARKER.sub("", s)
    s = " ".join(s.split())
    return re.sub(r"[\s.;:,]+$", "", s)


def compare_lines(text: str, eu_markers: bool = False) -> list[tuple[str, str]]:
    """(verbatim line, compare_key) for the comparable lines of a section text: heading lines, blank
    lines, Markdown table rules and (EU) the multi-SmPC marker lines are left out; table rows count."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _TABLE_RULE.match(stripped):
            continue
        if eu_markers and (stripped == "(all sentences already above)" or re.fullmatch(r"\[[^\]]+\]", stripped)):
            continue
        key = compare_key(line)
        if key:
            out.append((line, key))
    return out


def _parse_cat_file(out: bytes, specs: list[str]) -> dict[str, str | None]:
    """Parse ``git cat-file --batch`` output for ``specs`` (None for missing objects)."""
    texts: dict[str, str | None] = {}
    pos = 0
    for spec in specs:
        nl = out.index(b"\n", pos)
        head = out[pos:nl].decode()
        pos = nl + 1
        if head.endswith(" missing") or head.endswith(" ambiguous"):
            texts[spec] = None
            continue
        size = int(head.rsplit(" ", 1)[1])
        texts[spec] = out[pos:pos + size].decode("utf-8")
        pos += size + 1
    return texts


# ---------------------------------------------------------------- index

class Index:
    """Everything the tools need about one built repo, read once from git.

    The version list comes from a single ``git log``; the label -> folder map from
    a single ``ls-tree``. Section listings, texts and blames are cached per call."""

    BUILD_MARKER = BUILD_MARKER
    META = "label.json"
    jurisdiction = "us"
    date_key = "published_date"
    BUILD_HINT = "python3 tools/build.py build"
    NO_VERSIONS = "no dailymed Source-Id trailers"

    def __init__(self, repo: Path | str, molecules_dir: Path | str | None = None):
        self.repo = Path(repo)
        # Messages name the repo by its last path component only: tool errors reach MCP clients.
        if not (self.repo / ".git").exists():
            raise LabelError(not_built_message(self.repo, self.BUILD_HINT))
        self.molecules_dir = Path(molecules_dir) if molecules_dir else DEFAULT_MOLECULES
        self.versions = self._load_versions()
        if not self.versions:
            raise LabelError(f"repo {self.repo.name!r} holds no label versions ({self.NO_VERSIONS})")
        self.by_commit = {v.commit: v for v in self.versions}
        self.position = {v.commit: i for i, v in enumerate(self.versions)}
        # Commits are in build order: a batch appended by the daily job comes after everything before it,
        # even when DailyMed lists a version with an older Published-Date (EU: a corrigendum keeps the date
        # of the decision it corrects). ``published`` is therefore the running maximum of the dates in commit
        # order, so commit_as_of stays a bisect; a label with a version dated below that maximum at its
        # commit is out of order, and search reads it from its own as-of commit.
        self.published, self.out_of_order_setids, top = [], set(), ""
        for v in self.versions:
            if v.published < top:
                self.out_of_order_setids.add(v.setid)
            top = max(top, v.published)
            self.published.append(top)
        self.dated = sorted(self.versions, key=lambda v: (v.published, self.position[v.commit]))
        self.dated_published = [v.published for v in self.dated]
        self.by_setid: dict[str, list[Version]] = {}
        for v in self.dated:   # each label's versions in date order (same-day ties: commit order)
            self.by_setid.setdefault(v.setid, []).append(v)
        self.head = self.versions[-1].commit
        self.record_from = self.dated[0].published
        self.record_to = self.published[-1]
        self.build_date = self._build_date()
        self.folders = self._load_folders()
        self._lock = threading.Lock()  # guards the lazy loaders when tools fan out over threads
        self._metadata: dict[str, dict] | None = None
        self._molecules: dict[str, dict] | None = None
        self._setid_molecules: dict[str, list[tuple[str, dict]]] = {}
        self._files: dict[tuple[str, str], list[str]] = {}
        self._blame: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
        self._word_credit: dict[tuple[str, str, str], list[tuple[str, Version]]] = {}

    # ---- section naming (US: LOINC codes; EuIndex overrides with QRD numbers)

    def entry(self, path: str) -> dict:
        return section_entry(path)

    def code(self, path: str) -> str:
        return section_code(path)

    def pick(self, files: list[str], section: str) -> str:
        return pick_section(files, section)

    def pick_group(self, files: list[str], section: str) -> list[str]:
        return pick_section_group(files, section)

    def section_paths(self, code: str | None) -> list[str]:
        return section_paths(code)

    def after_record_end(self) -> str:
        return (f"after the record end ({self.record_to}, built {self.build_date}); "
                "later versions may exist on DailyMed")

    # ---- git plumbing

    def git(self, *args: str, stdin: str | None = None, ok_codes: tuple[int, ...] = (0,)) -> str:
        proc = subprocess.run(["git", "-C", str(self.repo), *args], input=stdin, capture_output=True, text=True)
        if proc.returncode not in ok_codes:
            raise LabelError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _load_versions(self) -> list[Version]:
        raw = self.git("log", "--reverse", "--format=%H%x00%B%x1e")
        out = []
        for record in raw.split("\x1e"):
            if not record.strip():
                continue
            commit, _, body = record.strip("\n").partition("\x00")
            t = _trailers(body)
            if not t.get("Source-Id", "").startswith("dailymed:"):
                continue
            subject = body.strip().splitlines()[0]
            out.append(Version(commit, t["Source-Id"].split(":", 1)[1].lower(), int(t["Source-Version"]),
                               t["Published-Date"], t.get("Effective-Time", ""), t.get("Label-Format", ""),
                               t.get("Source-URL", ""), t.get("Source-SHA256", ""),
                               tuple(a for a in t.get("Application-Numbers", "").split(", ") if a and a != "none stated"),
                               subject.split(" — label version")[0].strip(), t.get("Format-Conversion", "")))
        return out

    def manifest_entry(self) -> dict | None:
        """This corpus's entry in manifest.json when its head is this repo's HEAD, else None."""
        try:
            corpus = (json.loads(MANIFEST.read_text(encoding="utf-8")).get("corpora") or {}).get(self.jurisdiction)
        except (OSError, ValueError):
            return None
        return corpus if isinstance(corpus, dict) and corpus.get("head") == self.head else None

    def tags(self) -> dict:
        """The tags on HEAD and the newest daily/<date> tag reachable from it (the daily job's runs)."""
        at_head = sorted(self.git("tag", "--points-at", self.head).split())
        latest = self.git("describe", "--tags", "--match", "daily/*", "--abbrev=0", self.head, ok_codes=(0, 128)).strip()
        return {"at_head": at_head, "latest_daily": latest or None}

    def _build_date(self) -> str:
        """manifest.json's build_date for this corpus when its head is this repo's HEAD, else the date of the
        build marker, else record_to."""
        corpus = self.manifest_entry()
        if corpus and corpus.get("build_date"):
            return str(corpus["build_date"])
        marker = self.repo / ".git" / self.BUILD_MARKER
        if marker.exists():
            return datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        return self.record_to

    def _load_folders(self) -> dict[str, str]:
        folders = {}
        for line in self.git("ls-tree", "-r", "-d", "--name-only", self.head).splitlines():
            parts = line.split("/")
            if len(parts) == 2:
                folders[parts[1].lower()] = line
        return folders

    def notice(self) -> str:
        return NOTICE.format(record_to=self.record_to, build_date=self.build_date)

    def envelope(self) -> dict:
        return {"notice": self.notice(), "record_from": self.record_from, "record_to": self.record_to,
                "build_date": self.build_date}

    def scope(self) -> str:
        """What a search-style answer over this corpus covers: its label and molecule counts, read from the repo."""
        return notices.scope(self.jurisdiction, labels=len(self.by_setid), molecules=self.molecule_count())

    def molecule_count(self) -> int:
        """Molecules with at least one label in this record: from the molecules/ crosswalk, which files a
        combination label under every ingredient (velpatasvir has no folder of its own), else from the folders."""
        self.molecules()
        listed = {inn for setid in self.by_setid for inn, _ in self._setid_molecules.get(setid, [])}
        return len(listed) if listed else len({folder.split("/")[0] for folder in self.folders.values()})

    # ---- labels and versions

    def resolve_label(self, label_id: str) -> str:
        """The one setid that ``label_id`` names (a setid, 'dailymed:<setid>' or a unique prefix)."""
        if not isinstance(label_id, str) or not label_id.strip():
            raise LabelError("label_id must be a DailyMed setid or a unique prefix of one")
        key = label_id.strip().lower().removeprefix("dailymed:")
        if key in self.by_setid:
            return key
        setids = sorted(s for s in self.by_setid if s.startswith(key))
        if not setids:
            raise LabelError(f"no label matches {label_id!r}; use find_label to look one up by name, INN or NDC")
        if len(setids) > 1:
            raise LabelError(f"{label_id!r} matches several labels: {', '.join(setids)}")
        return setids[0]

    def label_versions(self, setid: str) -> list[Version]:
        return self.by_setid[setid]

    def as_of(self, setid: str, day: str) -> tuple[Version, Version | None]:
        """The version DailyMed showed on ``day`` and the one that replaced it, if any."""
        return as_of_versions(self.by_setid[setid], day)

    def next_version(self, v: Version) -> Version | None:
        lv = self.by_setid[v.setid]
        i = lv.index(v)
        return lv[i + 1] if i + 1 < len(lv) else None

    def previous_version(self, v: Version) -> Version | None:
        lv = self.by_setid[v.setid]
        i = lv.index(v)
        return lv[i - 1] if i else None

    def commit_as_of(self, day: str) -> str | None:
        """The newest commit before the running maximum of the dates passes ``day``; its tree holds every
        label's newest version up to that day, except labels in ``out_of_order_setids`` (see __init__)."""
        check_date(day)
        i = bisect.bisect_right(self.published, day)
        return self.versions[i - 1].commit if i else None

    def versions_between(self, day_from: str, day_to: str) -> list[Version]:
        """Versions dated in [from, to], in date order (same-day ties: commit order)."""
        check_date(day_from, "from")
        check_date(day_to, "to")
        if day_to < day_from:
            raise LabelError(f"'to' ({day_to}) must not be earlier than 'from' ({day_from})")
        lo = bisect.bisect_left(self.dated_published, day_from)
        hi = bisect.bisect_right(self.dated_published, day_to)
        return self.dated[lo:hi]

    def folder(self, setid: str) -> str:
        if setid in self.folders:
            return self.folders[setid]
        first = self.by_setid[setid][0]
        for path in self.git("show", "--format=", "--name-only", first.commit).splitlines():
            if f"/{setid}/" in path:
                self.folders[setid] = path.rsplit("/", 1)[0]
                return self.folders[setid]
        raise LabelError(f"label directory for {setid} not found in {first.commit[:12]}")

    def setid_of_path(self, path: str) -> str:
        parts = path.split("/")
        if len(parts) < 3:
            raise LabelError(f"unexpected path outside a label folder: {path}")
        return parts[1].lower()

    # ---- metadata and crosswalk

    def all_metadata(self) -> dict[str, dict]:
        """label.json (EU: product.json) of every label at HEAD, loaded once with one cat-file call."""
        with self._lock:
            if self._metadata is None:
                setids = sorted(self.by_setid)
                specs = [f"{self.head}:{self.folder(s)}/{self.META}" for s in setids]
                texts = self.texts(specs)
                self._metadata = {setid: json.loads(texts[spec]) if texts[spec] else {"setid": setid}
                                  for setid, spec in zip(setids, specs)}
        return self._metadata

    def metadata(self, setid: str) -> dict:
        return self.all_metadata()[setid]

    def status(self, setid: str) -> dict:
        """Current/retired status from the molecules/ crosswalk (``us[]`` entries; no entry or no status
        means current). last_published is the label's newest version in the repo. Status is kept out of
        the corpus so a retirement never rewrites history."""
        lv = self.by_setid[setid]
        entries = [e for _, e in self.molecule_entries(setid)]
        if any(e.get("status") == "removed" for e in entries):
            # removed on request (sources/removed.json) but still in this repo: only before the rebuild
            return {"status": "removed", "last_published": lv[-1].published}
        retired = [e for e in entries if e.get("status") == "retired"]
        if not retired:
            return {"status": "current"}
        successors = sorted({s for e in retired for s in e.get("successor_setids") or []})
        return {"status": "retired", "last_published": lv[-1].published, "successor_setids": successors}

    def retired_note(self, setid: str, day: str) -> str | None:
        st = self.status(setid)
        if st["status"] != "retired" or day <= st["last_published"]:
            return None
        successors = ", ".join(st["successor_setids"]) or "none recorded"
        return (f"DailyMed stopped updating this setid after {st['last_published']}; "
                f"see successors: {successors}")

    def molecules(self) -> dict[str, dict]:
        """molecules/<inn>.json by inn, loaded once."""
        with self._lock:
            if self._molecules is None:
                mols: dict[str, dict] = {}
                by_setid: dict[str, list[tuple[str, dict]]] = {}
                files = sorted(self.molecules_dir.glob("*.json")) if self.molecules_dir.exists() else []
                for path in files:
                    if path.name == "selection.json":
                        continue
                    try:
                        mol = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError) as err:
                        raise LabelError(f"cannot read crosswalk file {path.name}: {err}") from err
                    inn = (mol.get("inn") or path.stem).lower()
                    mols[inn] = mol
                    for entry in mol.get("us") or []:
                        setid = str(entry.get("setid", "")).lower()
                        if setid:
                            by_setid.setdefault(setid, []).append((inn, entry))
                self._setid_molecules = by_setid
                self._molecules = mols
        return self._molecules

    def molecule_entries(self, setid: str) -> list[tuple[str, dict]]:
        self.molecules()
        return self._setid_molecules.get(setid, [])

    def resolve_molecule(self, query: str) -> tuple[str, dict]:
        """(inn, crosswalk record) for an INN, ingredient name or brand name."""
        if not isinstance(query, str) or not query.strip():
            raise LabelError("molecule must be an INN, ingredient name or brand name")
        q = query.strip().lower()
        mols = self.molecules()
        if q in mols:
            return q, mols[q]
        hits = []
        for inn, mol in mols.items():
            names = {str(mol.get("name", "")).lower(), *(str(n).lower() for n in mol.get("ingredient_names") or [])}
            brands = {str(b).lower() for e in mol.get("us") or [] for b in e.get("brand_names") or []}
            if q in names or q in brands:
                hits.append(inn)
        if len(hits) == 1:
            return hits[0], mols[hits[0]]
        if hits:
            raise LabelError(f"{query!r} matches several molecules: {', '.join(sorted(hits))}; give the INN")
        raise LabelError(f"no molecule matches {query!r} in the crosswalk; use find_label to search labels")

    def label_inns(self, setid: str) -> list[str]:
        folder_inn = self.folder(setid).split("/")[0]
        inns = {folder_inn, *(inn for inn, _ in self.molecule_entries(setid))}
        return sorted(inns)

    def label_title(self, setid: str) -> str:
        return self.by_setid[setid][-1].title

    # ---- sections and text

    def section_files(self, v: Version) -> list[str]:
        """Section paths of a label in one version, in file-name order."""
        key = (v.commit, v.setid)
        if key not in self._files:
            folder = self.folder(v.setid)
            names = self.git("ls-tree", "--name-only", v.commit, f"{folder}/").splitlines()
            self._files[key] = sorted(n for n in names if n.endswith(".md"))
        return self._files[key]

    def all_section_files(self, setid: str) -> list[str]:
        """Every section path the label ever had."""
        folder = self.folder(setid)
        names: set[str] = set()
        for revs in self.rev_args(self.by_setid[setid]):
            names.update(self.git("log", "--format=", "--name-only", *revs, "--", f"{folder}/").split())
        return sorted(n for n in names if n.endswith(".md"))

    def text(self, commit: str, path: str) -> str:
        return self.git("show", f"{commit}:{path}")

    def texts(self, specs: list[str]) -> dict[str, str | None]:
        """Contents for many '<commit>:<path>' specs in one cat-file call (None when missing)."""
        if not specs:
            return {}
        proc = subprocess.run(["git", "-C", str(self.repo), "cat-file", "--batch"],
                              input="\n".join(specs).encode() + b"\n", capture_output=True)
        if proc.returncode != 0:
            raise LabelError(f"git cat-file failed: {proc.stderr.decode().strip()}")
        return _parse_cat_file(proc.stdout, specs)

    def header(self, v: Version, day: str | None = None) -> dict:
        """The citation block every answer that reads a version carries."""
        nxt = self.next_version(v)
        st = self.status(v.setid)
        window_to = nxt.published if nxt else None
        if nxt is None and st["status"] == "retired":
            window_to = st["last_published"]
            until = f"until DailyMed stopped updating this setid on {window_to}"
        elif nxt is None:
            until = f"until the record end on {self.record_to}"
        else:
            until = f"until {nxt.published}"
        wording = f"DailyMed showed version {v.version} of this label from {v.published} {until}"
        out = {"label_id": v.setid, "title": v.title, "version": v.version, "published_date": v.published,
               "effective_time": v.effective, "label_format": v.label_format,
               "effective_window": {"from": v.published, "to": window_to},
               "source_url": v.source_url, "commit": v.commit, "status": st["status"], "wording": wording}
        if nxt is not None and nxt.published == v.published:
            out["superseded_same_day"] = True
            out["wording"] = (f"version {v.version} was superseded the same day ({v.published}) by version "
                              f"{nxt.version}; DailyMed showed it on no date")
        if v.format_conversion:
            out["format_conversion"] = v.format_conversion
        note = self.retired_note(v.setid, day) if day else None
        if note:
            out["status_note"] = note
        if day and day > self.record_to:
            out["record_note"] = f"as_of {day} is {self.after_record_end()}"
        return out

    # ---- blame

    def blame_lines(self, commit: str, path: str) -> list[tuple[str, str, str]]:
        """(crediting commit, text, original file) for each line of ``path`` at ``commit``."""
        key = (commit, path)
        if key not in self._blame:
            porcelain = self.git("blame", "--line-porcelain", *BLAME_OPTS, commit, "--", path)
            rows, current, origin = [], None, path
            for line in porcelain.splitlines():
                head = line.split(" ")
                if len(head) >= 3 and len(head[0]) == 40 and head[1].isdigit() and head[2].isdigit():
                    current = head[0]
                elif line.startswith("filename "):
                    origin = line[len("filename "):]
                elif line.startswith("\t"):
                    rows.append((current or "", line[1:], origin))
            self._blame[key] = rows
        return self._blame[key]

    def credit(self, commit: str) -> Version:
        v = self.by_commit.get(commit)
        if v is None:
            raise LabelError(f"blame credited commit {commit[:12]}, which is not a label version")
        return v

    def word_credits(self, v: Version, path: str, line: str) -> list[tuple[str, Version]]:
        """(word, version) for each word of ``line`` as credited at version ``v`` (see WORD_BLAME_METHOD)."""
        key = (v.commit, path, line)
        if key in self._word_credit:
            return self._word_credit[key]
        line_credit = self._line_credit(v, path, line)
        words = line.split()
        result = [(w, line_credit) for w in words]
        prev = self.previous_version(line_credit)
        if prev is not None:
            old_lines = self._section_lines(prev, path)
            match = difflib.get_close_matches(line, old_lines, n=1, cutoff=WORD_MATCH_CUTOFF)
            if match:
                old_words = match[0].split()
                old_credits = self.word_credits(prev, path, match[0])
                for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_words, b=words, autojunk=False).get_opcodes():
                    if tag == "equal":
                        for k in range(i2 - i1):
                            result[j1 + k] = (words[j1 + k], old_credits[i1 + k][1])
        self._word_credit[key] = result
        return result

    def _line_credit(self, v: Version, path: str, line: str) -> Version:
        for commit, text, _ in self.blame_lines(v.commit, path):
            if text == line:
                return self.credit(commit)
        return v

    def _section_lines(self, v: Version, path: str) -> list[str]:
        if path not in self.section_files(v):
            code = self.code(path)
            same = [f for f in self.section_files(v) if self.code(f) == code]
            if not same:
                return []
            path = same[0]
        return [text for _, text, _ in self.blame_lines(v.commit, path) if text.strip()]

    # ---- diff and log

    def word_diff(self, a: str, b: str, paths: list[str]) -> list[dict]:
        """Per file: a plain word diff ({+added+} / [-removed-]) and word counts."""
        if not paths:
            return []
        return self._parse_word_diff(self.git("diff", "--word-diff=porcelain", "--no-color", a, b, "--", *paths))

    def word_diff_renamed(self, a: str, path_a: str, b: str, path_b: str) -> dict:
        """Word diff of one section whose file name changed between ``a`` and ``b``."""
        out = self._parse_word_diff(self.git("diff", "--word-diff=porcelain", "--no-color", f"{a}:{path_a}", f"{b}:{path_b}"))
        if not out:
            out = [{**self.entry(path_b), "path": path_b, "words_added": 0, "words_removed": 0, "word_diff": ""}]
        return {**out[0], **self.entry(path_b), "path": path_b, "renamed_from": Path(path_a).name}

    def blob_numstat(self, spec_a: str, spec_b: str) -> tuple[int, int]:
        """(lines added, lines removed) between two '<commit>:<path>' blobs."""
        out = self.git("diff", "--numstat", spec_a, spec_b).strip()
        if not out:
            return 0, 0
        added, removed = out.split("\t", 2)[:2]
        return (int(added) if added.isdigit() else 0), (int(removed) if removed.isdigit() else 0)

    def _parse_word_diff(self, out: str) -> list[dict]:
        files: list[dict] = []
        current: dict | None = None
        pieces: list[str] = []
        skip = ("---", "+++", "index ", "@@", "new file", "deleted file", "similarity index", "rename from", "rename to")
        for line in out.splitlines():
            if line.startswith("diff --git"):
                if current:
                    current["word_diff"] = "".join(pieces)
                    files.append(current)
                path = line.split(" b/", 1)[1]
                if not is_label_path(path):     # a corpus root file: skip its hunks
                    current, pieces = None, []
                    continue
                current = {**self.entry(path), "path": path, "words_added": 0, "words_removed": 0}
                pieces = []
            elif current is None or line.startswith(skip):
                continue
            elif line == "~":
                pieces.append("\n")
            elif line.startswith("+"):
                current["words_added"] += len(line[1:].split())
                pieces.append("{+" + line[1:] + "+}")
            elif line.startswith("-"):
                current["words_removed"] += len(line[1:].split())
                pieces.append("[-" + line[1:] + "-]")
            elif line.startswith(" "):
                pieces.append(line[1:])
        if current:
            current["word_diff"] = "".join(pieces)
            files.append(current)
        return files

    def log_numstat(self, versions: list[Version], paths: list[str] = ()) -> dict[str, list[dict]]:
        """commit -> changed files (status, lines added/removed) over a run of consecutive versions.
        A section file deleted and another created under the same LOINC code in one commit (the
        slug changed) is reported once, as 'renamed', with the line counts of the real change."""
        changes: dict[str, list[dict]] = {}
        for revs in self.rev_args(versions):
            out = self.git("log", "--raw", "--numstat", "--format=%x00%H", *revs, "--", *paths)
            self._parse_numstat(out, changes)
        for commit, files in changes.items():
            changes[commit] = self._pair_renames(commit, files)
        return changes

    def _pair_renames(self, commit: str, files: list[dict]) -> list[dict]:
        groups: dict[tuple[str, str], list[dict]] = {}
        for f in files:
            groups.setdefault((f["path"].rsplit("/", 1)[0], f["code"]), []).append(f)
        out: list[dict] = []
        for group in groups.values():
            deleted = [f for f in group if f["status"] == "deleted"]
            created = [f for f in group if f["status"] == "created"]
            if len(group) == 2 and len(deleted) == 1 and len(created) == 1:
                old, new = deleted[0], created[0]
                added, removed = self.blob_numstat(f"{commit}~1:{old['path']}", f"{commit}:{new['path']}")
                out.append({**new, "status": "renamed", "renamed_from": old["file"], "previous_path": old["path"],
                            "lines_added": added, "lines_removed": removed})
            else:
                out.extend(group)
        return sorted(out, key=lambda f: f["path"])

    def rev_args(self, versions: list[Version]) -> list[list[str]]:
        """Revision arguments covering exactly ``versions``: one range when they are a long
        consecutive run of the linear history, otherwise explicit commit lists."""
        if not versions:
            return []
        first, last = versions[0], versions[-1]
        positions = [self.position[v.commit] for v in versions]
        consecutive = (positions == sorted(positions) and positions[-1] - positions[0] + 1 == len(versions))
        if consecutive and len(versions) >= RANGE_MIN:
            return [[last.commit] if first is self.versions[0] else [f"{first.commit}~1..{last.commit}"]]
        commits = [v.commit for v in versions]
        return [["--no-walk=unsorted", *commits[i:i + ARGV_COMMITS]] for i in range(0, len(commits), ARGV_COMMITS)]

    def _parse_numstat(self, out: str, changes: dict[str, list[dict]]) -> None:
        status: dict[str, str] = {}
        commit = ""
        for line in out.splitlines():
            if line.startswith("\x00"):
                commit = line[1:]
                changes[commit] = []
                status = {}
            elif line.startswith(":"):
                meta, _, path = line.partition("\t")
                status[path] = meta.split()[-1][0]
            elif line and line[0].isdigit() or line.startswith("-\t"):
                added, removed, path = line.split("\t", 2)
                if not is_label_path(path):
                    continue
                changes[commit].append({**self.entry(path), "path": path,
                                        "status": {"A": "created", "D": "deleted"}.get(status.get(path, "M"), "changed"),
                                        "lines_added": int(added) if added.isdigit() else 0,
                                        "lines_removed": int(removed) if removed.isdigit() else 0})

    def pickaxe(self, pattern: str, versions: list[Version], paths: list[str] = ()) -> list[str]:
        """Commits among ``versions`` (consecutive) whose diff adds or removes a line matching
        the case-insensitive ERE ``pattern``; long ranges are scanned in parallel chunks."""
        if not versions:
            return []
        chunks = [versions]
        if len(versions) >= PICKAXE_PARALLEL_MIN:
            size = (len(versions) + PICKAXE_CHUNKS - 1) // PICKAXE_CHUNKS
            chunks = [versions[i:i + size] for i in range(0, len(versions), size)]

        def scan(chunk: list[Version]) -> list[str]:
            found = []
            for revs in self.rev_args(chunk):
                found.extend(self.git("log", "-i", "-G", pattern, "--format=%H", *revs, "--", *paths).split())
            return found

        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            hits = {c for part in pool.map(scan, chunks) for c in part}
        return sorted(hits, key=self.position.__getitem__)

    def grep_files(self, pattern: str, trees: list[str], paths: list[str] = ()) -> dict[str, dict[str, int]]:
        """tree -> {path: match count} for a case-insensitive ERE over the given trees."""
        if not trees:
            return {}
        out = self.git("grep", "-i", "-c", "-E", pattern, *trees, "--", *paths, ok_codes=(0, 1))
        found: dict[str, dict[str, int]] = {t: {} for t in trees}
        for line in out.splitlines():
            tree, _, rest = line.partition(":")
            path, _, count = rest.rpartition(":")
            if is_label_path(path):
                found.setdefault(tree, {})[path] = int(count)
        return found


class EuIndex(Index):
    """The EU corpus (build/eu): one commit per Commission decision whose English Annex I converted.

    Versions take the US shape so the git plumbing is shared: setid is the product folder name
    (EMEA-H-C-nnnnnn), published is the Effective-Date trailer (the decision date), version the
    Source-Version. corpus.json gives the record window, product.json the product metadata."""

    BUILD_MARKER = EU_BUILD_MARKER
    META = "product.json"
    jurisdiction = "eu"
    date_key = "effective_date"
    BUILD_HINT = "python3 tools/eu.py build"
    NO_VERSIONS = "no ec-union-register or ema-product-information Source-Id trailers"

    def __init__(self, repo: Path | str, molecules_dir: Path | str | None = None):
        super().__init__(repo, molecules_dir)
        try:
            self.corpus = json.loads(self.git("show", "main:corpus.json"))
        except (LabelError, ValueError) as err:
            raise LabelError(f"EU repo {self.repo.name!r} has no readable corpus.json at main: {err}") from err
        # The record window comes from the commits (corpus.json holds constants only).
        self.converter = self.versions[-1].converter
        self._eu_numbers: dict[tuple, str] | None = None
        self._text_commits: set[str] | None = None

    def all_metadata(self) -> dict[str, dict]:
        """product.json of every product at HEAD, plus the facts that may change after a product's first
        commit (mah, comparable_to_us, comparability_note), which live in the molecules/ crosswalk
        (``eu[]`` reference entries) rather than in the corpus."""
        metas = super().all_metadata()
        mols = self.molecules()   # takes self._lock itself, so it is read before the lock below
        with self._lock:
            if not getattr(self, "_merged", False):
                entries = {}
                for mol in mols.values():
                    for e in mol.get("eu") or []:
                        if e.get("label_id") and e.get("role") == "reference":
                            entries[e["label_id"]] = e
                for setid, meta in metas.items():
                    e = entries.get(setid) or {}
                    for k in ("mah", "comparable_to_us", "comparability_note"):
                        if k not in meta:
                            meta[k] = e.get(k)
                self._merged = True
        return metas

    def entry(self, path: str) -> dict:
        return eu_section_entry(path)

    def code(self, path: str) -> str:
        return eu_section_code(path)

    def pick(self, files: list[str], section: str) -> str:
        return pick_eu_section(files, section)

    def pick_group(self, files: list[str], section: str) -> list[str]:
        return [pick_eu_section(files, section)]

    def section_paths(self, code: str | None) -> list[str]:
        return eu_section_paths(code)

    def after_record_end(self) -> str:
        return (f"after the record end ({self.record_to}, the newest decision in the repo; the Union Register was "
                f"read on {self.build_date}); later decisions may exist")

    def _load_versions(self) -> list[Version]:
        raw = self.git("log", "--reverse", "--format=%H%x00%ae%x00%B%x1e", "main")
        out = []
        for record in raw.split("\x1e"):
            if not record.strip():
                continue
            commit, email, body = record.strip("\n").split("\x00", 2)
            t = _trailers(body)
            source_id = t.get("Source-Id", "")
            if not source_id.startswith(EU_SOURCE_PREFIXES):
                continue
            parts = source_id.split(":")
            register = source_id.startswith("ec-union-register:")
            eu_number = parts[1] if register else ""
            subject = body.strip().splitlines()[0]
            title = subject.split(" — ")[0].strip()
            out.append(Version(
                commit, email.split(".")[0], int(t["Source-Version"]), t["Effective-Date"],
                t.get("Decision-Date", ""), "", t.get("Source-URL", ""), t.get("Source-SHA256", ""),
                (eu_number,) if eu_number else (), title, "", jurisdiction="eu", name=title, eu_number=eu_number,
                decision_number=t.get("Decision-Number", ""), decision_date=t.get("Decision-Date", ""),
                procedure_id=parts[2] if register and len(parts) > 2 else "",
                ema_procedure=t.get("EMA-Procedure", ""), procedure_type=t.get("Procedure-Type", ""),
                procedure_closed=t.get("Procedure-Closed", ""), corrigendum_of=t.get("Corrigendum-Of", ""),
                annex_identical_to=t.get("Annex-Identical-To", ""), history=t.get("History", "in-force"),
                captured_date=t.get("Captured-Date", ""), converter=t.get("Converter-Version", "")))
        return out

    def _load_folders(self) -> dict[str, str]:
        folders = {}
        for line in self.git("ls-tree", "-r", "-d", "--name-only", self.head).splitlines():
            parts = line.split("/")
            if len(parts) == 2:
                folders[parts[1]] = line
        return folders

    def setid_of_path(self, path: str) -> str:
        parts = path.split("/")
        if len(parts) < 3:
            raise LabelError(f"unexpected path outside a product folder: {path}")
        return parts[1]

    def notice(self) -> str:
        return EU_NOTICE.format(record_to=self.record_to, build_date=self.build_date)

    def eu_numbers(self) -> dict[tuple, str]:
        """eu_number_key(EU number) -> label id, from product.json."""
        if self._eu_numbers is None:
            numbers = {}
            for setid, meta in self.all_metadata().items():
                key = eu_number_key(str(meta.get("eu_number") or ""))
                if key:
                    numbers[key] = setid
            self._eu_numbers = numbers
        return self._eu_numbers

    def alternate(self, label_id: str) -> tuple[str, dict] | None:
        """(molecule inn, molecules eu[] entry) when ``label_id`` is a listed alternate (not built)."""
        for inn, mol in self.molecules().items():
            for e in mol.get("eu") or []:
                if e.get("label_id") == label_id and e.get("role") == "alternate":
                    return inn, e
        return None

    def removed_entry(self, label_id: str) -> tuple[str, dict] | None:
        """(molecule inn, molecules eu[] entry) when ``label_id`` is marked ``removed: true`` (sources/removed.json)."""
        for inn, mol in self.molecules().items():
            for e in mol.get("eu") or []:
                if e.get("label_id") == label_id and e.get("removed"):
                    return inn, e
        return None

    def resolve_label(self, label_id: str) -> str:
        """The product folder name (EMEA-H-C-nnnnnn) for any accepted EU id form (see canonical_eu_id)."""
        if not isinstance(label_id, str) or not label_id.strip():
            raise LabelError("label_id must be an EU product id such as EMEA-H-C-003820, EMEA/H/C/003820 or EU/1/15/1024")
        canon = canonical_eu_id(label_id, self.eu_numbers())
        if canon and canon in self.by_setid:
            return canon
        if canon and self.removed_entry(canon):
            raise LabelError(f"{canon} was removed from the corpus on request ({REMOVED_NOTE}); no text is served "
                             "for it")
        if canon:
            alt = self.alternate(canon)
            if alt:
                inn, e = alt
                ref = (self.molecules()[inn].get("eu_selection") or {}).get("reference_label_id") or "none"
                raise LabelError(f"{canon} ({e.get('name')}) is not built: v1 builds one reference product per "
                                 f"molecule, and this is an alternate of {ref} ({inn})")
        if canon is None and not (EMEA_ID.match(_strip_prefix(label_id)) or eu_number_key(_strip_prefix(label_id))):
            raise LabelError(f"{label_id!r} is not an EU label id; use EMEA-H-C-nnnnnn, EMEA/H/C/nnnnnn or an EU "
                             "number such as EU/1/15/1024")
        raise LabelError(f"no EU label matches {label_id!r}; use find_label to look one up by name, INN or EU number")

    def as_of(self, setid: str, day: str) -> tuple[Version, Version | None]:
        """The newest commit of the product with Effective-Date <= day (same-day ties: the later commit)."""
        check_date(day)
        lv = self.by_setid[setid]
        shown = [v for v in lv if v.published <= day]
        if not shown:
            first = lv[0]
            source = (f"Commission decision {first.decision_number}" if first.decision_number
                      else f"the EMA product information captured on {first.captured_date}")
            raise LabelError(f"no EU text for {self.label_title(setid)} ({setid}) on {day}; the record for this "
                             f"product starts {first.published} with {source}")
        later = [v for v in lv if v.published > day]
        return shown[-1], (later[0] if later else None)

    def status(self, setid: str) -> dict:
        """History type (product.json), record window (from the product's first commit to the corpus record
        end) and the comparability fields (molecules/ crosswalk) of a product."""
        meta = self.metadata(setid)
        lv = self.by_setid[setid]
        return {"history_type": meta.get("history_type") or lv[-1].history,
                "record_from": lv[0].published, "record_to": self.record_to,
                "comparable_to_us": meta.get("comparable_to_us"),
                "comparability_note": meta.get("comparability_note")}

    def retired_note(self, setid: str, day: str) -> str | None:
        return None

    def label_inns(self, setid: str) -> list[str]:
        return [self.folder(setid).split("/")[0]]

    def label_title(self, setid: str) -> str:
        return self.metadata(setid).get("name") or self.by_setid[setid][-1].title

    def text_changed(self, commit: str) -> bool:
        """Whether the commit changed a section file (181 decisions reprint Annex I unchanged)."""
        with self._lock:
            if self._text_commits is None:
                self._text_commits = set(self.git("log", "--format=%H", "main", "--", "*.md").split())
        return commit in self._text_commits

    def corrigendum_note(self, v: Version, day: str) -> str | None:
        if not v.corrigendum_of or not v.procedure_closed or day >= v.procedure_closed:
            return None
        prev = self.previous_version(v)
        if v.corrigendum_of == "not identified":
            n = prev.version if prev else v.version - 1
            return (f"the register published this corrected annex on {v.procedure_closed}; until then it showed "
                    f"the previous annex, version {n}")
        named = next((x for x in self.by_setid[v.setid] if x.procedure_id == v.corrigendum_of), None)
        n = named.version if named else (prev.version if prev else v.version - 1)
        return (f"the register published this corrected annex on {v.procedure_closed}; until then it showed the "
                f"annex of procedure {v.corrigendum_of} (version {n} of this product)")

    def header(self, v: Version, day: str | None = None) -> dict:
        meta = self.metadata(v.setid)
        st = self.status(v.setid)
        nxt = self.next_version(v)
        name = meta.get("name") or v.title
        ema = meta.get("ema_product_number") or v.setid.replace("-", "/")
        until = f"until {nxt.published}" if nxt else f"until the record end on {st['record_to']}"
        if v.history == "current-only":
            wording = (f"EMA product information for {name} ({ema}) captured on {v.captured_date or v.published} "
                       "carries this text; no dated history before the capture")
        else:
            wording = (f"The Union Register carries this text as Annex I of Commission decision {v.decision_number} "
                       f"of {v.decision_date} (version {v.version} of {name}, {ema}), in force from {v.published} {until}")
            if v.corrigendum_of:
                wording += (f"; this is the corrected annex the register published on "
                            f"{v.procedure_closed or 'a date the register does not give'} "
                            f"(corrigendum of procedure {v.corrigendum_of})")
            if v.annex_identical_to:
                wording += f"; its annex is byte-identical to that of procedure {v.annex_identical_to}"
        out = {"jurisdiction": "eu", "label_id": v.setid, "title": name, "ema_product_number": ema,
               "eu_number": meta.get("eu_number") or v.eu_number, "molecule": meta.get("molecule"),
               "version": v.version, "effective_date": v.published, "decision_number": v.decision_number or None,
               "decision_date": v.decision_date or None, "procedure_id": v.procedure_id or None,
               "ema_procedure": v.ema_procedure or None, "procedure_type": v.procedure_type or None,
               "published_date": v.procedure_closed or None}
        if v.corrigendum_of:
            out["corrigendum_of"] = v.corrigendum_of
        if v.annex_identical_to:
            out["annex_identical_to"] = v.annex_identical_to
        if v.captured_date:
            out["captured_date"] = v.captured_date
        out.update({"history_type": st["history_type"], **product_window(st),
                    "effective_window": {"from": v.published, "to": nxt.published if nxt else None},
                    "source_url": v.source_url, "commit": v.commit, "text_changed": self.text_changed(v.commit)})
        if nxt is not None and nxt.published == v.published:
            out["superseded_same_day"] = True
            wording += f"; version {v.version} was replaced the same day by version {nxt.version}; no date returns it"
        out["wording"] = wording
        if day:
            note = self.corrigendum_note(v, day)
            if note:
                out["corrigendum_note"] = note
            if day > self.record_to:
                out["record_note"] = f"as_of {day} is {self.after_record_end()}"
        return out

    def citation(self, v: Version, path: str | None, day: str) -> dict:
        return {"doc_id": v.setid, "version": v.version, "section": self.code(path) if path else None, "as_of": day}


def product_window(st: dict) -> dict:
    """An EU product's record window under its own keys, so it never overwrites the corpus envelope's
    record_from / record_to at the top level of an answer."""
    return {"product_record_from": st["record_from"], "product_record_to": st["record_to"]}


def _status_fields(ix: Index, setid: str) -> dict:
    """ix.status() for merging into an answer or row: an EU product's window goes under product_record_*."""
    st = dict(ix.status(setid))
    if ix.jurisdiction == "eu":
        st.update(product_window(st))
        del st["record_from"], st["record_to"]
    return st


def eu_header_line(head: dict) -> str:
    """One line citing an EU header: the wording, the label, version and decision (or capture), the record
    window and any corrigendum or record-end note. Used by the label:// resource and the CLI."""
    if head.get("decision_number"):
        source = f"decision {head['decision_number']} of {head['decision_date']}"
    else:
        source = f"captured {head.get('captured_date') or head['effective_date']}"
    line = (f"{head['wording']} (label {head['label_id']}, version {head['version']}, {source}). "
            f"Product record {head['product_record_from']} to {head['product_record_to']}.")
    for key in ("corrigendum_note", "record_note"):
        if head.get(key):
            line += f" Note: {head[key]}."
    return line


def is_eu_repo(repo: Path) -> bool:
    """Whether ``repo`` is an EU corpus: built here (.git/asof-build-eu) or a clone of one (a .git directory
    or gitlink file, and corpus.json at main naming corpus "eu")."""
    git_path = repo / ".git"
    if (git_path / EU_BUILD_MARKER).exists():
        return True
    if not git_path.exists():
        return False
    proc = subprocess.run(["git", "-C", str(repo), "show", "main:corpus.json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    try:
        return json.loads(proc.stdout).get("corpus") == "eu"
    except ValueError:
        return False


def open_eu(path: Path | str | None, molecules_dir: Path | str | None = None) -> EuIndex | None:
    """The EU index of an EU corpus repo (see is_eu_repo); None when ``path`` is None or not an EU corpus.
    An EU corpus that will not index raises LabelError."""
    if path is None or str(path) == "":
        return None
    repo = Path(path)
    if not is_eu_repo(repo):
        return None
    return EuIndex(repo, molecules_dir)


def resolve_eu_repo(arg: str | Path | None) -> Path | None:
    """The EU repo to load: an explicit --eu-repo (empty string: none), else env ASOF_EU_REPO (empty: none),
    else eu/ when it is an EU corpus (the submodule layout), else build/eu when it is one, else None."""
    if arg is not None:
        return Path(arg) if str(arg) else None
    env = os.environ.get(EU_REPO_ENV)
    if env is not None:
        return Path(env) if env else None
    for repo in (SUBMODULE_EU_REPO, BUILD_EU_REPO):
        if is_eu_repo(repo):
            return repo
    return None


def as_of_versions(lv: list[Version], day: str) -> tuple[Version, Version | None]:
    """The version of one label (``lv`` oldest first) shown on ``day``, and its successor."""
    check_date(day)
    shown = [v for v in lv if v.published <= day]
    if not shown:
        raise LabelError(f"no version of {lv[0].setid} was published on or before {day}; "
                         f"the record for this label starts {lv[0].published} (version {lv[0].version})")
    later = [v for v in lv if v.published > day]
    return shown[-1], (later[0] if later else None)


def _trailers(body: str) -> dict[str, str]:
    out = {}
    for line in body.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key and " " not in key:
            out[key] = value.strip()
    return out


def chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def diff_targets(files_a: list[str], files_b: list[str], section: str | None,
                 index: Index | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """What to diff between two versions: paths for one tree diff (unchanged file names, plus
    files only one side has) and (old path, new path) pairs for sections whose file name changed."""
    code_of = index.code if index is not None else section_code
    group_of = index.pick_group if index is not None else pick_section_group
    codes_a = {code_of(f): f for f in files_a}
    codes_b = {code_of(f): f for f in files_b}
    codes = sorted(set(codes_a) | set(codes_b))
    if section:
        codes = sorted({code_of(f) for f in group_of(sorted(set(files_a) | set(files_b)), section)})
    tree_paths, renamed = [], []
    for code in codes:
        pa, pb = codes_a.get(code), codes_b.get(code)
        if pa and pb and pa != pb:
            renamed.append((pa, pb))
        else:
            tree_paths.append(pa or pb)
    return tree_paths, renamed


def is_label_path(path: str) -> bool:
    """Whether a repo path lies inside a label or product folder (``<inn>/<id>/<file>``). The root files of a
    corpus (README.md, NOTICE.md, corpus.json, written in its first commit) are not label text, so every reader
    of raw git output skips them."""
    return path.count("/") >= 2


def section_paths(code_or_word: str | None, prefix: str = "*") -> list[str]:
    """Pathspecs selecting one section's files (by LOINC code) across labels, or all sections."""
    if not code_or_word:
        return [f"{prefix}/*.md"]
    code = code_or_word.strip().lower()
    if not re.match(r"^\d{4,5}-\d$", code):
        raise LabelError(f"a corpus-wide section filter must be a LOINC code such as 43685-7, not {code_or_word!r}")
    return [f"{prefix}/{code}-*.md"]


# ---------------------------------------------------------------- routing

def _jurisdiction(value: str | None, default: str) -> str:
    j = (value or default).strip().lower()
    if j not in ("us", "eu", "all"):
        raise LabelError(f"jurisdiction must be 'us', 'eu' or 'all', not {value!r}")
    return j


def _index_for(index: Index, eu: EuIndex | None, label_id) -> Index:
    """The index a label id belongs to: EU ids (EMEA..., EU/...) go to the EU index."""
    if is_eu_id(label_id):
        if eu is None:
            raise LabelError(EU_REASON)
        return eu
    return index


def _head(ix: Index, v: Version, day: str | None, eu: EuIndex | None) -> dict:
    """ix.header(); US headers gain jurisdiction 'us' only when an EU index is loaded (answers without an EU index stay US-only)."""
    head = ix.header(v, day)
    if eu is not None and ix.jurisdiction == "us":
        return {"jurisdiction": "us", **head}
    return head


def _sub_envelope(ix: Index) -> dict:
    return {"notice": ix.notice(), "record_from": ix.record_from, "record_to": ix.record_to,
            "build_date": ix.build_date}


def pair_for(section: str) -> Pair | None:
    """The pair a LOINC code, QRD number or pair word names (None when it has no counterpart)."""
    s = section.strip().lower().replace(" ", "-").removesuffix(".md").removeprefix("smpc-")
    for p in PAIRS:
        fallback = {c for group in p.us_fallbacks for c in group}
        if s in (p.us, p.eu, p.name) or s in fallback or s in p.words:
            return p
    return None


def _translate_section(section: str | None) -> tuple[str | None, str | None, str | None]:
    """(US LOINC filter, EU QRD filter, note) for a corpus-wide section filter with jurisdiction 'all'."""
    if not section:
        return None, None, None
    s = section.strip().lower().removeprefix("smpc-")
    if LOINC.match(s):
        p = pair_for(s)
        if p:
            return s, p.eu, None
        return s, None, f"{s} has no SmPC counterpart in the pairing table; the EU side has no section filter"
    if QRD_NUMBER.match(s):
        p = pair_for(s)
        if p:
            return p.us, s, None
        return None, s, f"QRD section {s} has no US counterpart in the pairing table; the US side has no section filter"
    raise LabelError(f"a corpus-wide section filter must be a LOINC code (e.g. 43685-7) or a QRD number (e.g. 4.4), "
                     f"not {section!r}")


# ---------------------------------------------------------------- tools

def _lines_with(text: str, phrase: str) -> list[str]:
    return [line for line in text.splitlines() if phrase in normalise(line)]


def _label_summary(index: Index, setid: str) -> dict:
    lv = index.label_versions(setid)
    meta = index.metadata(setid)
    return {"id": setid, "title": index.label_title(setid), "labeler": meta.get("labeler", ""),
            "application_numbers": sorted(set(meta.get("application_numbers") or lv[-1].application_numbers)),
            "inn": index.label_inns(setid), "products": meta.get("products", []),
            "first_published": lv[0].published, "last_published": lv[-1].published, "versions": len(lv),
            **index.status(setid)}


def _eu_block(jurisdiction: str | None) -> dict | None:
    j = (jurisdiction or "us").strip().lower()
    if j not in ("us", "eu", "all"):
        raise LabelError(f"jurisdiction must be 'us', 'eu' or 'all', not {jurisdiction!r}")
    return {"eu_available": False, "eu_reason": EU_REASON} if j in ("eu", "all") else None


def _us_matches(index: Index, query: str) -> list[dict]:
    q = query.strip().lower()
    digits = ndc_digits(q)
    hits: dict[str, set[str]] = {}

    def hit(setid: str, how: str) -> None:
        if setid in index.by_setid:
            hits.setdefault(setid, set()).add(how)

    setid_key = q.removeprefix("dailymed:")
    if SETID_CHARS.match(setid_key) and len(setid_key) >= 4:
        for setid in index.by_setid:
            if setid.startswith(setid_key):
                hit(setid, "setid")
    for setid, meta in index.all_metadata().items():
        names = [p.get("name", "") for p in meta.get("products", [])] + [index.label_title(setid)]
        if len(q) >= 3 and any(q in n.lower() for n in names if n):
            hit(setid, "name")
        if q == str(meta.get("inn", "")).lower():
            hit(setid, "inn")
        if len(digits) >= 8 and any(ndc_digits(n).startswith(digits) for n in meta.get("package_ndcs", [])):
            hit(setid, "ndc")
    for inn, mol in index.molecules().items():
        names = {inn, str(mol.get("name", "")).lower(), *(str(n).lower() for n in mol.get("ingredient_names") or [])}
        for entry in mol.get("us") or []:
            setid = str(entry.get("setid", "")).lower()
            if q in names:
                hit(setid, "inn")
            if len(q) >= 3 and any(q in str(b).lower() for b in entry.get("brand_names") or []):
                hit(setid, "name")
            if len(digits) >= 8 and any(digits.startswith(ndc_digits(n)) or ndc_digits(n).startswith(digits)
                                        for n in entry.get("product_ndcs") or []):
                hit(setid, "ndc")
            if q.isdigit() and q in {str(r) for r in entry.get("rxcui") or []}:
                hit(setid, "rxcui")
    return [{**_label_summary(index, setid), "matched_on": sorted(hits[setid])} for setid in sorted(hits)]


US_HINT = ("no label matches; try a brand name (e.g. Synthroid), an INN (levothyroxine), "
           "a package NDC (0074-3727-13), an RxCUI or a setid prefix")
EU_HINT = ("no EU label matches; try a product name (e.g. Keytruda), an INN (pembrolizumab), an EMA number "
           "(EMEA/H/C/003820), an EU number (EU/1/15/1024) or a holder name")


def _eu_row(eu: EuIndex, setid: str, matched: set[str]) -> dict:
    meta = eu.metadata(setid)
    lv = eu.label_versions(setid)
    st = eu.status(setid)
    return {"id": setid, "jurisdiction": "eu", "title": meta.get("name") or lv[-1].title, "holder": meta.get("mah"),
            "ema_product_number": meta.get("ema_product_number"), "eu_number": meta.get("eu_number"),
            "inn": [meta.get("molecule") or eu.label_inns(setid)[0]], "molecule_name": meta.get("molecule_name"),
            "history_type": st["history_type"], "record_from": st["record_from"], "record_to": st["record_to"],
            "versions": len(lv), "first_effective": lv[0].published, "last_effective": lv[-1].published,
            "comparable_to_us": meta.get("comparable_to_us"), "comparability_note": meta.get("comparability_note"),
            "built": True, "matched_on": sorted(matched)}


def _eu_matches(eu: EuIndex, query: str) -> list[dict]:
    q = query.strip().lower()
    hits: dict[str, set[str]] = {}
    alternates: dict[str, tuple[str, dict, set[str]]] = {}
    near: dict[str, tuple[str, dict]] = {}
    canon = canonical_eu_id(query, eu.eu_numbers())
    number = eu_number_key(_strip_prefix(query))
    if canon and canon in eu.by_setid:
        hits.setdefault(canon, set()).add("eu_number" if number else "label_id")
    for setid, meta in eu.all_metadata().items():
        names = [str(meta.get("name") or ""), str(meta.get("register_name") or "")]
        if len(q) >= 3 and any(q in n.lower() for n in names if n):
            hits.setdefault(setid, set()).add("name")
        if q in {str(meta.get("molecule") or "").lower(), str(meta.get("molecule_name") or "").lower()}:
            hits.setdefault(setid, set()).add("inn")
        if len(q) >= 4 and q in str(meta.get("mah") or "").lower():
            hits.setdefault(setid, set()).add("mah")
    for inn, mol in eu.molecules().items():
        names = {inn, str(mol.get("name", "")).lower(), *(str(n).lower() for n in mol.get("ingredient_names") or [])}
        for e in mol.get("eu") or []:
            label_id = e.get("label_id")
            if e.get("role") == "reference" and label_id in eu.by_setid:
                if q in names:
                    hits.setdefault(label_id, set()).add("inn")
            elif e.get("role") == "alternate" or (e.get("removed") and label_id not in eu.by_setid):
                how = set()
                if len(q) >= 3 and q in str(e.get("name") or "").lower():
                    how.add("name")
                if canon == label_id or (number and number == eu_number_key(str(e.get("eu_number") or ""))):
                    how.add("eu_number" if number else "label_id")
                if how:
                    alternates[label_id] = (inn, e, how)
        for nm in (mol.get("eu_selection") or {}).get("near_misses_not_matched") or []:
            label_id = canonical_eu_id(str(nm.get("ema_product_number") or ""))
            if label_id and len(q) >= 3 and q in str(nm.get("name") or "").lower():
                near.setdefault(label_id, (inn, nm))
    rows = [_eu_row(eu, setid, hits[setid]) for setid in sorted(hits)]
    for label_id in sorted(alternates):
        inn, e, how = alternates[label_id]
        sel = eu.molecules()[inn].get("eu_selection") or {}
        ref = sel.get("reference_label_id")
        removed = bool(e.get("removed"))
        if removed:
            note = f"removed from the corpus on request ({REMOVED_NOTE})"
        elif ref:
            note = f"not built; alternate of {ref}"
        else:
            note = f"not built; no EU reference for {inn}: {sel.get('no_reference_reason') or 'none recorded'}"
        rows.append({"id": label_id, "jurisdiction": "eu", "title": e.get("name"), "holder": e.get("mah"),
                     "ema_product_number": e.get("ema_product_number"), "eu_number": e.get("eu_number"),
                     "inn": [inn], "molecule_name": eu.molecules()[inn].get("name"),
                     "history_type": "removed" if removed else "not-built",
                     "record_from": None, "record_to": None, "versions": None, "comparable_to_us": None,
                     "comparability_note": None, "built": False, "generic": e.get("generic"),
                     "biosimilar": e.get("biosimilar"), "note": note, "matched_on": sorted(how)})
    for label_id in sorted(set(near) - set(alternates) - set(hits)):
        inn, nm = near[label_id]
        sel = eu.molecules()[inn].get("eu_selection") or {}
        reason = sel.get("no_reference_reason") or f"the EU reference of {inn} is {sel.get('reference_label_id')}"
        rows.append({"id": label_id, "jurisdiction": "eu", "title": nm.get("name"), "holder": None,
                     "ema_product_number": nm.get("ema_product_number"), "eu_number": None, "inn": [inn],
                     "molecule_name": eu.molecules()[inn].get("name"), "history_type": "not-built",
                     "record_from": None, "record_to": None, "versions": None, "comparable_to_us": None,
                     "comparability_note": None, "built": False, "kind": nm.get("kind"), "matched_on": ["near_miss"],
                     "note": f"central product not matched by the exact-INN rule for {inn} "
                             f"({nm.get('kind')} near miss): {reason}"})
    return rows


def find_label(index: Index, query: str, jurisdiction: str | None = None, eu: EuIndex | None = None) -> dict:
    """Labels matching a setid (or prefix), brand or product name, INN, NDC or RxCUI; with an EU index
    also EU products by id, EU number, name, INN or holder. Default jurisdiction: 'all' with an EU index
    loaded, 'us' without (the US-only answer)."""
    if not isinstance(query, str) or not query.strip():
        raise LabelError("query must be a setid, brand name, INN, NDC or RxCUI")
    if eu is None:
        blk = _eu_block(jurisdiction)
        out = {**index.envelope(), "query": query, "matches": []}
        if blk:
            out.update(blk)
        if (jurisdiction or "us").lower() == "eu" or query.strip().upper().startswith("EU/"):
            out.update({"eu_available": False, "eu_reason": EU_REASON, "count": 0, "hint": EU_REASON})
            return out
        out["matches"] = _us_matches(index, query)
        out["count"] = len(out["matches"])
        if not out["matches"]:
            out["hint"] = US_HINT
        return out
    j = _jurisdiction(jurisdiction, "all")
    if j == "eu":
        out = {**eu.envelope(), "query": query, "jurisdiction": "eu", "matches": _eu_matches(eu, query)}
    else:
        out = {**index.envelope(), "query": query, "jurisdiction": j}
        if j == "all":
            out["eu"] = _sub_envelope(eu)
        us_rows = [{"jurisdiction": "us", **r} for r in _us_matches(index, query)]
        out["matches"] = us_rows + (_eu_matches(eu, query) if j == "all" else [])
    out["count"] = len(out["matches"])
    out["eu_available"] = True
    if not out["matches"]:
        out["hint"] = {"us": US_HINT, "eu": EU_HINT, "all": US_HINT + "; " + EU_HINT}[j]
    return out


def read_label(index: Index, label_id: str, date: str, section: str | None = None, cap: int = READ_CAP_CHARS,
               eu: EuIndex | None = None) -> dict:
    """The text in force on ``date`` (US: what DailyMed showed; EU: the Annex I of the Commission
    decision in force): one section, or the section index plus capped text."""
    ix = _index_for(index, eu, label_id)
    setid = ix.resolve_label(label_id)
    v, _ = ix.as_of(setid, check_date(date))
    files = ix.section_files(v)
    out = {**ix.envelope(), "as_of": date, **_head(ix, v, date, eu)}
    if section:
        path = ix.pick(files, section)
        out["section"] = ix.entry(path)
        out["text"] = ix.text(v.commit, path)
        return out
    specs = [f"{v.commit}:{f}" for f in files]
    texts = ix.texts(specs)
    out["sections"] = []
    parts, used, truncated = [], 0, []
    for f, spec in zip(files, specs):
        text = texts[spec] or ""
        out["sections"].append({**ix.entry(f), "lines": len(text.splitlines()), "chars": len(text)})
        if used + len(text) <= cap:
            parts.append(f"=== {Path(f).name}\n\n{text}")
            used += len(text)
        else:
            truncated.append(Path(f).name)
    out["text"] = "\n".join(parts)
    if truncated:
        out["truncated"] = truncated
        out["note"] = (f"text capped at {cap} characters; {len(truncated)} section(s) left out: "
                       f"{', '.join(truncated)}. Ask for one section at a time with the section argument.")
    return out


def _eu_version_fields(v: Version) -> dict:
    return {"decision_number": v.decision_number or None, "decision_date": v.decision_date or None,
            "procedure_id": v.procedure_id or None}


def section_history(index: Index, label_id: str, section: str, eu: EuIndex | None = None) -> dict:
    """Every version that created, changed or deleted one section's file."""
    ix = _index_for(index, eu, label_id)
    setid = ix.resolve_label(label_id)
    paths = ix.pick_group(ix.all_section_files(setid), section)
    lv = ix.label_versions(setid)
    changes = ix.log_numstat(lv, paths)
    history = []
    for v in lv:
        for ch in changes.get(v.commit, []):
            if ix.jurisdiction == "us":
                row = {"version": v.version, "published_date": v.published, "effective_time": v.effective,
                       "status": ch["status"], "lines_added": ch["lines_added"], "lines_removed": ch["lines_removed"],
                       "file": ch["file"], "format_conversion": v.format_conversion or None, "source_url": v.source_url}
            else:
                row = {"version": v.version, "effective_date": v.published, **_eu_version_fields(v),
                       "status": ch["status"], "lines_added": ch["lines_added"], "lines_removed": ch["lines_removed"],
                       "file": ch["file"], "source_url": v.source_url}
            if "renamed_from" in ch:
                row["renamed_from"] = ch["renamed_from"]
            history.append(row)
    newest = history[-1]["file"] if history else paths[-1]  # the file name as of the section's latest change
    out = {**ix.envelope(), "label_id": setid, "title": ix.label_title(setid),
           "section": ix.entry(newest), "changes": history, "count": len(history), **_status_fields(ix, setid)}
    if ix.jurisdiction == "eu":
        out = {"jurisdiction": "eu", **out}
    elif eu is not None:
        out = {"jurisdiction": "us", **out}
    if len(paths) > 1:
        out["file_names"] = [Path(p).name for p in paths]
        out["note"] = "the section's file name changed across versions; 'renamed' rows carry the old name"
    return out


def blame(index: Index, label_id: str, date: str, section: str, words: bool = False,
          eu: EuIndex | None = None) -> dict:
    """Each line of a section on ``date`` credited to the version that first wrote it."""
    ix = _index_for(index, eu, label_id)
    setid = ix.resolve_label(label_id)
    v, _ = ix.as_of(setid, check_date(date))
    path = ix.pick(ix.section_files(v), section)
    dk = ix.date_key
    out = {**ix.envelope(), "as_of": date, **_head(ix, v, date, eu), "section": ix.entry(path),
           "method": "git blame -M -C: moved text keeps the version that first wrote it", "lines": []}
    for n, (commit, text, origin) in enumerate(ix.blame_lines(v.commit, path), start=1):
        src = ix.credit(commit)
        row = {"line": n, "text": text, "version": src.version, dk: src.published}
        if ix.jurisdiction == "eu":
            row.update(_eu_version_fields(src))
        if origin != path:
            row["moved_from"] = ix.entry(origin)["code"]
        if words and text.strip():
            row["words"] = [{"text": w, "version": c.version, dk: c.published}
                            for w, c in ix.word_credits(v, path, text)]
        out["lines"].append(row)
    if words:
        out["word_method"] = WORD_BLAME_METHOD
    if ix.jurisdiction == "eu":
        seen = sorted({(r["version"], r[dk], r["decision_number"], r["procedure_id"])
                       for r in out["lines"] if r["text"].strip()})
        out["contributing_versions"] = [{"version": a, dk: b, "decision_number": c, "procedure_id": d}
                                        for a, b, c, d in seen]
    else:
        versions = sorted({(r["version"], r["published_date"]) for r in out["lines"] if r["text"].strip()})
        out["contributing_versions"] = [{"version": a, "published_date": b} for a, b in versions]
    return out


def diff(index: Index, label_id: str, date_a: str, date_b: str, section: str | None = None,
         eu: EuIndex | None = None) -> dict:
    """Word-level diff between the versions in force on two dates."""
    ix = _index_for(index, eu, label_id)
    setid = ix.resolve_label(label_id)
    check_date(date_a, "date_a")
    check_date(date_b, "date_b")
    if date_b < date_a:
        raise LabelError("date_b must not be earlier than date_a")
    a, _ = ix.as_of(setid, date_a)
    b, _ = ix.as_of(setid, date_b)
    lv = ix.label_versions(setid)
    between = lv[lv.index(a) + 1:lv.index(b) + 1] if ix.jurisdiction == "eu" else \
        [x for x in lv if a.published < x.published <= b.published]
    if ix.jurisdiction == "eu":
        rows = [{"version": x.version, "effective_date": x.published, **_eu_version_fields(x)} for x in between]
    else:
        rows = [{"version": x.version, "published_date": x.published,
                 "format_conversion": x.format_conversion or None} for x in between]
    out = {**ix.envelope(), "label_id": setid, "title": ix.label_title(setid),
           "from": {"as_of": date_a, **_head(ix, a, date_a, eu)}, "to": {"as_of": date_b, **_head(ix, b, date_b, eu)},
           "versions_between": rows}
    if ix.jurisdiction == "eu":
        out = {"jurisdiction": "eu", **out}
    elif eu is not None:
        out = {"jurisdiction": "us", **out}
    if a.commit == b.commit:
        out.update({"same_version": True, "sections": [], "note": "Same version on both dates: no differences."})
        return out
    tree_paths, renamed = diff_targets(ix.section_files(a), ix.section_files(b), section, ix)
    sections = ix.word_diff(a.commit, b.commit, tree_paths)
    sections += [ix.word_diff_renamed(a.commit, pa, b.commit, pb) for pa, pb in renamed]
    sections.sort(key=lambda s: s["code"])
    out.update({"same_version": False, "sections": sections, "changed_sections": len(sections)})
    if not sections:
        out["note"] = f"No changes to {section or 'the label'} between these versions."
    return out


def changes_between(index: Index, from_date: str, to_date: str, jurisdiction: str | None = None,
                    section: str | None = None, query: str | None = None, limit: int | None = None,
                    compact: bool = False, eu: EuIndex | None = None) -> dict:
    """Every label version in [from, to] with its changed sections; with ``query``, only the changes
    where a section gained, lost or reworded the phrase. ``compact`` lists section codes only.
    ``limit`` defaults to DEFAULT_LIMIT with a query, DEFAULT_LIMIT_ALL without. Jurisdiction 'us'
    (default), 'eu' (QRD section numbers) or 'all' (the US answer plus an ``eu`` sub-object)."""
    if eu is None:
        blk = _eu_block(jurisdiction)
        return _changes_one(index, from_date, to_date, index.section_paths(section), section, query, limit, compact,
                            blk, eu_empty=bool(blk) and jurisdiction.strip().lower() == "eu")
    j = _jurisdiction(jurisdiction, "us")
    if j == "us":
        return _changes_one(index, from_date, to_date, index.section_paths(section), section, query, limit, compact)
    if j == "eu":
        out = _changes_one(eu, from_date, to_date, eu.section_paths(section), section, query, limit, compact)
        return {"jurisdiction": "eu", **out, "eu_available": True}
    us_sec, eu_sec, note = _translate_section(section)
    out = _changes_one(index, from_date, to_date, index.section_paths(us_sec), us_sec, query, limit, compact)
    try:
        sub = _changes_one(eu, from_date, to_date, eu.section_paths(eu_sec), eu_sec, query, limit, compact)
    except LabelError as err:
        sub = {**_sub_envelope(eu), "error": str(err)}
    out.update({"jurisdiction": "all", "section_requested": section, "eu": {"jurisdiction": "eu", **sub},
                "eu_available": True})
    if note:
        out["section_note"] = note
    return out


def _changes_one(ix: Index, from_date: str, to_date: str, paths: list[str], section: str | None, query: str | None,
                 limit: int | None, compact: bool, extra: dict | None = None, eu_empty: bool = False) -> dict:
    window = ix.versions_between(from_date, to_date)
    if to_date < ix.record_from:
        raise LabelError(f"{to_date} is before the record starts ({ix.record_from}); no label was published yet")
    if limit is None:
        limit = DEFAULT_LIMIT if query is not None else DEFAULT_LIMIT_ALL
    out = {**ix.envelope(), "from": from_date, "to": to_date, "section": section, "query": query,
           "versions_in_window": len(window)}
    if from_date > ix.record_to:
        out["record_note"] = f"the window starts {ix.after_record_end()}"
    if extra:
        out.update(extra)
        if eu_empty:
            out.update({"count": 0, "changes": [], "truncated": False, "versions_in_window": 0})
            return out
    out["scope"] = ix.scope()
    if query is None:
        out.update(_all_changes(ix, window, paths, limit, compact))
        return out
    out.update(_phrase_changes(ix, window, paths, query, limit, compact))
    return out


SECTION_KEYS = ("code", "title", "file", "status", "lines_added", "lines_removed", "renamed_from")


def _section_summary(f: dict, compact: bool) -> dict | str:
    return f["code"] if compact else {k: f[k] for k in SECTION_KEYS if k in f}


def _eu_row_base(ix: EuIndex, v: Version) -> dict:
    st = ix.status(v.setid)
    return {"label_id": v.setid, "title": ix.label_title(v.setid), "version": v.version, "effective_date": v.published,
            **_eu_version_fields(v), "jurisdiction": "eu", "history_type": st["history_type"], **product_window(st)}


def _all_changes(index: Index, window: list[Version], paths: list[str], limit: int, compact: bool) -> dict:
    """Window versions with changed section files, streamed in chunks so only ``limit`` full rows are kept."""
    rows, count, unchanged, metadata_only = [], 0, 0, 0
    meta = index.META
    for chunk in chunks(window, ARGV_COMMITS):
        changes = index.log_numstat(chunk, [*paths, f"*/{meta}"])
        for v in chunk:
            files = [f for f in changes.get(v.commit, []) if f["file"] != meta]
            if not files:
                unchanged += 1
                metadata_only += bool(changes.get(v.commit))
                continue
            count += 1
            if len(rows) < limit:
                if index.jurisdiction == "us":
                    rows.append({"label_id": v.setid, "title": v.title, "version": v.version,
                                 "published_date": v.published, "new_label": index.previous_version(v) is None,
                                 "format_conversion": v.format_conversion or None,
                                 "sections": [_section_summary(f, compact) for f in files]})
                else:
                    rows.append({**_eu_row_base(index, v), "new_label": index.previous_version(v) is None,
                                 "sections": [_section_summary(f, compact) for f in files]})
    note = ("unchanged_versions are versions in the window whose section text did not change in this scope "
            f"(identical text after normalisation, or only {meta}: metadata_only_versions); they are not listed. "
            f"{NEW_LABEL_NOTE}")
    if index.jurisdiction == "us":
        note += " " + notices.FORMAT_CONVERSION
    return {"count": count, "changes": rows, "truncated": count > limit, "compact": compact,
            "unchanged_versions": unchanged, "metadata_only_versions": metadata_only, "note": note}


def _phrase_changes(index: Index, window: list[Version], paths: list[str], query: str, limit: int,
                    compact: bool) -> dict:
    """Candidate commits come from git log -G; each is classified from its before/after section
    texts, loaded in chunks of TEXT_CHUNK_COMMITS so memory stays bounded on long windows."""
    phrase = normalise(query)
    pattern = ere_pattern(query)
    candidates = [index.by_commit[c] for c in index.pickaxe(pattern, window, paths) if c in index.by_commit]
    changes, new_labels = [], []
    for chunk in chunks(candidates, TEXT_CHUNK_COMMITS):
        changed = index.log_numstat(chunk, paths)
        specs = [spec for commit, files in changed.items() for f in files for spec in _before_after(commit, f)]
        texts = index.texts(specs)
        for v in chunk:
            for f in changed.get(v.commit, []):
                after_spec, before_spec = _before_after(v.commit, f)
                _classify(index, v, f, texts.get(before_spec) or "", texts.get(after_spec) or "", phrase,
                          changes, new_labels, compact)
    dk = index.date_key
    for rows in (changes, new_labels):
        rows.sort(key=lambda r: (r[dk], r["label_id"], r["section"]["code"]))
    summary = {kind: sum(1 for c in changes if c["change"] == kind) for kind in ("added", "removed", "reworded")}
    note = f"{LINES_NOTE} {NEW_LABELS_NOTE} A renamed section file is classified once, before vs after."
    if index.jurisdiction == "us":
        note += " " + notices.FORMAT_CONVERSION
        note += " " + notices.OLD_FORMAT_SECTIONS
    return {"phrase": phrase, "count": len(changes), "summary": summary, "changes": changes[:limit],
            "truncated": len(changes) > limit, "new_labels": new_labels, "compact": compact, "note": note}


# A version with no earlier version of its label in this record (Index.previous_version is None). The record holds
# every version the source still lists, so this is often version 1 but not always (e.g. a label whose record starts
# at version 48); it says nothing about when the label first reached DailyMed or the Union Register.
NEW_LABEL_NOTE = ("new_label is true for a version that is the first version of its label in this record (no earlier "
                  "version of that label is in the record); that is often version 1 but not always, and it does not "
                  "mean the label was new on DailyMed or in the Union Register.")
NEW_LABELS_NOTE = ("new_labels lists each version in the window that is the first version of its label in this record "
                   "(no earlier version of that label is in the record, so there is nothing to compare it with; often "
                   "version 1 but not always, and not necessarily new on DailyMed or in the Union Register) and whose "
                   "section already carried the phrase; these are not counted as added. Later versions of the same "
                   "label in the window are compared with their previous version as usual, so a label can be in "
                   "new_labels and in changes.")
LINES_NOTE = ("Every row in changes carries lines_before (the lines holding the phrase in that section of the previous "
              "version) and lines_after (the same in this version): an added row has only lines_after, a removed row "
              "only lines_before (the deleted text, with lines_after empty), a reworded row both. A new_labels row "
              "has lines_after, with lines_before null. With compact, both are line counts.")


def label_headings(text: str) -> list[str]:
    """The label's own top-level section headings in a section file, upper-cased. An old-format US label files
    its WARNINGS and PRECAUTIONS sections under 43685-7, so these say what the label itself called them."""
    heads = (re.sub(r"^[\d.]+\s+", "", line[2:].strip()).upper() for line in text.splitlines() if line.startswith("# "))
    return [h for h in heads if h]


def _before_after(commit: str, f: dict) -> tuple[str, str]:
    """cat-file specs of a changed file after and before its commit (the old name when renamed)."""
    return f"{commit}:{f['path']}", f"{commit}~1:{f.get('previous_path', f['path'])}"


def _classify(index: Index, v: Version, f: dict, before: str, after: str, phrase: str,
              changes: list[dict], new_labels: list[dict], compact: bool) -> None:
    now, was = phrase in normalise(after), phrase in normalise(before)
    if index.jurisdiction == "us":
        row = {"label_id": v.setid, "title": v.title, "version": v.version, "published_date": v.published,
               "section": section_entry(f["path"]), "format_conversion": v.format_conversion or None,
               "source_url": v.source_url}
    else:
        row = {**_eu_row_base(index, v), "section": index.entry(f["path"]), "source_url": v.source_url}
    if f["status"] == "renamed":
        row.update({"renamed": True, "renamed_from": f["renamed_from"]})
    if index.jurisdiction == "us":
        headings = label_headings(after if now else before)
        if headings and headings != [row["section"]["title"].upper()]:
            row["section"] = {**row["section"], "label_headings": headings}
    after_lines, before_lines = _lines_with(after, phrase), _lines_with(before, phrase)
    reworded = was and now and before_lines != after_lines
    if compact:
        after_lines, before_lines = len(after_lines), len(before_lines)
    if index.previous_version(v) is None:
        if now:
            new_labels.append({**row, "new_label": True, "lines_before": None, "lines_after": after_lines})
        return
    if now and not was:
        kind = "added"
    elif was and not now:
        kind = "removed"
    elif reworded:
        kind = "reworded"
    else:
        return
    changes.append({**row, "change": kind, "lines_before": before_lines, "lines_after": after_lines})


def search(index: Index, text: str, date: str, jurisdiction: str | None = None, section: str | None = None,
           limit: int = DEFAULT_LIMIT, eu: EuIndex | None = None) -> dict:
    """Labels whose text in force on ``date`` contains ``text`` (case- and whitespace-insensitive).
    Jurisdiction 'us' (default), 'eu' or 'all' (the US answer plus an ``eu`` sub-object)."""
    if eu is None:
        blk = _eu_block(jurisdiction)
        return _search_one(index, text, date, section, index.section_paths, limit, None, blk,
                           eu_empty=bool(blk) and jurisdiction.strip().lower() == "eu")
    j = _jurisdiction(jurisdiction, "us")
    if j == "us":
        return _search_one(index, text, date, section, index.section_paths, limit, eu)
    if j == "eu":
        return {"jurisdiction": "eu", **_search_one(eu, text, date, section, eu.section_paths, limit, eu),
                "eu_available": True}
    us_sec, eu_sec, note = _translate_section(section)
    out = _search_one(index, text, date, us_sec, index.section_paths, limit, eu)
    try:
        sub = _search_one(eu, text, date, eu_sec, eu.section_paths, limit, eu)
    except LabelError as err:
        sub = {**_sub_envelope(eu), "error": str(err)}
    out.update({"jurisdiction": "all", "section_requested": section, "eu": {"jurisdiction": "eu", **sub},
                "eu_available": True})
    if note:
        out["section_note"] = note
    return out


def _search_one(ix: Index, text: str, date: str, section: str | None, paths_of, limit: int, eu: EuIndex | None,
                extra: dict | None = None, eu_empty: bool = False) -> dict:
    check_date(date)
    pattern = ere_pattern(text)
    out = {**ix.envelope(), "text": text, "as_of": date, "section": section, "matches": []}
    if extra:
        out.update(extra)
        if eu_empty:
            out.update({"count": 0, "truncated": False})
            return out
    out["scope"] = ix.scope()
    commit = ix.commit_as_of(date)
    if commit is None:
        raise LabelError(f"{date} is before the record starts ({ix.record_from}); no label was published yet")
    paths = paths_of(section)
    found = ix.grep_files(pattern, [commit], paths).get(commit, {})
    per_label: dict[str, dict[str, int]] = {}
    late = ix.out_of_order_setids
    for path, count in found.items():
        if not is_label_path(path) or Path(path).name == ix.META or ix.setid_of_path(path) in late:
            continue
        per_label.setdefault(ix.setid_of_path(path), {})[path] = count
    for setid in sorted(late):   # read each out-of-order label from its own as-of commit
        if ix.by_setid[setid][0].published > date:
            continue
        v, _ = ix.as_of(setid, date)
        folder = ix.folder(setid)
        scoped = [f"{folder}/{p.split('/', 1)[1] if p.startswith('*/') else p}" for p in paths] or [f"{folder}/"]
        for path, count in ix.grep_files(pattern, [v.commit], scoped).get(v.commit, {}).items():
            if Path(path).name != ix.META:
                per_label.setdefault(setid, {})[path] = count
    for setid in sorted(per_label):
        v, _ = ix.as_of(setid, date)
        row = {**_head(ix, v, date, eu), "sections": [{**ix.entry(p), "matches": n}
                                                      for p, n in sorted(per_label[setid].items())]}
        out["matches"].append(row)
    out["count"] = len(out["matches"])
    out["matches"] = out["matches"][:limit]
    out["truncated"] = out["count"] > limit
    if ix.jurisdiction == "us":
        out["method"] = ("git grep on the newest commit published on or before the date; that tree holds every "
                         "label's newest version up to that day because history is linear in Published-Date order")
    else:
        out["method"] = ("git grep on the newest commit whose Effective-Date (Commission decision date) is on or "
                         "before the date; that tree holds every product's newest Annex I up to that day because "
                         "history is linear in Effective-Date order")
    if late:
        out["method"] += (f"; {len(late)} label(s) with a version appended after a later-dated one were each grepped "
                          "at their own version in force on the date instead")
    return out


def first_appearance(index: Index, text: str, label_id: str | None = None, limit: int = DEFAULT_LIMIT,
                     jurisdiction: str | None = None, eu: EuIndex | None = None) -> dict:
    """Per label, the first version containing ``text`` and the version where it left (if it did).
    A label appears once per entry/exit episode, so ``count`` counts episodes and
    ``labels_count`` distinct labels. With ``label_id`` the id decides the corpus; otherwise
    jurisdiction 'us' (default), 'eu' or 'all' (the US answer plus an ``eu`` sub-object)."""
    if label_id:
        ix = _index_for(index, eu, label_id)
        if jurisdiction is not None and _jurisdiction(jurisdiction, "us") != ix.jurisdiction:
            raise LabelError(f"label_id {label_id!r} is a {ix.jurisdiction.upper()} label but jurisdiction is "
                             f"{jurisdiction!r}; omit jurisdiction or make it agree")
        out = _first_one(ix, text, label_id, limit, eu)
        if eu is not None:
            out = {"jurisdiction": ix.jurisdiction, **out}
        return out
    j = _jurisdiction(jurisdiction, "us")
    if eu is None:
        if j == "us":
            return _first_one(index, text, None, limit, None)
        if j == "eu":
            ere_pattern(text)
            return {**index.envelope(), "text": text, "label_id": None, "episodes": [], "count": 0,
                    "labels_count": 0, "truncated": False, "eu_available": False, "eu_reason": EU_REASON}
        return {**_first_one(index, text, None, limit, None), "eu_available": False, "eu_reason": EU_REASON}
    if j == "us":
        return {"jurisdiction": "us", **_first_one(index, text, None, limit, eu)}
    if j == "eu":
        return {"jurisdiction": "eu", **_first_one(eu, text, None, limit, eu), "eu_available": True}
    out = {"jurisdiction": "all", **_first_one(index, text, None, limit, eu)}
    out.update({"eu": {"jurisdiction": "eu", **_first_one(eu, text, None, limit, eu)}, "eu_available": True})
    return out


def _first_keys(ix: Index) -> tuple[str, str]:
    return ("first_published", "left_published") if ix.jurisdiction == "us" else ("first_effective", "left_effective")


def _first_one(ix: Index, text: str, label_id: str | None, limit: int, eu: EuIndex | None) -> dict:
    pattern = ere_pattern(text)
    phrase = normalise(text)
    out = {**ix.envelope(), "text": text, "label_id": None, "episodes": []}
    if label_id:
        setid = ix.resolve_label(label_id)
        out["label_id"] = setid
        scope, paths = ix.label_versions(setid), [f"{ix.folder(setid)}/*.md"]
    else:
        scope, paths = ix.versions, ["*.md"]
        out["scope"] = ix.scope()
    candidates = ix.pickaxe(pattern, scope, paths)
    by_label: dict[str, list[Version]] = {}
    for c in candidates:
        v = ix.by_commit.get(c)
        if v:
            by_label.setdefault(v.setid, []).append(v)

    def episodes(item: tuple[str, list[Version]]) -> list[dict]:
        setid, cands = item
        cands.sort(key=lambda v: (v.published, ix.position[v.commit]))
        present = ix.grep_files(pattern, [v.commit for v in cands], [f"{ix.folder(setid)}/*.md"])
        return _timeline(ix, setid, cands, present)

    entries = []
    with ThreadPoolExecutor(max_workers=PICKAXE_CHUNKS) as pool:
        for part in pool.map(episodes, by_label.items()):
            entries.extend(part)
    first_key, _ = _first_keys(ix)
    entries.sort(key=lambda e: (e[first_key], e["label_id"]))
    out["episodes"] = entries[:limit]
    out["count"] = len(entries)
    out["labels_count"] = len({e["label_id"] for e in entries})
    out["truncated"] = len(entries) > limit
    out["note"] = "one row per entry/exit episode of the phrase in a label; a label that gained, lost and regained it has two rows"
    if entries:
        first = entries[0]
        out["earliest"] = {k: first[k] for k in ("label_id", "title", "first_version", first_key, "sections")}
    elif label_id:
        out["note"] = f"{phrase!r} never appeared in any version of {out['label_id']}"
    else:
        out["note"] = f"{phrase!r} never appeared in any label version in the record"
    return out


def _timeline(index: Index, setid: str, cands: list[Version], present: dict[str, dict[str, int]]) -> list[dict]:
    """Entry/exit episodes of a phrase in one label, from presence at each candidate version."""
    episodes: list[dict] = []
    open_ep: dict | None = None
    first_key, left_key = _first_keys(index)
    eu_side = index.jurisdiction == "eu"
    for v in cands:
        files = {p: n for p, n in present.get(v.commit, {}).items() if Path(p).name != index.META}
        if files and open_ep is None:
            open_ep = {"label_id": setid, "title": index.label_title(setid), "first_version": v.version,
                       first_key: v.published, "first_source_url": v.source_url,
                       "sections": [index.entry(p) for p in sorted(files)],
                       "new_label": index.previous_version(v) is None, "left_version": None, left_key: None}
            if eu_side:
                open_ep.update({"first_decision_number": v.decision_number or None,
                                "first_procedure_id": v.procedure_id or None})
        elif not files and open_ep is not None:
            open_ep.update({"left_version": v.version, left_key: v.published})
            if eu_side:
                open_ep["left_decision_number"] = v.decision_number or None
            episodes.append(open_ep)
            open_ep = None
    if open_ep is not None:
        open_ep["present_in_latest_version"] = True
        episodes.append(open_ep)
    for ep in episodes:
        ep.setdefault("present_in_latest_version", False)
        ep.update(_status_fields(index, setid))
        if eu_side:
            ep["jurisdiction"] = "eu"
    return episodes


# ---------------------------------------------------------------- compare_jurisdictions

def compare_jurisdictions(index: Index, molecule: str, date: str, section: str | None = None,
                          eu: EuIndex | None = None, cap: int = READ_CAP_CHARS,
                          us_label_id: str | None = None, *, line_cap: int | None = ONLY_IN_MAX_LINES) -> dict:
    """US label versus EU SmPC of one molecule on ``date``, aligned by topic (PAIRS). Without an EU
    index: the US sections of every label of the molecule and eu_available false (the US-only answer).
    ``line_cap`` limits the only_in lines listed per side with a section (None: all of them); it is not part of
    the MCP schema and exists so compare-check can round-trip every line."""
    if eu is None:
        return _compare_us_only(index, molecule, date, section, cap, us_label_id)
    return _compare_two(index, eu, molecule, date, section, cap, us_label_id, line_cap=line_cap)


def _compare_us_only(index: Index, molecule: str, date: str, section: str | None, cap: int,
                     us_label_id: str | None) -> dict:
    check_date(date)
    inn, mol = index.resolve_molecule(molecule)
    setids = sorted({str(e.get("setid", "")).lower() for e in mol.get("us") or []} & set(index.by_setid))
    if us_label_id:
        chosen = index.resolve_label(us_label_id)
        if chosen not in setids:
            raise LabelError(f"{us_label_id!r} is not a US label of {inn}")
        setids = [chosen]
    us_labels, not_yet = [], []
    for setid in setids:
        lv = index.label_versions(setid)
        if lv[0].published > date:
            not_yet.append({"label_id": setid, "title": index.label_title(setid), "record_starts": lv[0].published})
            continue
        v, _ = index.as_of(setid, date)
        files = index.section_files(v)
        row = {**index.header(v, date), "sections": [section_entry(f) for f in files]}
        if section:
            try:
                path = pick_section(files, section)
            except LabelError as err:
                row["section_error"] = str(err)
            else:
                text = index.text(v.commit, path)
                row["section"] = {**section_entry(path), "text": text[:cap], "truncated": len(text) > cap}
        us_labels.append(row)
    return {**index.envelope(), "molecule": inn, "as_of": date, "section": section,
            "us": {"labels": us_labels, "not_yet_published": not_yet, "count": len(us_labels)},
            "eu": {"available": False, "reason": EU_REASON, "labels": []},
            "eu_available": False, "eu_reason": EU_REASON,
            "note": "No EU SmPC text exists in this build; nothing on the EU side is inferred or fabricated."}


PREVIOUSLY = re.compile(r"^(.*?)\s*\(previously\s+(.+?)\)\s*$", re.I)


def eu_name_forms(name: str) -> set[str]:
    """The lower-cased, whitespace-collapsed forms an EU product name answers to: the full name, the name
    without a trailing ' (previously X)' bracket, and the former name X inside it."""
    full = normalise(str(name or ""))
    if not full:
        return set()
    forms = {full}
    m = PREVIOUSLY.match(full)
    if m:
        forms.update(f for f in (m.group(1).strip(), m.group(2).strip()) if f)
    return forms


def resolve_molecule_any(index: Index, query: str, eu: EuIndex | None = None) -> tuple[str, dict]:
    """(inn, crosswalk record) for an INN, ingredient, US brand or EU product name. EU names match a
    molecules eu[] name in full, without its ' (previously X)' suffix, or as the former name X, and with an
    EU index also a built product's register_name; case and white space are ignored."""
    try:
        return index.resolve_molecule(query)
    except LabelError as err:
        if "no molecule matches" not in str(err):
            raise
        q = normalise(query)
        hits = {inn for inn, mol in index.molecules().items()
                if any(q in eu_name_forms(e.get("name")) for e in mol.get("eu") or [])}
        if eu is not None:
            for setid, meta in eu.all_metadata().items():
                inn = meta.get("molecule") or eu.label_inns(setid)[0]
                if q and q == normalise(str(meta.get("register_name") or "")) and inn in index.molecules():
                    hits.add(inn)
        hits = sorted(hits)
        if len(hits) == 1:
            return hits[0], index.molecules()[hits[0]]
        if hits:
            raise LabelError(f"{query!r} matches several molecules: {', '.join(hits)}; give the INN") from err
        raise


def _first_word(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return words[0] if words else ""


def _us_candidates(index: Index, mol: dict, date: str) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """(in force on date, not yet published) single-ingredient NDA/BLA labels of a molecule in build/us."""
    in_force, not_yet, seen = [], [], set()
    for entry in mol.get("us") or []:
        setid = str(entry.get("setid", "")).lower()
        if setid in seen or setid not in index.by_setid or entry.get("combination") is True:
            continue
        if not {"NDA", "BLA"} & set(entry.get("marketing_categories") or []):
            continue
        seen.add(setid)
        lv = index.label_versions(setid)
        st = index.status(setid)
        if lv[0].published > date:
            not_yet.append((setid, entry))
        elif st["status"] == "retired" and st["last_published"] < date:
            continue
        else:
            in_force.append((setid, entry))
    return in_force, not_yet


NOTE_BRAND = re.compile(r"matches the US NDA/BLA brand (.+?)\s*$")
COMPARE_KEY_NOTE = ("lines are compared after compare_key normalisation (case, Unicode spaces, quotes, dashes, list "
                    "markers, trailing punctuation); only_in counts are distinct lines after compare_key "
                    "normalisation; a line repeated within a section is listed and counted once; every listed line "
                    "is a verbatim line of the cited version's section file and nothing on either side is inferred")


def note_brand(note: str | None) -> str | None:
    """The US brand a comparability_note names ('EU name matches the US NDA/BLA brand X'), else None."""
    m = NOTE_BRAND.search(note or "")
    return m.group(1) if m else None


def _brands(entry: dict) -> set[str]:
    return {normalise(str(b)) for b in entry.get("brand_names") or [] if str(b).strip()}


def _pick_primary(index: Index, cands: list[tuple[str, dict]], eu_name: str | None,
                  note: str | None) -> tuple[tuple[str, dict] | None, str]:
    """The primary US label among ``cands``: (1) a candidate carrying the brand the comparability note names,
    then one carrying the full EU name (without ' (previously ...)'); (2) a brand whose first word equals the EU
    name's first word, preferring a brand that is that word alone (INVEGA before INVEGA SUSTENNA); (3) the
    most versions. Ties at each step: most versions, earliest first version, then setid."""
    def rank(item):
        lv = index.label_versions(item[0])
        return (-len(lv), lv[0].published, item[0])

    if not cands:
        return None, ""
    named = note_brand(note)
    if named:
        hit = [c for c in cands if normalise(named) in _brands(c[1])]
        if hit:
            return sorted(hit, key=rank)[0], "brand named in comparability_note"
    if eu_name:
        base = normalise(PREVIOUSLY.sub(r"\1", str(eu_name)) if PREVIOUSLY.match(str(eu_name)) else str(eu_name))
        hit = [c for c in cands if base and base in _brands(c[1])]
        if hit:
            return sorted(hit, key=rank)[0], "brand equals the EU product name"
    want = _first_word(eu_name) if eu_name else ""
    brand = [c for c in cands if want and any(_first_word(b) == want for b in c[1].get("brand_names") or [])]
    if brand:
        return (sorted(brand, key=lambda c: (want not in _brands(c[1]), *rank(c)))[0],
                "brand first word equals the EU product name's first word")
    return sorted(cands, key=rank)[0], "most versions (ties: earliest first version, then setid)"


def _qualifies(index: Index, setid: str, entry: dict) -> bool:
    return (setid in index.by_setid and entry.get("combination") is not True
            and bool({"NDA", "BLA"} & set(entry.get("marketing_categories") or [])))


def pairing_caveat(index: Index, mol: dict, chosen: tuple[str, dict] | None, note: str | None, date: str) -> str | None:
    """Why the compared US label is not the brand the comparability note names, or None when it is (or the
    note names no brand, or no US label was chosen)."""
    named = note_brand(note)
    if not named or chosen is None or normalise(named) in _brands(chosen[1]):
        return None
    brand_b = (chosen[1].get("brand_names") or [index.label_title(chosen[0])])[0]
    tail = f"; compared {brand_b} (setid {chosen[0]}) instead, a different US product"
    entries = [(str(e.get("setid", "")).lower(), e) for e in mol.get("us") or []
               if normalise(named) in _brands(e)]
    if not entries:
        return f"the US label named in the comparability note ({named}) is not in molecules us[]" + tail
    good = [(s, e) for s, e in entries if _qualifies(index, s, e)]
    if not good:
        return (f"the US label named in the comparability note ({named}, setid {entries[0][0]}) is not an NDA/BLA "
                "single-ingredient label" + tail)
    for setid, _ in good:
        lv = index.label_versions(setid)
        if lv[0].published > date:
            return (f"the US label named in the comparability note ({named}, setid {setid}) is not yet published on "
                    f"{date} (record starts {lv[0].published})" + tail)
    for setid, _ in good:
        st = index.status(setid)
        if st.get("status") == "retired" and st["last_published"] < date:
            return (f"the US label named in the comparability note ({named}, setid {setid}) is retired; DailyMed "
                    f"last showed it on {st['last_published']} (its last published version), so it is not a "
                    f"candidate on {date}" + tail)
    return (f"the US label named in the comparability note ({named}, setid {good[0][0]}) was not chosen (us_label_id "
            "named another label)" + tail)


def us_retired_candidates(index: Index, mol: dict, date: str) -> list[dict]:
    """Single-ingredient NDA/BLA labels of a molecule in build/us retired before ``date`` (last published < date)."""
    out, seen = [], set()
    for entry in mol.get("us") or []:
        setid = str(entry.get("setid", "")).lower()
        if setid in seen or not _qualifies(index, setid, entry):
            continue
        seen.add(setid)
        st = index.status(setid)
        if st.get("status") == "retired" and st["last_published"] < date:
            out.append({"id": setid, "brand": (entry.get("brand_names") or [index.label_title(setid)])[0],
                        "last_published": st["last_published"]})
    return out


def no_us_label_phrase(inn: str, date: str) -> str:
    return f"DailyMed showed no single-ingredient NDA/BLA label of {inn} on {date} in the US corpus"


def _us_files_for(pair: Pair, by: dict[str, str]) -> tuple[list[str], bool]:
    if pair.us in by:
        return [by[pair.us]], False
    for group in pair.us_fallbacks:
        found = [by[c] for c in group if c in by]
        if found:
            return found, True
    return [], False


def _side(ix: Index, v: Version, paths: list[str], texts: dict, fallback: bool = False) -> tuple[dict, str]:
    text = "\n".join(texts.get(f"{v.commit}:{p}") or "" for p in paths)
    codes = [ix.code(p) for p in paths]
    side = {"code": "+".join(codes), "file": Path(paths[0]).name, "lines": len(text.splitlines()), "chars": len(text)}
    if len(paths) > 1 or fallback:
        side["files"] = [Path(p).name for p in paths]
        side["fallback"] = fallback
    return side, text


def _only_in(lines: list[tuple[str, str]], other: set[str]) -> list[str]:
    out, seen = [], set()
    for line, key in lines:
        if key not in other and key not in seen:
            seen.add(key)
            out.append(line)
    return out


def _compare_two(index: Index, eu: EuIndex, molecule: str, date: str, section: str | None, cap: int,
                 us_label_id: str | None, *, line_cap: int | None = ONLY_IN_MAX_LINES) -> dict:
    check_date(date)
    inn, mol = resolve_molecule_any(index, molecule, eu)
    sel = mol.get("eu_selection") or {}
    eu_entries = mol.get("eu") or []
    ref_entry = next((e for e in eu_entries if e.get("role") == "reference"), None)
    ref_id = sel.get("reference_label_id") or (ref_entry or {}).get("label_id")
    notes: list[str] = []
    eu_removed = bool(ref_id and ref_id not in eu.by_setid and ref_entry and ref_entry.get("removed"))

    # ---- EU side
    eu_side: dict = {**_sub_envelope(eu), "product": None, "reason": None,
                     "alternates_not_built": [{"label_id": e.get("label_id"), "name": e.get("name"),
                                               "generic": e.get("generic"), "biosimilar": e.get("biosimilar")}
                                              for e in eu_entries if e.get("role") == "alternate"]}
    comparability = {"comparable_to_us": None, "note": None}
    eu_v: Version | None = None
    eu_name = (ref_entry or {}).get("name")
    if ref_id and ref_id in eu.by_setid:
        meta = eu.metadata(ref_id)
        eu_name = meta.get("name") or eu_name
        comparability = {"comparable_to_us": meta.get("comparable_to_us"), "note": meta.get("comparability_note")}
        lv = eu.label_versions(ref_id)
        if lv[0].published > date:
            first = lv[0]
            eu_side["product"] = {"label_id": ref_id, "name": eu_name, "not_yet": True,
                                  "product_record_from": first.published,
                                  "first_decision_number": first.decision_number or None,
                                  "history_type": eu.status(ref_id)["history_type"]}
            eu_side["reason"] = (f"no EU text for {eu_name} ({ref_id}) on {date}; the record for this product starts "
                                 f"{first.published} with Commission decision {first.decision_number}")
        else:
            eu_v, _ = eu.as_of(ref_id, date)
            st = eu.status(ref_id)
            eu_side["product"] = {**eu.header(eu_v, date), "history_type": st["history_type"], **product_window(st),
                                  "ema_product_number": meta.get("ema_product_number"),
                                  "eu_number": meta.get("eu_number"), "mah": meta.get("mah"),
                                  "sections": [eu.entry(f) for f in eu.section_files(eu_v)]}
    elif ref_id and eu_removed:
        eu_side["reason"] = (f"the EU reference product {ref_id} was removed from the corpus on request "
                             f"({REMOVED_NOTE})")
        comparability = {"comparable_to_us": ref_entry.get("comparable_to_us"),
                         "note": ref_entry.get("comparability_note")}
    elif ref_id:
        eu_side["reason"] = f"the EU reference product {ref_id} is not built in this EU repo"
        if ref_entry:
            comparability = {"comparable_to_us": ref_entry.get("comparable_to_us"),
                             "note": ref_entry.get("comparability_note")}
    else:
        eu_side["reason"] = sel.get("no_reference_reason") or "no EU reference product in the crosswalk"
        comparability["note"] = f"no EU reference product: {eu_side['reason']}"

    # ---- US side
    in_force, not_yet = _us_candidates(index, mol, date)
    us_v: Version | None = None
    chosen, chosen_by = None, ""
    if us_label_id:
        setid = index.resolve_label(us_label_id)
        entries = {str(e.get("setid", "")).lower(): e for e in mol.get("us") or []}
        if setid not in entries:
            raise LabelError(f"{us_label_id!r} is not a US label of {inn}; its setids are listed by find_label {inn!r}")
        index.as_of(setid, date)   # raises when the label has no version on date
        chosen, chosen_by = (setid, entries[setid]), "us_label_id"
    else:
        chosen, chosen_by = _pick_primary(index, in_force, eu_name, comparability.get("note"))
    us_label = None
    if chosen:
        us_v, _ = index.as_of(chosen[0], date)
        us_label = {**_head(index, us_v, date, eu), "brands": chosen[1].get("brand_names") or [],
                    "marketing_categories": chosen[1].get("marketing_categories") or [], "chosen_by": chosen_by,
                    "sections": [section_entry(f) for f in index.section_files(us_v)]}

    def summary(setid: str, entry: dict) -> dict:
        lv = index.label_versions(setid)
        return {"id": setid, "title": index.label_title(setid), "brands": entry.get("brand_names") or [],
                "versions": len(lv), "first_published": lv[0].published}

    others = [summary(s, e) for s, e in in_force if not chosen or s != chosen[0]]
    out = {**index.envelope(), "molecule": inn, "molecule_name": mol.get("name"), "as_of": date, "section": section,
           "eu_available": not eu_removed, "comparability": comparability,
           "us": {"label": us_label, "other_labels": others,
                  "not_yet_published": [{**summary(s, e), "record_starts": index.label_versions(s)[0].published}
                                        for s, e in not_yet],
                  "count": len(in_force) + (1 if chosen and chosen[0] not in {s for s, _ in in_force} else 0)},
           "eu": eu_side, "pairs": [], "us_only_sections": [], "eu_only_sections": []}
    caveat = pairing_caveat(index, mol, chosen, comparability.get("note"), date)
    if caveat:
        comparability["pairing_caveat"] = caveat
    if comparability.get("comparable_to_us") in ("no", "unknown"):
        notes.append(f"comparable_to_us is {comparability['comparable_to_us']!r}: {comparability.get('note')}")
    if caveat:
        notes.append(caveat)
    if us_v is None:
        notes.append(no_us_label_phrase(inn, date) + " (a retired label counts until its last published version)")
    if eu_v is None:
        notes.append(f"no EU text to compare: {eu_side['reason']}")
    if us_v is not None and eu_v is not None:
        _fill_pairs(index, eu, us_v, eu_v, date, section, cap, out, notes, line_cap=line_cap)
        notes.append(COMPARE_KEY_NOTE)
    out["note"] = "; ".join(notes)
    return out


def _fill_pairs(index: Index, eu: EuIndex, us_v: Version, eu_v: Version, date: str, section: str | None, cap: int,
                out: dict, notes: list[str], *, line_cap: int | None = ONLY_IN_MAX_LINES) -> None:
    us_files, eu_files = index.section_files(us_v), eu.section_files(eu_v)
    us_by, eu_by = by_code(us_files), {eu_section_code(f): f for f in eu_files}
    plan = []
    used_us, used_eu = set(), set()
    for p in PAIRS:
        us_paths, fallback = _us_files_for(p, us_by)
        eu_path = eu_by.get(p.eu)
        plan.append((p, us_paths, fallback, eu_path))
        used_us.update(us_paths)
        if eu_path:
            used_eu.add(eu_path)
    out["us_only_sections"] = [section_code(f) for f in us_files if f not in used_us]
    out["eu_only_sections"] = [eu_section_code(f) for f in sorted(eu_files, key=_eu_order) if f not in used_eu]
    present = {c: n for c, n in US_ONLY_NOTES.items() if c in out["us_only_sections"]}
    if present:
        out["section_notes"] = present
    if section:
        pair = pair_for(section)
        if pair is None:
            _no_counterpart(index, eu, us_v, eu_v, us_files, eu_files, date, section, cap, out, notes)
            return
        plan = [x for x in plan if x[0] is pair]
    us_texts = index.texts([f"{us_v.commit}:{f}" for _, paths, _, _ in plan for f in paths])
    eu_texts = eu.texts([f"{eu_v.commit}:{e}" for _, _, _, e in plan if e])
    for p, us_paths, fallback, eu_path in plan:
        us_side = eu_part = None
        us_text = eu_text = ""
        if us_paths:
            us_side, us_text = _side(index, us_v, us_paths, us_texts, fallback)
        if eu_path:
            eu_part, eu_text = _side(eu, eu_v, [eu_path], eu_texts)
        us_lines = compare_lines(us_text)
        eu_lines = compare_lines(eu_text, eu_markers=True)
        us_keys, eu_keys = {k for _, k in us_lines}, {k for _, k in eu_lines}
        only_us, only_eu = _only_in(us_lines, eu_keys), _only_in(eu_lines, us_keys)
        row = {"pair": p.name, "us": us_side, "eu": eu_part, "shared_lines": len(us_keys & eu_keys)}
        if section:
            if us_side:
                us_side.update({"text": us_text[:cap], "truncated": len(us_text) > cap,
                                "citation": {"doc_id": us_v.setid, "version": us_v.version,
                                             "section": section_code(us_paths[0]) if len(us_paths) == 1 else None,
                                             "as_of": date}})
                if len(us_paths) > 1:
                    us_side["citation"]["sections"] = [section_code(f) for f in us_paths]
            if eu_part:
                eu_part.update({"text": eu_text[:cap], "truncated": len(eu_text) > cap,
                                "citation": eu.citation(eu_v, eu_path, date)})
            limit = len(only_us) + len(only_eu) if line_cap is None else line_cap
            row["only_in_us"] = {"count": len(only_us), "lines": only_us[:limit], "truncated": len(only_us) > limit}
            row["only_in_eu"] = {"count": len(only_eu), "lines": only_eu[:limit], "truncated": len(only_eu) > limit}
        else:
            row["only_in_us_lines"] = len(only_us)
            row["only_in_eu_lines"] = len(only_eu)
        out["pairs"].append(row)


def _no_counterpart(index: Index, eu: EuIndex, us_v: Version, eu_v: Version, us_files: list[str],
                    eu_files: list[str], date: str, section: str, cap: int, out: dict, notes: list[str]) -> None:
    """A section with no counterpart in PAIRS: that side's text with pair null."""
    s = section.strip().lower()
    side_ix, v, path = None, None, None
    if LOINC.match(s):
        side_ix, v, path = index, us_v, index.pick(us_files, section)
    elif QRD_NUMBER.match(s.removeprefix("smpc-")) or s.startswith("smpc-"):
        side_ix, v, path = eu, eu_v, eu.pick(eu_files, section)
    else:
        try:
            side_ix, v, path = index, us_v, index.pick(us_files, section)
        except LabelError:
            try:
                side_ix, v, path = eu, eu_v, eu.pick(eu_files, section)
            except LabelError:
                raise LabelError(f"section {section!r} names no pair ({', '.join(p.name for p in PAIRS)}) and no "
                                 "section of either label") from None
    text = side_ix.text(v.commit, path)
    side = {**side_ix.entry(path), "lines": len(text.splitlines()), "chars": len(text), "text": text[:cap],
            "truncated": len(text) > cap,
            "citation": {"doc_id": v.setid, "version": v.version, "section": side_ix.code(path), "as_of": date}}
    key = "us" if side_ix is index else "eu"
    out["pairs"].append({"pair": None, "us": side if key == "us" else None, "eu": side if key == "eu" else None})
    notes.append("no counterpart in the pairing table")


# ---------------------------------------------------------------- citations

def check_citations(index: Index, citations: list[dict], eu: EuIndex | None = None) -> dict:
    """Strict, deterministic check of {doc_id, version, section, quote, as_of} citations; items may mix
    US setids and EU ids."""
    if not isinstance(citations, list) or not citations:
        raise LabelError("citations must be a non-empty list of {doc_id, version, section, quote, as_of}")
    cache: dict[str, dict[str, str]] = {}
    results = [_check_one(index, c, n, eu, cache) for n, c in enumerate(citations)]
    passed = sum(1 for r in results if r["exists"] and r["quote_found"] and r["in_force_on_as_of"])
    if eu is None:
        criteria = ("a citation passes when the version exists, the quote is in the cited section of that "
                    "version (case and whitespace ignored) and that version is the one DailyMed showed on as_of")
    else:
        criteria = ("the version exists, the quote is in the cited section of that version (case and whitespace "
                    "ignored) and that version was in force on as_of (US: what DailyMed showed that day; EU: the "
                    "Annex I of the Commission decision in force)")
    return {**index.envelope(), "results": results, "count": len(results), "passed": passed, "criteria": criteria}


def _check_one(index: Index, c: dict, n: int, eu: EuIndex | None = None,
               cache: dict[str, dict[str, str]] | None = None) -> dict:
    out = {"index": n, "exists": False, "quote_found": False, "in_force_on_as_of": False, "problems": []}
    if not isinstance(c, dict):
        out["problems"].append("citation must be an object")
        return out
    doc_id, version, section, quote, as_of = (c.get(k) for k in ("doc_id", "version", "section", "quote", "as_of"))
    try:
        ix = _index_for(index, eu, doc_id)
        setid = ix.resolve_label(str(doc_id or ""))
    except LabelError as err:
        out["problems"].append(str(err))
        return out
    if eu is not None:
        out["jurisdiction"] = ix.jurisdiction
    out["label_id"] = setid
    try:
        vnum = int(version)
    except (TypeError, ValueError):
        out["problems"].append(f"version must be an integer, got {version!r}")
        return out
    by_number = {v.version: v for v in ix.label_versions(setid)}
    v = by_number.get(vnum)
    if v is None:
        out["problems"].append(f"version {vnum} of {setid} is not in the record; versions: "
                               f"{', '.join(str(x) for x in sorted(by_number))}")
    else:
        out["exists"] = True
        out["cited"] = _head(ix, v, None, eu)
        _check_quote(ix, v, section, quote, out, cache)
    if as_of is not None:
        try:
            shown, _ = ix.as_of(setid, str(as_of))
        except LabelError as err:
            out["problems"].append(str(err))
        else:
            out["in_force"] = _head(ix, shown, str(as_of), eu)
            out["in_force_on_as_of"] = v is not None and shown.commit == v.commit
    else:
        out["problems"].append("as_of missing; in_force_on_as_of cannot be checked")
    return out


def _check_quote(index: Index, v: Version, section, quote, out: dict,
                 cache: dict[str, dict[str, str]] | None = None) -> None:
    files = index.section_files(v)
    if not isinstance(quote, str) or not quote.strip():
        out["problems"].append("quote missing")
        return
    needle = normalise(quote)
    key = f"{index.jurisdiction}:{v.commit}:{v.setid}"
    if cache is not None and key in cache:
        normalised = cache[key]
    else:
        texts = index.texts([f"{v.commit}:{f}" for f in files])
        normalised = {f: normalise(texts.get(f"{v.commit}:{f}") or "") for f in files}
        if cache is not None:
            cache[key] = normalised
    found_in = [f for f in files if needle in normalised[f]]
    out["found_in_sections"] = [index.entry(f) for f in found_in]
    if section:
        try:
            path = index.pick(files, str(section))
        except LabelError as err:
            out["problems"].append(str(err))
            return
        out["section"] = index.entry(path)
        out["quote_found"] = path in found_in
        if not out["quote_found"] and found_in:
            out["problems"].append(f"quote is not in {index.code(path)} but appears in "
                                   f"{', '.join(index.code(f) for f in found_in)}")
    else:
        out["quote_found"] = bool(found_in)
        if not found_in:
            out["problems"].append("section missing and quote not found in any section of this version")


# ---------------------------------------------------------------- coverage

def coverage(index: Index, eu: EuIndex | None = None) -> dict:
    """Scope, record window, build date and history type of each corpus."""
    retired = sum(1 for s in index.by_setid if index.status(s)["status"] == "retired")
    inns = {index.folder(s).split("/")[0] for s in index.by_setid}
    us = {"jurisdiction": "us", "source": "DailyMed (U.S. National Library of Medicine)",
          "labels": len(index.by_setid), "versions": len(index.versions), "molecule_folders": len(inns),
          "molecule_files": len(index.molecules()), "record_from": index.record_from, "record_to": index.record_to,
          "build_date": index.build_date, "history_type": "in-force", "retired_labels": retired,
          "head_commit": index.head, "repo": index.repo.name,
          "dailymed_db_published": (index.manifest_entry() or {}).get("dailymed_db_published"),
          "tags": index.tags(),
          "date_rule": "'on date X' is the newest version whose Published-Date <= X: what DailyMed showed that day"}
    if eu is None:
        eu_part = {"jurisdiction": "eu", "built": False, "reason": EU_REASON}
        return {**index.envelope(), "corpora": [us, eu_part], "eu_available": False}
    return {**index.envelope(), "corpora": [us, eu_coverage(eu)], "eu_available": True}


def eu_coverage(eu: EuIndex) -> dict:
    metas = eu.all_metadata()
    by_type: dict[str, list[str]] = {t: [] for t in HISTORY_TYPES}
    products = []
    for setid in sorted(metas):
        st = eu.status(setid)
        t = st["history_type"]
        by_type.setdefault(t, []).append(setid)
        products.append({"label_id": setid, "name": eu.label_title(setid), "molecule": metas[setid].get("molecule"),
                         "history_type": t, "record_from": st["record_from"], "record_to": st["record_to"],
                         "versions": len(eu.label_versions(setid)),
                         "comparable_to_us": metas[setid].get("comparable_to_us")})
    from_products = {t: len(ids) for t, ids in by_type.items()}
    from_commits = {t: 0 for t in HISTORY_TYPES}
    for setid, lv in eu.by_setid.items():
        from_commits[lv[-1].history] = from_commits.get(lv[-1].history, 0) + 1
    out = {"jurisdiction": "eu", "built": True, "source": EU_SOURCE, "repo": eu.repo.name, "head_commit": eu.head,
           "products": len(metas), "molecules": len({p["molecule"] for p in products}),
           "versions": len(eu.versions), "record_from": eu.record_from, "record_to": eu.record_to,
           "build_date": eu.build_date, "converter": eu.converter, "tags": eu.tags(),
           "history_types": from_products, "products_by_history_type": by_type, "products_list": products,
           "date_rule": "'on date X' is the newest commit whose Effective-Date (the Commission decision date) <= X; "
                        "corrected annexes count from their decision date",
           "corrigenda": sum(1 for v in eu.versions if v.corrigendum_of)}
    if from_commits != from_products:
        out["history_types_problem"] = (f"product.json history_type counts {from_products} differ from the History "
                                        f"trailers of each product's newest commit {from_commits}")
    return out
