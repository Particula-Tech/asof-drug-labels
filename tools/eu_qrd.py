#!/usr/bin/env python3
"""EU annex PDF (Union Register Commission decision) to SmPC section Markdown.

Only this module uses a third-party package: PyMuPDF, pinned in
tools/requirements-eu.txt and installed in .venv-eu. Run it with
``.venv-eu/bin/python``. The conversion is split in two stages so the rules can
be tested without PDFs:

- ``extract_items(pdf_bytes)`` (needs PyMuPDF) turns the PDF into visual lines
  and ruled tables with page coordinates.
- ``convert_items(items)`` (standard library only) does everything else.

``convert(pdf_bytes)`` runs both. Changing anything that alters the output
changes the blame, so bump EU_CONVERTER_VERSION with every such change; the
build records it together with the PyMuPDF version.

Rules:
- Annex I runs from the line "ANNEX I" (a footnote mark such as "ANNEX I*"
  is allowed; repeated Annex I titles are dropped) to the first later line "ANNEX II"
  (or "ANNEX III"/"ANNEX IV", or the labelling title if Annex II is missing).
  Annex II and III are not converted.
- A new SmPC starts at every "1. NAME OF THE MEDICINAL PRODUCT" heading
  ("1. Name of Proprietary Medicinal Product" in 1990s annexes). Without an
  "ANNEX I" line, the first such heading starts Annex I only when no Annex II
  or III title comes before it (labelling particulars in Annex III start with
  the same heading). A 1990s third-level heading "4.1.1 Therapeutic
  indications" counts as 4.1; other third-level numbers stay body text.
- An Annex I without any section 1 (e.g. the scientific conclusions of an
  Article 20 referral) gives status "no-smpc" and no sections.
- QRD headings 1, 2, 3, 4.1-4.9, 5.1-5.3, 6.1-6.6, 7, 8, 9, 10 (and 11, 12 for
  radiopharmaceuticals) are recognised by number plus a keyword that holds
  across QRD template versions ("4.6 Pregnancy and lactation" and
  "4.6 Fertility, pregnancy and lactation" are both 4.6), and only in
  increasing order within one SmPC. The file name comes from a fixed canonical
  title per number (``SECTIONS``), never from the printed heading; the first
  line of each file is the printed heading with a normalised number.
- A word-processor list number before a heading ("1.2. 4.1 Therapeutic
  indications") is dropped with an ``auto-number-prefix`` warning.
- A heading title that matches only after rejoining a single letter to the
  next word ("MARKETING A UTHORISATION HOLDER", a kerning gap read as a
  space) is accepted and printed rejoined.
- A heading title with an unbalanced ")" is body text ("4.8 Undesirable
  effects) compared with ..." is a wrapped cross-reference). Once the first
  three headings of an SmPC are bold, a plain (not bold) heading candidate in
  that SmPC is body text.
- Text under the group headings 4, 5 and 6 (normally none) goes to
  ``smpc-4-clinical-particulars`` etc. with a warning. A numbered section
  with no text (8 is often empty in an initial authorisation) is written as a
  file holding only its heading line.
- The additional-monitoring statement ("This medicinal product is subject to
  additional monitoring ..." and its three standard follow-on sentences) goes
  to ``smpc-0-additional-monitoring`` wherever it sits, without the black
  triangle glyph (drawn as an image or a
  symbol, so it is not reliable text). Any other text before the first
  section 1 goes to ``smpc-0-front-matter`` with a warning. These two files
  have no printed heading; their first line is a fixed one ("# 0 Additional
  monitoring", "# 0 Front matter"). The title
  "SUMMARY OF PRODUCT CHARACTERISTICS" is dropped.
- Text is read glyph by glyph: the glyphs of each PDF text line are put in
  visual (x) order, whatever their order in the content stream (Lyrica 2015
  stores the "1.23" of its CLcr formula as ".1" + "23"), and a glyph printed
  twice at the same place is kept once. A space set as a text line of its own
  still separates the words beside it.
- Page numbers and running headers or footers (margin lines repeated on many
  pages) are dropped, and so is rotated text (watermarks, rotated labels),
  which is counted in the stats.
- Lines are joined into paragraphs. A new paragraph (blank line before it)
  starts at a vertical gap larger than 1.45 times the usual line pitch and at a
  bullet. A hard line break (new line, no blank line) is kept when a fully bold
  or italic line meets a plain one, when a line returns left of a bullet's
  text, and when the previous line ended early (the next word, measured with
  the PDF font size, would have fitted on it), unless the previous line has no
  closing punctuation and the next starts in lowercase.
- A line ending in a soft hyphen is joined without it. A line ending in "-"
  before a lowercase letter is joined without the hyphen only when the joined
  word appears elsewhere in the annex and the hyphenated form does not;
  otherwise the hyphen is kept ("first-" + "line" reads "first-line"). Any
  other line ending in a hyphen or en dash after a letter or digit joins the
  next word or number without a space ("KEYNOTE-" + "811", "0.25-" + "20.6",
  "2-" + "year"), unless the annex prints that hyphen-final word followed by a
  space inside a line elsewhere ("HER2- advanced").
- One sentence per line with the sentence rules of tools/spl.py
  (``spl.split_sentences``), except that a sentence never ends after "e.g.",
  "i.e.", "vs.", "approx.", "cf." or "incl." (also after a bracket, which spl
  misses) or after a number followed by a hyphen, and a line ending in one of
  those is never a hard line break. Paragraphs are separated by a blank line; list
  items are consecutive "- " lines, nested ones indented by two spaces.
- Ruled tables found by PyMuPDF become pipe tables when the layout is
  unambiguous: at least two columns and two non-empty rows, no column without
  any text, header included (a sign of misdetected columns), under half of
  the cells empty or else a header row with text in every column and a first
  column filled in every row (a sparse ADR frequency table), no image over it
  and every word inside its box wholly inside one cell. Cell text is built from
  the glyphs inside the cell: a subscript or superscript joins the line it
  overlaps most ("Cmin ↓ 32%", "AUC12h", "HbA1c"). A ruled row whose cells
  stack several groups separated by a blank band at the same height in two or
  more cells ("Common" / "Uncommon" beside their terms) is written as one row
  per group; a cell with a line across the band, or a first-column label in one
  band, stays in the first of them. Tables ruled only horizontally (no
  vertical rules) are found from rules of equal width and the vertical white
  strips between their columns; see ``_rule_tables``. Merged cells are written as empty cells; a table continued on the next
  page with the same column count is joined, a repeated header row is
  dropped and a first row that continues in lowercase is joined to the last
  row before the break. A ruled region that holds a bold QRD section heading
  is a frame around the page text, not a table, and its lines stay ordinary
  text (``frames_ignored`` in the stats). Other ruled regions with two or more columns (figures, unclear
  grids) are written as line text, one visual line per line. Lines with wide
  internal gaps (unruled tables) are also written as line text.
- Several SmPCs in one annex (strengths or forms): each section file holds the
  first SmPC's text, then for every later SmPC only the sentences (table:
  the whole table) not already in the file, compared after lowercasing,
  dropping list markers, collapsing white space, dropping spaces around
  hyphens and a final period, under a line "[<that SmPC's product name>]"
  (the first line of its section 1; " (SmPC n)" is added when an earlier
  SmPC has the same name). When at least one later SmPC adds text to a
  section, every later SmPC gets its marker there, followed by
  "(all sentences already above)" when it adds nothing; when none adds
  anything the file has no marker at all.
- A partly bold line whose tail after sentence punctuation is a later QRD
  heading ("...telithromycin).4.6 Fertility, pregnancy and lactation", the
  heading typeset on the previous line's baseline) is split in two, with a
  ``heading-inside-line`` warning.
- A heading printed without its number ("Fertility, pregnancy and lactation"
  alone) is accepted only when its line is bold, its text is exactly a known
  title of the next expected section (``UNNUMBERED_TITLES``), with an
  ``unnumbered-heading`` warning.
- ``status`` says whether there is text: an annex whose Annex I title is
  followed directly by Annex II, or that holds only Annex II/III, did not
  change the SmPC ("empty-annex-i", "no-annex-i"); the build can skip it
  without a commit.
- Known limits: a footnote mark set as a superscript right after a word stays
  glued to it ("p-valued" for "p-value" + "d"); formula layouts (fraction
  bars, brackets drawn as lines) are written line by line, and glyphs drawn
  as images or missing from the text layer (the "×" and "µ" of the Lyrica
  CLcr formula) are absent.
- No OCR. A PDF without a usable text layer gives no sections and a
  ``no-text-layer`` (or ``unusable-text-layer``) warning; image-only pages
  inside Annex I give a ``page-without-text`` warning each.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import io
import json
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spl import split_sentences  # noqa: E402  (standard library only)

EU_CONVERTER_VERSION = "4"


# ---------------------------------------------------------------- QRD sections

@dataclass(frozen=True)
class QrdSection:
    number: str          # "4.3"
    canonical: str       # fixed title used for the file slug
    pattern: str         # regex matched at the start of the normalised printed title
    group: bool = False  # 4, 5, 6: group headings without their own text

    @property
    def slug(self) -> str:
        return f"smpc-{self.number}-" + re.sub(r"[^a-z0-9]+", "-", self.canonical.lower()).strip("-")


SECTIONS: tuple[QrdSection, ...] = (
    QrdSection("1", "Name of the medicinal product", r"name of (the )?(proprietary )?medicinal product"),
    QrdSection("2", "Qualitative and quantitative composition", r"qualitative and quantitative composition"),
    QrdSection("3", "Pharmaceutical form", r"pharmaceutical forms?\b"),
    QrdSection("4", "Clinical particulars", r"clinical particulars", group=True),
    QrdSection("4.1", "Therapeutic indications", r"(therapeutic )?indications?\b"),
    QrdSection("4.2", "Posology and method of administration", r"(posology|dosage|method of administration)"),
    QrdSection("4.3", "Contraindications", r"contra ?indications?\b"),
    QrdSection("4.4", "Special warnings and precautions for use", r"(special )?warnings?\b"),
    QrdSection("4.5", "Interaction with other medicinal products and other forms of interaction", r"interactions?\b"),
    QrdSection("4.6", "Fertility, pregnancy and lactation", r"(fertility|pregnancy|use (during|in) pregnancy)"),
    QrdSection("4.7", "Effects on ability to drive and use machines", r"(effects? on (the )?ability|ability to drive)"),
    QrdSection("4.8", "Undesirable effects", r"(undesirable effects|adverse (reactions|effects))"),
    QrdSection("4.9", "Overdose", r"overdos"),
    QrdSection("5", "Pharmacological properties", r"pharmacological properties", group=True),
    QrdSection("5.1", "Pharmacodynamic properties", r"pharmacodynamic"),
    QrdSection("5.2", "Pharmacokinetic properties", r"pharmacokinetic"),
    QrdSection("5.3", "Preclinical safety data", r"pre ?clinical safety"),
    QrdSection("6", "Pharmaceutical particulars", r"pharmaceutical particulars", group=True),
    QrdSection("6.1", "List of excipients", r"(list of )?excipients?\b"),
    QrdSection("6.2", "Incompatibilities", r"incompatibilit"),
    QrdSection("6.3", "Shelf life", r"shelf ?life"),
    QrdSection("6.4", "Special precautions for storage", r"(special precautions for storage|storage)"),
    QrdSection("6.5", "Nature and contents of container", r"nature and contents? of"),
    QrdSection("6.6", "Special precautions for disposal and other handling",
               r"(special precautions for (disposal|handling)|instructions for|disposal|handling)"),
    QrdSection("7", "Marketing authorisation holder", r"marketing authori[sz]ation holder"),
    QrdSection("8", "Marketing authorisation numbers",
               r"(marketing authori[sz]ation numbers?|numbers? (s )?in the community register)"),
    QrdSection("9", "Date of first authorisation/renewal of the authorisation",
               r"(date of (the )?first authori[sz]ation|date of authori[sz]ation|renewal of the authori[sz]ation)"),
    QrdSection("10", "Date of revision of the text", r"date of (the )?revision"),
    QrdSection("11", "Dosimetry", r"dosimetry"),
    QrdSection("12", "Instructions for preparation of radiopharmaceuticals", r"instructions for preparation"),
)
BY_NUMBER = {s.number: s for s in SECTIONS}
ORDER = {s.number: i for i, s in enumerate(SECTIONS)}
GROUP_SLUGS = {s.slug for s in SECTIONS if s.group}
# Exact titles (normalised) accepted for a heading printed without its number,
# and only for the next section expected in the SmPC.
UNNUMBERED_TITLES: dict[str, set[str]] = {s.number: {" ".join(re.sub(r"[^0-9a-z]+", " ", s.canonical.lower()).split())}
                                          for s in SECTIONS if not s.group}
for _number, _titles in {
    "4.4": ["special warnings and special precautions for use"],
    "4.6": ["pregnancy and lactation", "use during pregnancy and lactation"],
    "4.7": ["effects on the ability to drive and use machines"],
    "6.6": ["special precautions for disposal", "instructions for use and handling",
            "instructions for use handling and disposal", "instructions for use and handling and disposal"],
    "8": ["marketing authorisation number", "marketing authorisation number s"],
}.items():
    UNNUMBERED_TITLES[_number].update(_titles)
ADDITIONAL_MONITORING = "smpc-0-additional-monitoring"
FRONT_MATTER = "smpc-0-front-matter"
# Fixed heading lines for the two files that have no printed QRD heading.
FIXED_HEADINGS = {ADDITIONAL_MONITORING: "# 0 Additional monitoring", FRONT_MATTER: "# 0 Front matter"}
NOTHING_NEW = "(all sentences already above)"

_HEADING = re.compile(r"^(\d{1,2})(?:\.\s?(\d))?(?:\.(1))?\.?(?:\s+|$)(.*)$")  # group 3: "4.1.1" (QRD 1990s)
_NUMBER_ONLY = re.compile(r"^\d{1,2}(?:\.\d)?\.?$")
_MONITORING = re.compile(r"^\W*this medicinal product is subject to additional monitoring", re.I)
_SPC_TITLE = re.compile(r"^summar(y|ies) of (the )?products? characteristics\W*$", re.I)
_ANNEX_I = re.compile(r"^annex\s+i\s*[*†‡]*$", re.I)  # "ANNEX I*": a footnote mark in older annexes
_ANNEX_END = re.compile(r"^(annex\s+(ii|iii|iv|v)\s*[*†‡]*|labelling and package leaflets?)$", re.I)
_PAGE_NUMBER = re.compile(r"^(page\s+)?\d{1,4}(\s*(/|of)\s*\d{1,4})?$", re.I)

BULLETS = set("•▪■□●○◦‣➢➔⇨✓❖⬥◻❑·▫►-–—o")  # "*" is a footnote mark, not a bullet
_STRONG_BULLETS = set("•▪■□●○◦‣➢➔⇨✓❖⬥◻❑·▫►")


# ---------------------------------------------------------------- items

@dataclass
class Line:
    """One visual line of text on a page (PDF points, origin top left)."""
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page_height: float = 842.0
    bold: float = 0.0       # share of characters in bold
    italic: float = 0.0     # share of characters in italics
    bullet: bool = False    # the text started with a list marker (removed from text)
    marker_x: float = 0.0   # x of the list marker when bullet
    tabular: bool = False   # wide internal gaps: write as line text
    verbatim: bool = False  # inside an ambiguous ruled region: write as line text
    first_word_w: float = 0.0  # measured width of the first word (0: estimate from the average)

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Table:
    """A ruled table accepted as unambiguous; rows of cell text."""
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    rows: list[list[str]]
    page_height: float = 842.0

    @property
    def text(self) -> str:
        return " ".join(c for r in self.rows for c in r)


@dataclass
class Stats:
    pages: int = 0
    annex_pages: list[int] = field(default_factory=list)  # first and last page (1-based) of Annex I
    rotated_lines_dropped: int = 0
    margin_lines_dropped: int = 0
    tables: int = 0
    tables_as_line_text: int = 0
    tabular_lines: int = 0
    frames_ignored: int = 0   # ruled regions holding a bold QRD heading: a page frame, not a table
    rule_tables: int = 0      # tables ruled only horizontally, columns found from vertical white strips
    mupdf_messages: int = 0   # MuPDF errors/warnings while reading (not shown on stderr)


# ---------------------------------------------------------------- helpers

def _collapse(text: str) -> str:
    return " ".join(text.split())


def _norm(text: str) -> str:
    """Comparison form: lowercase, alphanumerics only, single spaces."""
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _sentence_key(line: str) -> str:
    text = line.strip()
    if text.startswith("- "):
        text = text[2:]
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐‑‒–—−]", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)       # "157 - 176" and "157-176" are the same sentence
    return _collapse(text).rstrip(" .;:,")       # so are "... marketed" and "... marketed."


def match_heading(text: str) -> tuple[QrdSection, str] | None:
    """Return (section, printed title) when ``text`` is a QRD heading line."""
    m = _HEADING.match(_collapse(text))
    if not m:
        return None
    if m.group(3) and not m.group(2):
        return None
    number = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
    title = m.group(4).strip()
    sec = BY_NUMBER.get(number)
    if sec is None or not title or len(title) > 160:
        return None
    if title.count(")") > title.count("("):
        return None  # "4.8 Undesirable effects) compared with ...": a wrapped cross-reference
    if not re.match(sec.pattern, _norm(title)):
        # "MARKETING A UTHORISATION HOLDER": a kerning gap read as a space.
        joined = re.sub(r"\b([A-Za-z]) (?=[A-Za-z]{2,})", r"\1", title)
        if joined == title or not re.match(sec.pattern, _norm(joined)):
            return None
        title = joined
    return sec, title


def heading_line(sec: QrdSection, title: str) -> str:
    number = sec.number if "." in sec.number else sec.number + "."
    return f"# {number} {_collapse(title)}"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


# ---------------------------------------------------------------- stage B: pure

def remove_margins(items: list, stats: Stats | None = None) -> list:
    """Drop page numbers and running headers/footers (repeated margin lines)."""
    stats = stats if stats is not None else Stats()
    pages = {it.page for it in items}
    zone_counts: dict[str, set[int]] = {}

    def zone(it: Line) -> str | None:
        if it.y1 < 0.08 * it.page_height:
            return "top"
        if it.y0 > 0.92 * it.page_height:
            return "bottom"
        return None

    for it in items:
        if isinstance(it, Line) and zone(it):
            key = zone(it) + ":" + re.sub(r"\d+", "#", _norm(it.text))
            zone_counts.setdefault(key, set()).add(it.page)
    threshold = max(3, int(0.3 * len(pages)))
    out = []
    for it in items:
        if isinstance(it, Line):
            z = zone(it)
            text = _collapse(it.text)
            if (_PAGE_NUMBER.match(text) and (it.y0 > 0.85 * it.page_height or it.y1 < 0.1 * it.page_height)) or (
                    z and len(zone_counts[z + ":" + re.sub(r"\d+", "#", _norm(it.text))]) >= threshold):
                stats.margin_lines_dropped += 1
                continue
        out.append(it)
    return out


def annex_bounds(items: list) -> tuple[int, int, list[str]]:
    """Indexes [start, end) of the Annex I content and warnings."""
    warnings = []
    start = next((i + 1 for i, it in enumerate(items) if isinstance(it, Line) and _ANNEX_I.match(_collapse(it.text))),
                 None)
    if start is None:
        first = next((i for i, it in enumerate(items) if isinstance(it, Line) and (h := match_heading(it.text))
                      and h[0].number == "1"), None)
        later_annex = next((i for i, it in enumerate(items) if isinstance(it, Line)
                            and _ANNEX_END.match(_collapse(it.text))), None)
        if first is not None and later_annex is not None and later_annex < first:
            first = None  # a "1. NAME OF THE MEDICINAL PRODUCT" in Annex III labelling is not an SmPC
        if first is None:
            later = next((_collapse(it.text) for it in items if isinstance(it, Line)
                          and _ANNEX_END.match(_collapse(it.text))), None)
            detail = f" (the document starts with '{later}')" if later else ""
            return 0, 0, ["no-annex-i: no 'ANNEX I' line and no SmPC section 1 before any later annex" + detail]
        warnings.append("annex-i-marker-missing: started at the first section 1 heading")
        start = first
    end = next((i for i in range(start, len(items)) if isinstance(items[i], Line)
                and _ANNEX_END.match(_collapse(items[i].text))), None)
    if end is None:
        warnings.append("annex-ii-marker-missing: Annex I taken to the end of the document")
        end = len(items)
    return start, end, warnings


@dataclass
class _Layout:
    pitch: float   # usual baseline-to-baseline distance of body lines
    right: float   # right edge of body text


def _layout(items: list) -> _Layout:
    lines = [it for it in items if isinstance(it, Line) and not (it.verbatim or it.tabular)]
    pitches = []
    for a, b in zip(lines, lines[1:]):
        d = b.y0 - a.y0
        if a.page == b.page and 0.6 * a.height < d < 1.6 * a.height:
            pitches.append(round(d, 1))
    if pitches:  # the most common pitch; ties go to the smaller one
        counts = {p: pitches.count(p) for p in set(pitches)}
        pitch = min(counts, key=lambda p: (-counts[p], p))
    else:
        pitch = 1.15 * statistics.median([l.height for l in lines]) if lines else 12.0
    right = _percentile([l.x1 for l in lines], 0.98) if lines else 0.0
    return _Layout(pitch=pitch, right=right)


def _vocabulary(items: list) -> tuple[set[str], set[str], set[str]]:
    """(plain words, hyphenated words, words ending in a hyphen inside a line such as "HER2-") of the annex."""
    plain, hyphenated, dangling = set(), set(), set()
    for it in items:
        if isinstance(it, Line):
            words = it.text.split()
            for w in words[:-1] if words and words[-1].endswith(("-", "\u00ad")) else words:
                w = w.strip(".,;:()[]\"'“”’").lower()
                if not w:
                    continue
                if w.endswith("-") and len(w) > 1:
                    dangling.add(w)
                (hyphenated if "-" in w else plain).add(w)
    return plain, hyphenated, dangling


def _join(left: str, right: str, vocab: tuple[set[str], set[str]]) -> str:
    if not left:
        return right
    if left.endswith("\u00ad"):
        return left[:-1] + right
    if left.endswith("-") and len(left) > 1 and left[-2].isalpha() and right[:1].islower():
        head = re.findall(r"[\w-]+-$", left)
        tail = re.match(r"[\w-]+", right)
        if head and tail:
            joined = (head[0][:-1] + tail.group(0)).lower()
            if joined in vocab[0] and (head[0] + tail.group(0)).lower() not in vocab[1]:
                return left[:-1] + right
        return left + right
    if left[-1:] in _JOIN_DASHES and len(left) > 1 and left[-2].isalnum() and right[:1].isalnum():
        word = re.findall(r"[^\s]+$", left)[0].strip(".,;:()[]\"'“”’").lower()
        if len(vocab) > 2 and word in vocab[2]:
            return left + " " + right  # "HER2-" + "advanced": the annex prints "HER2- advanced" elsewhere
        return left + right  # "Kaplan-" + "Meier", "KEYNOTE-" + "811", "0.25-" + "20.6", "2-" + "year"
    return left + " " + right


_JOIN_DASHES = set("-‐‑–")


def _styled(x: float) -> bool:
    return x >= 0.9


def _break(prev: Line, line: Line, lay: _Layout, in_item: bool = False) -> str | None:
    """How ``line`` follows ``prev``: None (same paragraph), "line" (hard line
    break, written without a blank line) or "para" (new paragraph)."""
    if line.bullet:
        return "para"
    soft_end = prev.text.endswith(("-", "\u00ad", ",", "(", "/"))
    if prev.page == line.page and line.y0 - prev.y0 > 1.45 * lay.pitch:
        return "para"
    return "line" if _line_break(prev, line, lay, in_item, soft_end) else None


def _line_break(prev: Line, line: Line, lay: _Layout, in_item: bool, soft_end: bool) -> bool:
    if _ends_open(prev.text):
        return False  # "(squamous vs." + "non-squamous)", "157-" + "176 and 3-3.5 ml/min"
    starts_upper = bool(re.match(r"[A-Z0-9(\[\"“]", line.text))
    if not soft_end and starts_upper and (
            _styled(prev.bold) != _styled(line.bold) or _styled(prev.italic) != _styled(line.italic)):
        return True
    if in_item and line.x0 < prev.x0 - 5:
        return True
    open_end = not re.search(r"[.:;!?]['\"”’)\]]*$", prev.text)
    if open_end and line.text[:1].islower():
        return False
    first_word = line.text.split(" ", 1)[0] if line.text else ""
    char_w = (prev.x1 - prev.x0) / max(1, len(prev.text))
    if open_end and line.text[:1].isdigit() and prev.text[-1:].islower() and prev.x1 > lay.right - 15 * char_w:
        return False
    # The previous line ended early: the next word would have fitted on it.
    # A measured word width allows half a character of slack; a width
    # estimated from the average character width allows two.
    if line.first_word_w:
        need, slack = line.first_word_w + 0.5 * char_w, 0.5 * char_w
    else:
        need, slack = char_w * (len(first_word) + 1), 2 * char_w
    return bool(lay.right) and prev.x1 + need < lay.right - slack


# Tokens that end in a period without ending a sentence in EU annexes. spl.split_sentences knows most
# of them but misses them after an opening bracket ("(i.e. EMEND ...)"), and the line-break rule must
# not end a line there either.
EU_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "approx.", "cf.", "incl.")
_OPEN_END = re.compile(r"(?:(?:^|[\s(\[/])(?:" + "|".join(re.escape(a) for a in EU_ABBREVIATIONS)
                       + r")|\d ?[-‐‑–])$", re.I)


def _ends_open(text: str) -> bool:
    """True when a line or sentence ends in a non-terminal abbreviation or a number followed by a hyphen."""
    return bool(_OPEN_END.search(text.rstrip()))


def split_eu_sentences(text: str) -> list[str]:
    """spl.split_sentences, then sentences that end in a non-terminal abbreviation or "<digit>-" are joined
    to the next one."""
    out: list[str] = []
    for s in split_sentences(text):
        if out and _ends_open(out[-1]):
            out[-1] += " " + s
        else:
            out.append(s)
    return out


@dataclass
class _Block:
    kind: str                 # "para", "list", "lines", "table"
    lines: list[str]          # rendered output lines
    units: list[list[str]]    # merge units (each a list of output lines)


def _render_table(rows: list[list[str]]) -> list[str]:
    width = max(len(r) for r in rows)
    rows = [[c.replace("|", "\\|") for c in r] + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    return out + ["| " + " | ".join(r) + " |" for r in rows[1:]]


def render_flow(items: list, lay: _Layout, vocab: tuple[set[str], set[str]]) -> list[_Block]:
    """Turn a section's lines and tables into Markdown blocks."""
    blocks: list[_Block] = []
    para: list[Line] = []
    verbatim: list[str] = []
    tight = False

    def flush_para() -> None:
        if not para:
            return
        text = ""
        for ln in para:
            text = _join(text, _collapse(ln.text), vocab)
        sentences = split_eu_sentences(text)
        if not sentences:
            para.clear()
            return
        first = para[0]
        if first.bullet:
            level = 0
            prev_list = blocks[-1] if blocks and blocks[-1].kind == "list" else None
            if prev_list is not None:
                markers = getattr(prev_list, "_markers")
                while markers and first.marker_x < markers[-1] - 4:
                    markers.pop()
                if markers and first.marker_x > markers[-1] + 4:
                    markers.append(first.marker_x)
                elif not markers:
                    markers.append(first.marker_x)
                level = len(markers) - 1
            pad = "  " * level
            item = [pad + "- " + sentences[0]] + [pad + "  " + s for s in sentences[1:]]
            if prev_list is None:
                prev_list = _Block("list", [], [])
                setattr(prev_list, "_markers", [first.marker_x])
                blocks.append(prev_list)
            prev_list.lines.extend(item)
            prev_list.units.extend([[ln] for ln in item])
        elif tight and blocks and blocks[-1].kind == "para":
            blocks[-1].lines.extend(sentences)
            blocks[-1].units.extend([[s] for s in sentences])
        else:
            blocks.append(_Block("para", sentences, [[s] for s in sentences]))
        para.clear()

    def flush_verbatim() -> None:
        if verbatim:
            blocks.append(_Block("lines", list(verbatim), [[v] for v in verbatim]))
            verbatim.clear()

    pending_table: Table | None = None

    def flush_table() -> None:
        nonlocal pending_table
        if pending_table is not None:
            lines = _render_table(pending_table.rows)
            blocks.append(_Block("table", lines, [lines]))
            pending_table = None

    for it in items:
        if isinstance(it, Table):
            flush_para()
            flush_verbatim()
            if (pending_table is not None and it.page == pending_table.page + 1
                    and len(it.rows[0]) == len(pending_table.rows[0])
                    and pending_table.y1 > 0.75 * pending_table.page_height and it.y0 < 0.25 * it.page_height):
                rows = [list(r) for r in (it.rows[1:] if it.rows[0] == pending_table.rows[0] else it.rows)]
                prev_rows = [list(r) for r in pending_table.rows]
                # A row broken by the page break continues in lowercase.
                if rows and any(rows[0]) and all(c[:1].islower() for c in rows[0] if c):
                    last = prev_rows[-1]
                    prev_rows[-1] = [_join(a, b, vocab) if b else a for a, b in zip(last, rows[0])] + last[len(rows[0]):]
                    rows = rows[1:]
                pending_table = Table(it.page, it.x0, it.y0, it.x1, it.y1, prev_rows + rows, it.page_height)
                continue
            else:
                flush_table()
                pending_table = it
            continue
        flush_table()
        if it.verbatim or it.tabular:
            flush_para()
            text = _collapse(("- " if it.bullet else "") + it.text)
            if text:
                verbatim.append(text)
            continue
        flush_verbatim()
        brk = _break(para[-1], it, lay, in_item=para[0].bullet) if para else "para"
        if brk:
            flush_para()
            tight = brk == "line"
        para.append(it)
    flush_para()
    flush_verbatim()
    flush_table()
    return blocks


