#!/usr/bin/env python3
"""README visuals, generated from the built label-history repos (standard library only).

  python3 -I tools/visualise.py all [--png] [--check]
  python3 -I tools/visualise.py queries|life|churn|rewritten|us-vs-eu [--repo build/us] [--eu-repo build/eu]
                                [--molecules molecules] [--date YYYY-MM-DD] [--out visualisations]

Writes visualisations/<name>.svg (1080 x 1350, hand-written SVG) and visualisations/data/<name>.json
(inputs, every computed number and the generated title) for:

  01-example-queries              data only: the answers the README's three example queries give on this build
  02-life-of-a-label             one label's sections over time (a fixed rule picks the label)
  03-label-churn                 section changes per 100 live labels per year
  04-most-rewritten              the 20 setids with the most versions
  05-us-vs-eu-contraindications  molecules where only one of US 34070-3 and EU SmPC 4.3 lists a
                                 contraindication beyond hypersensitivity

--date sets the chart date of visual 5 (default: the US record end); visuals 2 to 4 use the whole
record. --png renders each SVG at 2x (2160 x 2700) with Google Chrome headless when it is installed,
otherwise prints the command and keeps the SVG. --check regenerates into a temporary directory and
exits 1 if any SVG or data JSON differs byte for byte from the files in --out. Nothing in an SVG
depends on the time of the run, so regeneration is byte-stable.

Dates are DailyMed Published-Dates (what DailyMed showed), never FDA approval dates.
"""
from __future__ import annotations

import argparse
import bisect
import filecmp
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import labels  # noqa: E402

ROOT = labels.ROOT
DEFAULT_OUT = ROOT / "visualisations"
NAMES = {"queries": "01-example-queries", "life": "02-life-of-a-label", "churn": "03-label-churn", "rewritten": "04-most-rewritten",
         "us-vs-eu": "05-us-vs-eu-contraindications"}

# ---- design tokens
W, H = 1080, 1350
MARGIN = 64
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
INK, MUTED, HAIRLINE, SURFACE = "#1F2937", "#6B7280", "#E5E7EB", "#FFFFFF"
GREY_LIGHT, GREY_MID, GREY_DARK = "#D1D5DB", "#9CA3AF", "#6B7280"
ACCENT = "#C2410C"
TITLE_PX, SUBTITLE_PX, LABEL_PX, SMALL_PX = 52, 30, 26, 22
MIN_PX = 22


def us_source(record_to: str) -> str:
    """Source line of a US chart: attribution, what the counts are, the date rule and independence."""
    return ("Source: NLM DailyMed, via Particula-Tech/asof-drug-labels. Counts are of captured DailyMed versions as "
            f"of {fmt_day(record_to)}; dates are when DailyMed showed each version, not FDA approval dates. "
            "Independent project by Particula Tech; product names identify the medicines only; no affiliation with "
            "or endorsement by any manufacturer, FDA or NLM is claimed.")


def eu_source(record_to: str) -> str:
    """Source line of the US-versus-EU chart (record_to: the compare date)."""
    return ("Source: NLM DailyMed; European Commission Union Register and EMA; via Particula-Tech/asof-drug-labels. "
            f"Counts are of captured DailyMed and Union Register versions as of {fmt_day(record_to)}. Independent "
            "project by Particula Tech; product names identify the medicines only; no affiliation with or "
            "endorsement by any manufacturer, FDA, NLM, EMA or the European Commission is claimed.")


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

BOXED = "34066-1"
META_FILE = "label.json"

# Churn groups; 'Other' is everything else.
CHURN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Boxed warning", ("34066-1",)),
    ("Indications", ("34067-9",)),
    ("Dosage", ("34068-7",)),
    ("Contraindications", ("34070-3",)),
    ("Warnings and precautions", ("43685-7", "34071-1", "42232-9")),
    ("Adverse reactions", ("34084-4",)),
    ("Drug interactions", ("34073-7",)),
    ("Use in specific populations", ("43684-0",)),
)
OTHER = "Other"
# Reason groups: the eight clinical groups plus Clinical studies.
REASON_GROUPS = CHURN_GROUPS + (("Clinical studies", ("34092-7",)),)
# Rows (life of a label).
LIFE_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = CHURN_GROUPS + (
    ("Patient information", ("42230-3", "42231-1", "34076-0", "59845-8")),
    ("Package label", ("51945-4", "42229-5", "34069-5")),
)

# Hero-label rule constants
HERO_MIN_VERSIONS = 40
HERO_FIRST_BEFORE = "2010-01-01"
# Churn chart
CHURN_FIRST_YEAR = 2009
# Most-rewritten chart
TOP_N = 20
# Items that are not a contraindication beyond hypersensitivity, and the committed table of descriptive lines
US_EU_HYPER = re.compile(r"sensitiv|allerg|anaphyla")
US_EU_XREF = re.compile(r"^\[?see\b")
US_EU_TABLE = ROOT / "sources" / "contraindication-descriptions.json"


class VisualError(RuntimeError):
    pass


# ================================================================ log model

@dataclass
class Commit:
    sha: str
    setid: str
    version: int
    published: str
    format_conversion: str
    title: str
    files: tuple[tuple[str, str], ...]   # (status A/M/D, path)
    position: int = 0


@dataclass
class Label:
    setid: str
    commits: list[Commit] = field(default_factory=list)   # date order, same-day ties in commit order

    @property
    def first_published(self) -> str:
        return self.commits[0].published

    @property
    def last_published(self) -> str:
        return self.commits[-1].published


LOG_FORMAT = "--format=%x1e%H%x00%B%x00"


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
    proc = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
                          capture_output=True, text=True, env=env)
    if proc.returncode:
        raise VisualError(f"git {' '.join(args[:2])} failed in {repo.name}: {proc.stderr.strip()}")
    return proc.stdout


def read_log(repo: Path) -> str:
    """``git log --reverse --no-renames --name-status`` of the whole repo in the format parse_log reads."""
    return git(repo, "log", "--reverse", "--no-renames", "--name-status", LOG_FORMAT)


def trailers(body: str) -> dict[str, str]:
    out = {}
    for line in body.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key and " " not in key and key[0].isupper():
            out[key] = value.strip()
    return out


def parse_log(text: str) -> list[Commit]:
    """Commits (oldest first) with DailyMed Source-Id trailers; other commits are skipped."""
    out: list[Commit] = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        sha, body, rest = (record.split("\x00", 2) + ["", ""])[:3]
        t = trailers(body)
        if not t.get("Source-Id", "").startswith("dailymed:"):
            continue
        files = []
        for line in rest.splitlines():
            if not line.strip():
                continue
            status, _, path = line.partition("\t")
            if path.strip().count("/") >= 2:   # label files only; the corpus root files are not label text
                files.append((status.strip()[:1], path.strip()))
        subject = body.strip().splitlines()[0] if body.strip() else ""
        out.append(Commit(sha.strip(), t["Source-Id"].split(":", 1)[1].lower(), int(t["Source-Version"]),
                          t["Published-Date"], t.get("Format-Conversion", ""),
                          subject.split(" — label version")[0].strip(), tuple(files), len(out)))
    return out


def group_labels(commits: list[Commit]) -> dict[str, Label]:
    by: dict[str, Label] = {}
    for c in sorted(commits, key=lambda c: (c.published, c.position)):
        by.setdefault(c.setid, Label(c.setid)).commits.append(c)
    return by


def file_code(path: str) -> str:
    """LOINC code of a section file ('label.json' and non-section files give '')."""
    name = path.rsplit("/", 1)[-1]
    if name == META_FILE or not name.endswith(".md"):
        return ""
    return labels.section_code(path)


def changed_codes(c: Commit) -> set[str]:
    """Section codes a commit added, modified or deleted. A file renamed within one code counts once."""
    return {code for status, path in c.files if status in "AMD" and (code := file_code(path))}


def counted_changes(lab: Label) -> list[tuple[Commit, set[str]]]:
    """(commit, changed codes) for every version that counts as a change in the churn chart: not the first version
    of the setid and not a commit carrying a Format-Conversion trailer."""
    return [(c, changed_codes(c)) for i, c in enumerate(lab.commits) if i and not c.format_conversion]


def group_of(code: str, groups=CHURN_GROUPS) -> str:
    for name, codes in groups:
        if code in codes:
            return name
    return OTHER


# ================================================================ crosswalk

