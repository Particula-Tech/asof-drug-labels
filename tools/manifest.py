#!/usr/bin/env python3
"""Write or check manifest.json: what each corpus repo holds and when it was built.

manifest.json lives in the top repo (not in a corpus), so it can change every day without rewriting
corpus history. The reader (tools/labels.py) takes each corpus's build_date from it when its head is the
repo's HEAD, and every MCP answer repeats that date.

  python3 -I tools/manifest.py write [--build-date YYYY-MM-DD] [--dailymed-db-published TEXT]
  python3 -I tools/manifest.py --check

build_date is kept from the existing manifest while a corpus HEAD is unchanged; when the HEAD moves (a
build or an append) it becomes --build-date, else today's UTC date. --check recomputes every field from
the repos and exits 1 on any difference (it skips a corpus whose repo is absent). Standard library only,
read-only on the repos, no network. Requires Python 3.12+.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import labels  # noqa: E402
import mcp_server  # noqa: E402
import notices  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"
MOLECULES = ROOT / "molecules"
SOURCES_US = build.SOURCES
PROJECT = "Particula-Tech/asof-drug-labels"

NOTICE_FILE = "NOTICE.md at the corpus root, written in the first commit"
US_TERMS = {
    "sources": [{"name": "DailyMed, U.S. National Library of Medicine",
                 "url": "https://dailymed.nlm.nih.gov/dailymed/"}],
    "licence": notices.US_LICENCE,
    "changes": notices.US_CHANGES,
    "notice_file": NOTICE_FILE,
    "contact": notices.CONTACT,
    "attribution": "Courtesy of the U.S. National Library of Medicine",
}
EU_TERMS = {
    "sources": [{"name": "European Commission, Union Register of medicinal products",
                 "url": "https://ec.europa.eu/health/documents/community-register/html/index_en.htm"},
                {"name": "European Medicines Agency", "url": "https://www.ema.europa.eu/"}],
    "licence": notices.EU_LICENCE,
    "changes": notices.EU_CHANGES,
    "notice_file": NOTICE_FILE,
    "contact": notices.CONTACT,
    "attribution": "Source: European Commission, Union Register of medicinal products, and the European "
                   "Medicines Agency (EMA)",
}
US_DATE_RULE = ("'on date X' is the newest version whose Published-Date is on or before X: what DailyMed showed "
                "that day, not the FDA approval date")
EU_DATE_RULE = ("'on date X' is the newest Annex I whose Commission decision date (Effective-Date) is on or before "
                "X; a corrected annex counts from the date of the decision it corrects")


TOP_NOTICES = {"contact": notices.CONTACT, "rights_and_corrections": notices.RIGHTS_HOLDERS,
               "independence": notices.INDEPENDENCE, "warranty": notices.NO_WARRANTY}


class ManifestError(RuntimeError):
    """The manifest cannot be written or does not match the repos."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def molecule_count(molecules_dir: Path = MOLECULES) -> int:
    return sum(1 for p in molecules_dir.glob("*.json") if p.name != "selection.json")


def us_listing_counts(setids, sources_dir: Path = SOURCES_US) -> dict:
    """DailyMed-listed, pinned and logged version counts of ``setids`` from sources/us, with the definitions of
    reports/coverage-us.json totals (listed = versions DailyMed lists; logged = listed versions with a problem
    and no pin; fetched_pct = listed versions that are pinned, as a percentage). It reads only the pins, not the
    raw store, so the daily job (which has no raw store) refreshes it on every run."""
    listed_n = fetched_n = logged_n = 0
    for setid in sorted(setids):
        path = sources_dir / f"{setid}.json"
        if not path.exists():
            raise ManifestError(f"no sources file for {setid} in {sources_dir.name}/")
        src = json.loads(path.read_text(encoding="utf-8"))
        listed = {v["version"] for v in src.get("listed_versions") or []}
        pinned = {v["version"] for v in src.get("versions") or []}
        logged = {int(k) for k in src.get("problems") or {} if k.isdigit()} - pinned
        listed_n += len(listed)
        fetched_n += len(listed & pinned)
        logged_n += len(listed & logged)
    return {"listed_versions": listed_n, "logged_versions": logged_n,
            "fetched_pct": round(100 * fetched_n / (listed_n or 1), 2)}


def removed_count(corpus: str, removed_path: Path = build.REMOVED) -> int:
    """Labels (US) or products (EU) listed in sources/removed.json."""
    return sum(1 for e in build.load_removed(removed_path) if e["corpus"] == corpus)


def us_corpus(ix: labels.Index, sources_dir: Path = SOURCES_US, removed_path: Path = build.REMOVED) -> dict:
    retired = sum(1 for s in ix.by_setid if ix.status(s)["status"] == "retired")
    converter = ix.git("log", "-1", "--format=%(trailers:key=Converter-Version,valueonly,separator=)", ix.head).strip()
    return {"scope": build.US_SCOPE, **US_TERMS, "removed_labels": removed_count("us", removed_path), "history_types": {"in-force": len(ix.by_setid)},
            "record_from": ix.record_from, "record_to": ix.record_to, "head": ix.head,
            "commits": len(ix.versions), "labels": len(ix.by_setid), "retired_labels": retired,
            "molecule_folders": len({ix.folder(s).split("/")[0] for s in ix.by_setid}),
            **us_listing_counts(ix.by_setid, sources_dir),
            "converter": f"spl {converter}" if converter else None, "date_rule": US_DATE_RULE}