@dataclass
class _SmPC:
    sections: dict[str, list] = field(default_factory=dict)   # slug -> items
    headings: dict[str, str] = field(default_factory=dict)    # slug -> heading line
    start_page: int = 0
    end_page: int = 0


def split_smpcs(items: list, warnings: list[str]) -> tuple[list, list[_SmPC]]:
    """Split Annex I items into front matter and SmPCs with their sections."""
    items = list(items)
    front: list = []
    smpcs: list[_SmPC] = []
    current: _SmPC | None = None
    slug: str | None = None
    last_order = -1
    heading_bold: list[bool] = []   # boldness of the accepted headings of the current SmPC
    i = 0
    while i < len(items):
        it = items[i]
        head = None
        consumed = 1
        auto_number = False
        if (isinstance(it, Line) and re.fullmatch(r"\d+(\.\d+)+\.?", _collapse(it.text)) and i + 1 < len(items)
                and isinstance(items[i + 1], Line) and (m := _AUTO_NUMBER.match(_collapse(items[i + 1].text)))
                and match_heading(m.group(2)) is not None):
            i += 1  # a bare list number ("1.1.") left over from the same word-processor numbering
            continue
        if isinstance(it, Line) and not (it.verbatim or it.bullet):
            head = match_heading(it.text)
            m = _AUTO_NUMBER.match(_collapse(it.text))
            if head is None and m and (head := match_heading(m.group(2))) is not None:
                auto_number = True
                warnings.append(f"auto-number-prefix: '{m.group(1)}' dropped before heading '{m.group(2)[:60]}'")
            if head is None and _NUMBER_ONLY.match(_collapse(it.text)) and i + 1 < len(items) \
                    and isinstance(items[i + 1], Line):
                head = match_heading(_collapse(it.text) + " " + _collapse(items[i + 1].text))
                consumed = 2 if head else 1
            if head is None and current is not None and 0.2 <= it.bold < 0.9:
                split = _split_glued_heading(it, last_order)
                if split is not None:
                    items[i:i + 1] = split
                    warnings.append(f"heading-inside-line: SmPC {len(smpcs)} heading '{split[1].text}' printed on "
                                    "the same line as the text before it; split")
                    it, head = items[i], None
            if head is None and current is not None and _styled(it.bold):
                nxt = next((s for s in SECTIONS if ORDER[s.number] > last_order and not s.group), None)
                if nxt is not None and _norm(it.text) in UNNUMBERED_TITLES[nxt.number]:
                    head = (nxt, _collapse(it.text))
                    warnings.append(f"unnumbered-heading: SmPC {len(smpcs)} section {nxt.number} printed as "
                                    f"'{_collapse(it.text)}' without its number")
        if (head is not None and not auto_number and sec_is_plain(items, i, consumed) and len(heading_bold) >= 3 and all(heading_bold)
                and head[0].number != "1"):
            head = None  # this SmPC prints its headings in bold; a plain line is body text
        if head is not None:
            sec, title = head
            if sec.number == "1":
                heading_bold = []
                if current is not None:
                    current.end_page = items[i - 1].page if i else it.page
                current = _SmPC(start_page=it.page)
                smpcs.append(current)
                last_order = -1
            if current is not None and ORDER[sec.number] > last_order:
                # A heading title can wrap onto a second bold line.
                last = items[i + consumed - 1]
                while (i + consumed < len(items) and isinstance(items[i + consumed], Line)
                       and _styled(last.bold) and _styled(items[i + consumed].bold)
                       and items[i + consumed].page == last.page
                       and items[i + consumed].y0 - last.y0 < 1.3 * max(last.height, 1)
                       and match_heading(items[i + consumed].text) is None
                       and not re.match(r"\d+(\.\d+)*\.?(\s|$)", _collapse(items[i + consumed].text))):
                    title += " " + _collapse(items[i + consumed].text)
                    last = items[i + consumed]
                    consumed += 1
                expected = [s for s in SECTIONS if last_order < ORDER[s.number] < ORDER[sec.number]
                            and not s.group and int(s.number.split(".")[0]) <= 10]
                if last_order >= 0 and expected:
                    warnings.append(f"missing-section: SmPC {len(smpcs)} has no heading "
                                    + ", ".join(s.number for s in expected) + f" before {sec.number}")
                last_order = ORDER[sec.number]
                heading_bold.append(not sec_is_plain(items, i, consumed))
                slug = sec.slug
                current.sections.setdefault(slug, [])
                current.headings.setdefault(slug, heading_line(sec, title))
                i += consumed
                continue
        if current is None:
            front.append(it)
        else:
            current.sections[slug].append(it)
        i += 1
    if current is not None and items:
        current.end_page = items[-1].page
    return front, smpcs