@dataclass
class Crosswalk:
    molecules: dict[str, dict]
    entries: dict[str, list[tuple[str, dict]]]   # setid -> [(inn, us entry)]

    def retired(self, setid: str) -> bool:
        return any(e.get("status") == "retired" for _, e in self.entries.get(setid, []))

    def single_nda_bla(self, setid: str) -> bool:
        es = [e for _, e in self.entries.get(setid, [])]
        if not es:
            return False
        for e in es:
            cats = set(e.get("marketing_categories") or [])
            apps = [a for a in e.get("application_numbers") or [] if a[:3] in ("NDA", "BLA")]
            if e.get("combination") or len(e.get("ingredients") or [None]) > 1:
                return False
            if not (cats & {"NDA", "BLA"} or (not cats and apps)):
                return False
        return True


def load_crosswalk(molecules_dir: Path) -> Crosswalk:
    mols, entries = {}, {}
    for path in sorted(Path(molecules_dir).glob("*.json")):
        if path.name == "selection.json":
            continue
        mol = json.loads(path.read_text(encoding="utf-8"))
        inn = (mol.get("inn") or path.stem).lower()
        mols[inn] = mol
        for e in mol.get("us") or []:
            setid = str(e.get("setid", "")).lower()
            if setid:
                entries.setdefault(setid, []).append((inn, e))
    return Crosswalk(mols, entries)


# ================================================================ helpers

def fmt_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def num(value: float, places: int = 1) -> str:
    s = f"{value:.{places}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def thousands(n: int) -> str:
    return f"{n:,}"


def brand_name(name: str) -> str:
    """'KEYTRUDA' -> 'Keytruda'; short all-caps suffixes (ER, XR, HCT) stay upper case; bracketed parts such as
    '(0009-6582)' are dropped."""
    words = []
    plain = re.sub(r"\s*\([^)]*\)", "", str(name)).strip() or str(name)
    for w in plain.split():
        if w.isupper() and len(w) > 3:
            words.append(w.capitalize())
        elif w.isupper() and len(w) <= 3 and not w.isalpha():
            words.append(w)
        elif w.isupper() and words:
            words.append(w)
        elif w.isupper():
            words.append(w.capitalize())
        else:
            words.append(w)
    return " ".join(words)


def last_complete_year(record_to: str) -> int:
    d = date.fromisoformat(record_to)
    return d.year if (d.month, d.day) == (12, 31) else d.year - 1


def year_fraction(day: str, start: date, end: date) -> float:
    d = date.fromisoformat(day)
    return (d.toordinal() - start.toordinal()) / max(1, end.toordinal() - start.toordinal())


# ================================================================ text measuring and SVG

# Helvetica AFM widths (per 1000 em); bold widths stand in for semibold.
_REG = dict(zip(" !\"#$%&'()*+,-./0123456789:;<=>?@", [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584,
                                                       278, 333, 278, 278] + [556] * 10 + [278, 278, 584, 584, 584,
                                                                                         556, 1015]))
_REG.update(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722,
                                               778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611]))
_REG.update(zip("abcdefghijklmnopqrstuvwxyz", [556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556,
                                               556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500]))
_BOLD = dict(_REG)
_BOLD.update(zip("abcdefghijklmnopqrstuvwxyz", [556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611,
                                                611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500]))
_BOLD.update(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722,
                                                778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611]))
_BOLD.update({"!": 333, "\"": 474, "'": 238, ":": 333, ";": 333, "?": 611})
SAFETY = 1.05


def text_width(text: str, size: float, bold: bool = False) -> float:
    table = _BOLD if bold else _REG
    return sum(table.get(ch, 600) for ch in text) / 1000 * size * SAFETY


def wrap(text: str, size: float, max_width: float, bold: bool = False) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if cur and text_width(trial, size, bold) > max_width:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _f(v: float) -> str:
    return num(round(v, 2), 2)