def eu_corpus(eu: labels.EuIndex, removed_path: Path = build.REMOVED) -> dict:
    types: dict[str, int] = {}
    for setid in eu.by_setid:
        t = eu.status(setid)["history_type"]
        types[t] = types.get(t, 0) + 1
    return {"scope": "English Annex I (summary of product characteristics) of every Commission decision of one "
                     "reference product per molecule, one file per QRD section",
            **EU_TERMS, "removed_products": removed_count("eu", removed_path),
            "history_types": dict(sorted(types.items())),
            "record_from": eu.record_from, "record_to": eu.record_to, "head": eu.head,
            "commits": len(eu.versions), "products": len(eu.by_setid),
            "molecules": len({eu.folder(s).split("/")[0] for s in eu.by_setid}),
            "corrigenda": sum(1 for v in eu.versions if v.corrigendum_of),
            "converter": eu.converter or None, "date_rule": EU_DATE_RULE}


def compute(us_repo: Path | None, eu_repo: Path | None, molecules_dir: Path = MOLECULES,
            sources_dir: Path = SOURCES_US) -> dict:
    """Every manifest field that the repos and sources/us determine (no build_date, no generated)."""
    corpora: dict[str, dict] = {}
    if us_repo is not None and (us_repo / ".git").exists():
        corpora["us"] = us_corpus(labels.Index(us_repo, molecules_dir), sources_dir)
    if eu_repo is not None:
        eu = labels.open_eu(eu_repo, molecules_dir)
        if eu is not None:
            corpora["eu"] = eu_corpus(eu)
    return {"project": PROJECT, "server_version": mcp_server.SERVER_VERSION,
            "molecules": molecule_count(molecules_dir), **TOP_NOTICES, "corpora": corpora}


def merge(old: dict | None, fresh: dict, build_date: str | None = None, dailymed_db_published: str | None = None,
          generated: str | None = None) -> dict:
    """The manifest to write: fresh fields, build_date kept per corpus while its head is unchanged (else
    build_date or today), dailymed_db_published from the argument, else kept from the old manifest."""
    old_corpora = (old or {}).get("corpora") or {}
    out = {"generated": generated or utc_now(), **fresh, "corpora": {}}
    for name, corpus in fresh["corpora"].items():
        prev = old_corpora.get(name) or {}
        same = prev.get("head") == corpus["head"] and prev.get("build_date")
        entry = dict(corpus, build_date=prev["build_date"] if same else (build_date or today()))
        if name == "us":
            entry["dailymed_db_published"] = dailymed_db_published or prev.get("dailymed_db_published")
        out["corpora"][name] = entry
    return out


def write_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def check(path: Path, fresh: dict) -> list[str]:
    """Differences between the manifest on disk and the repos (build_date and generated are not compared,
    apart from build_date being present)."""
    if not path.exists():
        return [f"{path.name} is missing; run tools/manifest.py write"]
    have = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    for key in ("project", "server_version", "molecules", *TOP_NOTICES):
        if have.get(key) != fresh.get(key):
            problems.append(f"{key}: manifest {have.get(key)!r}, now {fresh.get(key)!r}")
    for name, corpus in fresh["corpora"].items():
        got = (have.get("corpora") or {}).get(name)
        if got is None:
            problems.append(f"corpora.{name} is missing")
            continue
        if not got.get("build_date"):
            problems.append(f"corpora.{name}.build_date is missing")
        for key, value in corpus.items():
            if got.get(key) != value:
                problems.append(f"corpora.{name}.{key}: manifest {got.get(key)!r}, repo {value!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", choices=["write"], help="write manifest.json (omit with --check)")
    p.add_argument("--check", action="store_true", help="compare manifest.json with the repos; exit 1 on drift")
    p.add_argument("--us-repo", help="US repo (default: env ASOF_US_REPO, else us/, else build/us)")
    p.add_argument("--eu-repo", help="EU repo (default: env ASOF_EU_REPO, else eu/, else build/eu)")
    p.add_argument("--molecules", default=str(MOLECULES))
    p.add_argument("--sources", default=str(SOURCES_US), help="sources/us (listed, pinned and logged versions)")
    p.add_argument("--out", default=str(MANIFEST))
    p.add_argument("--build-date", help="build_date of a corpus whose HEAD moved (default: today, UTC)")
    p.add_argument("--dailymed-db-published", help="metadata.db_published_date of the DailyMed listing used")
    args = p.parse_args(argv)
    if bool(args.cmd) == args.check:
        p.error("give either 'write' or --check")
    if args.build_date:
        labels.check_date(args.build_date, "build_date")
    us_repo = labels.resolve_us_repo(args.us_repo)
    eu_repo = labels.resolve_eu_repo(args.eu_repo)
    try:
        fresh = compute(us_repo, eu_repo, Path(args.molecules), Path(args.sources))
    except (labels.LabelError, ManifestError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    out = Path(args.out)
    if args.check:
        if not fresh["corpora"]:
            print("no corpus repo found; nothing to check")
            return 0
        problems = check(out, fresh)
        for line in problems:
            print(f"DRIFT {line}", file=sys.stderr)
        print(f"{out.name}: {'matches' if not problems else f'{len(problems)} differences from'} the repos "
              f"({', '.join(sorted(fresh['corpora']))})")
        return 1 if problems else 0
    if not fresh["corpora"]:
        print("error: no corpus repo found (build/us, us/ or ASOF_US_REPO)", file=sys.stderr)
        return 2
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    manifest = merge(old, fresh, args.build_date, args.dailymed_db_published)
    write_manifest(out, manifest)
    for name, c in manifest["corpora"].items():
        print(f"{name}: {c['commits']} commits, record {c['record_from']} to {c['record_to']}, built "
              f"{c['build_date']}, HEAD {c['head'][:12]}")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