def sec_is_plain(items: list, i: int, consumed: int) -> bool:
    """True when none of the lines of a heading candidate is mostly bold."""
    return all(not (isinstance(it, Line) and it.bold >= 0.5) for it in items[i:i + consumed])


_AUTO_NUMBER = re.compile(r"^(\d+(?:\.\d+)+\.?)\s+(\d.*)$")  # "1.2. 4.1 Therapeutic indications"
_GLUE_POINT = re.compile(r"(?<=[.:;)\]])\s*(?=\d)")


def _split_glued_heading(line: Line, last_order: int) -> list[Line] | None:
    """A partly bold line "<end of a sentence>.4.6 Fertility, pregnancy and
    lactation": return it as two lines (text, then the heading) when the tail
    is a QRD heading later than the current section."""
    text = _collapse(line.text)
    for m in _GLUE_POINT.finditer(text):
        head = match_heading(text[m.end():])
        if head is not None and ORDER[head[0].number] > last_order and not head[0].group:
            share = m.start() / max(1, len(text))
            x_split = line.x0 + share * (line.x1 - line.x0)
            body = dataclasses.replace(line, text=text[:m.start()], x1=x_split, bold=0.0)
            heading = dataclasses.replace(line, text=text[m.end():], x0=x_split, bold=1.0, italic=0.0)
            return [body, heading]
    return None