class Svg:
    def __init__(self, title: str, desc: str):
        self.parts: list[str] = []
        self.title, self.desc = title, desc
        self.min_font = 999

    def add(self, s: str) -> None:
        self.parts.append(s)

    def rect(self, x, y, w, h, fill, **kw) -> None:
        extra = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in kw.items())
        self.add(f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}" fill="{fill}"{extra}/>')

    def line(self, x1, y1, x2, y2, stroke, width=1.0, **kw) -> None:
        extra = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in kw.items())
        self.add(f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" stroke="{stroke}" '
                 f'stroke-width="{_f(width)}"{extra}/>')

    def circle(self, cx, cy, r, fill, **kw) -> None:
        extra = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in kw.items())
        self.add(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" fill="{fill}"{extra}/>')

    def text(self, x, y, s: str, size: int, fill: str = INK, weight: int = 400, anchor: str = "start") -> None:
        if size < MIN_PX:
            raise VisualError(f"text under {MIN_PX} px: {s!r}")
        self.min_font = min(self.min_font, size)
        attrs = f' font-weight="{weight}"' if weight != 400 else ""
        attrs += f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(f'<text x="{_f(x)}" y="{_f(y)}" font-size="{size}" fill="{fill}"{attrs}>{escape(s)}</text>')

    def render(self) -> str:
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
                f'font-family="{FONT}">\n<title>{escape(self.title)}</title>\n<desc>{escape(self.desc)}</desc>\n'
                f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>\n')
        return head + "\n".join(self.parts) + "\n</svg>\n"


@dataclass
class Frame:
    top: float      # first y below the header
    bottom: float   # last y above the footer


def draw_frame(svg: Svg, title: str, subtitle: str, footnote: str, source: str, stamp: str) -> Frame:
    """Title (finding), subtitle, footnote, source bottom-left and the build stamp bottom-right."""
    width = W - 2 * MARGIN
    y = MARGIN + TITLE_PX * 0.8
    for line in wrap(title, TITLE_PX, width, bold=True):
        svg.text(MARGIN, y, line, TITLE_PX, INK, 600)
        y += TITLE_PX * 1.2
    y += 8 - TITLE_PX * 1.2 + SUBTITLE_PX * 1.3
    for line in wrap(subtitle, SUBTITLE_PX, width):
        svg.text(MARGIN, y, line, SUBTITLE_PX, MUTED)
        y += SUBTITLE_PX * 1.3
    top = y + 16
    # footer, bottom up
    yb = H - 40
    stamp_w = text_width(stamp, SMALL_PX)
    src_lines = wrap(source, SMALL_PX, width)
    svg.text(W - MARGIN, yb, stamp, SMALL_PX, MUTED, anchor="end")
    if text_width(src_lines[-1], SMALL_PX) + stamp_w + 32 > width:
        yb -= SMALL_PX * 1.3   # the stamp gets a line of its own
    for i, line in enumerate(reversed(src_lines)):
        svg.text(MARGIN, yb - i * SMALL_PX * 1.3, line, SMALL_PX, MUTED)
    yb -= len(src_lines) * SMALL_PX * 1.3 + 12
    svg.line(MARGIN, yb, W - MARGIN, yb, HAIRLINE)
    yb -= 12 + SMALL_PX * 0.3
    note_lines = wrap(footnote, SMALL_PX, width) if footnote else []
    for i, line in enumerate(reversed(note_lines)):
        svg.text(MARGIN, yb - i * SMALL_PX * 1.3, line, SMALL_PX, MUTED)
    bottom = yb - len(note_lines) * SMALL_PX * 1.3 - 8
    return Frame(top, bottom)


def stamp(build_date: str, us_head: str, eu_head: str | None = None) -> str:
    s = f"Built {build_date} · US {us_head[:7]}"
    return s + (f" · EU {eu_head[:7]}" if eu_head else "")


# ================================================================ context

@dataclass
class Context:
    repo: Path
    eu_repo: Path | None
    molecules: Path
    chart_date: str | None
    _index: labels.Index | None = None
    _eu: labels.EuIndex | None = None
    _labels: dict[str, Label] | None = None
    _cross: Crosswalk | None = None

    @property
    def index(self) -> labels.Index:
        if self._index is None:
            self._index = labels.Index(self.repo, self.molecules)
        return self._index

    @property
    def eu(self) -> labels.EuIndex:
        if self._eu is None:
            if not self.eu_repo:
                raise VisualError("visual 5 needs the EU repo (--eu-repo)")
            self._eu = labels.EuIndex(self.eu_repo, self.molecules)
        return self._eu

    @property
    def labels(self) -> dict[str, Label]:
        if self._labels is None:
            self._labels = group_labels(parse_log(read_log(self.repo)))
        return self._labels

    @property
    def cross(self) -> Crosswalk:
        if self._cross is None:
            self._cross = load_crosswalk(self.molecules)
        return self._cross

    def inputs(self, eu: bool = False) -> dict:
        ix = self.index
        out = {"us_head": ix.head, "us_record_from": ix.record_from, "us_record_to": ix.record_to,
               "build_date": ix.build_date, "us_labels": len(ix.by_setid), "us_versions": len(ix.versions)}
        if eu:
            out.update({"eu_head": self.eu.head, "eu_record_from": self.eu.record_from,
                        "eu_record_to": self.eu.record_to})
        return out


# ================================================================ visual 2: life of a label

def boxed_arc(lab: Label) -> dict | None:
    """Presence of the boxed-warning file over a label's versions: None unless it was absent in the first
    version, added later and absent again in the last version."""
    present, history = False, []
    for i, c in enumerate(lab.commits):
        st = {s for s, p in c.files if file_code(p) == BOXED}
        if "A" in st and "D" not in st:
            present_now = True
        elif "D" in st and "A" not in st:
            present_now = False
        else:
            present_now = present
        history.append((i, c, st, present_now))
        present = present_now
    if history[0][3] or history[-1][3] or not any(h[3] for h in history):
        return None
    added = next(h for h in history if h[3])
    last = max(h[0] for h in history if h[3])
    removed = history[last + 1]
    touched = [(i, c) for i, c, st, p in history if added[0] < i <= last and st]
    # Exclusion: a format-conversion version rewrites most sections at once, so it is not a change
    real = [c.version for i, c in touched if not c.format_conversion]
    conversions = [c.version for i, c in touched if c.format_conversion]
    return {"added_index": added[0] + 1, "added_version": added[1].version, "added_date": added[1].published,
            "last_present_index": last + 1, "last_present_version": history[last][1].version,
            "last_present_date": history[last][1].published,
            "removed_index": removed[0] + 1, "removed_version": removed[1].version,
            "removed_date": removed[1].published, "changed_while_present": len(touched),
            "boxed_warning_changes": len(real), "boxed_warning_change_versions": real,
            "boxed_warning_conversions": len(conversions), "boxed_warning_conversion_versions": conversions}


def hero_candidates(labs: dict[str, Label], cross: Crosswalk) -> list[dict]:
    """Hero-label rule: current single-ingredient NDA/BLA labels with >= 40 versions and a first Published-Date before
    2010 whose boxed warning was added after the first version and later deleted; most versions first."""
    out = []
    for setid, lab in labs.items():
        if len(lab.commits) < HERO_MIN_VERSIONS or lab.first_published >= HERO_FIRST_BEFORE:
            continue
        if cross.retired(setid) or not cross.single_nda_bla(setid):
            continue
        arc = boxed_arc(lab)
        if arc:
            out.append({"setid": setid, "versions": len(lab.commits), "first_published": lab.first_published,
                        "last_published": lab.last_published, "title": lab.commits[-1].title, **arc})
    return sorted(out, key=lambda r: (-r["versions"], r["first_published"], r["setid"]))


def label_name(repo: Path, lab: Label) -> str:
    """products[0].name from label.json at the label's last commit, else the commit subject."""
    last = lab.commits[-1]
    path = next((p for _, p in last.files if p.endswith("/" + META_FILE)), None)
    if path is None:
        folder = next((p.rsplit("/", 1)[0] for c in lab.commits for _, p in c.files), None)
        path = f"{folder}/{META_FILE}" if folder else None
    try:
        meta = json.loads(git(repo, "show", f"{last.sha}:{path}")) if path else {}
    except (VisualError, ValueError):
        meta = {}
    products = meta.get("products") or []
    if products and products[0].get("name"):
        return str(products[0]["name"])
    return last.title


def life_title(name: str, versions: int, first_year: int, arc: dict) -> str:
    changes, conv = arc["boxed_warning_changes"], arc["boxed_warning_conversions"]
    plus = f" (plus {conv} format conversion{'' if conv == 1 else 's'})" if conv else ""
    return (f"{name}: {versions} label versions since {first_year}. The boxed warning appeared on "
            f"{fmt_day(arc['added_date'])}, changed {changes} time{'' if changes == 1 else 's'}{plus}, and was "
            f"gone on {fmt_day(arc['removed_date'])}.")


def life_rows(lab: Label) -> list[dict]:
    rows = []
    for name, codes in LIFE_ROWS + ((OTHER, ()),):
        ticks = []
        for i, c in enumerate(lab.commits):
            st = sorted({s for s, p in c.files if (code := file_code(p)) and
                         (code in codes if codes else group_of(code, LIFE_ROWS) == OTHER)})
            if st:
                ticks.append({"index": i + 1, "version": c.version, "date": c.published, "status": "".join(st),
                              "format_conversion": bool(c.format_conversion)})
        rows.append({"row": name, "codes": list(codes), "ticks": ticks})
    return rows


def build_life(ctx: Context) -> tuple[str, dict]:
    labs, ix = ctx.labels, ctx.index
    cands = hero_candidates(labs, ctx.cross)
    if not cands:
        raise VisualError("no label meets the hero-label rule")
    hero = cands[0]
    lab = labs[hero["setid"]]
    raw_name = label_name(ctx.repo, lab)
    name = brand_name(raw_name)
    first_year = int(lab.first_published[:4])
    title = life_title(name, len(lab.commits), first_year, hero)
    rows = life_rows(lab)
    conversions = [{"index": i + 1, "version": c.version, "date": c.published, "kind": c.format_conversion}
                   for i, c in enumerate(lab.commits) if c.format_conversion]
    data = {"visual": NAMES["life"], "inputs": ctx.inputs(),
            "rule": ("current single-ingredient NDA/BLA labels with at least 40 versions and a first Published-Date "
                     "before 2010-01-01 whose boxed-warning file (34066-1) was added after the first version and "
                     "later deleted; ties by more versions"),
            "candidates": cands, "hero": {**hero, "name": raw_name, "display_name": name,
                                          "versions": len(lab.commits), "first_year": first_year},
            "format_conversions": conversions, "rows": rows, "title": title}
    return render_life(data, ix.record_to), data


def render_life(data: dict, record_to: str) -> str:
    hero, rows = data["hero"], data["rows"]
    svg = Svg(data["title"], "One row per part of the label, one tick per version that changed it.")
    subtitle = (f"Each tick is a version of the {hero['display_name']} label (DailyMed setid {hero['setid'][:8]}) "
                "that added, changed or removed that part of the label.")
    foot = []
    convs = data["format_conversions"]
    if convs:
        vs = [str(c["version"]) for c in convs]
        listed = vs[0] if len(vs) == 1 else ", ".join(vs[:-1]) + " and " + vs[-1]
        when = fmt_day(convs[0]["date"]) if len(convs) == 1 else \
            f"{fmt_day(convs[0]['date'])} to {fmt_day(convs[-1]['date'])}"
        foot.append(f"Hollow {'tick' if len(vs) == 1 else 'ticks'}: label version{'s' if len(vs) > 1 else ''} "
                    f"{listed} ({when}) switched between the old and the new label format, so most sections "
                    "changed at once.")
    foot.append(f"Ticks in the first column are the first version on DailyMed ({fmt_day(hero['first_published'])}).")
    fr = draw_frame(svg, data["title"], subtitle, "Method: " + " ".join(foot),
                    us_source(data["inputs"]["us_record_to"]),
                    stamp(data["inputs"]["build_date"], data["inputs"]["us_head"]))
    label_w = max(text_width(r["row"], LABEL_PX) for r in rows) + 24
    x0, x1 = MARGIN + label_w, W - MARGIN - 8
    start = date(int(hero["first_published"][:4]), 1, 1)
    end = date.fromisoformat(record_to)
    axis_h = 44
    annot_h = 40
    top = fr.top + annot_h
    row_h = min(64.0, (fr.bottom - axis_h - top) / len(rows))
    xs = lambda d: x0 + (x1 - x0) * year_fraction(d, start, end)  # noqa: E731
    # year grid
    years = list(range(start.year, end.year + 1))
    step = 1
    while (x1 - x0) / (len(years) / step) < text_width("2000", SMALL_PX) + 14:
        step += 1
    y_axis = top + row_h * len(rows) + 8
    for yr in years[::step] if step > 1 else years:
        x = xs(f"{yr}-01-01")
        svg.line(x, top, x, y_axis, HAIRLINE)
        svg.text(x, y_axis + SMALL_PX + 6, str(yr), SMALL_PX, MUTED, anchor="middle")
    for i, r in enumerate(rows):
        yc = top + row_h * (i + 0.5)
        svg.line(x0, top + row_h * (i + 1), x1, top + row_h * (i + 1), HAIRLINE)
        is_bw = r["codes"] == [BOXED]
        svg.text(MARGIN, yc + LABEL_PX * 0.35, r["row"], LABEL_PX, ACCENT if is_bw else INK, 600 if is_bw else 400)
        tick_h = row_h * 0.56
        if is_bw:
            xa, xr = xs(hero["added_date"]), xs(hero["removed_date"])
            svg.rect(xa, yc - tick_h / 2, xr - xa, tick_h, ACCENT, fill_opacity="0.14")
            note = f"boxed warning {fmt_day(hero['added_date'])} to {fmt_day(hero['removed_date'])}"
            nw = text_width(note, SMALL_PX)
            xn = min(max((xa + xr) / 2, x0 + nw / 2), x1 - nw / 2)
            svg.text(xn, top - 12, note, SMALL_PX, ACCENT, anchor="middle")
        for t in r["ticks"]:
            x = xs(t["date"])
            color = ACCENT if is_bw else INK
            if t["format_conversion"]:
                svg.rect(x - 2.5, yc - tick_h / 2, 5, tick_h, SURFACE, stroke=color, stroke_width="1.5")
            else:
                svg.rect(x - 1.5, yc - tick_h / 2, 3, tick_h, color)
    return svg.render()


# ================================================================ visual 3: churn

def churn_data(labs: dict[str, Label], cross: Crosswalk, record_to: str) -> dict:
    """Churn counts: section changes per group and year, live labels per year, rates per 100 live labels."""
    last_year = last_complete_year(record_to)
    years = list(range(CHURN_FIRST_YEAR, last_year + 1))
    groups = [g for g, _ in CHURN_GROUPS] + [OTHER]
    counts = {y: {g: 0 for g in groups} for y in years}
    live = {y: 0 for y in years}
    for setid, lab in labs.items():
        current = not cross.retired(setid)
        for y in years:
            if lab.first_published <= f"{y}-12-31" and (current or lab.last_published >= f"{y}-01-01"):
                live[y] += 1
        for c, codes in counted_changes(lab):
            y = int(c.published[:4])
            if y in counts:
                for code in codes:
                    counts[y][group_of(code)] += 1
    clinical = [g for g, _ in CHURN_GROUPS]
    rows = []
    for y in years:
        rate = {g: (100 * counts[y][g] / live[y] if live[y] else 0.0) for g in groups}
        rows.append({"year": y, "live_labels": live[y], "changes": counts[y],
                     "per_100": {g: round(v, 2) for g, v in rate.items()},
                     "clinical_per_label": round(sum(counts[y][g] for g in clinical) / live[y], 2) if live[y] else 0.0,
                     "total_per_100": round(sum(rate.values()), 2)})
    return {"years": years, "groups": groups, "rows": rows}


def churn_title(rows: list[dict]) -> tuple[str, dict, dict]:
    peak = max(rows, key=lambda r: (r["clinical_per_label"], -r["year"]))
    last = rows[-1]
    title = (f"A live US label had {peak['clinical_per_label']:.1f} clinical-section changes in {peak['year']}, "
             f"the busiest year since {rows[0]['year']}, and {last['clinical_per_label']:.1f} in {last['year']}")
    return title, peak, last


def build_churn(ctx: Context) -> tuple[str, dict]:
    ix = ctx.index
    d = churn_data(ctx.labels, ctx.cross, ix.record_to)
    title, peak, last = churn_title(d["rows"])
    molecules = sum(1 for _ in ctx.molecules.glob("*.json")) - (1 if (ctx.molecules / "selection.json").exists() else 0)
    data = {"visual": NAMES["churn"], "inputs": ctx.inputs(),
            "rule": ("a section change is a section file added, modified or deleted in a commit (one per LOINC code "
                     "per commit), excluding each setid's first version, commits with a Format-Conversion trailer "
                     "and label.json; year = Published-Date year; live labels in a year = setids first published on "
                     "or before 31 Dec that are current or whose last version is on or after 1 Jan"),
            "group_codes": {g: list(c) for g, c in CHURN_GROUPS} | {OTHER: "everything else"},
            **d, "peak": {"year": peak["year"], "clinical_per_label": peak["clinical_per_label"]},
            "last": {"year": last["year"], "clinical_per_label": last["clinical_per_label"]},
            "molecules": molecules, "title": title}
    return render_churn(data), data


def nice_step(top: float, n: int = 5) -> float:
    raw = top / n
    mag = 10 ** len(str(int(raw))) / 10 if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def render_churn(data: dict) -> str:
    rows, groups = data["rows"], data["groups"]
    svg = Svg(data["title"], "Stacked bars of section changes per 100 live labels per year, by section.")
    example = round(data["peak"]["clinical_per_label"], 1)
    subtitle = (f"Section changes per 100 labels live that year, stacked by section group ({example:.1f} per label "
                f"= {100 * example:.0f} per 100)")
    foot = (f"Method: per 100 labels live that year; first versions and format conversions excluded; the corpus is "
            f"{data['molecules']} molecules selected in 2026, so earlier years count fewer labels.")
    fr = draw_frame(svg, data["title"], subtitle, foot, us_source(data["inputs"]["us_record_to"]),
                    stamp(data["inputs"]["build_date"], data["inputs"]["us_head"]))
    stack = list(reversed(groups))     # bottom: Other ... top: Boxed warning
    colors = {OTHER: GREY_LIGHT, groups[0]: ACCENT}
    for i, g in enumerate(stack[1:-1]):
        colors[g] = GREY_DARK if i % 2 == 0 else GREY_MID
    label_col = max(text_width(f"{g} 000", SMALL_PX) for g in groups) + 28
    y_label_w = text_width("000", SMALL_PX) + 12
    x0, x1 = MARGIN + y_label_w, W - MARGIN - label_col
    top, bottom = fr.top + 10, fr.bottom - SMALL_PX - 20
    peak_total = max(r["total_per_100"] for r in rows)
    step = nice_step(peak_total)
    ymax = step * (int(peak_total / step) + 1)
    ys = lambda v: bottom - (bottom - top) * v / ymax  # noqa: E731
    v = 0.0
    while v <= ymax + 1e-9:
        svg.line(x0, ys(v), x1, ys(v), HAIRLINE)
        svg.text(x0 - 10, ys(v) + 8, num(v), SMALL_PX, MUTED, anchor="end")
        v += step
    slot = (x1 - x0) / len(rows)
    bw = slot * 0.7
    for i, r in enumerate(rows):
        x = x0 + slot * i + (slot - bw) / 2
        y = bottom
        for g in stack:
            h = (bottom - top) * r["per_100"][g] / ymax
            if h > 0:
                svg.rect(x, y - h, bw, h, colors[g])
                if g != stack[0]:
                    svg.line(x, y, x + bw, y, SURFACE, 1.5)
                y -= h
        if r["year"] % 2 == 1 or len(rows) <= 9:
            svg.text(x + bw / 2, bottom + SMALL_PX + 8, str(r["year"]), SMALL_PX, MUTED, anchor="middle")
    # direct labels at the last bar, de-overlapped
    last = rows[-1]
    xl = x0 + slot * (len(rows) - 1) + (slot + bw) / 2
    y, mids = bottom, []
    for g in stack:
        h = (bottom - top) * last["per_100"][g] / ymax
        mids.append((g, y - h / 2))
        y -= h
    placed = spread([m for _, m in mids][::-1], SMALL_PX + 6, top, bottom)[::-1]
    for (g, ym), yp in zip(mids, placed):
        color = ACCENT if g == groups[0] else (MUTED if g == OTHER else INK)
        svg.line(xl + 2, ym, xl + 14, yp, GREY_MID, 1)
        svg.text(xl + 18, yp + 7, f"{g} {num(last['per_100'][g], 0)}", SMALL_PX, color,
                 600 if g == groups[0] else 400)
    return svg.render()


def spread(ys: list[float], gap: float, lo: float, hi: float) -> list[float]:
    """Positions for labels wanted at ``ys`` (ascending) at least ``gap`` apart and inside [lo, hi]."""
    out = []
    for y in ys:
        out.append(max(y, out[-1] + gap) if out else max(y, lo))
    if out and out[-1] > hi:
        out[-1] = hi
        for i in range(len(out) - 2, -1, -1):
            out[i] = min(out[i], out[i + 1] - gap)
    return out


# ================================================================ visual 4: most rewritten

def top_labels(labs: dict[str, Label], n: int = TOP_N) -> list[Label]:
    return sorted(labs.values(), key=lambda lab: (-len(lab.commits), lab.first_published, lab.setid))[:n]


def reason_of(lab: Label) -> tuple[str, dict[str, int]]:
    counts = {g: 0 for g, _ in REASON_GROUPS}
    for _, codes in counted_changes(lab):
        for code in codes:
            g = group_of(code, REASON_GROUPS)
            if g in counts:
                counts[g] += 1
    best = max(counts.items(), key=lambda kv: kv[1])   # ties: the first group in group order
    return best[0], counts


def rewritten_title(top: dict, total: int) -> str:
    return (f"{top['display_name']}'s US label has {top['versions']} versions since {top['first_year']}, the most "
            f"of {thousands(total)} labels in the corpus")


def build_rewritten(ctx: Context) -> tuple[str, dict]:
    labs = ctx.labels
    rows = []
    for rank, lab in enumerate(top_labels(labs), 1):
        raw = label_name(ctx.repo, lab)
        reason, counts = reason_of(lab)
        retired = ctx.cross.retired(lab.setid)
        rows.append({"rank": rank, "setid": lab.setid, "name": raw, "display_name": brand_name(raw),
                     "retired": retired, "versions": len(lab.commits), "first_published": lab.first_published,
                     "first_year": int(lab.first_published[:4]), "last_published": lab.last_published,
                     "most_changed": reason, "group_changes": counts})
    title = rewritten_title(rows[0], len(labs))
    data = {"visual": NAMES["rewritten"], "inputs": ctx.inputs(),
            "rule": ("the 20 setids with the most commits, ties by earlier first Published-Date; name = products[0]"
                     ".name of label.json at the last commit; most changed = the group with the most section changes "
                     "(counted as in visual 3) among the eight clinical groups and Clinical studies (34092-7)"),
            "labels": len(labs), "rows": rows, "title": title}
    return render_rewritten(data), data


def render_rewritten(data: dict) -> str:
    rows = data["rows"]
    svg = Svg(data["title"], "Horizontal bars: label versions of the 20 most-rewritten US labels.")
    subtitle = ("Label versions DailyMed showed per setid, and the section changed most often "
                "(first versions and format conversions not counted).")
    foot = ("Method: versions counted per DailyMed setid; one product can span several setids. A retired setid is one "
            "DailyMed no longer updates.")
    fr = draw_frame(svg, data["title"], subtitle, foot, us_source(data["inputs"]["us_record_to"]),
                    stamp(data["inputs"]["build_date"], data["inputs"]["us_head"]))
    names = [r["display_name"] + (" (retired setid)" if r["retired"] else "") for r in rows]
    name_w = max(text_width(nm, LABEL_PX) for nm in names) + 20
    reason_w = max(text_width(r["most_changed"], SMALL_PX) for r in rows)
    reason_w = max(reason_w, text_width("most changed", SMALL_PX))
    count_w = text_width("000", LABEL_PX) + 10
    x0 = MARGIN + name_w
    x_reason = W - MARGIN - reason_w
    bar_max = x_reason - 24 - count_w - x0
    head_y = fr.top + SMALL_PX
    svg.text(MARGIN, head_y, "label", SMALL_PX, MUTED)
    svg.text(x0, head_y, "versions", SMALL_PX, MUTED)
    svg.text(x_reason, head_y, "most changed", SMALL_PX, MUTED)
    top = head_y + 14
    row_h = min(52.0, (fr.bottom - top) / len(rows))
    vmax = rows[0]["versions"]
    for i, (r, nm) in enumerate(zip(rows, names)):
        yc = top + row_h * (i + 0.5)
        first = i == 0
        svg.text(MARGIN, yc + LABEL_PX * 0.35, nm, LABEL_PX, INK, 600 if first else 400)
        bl = bar_max * r["versions"] / vmax
        svg.rect(x0, yc - row_h * 0.3, bl, row_h * 0.6, ACCENT if first else GREY_MID)
        svg.text(x0 + bl + 8, yc + LABEL_PX * 0.35, str(r["versions"]), LABEL_PX, ACCENT if first else INK,
                 600 if first else 400)
        svg.text(x_reason, yc + SMALL_PX * 0.35, r["most_changed"], SMALL_PX, MUTED)
    return svg.render()


# ================================================================ visual 5: US vs EU contraindications

US_NONE_KEY = "none"          # compare_key of "None.": the section says there is no contraindication
CATEGORIES = ("none", "hypersensitivity-only", "lists")
CELL_NAMES = ("only_eu", "only_us", "both", "neither")
DESCRIPTION_FIELDS = ("molecule", "side", "doc_id", "version", "section", "line", "reason")
DESCRIPTION_SECTIONS = {"us": "34070-3", "eu": "smpc-4.3"}
NUMBER_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def us_items(text: str) -> list[list[str]]:
    """The items of a US Contraindications section: heading lines, table rules and intro lines (stripped text
    ending with ':') are dropped; a blank line ends an item; inside a block a list-marker line starts a new item
    and every other line continues the current one (SPL paragraphs and bullets)."""
    items: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if cur:
                items.append(cur)
            cur = []
            continue
        if not labels.compare_lines(line) or s.endswith(":"):
            continue
        if labels._LIST_MARKER.match(s) and cur:
            items.append(cur)
            cur = []
        cur.append(s)
    if cur:
        items.append(cur)
    return items


def eu_items(text: str) -> list[list[str]]:
    """The items of an EU SmPC 4.3: every comparable line is one item (the SmPC lists one contraindication per
    line and the PDF conversion does not keep paragraph spacing), intro lines ending with ':' dropped."""
    return [[line.strip()] for line, _ in labels.compare_lines(text, eu_markers=True)
            if not line.strip().endswith(":")]


def item_key(item: list[str]) -> str:
    return labels.compare_key(" ".join(item))


def drop_reason(key: str, described: frozenset[str] = frozenset()) -> str | None:
    """Why an item is not a contraindication beyond hypersensitivity, or None when it is one."""
    if key == US_NONE_KEY:
        return "none"
    if US_EU_XREF.match(key):
        return "cross-reference"
    if US_EU_HYPER.search(key):
        return "hypersensitivity"
    if key in described:
        return "descriptions table"
    return None


def classify_items(items: list[list[str]], described: frozenset[str] = frozenset()) -> dict:
    """category none when every item is "None."; lists when at least one item is kept; else
    hypersensitivity-only."""
    if not items:
        raise VisualError("an empty section has no category")
    kept = [it for it in items if drop_reason(item_key(it), described) is None]
    dropped = [it for it in items if drop_reason(item_key(it), described) is not None]
    if all(item_key(it) == US_NONE_KEY for it in items):
        category = "none"
    else:
        category = "lists" if kept else "hypersensitivity-only"
    return {"category": category, "kept": kept, "dropped": dropped}


def cell_of(us_category: str, eu_category: str) -> str:
    if us_category == "lists" and eu_category == "lists":
        return "both"
    if eu_category == "lists":
        return "only_eu"
    if us_category == "lists":
        return "only_us"
    return "neither"


def load_descriptions(path: Path = US_EU_TABLE) -> list[dict]:
    """The committed table of descriptive lines set aside by hand (sources/contraindication-descriptions.json)."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise VisualError(f"cannot read the descriptions table {path}: {err}") from err
    if not isinstance(entries, list):
        raise VisualError(f"{path.name}: entries must be a list")
    for n, e in enumerate(entries):
        missing = [k for k in DESCRIPTION_FIELDS if not isinstance(e, dict) or e.get(k) in (None, "")]
        if missing:
            raise VisualError(f"{path.name} entry {n}: missing {', '.join(missing)}")
        if e["side"] not in DESCRIPTION_SECTIONS or e["section"] != DESCRIPTION_SECTIONS[e["side"]]:
            raise VisualError(f"{path.name} entry {n}: side must be us (section 34070-3) or eu (section smpc-4.3)")
        if not isinstance(e["version"], int) or isinstance(e["version"], bool):
            raise VisualError(f"{path.name} entry {n}: version must be an integer")
    return entries


def match_description(entry: dict, items: list[list[str]]) -> list[str]:
    """The one item of its side that a table entry names; a build error when it matches none or several."""
    key = labels.compare_key(entry["line"])
    hits = [it for it in items if item_key(it) == key]
    if len(hits) != 1:
        raise VisualError(f"descriptions table: {entry['molecule']} {entry['side'].upper()} line {entry['line']!r} "
                          f"matches {len(hits)} items on the date (needs exactly one); update "
                          f"{US_EU_TABLE.relative_to(ROOT)}")
    return hits[0]


def classify_pair(us: list[list[str]], eu: list[list[str]], entries: list[dict] = ()) -> dict:
    """Both sides of one molecule, with the table entries of that molecule set aside, and the rule-only result."""
    items = {"us": us, "eu": eu}
    described: dict[str, set[str]] = {"us": set(), "eu": set()}
    for e in entries:
        described[e["side"]].add(item_key(match_description(e, items[e["side"]])))
    out = {side: classify_items(items[side], frozenset(described[side])) for side in items}
    rule = {side: classify_items(items[side])["category"] for side in items}
    out["cell"] = cell_of(out["us"]["category"], out["eu"]["category"])
    out["rule_only_cell"] = cell_of(rule["us"], rule["eu"])
    return out


def tally(cells: list[str]) -> dict:
    counts = {name: sum(1 for c in cells if c == name) for name in CELL_NAMES}
    counts["one_side_only"] = counts["only_eu"] + counts["only_us"]
    return counts


def number_word(n: int) -> str:
    return NUMBER_WORDS[n] if 0 <= n < len(NUMBER_WORDS) else str(n)


def us_eu_title(one_side: int, n: int, day: str) -> str:
    return (f"In {one_side} of {n} molecules, only one of the US and EU labels lists a contraindication beyond "
            f"hypersensitivity ({fmt_day(day)})")


def us_eu_subtitle(data: dict) -> str:
    return (f"US Contraindications section against EU SmPC section 4.3, for the {len(data['rows'])} molecules whose "
            "EU reference product is comparable to the US brand.")


def us_eu_footnote(data: dict) -> str:
    day = fmt_day(data["inputs"]["date"])
    entries = data["descriptions_table"]["entries"]
    sentences = "sentence is" if entries == 1 else "sentences are"
    return ("Method: hypersensitivity and allergy items are set aside on both sides because every EU SmPC carries the "
            "template line; intro lines, cross-references and \"None.\" are not contraindications; "
            f"{number_word(entries)} descriptive {sentences} set aside by the committed table "
            f"{data['descriptions_table']['path']}. The other label may carry the same risk under Warnings and "
            "Precautions or a boxed warning: the chart reads the Contraindications sections only. US: what DailyMed "
            f"showed on {day}; EU: Annex I of the Commission decision in force on that date; every counted line "
            "passes check_citations. Absence from an extracted Contraindications section does not establish absence "
            "of a risk or suitability for any patient.")


US_EU_RULE = (
    "population: molecules with a built EU reference product whose cli.check_molecule verdict on the date works "
    "with no pairing caveat, whose reference product has comparable_to_us yes, and whose compare_jurisdictions "
    "contraindications pair (no line cap) has text on both sides, neither truncated; rows in selection rank order, "
    "then INN. Items: US 34070-3 without heading lines, table rules and intro lines (ending with ':'), a blank "
    "line ends an item, a list-marker line starts one and every other line continues the current item; EU SmPC 4.3 "
    "one item per compare_lines line (eu_markers), intro lines dropped. Item key = compare_key of the item's lines "
    "joined. Not a contraindication beyond hypersensitivity: key 'none', a cross-reference (key matches "
    "^\\[?see\\b), the hypersensitivity and allergy family (key matches sensitiv|allerg|anaphyla, so an item "
    "naming hypersensitivity and another contraindication in one sentence counts as hypersensitivity) and the "
    "items named in the descriptions table. Category per side: none when every item is 'None.', lists when at "
    "least one item is kept, else hypersensitivity-only. Cells: only_eu = US none or hypersensitivity-only and EU "
    "lists; only_us = US lists and EU none or hypersensitivity-only; both; neither. Presence only: no wording "
    "comparison and nothing counted per item.")


def build_us_vs_eu(ctx: Context) -> tuple[str, dict]:
    import cli   # the compare-check verdict (same code as reports/compare-check.json)
    ix, eu = ctx.index, ctx.eu
    day = ctx.chart_date or ix.record_to
    labels.check_date(day)
    entries = load_descriptions()
    mols = ix.molecules()
    order = sorted(mols, key=lambda inn: (mols[inn].get("rank") or 10**6, inn))
    funnel = dict.fromkeys(("reference_products", "works", "no_pairing_caveat", "comparable_yes", "both_sides"), 0)
    rows, skipped, rule_cells = [], [], []

    def skip(inn: str, reason: str, detail: str = "") -> None:
        skipped.append({"molecule": inn, "rank": mols[inn].get("rank"), "reason": reason, "detail": detail})

    for inn in order:
        ref = next((e for e in mols[inn].get("eu") or [] if e.get("role") == "reference"), None)
        if not ref:
            continue
        funnel["reference_products"] += 1
        verdict = cli.check_molecule(ix, eu, inn, day)
        if not verdict["works"]:
            skip(inn, "compare does not work", "; ".join(verdict["reasons"]))
            continue
        funnel["works"] += 1
        if verdict.get("pairing_caveat"):
            skip(inn, "pairing caveat", verdict["pairing_caveat"])
            continue
        funnel["no_pairing_caveat"] += 1
        if ref.get("comparable_to_us") != "yes":
            skip(inn, f"comparable_to_us is {ref.get('comparable_to_us') or 'not set'}", ref.get("comparability_note") or "")
            continue
        funnel["comparable_yes"] += 1
        out = labels.compare_jurisdictions(ix, inn, day, "contraindications", eu=eu, line_cap=None)
        pair = out["pairs"][0] if out["pairs"] else {}
        us_side, eu_side = pair.get("us"), pair.get("eu")
        if not (us_side and eu_side and us_side["text"].strip() and eu_side["text"].strip()):
            skip(inn, "contraindications section missing on one side")
            continue
        if us_side["truncated"] or eu_side["truncated"]:
            skip(inn, "contraindications section truncated")
            continue
        funnel["both_sides"] += 1
        items = {"us": us_items(us_side["text"]), "eu": eu_items(eu_side["text"])}
        empty = [side.upper() for side in items if not items[side]]
        if empty:
            skip(inn, "empty contraindications section", " and ".join(empty))
            continue
        cites = {"us": us_side["citation"], "eu": eu_side["citation"]}
        mine = [e for e in entries if e["molecule"] == inn]
        for e in mine:
            c = cites[e["side"]]
            if (e["doc_id"], e["version"]) != (c["doc_id"], c["version"]):
                raise VisualError(f"descriptions table: {inn} {e['side'].upper()} cites {e['doc_id']} v{e['version']}"
                                  f", but the label on {day} is {c['doc_id']} v{c['version']}")
        res = classify_pair(items["us"], items["eu"], mine)
        rule_cells.append(res["rule_only_cell"])
        quotes = [(cites[side]["doc_id"], cites[side]["version"], cites[side]["section"], line)
                  for side in ("us", "eu") for item in res[side]["kept"] for line in item]
        quotes += [(e["doc_id"], e["version"], e["section"], e["line"]) for e in mine]
        if quotes:
            results = labels.check_citations(ix, [{"doc_id": d, "version": v_, "section": s, "quote": q, "as_of": day}
                                                  for d, v_, s, q in quotes], eu=eu)["results"]
            bad = [quotes[r["index"]][3] for r in results
                   if not (r["exists"] and r["quote_found"] and r["in_force_on_as_of"])]
            if bad:
                raise VisualError(f"{inn}: {len(bad)} line(s) failed check_citations: {bad[:3]}")
        rows.append({"molecule": inn, "rank": mols[inn].get("rank"), "eu_product": out["eu"]["product"]["title"],
                     "eu_label_id": cites["eu"]["doc_id"], "eu_version": cites["eu"]["version"],
                     "eu_decision": out["eu"]["product"].get("decision_number"),
                     "us_label_id": cites["us"]["doc_id"], "us_version": cites["us"]["version"],
                     "us_title": out["us"]["label"]["title"], "us_chosen_by": out["us"]["label"].get("chosen_by"),
                     "us_category": res["us"]["category"], "eu_category": res["eu"]["category"], "cell": res["cell"],
                     "us_items_kept": res["us"]["kept"], "eu_items_kept": res["eu"]["kept"],
                     "us_items_dropped": res["us"]["dropped"], "eu_items_dropped": res["eu"]["dropped"],
                     "citations_checked": len(quotes)})
    in_rows = {r["molecule"] for r in rows}
    orphans = [e for e in entries if e["molecule"] not in in_rows]
    if orphans:
        raise VisualError(f"descriptions table: {', '.join(e['molecule'] for e in orphans)} not in the visual 5 "
                          f"population on {day}; update {US_EU_TABLE.relative_to(ROOT)}")
    if not rows:
        raise VisualError(f"no molecule qualifies for visual 5 on {day}")
    cells = {f"{u}/{e}": sum(1 for r in rows if (r["us_category"], r["eu_category"]) == (u, e))
             for u in CATEGORIES for e in CATEGORIES}
    counts = tally([r["cell"] for r in rows])
    if sum(counts[name] for name in CELL_NAMES) != len(rows) or sum(cells.values()) != len(rows):
        raise VisualError("visual 5: the cells do not add up to the rows")
    n = len(rows)
    title = us_eu_title(counts["one_side_only"], n, day)
    data = {"visual": NAMES["us-vs-eu"], "inputs": {**ctx.inputs(eu=True), "date": day}, "rule": US_EU_RULE,
            "funnel": funnel, "cells": cells, "only_eu": counts["only_eu"],
            "only_eu_us_none": cells["none/lists"], "only_us": counts["only_us"], "both": counts["both"],
            "neither": counts["neither"], "one_side_only": counts["one_side_only"],
            "pct": round(100 * counts["one_side_only"] / n, 1),
            "us_none": sum(1 for r in rows if r["us_category"] == "none"),
            "rule_only": {k: tally(rule_cells)[k] for k in ("only_eu", "only_us", "one_side_only")},
            "descriptions_table": {"path": str(US_EU_TABLE.relative_to(ROOT)), "entries": len(entries),
                                   "matched": sum(1 for e in entries if e["molecule"] in in_rows)},
            "citations_checked": sum(r["citations_checked"] for r in rows),
            "rows": rows, "skipped": skipped,
            "labelled_only_eu": [r["molecule"] for r in rows if r["cell"] == "only_eu"],
            "labelled_only_us": [r["molecule"] for r in rows if r["cell"] == "only_us"],
            "title": title}
    return render_us_vs_eu(data), data


def us_eu_columns(blocks: list, cols: int, size: int, tag: str, gap: float) -> list[float]:
    """Column widths for visual 5: equal columns, unless a name would be cut and columns sized to their widest
    name (the spare width shared out as gaps of at least 12 px) fit every name whole."""
    width = W - 2 * MARGIN
    equal = (width - gap * (cols - 1)) / cols
    need = [0.0] * cols
    for _, block, tagged in blocks:
        per_col = -(-len(block) // cols)
        for i, r in enumerate(block):
            has_tag = tagged and r["us_category"] == "none"
            w = text_width(r["molecule"], size) + (text_width(tag, size) + 10 if has_tag else 0)
            need[i // per_col] = max(need[i // per_col], w)
    if max(need) <= equal or sum(need) + 12 * (cols - 1) > width:
        return [equal] * cols
    return [float(math.ceil(n)) for n in need]   # whole pixels: no float shortfall cuts a name


def render_us_vs_eu(data: dict) -> str:
    """Two blocks of molecule names in three columns, selection-rank order: only the EU SmPC lists a
    contraindication beyond hypersensitivity (a "None." tag where the US section reads "None."), and only the US
    label does; then one muted line with both and neither."""
    rows = data["rows"]
    svg = Svg(data["title"], "Two lists of molecule names: where only the EU SmPC section 4.3 lists a "
                             "contraindication beyond hypersensitivity, and where only the US Contraindications "
                             "section does.")
    fr = draw_frame(svg, data["title"], us_eu_subtitle(data), us_eu_footnote(data), eu_source(data["inputs"]["date"]),
                    stamp(data["inputs"]["build_date"], data["inputs"]["us_head"], data["inputs"]["eu_head"]))
    gap = 28
    blocks = [(f"Only the EU SmPC lists one · {data['only_eu']}", [r for r in rows if r["cell"] == "only_eu"], True),
              (f"Only the US label lists one · {data['only_us']}", [r for r in rows if r["cell"] == "only_us"], False)]
    blocks = [b for b in blocks if b[1]]
    head_h, block_gap = LABEL_PX + 12, 14
    avail = fr.bottom - fr.top - len(blocks) * (head_h + block_gap) - LABEL_PX
    for cols in (3, 4):   # a fourth column only when three leave the rows too tight
        name_rows = sum(-(-len(b) // cols) for _, b, _ in blocks)
        pitch = min(44.0, avail / max(1, name_rows))
        if pitch >= SMALL_PX * 1.15:
            break
    size = LABEL_PX if pitch >= 40 else 24 if pitch >= 34 else SMALL_PX
    if pitch < SMALL_PX * 1.15:
        raise VisualError(f"visual 5: {name_rows} rows of names do not fit ({pitch:.1f} px a row)")
    tag = "None."
    widths = us_eu_columns(blocks, cols, size, tag, gap)
    lefts = [MARGIN + sum(widths[:c]) + c * (W - 2 * MARGIN - sum(widths)) / max(1, cols - 1) for c in range(cols)]
    y0 = fr.top
    for heading, block, tagged in blocks:
        svg.text(MARGIN, y0 + LABEL_PX, heading, LABEL_PX, INK, 600)
        if tagged and any(r["us_category"] == "none" for r in block):
            svg.text(W - MARGIN, y0 + LABEL_PX, f"{tag} = the US section reads \"None.\"", SMALL_PX, ACCENT,
                     anchor="end")
        svg.line(MARGIN, y0 + LABEL_PX + 8, W - MARGIN, y0 + LABEL_PX + 8, HAIRLINE)
        y0 += head_h
        per_col = -(-len(block) // cols)
        for i, r in enumerate(block):
            c, k = divmod(i, per_col)
            x, y, col_w = lefts[c], y0 + k * pitch + size, widths[c]
            has_tag = tagged and r["us_category"] == "none"
            room = col_w - (text_width(tag, size) + 10 if has_tag else 0)
            name = r["molecule"]
            while text_width(name, size) > room and len(name) > 4:
                name = name[:-2] + "…"
            svg.text(x, y, name, size, INK)
            if has_tag:
                svg.text(x + col_w, y, tag, size, ACCENT, anchor="end")
        y0 += per_col * pitch + block_gap
    svg.text(MARGIN, y0 + LABEL_PX * 0.9, f"Both list one: {data['both']} · Neither: {data['neither']}", LABEL_PX,
             MUTED)
    return svg.render()


# ================================================================ visual 1: the example queries (data only)

Q1 = {"label_id": "1e11ad30-1041-4520-10b0-8f9d30d30fcc", "section": "43685-7",
      "sentence": ("Consider the potential for food or drug interactions and adjust the administration or dosage of "
                   "SYNTHROID as needed")}
Q2 = {"from": "2023-01-01", "to": "2025-12-31", "section": "43685-7", "query": "suicidal ideation"}
# Query 3: a contraindications pair where the difference is content, not wording: the US section reads only
# "None." and the EU 4.3 lists contraindications. Bevacizumab (Avastin) first, olanzapine if bevacizumab is not
# that clean on the date. Pembrolizumab indications is the stated fallback (line identity only).
Q3_CANDIDATES = ("bevacizumab", "olanzapine")
Q3 = {"section": "contraindications", "fallback": {"molecule": "pembrolizumab", "section": "indications",
                                                   "us_label_id": "9333c79b-d487-4538-a9f0-71b91a02b287",
                                                   "eu_label_id": "EMEA-H-C-003820"}}
HERO_LOG_FILE = "34066-1-boxed-warning.md"


def query3(ix: labels.Index, eu: labels.EuIndex, day: str) -> dict:
    """The first Q3_CANDIDATES molecule whose US contraindications section is only "None." while its EU 4.3
    lists at least one line, with the exact compare output (both sides' lines verbatim)."""
    tried = []
    for molecule in Q3_CANDIDATES:
        out = labels.compare_jurisdictions(ix, molecule, day, Q3["section"], eu=eu)
        pair = out["pairs"][0] if out["pairs"] else {}
        us_keys = [k for _, k in labels.compare_lines((pair.get("us") or {}).get("text") or "")]
        eu_rest = [ln for ln in (pair.get("only_in_eu") or {}).get("lines") or []
                   if not US_EU_HYPER.search(labels.compare_key(ln))]
        clean = bool(pair.get("us") and pair.get("eu")) and us_keys == [US_NONE_KEY] and bool(eu_rest)
        tried.append({"molecule": molecule, "clean": clean})
        if clean:
            return {"molecule": molecule, "tried": tried, "as_of": day, "section_used": Q3["section"],
                    "us_label_id": out["us"]["label"]["label_id"], "us_title": out["us"]["label"]["title"],
                    "us_version": out["us"]["label"]["version"],
                    "us_published_date": out["us"]["label"]["published_date"],
                    "eu_label_id": out["eu"]["product"]["label_id"], "eu_title": out["eu"]["product"]["title"],
                    "eu_version": out["eu"]["product"]["version"],
                    "eu_decision": out["eu"]["product"]["decision_number"],
                    "eu_decision_date": out["eu"]["product"]["decision_date"],
                    "eu_wording": out["eu"]["product"]["wording"], "shared_lines": pair["shared_lines"],
                    "only_in_us": pair["only_in_us"]["count"], "only_in_eu": pair["only_in_eu"]["count"],
                    "us_lines": pair["only_in_us"]["lines"], "eu_lines": pair["only_in_eu"]["lines"],
                    "fallback": Q3["fallback"]}
    raise VisualError(f"example query 3: no candidate has a US 'None.' against an EU contraindication on {day} "
                      f"({tried}); use the fallback {Q3['fallback']}")


def build_queries(ctx: Context) -> tuple[None, dict]:
    """The answers the README's three example queries must give on this build, from the same library the MCP
    server uses: blame with words, changes_between with a phrase, compare_jurisdictions."""
    ix, eu = ctx.index, ctx.eu
    day = ix.record_to          # the date printed in the README commands, so a reader gets the same as_of
    b = labels.blame(ix, Q1["label_id"], day, Q1["section"], words=True)
    hit = next((ln for ln in b["lines"] if ln["text"].lower().startswith(Q1["sentence"].lower())), None)
    if hit is None:
        raise VisualError("example query 1: the Synthroid sentence is not in the label on the build date")
    words = sorted({(w["version"], w["published_date"]) for w in hit.get("words") or []})
    q1 = {**Q1, "as_of": day, "label_version_read": b["version"], "credited_version": hit["version"],
          "credited_published_date": hit["published_date"], "line": hit["text"],
          "word_credits": [{"version": v_, "published_date": d} for v_, d in words]}
    ch = labels.changes_between(ix, Q2["from"], Q2["to"], section=Q2["section"], query=Q2["query"])
    added = [{"label_id": c["label_id"], "title": c["title"], "version": c["version"],
              "published_date": c["published_date"]} for c in ch["changes"] if c["change"] == "added"]
    q2 = {**Q2, **{k: ch["summary"][k] for k in ("added", "removed", "reworded")},
          "versions_in_window": ch["versions_in_window"], "added_labels": added,
          "first_added": added[0] if added else None, "new_labels_with_phrase": len(ch.get("new_labels") or [])}
    q3 = query3(ix, eu, day)
    cands = hero_candidates(ctx.labels, ctx.cross)
    folder = ix.folder(cands[0]["setid"]) if cands else None
    log_path = f"{folder}/{HERO_LOG_FILE}" if folder else None
    log_lines = len(git(ctx.repo, "log", "--oneline", "--", log_path).splitlines()) if log_path else 0
    data = {"visual": NAMES["queries"], "inputs": ctx.inputs(eu=True), "query1_blame": q1,
            "query2_changes": q2, "query3_compare": q3,
            "git_log_example": {"path": log_path, "commits": log_lines}, "title": "Example queries"}
    return None, data


# ================================================================ output

BUILDERS = {"queries": build_queries, "life": build_life, "churn": build_churn, "rewritten": build_rewritten, "us-vs-eu": build_us_vs_eu}


def write_outputs(ctx: Context, which: list[str], out: Path) -> list[Path]:
    (out / "data").mkdir(parents=True, exist_ok=True)
    written = []
    for key in which:
        svg_text, data = BUILDERS[key](ctx)
        name = NAMES[key]
        svg_path, data_path = out / f"{name}.svg", out / "data" / f"{name}.json"
        if svg_text is not None:
            svg_path.write_text(svg_text, encoding="utf-8")
            written.append(svg_path)
        data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
        written.append(data_path)
    return written


CHROME_PATHS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",)


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise VisualError(f"{path.name} is not a PNG")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def chrome_command(chrome: str, html: Path, png: Path, profile: Path) -> list[str]:
    return [chrome, "--headless", "--disable-gpu", "--disable-extensions", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={profile}", f"--screenshot={png}",
            f"--window-size={2 * W},{2 * H}", "--force-device-scale-factor=1", html.as_uri()]


def render_png(svg_path: Path) -> Path | None:
    """<name>.png at 2x next to the SVG through Chrome headless (local file only); None without Chrome."""
    png = svg_path.with_suffix(".png")
    chrome = find_chrome()
    with tempfile.TemporaryDirectory(prefix="asof-png-") as tmp:
        html = Path(tmp) / "page.html"
        html.write_text(f'<!doctype html><html><head><meta charset="utf-8"><style>html,body{{margin:0;padding:0;'
                        f'background:#fff}}img{{display:block;width:{2 * W}px;height:{2 * H}px}}</style></head>'
                        f'<body><img src="{svg_path.resolve().as_uri()}"></body></html>', encoding="utf-8")
        cmd = chrome_command(chrome or "google-chrome", html, png, Path(tmp) / "profile")
        if chrome is None:
            print("Google Chrome not found; render the PNG with:\n  " + " ".join(cmd), file=sys.stderr)
            return None
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode or not png.exists():
            raise VisualError(f"Chrome failed to render {svg_path.name}: {proc.stderr.strip()[-400:]}")
    size = png_size(png)
    if size != (2 * W, 2 * H):
        raise VisualError(f"{png.name} is {size[0]} x {size[1]}, expected {2 * W} x {2 * H}")
    return png


def check(ctx: Context, which: list[str], out: Path) -> list[str]:
    """Names of the files that differ from a fresh regeneration (missing files count as different)."""
    with tempfile.TemporaryDirectory(prefix="asof-vis-") as tmp:
        fresh = write_outputs(ctx, which, Path(tmp))
        diffs = []
        for f in fresh:
            rel = f.relative_to(tmp)
            committed = out / rel
            if not committed.exists() or not filecmp.cmp(f, committed, shallow=False):
                diffs.append(str(rel))
        return diffs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("visual", choices=["all", *NAMES])
    p.add_argument("--repo", type=Path, default=labels.resolve_us_repo(), help="built US repo (default build/us)")
    p.add_argument("--eu-repo", type=Path, default=None, help="built EU repo (default build/eu)")
    p.add_argument("--molecules", type=Path, default=labels.DEFAULT_MOLECULES, help="crosswalk directory")
    p.add_argument("--date", default=None, help="chart date of visual 5 (default: the US record end)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default visualisations/)")
    p.add_argument("--png", action="store_true", help="also render 2160 x 2700 PNGs with Chrome headless")
    p.add_argument("--check", action="store_true", help="exit 1 if a regenerated SVG or data JSON differs from --out")
    args = p.parse_args(argv)
    eu_repo = args.eu_repo
    if eu_repo is None:
        env = os.environ.get(labels.EU_REPO_ENV)
        eu_repo = Path(env) if env else labels.DEFAULT_EU_REPO
    which = list(NAMES) if args.visual == "all" else [args.visual]
    ctx = Context(args.repo, eu_repo, args.molecules, args.date)
    try:
        if args.check:
            diffs = check(ctx, which, args.out)
            if diffs:
                print("differs from a fresh regeneration: " + ", ".join(diffs), file=sys.stderr)
                return 1
            print(f"ok: {len(which)} visual(s) regenerate byte for byte")
            return 0
        for path in write_outputs(ctx, which, args.out):
            print(path)
            if args.png and path.suffix == ".svg":
                png = render_png(path)
                if png:
                    print(png)
    except (VisualError, labels.LabelError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
