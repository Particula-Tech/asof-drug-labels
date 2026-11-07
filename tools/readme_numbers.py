#!/usr/bin/env python3
"""Every number in README.md, regenerated from committed JSON (standard library only).

  python3 -I tools/readme_numbers.py --check    # exit 1 naming drifted keys or a number outside a marker
  python3 -I tools/readme_numbers.py --write    # rewrite README.md in place
  python3 -I tools/readme_numbers.py --list     # print every key with its value and source

A number in README.md is written inside a marker:

  <!-- n:us_labels -->5,393<!-- /n -->

The marker comments do not render, so the README reads as plain text. Keys come only from files
committed in the top repo (never from build/us or build/eu), so --check runs anywhere, including CI
without the corpus:

  manifest.json                         project, server_version, us_* and eu_* corpus figures
  molecules/*.json                      molecules (file count, selection.json excluded), eu_no_reference,
                                        eu_no_central, eu_only_combination
  molecules/selection.json              cap_nda_bla, cap_generic
  reports/coverage-us.json              us_logged_no_file, us_logged_corrupt (listed, logged and fetched_pct
                                        come from manifest.json, refreshed by every daily run)
  reports/eu-coverage.json              eu_decisions_listed, eu_annexes_*
  reports/compare-check.json            compare_*
  reports/srlc-validation.json          srlc_* (the SrLC result)
  reports/srlc-population.json          srlc_population (all sections), srlc_population_sections (the six sampled
                                        sections), srlc_eligible, srlc_eligible_applications
  sources/removed.json                  removed_total (labels and products removed on request)
  tools/tests/known_readings.json       known_readings_us
  tools/tests/eu_known_readings.json    known_readings_eu
  visualisations/data/*.json            title_*, life_*, churn_*, rewritten_*, us_eu_*, q1_* .. q3_*

Integers are printed with thousands separators (years and version numbers in PLAIN are not). --check also fails on any thousands-separated number
(\\d{1,3}(,\\d{3})+) and on any bare integer of 4 or more digits found outside a marker (years 1990-2099 and
ALLOWLIST excepted; code, URLs and link targets are not prose), which catches a number pasted by hand. Run
--list for the full key table; KEYS below is the authority.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MARKER = re.compile(r"<!-- n:([a-z0-9_]+) -->(.*?)<!-- /n -->", re.S)
THOUSANDS = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
VIS = "visualisations/data/"


class NumbersError(RuntimeError):
    pass


def _get(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, list):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise NumbersError(f"missing field {path!r}")
    return data


def fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def pct(fraction: float) -> int:
    return round(100 * fraction)


# key -> (source file relative to the root, field path or function of the loaded JSON)
Getter = str | Callable[[Any], Any]
KEYS: dict[str, tuple[str, Getter]] = {
    # manifest.json
    "project": ("manifest.json", "project"),
    "server_version": ("manifest.json", "server_version"),
    "us_labels": ("manifest.json", "corpora.us.labels"),
    "us_versions": ("manifest.json", "corpora.us.commits"),
    "us_retired_labels": ("manifest.json", "corpora.us.retired_labels"),
    "us_molecule_folders": ("manifest.json", "corpora.us.molecule_folders"),
    "us_record_from": ("manifest.json", "corpora.us.record_from"),
    "us_record_to": ("manifest.json", "corpora.us.record_to"),
    "us_build_date": ("manifest.json", "corpora.us.build_date"),
    "us_head": ("manifest.json", lambda d: d["corpora"]["us"]["head"][:12]),
    "us_converter": ("manifest.json", "corpora.us.converter"),
    "dailymed_db_published": ("manifest.json", "corpora.us.dailymed_db_published"),
    "eu_products": ("manifest.json", "corpora.eu.products"),
    "eu_versions": ("manifest.json", "corpora.eu.commits"),
    "eu_corrigenda": ("manifest.json", "corpora.eu.corrigenda"),
    "eu_record_from": ("manifest.json", "corpora.eu.record_from"),
    "eu_record_to": ("manifest.json", "corpora.eu.record_to"),
    "eu_build_date": ("manifest.json", "corpora.eu.build_date"),
    "eu_head": ("manifest.json", lambda d: d["corpora"]["eu"]["head"][:12]),
    "eu_converter": ("manifest.json", "corpora.eu.converter"),
    # molecules/
    "molecules": ("molecules", "count"),
    "cap_nda_bla": ("molecules/selection.json", "parameters.cap_nda"),
    "cap_generic": ("molecules/selection.json", "parameters.cap_generic"),
    "eu_no_reference": ("molecules", "no_reference"),
    "eu_no_central": ("molecules", "no_central"),
    "eu_only_combination": ("molecules", "only_combination"),
    # US listing counts: manifest.json recomputes them from sources/us on every write (also in the daily job),
    # so they move together with us_versions; reports/coverage-us.json stays the build-time report
    "us_listed_versions": ("manifest.json", "corpora.us.listed_versions"),
    "us_logged_versions": ("manifest.json", "corpora.us.logged_versions"),
    "us_fetched_pct": ("manifest.json", "corpora.us.fetched_pct"),
    # US coverage (build-time reasons)
    "us_logged_no_file": ("reports/coverage-us.json",
                          "problem_reasons.listed in /history, but the archive has no file"),
    "us_logged_corrupt": ("reports/coverage-us.json", "problem_reasons.File is not a zip file"),
    # EU coverage
    "eu_decisions_listed": ("reports/eu-coverage.json", "totals.decisions_listed"),
    "eu_annexes_listed": ("reports/eu-coverage.json", "totals.annexes_en_listed"),
    "eu_annexes_skipped": ("reports/eu-coverage.json", "totals.annexes_skipped"),
    "eu_annexes_not_fetched": ("reports/eu-coverage.json", "totals.annexes_not_fetched"),
    # compare-check
    "compare_date": ("reports/compare-check.json", "acceptance_date"),
    "compare_molecules": ("reports/compare-check.json", lambda d: d["summary"][d["acceptance_date"]]["molecules"]),
    "compare_works": ("reports/compare-check.json", lambda d: d["summary"][d["acceptance_date"]]["works"]),
    "compare_not_working": ("reports/compare-check.json",
                            lambda d: d["summary"][d["acceptance_date"]]["molecules"]
                            - d["summary"][d["acceptance_date"]]["works"]),
    "compare_lines_checked": ("reports/compare-check.json",
                              lambda d: d["summary"][d["acceptance_date"]]["citation_lines_checked"]),
    "compare_citation_failures": ("reports/compare-check.json",
                                  lambda d: d["summary"][d["acceptance_date"]]["citation_failures"]),
    "compare_info_date": ("reports/compare-check.json", "dates.1"),
    "compare_info_works": ("reports/compare-check.json", lambda d: d["summary"][d["dates"][1]]["works"]),
    # SrLC validation (frozen)
    "srlc_generated": ("reports/srlc-validation.json", "generated"),
    "srlc_matches": ("reports/srlc-validation.json", "headline.matches"),
    "srlc_items": ("reports/srlc-validation.json", "headline.items"),
    "srlc_rate_pct": ("reports/srlc-validation.json", lambda d: pct(d["headline"]["match_rate"])),
    "srlc_window_days": ("reports/srlc-validation.json", "constants.WINDOW_DAYS"),
    "srlc_min_quote_tokens": ("reports/srlc-validation.json", "constants.MIN_QUOTE_TOKENS"),
    "srlc_first_year": ("reports/srlc-validation.json", lambda d: d["constants"]["YEARS"][0]),
    "srlc_last_year": ("reports/srlc-validation.json", lambda d: d["constants"]["YEARS"][-1]),
    "srlc_median_lag": ("reports/srlc-validation.json", "lag_stats_matches.median"),
    "srlc_lag_q1": ("reports/srlc-validation.json", "lag_stats_matches.q1"),
    "srlc_lag_q3": ("reports/srlc-validation.json", "lag_stats_matches.q3"),
    "srlc_lag_min_before": ("reports/srlc-validation.json", lambda d: -d["lag_stats_matches"]["min"]),
    "srlc_lag_max": ("reports/srlc-validation.json", "lag_stats_matches.max"),
    "srlc_misses": ("reports/srlc-validation.json", lambda d: d["headline"]["items"] - d["headline"]["matches"]),
    "srlc_early": ("reports/srlc-validation.json", "by_category.early"),
    "srlc_late": ("reports/srlc-validation.json", "by_category.late"),
    "srlc_absent": ("reports/srlc-validation.json", "by_category.absent"),
    "srlc_strict": ("reports/srlc-validation.json", "sensitivity.strict.matches"),
    "srlc_loose": ("reports/srlc-validation.json", "sensitivity.loose.matches"),
    "srlc_window_365": ("reports/srlc-validation.json", "sensitivity.window_365.matches"),
    "srlc_window_365_days": ("reports/srlc-validation.json", "sensitivity.window_365.window_days"),
    "srlc_replaced": ("reports/srlc-validation.json", lambda d: len(d["sample"]["revisions"][0]["changes"])),
    "srlc_population": ("reports/srlc-population.json", "funnel.in_range"),
    "srlc_population_sections": ("reports/srlc-population.json", "funnel.sampled_section"),
    "srlc_eligible": ("reports/srlc-population.json", "funnel.eligible"),
    "srlc_eligible_applications": ("reports/srlc-population.json", "eligible_applications"),
    # removed on request
    "removed_total": ("sources/removed.json", lambda d: len(d["removed"])),
    # known readings
    "known_readings_us": ("tools/tests/known_readings.json", lambda d: len(d["readings"])),
    "known_readings_eu": ("tools/tests/eu_known_readings.json", lambda d: len(d["readings"])),
    # visuals
    "title_life": (VIS + "02-life-of-a-label.json", "title"),
    "title_churn": (VIS + "03-label-churn.json", "title"),
    "title_rewritten": (VIS + "04-most-rewritten.json", "title"),
    "title_us_vs_eu": (VIS + "05-us-vs-eu-contraindications.json", "title"),
    "life_name": (VIS + "02-life-of-a-label.json", "hero.display_name"),
    "life_setid": (VIS + "02-life-of-a-label.json", "hero.setid"),
    "life_versions": (VIS + "02-life-of-a-label.json", "hero.versions"),
    "life_boxed_added": (VIS + "02-life-of-a-label.json", "hero.added_date"),
    "life_boxed_removed": (VIS + "02-life-of-a-label.json", "hero.removed_date"),
    "churn_peak_year": (VIS + "03-label-churn.json", "peak.year"),
    "churn_peak_rate": (VIS + "03-label-churn.json", lambda d: f"{d['peak']['clinical_per_label']:.1f}"),
    "churn_last_year": (VIS + "03-label-churn.json", "last.year"),
    "churn_last_rate": (VIS + "03-label-churn.json", lambda d: f"{d['last']['clinical_per_label']:.1f}"),
    "rewritten_top_name": (VIS + "04-most-rewritten.json", "rows.0.display_name"),
    "rewritten_top_versions": (VIS + "04-most-rewritten.json", "rows.0.versions"),
    "us_eu_molecules": (VIS + "05-us-vs-eu-contraindications.json", lambda d: len(d["rows"])),
    "us_eu_date": (VIS + "05-us-vs-eu-contraindications.json", "inputs.date"),
    "us_eu_one_side": (VIS + "05-us-vs-eu-contraindications.json", "one_side_only"),
    "us_eu_pct": (VIS + "05-us-vs-eu-contraindications.json", lambda d: round(d["pct"])),
    "us_eu_only_eu": (VIS + "05-us-vs-eu-contraindications.json", "only_eu"),
    "us_eu_only_eu_us_none": (VIS + "05-us-vs-eu-contraindications.json", "only_eu_us_none"),
    "us_eu_only_us": (VIS + "05-us-vs-eu-contraindications.json", "only_us"),
    "us_eu_both": (VIS + "05-us-vs-eu-contraindications.json", "both"),
    "us_eu_neither": (VIS + "05-us-vs-eu-contraindications.json", "neither"),
    "us_eu_us_none": (VIS + "05-us-vs-eu-contraindications.json", "us_none"),
    "us_eu_rule_only": (VIS + "05-us-vs-eu-contraindications.json", "rule_only.one_side_only"),
    # example queries (visual 1)
    "q1_as_of": (VIS + "01-example-queries.json", "query1_blame.as_of"),
    "q1_version": (VIS + "01-example-queries.json", "query1_blame.credited_version"),
    "q1_date": (VIS + "01-example-queries.json", "query1_blame.credited_published_date"),
    "q2_added": (VIS + "01-example-queries.json", "query2_changes.added"),
    "q2_removed": (VIS + "01-example-queries.json", "query2_changes.removed"),
    "q2_reworded": (VIS + "01-example-queries.json", "query2_changes.reworded"),
    "q2_first_version": (VIS + "01-example-queries.json", "query2_changes.first_added.version"),
    "q2_first_date": (VIS + "01-example-queries.json", "query2_changes.first_added.published_date"),
    "q3_date": (VIS + "01-example-queries.json", "query3_compare.as_of"),
    "q3_section": (VIS + "01-example-queries.json", "query3_compare.section_used"),
    "q3_molecule": (VIS + "01-example-queries.json", "query3_compare.molecule"),
    "q3_eu_version": (VIS + "01-example-queries.json", "query3_compare.eu_version"),
    "q3_us_version": (VIS + "01-example-queries.json", "query3_compare.us_version"),
    "q3_eu_decision": (VIS + "01-example-queries.json", "query3_compare.eu_decision"),
    "q3_eu_decision_date": (VIS + "01-example-queries.json", "query3_compare.eu_decision_date"),
    "q3_only_us": (VIS + "01-example-queries.json", "query3_compare.only_in_us"),
    "q3_only_eu": (VIS + "01-example-queries.json", "query3_compare.only_in_eu"),
    "q3_shared": (VIS + "01-example-queries.json", "query3_compare.shared_lines"),
}


# Years and label version numbers are identifiers, printed without a thousands separator.
PLAIN = {"srlc_first_year", "srlc_last_year", "churn_peak_year", "churn_last_year", "q1_version",
         "q2_first_version", "q3_us_version", "q3_eu_version"}

# Bare integers of 4 or more digits outside markers that are allowed in the README (years 1990-2099 always are).
# Empty: every number of 4 or more digits in the README comes from a marker.
ALLOWLIST: tuple[str, ...] = ()
BARE = re.compile(r"(?<![\w.,/-])\d{4,}(?![\w,/-]|\.\d)")
FENCE = re.compile(r"^```.*?^```", re.S | re.M)
PRE = re.compile(r"<pre>.*?</pre>", re.S)
CODE = re.compile(r"`[^`\n]*`")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
URL = re.compile(r"https?://\S+")
TAG = re.compile(r"<[a-zA-Z/][^>]*>")


def load_source(root: Path, rel: str, cache: dict) -> Any:
    if rel not in cache:
        path = root / rel
        if rel == "molecules":
            if not path.is_dir():
                raise NumbersError("molecules/ is missing")
            cache[rel] = molecule_counts(path)
        else:
            try:
                cache[rel] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as err:
                raise NumbersError(f"cannot read {rel}: {err}") from err
    return cache[rel]


def molecule_counts(path: Path) -> dict[str, int]:
    """molecules/: the file count, and the molecules with no EU reference product (no eu[] entry with role
    reference), split by eu_selection.no_reference_reason."""
    out = {"count": 0, "no_reference": 0, "no_central": 0, "only_combination": 0}
    for p in sorted(path.glob("*.json")):
        if p.name == "selection.json":
            continue
        out["count"] += 1
        try:
            mol = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as err:
            raise NumbersError(f"cannot read molecules/{p.name}: {err}") from err
        if any(e.get("role") == "reference" for e in mol.get("eu") or []):
            continue
        out["no_reference"] += 1
        reason = (mol.get("eu_selection") or {}).get("no_reference_reason") or ""
        out["no_central"] += reason.startswith("no central product")
        out["only_combination"] += reason.startswith("only combination")
    return out


def values(root: Path = ROOT, keys: list[str] | None = None) -> dict[str, str]:
    """The formatted value of every key (or of ``keys``)."""
    cache: dict = {}
    out = {}
    for key in keys or list(KEYS):
        if key not in KEYS:
            raise NumbersError(f"unknown key {key!r} (see KEYS in tools/readme_numbers.py)")
        rel, getter = KEYS[key]
        data = load_source(root, rel, cache)
        try:
            raw = getter(data) if callable(getter) else _get(data, getter)
        except (KeyError, IndexError, TypeError) as err:
            raise NumbersError(f"{key}: cannot read it from {rel}: {err}") from err
        out[key] = str(raw) if key in PLAIN else fmt(raw)
    return out


def markers(text: str) -> list[tuple[str, str]]:
    return MARKER.findall(text)


def rewrite(text: str, vals: dict[str, str]) -> str:
    return MARKER.sub(lambda m: f"<!-- n:{m.group(1)} -->{vals[m.group(1)]}<!-- /n -->", text)


def unmarked_numbers(text: str) -> list[str]:
    """Thousands-separated numbers outside every marker."""
    return THOUSANDS.findall(MARKER.sub("", text))


def prose(text: str) -> str:
    """The README without markers, fenced code, <pre> blocks, inline code, link targets, URLs and HTML tags."""
    for pattern in (MARKER, FENCE, PRE, CODE, LINK_TARGET, URL, TAG):
        text = pattern.sub(" ", text)
    return text


def bare_numbers(text: str) -> list[str]:
    """Integers of 4 or more digits typed outside a marker (years 1990-2099 and ALLOWLIST excepted)."""
    return [n for n in BARE.findall(prose(text))
            if not (len(n) == 4 and 1990 <= int(n) <= 2099) and n not in ALLOWLIST]


def check_text(text: str, root: Path = ROOT) -> list[str]:
    """Problems with a README text: drifted keys, unknown keys, numbers outside markers."""
    found = markers(text)
    if not found:
        return ["no <!-- n:key --> markers found"]
    unknown = sorted({k for k, _ in found if k not in KEYS})
    if unknown:
        return [f"unknown key(s): {', '.join(unknown)}"]
    vals = values(root, sorted({k for k, _ in found}))
    problems = [f"{k}: README has {v!r}, the source gives {vals[k]!r}" for k, v in found if v != vals[k]]
    loose = unmarked_numbers(text)
    if loose:
        problems.append(f"number(s) outside a marker: {', '.join(sorted(set(loose)))}")
    bare = bare_numbers(text)
    if bare:
        problems.append(f"number(s) of 4 or more digits outside a marker: {', '.join(sorted(set(bare)))}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if a marker or loose number disagrees")
    mode.add_argument("--write", action="store_true", help="rewrite the markers in place")
    mode.add_argument("--list", action="store_true", help="print every key, its value and source")
    p.add_argument("--readme", type=Path, default=README, help="file to check or rewrite (default README.md)")
    p.add_argument("--root", type=Path, default=ROOT, help="directory the sources are read from")
    args = p.parse_args(argv)
    try:
        if args.list:
            vals = values(args.root)
            for key, (rel, _) in KEYS.items():
                print(f"{key}\t{vals[key]}\t{rel}")
            return 0
        text = args.readme.read_text(encoding="utf-8")
        if args.write:
            found = markers(text)
            unknown = sorted({k for k, _ in found if k not in KEYS})
            if unknown:
                print(f"error: unknown key(s): {', '.join(unknown)}", file=sys.stderr)
                return 2
            new = rewrite(text, values(args.root, sorted({k for k, _ in found})))
            if new != text:
                args.readme.write_text(new, encoding="utf-8")
            print(f"{args.readme.name}: {len(found)} markers, {'rewritten' if new != text else 'unchanged'}")
            loose = unmarked_numbers(new)
            if loose:
                print(f"warning: number(s) outside a marker: {', '.join(sorted(set(loose)))}", file=sys.stderr)
            return 0
        problems = check_text(text, args.root)
        if problems:
            for line in problems:
                print(line, file=sys.stderr)
            return 1
        print(f"ok: {len(markers(text))} markers in {args.readme.name} match their sources")
        return 0
    except NumbersError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