_MONITORING_MORE = re.compile(r"^(this will allow quick identification|healthcare professionals are asked to report"
                              r"|see section 4\.8 for how to report)", re.I)


def take_monitoring(block: _Block) -> list[str]:
    """Remove the additional-monitoring statement from a paragraph block and
    return its sentences (empty when the block does not hold it)."""
    if block.kind != "para":
        return []
    start = next((i for i, s in enumerate(block.lines) if _MONITORING.match(s)), None)
    if start is None:
        return []
    end = start + 1
    while end < len(block.lines) and _MONITORING_MORE.match(block.lines[end]):
        end += 1
    taken = block.lines[start:end]
    taken[0] = re.sub(r"^[^0-9A-Za-z]+", "", taken[0])  # the black triangle, if it came through as text
    del block.lines[start:end]
    block.units = [[ln] for ln in block.lines]
    return taken


def convert_items(items: list, stats: Stats | None = None) -> dict:
    """Stage B: items in reading order (all pages) -> sections. Standard library only."""
    stats = stats if stats is not None else Stats()
    warnings: list[str] = []
    items = remove_margins(items, stats)
    start, end, w = annex_bounds(items)
    warnings += w
    annex = [it for it in items[start:end]
             if not (isinstance(it, Line) and (_SPC_TITLE.match(_collapse(it.text))
                                               or _ANNEX_I.match(_collapse(it.text))))]
    if annex:
        stats.annex_pages = [annex[0].page + 1, annex[-1].page + 1]
    lay = _layout(annex)
    vocab = _vocabulary(annex)
    front, smpcs = split_smpcs(annex, warnings)
    if start == end == 0:
        status = "no-annex-i"
    elif not annex:
        status = "empty-annex-i"
        warnings.append("empty-annex-i: the 'ANNEX I' title is followed directly by the next annex "
                        "(this decision did not change the SmPC)")
    elif not smpcs:
        status = "no-smpc"
        first = next((_collapse(it.text) for it in annex if isinstance(it, Line)), "")
        warnings.append("no-smpc: no '1. NAME OF THE MEDICINAL PRODUCT' heading inside Annex I, which starts with "
                        f"'{first[:100]}'")
        return {"status": status, "smpcs": [], "sections": {}, "warnings": warnings}
    else:
        status = "converted"

    rendered: list[dict[str, list[_Block]]] = []
    monitoring: list[_Block] = []
    for k, smpc in enumerate([None] + smpcs):
        sections = {FRONT_MATTER: front} if smpc is None else smpc.sections
        out: dict[str, list[_Block]] = {}
        for slug, sec_items in sections.items():
            blocks = []
            for b in render_flow(sec_items, lay, vocab):
                taken = take_monitoring(b)
                if taken:
                    monitoring.append(_Block("para", taken, [[t] for t in taken]))
                if b.lines:
                    blocks.append(b)
            out[slug] = blocks
        if smpc is None:
            if out[FRONT_MATTER]:
                warnings.append("front-matter: text before the first section 1 kept in " + FRONT_MATTER)
            else:
                del out[FRONT_MATTER]
        rendered.append(out)
    for k, smpc in enumerate(smpcs):
        for group in sorted(GROUP_SLUGS):
            if rendered[k + 1].get(group):
                warnings.append(f"group-heading-text: SmPC {k + 1} has text under {group}")

    names = []
    for k, smpc in enumerate(smpcs):
        name_blocks = rendered[k + 1].get(BY_NUMBER["1"].slug) or []
        name = name_blocks[0].lines[0].lstrip("- ") if name_blocks and name_blocks[0].lines else f"SmPC {k + 1}"
        if name in names:
            name += f" (SmPC {k + 1})"   # two SmPCs printed with the same name (Insuman OptiPen and OptiClik)
        names.append(name)

    # Merge: first SmPC in full, later SmPCs only with units not yet in the file.
    files: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    headings: dict[str, str] = {}
    slugs_in_order: list[str] = []

    def add_slug(slug: str) -> None:
        if slug not in files:
            files[slug] = []
            seen[slug] = set()
            slugs_in_order.append(slug)

    if monitoring:
        add_slug(ADDITIONAL_MONITORING)
        for b in monitoring:
            new = [u for u in b.units if "\n".join(_sentence_key(x) for x in u) not in seen[ADDITIONAL_MONITORING]]
            for u in new:
                seen[ADDITIONAL_MONITORING].add("\n".join(_sentence_key(x) for x in u))
            if new:
                files[ADDITIONAL_MONITORING].append("\n".join(x for u in new for x in u))
    later: dict[str, list[tuple[int, list[str]]]] = {}   # slug -> (SmPC index, new chunks) of SmPC 2 onwards
    for k, out in enumerate(rendered):
        smpc = smpcs[k - 1] if k else None
        for slug, blocks in out.items():
            if smpc is not None:
                headings.setdefault(slug, smpc.headings.get(slug, ""))
            add_slug(slug)
            chunks: list[str] = []
            for b in blocks:
                keep = []
                for u in b.units:
                    key = "\n".join(_sentence_key(x) for x in u)
                    if k <= 1 or key not in seen[slug]:
                        keep.append(u)
                    seen[slug].add(key)
                if keep:
                    chunks.append("\n".join(x for u in keep for x in u))
            if k > 1:
                later.setdefault(slug, []).append((k, chunks))
            else:
                files[slug].extend(chunks)
    # A later SmPC that adds nothing to a section gets a marker with NOTHING_NEW when some other later
    # SmPC does add text there, so every strength is accounted for; when no later SmPC adds anything the
    # section carries no marker at all.
    for slug, parts in later.items():
        if not any(chunks for _, chunks in parts):
            continue
        for k, chunks in parts:
            files[slug].append(f"[{names[k - 1]}]")
            files[slug].extend(chunks or [NOTHING_NEW])

    sections = {}
    order = {s.slug: ORDER[s.number] for s in SECTIONS}
    order[ADDITIONAL_MONITORING] = -2
    order[FRONT_MATTER] = -1
    for slug in sorted(slugs_in_order, key=lambda s: order.get(s, 99)):
        body = [c for c in files[slug] if c.strip()]
        if not body and slug in GROUP_SLUGS:
            continue  # group headings 4, 5, 6 normally carry no text of their own
        heading = headings.get(slug) or FIXED_HEADINGS.get(slug)
        if heading:
            body = [heading] + body
        if body:
            sections[slug] = "\n\n".join(body) + "\n"
    smpc_info = [{"name": names[k], "start_page": s.start_page + 1, "end_page": s.end_page + 1,
                  "sections": [sl for sl in s.sections]} for k, s in enumerate(smpcs)]
    if len(smpcs) > 1:
        warnings.append(f"multi-smpc: {len(smpcs)} SmPCs merged (later ones only add sentences not already present)")
    return {"status": status, "smpcs": smpc_info, "sections": sections, "warnings": warnings}


