#!/usr/bin/env python3
"""Read the label-history repos: what DailyMed showed on a date (US), which Annex I of a
Commission decision was in force (EU), and who wrote each sentence.

  python tools/cli.py show  2015-01-01 <label> [--section 43685-7 | 4.3]
  python tools/cli.py blame 2024-06-01 <label> --section 34066-1 [--words]
  python tools/cli.py diff  2016-01-01 2018-01-01 <label> [--section 43685-7]
  python tools/cli.py find Synthroid [--jurisdiction all|us|eu]
  python tools/cli.py history <label> --section warnings
  python tools/cli.py changes 2023-01-01 2025-12-31 --section 43685-7 --query "suicidal ideation"
  python tools/cli.py search "suicidal ideation" 2024-01-01 [--section 43685-7] [--jurisdiction eu]
  python tools/cli.py first "suicidal ideation" [--label <label>] [--jurisdiction all]
  python tools/cli.py compare pembrolizumab 2024-06-01 [--section contraindications] [--us-label <setid>]
  python tools/cli.py cite citations.json
  python tools/cli.py coverage
  python tools/cli.py compare-check [--date 2026-11-01] [--out reports/compare-check.json]

The subcommands mirror the MCP tools in tools/mcp_server.py and print JSON,
except show / blame / diff, which print text, and compare-check, which writes a
report. <label> is a US setid (or a unique prefix) or an EU id (EMEA-H-C-nnnnnn,
EMEA/H/C/nnnnnn or EU/1/yy/nnn). US: "on date X" means the newest version whose
Published-Date is on or before X, what DailyMed showed that day, which is not
necessarily the FDA-approved text. EU: the newest Annex I whose Commission
decision date (Effective-Date) is on or before X. Dates come from commit
trailers, never from the commit date. The EU repo is --eu-repo, else env
ASOF_EU_REPO (empty for none), else build/eu when tools/eu.py built it.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labels  # noqa: E402
import notices  # noqa: E402
from labels import BLAME_OPTS, DEFAULT_REPO, NOTICE, Version, pick_section  # noqa: E402,F401

ROOT = labels.ROOT
# The final paragraph of --help: warranty, intended purpose and the rights-holder contact.
LEGAL = textwrap.fill(notices.NO_WARRANTY + " Particula Tech claims no copyright in any label or SmPC text; rights "
                      f"holders and corrections: {notices.CONTACT}.", 79) + "\n"
CliError = labels.LabelError
CHECK_SECTIONS = ("contraindications", "warnings")
CHECK_MIN_PAIRS = 6
CHECK_DATE = "2026-11-01"
CHECK_INFO_DATE = "2015-01-01"
CHECK_OUT = ROOT / "reports" / "compare-check.json"
EU_WORDING_START = "The Union Register carries this text as Annex I of Commission decision"
BANNED_EU_WORDS = ("approved", "fda", "authorised on")

_indexes: dict[Path, labels.Index] = {}
_eu_indexes: dict[tuple, labels.EuIndex | None] = {}


def index(repo: Path, molecules: Path | None = None) -> labels.Index:
    """One Index per repo path for the life of the process."""
    key = Path(repo).resolve()
    if key not in _indexes:
        _indexes[key] = labels.Index(repo, molecules)
    return _indexes[key]


def eu_index(args: argparse.Namespace) -> labels.EuIndex | None:
    """The EU index for --eu-repo / ASOF_EU_REPO / build/eu, loaded once; None when no EU repo is configured."""
    path = labels.resolve_eu_repo(getattr(args, "eu_repo", None))
    key = (str(Path(path).resolve()) if path else None, str(args.molecules))
    if key not in _eu_indexes:
        _eu_indexes[key] = labels.open_eu(path, args.molecules)
    return _eu_indexes[key]


def label_index(args: argparse.Namespace, label_id: str) -> labels.Index:
    """The index a label id belongs to: EU ids go to the EU index (an error when none is loaded)."""
    if labels.is_eu_id(label_id):
        eu = eu_index(args)
        if eu is None:
            raise CliError(labels.EU_REASON)
        return eu
    return index(args.repo, args.molecules)


# Thin wrappers kept for callers that work on plain version lists (tests/test_build.py).

def versions(repo: Path) -> list[Version]:
    """Every label version in the repo, oldest first."""
    return index(repo).versions


def resolve_label(all_versions: list[Version], label_id: str) -> list[Version]:
    key = label_id.lower().removeprefix("dailymed:")
    setids = sorted({v.setid for v in all_versions if v.setid.startswith(key)})
    if not setids:
        raise CliError(f"no label matches {label_id!r}")
    if len(setids) > 1:
        raise CliError(f"{label_id!r} matches several labels: {', '.join(setids)}")
    return [v for v in all_versions if v.setid == setids[0]]


def as_of(label_versions: list[Version], day: str) -> tuple[Version, Version | None]:
    """The version DailyMed showed on ``day`` and the one that replaced it, if any."""
    return labels.as_of_versions(label_versions, day)


def section_files(repo: Path, v: Version) -> list[str]:
    return index(repo).section_files(v)


def header(v: Version, nxt: Version | None, label_versions: list[Version], record_to: str | None = None) -> str:
    until = (f"until {nxt.published} (v{nxt.version})" if nxt
             else f"newest in the record (record ends {record_to or label_versions[-1].published})")
    return (f"DailyMed showed {v.setid} version {v.version} (published {v.published}, "
            f"SPL effective {v.effective}, {v.label_format} format), {until}. "
            f"Record {label_versions[0].published} to {label_versions[-1].published}.")


def text_header(ix: labels.Index, v: Version, nxt: Version | None, day: str | None) -> str:
    """The header line of show / blame: the US line, or the EU wording line."""
    if ix.jurisdiction == "eu":
        return labels.eu_header_line(ix.header(v, day))
    return header(v, nxt, ix.label_versions(v.setid), ix.record_to)


# ---------------------------------------------------------------- text commands

def cmd_show(args: argparse.Namespace) -> None:
    ix = label_index(args, args.label)
    setid = ix.resolve_label(args.label)
    v, nxt = ix.as_of(setid, labels.check_date(args.date))
    files = ix.section_files(v)
    chosen = [ix.pick(files, args.section)] if args.section else files
    print(text_header(ix, v, nxt, args.date))
    note = ix.retired_note(setid, args.date)
    if note:
        print(note)
    for f in chosen:
        print(f"\n=== {Path(f).name}\n")
        print(ix.text(v.commit, f), end="")
    print(f"\n{ix.notice()}")


def cmd_blame(args: argparse.Namespace) -> None:
    ix = label_index(args, args.label)
    setid = ix.resolve_label(args.label)
    v, nxt = ix.as_of(setid, labels.check_date(args.date))
    path = ix.pick(ix.section_files(v), args.section)
    eu_side = ix.jurisdiction == "eu"
    print(text_header(ix, v, nxt, args.date))
    who = "Commission decision" if eu_side else "label version"
    print(f"Each line credited to the {who} that first wrote it, following moves ({Path(path).name}):\n")
    width = 34 if eu_side else 16
    for commit, text, _ in ix.blame_lines(v.commit, path):
        if not text.strip():
            print(f"{'':{width}} |")
            continue
        src = ix.credit(commit)
        if eu_side:
            credit = f"v{src.version:<4} {src.published} {src.decision_number or src.captured_date}"
            print(f"{credit:<{width}} | {text}")
        else:
            print(f"v{src.version:<5} {src.published} | {text}")
        if args.words:
            print(f"{'':{width}} | " + " ".join(f"{w}@v{c.version}" for w, c in ix.word_credits(v, path, text)))
    if args.words:
        print(f"\nWord credit: {labels.WORD_BLAME_METHOD}")
    print(f"\n{ix.notice()}")


def cmd_diff(args: argparse.Namespace) -> None:
    ix = label_index(args, args.label)
    setid = ix.resolve_label(args.label)
    labels.check_date(args.date_a, "date_a")
    labels.check_date(args.date_b, "date_b")
    if args.date_b < args.date_a:
        raise CliError("date_b must not be earlier than date_a")
    a, _ = ix.as_of(setid, args.date_a)
    b, _ = ix.as_of(setid, args.date_b)
    lv = ix.label_versions(setid)
    if ix.jurisdiction == "eu":
        between = lv[lv.index(a) + 1:lv.index(b) + 1]
        print(f"From: {labels.eu_header_line(ix.header(a, args.date_a))}")
        print(f"To: {labels.eu_header_line(ix.header(b, args.date_b))}")
        print("Commission decisions after the first and up to the second: "
              f"{', '.join(f'v{x.version} ({x.decision_number or x.captured_date}, {x.published})' for x in between) or 'none'}.")
    else:
        between = [x for x in lv if a.published < x.published <= b.published]
        print(f"From version {a.version} ({a.published}) to version {b.version} ({b.published}); "
              f"versions published after the first and up to the second: "
              f"{', '.join(f'v{x.version} ({x.published})' for x in between) or 'none'}.")
    if a.commit == b.commit:
        print("Same version on both dates: no differences.")
        return
    tree_paths, renamed = labels.diff_targets(ix.section_files(a), ix.section_files(b), args.section, ix)
    out = ix.git("diff", "--word-diff=plain", "--no-color", a.commit, b.commit, "--", *tree_paths) if tree_paths else ""
    for old, new in renamed:  # the section's file name changed: diff the two blobs as one change
        out += ix.git("diff", "--word-diff=plain", "--no-color", f"{a.commit}:{old}", f"{b.commit}:{new}")
    scope = args.section or "the label"
    print(out if out.strip() else f"No changes to {scope} between these versions.", end="" if out.strip() else "\n")
    print(f"\n{ix.notice()}")


# ---------------------------------------------------------------- JSON commands (mirror the MCP tools)

def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def us(args: argparse.Namespace) -> labels.Index:
    return index(args.repo, args.molecules)


def cmd_find(args: argparse.Namespace) -> None:
    emit(labels.find_label(us(args), args.query, args.jurisdiction, eu=eu_index(args)))


def cmd_history(args: argparse.Namespace) -> None:
    emit(labels.section_history(us(args), args.label, args.section, eu=eu_index(args)))


def cmd_changes(args: argparse.Namespace) -> None:
    emit(labels.changes_between(us(args), args.date_from, args.date_to, args.jurisdiction, args.section, args.query,
                                args.limit, args.compact, eu=eu_index(args)))


def cmd_search(args: argparse.Namespace) -> None:
    emit(labels.search(us(args), args.text, args.date, args.jurisdiction, args.section, args.limit, eu=eu_index(args)))


def cmd_first(args: argparse.Namespace) -> None:
    emit(labels.first_appearance(us(args), args.text, args.label, args.limit, args.jurisdiction, eu=eu_index(args)))


def cmd_compare(args: argparse.Namespace) -> None:
    emit(labels.compare_jurisdictions(us(args), args.molecule, args.date, args.section, eu=eu_index(args),
                                      us_label_id=args.us_label))


def cmd_cite(args: argparse.Namespace) -> None:
    source = Path(args.citations)
    try:
        raw = source.read_text(encoding="utf-8") if source.exists() else args.citations
        citations = json.loads(raw)
    except (OSError, ValueError) as err:
        raise CliError(f"citations must be a JSON list or a path to one: {err}") from err
    emit(labels.check_citations(us(args), citations, eu=eu_index(args)))


def cmd_coverage(args: argparse.Namespace) -> None:
    emit(labels.coverage(us(args), eu_index(args)))


# ---------------------------------------------------------------- compare-check

def _quote_citations(out: dict) -> list[dict]:
    """check_citations items for every only_in_us / only_in_eu line of a compare answer with a section (the
    check asks for the answer with line_cap=None, so the lists hold every line)."""
    items = []
    for pair in out["pairs"]:
        for side, key in (("us", "only_in_us"), ("eu", "only_in_eu")):
            part = pair.get(side)
            if not part or key not in pair:
                continue
            cite = {k: part["citation"][k] for k in ("doc_id", "version", "section", "as_of")}
            items += [{**cite, "quote": line, "side": side} for line in pair[key]["lines"]]
    return items


def _banned_words(text: str) -> list[str]:
    low = text.lower()
    return [w for w in BANNED_EU_WORDS if w in low]


def _no_us_label_reason(ix: labels.Index, inn: str, day: str) -> str:
    """Why compare had no US label: the DailyMed phrase, naming each retired candidate and its last version."""
    retired = labels.us_retired_candidates(ix, ix.molecules().get(inn) or {}, day)
    base = labels.no_us_label_phrase(inn, day)
    if not retired:
        return base + " (a retired label counts until its last published version)"
    return base + "; " + ", ".join(f"{r['brand']} {r['id'][:8]} retired, last published {r['last_published']}"
                                   for r in retired)


def check_molecule(ix: labels.Index, eu: labels.EuIndex, inn: str, day: str) -> dict:
    """compare_jurisdictions for one molecule on ``day``: without a section, then with each CHECK_SECTIONS
    section, every only_in line (no line cap) round-tripped through check_citations; the verdict and why."""
    row: dict = {"date": day, "works": False, "reasons": []}
    started = time.perf_counter()
    try:
        full = labels.compare_jurisdictions(ix, inn, day, eu=eu)
        per = {s: labels.compare_jurisdictions(ix, inn, day, s, eu=eu, line_cap=None) for s in CHECK_SECTIONS}
    except labels.LabelError as err:
        row.update({"reasons": [f"error: {err}"], "compare_seconds": round(time.perf_counter() - started, 3)})
        return row
    row["compare_seconds"] = round(time.perf_counter() - started, 3)
    product, label = full["eu"]["product"], full["us"]["label"]
    row["comparable_to_us"] = full["comparability"]["comparable_to_us"]
    row["pairing_caveat"] = full["comparability"].get("pairing_caveat")
    if product is None:
        row["reasons"].append(f"no EU product: {full['eu']['reason']}")
    elif product.get("not_yet"):
        row["reasons"].append(f"EU record starts {product['product_record_from']}")
    else:
        row.update({"eu_label_id": product["label_id"], "eu_version": product["version"],
                    "eu_decision": product.get("decision_number"), "history_type": product["history_type"],
                    "eu_wording": product["wording"]})
        if product["history_type"] not in ("in-force", "current-only"):
            row["reasons"].append(f"history_type {product['history_type']!r}")
        wording_problems = _banned_words(" ".join(str(product.get(k) or "") for k in
                                                  ("wording", "corrigendum_note", "record_note")))
        if product["history_type"] == "in-force" and not product["wording"].startswith(EU_WORDING_START):
            wording_problems.append("wording start")
        if wording_problems:
            row["reasons"].append(f"EU wording problem: {', '.join(wording_problems)}")
    if label is None:
        row["reasons"].append(_no_us_label_reason(ix, inn, day))
    else:
        row.update({"us_label_id": label["label_id"], "us_version": label["version"], "us_title": label["title"],
                    "us_chosen_by": label["chosen_by"]})
    both = [p["pair"] for p in full["pairs"] if p["us"] and p["eu"]]
    row["pairs_with_both_sides"] = len(both)
    row["pairs_missing_a_side"] = [p["pair"] for p in full["pairs"] if not (p["us"] and p["eu"])]
    if product is not None and not product.get("not_yet") and label is not None and len(both) < CHECK_MIN_PAIRS:
        brand = (label.get("brands") or [label["title"]])[0]
        row["reasons"].append(f"{brand} {label['label_id'][:8]}: {len(both)} of {len(labels.PAIRS)} pairs have text "
                              "on both sides")
    items = [c for s in CHECK_SECTIONS for c in _quote_citations(per[s])]
    row["only_in"] = {s: {"us": per[s]["pairs"][0]["only_in_us"]["count"] if per[s]["pairs"] and
                          "only_in_us" in per[s]["pairs"][0] else 0,
                          "eu": per[s]["pairs"][0]["only_in_eu"]["count"] if per[s]["pairs"] and
                          "only_in_eu" in per[s]["pairs"][0] else 0} for s in CHECK_SECTIONS}
    failures: list[dict] = []
    started = time.perf_counter()
    if items:
        checked = labels.check_citations(ix, [{k: v for k, v in c.items() if k != "side"} for c in items], eu=eu)
        for c, r in zip(items, checked["results"]):
            if not (r["exists"] and r["quote_found"] and r["in_force_on_as_of"]):
                failures.append({"side": c["side"], "doc_id": c["doc_id"], "version": c["version"],
                                 "section": c["section"], "quote": c["quote"][:200], "problems": r["problems"]})
    row["citations_seconds"] = round(time.perf_counter() - started, 3)
    row["citation_lines_checked"] = len(items)
    row["citation_failures"] = failures
    if failures:
        row["reasons"].append(f"{len(failures)} only_in line(s) failed check_citations")
    row["works"] = not row["reasons"]
    return row


def compare_check(ix: labels.Index, eu: labels.EuIndex, dates: list[str], progress=None) -> dict:
    """Run check_molecule for every molecule with a built EU reference on each date; the report dict."""
    metas = eu.all_metadata()
    molecules = sorted({meta.get("molecule") or eu.label_inns(setid)[0]: setid for setid, meta in metas.items()}.items())
    rows: list[dict] = []
    for n, (inn, setid) in enumerate(molecules, start=1):
        meta = metas[setid]
        entry = {"molecule": inn, "eu_label_id": setid, "eu_name": meta.get("name"),
                 "comparable_to_us": meta.get("comparable_to_us"), "comparability_note": meta.get("comparability_note"),
                 "dates": {}}
        for day in dates:
            entry["dates"][day] = check_molecule(ix, eu, inn, day)
        rows.append(entry)
        if progress:
            progress(f"{n}/{len(molecules)} {inn}: " + ", ".join(
                f"{d} {'works' if entry['dates'][d]['works'] else 'no'}" for d in dates))
    summary = {}
    for day in dates:
        verdicts = [(r["comparable_to_us"], r["dates"][day]) for r in rows]
        by_comp: dict[str, dict] = {}
        for comp, v in verdicts:
            slot = by_comp.setdefault(str(comp), {"molecules": 0, "works": 0})
            slot["molecules"] += 1
            slot["works"] += v["works"]
        seconds = [v["compare_seconds"] for _, v in verdicts]
        summary[day] = {
            "molecules": len(verdicts), "works": sum(v["works"] for _, v in verdicts),
            "by_comparable_to_us": dict(sorted(by_comp.items())),
            "citation_lines_checked": sum(v.get("citation_lines_checked", 0) for _, v in verdicts),
            "citation_failures": sum(len(v.get("citation_failures", [])) for _, v in verdicts),
            "compare_seconds_per_molecule": {"median": round(statistics.median(seconds), 3) if seconds else None,
                                             "max": round(max(seconds), 3) if seconds else None},
            "not_working": {r["molecule"]: r["dates"][day]["reasons"] for r in rows if not r["dates"][day]["works"]},
            "works_with_pairing_caveat": sum(1 for _, v in verdicts if v["works"] and v.get("pairing_caveat")),
            "pairing_caveats": {r["molecule"]: {"works": r["dates"][day]["works"],
                                                "caveat": r["dates"][day]["pairing_caveat"]}
                                for r in rows if r["dates"][day].get("pairing_caveat")},
        }
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "acceptance_date": dates[0], "dates": dates, "us_head": ix.head, "eu_head": eu.head,
        "criteria": ("a molecule works on a date when compare_jurisdictions answers without error; eu.product is "
                     "not null with history_type in-force or current-only; us.label is not null; at least "
                     f"{CHECK_MIN_PAIRS} of the {len(labels.PAIRS)} pairs have text on both sides; and every "
                     f"line of only_in_us and only_in_eu for the sections {' and '.join(CHECK_SECTIONS)}, passed to "
                     "check_citations with the citation the answer gives, comes back with exists, quote_found and "
                     "in_force_on_as_of all true (the check asks for every line, without the per-side cap of the "
                     "tool answer). only_in counts are distinct lines after compare_key normalisation; a line "
                     "repeated within a section is listed and counted once. A molecule whose compared US label is "
                     "not the brand its comparability note names carries pairing_caveat and is counted under "
                     "works_with_pairing_caveat as well"),
        "sections_checked": list(CHECK_SECTIONS),
        "only_in_lines_listed_per_side_in_tool_answers": labels.ONLY_IN_MAX_LINES, "lines_round_tripped": "all",
        "summary": summary, "molecules": rows,
    }


def _reason_group(reason: str) -> str:
    """The summary group of a not-working reason (the per-molecule list keeps the full text)."""
    if reason.startswith(("error", "no EU product")):
        return reason.split(":")[0]
    if reason.startswith("EU record starts"):
        return "EU record starts after the date"
    if reason.startswith("DailyMed showed no single-ingredient"):
        return "DailyMed showed no single-ingredient NDA/BLA label of the molecule on the date in the US corpus"
    if re.search(rf"\d+ of {len(labels.PAIRS)} pairs have text on both sides$", reason):
        return f"fewer than {CHECK_MIN_PAIRS} of {len(labels.PAIRS)} pairs have text on both sides"
    return reason


_CAVEAT = re.compile(r"^the US label named in the comparability note \((?P<named>[^,()]+)(?:, setid (?P<setid>[0-9a-f-]+))?\) "
                     r"(?P<why>.+?); compared (?P<chosen>.+) \(setid (?P<chosen_setid>[0-9a-f-]+)\) instead, a different US "
                     r"product$")
_RETIRED = re.compile(r"^is retired; DailyMed last showed it on (?P<last>\d{4}-\d{2}-\d{2}) \(its last published version\), "
                      r"so it is not a candidate on \d{4}-\d{2}-\d{2}$")
_NOT_YET = re.compile(r"^is not yet published on \d{4}-\d{2}-\d{2} \(record starts (?P<first>\d{4}-\d{2}-\d{2})\)$")


def _caveat_row(inn: str, works: bool, caveat: str) -> list[str]:
    """One pairing caveat as table cells: molecule, the label the note names, why it was not compared, the label
    compared instead (setids shortened to 8 characters; the JSON keeps the full text)."""
    m = _CAVEAT.match(caveat)
    molecule = f"{inn} ({'works' if works else 'does not work'})"
    if not m:
        return [molecule, caveat, "", ""]
    named = m["named"] + (f" `{m['setid'][:8]}`" if m["setid"] else "")
    why = m["why"]
    if r := _RETIRED.match(why):
        why = f"retired; last published {r['last']}"
    elif n := _NOT_YET.match(why):
        why = f"not yet published (first version {n['first']})"
    return [molecule, named, why, f"{m['chosen']} `{m['chosen_setid'][:8]}`"]


def compare_check_markdown(report: dict, json_name: str = "compare-check.json") -> str:
    dates = report["dates"]
    lines = ["# compare_jurisdictions check", "",
             f"Generated {report['generated']} by `python3 -I tools/cli.py compare-check`. US corpus HEAD "
             f"`{report['us_head']}`, EU corpus HEAD `{report['eu_head']}`.", "",
             f"Criteria: {report['criteria']}.", "", "## Summary", "",
             "| Date | Molecules | Works | Works with pairing caveat | yes | unknown | no | only_in lines checked | "
             "Citation failures | Median s | Max s |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for day in dates:
        s = report["summary"][day]
        comp = s["by_comparable_to_us"]
        cell = lambda k: f"{comp.get(k, {}).get('works', 0)} of {comp.get(k, {}).get('molecules', 0)}"
        t = s["compare_seconds_per_molecule"]
        lines.append(f"| {day} | {s['molecules']} | {s['works']} | {s['works_with_pairing_caveat']} | {cell('yes')} | "
                     f"{cell('unknown')} | {cell('no')} | "
                     f"{s['citation_lines_checked']} | {s['citation_failures']} | {t['median']} | {t['max']} |")
    lines += ["", "The columns yes, unknown and no split the molecules by comparable_to_us of the EU reference "
              "product. Times are the three compare_jurisdictions calls per molecule (no section, then each checked "
              "section).", ""]
    for day in dates:
        caveats = report["summary"][day]["pairing_caveats"]
        lines += [f"## Pairing caveats on {day}", ""]
        if not caveats:
            lines += ["None.", ""]
        else:
            n_works = report["summary"][day]["works_with_pairing_caveat"]
            lines += [f"{len(caveats)} {_plural(len(caveats), 'molecule')} compare a US label other than the brand the "
                      f"comparability note names ({n_works} of them count as working). Each compared label is a "
                      "different US product; the full text of every caveat is in the JSON.", ""]
            lines += ["| Molecule | Label the note names | Why it was not compared | Compared instead |",
                      "| --- | --- | --- | --- |"]
            lines += ["| " + " | ".join(_caveat_row(inn, c["works"], c["caveat"])) + " |"
                      for inn, c in sorted(caveats.items())]
            lines.append("")
    for day in dates:
        missing = report["summary"][day]["not_working"]
        lines += [f"## Not working on {day}", ""]
        if not missing:
            lines += ["None.", ""]
            continue
        counts: dict[str, int] = {}
        for reasons in missing.values():
            for r in reasons:
                key = _reason_group(r)
                counts[key] = counts.get(key, 0) + 1
        lines += [f"{len(missing)} {_plural(len(missing), 'molecule')}. Reasons (a molecule can have several):", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        lines.append("")
        if len(missing) <= 60:
            lines += [f"- {inn}: {'; '.join(reasons)}" for inn, reasons in sorted(missing.items())]
            lines.append("")
    lines += ["## Molecules", "",
              f"One entry per molecule ({len(report['molecules']):,}) is in `{json_name}` under `molecules`: the EU "
              "product, comparable_to_us, the US label compared, pairs with text on both sides, only_in lines checked, "
              "citation failures and the verdict with its reasons on each date."]
    return "\n".join(lines) + "\n"


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"


def cmd_compare_check(args: argparse.Namespace) -> None:
    eu = eu_index(args)
    if eu is None:
        raise CliError(labels.EU_REASON)
    dates = [labels.check_date(args.date)]
    if args.info_date and args.info_date != args.date:
        dates.append(labels.check_date(args.info_date, "info_date"))
    log = (lambda text: print(text, file=sys.stderr, flush=True)) if args.progress else None
    report = compare_check(us(args), eu, dates, log)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(compare_check_markdown(report, out.name), encoding="utf-8")
    for day in dates:
        s = report["summary"][day]
        comp = ", ".join(f"{k} {v['works']} of {v['molecules']}" for k, v in s["by_comparable_to_us"].items())
        print(f"{day}: {s['works']} of {s['molecules']} molecules work ({comp}; "
              f"{s['works_with_pairing_caveat']} with a pairing caveat); {s['citation_lines_checked']} "
              f"only_in lines checked, {s['citation_failures']} failed; compare median "
              f"{s['compare_seconds_per_molecule']['median']} s, max {s['compare_seconds_per_molecule']['max']} s")
    print(f"wrote {out} and {out.with_suffix('.md')}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ + "\n" + LEGAL, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=labels.resolve_us_repo(),
                   help="built US repo (env ASOF_US_REPO; default us/ when it is a git repo, else build/us)")
    p.add_argument("--molecules", type=Path, default=labels.DEFAULT_MOLECULES, help="crosswalk directory")
    p.add_argument("--eu-repo", default=None,
                   help="built EU repo (env ASOF_EU_REPO, empty for none; default build/eu when tools/eu.py built it)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="label text in force on a date (US: what DailyMed showed; EU: Annex I in force)")
    s.add_argument("date")
    s.add_argument("label")
    s.add_argument("--section", help="US: LOINC code or word, e.g. 43685-7 or warnings; EU: QRD number, e.g. 4.3")
    s.set_defaults(func=cmd_show)
    b = sub.add_parser("blame", help="credit each line of a section to the version that wrote it")
    b.add_argument("date")
    b.add_argument("label")
    b.add_argument("--section", required=True)
    b.add_argument("--words", action="store_true", help="also credit each word")
    b.set_defaults(func=cmd_blame)
    d = sub.add_parser("diff", help="word-level diff between the versions in force on two dates")
    d.add_argument("date_a")
    d.add_argument("date_b")
    d.add_argument("label")
    d.add_argument("--section")
    d.set_defaults(func=cmd_diff)
    f = sub.add_parser("find", help="find labels by id, name, INN, NDC, RxCUI, EMA or EU number (JSON)")
    f.add_argument("query")
    f.add_argument("--jurisdiction", default=None, choices=["us", "eu", "all"],
                   help="all (default when an EU repo is loaded), us or eu")
    f.set_defaults(func=cmd_find)
    h = sub.add_parser("history", help="every version that changed one section (JSON)")
    h.add_argument("label")
    h.add_argument("--section", required=True)
    h.set_defaults(func=cmd_history)
    c = sub.add_parser("changes", help="every label change in a date window, optionally tracking a phrase (JSON)")
    c.add_argument("date_from")
    c.add_argument("date_to")
    c.add_argument("--jurisdiction", choices=["us", "eu", "all"], help="us (default), eu or all")
    c.add_argument("--section", help="US: LOINC code, e.g. 43685-7; EU: QRD number, e.g. 4.4")
    c.add_argument("--query", help="phrase whose addition, removal or rewording to report")
    c.add_argument("--limit", type=int, default=None,
                   help=f"rows to print (default {labels.DEFAULT_LIMIT} with --query, {labels.DEFAULT_LIMIT_ALL} without)")
    c.add_argument("--compact", action="store_true", help="section codes and counts only")
    c.set_defaults(func=cmd_changes)
    se = sub.add_parser("search", help="labels whose text in force on a date contains a phrase (JSON)")
    se.add_argument("text")
    se.add_argument("date")
    se.add_argument("--jurisdiction", choices=["us", "eu", "all"], help="us (default), eu or all")
    se.add_argument("--section", help="US: LOINC code, e.g. 43685-7; EU: QRD number, e.g. 4.4")
    se.add_argument("--limit", type=int, default=labels.DEFAULT_LIMIT)
    se.set_defaults(func=cmd_search)
    fi = sub.add_parser("first", help="first version containing a phrase, per label or corpus-wide (JSON)")
    fi.add_argument("text")
    fi.add_argument("--label")
    fi.add_argument("--jurisdiction", choices=["us", "eu", "all"],
                    help="corpus without --label: us (default), eu or all; with --label it must agree")
    fi.add_argument("--limit", type=int, default=labels.DEFAULT_LIMIT)
    fi.set_defaults(func=cmd_first)
    co = sub.add_parser("compare", help="US label versus EU SmPC of one molecule on a date, by topic (JSON)")
    co.add_argument("molecule")
    co.add_argument("date")
    co.add_argument("--section", help="LOINC code, QRD number or pair word, e.g. contraindications")
    co.add_argument("--us-label", help="US setid to compare instead of the primary NDA/BLA pick")
    co.set_defaults(func=cmd_compare)
    ci = sub.add_parser("cite", help="check citations (JSON list or file of {doc_id, version, section, quote, as_of})")
    ci.add_argument("citations")
    ci.set_defaults(func=cmd_cite)
    cv = sub.add_parser("coverage", help="scope, record window and history type of each corpus (JSON)")
    cv.set_defaults(func=cmd_coverage)
    cc = sub.add_parser("compare-check", help="compare_jurisdictions for every molecule with a built EU reference, "
                                              "with a citation round trip; writes a JSON and Markdown report")
    cc.add_argument("--date", default=CHECK_DATE, help=f"acceptance date (default {CHECK_DATE})")
    cc.add_argument("--info-date", default=CHECK_INFO_DATE,
                    help=f"second date reported for information (default {CHECK_INFO_DATE}; empty for none)")
    cc.add_argument("--out", default=str(CHECK_OUT), help="JSON report path; the .md goes next to it")
    cc.add_argument("--progress", action="store_true", help="print one line per molecule to stderr")
    cc.set_defaults(func=cmd_compare_check)
    args = p.parse_args(argv)
    try:
        args.func(args)
    except (CliError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
