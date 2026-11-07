"""SPL (HL7 v3 Structured Product Labeling) to section Markdown.

Pure functions, standard library only. Changing anything that alters the
output changes the blame, so bump CONVERTER_VERSION with every such change.

Rules (also recorded in reproduce.md):
- One file per top-level section, keyed by its LOINC code: ``{code}-{slug}.md``.
- Old-format WARNINGS (34071-1) and PRECAUTIONS (42232-9) both map onto
  43685-7 WARNINGS AND PRECAUTIONS, so history continues across the switch to
  the PLR format.
- A boxed warning (34066-1) is lifted out wherever it sits (old-format labels
  nest it inside WARNINGS) so it always has its own file.
- Top-level sections that share a code (e.g. repeated package-label panels)
  are joined into one file in document order. Uncoded sections count as
  42229-5 (SPL unclassified section).
- SPL product data elements (48780-1), Highlights excerpts and images are
  not label text and are skipped.
- One sentence per line; tables become pipe tables.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

CONVERTER_VERSION = "2"

NS = "{urn:hl7-org:v3}"
BOXED = "34066-1"
UNCLASSIFIED = "42229-5"
PRODUCT_DATA = "48780-1"
WARNINGS_AND_PRECAUTIONS = "43685-7"
CODE_MAP = {"34071-1": WARNINGS_AND_PRECAUTIONS, "42232-9": WARNINGS_AND_PRECAUTIONS}
SKIP_CODES = {PRODUCT_DATA}
PLR_MARKERS = {WARNINGS_AND_PRECAUTIONS, "43678-2"}  # W&P, Dosage forms and strengths

SLUGS = {
    "34066-1": "boxed-warning",
    "43683-2": "recent-major-changes",
    "34067-9": "indications-and-usage",
    "34068-7": "dosage-and-administration",
    "43678-2": "dosage-forms-and-strengths",
    "34070-3": "contraindications",
    "43685-7": "warnings-and-precautions",
    "34084-4": "adverse-reactions",
    "34073-7": "drug-interactions",
    "43684-0": "use-in-specific-populations",
    "42227-9": "drug-abuse-and-dependence",
    "34088-5": "overdosage",
    "34089-3": "description",
    "34090-1": "clinical-pharmacology",
    "43680-8": "nonclinical-toxicology",
    "34092-7": "clinical-studies",
    "34093-5": "references",
    "34069-5": "how-supplied-storage-and-handling",
    "34076-0": "patient-counseling-information",
    "42230-3": "patient-package-insert",
    "42231-1": "medication-guide",
    "59845-8": "instructions-for-use",
    "51945-4": "package-label",
    "42229-5": "unclassified",
}

# Tokens that end with a period but do not end a sentence (compared lowercase,
# without the trailing period).
ABBREVIATIONS = {
    "e.g", "i.e", "etc", "vs", "approx", "dr", "no", "nos", "u.s", "u.s.a", "fig",
    "figs", "inc", "ltd", "co", "corp", "st", "mr", "ms", "mrs", "ref", "refs",
    "vol", "al", "ca", "min", "max", "sec", "hr", "hrs", "wk", "wks", "mo",
}
_BREAK = re.compile(r"(?<=[.!?])([\"'”’)\]]*)\s+(?=[A-Z0-9\"“(\[])")


class SplError(ValueError):
    """The document is not a usable SPL label."""


@dataclass
class Section:
    code: str
    blocks: list[str] = field(default_factory=list)


@dataclass
class Label:
    setid: str
    version: int
    effective_time: str
    doc_title: str
    label_format: str  # "PLR" or "old"
    metadata: dict
    files: dict[str, str]  # file name -> Markdown


def _t(tag: str) -> str:
    return NS + tag


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _code_of(section: ET.Element) -> str:
    code = section.find(_t("code"))
    value = code.get("code") if code is not None else None
    return value.strip() if value and value.strip() else UNCLASSIFIED


def _display_name(section: ET.Element) -> str:
    code = section.find(_t("code"))
    return (code.get("displayName") or "") if code is not None else ""


def slug_for(code: str, display_name: str = "") -> str:
    if code in SLUGS:
        return SLUGS[code]
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    return slug or "section"


def split_sentences(text: str) -> list[str]:
    """Split collapsed prose into sentences, one per list entry."""
    text = _collapse(text)
    if not text:
        return []
    out, start = [], 0
    for m in _BREAK.finditer(text):
        end = m.start() + len(m.group(1))
        candidate = text[start:end]
        last_token = candidate.rsplit(" ", 1)[-1].rstrip(".!?\"'”’)]").lower()
        if last_token in ABBREVIATIONS or re.fullmatch(r"[a-z]", last_token):
            continue
        out.append(candidate.strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _inline(el: ET.Element, skip: tuple[str, ...] = ()) -> str:
    """Text of an inline container, skipping images, footnote markers and any
    direct-child tags named in ``skip``."""
    parts: list[str] = []

    def keep(text: str | None) -> bool:
        # Whitespace-only text that contains a line break is the XML's pretty-printing
        # between tags; DailyMed's renderer drops it ("Ag" + "e" reads "Age").
        return bool(text) and not (text.isspace() and "\n" in text)

    def walk(node: ET.Element, top: bool = False) -> None:
        if keep(node.text):
            parts.append(node.text)
        for child in node:
            tag = child.tag.replace(NS, "")
            if top and tag in skip:
                pass
            elif tag == "br":
                parts.append(" ")
            elif tag in ("renderMultiMedia", "footnoteRef"):
                pass
            elif tag in ("list", "table"):
                parts.append(" ")  # block content inside inline content: flatten
                walk(child)
                parts.append(" ")
            else:
                walk(child)
            if keep(child.tail):
                parts.append(child.tail)

    walk(el, top=True)
    text = _collapse("".join(parts))
    # Markup boundaries (links, bold runs) leave spaces inside brackets and
    # before punctuation; they are artefacts, not label text.
    text = re.sub(r"\s+([)\],.;:])", r"\1", text)
    return re.sub(r"([(\[])\s+", r"\1", text)


def _table(table: ET.Element) -> list[str]:
    rows: list[list[str]] = []
    header_rows = 0
    for part in ("thead", "tbody", "tfoot"):
        for grp in table.findall(_t(part)):
            for tr in grp.findall(_t("tr")):
                cells: list[str] = []
                for cell in tr:
                    if cell.tag not in (_t("td"), _t("th")):
                        continue
                    text = _inline(cell).replace("|", "\\|")
                    cells.append(text)
                    span = cell.get("colspan")
                    if span and span.isdigit():
                        cells.extend([""] * (int(span) - 1))
                rows.append(cells)
                if part == "thead":
                    header_rows += 1
    for tr in table.findall(_t("tr")):  # rows directly under <table>
        rows.append([_inline(c).replace("|", "\\|") for c in tr if c.tag in (_t("td"), _t("th"))])
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = rows[0] if header_rows == 0 else rows[header_rows - 1]
    body = rows[1:] if header_rows == 0 else rows[:header_rows - 1] + rows[header_rows:]
    lines = ["| " + " | ".join(head) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    blocks = []
    caption = table.find(_t("caption"))
    if caption is not None and _inline(caption):
        blocks.append("\n".join(split_sentences(_inline(caption))))
    blocks.append("\n".join(lines))
    return blocks


def _list(lst: ET.Element, indent: int = 0) -> str:
    ordered = lst.get("listType") == "ordered"
    lines = []
    for n, item in enumerate(lst.findall(_t("item")), start=1):
        marker = f"{n}. " if ordered else "- "
        pad = " " * indent
        sentences = split_sentences(_inline(item, skip=("list",))) or [""]
        lines.append(pad + marker + sentences[0])
        lines += [pad + " " * len(marker) + s for s in sentences[1:]]
        for nested in item.findall(_t("list")):  # nested lists go after the item text
            lines.append(_list(nested, indent + len(marker)))
    return "\n".join(lines)


def _text_blocks(text: ET.Element) -> list[str]:
    blocks: list[str] = []
    loose: list[str] = []

    def flush() -> None:
        if loose:
            sentences = split_sentences(" ".join(loose))
            if sentences:
                blocks.append("\n".join(sentences))
            loose.clear()

    if text.text and text.text.strip():
        loose.append(text.text)
    for child in text:
        tag = child.tag.replace(NS, "")
        if tag == "paragraph":
            flush()
            sentences = split_sentences(_inline(child))
            if sentences:
                blocks.append("\n".join(sentences))
        elif tag == "list":
            flush()
            blocks.append(_list(child))
        elif tag == "table":
            flush()
            blocks.extend(_table(child))
        elif tag in ("renderMultiMedia",):
            pass
        else:
            loose.append(_inline(child))
        if child.tail and child.tail.strip():
            loose.append(child.tail)
    flush()
    return [b for b in blocks if b.strip()]


def _render(section: ET.Element, depth: int, lifted: list[tuple[str, list[str]]]) -> list[str]:
    """Render a section and its subsections. Boxed warnings found below the top
    level are appended to ``lifted`` instead of being rendered in place."""
    blocks: list[str] = []
    title = section.find(_t("title"))
    title_text = _inline(title) if title is not None else ""
    # Section numbers split across markup runs ("5." + "3") read "5. 3"; join them.
    title_text = re.sub(r"^(\d+)\.\s+(\d+)\b", r"\1.\2", title_text)
    if title_text:
        blocks.append("#" * min(depth, 6) + " " + title_text)
    for child in section:
        tag = child.tag.replace(NS, "")
        if tag == "text":
            blocks.extend(_text_blocks(child))
        elif tag == "component":
            sub = child.find(_t("section"))
            if sub is None:
                continue
            if _code_of(sub) == BOXED:
                lifted.append((BOXED, _render(sub, 1, lifted)))
            else:
                blocks.extend(_render(sub, depth + 1, lifted))
    return blocks


def _metadata(root: ET.Element) -> dict:
    labeler = root.findtext(f"{_t('author')}/{_t('assignedEntity')}/{_t('representedOrganization')}/{_t('name')}")
    products, ndcs, apps = set(), set(), set()
    for mp in root.iter(_t("manufacturedProduct")):
        inner = mp.find(_t("manufacturedProduct"))
        if inner is None:
            continue
        name = _collapse(inner.findtext(_t("name")) or "")
        generic = _collapse(inner.findtext(f"{_t('asEntityWithGeneric')}/{_t('genericMedicine')}/{_t('name')}") or "")
        form = inner.find(_t("formCode"))
        products.add((name, generic.lower(), (form.get("displayName") or "") if form is not None else ""))
    for code in root.iter(_t("code")):
        if code.get("codeSystem") == "2.16.840.1.113883.6.69" and code.get("code", "").count("-") == 2:
            ndcs.add(code.get("code"))
    for appr in root.iter(_t("approval")):
        ident = appr.find(_t("id"))
        if ident is not None and ident.get("extension"):
            apps.add(ident.get("extension"))
    return {
        "labeler": _collapse(labeler or ""),
        "products": [{"name": n, "generic": g, "form": f} for n, g, f in sorted(products)],
        "package_ndcs": sorted(ndcs),
        "application_numbers": sorted(apps),
    }


def convert(xml_bytes: bytes) -> Label:
    """Convert one SPL document to a Label with one Markdown file per section."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as err:
        raise SplError(f"not well-formed XML: {err}") from err
    if root.tag != _t("document"):
        raise SplError(f"root element is {root.tag!r}, expected an HL7 v3 document")
    setid_el, ver_el, eff_el = root.find(_t("setId")), root.find(_t("versionNumber")), root.find(_t("effectiveTime"))
    if setid_el is None or ver_el is None or eff_el is None:
        raise SplError("missing setId, versionNumber or effectiveTime")
    body = root.find(f"{_t('component')}/{_t('structuredBody')}")
    if body is None:
        raise SplError("missing structuredBody")

    top_codes: list[str] = []
    sections: dict[str, Section] = {}
    order: list[str] = []
    names: dict[str, str] = {}
    lifted: list[tuple[str, list[str]]] = []

    def add(code: str, blocks: list[str], display: str = "") -> None:
        if code not in sections:
            sections[code] = Section(code)
            order.append(code)
            names[code] = display
        sections[code].blocks.extend(blocks)

    for comp in body.findall(_t("component")):
        sec = comp.find(_t("section"))
        if sec is None:
            continue
        raw_code = _code_of(sec)
        top_codes.append(raw_code)
        if raw_code in SKIP_CODES:
            continue
        code = CODE_MAP.get(raw_code, raw_code)
        before = len(lifted)
        blocks = _render(sec, 1, lifted)
        if raw_code == BOXED:
            add(BOXED, blocks, _display_name(sec))
        else:
            # Lifted boxed warnings go first so the boxed file reads in document order.
            for lcode, lblocks in lifted[before:]:
                add(lcode, lblocks)
            add(code, blocks, _display_name(sec))
        del lifted[before:]

    files = {}
    for code in order:
        content = "\n\n".join(b for b in sections[code].blocks if b.strip())
        if content.strip():
            files[f"{code}-{slug_for(code, names.get(code, ''))}.md"] = content + "\n"

    title = root.find(_t("title"))
    return Label(
        setid=(setid_el.get("root") or "").lower(),
        version=int(ver_el.get("value")),
        effective_time=(eff_el.get("value") or "")[:8],
        doc_title=_inline(title) if title is not None else "",
        label_format="PLR" if PLR_MARKERS & set(top_codes) else "old",
        metadata=_metadata(root),
        files=files,
    )