# ---------------------------------------------------------------- stage A: PDF

_SYMBOL = dict(zip(range(0x41, 0x5B), "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"))
_SYMBOL.update(zip(range(0x61, 0x7B), "αβχδεφγηιϕκλμνοπθρστυϖωξψζ"))
_SYMBOL.update({0x20: " ", 0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅", 0x5C: "∴",
                0x5E: "⊥", 0x60: "‾", 0x7E: "∼", 0xA0: "€", 0xA2: "′", 0xA3: "≤", 0xA4: "⁄", 0xA5: "∞",
                0xA6: "ƒ", 0xAB: "↔", 0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓", 0xB0: "°", 0xB1: "±",
                0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂", 0xB7: "•", 0xB8: "÷", 0xB9: "≠",
                0xBA: "≡", 0xBB: "≈", 0xBC: "…", 0xD7: "⋅", 0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔",
                0xDE: "⇒", 0xE1: "〈", 0xE5: "∑", 0xF1: "〉", 0xF2: "∫"})
for _c in "!#%&()+,./0123456789:;<=>?[]_{|}":
    _SYMBOL[ord(_c)] = _c
_WINGDINGS = {0x6C: "●", 0x6E: "■", 0x6F: "□", 0x71: "❑", 0x76: "❖", 0x77: "⬥", 0x9F: "•", 0xA7: "▪",
              0xA8: "◻", 0xD8: "➢", 0xE8: "➔", 0xF0: "⇨", 0xFC: "✓", 0xFE: "☑"}


def _map_char(ch: str, font: str) -> str:
    cp = ord(ch)
    if 0xF020 <= cp <= 0xF0FF:
        low = cp - 0xF000
        if "wingding" in font.lower():
            return _WINGDINGS.get(low, "•")
        return _SYMBOL.get(low, ch)
    if ch in "     \t":
        return " "
    return ch


def pymupdf_version() -> str:
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf  # type: ignore[no-redef]
    return pymupdf.VersionBind


def _import_pymupdf():
    try:
        import pymupdf
    except ImportError as err:  # pragma: no cover - environment problem
        raise RuntimeError("PyMuPDF is not installed; run with .venv-eu/bin/python") from err
    return pymupdf


@dataclass
class Char:
    """One glyph (PDF points, origin top left); ``c`` is already mapped (Symbol, Wingdings, ligatures)."""
    x0: float
    y0: float
    x1: float
    y1: float
    c: str
    size: float = 10.0
    bold: bool = False
    italic: bool = False
    seg: int = 0          # the PDF text line it came from

    @property
    def blank(self) -> bool:
        return not self.c.strip()


def order_chars(chars: list[Char]) -> list[Char]:
    """The glyphs of one PDF line in visual order (left to right), whatever their order in the content
    stream: Lyrica 2015 writes ".1" + "23" for the "1.23" printed in its CLcr formula. A glyph without width
    (a combining mark) stays after the glyph before it in the stream. A glyph printed twice at the same place
    (fake bold) is kept once."""
    keyed = []
    last_x = -1e9
    for i, ch in enumerate(chars):
        if ch.x1 - ch.x0 < 0.5 and ch.c.strip():
            keyed.append(((last_x, 1, i), ch))
        else:
            last_x = ch.x0
            keyed.append(((ch.x0, 0, i), ch))
    out: list[Char] = []
    for _, ch in sorted(keyed, key=lambda kc: kc[0]):
        if ch.c.strip() and any(o.c == ch.c and abs(o.x0 - ch.x0) < 0.6 and abs(o.y0 - ch.y0) < 0.6
                                for o in out[-3:]):
            continue
        out.append(ch)
    return out


def chars_text(chars: list[Char], size: float) -> str:
    """Text of glyphs on one visual line, in x order. A blank glyph is a space; so is a gap wider than 0.15 of
    the font size between glyphs of different PDF text lines (within one text line the PDF's own spaces
    are trusted). A subscript or superscript set tight against its base word is joined to it."""
    text, prev, pending = "", None, False
    for ch in chars:
        if ch.blank:
            pending = prev is not None
            continue
        if prev is not None and (pending or (ch.seg != prev.seg and ch.x0 - prev.x1 > 0.15 * size)) \
                and not text.endswith(" "):
            text += " "
        text += ch.c
        prev, pending = ch, False
    return text


def char_lines(chars: list[Char]) -> list[tuple[float, float, str]]:
    """Group the glyphs of a table cell into visual lines (y0, y1, text), top to bottom. Glyphs of the usual
    size form the lines; smaller ones (subscripts, superscripts, footnote marks) join the line they overlap
    most, so "C" + "min" reads "Cmin" on its own line instead of "min" on the next."""
    marks = [c for c in chars if not c.blank]
    if not marks:
        return []
    usual = statistics.median(c.size for c in marks)
    base = sorted((c for c in marks if c.size >= 0.8 * usual), key=lambda c: ((c.y0 + c.y1) / 2, c.x0))
    small = [c for c in chars if c.blank or c.size < 0.8 * usual]
    lines: list[list] = []   # [y0, y1, [chars]]

    def overlap(a0, a1, b0, b1) -> float:
        return min(a1, b1) - max(a0, b0)

    for ch in base:
        h = ch.y1 - ch.y0
        best = max(lines[-4:], key=lambda ln: overlap(ln[0], ln[1], ch.y0, ch.y1), default=None)
        if best is not None and overlap(best[0], best[1], ch.y0, ch.y1) > 0.5 * min(h, best[1] - best[0]):
            best[0], best[1] = min(best[0], ch.y0), max(best[1], ch.y1)
            best[2].append(ch)
        else:
            lines.append([ch.y0, ch.y1, [ch]])
    for ch in small:
        best = max(lines, key=lambda ln: overlap(ln[0], ln[1], ch.y0, ch.y1), default=None)
        if best is not None and overlap(best[0], best[1], ch.y0, ch.y1) > 0:
            best[2].append(ch)
        elif not ch.blank:
            lines.append([ch.y0, ch.y1, [ch]])
    lines.sort(key=lambda ln: ln[0])
    out = []
    for y0, y1, chs in lines:
        text = _collapse(chars_text(sorted(chs, key=lambda c: c.x0), usual))
        if text:
            out.append((y0, y1, text))
    return out


def join_cell_lines(texts: list[str]) -> str:
    """The lines of a cell as one text: a line ending in a soft hyphen joins without it, one ending in a
    hyphen after a letter or digit joins without a space ("phospho-" + "kinase", "KEYNOTE-" + "811")."""
    out = ""
    for part in texts:
        part = _collapse(part)
        if not part:
            continue
        if out.endswith("­"):
            out = out[:-1] + part
        elif out[-1:] in _JOIN_DASHES and len(out) > 1 and out[-2].isalnum() and part[:1].isalnum():
            out += part
        elif out:
            out += " " + part
        else:
            out = part
    return out


def split_row(cells: list[list[tuple[float, float, str]] | None]) -> list[list[str]]:
    """One ruled table row as one or more output rows.

    ADR tables stack several frequencies in one ruled cell, each with its terms beside it, separated by a
    blank band ("Common" / "Uncommon" next to "vertigo" / "deafness, tinnitus"). A band of at least 0.75 of
    the usual line height that is empty in two or more cells at the same height splits the row there. Only
    the first cell may have a line across a split, and only when it is one block of text (the organ class
    label): it then stays in the first sub-row, as does a first-column label that sits in one band. Any
    other cell with a line across a split (a header "Herceptin" / "N=172" beside a wrapped one) means the
    row is not stacked, and it is written as one row."""
    joined = [join_cell_lines([t for _, _, t in c]) if c else "" for c in cells]
    heights = [y1 - y0 for c in cells if c for y0, y1, _ in c]
    if len(heights) < 4:
        return [joined]
    gap_min = 0.75 * statistics.median(heights)
    gaps: list[tuple[float, float, int]] = []
    for i, c in enumerate(cells):
        for a, b in zip(c or [], (c or [])[1:]):
            if b[0] - a[1] >= gap_min:
                gaps.append((a[1], b[0], i))
    gaps.sort()
    cuts: list[float] = []
    k = 0
    while k < len(gaps):
        lo, hi, members = gaps[k][0], gaps[k][1], {gaps[k][2]}
        k += 1
        while k < len(gaps) and gaps[k][0] < hi:
            lo, hi = max(lo, gaps[k][0]), min(hi, gaps[k][1])
            members.add(gaps[k][2])
            k += 1
        if len(members) >= 2 and lo < hi:
            cuts.append((lo + hi) / 2)
    if not cuts:
        return [joined]
    rows = [[""] * len(cells) for _ in range(len(cuts) + 1)]
    split_cells = 0
    for i, c in enumerate(cells):
        if not c:
            continue
        if any(y0 < cut < y1 for y0, y1, _ in c for cut in cuts):
            if i == 0 and not any(b[0] - a[1] >= gap_min for a, b in zip(c, c[1:])):
                rows[0][i] = joined[i]   # the organ class label beside the stacked groups
                continue
            return [joined]   # text of another cell runs across the band: not a stacked row
        parts: dict[int, list[str]] = {}
        for y0, y1, t in c:
            parts.setdefault(sum(1 for cut in cuts if cut < y0), []).append(t)
        if i == 0 and len(parts) == 1:
            rows[0][i] = joined[i]
            continue
        for band, texts in parts.items():
            rows[band][i] = join_cell_lines(texts)
        split_cells += len(parts) >= 2
    if split_cells < 2:
        return [joined]
    return [r for r in rows if any(r)]


def table_is_clear(rows: list[list[str]], merged: int = 0) -> bool:
    """A ruled grid is written as a pipe table when no column is empty in every row (header included) and
    under half of the cells are empty, not counting the ``merged`` cells covered by a merged cell (written
    as empty cells). A sparser grid is accepted when its header row has text in every column and every
    other row names its first column: an ADR frequency table (Xeljanz 2022) has many empty cells by nature."""
    cells = [c for r in rows for c in r]
    if len(rows) < 2 or not cells or has_empty_column(rows):
        return False
    empty = sum(1 for c in cells if not c) - merged
    if empty * 2 < len(cells) - merged:
        return True
    return all(rows[0]) and all(r[0] for r in rows[1:])


def rule_columns(spans: list[tuple[float, float]], x0: float, x1: float, min_gap: float
                 ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(columns, strips) of a region from the x extents of its glyphs: runs of ink separated by vertical
    strips at least ``min_gap`` wide that no glyph of the region touches. The outer column bounds are
    stretched to x0 and x1 and inner ones meet in the middle of each strip."""
    ink = sorted((max(a, x0), min(b, x1)) for a, b in spans if b > x0 and a < x1)
    runs: list[list[float]] = []
    for a, b in ink:
        if runs and a - runs[-1][1] < min_gap:
            runs[-1][1] = max(runs[-1][1], b)
        else:
            runs.append([a, b])
    if len(runs) < 2:
        return [], []
    bounds = [x0] + [(runs[i][1] + runs[i + 1][0]) / 2 for i in range(len(runs) - 1)] + [x1]
    strips = [(runs[i][1], runs[i + 1][0]) for i in range(len(runs) - 1)]
    return list(zip(bounds, bounds[1:])), strips


def _is_marker_column(cells: list[str]) -> bool:
    filled = [c for c in cells if c]
    return bool(filled) and all(c in BULLETS or re.fullmatch(r"\(?[0-9a-zA-Z]{1,3}[.)]", c) for c in filled)


# ---------------------------------------------------------------- stage A helpers

def _page_chars(page, stats: Stats) -> tuple[list[list], list[tuple[float, float, float, float]], list[Char]]:
    """(segments, blank segments, glyphs) of a page. A segment is one horizontal PyMuPDF line:
    [y0, y1, x0, x1, text, size, n_bold, n_italic, n_chars]. Blank segments (a space set as its own line)
    are kept apart as (x0, y0, x1, y1): they separate words but do not make lines."""
    pm = _import_pymupdf()
    flags = pm.TEXT_PRESERVE_WHITESPACE | pm.TEXT_MEDIABOX_CLIP
    segments, blanks, glyphs = [], [], []
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for ln in block["lines"]:
            dx, dy = ln["dir"]
            if abs(dy) > 0.01 or dx < 0:
                if any(ch["c"].strip() for sp in ln["spans"] for ch in sp["chars"]):
                    stats.rotated_lines_dropped += 1
                continue
            chars: list[Char] = []
            for sp in ln["spans"]:
                font = sp["font"].lower()
                bold = bool(sp["flags"] & 16 or "bold" in font or font.endswith(("_bo", ",bd")))
                italic = bool(sp["flags"] & 2 or "ital" in font or "oblique" in font)
                for ch in sp["chars"]:
                    c = _map_char(ch["c"], sp["font"]).replace("ﬁ", "fi").replace("ﬂ", "fl")
                    bx0, by0, bx1, by1 = ch["bbox"]
                    chars.append(Char(bx0, by0, bx1, by1, c, sp["size"], bold, italic, len(segments) + len(blanks)))
            if not any(not c.blank for c in chars):
                if chars:
                    x0, y0, x1, y1 = ln["bbox"]
                    blanks.append((x0, y0, x1, y1))
                continue
            chars = order_chars(chars)
            glyphs.extend(chars)
            text = "".join(c.c for c in chars)
            ink = [c for c in chars if not c.blank]
            nc = sum(len(c.c) for c in ink)
            nb = sum(len(c.c) for c in ink if c.bold)
            ni = sum(len(c.c) for c in ink if c.italic)
            x0, y0, x1, y1 = ln["bbox"]
            segments.append([y0, y1, x0, x1, text, max(c.size for c in ink), nb, ni, nc])
    return segments, blanks, glyphs


def _page_lines(page, pno: int, stats: Stats) -> tuple[list[Line], list[Char]]:
    pm = _import_pymupdf()
    height = page.rect.height
    segments, blanks, glyphs = _page_chars(page, stats)
    segments.sort(key=lambda s: (round(s[0], 1), s[2]))
    clusters: list[list] = []
    for seg in segments:
        h = seg[1] - seg[0]
        for cl in reversed(clusters[-4:]):
            cy0 = min(s[0] for s in cl)
            cy1 = max(s[1] for s in cl)
            overlap = min(cy1, seg[1]) - max(cy0, seg[0])
            if overlap > 0.5 * min(h, cy1 - cy0):
                cl.append(seg)
                break
        else:
            clusters.append([seg])
    lines = []
    for cl in clusters:
        cl.sort(key=lambda s: s[2])
        cy0, cy1 = min(s[0] for s in cl), max(s[1] for s in cl)
        # blank segments inside this visual line: a word space set as a line of its own
        spaces = [((b[0] + b[2]) / 2) for b in blanks if cy0 <= (b[1] + b[3]) / 2 <= cy1]
        size = max(s[5] for s in cl) or 10
        bullet, marker_x = False, 0.0
        first = cl[0][4].strip()
        if len(cl) > 1 and len(first) == 1 and first in BULLETS:
            bullet, marker_x = True, cl[0][2]
            cl = cl[1:]
        elif len(first) > 2 and first[0] in _STRONG_BULLETS and first[1] in " \t":
            bullet, marker_x = True, cl[0][2]
            cl[0] = cl[0][:4] + [cl[0][4].lstrip()[1:]] + cl[0][5:]
        text, tabular = "", False
        for a, b in zip([None] + cl[:-1], cl):
            if a is None:
                text = b[4]
                continue
            gap = b[2] - a[3]
            if gap > 3 * size and not (a is cl[0] and _NUMBER_ONLY.match(a[4].strip())):
                tabular = True
            spaced = gap > 0.15 * size or any(a[3] - 1 <= x <= b[2] + 1 for x in spaces)
            text += (" " if spaced and not text.endswith(" ") and not b[4].startswith(" ") else "") + b[4]
        text = _collapse(text)
        if not text:
            continue
        nc = sum(s[8] for s in cl) or 1
        word = text.split(" ", 1)[0]
        seg_text = cl[0][4].strip()
        font = "tibo" if cl[0][6] > cl[0][8] / 2 else "tiro"
        nominal = pm.get_text_length(seg_text, fontname=font, fontsize=size) if seg_text else 0
        scale = (cl[0][3] - cl[0][2]) / nominal if nominal else 0
        first_word_w = pm.get_text_length(word, fontname=font, fontsize=size) * scale if scale else 0.0
        lines.append(Line(page=pno, x0=cl[0][2], y0=min(s[0] for s in cl), x1=max(s[3] for s in cl),
                          y1=max(s[1] for s in cl), text=text, page_height=height,
                          bold=sum(s[6] for s in cl) / nc, italic=sum(s[7] for s in cl) / nc,
                          bullet=bullet, marker_x=marker_x, tabular=tabular, first_word_w=first_word_w))
    lines.sort(key=lambda l: (l.y0, l.x0))
    return lines, glyphs


def has_empty_column(rows: list[list[str]]) -> bool:
    """True when some column has no text in any row, header included: the column grid was misdetected,
    so the table is not written as a pipe table."""
    width = max((len(r) for r in rows), default=0)
    return any(all(i >= len(r) or not r[i] for r in rows) for i in range(width))


def is_frame(lines: list[Line]) -> bool:
    """A ruled region holding a bold QRD section heading is a frame drawn around
    the page text (Exjade 2017-2026), not a table: its lines stay ordinary text."""
    return any(ln.bold >= 0.5 and match_heading(ln.text) for ln in lines)


def _in_box(ch: Char, box, pad: float = 1.0) -> bool:
    cx, cy = (ch.x0 + ch.x1) / 2, (ch.y0 + ch.y1) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def _cell_rows(cell_boxes: list[list], glyphs: list[Char]) -> list[list[str]]:
    """Rows of cell text from the glyphs inside each cell box (None: a cell covered by a merged one, written
    as an empty cell)."""
    out = []
    for row in cell_boxes:
        cells = []
        for box in row:
            if box is None:
                cells.append(None)
                continue
            cells.append(char_lines([g for g in glyphs if _in_box(g, box, 0.5)]))
        out.extend(split_row(cells))
    return out


def _horizontal_rules(page) -> tuple[list[tuple[float, float, float]], list]:
    """(horizontal rules as (y, x0, x1) at least 150 pt long, every other drawn item as a bbox)."""
    rules, other = [], []
    for d in page.get_drawings():
        for item in d["items"]:
            kind = item[0]
            if kind == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < 1 and abs(a.x - b.x) >= 150:
                    rules.append((round((a.y + b.y) / 2, 1), min(a.x, b.x), max(a.x, b.x)))
                else:
                    other.append(("l", min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)))
            elif kind == "re":
                r = item[1]
                if r.height < 2 and r.width >= 150:
                    rules.append((round((r.y0 + r.y1) / 2, 1), r.x0, r.x1))
                elif r.width < 2 and r.height > 2:
                    other.append(("l", r.x0, r.y0, r.x1, r.y1))
                # filled rectangles (cell shading) are neither rules nor a reason to refuse
            elif kind in ("c", "qu"):
                rect = d["rect"]
                other.append((kind, rect.x0, rect.y0, rect.x1, rect.y1))
    return rules, other


def _rule_tables(page, pno: int, lines: list[Line], glyphs: list[Char], busy: list, stats: Stats
                 ) -> tuple[set[int], list[Table]]:
    """Tables ruled only horizontally (Opdivo 2020 Table 4): no vertical rules, so find_tables does not
    see them. Rules of the same width (x0 and x1 within 3 pt, at least 150 pt long) bound row bands;
    columns are vertical strips that no glyph between the first and the last rule touches.
    Accepted when there are 2 to 8 columns, at least 2 rows, at least 3 lines of text, no column is empty in
    every row or holds only list markers, the region holds no curve, vertical line or image (a chart) and
    no bold QRD heading (a page frame), and it overlaps no table found from a full grid. A table cut by
    the page break has no closing rule: lines below the last rule that keep to the columns and run on
    to the foot of the page (below 80 % of its height) are its last row."""
    rules, other = _horizontal_rules(page)
    groups: list[list[tuple[float, float, float]]] = []
    for r in sorted(rules, key=lambda r: (round(r[1]), round(r[2]), r[0])):
        g = next((g for g in groups if abs(g[0][1] - r[1]) <= 3 and abs(g[0][2] - r[2]) <= 3), None)
        if g is None:
            groups.append([r])
        elif all(abs(r[0] - o[0]) > 1.5 for o in g):
            g.append(r)
    taken: set[int] = set()
    tables: list[Table] = []
    for g in groups:
        ys = sorted(r[0] for r in g)
        if len(ys) < 3:
            continue
        x0, x1 = min(r[1] for r in g), max(r[2] for r in g)
        box = (x0, ys[0], x1, ys[-1])

        def hits(b) -> bool:
            return not (b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3])

        if any(hits(b) for b in busy) or any(hits(o[1:]) for o in other):
            continue
        inside = [i for i, ln in enumerate(lines) if i not in taken
                  and box[0] - 1 <= (ln.x0 + ln.x1) / 2 <= box[2] + 1 and box[1] <= (ln.y0 + ln.y1) / 2 <= box[3]]
        if len(inside) < 3 or is_frame([lines[i] for i in inside]):
            continue
        ink = [c for c in glyphs if not c.blank and _in_box(c, box, 0)]
        if not ink:
            continue
        size = statistics.median(c.size for c in ink)
        cols, strips = rule_columns([(c.x0, c.x1) for c in ink], x0, x1, max(6.0, 0.8 * size))
        if not 2 <= len(cols) <= 8:
            continue
        bands = list(zip(ys, ys[1:]))
        tail, bottom = [], ys[-1]
        for i in sorted((i for i, ln in enumerate(lines) if i not in taken and i not in inside
                         and ln.y0 >= ys[-1] - 1 and box[0] - 1 <= (ln.x0 + ln.x1) / 2 <= box[2] + 1),
                        key=lambda i: lines[i].y0):
            ln = lines[i]
            mine = [c for c in glyphs if not c.blank and _in_box(c, (ln.x0, ln.y0, ln.x1, ln.y1), 0)]
            if ln.y0 - bottom > 1.5 * max(ln.height, 1) or ln.y0 > 0.92 * ln.page_height or any(
                    c.x1 > a + 0.5 and c.x0 < b - 0.5 for c in mine for a, b in strips):
                break
            tail.append(i)
            bottom = ln.y1
        if tail and bottom > 0.8 * page.rect.height:
            bands.append((ys[-1], bottom + 0.5))
            inside += tail
            box = (box[0], box[1], box[2], bottom + 0.5)
        grid = [[(cx0, ya, cx1, yb) for cx0, cx1 in cols] for ya, yb in bands
                if any(ya <= (c.y0 + c.y1) / 2 <= yb for c in glyphs if not c.blank)]
        rows = _cell_rows(grid, glyphs)
        if len(rows) < 2 or has_empty_column(rows) or any(
                _is_marker_column([r[i] for r in rows]) for i in range(len(cols))):
            continue
        tables.append(Table(pno, box[0], box[1], box[2], box[3], rows, page.rect.height))
        taken.update(inside)
        stats.rule_tables += 1
    return taken, tables


def _page_tables(page, pno: int, lines: list[Line], glyphs: list[Char], stats: Stats
                 ) -> tuple[list[Line], list[Table]]:
    """Find ruled tables; accepted ones replace their lines, ambiguous ones mark lines verbatim."""
    with contextlib.redirect_stdout(io.StringIO()):
        found = page.find_tables().tables
    words = page.get_text("words") if found else []
    images = [img["bbox"] for img in page.get_image_info()]
    tables: list[Table] = []
    taken: set[int] = set()
    busy = list(images)
    for tb in found:
        if tb.col_count < 2:
            continue
        x0, y0, x1, y1 = tb.bbox
        inside = [i for i, ln in enumerate(lines) if x0 - 1 <= (ln.x0 + ln.x1) / 2 <= x1 + 1
                  and y0 - 1 <= (ln.y0 + ln.y1) / 2 <= y1 + 1]
        if is_frame([lines[i] for i in inside]):
            stats.frames_ignored += 1
            continue
        busy.append(tb.bbox)
        boxes = [list(row.cells) for row in tb.rows]
        rows = _cell_rows(boxes, glyphs)
        keep = [r for r in rows if any(r)]
        merged = sum(1 for row in boxes for b in row if b is None)
        cell_boxes = [c for row in tb.rows for c in row.cells if c is not None]
        uncovered = 0  # words inside the table box that do not sit wholly inside one cell
        for w in words:
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1 and not any(
                    b[0] - 1.5 <= w[0] and w[2] <= b[2] + 1.5 and b[1] - 1.5 <= cy <= b[3] + 1.5
                    for b in cell_boxes):
                uncovered += 1
        overlaps_image = any(not (b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1) for b in images)
        if not uncovered and not overlaps_image and table_is_clear(keep, merged):
            tables.append(Table(pno, x0, y0, x1, y1, keep, page.rect.height))
            taken.update(inside)
            stats.tables += 1
        else:
            for i in inside:
                lines[i].verbatim = True
            stats.tables_as_line_text += 1
    more, rule_tables = _rule_tables(page, pno, lines, glyphs, busy, stats)
    taken |= more
    return [ln for i, ln in enumerate(lines) if i not in taken], tables + rule_tables


@contextlib.contextmanager
def _quiet_mupdf(pm, stats: Stats):
    """Keep MuPDF's messages (e.g. "No common ancestor in structure tree", a
    tagging defect that does not affect the text) off stderr; count them."""
    shown = pm.TOOLS.mupdf_display_errors()
    shown_w = pm.TOOLS.mupdf_display_warnings()
    pm.TOOLS.mupdf_display_errors(False)
    pm.TOOLS.mupdf_display_warnings(False)
    pm.TOOLS.reset_mupdf_warnings()
    try:
        yield
    finally:
        messages = pm.TOOLS.mupdf_warnings()
        stats.mupdf_messages = len(messages.splitlines()) if messages else 0
        pm.TOOLS.reset_mupdf_warnings()
        pm.TOOLS.mupdf_display_errors(shown)
        pm.TOOLS.mupdf_display_warnings(shown_w)


def extract_items(pdf_bytes: bytes) -> tuple[list, Stats, list[str]]:
    """Stage A: PDF bytes -> visual lines and accepted tables in reading order."""
    pm = _import_pymupdf()
    stats = Stats()
    warnings: list[str] = []
    try:
        doc = pm.open(stream=pdf_bytes, filetype="pdf")
    except Exception as err:  # PyMuPDF raises several types for broken files
        return [], stats, [f"unreadable-pdf: {type(err).__name__}: {err}"]
    with doc, _quiet_mupdf(pm, stats):
        stats.pages = doc.page_count
        pages = [_page_lines(doc[p], p, stats) for p in range(doc.page_count)]
        page_lines = [pl for pl, _ in pages]
        chars = sum(len(l.text.replace(" ", "")) for pl in page_lines for l in pl)
        if chars < 50 * max(1, doc.page_count):
            warnings.append(f"no-text-layer: {chars} characters of text on {doc.page_count} pages; no OCR is done")
            return [], stats, warnings
        alltext = "".join(l.text for pl in page_lines for l in pl)
        good = sum(1 for c in alltext if c.isalnum() or c in " .,;:()-/%'\"")
        if good < 0.8 * len(alltext):
            warnings.append(f"unusable-text-layer: only {good} of {len(alltext)} characters are letters, digits or "
                            "punctuation (font without a Unicode map?)")
            return [], stats, warnings
        flat = remove_margins([l for pl in page_lines for l in pl])
        start, end, _ = annex_bounds(flat)
        first_page = flat[start].page if start < len(flat) else 0
        last_page = flat[end - 1].page if 0 < end <= len(flat) else doc.page_count - 1
        items: list = []
        for p in range(doc.page_count):
            lines = page_lines[p]
            if first_page <= p <= last_page:
                if not lines and doc[p].get_images():
                    warnings.append(f"page-without-text: page {p + 1} is an image without a text layer")
                lines, tables = _page_tables(doc[p], p, lines, pages[p][1], stats)
                merged = sorted(lines + tables, key=lambda it: (it.y0, it.x0))
                items.extend(merged)
            else:
                items.extend(lines)
        stats.tabular_lines = sum(1 for it in items if isinstance(it, Line) and it.tabular
                                  and first_page <= it.page <= last_page)
    return items, stats, warnings


def convert(pdf_bytes: bytes) -> dict:
    """PDF bytes -> {status, smpcs, sections, warnings, stats, converter_version, pymupdf_version, sha256}.

    ``status`` is "converted" when there are sections; otherwise it names why
    not: "empty-annex-i" or "no-annex-i" (the decision did not change Annex I),
    "no-smpc", "no-text-layer", "unusable-text-layer" or "unreadable-pdf".

    ``sections`` maps a file stem (``smpc-4.3-contraindications``) to its
    Markdown; the build writes each as ``<stem>.md``."""
    items, stats, warnings = extract_items(pdf_bytes)
    if items:
        result = convert_items(items, stats)
    else:
        status = warnings[0].split(":", 1)[0] if warnings else "no-text-layer"
        result = {"status": status, "smpcs": [], "sections": {}, "warnings": []}
    result["warnings"] = warnings + result["warnings"]
    result["stats"] = stats.__dict__
    result["converter_version"] = EU_CONVERTER_VERSION
    result["pymupdf_version"] = pymupdf_version()
    result["sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
    return result


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Convert an EU annex PDF to SmPC section Markdown files.")
    p.add_argument("pdf", help="annex PDF (anx_<id>_en.pdf)")
    p.add_argument("--out", help="directory to write <stem>.md files into")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)
    result = convert(Path(args.pdf).read_bytes())
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for stem, text in result["sections"].items():
            (out / f"{stem}.md").write_text(text, encoding="utf-8")
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"converter {result['converter_version']} (PyMuPDF {result['pymupdf_version']})")
        print(f"pages {result['stats']['pages']}, Annex I pages {result['stats']['annex_pages']}, "
              f"SmPCs {len(result['smpcs'])}, sections {len(result['sections'])}")
        for s in result["smpcs"]:
            print(f"  SmPC pages {s['start_page']}-{s['end_page']}: {s['name']}")
        for w in result["warnings"]:
            print(f"  warning: {w}")
    return 0 if result["sections"] else 1


if __name__ == "__main__":
    sys.exit(main())
