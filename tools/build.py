#!/usr/bin/env python3
"""Build the US label-history git repo from DailyMed SPL versions.

Stages (each writes to disk, so a failed run resumes where it stopped):
  fetch      download every version of one current setid (network)
  fetch-all  the same for every setid listed in molecules/*.json (network)
  ingest     register already-downloaded zips in the raw store
  coverage   report listed vs fetched versions and every logged problem
  build      convert every pinned version and commit it, one commit per version;
             --append adds only the versions HEAD lacks (sources entries with an "added" batch date)
  fetch-missing  re-download pinned versions whose raw zip is absent and verify the pinned SHA-256
                 (rebuilds a raw store from sources/us, e.g. on a new machine or after a CI run)

Inputs are pinned in sources/us/<setid>.json (listed versions, fetched
versions with published date, URL and SHA-256, and any problems). Raw zips
live outside git in raw/sha256/<hash>.zip (env ASOF_RAW moves the raw store, as the daily job does).

Examples:
  python tools/build.py fetch-all                      # 2 requests/s, resumable
  python tools/build.py coverage
  python tools/build.py build --clean
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notices  # noqa: E402
import spl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources" / "us"
RAW_ENV = "ASOF_RAW"     # the raw store root; default raw/ (worker processes read the same variable)
RAW = Path(os.environ.get(RAW_ENV) or ROOT / "raw").resolve()
MOLECULES = ROOT / "molecules"
REPORTS = ROOT / "reports"
BUILD_MARKER = "asof-build"
DAILYMED = "https://dailymed.nlm.nih.gov/dailymed"
USER_AGENT = "ParticulaTech-research/0.1 (drug-label history check)"
MIN_GAP_S = 0.5            # 2 requests per second
RETRY_BACKOFF_S = (5, 30, 120)
ABORT_AFTER_FAILURES = 10  # consecutive transient failures before the whole run stops
TRANSIENT = {429, 500, 502, 503, 504}
COMMITTER = "asof-drug-labels build <build@labels.invalid>"
REMOVED = ROOT / "sources" / "removed.json"
US_SCOPE = ("Human prescription drug labels (SPL) of the selected molecules, one folder per setid and one file per "
            "LOINC section")


class BuildError(RuntimeError):
    """A build input is missing or inconsistent."""


class NotAvailable(BuildError):
    """The source answered, but the item does not exist (e.g. HTTP 404)."""


class FetchAborted(BuildError):
    """Too many consecutive transient failures; stop instead of hammering the source."""


# ---------------------------------------------------------------- removed on request

REMOVED_CORPORA = ("us", "eu")
REMOVED_REASONS = ("rights-holder-request", "personal-data-request", "other")
REMOVED_FIELDS = ("corpus", "id", "reason", "requested_by", "request_date", "removed_on")
US_ID = re.compile(r"^[0-9a-z][0-9a-z-]{20,60}$")      # DailyMed setids (a few carry letters beyond hex)
EU_ID = re.compile(r"^EMEA-H-C-\d{6}$")
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_removed_id(corpus: str, ident: str) -> bool:
    return bool((US_ID if corpus == "us" else EU_ID).match(ident))


def load_removed(path: Path = REMOVED) -> list[dict]:
    """The entries of sources/removed.json (labels and products removed on request); [] when the file is
    absent. A malformed file or entry raises BuildError, so no build runs on a list it cannot honour."""
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        raise BuildError(f"{path.name} is not valid JSON: {err}") from err
    if not isinstance(doc, dict) or not isinstance(doc.get("removed"), list):
        raise BuildError(f"{path.name} must be an object with a 'removed' list")
    for i, e in enumerate(doc["removed"]):
        where = f"{path.name} entry {i}"
        if not isinstance(e, dict):
            raise BuildError(f"{where} is not an object")
        missing = [k for k in REMOVED_FIELDS if not isinstance(e.get(k), str) or not e[k].strip()]
        if missing:
            raise BuildError(f"{where} lacks {', '.join(missing)}")
        if e["corpus"] not in REMOVED_CORPORA:
            raise BuildError(f"{where}: unknown corpus {e['corpus']!r} (expected {', '.join(REMOVED_CORPORA)})")
        if e["reason"] not in REMOVED_REASONS:
            raise BuildError(f"{where}: unknown reason {e['reason']!r} (expected {', '.join(REMOVED_REASONS)})")
        if not valid_removed_id(e["corpus"], e["id"]):
            raise BuildError(f"{where}: {e['id']!r} is not a {e['corpus'].upper()} id")
        for k in ("request_date", "removed_on"):
            if not ISO_DAY.match(e[k]):
                raise BuildError(f"{where}: {k} {e[k]!r} is not YYYY-MM-DD")
        if "note" in e and not isinstance(e["note"], str):
            raise BuildError(f"{where}: note must be text")
    return doc["removed"]


def removed_ids(corpus: str, path: Path = REMOVED) -> set[str]:
    """The ids of one corpus in sources/removed.json: lower-cased US setids, EU product folders as written."""
    return {e["id"].lower() if corpus == "us" else e["id"] for e in load_removed(path) if e["corpus"] == corpus}


# ---------------------------------------------------------------- raw store

def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_path(digest: str, raw_dir: Path = RAW) -> Path:
    return raw_dir / "sha256" / f"{digest}.zip"


def spl_xml(zip_bytes: bytes) -> bytes:
    """The single SPL XML document inside a DailyMed zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if len(xmls) != 1:
            raise BuildError(f"expected one XML file in the zip, found {xmls}")
        return zf.read(xmls[0])


def identify(zip_bytes: bytes) -> tuple[str, int, str]:
    """(setid, version, effective_time) as stated inside the document."""
    label = spl.convert(spl_xml(zip_bytes))
    return label.setid, label.version, label.effective_time


def store(zip_bytes: bytes, raw_dir: Path = RAW) -> str:
    digest = sha256_of(zip_bytes)
    path = raw_path(digest, raw_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(zip_bytes)
        tmp.replace(path)
    return digest


# ---------------------------------------------------------------- sources

def parse_published(value: str) -> str:
    """'Apr 13, 2007' (DailyMed history API) -> '2007-04-13'."""
    return datetime.strptime(value.strip(), "%b %d, %Y").strftime("%Y-%m-%d")


def history_dates(history: dict) -> dict[int, str]:
    return {int(r["spl_version"]): parse_published(r["published_date"]) for r in history["data"]["history"]}


def source_url(setid: str, version: int) -> str:
    return f"{DAILYMED}/getFile.cfm?type=zip&setid={setid}&version={version}"


def new_source(setid: str, inn: str) -> dict:
    return {"setid": setid, "inn": inn, "source": "dailymed", "versions": []}


def load_sources(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_sources(path: Path, src: dict) -> None:
    src["versions"] = sorted(src["versions"], key=lambda v: v["version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(src, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


PIN_CHANGED = "the file downloaded again differs from its pinned SHA-256; the pin is kept"


def upsert_version(src: dict, entry: dict) -> None:
    """Pin one version. A version pinned before keeps its pin when the new file has the same SHA-256 (its
    ``added`` batch is kept too); a different file never replaces a pin, because the pin is in the
    committed history (a full rebuild must give the same commits), so that is raised as a BuildError."""
    old = next((v for v in src["versions"] if v["version"] == entry["version"]), None)
    if old is not None:
        if old["sha256"] != entry["sha256"]:
            raise BuildError(f"v{entry['version']}: {PIN_CHANGED} ({old['sha256'][:12]}, now {entry['sha256'][:12]})")
        return
    src["versions"] = src["versions"] + [entry]
    src.get("problems", {}).pop(str(entry["version"]), None)
    since = src.get("problem_since")
    if since is not None:
        since.pop(str(entry["version"]), None)
        if not since:
            del src["problem_since"]


RETRY_DAYS = 30   # a problem version the daily job logged is retried by the Sunday sweep for this many days


def log_problem(src: dict, version: int, err: object, added: str | None) -> None:
    """Record a version that could not be fetched. The daily path (``added`` set) also records the run date
    under problem_since (kept from the first failure), which bounds the sweep's retries (see pinned_versions);
    versions logged by a full build carry no problem_since and are never retried by the daily job."""
    src.setdefault("problems", {})[str(version)] = str(err)
    if added:
        src.setdefault("problem_since", {}).setdefault(str(version), added.split(".")[0])


def cmd_ingest(args: argparse.Namespace) -> int:
    dates = history_dates(json.loads(Path(args.history).read_text(encoding="utf-8")))
    sources: dict[str, dict] = {}
    for zip_path in args.zips:
        data = Path(zip_path).read_bytes()
        setid, version, _ = identify(data)
        if version not in dates:
            raise BuildError(f"{zip_path}: version {version} is not in the history file {args.history}")
        path = SOURCES / f"{setid}.json"
        src = sources.get(setid) or (load_sources(path) if path.exists() else new_source(setid, args.inn))
        if src["inn"] != args.inn:
            raise BuildError(f"{setid} is pinned with inn {src['inn']!r}, not {args.inn!r}")
        src["listed_versions"] = [{"version": v, "published_date": d} for v, d in sorted(dates.items())]
        upsert_version(src, {"version": version, "published_date": dates[version],
                             "url": source_url(setid, version), "sha256": store(data)})
        sources[setid] = src
        print(f"ingested {setid} v{version} published {dates[version]}")
    for setid, src in sources.items():
        save_sources(SOURCES / f"{setid}.json", src)
    return 0


# ---------------------------------------------------------------- fetch

class Fetcher:
    """Rate-limited GET with a fixed User-Agent, retries with back-off on transient
    errors, and a circuit breaker. Thread-safe: several workers may share one
    Fetcher, and the rate limit applies to all of them together.
    ``opener`` and ``sleep`` are injectable for tests."""

    def __init__(self, opener=urllib.request.urlopen, min_gap_s: float = MIN_GAP_S, log_path: Path | None = None,
                 backoff=RETRY_BACKOFF_S, abort_after: int = ABORT_AFTER_FAILURES, sleep=time.sleep):
        self.opener, self.min_gap_s, self.log_path = opener, min_gap_s, log_path
        self.backoff, self.abort_after, self.sleep = backoff, abort_after, sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.consecutive_failures = 0
        self.requests = 0

    def _once(self, url: str) -> tuple[int, bytes, str]:
        with self._lock:  # reserve the next start slot; slots are min_gap_s apart across all threads
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.min_gap_s
            self.requests += 1
        wait = slot - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
            with self._lock, self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\t{status or 'ERR'}\t{len(body)}\t{url}\t{note}\n")
        return status, body, note

    def get(self, url: str) -> bytes:
        for attempt in range(len(self.backoff) + 1):
            status, body, note = self._once(url)
            if status == 200:
                with self._lock:
                    self.consecutive_failures = 0
                return body
            if status and status not in TRANSIENT:
                with self._lock:
                    self.consecutive_failures = 0
                raise NotAvailable(f"HTTP {status} for {url}")
            if attempt < len(self.backoff):
                self.sleep(self.backoff[attempt])
        with self._lock:
            self.consecutive_failures += 1
            failures = self.consecutive_failures
        if failures >= self.abort_after:
            raise FetchAborted(f"{failures} consecutive transient failures; last: {url} ({status or note})")
        raise BuildError(f"gave up after retries: {url} ({status or note})")


def fetch_history(setid: str, fetcher: Fetcher) -> tuple[dict[int, str], str | None]:
    """All listed versions of a setid and its title (None for a retired setid)."""
    dates: dict[int, str] = {}
    page, title = 1, None
    while True:
        history = json.loads(fetcher.get(f"{DAILYMED}/services/v2/spls/{setid}/history.json?pagesize=100&page={page}"))
        dates.update(history_dates(history))
        title = title or (history["data"].get("spl") or {}).get("title")
        if page >= int(history["metadata"].get("total_pages") or 1):
            return dates, title
        page += 1


def pinned_versions(src: dict, raw_dir: Path, trust_pins: bool, retry_from: str | None = None) -> set[int]:
    """The versions a fetch leaves alone. Normally: pinned versions whose raw zip is on disk. With
    ``trust_pins`` (the daily job, which runs without the raw store): every pinned version, plus versions
    logged as a problem below the newest pinned one, so a version that failed in a daily run is retried
    while it is the newest. ``retry_from`` (the Sunday sweep: the run date minus RETRY_DAYS) also retries
    the logged versions below the newest whose problem_since is on or after that date."""
    if not trust_pins:
        return {v["version"] for v in src["versions"] if raw_path(v["sha256"], raw_dir).exists()}
    pinned = {v["version"] for v in src["versions"]}
    top = max(pinned, default=0)
    since = src.get("problem_since") or {}
    logged = {int(k) for k in src.get("problems") or {} if k.isdigit() and int(k) < top
              and not (retry_from and since.get(k, "") >= retry_from)}
    return pinned | logged


def sweep_retry_versions(src: dict, retry_from: str) -> list[int]:
    """The logged versions below the newest pinned one that a sweep with ``retry_from`` requests again."""
    pinned = {v["version"] for v in src["versions"]}
    top = max(pinned, default=0)
    since = src.get("problem_since") or {}
    return sorted(int(k) for k in src.get("problems") or {} if k.isdigit() and int(k) < top
                  and int(k) not in pinned and since.get(k, "") >= retry_from)


def fetch_setid(setid: str, inn: str, fetcher: Fetcher, sources_dir: Path = SOURCES, raw_dir: Path = RAW,
                refresh: bool = False, extra: dict | None = None, archive_dir: Path | None = None,
                added: str | None = None, trust_pins: bool = False, retry_from: str | None = None) -> list[str]:
    """Fetch every listed version of a setid not already pinned: a current setid through
    getFile.cfm, a retired one (null title in /history) through the archive.

    ``extra`` holds crosswalk facts copied into the sources file (successor_setids of a
    retired label). ``added`` (the daily job's batch date) is written into every newly pinned
    version, so ``build --append`` can commit it after HEAD (see entry_key); ``trust_pins`` see
    pinned_versions (with ``retry_from``). Returns problems as strings; they are also persisted in the sources
    file so the coverage report can account for every listed version."""
    path = sources_dir / f"{setid}.json"
    src = load_sources(path) if path.exists() else new_source(setid, inn)
    if extra and any(src.get(k) != v for k, v in extra.items()):
        src.update(extra)
        save_sources(path, src)
    have = pinned_versions(src, raw_dir, trust_pins, retry_from)
    listed = {v["version"] for v in src.get("listed_versions", [])}
    if listed and listed <= have and not refresh:
        return []

    dates, title = fetch_history(setid, fetcher)
    src["listed_versions"] = [{"version": v, "published_date": d} for v, d in sorted(dates.items())]
    src["history_title"] = title
    src["status"] = "current" if title else "retired"
    src.setdefault("problems", {})
    save_sources(path, src)
    if not title:
        return fetch_retired(setid, src, dates, fetcher, path, raw_dir, archive_dir or raw_dir / "archive",
                             added=added, trust_pins=trust_pins, retry_from=retry_from)

    problems = []
    for version in sorted(set(dates) - have):
        url = source_url(setid, version)
        try:
            data = fetcher.get(url)
            got_setid, got_version, _ = identify(data)
            if (got_setid, got_version) != (setid, version):
                raise BuildError(f"asked for {setid} v{version}, got {got_setid} v{got_version}")
        except FetchAborted:
            save_sources(path, src)
            raise
        except (BuildError, spl.SplError, zipfile.BadZipFile, OSError) as err:
            log_problem(src, version, err, added)
            problems.append(f"{setid} v{version}: {err}")
            save_sources(path, src)
            continue
        pin(src, with_batch({"version": version, "published_date": dates[version], "url": url,
                             "sha256": store(data, raw_dir)}, added), problems, setid)
        save_sources(path, src)  # after every file, so an interrupted run resumes
    return problems


def with_batch(entry: dict, added: str | None) -> dict:
    if added:
        entry["added"] = added
    return entry


def pin(src: dict, entry: dict, problems: list[str], setid: str) -> None:
    """upsert_version, with a refused pin change logged under problems["pin:<version>"] (not a digit key:
    the version stays pinned and counted as fetched)."""
    try:
        upsert_version(src, entry)
    except BuildError as err:
        src.setdefault("problems", {})[f"pin:{entry['version']}"] = str(err)
        problems.append(f"{setid} {err}")


NO_ARCHIVED_FILE = "listed in /history, but the archive has no file for this version"


def fetch_retired(setid: str, src: dict, dates: dict[int, str], fetcher: Fetcher, path: Path, raw_dir: Path,
                  archive_dir: Path, added: str | None = None, trust_pins: bool = False,
                  retry_from: str | None = None) -> list[str]:
    """Versions of a retired setid through getArchivalFile.cfm, with the archive ids from the
    archive searched by setid. A version the archive lists but /history does not is added with
    the archive's posted date (the same date /history gives for versions it does list)."""
    import archive  # archive.py builds on this module, so it is imported only when needed

    try:
        listing = {r.version: r for r in archive.setid_listing(fetcher, setid, archive_dir)}
    except FetchAborted:
        raise
    except (NotAvailable, archive.ArchiveError) as err:
        src["problems"]["history"] = f"archive listing failed: {err}"
        save_sources(path, src)
        return [f"{setid}: archive listing failed: {err}"]
    for version, row in listing.items():
        dates.setdefault(version, row.posted)
    src["listed_versions"] = [{"version": v, "published_date": d} for v, d in sorted(dates.items())]
    save_sources(path, src)

    have = pinned_versions(src, raw_dir, trust_pins, retry_from)
    problems = []
    for version in sorted(set(dates) - have):
        row = listing.get(version)
        try:
            if row is None:
                raise NotAvailable(NO_ARCHIVED_FILE)
            data = archive.archived_zip(fetcher, row.archive_id, archive_dir, raw_dir)
            got_setid, got_version, _ = identify(data)
            if (got_setid.lower(), got_version) != (setid, version):
                raise BuildError(f"asked for {setid} v{version}, got {got_setid} v{got_version}")
        except FetchAborted:
            save_sources(path, src)
            raise
        except (BuildError, spl.SplError, zipfile.BadZipFile, OSError) as err:
            log_problem(src, version, err, added)
            problems.append(f"{setid} v{version}: {err}")
            save_sources(path, src)
            continue
        pin(src, with_batch({"version": version, "published_date": dates[version],
                             "url": archive.archival_url(row.archive_id), "sha256": sha256_of(data)}, added),
            problems, setid)
        save_sources(path, src)
    return problems


def label_status(src: dict) -> dict:
    """Whether DailyMed still updates this setid, from its sources file. Not written into label.json:
    status changes when a setid retires, and the corpus history stays append-only, so the reader takes
    status from the molecules/ crosswalk instead (labels.Index.status)."""
    if src.get("status") != "retired":
        return {"status": "current"}
    dates = [v["published_date"] for v in src.get("listed_versions") or src["versions"]]
    return {"status": "retired", "last_published": max(dates),
            "successor_setids": sorted(src.get("successor_setids", []))}


def molecule_entries(molecules_dir: Path = MOLECULES) -> dict[str, dict]:
    """The crosswalk entry of every US setid (from the first molecule file that lists it)."""
    entries: dict[str, dict] = {}
    for path in sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json"):
        for entry in json.loads(path.read_text(encoding="utf-8")).get("us") or []:
            if isinstance(entry, dict):
                entries.setdefault(entry["setid"].lower(), entry)
    return entries


def crosswalk_extra(entry: dict | None) -> dict | None:
    if entry and entry.get("status") == "retired":
        return {"successor_setids": sorted(entry.get("successor_setids", []))}
    return None


def molecule_setids(molecules_dir: Path = MOLECULES) -> list[tuple[str, str]]:
    """(setid, inn) for every US setid in molecules/<inn>.json, each setid once
    (the first molecule in file-name order owns a setid shared by several)."""
    seen: dict[str, str] = {}
    files = sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json")
    if not files:
        raise BuildError(f"no molecule files in {molecules_dir}; run tools/select_molecules.py first")
    for path in files:
        mol = json.loads(path.read_text(encoding="utf-8"))
        inn = mol.get("inn") or path.stem
        for entry in mol.get("us") or mol.get("us_setids") or []:
            setid = entry["setid"] if isinstance(entry, dict) else entry
            seen.setdefault(setid.lower(), inn)
    return sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))


def cmd_fetch(args: argparse.Namespace) -> int:
    problems = fetch_setid(args.setid, args.inn, Fetcher(log_path=RAW / "requests.tsv"), refresh=args.refresh)
    for p in problems:
        print(f"PROBLEM {p}", file=sys.stderr)
    return 1 if problems else 0


def _fetch_one(setid: str, inn: str, fetcher: Fetcher, refresh: bool, extra: dict | None = None) -> list[str]:
    try:
        return fetch_setid(setid, inn, fetcher, refresh=refresh, extra=extra)
    except FetchAborted:
        raise
    except BuildError as err:  # the history request failed after retries
        path = SOURCES / f"{setid}.json"
        src = load_sources(path) if path.exists() else new_source(setid, inn)
        src.setdefault("problems", {})["history"] = str(err)
        save_sources(path, src)
        return [f"{setid}: {err}"]


def cmd_fetch_all(args: argparse.Namespace) -> int:
    pairs = molecule_setids(Path(args.molecules))
    entries = molecule_entries(Path(args.molecules))
    if args.limit:
        pairs = pairs[: args.limit]
    fetcher = Fetcher(log_path=RAW / "requests.tsv")
    stop = threading.Event()
    started, n_problems, done = time.monotonic(), 0, 0

    def work(setid: str, inn: str) -> list[str]:
        if stop.is_set():
            return []
        return _fetch_one(setid, inn, fetcher, args.refresh, crosswalk_extra(entries.get(setid)))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(work, setid, inn): (setid, inn) for setid, inn in pairs}
        for fut in as_completed(futures):
            setid, inn = futures[fut]
            try:
                problems = fut.result()
            except FetchAborted as err:
                stop.set()
                for other in futures:
                    other.cancel()
                print(f"ABORTED: {err}. Rerun to resume.", file=sys.stderr, flush=True)
                return 3
            done += 1
            n_problems += len(problems)
            print(f"[{done}/{len(pairs)}] {inn} {setid}: {len(problems)} problems; total {fetcher.requests} "
                  f"requests, {(time.monotonic() - started) / 3600:.2f} h", flush=True)
            for p in problems:
                print(f"  PROBLEM {p}", flush=True)
    print(f"done: {len(pairs)} setids, {fetcher.requests} requests, {n_problems} problems logged", flush=True)
    return 0


# ---------------------------------------------------------------- raw-store recovery

def missing_raw(sources_dir: Path = SOURCES, raw_dir: Path = RAW) -> list[tuple[str, int, dict]]:
    """(setid, version, pinned entry) of every pinned version whose raw zip is not on disk."""
    out = []
    for path in sorted(sources_dir.glob("*.json")):
        src = load_sources(path)
        out += [(src["setid"], v["version"], v) for v in src["versions"] if not raw_path(v["sha256"], raw_dir).exists()]
    return out


def fetch_missing(fetcher: Fetcher, sources_dir: Path = SOURCES, raw_dir: Path = RAW, limit: int | None = None,
                  workers: int = 4) -> dict:
    """Download the pinned URL of every version whose raw zip is missing and keep it only when its SHA-256
    is the pinned one. Sources files are never changed: a file that differs is reported, not pinned."""
    todo = missing_raw(sources_dir, raw_dir)
    if limit:
        todo = todo[:limit]
    result = {"missing": len(todo), "restored": 0, "mismatch": [], "failed": []}
    lock = threading.Lock()

    def work(item: tuple[str, int, dict]) -> None:
        setid, version, entry = item
        try:
            data = fetcher.get(entry["url"])
        except FetchAborted:
            raise
        except BuildError as err:
            with lock:
                result["failed"].append(f"{setid} v{version}: {err}")
            return
        if sha256_of(data) != entry["sha256"]:
            with lock:
                result["mismatch"].append(f"{setid} v{version}: {entry['url']} now has SHA-256 {sha256_of(data)}, "
                                          f"pinned {entry['sha256']}")
            return
        store(data, raw_dir)
        with lock:
            result["restored"] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in [pool.submit(work, item) for item in todo]:
            fut.result()
    return result


def cmd_fetch_missing(args: argparse.Namespace) -> int:
    if args.dry_run:
        todo = missing_raw()
        print(f"{len(todo)} pinned versions have no raw zip in {RAW / 'sha256'}")
        return 0
    res = fetch_missing(Fetcher(log_path=RAW / "requests.tsv"), limit=args.limit, workers=args.workers)
    for line in res["mismatch"] + res["failed"]:
        print(f"PROBLEM {line}", file=sys.stderr)
    print(f"{res['missing']} missing, {res['restored']} restored, {len(res['mismatch'])} differ from their pin, "
          f"{len(res['failed'])} failed")
    return 1 if res["mismatch"] or res["failed"] else 0


# ---------------------------------------------------------------- coverage

def coverage(molecules_dir: Path = MOLECULES, sources_dir: Path = SOURCES, raw_dir: Path = RAW) -> dict:
    rows, totals = [], {"setids": 0, "listed": 0, "fetched": 0, "logged": 0, "unaccounted": 0,
                        "setids_not_started": 0, "setids_retired": 0}
    reasons: dict[str, int] = {}
    for setid, inn in molecule_setids(molecules_dir):
        totals["setids"] += 1
        path = sources_dir / f"{setid}.json"
        if not path.exists():
            totals["setids_not_started"] += 1
            rows.append({"setid": setid, "inn": inn, "status": "not-started"})
            continue
        src = load_sources(path)
        listed = {v["version"] for v in src.get("listed_versions", [])}
        fetched = {v["version"] for v in src["versions"] if raw_path(v["sha256"], raw_dir).exists()}
        problems = src.get("problems", {})
        logged = {int(k) for k in problems if k.isdigit()} - fetched
        if src.get("status") == "retired":
            totals["setids_retired"] += 1
        for msg in problems.values():
            key = msg.split(" for ")[0].split(":")[0][:60]
            reasons[key] = reasons.get(key, 0) + 1
        unaccounted = listed - fetched - logged
        totals["listed"] += len(listed)
        totals["fetched"] += len(listed & fetched)
        totals["logged"] += len(logged & listed)
        totals["unaccounted"] += len(unaccounted)
        rows.append({"setid": setid, "inn": inn, "status": src.get("status", "unknown"), "listed": len(listed),
                     "fetched": len(listed & fetched), "logged": len(logged & listed),
                     "unaccounted": sorted(unaccounted), "problems": problems})
    listed = totals["listed"] or 1
    totals["fetched_pct"] = round(100 * totals["fetched"] / listed, 2)
    totals["fetched_or_logged_pct"] = round(100 * (totals["fetched"] + totals["logged"]) / listed, 2)
    return {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "totals": totals,
            "problem_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])), "setids": rows}


def cmd_coverage(args: argparse.Namespace) -> int:
    report = coverage(Path(args.molecules))
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / "coverage-us.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    t = report["totals"]
    print(f"setids {t['setids']} (not started {t['setids_not_started']}, retired {t['setids_retired']}); "
          f"versions listed {t['listed']}, fetched {t['fetched']} ({t['fetched_pct']}%), "
          f"fetched or logged {t['fetched_or_logged_pct']}%, unaccounted {t['unaccounted']}")
    for reason, n in list(report["problem_reasons"].items())[:10]:
        print(f"  {n:>6}  {reason}")
    print(f"written {out}")
    return 0


# ---------------------------------------------------------------- build

def git_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C", "TZ": "UTC",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, *args: str, env: dict | None = None, stdin: str | None = None) -> str:
    base = git_env(repo)
    if env:
        base.update(env)
    proc = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                           "-c", "core.autocrlf=false", *args],
                          input=stdin, capture_output=True, text=True, env=base)
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def subject(label: spl.Label) -> str:
    products = label.metadata["products"]
    if products:
        p = products[0]
        name = f"{p['name']} ({p['generic']}) {p['form'].lower()}".strip()
    else:
        name = label.doc_title or label.setid
    return f"{name} — label version {label.version}"


def commit_message(label: spl.Label, entry: dict, conversion: str | None) -> str:
    eff = label.effective_time
    lines = [subject(label), "",
             f"Source-Id: dailymed:{label.setid}",
             f"Source-Version: {label.version}",
             f"Published-Date: {entry['published_date']}",
             f"Effective-Time: {eff[:4]}-{eff[4:6]}-{eff[6:8]}",
             f"Application-Numbers: {', '.join(label.metadata['application_numbers']) or 'none stated'}",
             f"Source-URL: {entry['url']}",
             f"Source-SHA256: {entry['sha256']}",
             f"Label-Format: {label.label_format}",
             f"Converter-Version: {spl.CONVERTER_VERSION}",
             "History: in-force"]
    if conversion:
        lines.append(f"Format-Conversion: {conversion}")
    lines.append(notices.TRAILER_US)
    return "\n".join(lines) + "\n"


def epoch(date: str) -> int:
    return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def prepare_out(out: Path, clean: bool) -> None:
    if out.exists() and any(out.iterdir()):
        if not clean:
            raise BuildError(f"{out} is not empty; pass --clean to rebuild it")
        if not (out / ".git" / BUILD_MARKER).exists():
            raise BuildError(f"{out} was not created by build.py (no .git/{BUILD_MARKER}); refusing to delete it")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


def entry_key(added: str | None, published: str, setid: str, version: int) -> tuple:
    """Build order of one pinned version: its batch (``added``, the daily run that pinned it; '' for
    versions pinned before the first daily run), then Published-Date, setid and version. A full build and
    a chain of appends therefore commit the same versions in the same order."""
    return (added or "", published, setid, version)


def pinned_entries(sources_dir: Path, removed: set[str] | None = None) -> list[tuple]:
    """(published, setid, version, inn, entry) of every pinned version, in build order, without the setids in
    ``removed`` (default: the US ids of sources/removed.json)."""
    removed = removed_ids("us") if removed is None else {s.lower() for s in removed}
    entries = []
    for path in sorted(sources_dir.glob("*.json")):
        src = load_sources(path)
        if src["setid"].lower() in removed:
            continue
        for v in src["versions"]:
            entries.append((v["published_date"], src["setid"], v["version"], src["inn"], v))
    entries.sort(key=lambda e: entry_key(e[4].get("added"), e[0], e[1], e[2]))
    return entries


def _convert_entry(args: tuple[str, str, int, str, dict]) -> tuple:
    """Worker: read, verify and convert one pinned version (runs in a subprocess)."""
    published, setid, version, inn, entry = args
    zpath = raw_path(entry["sha256"])
    if not zpath.exists():
        raise BuildError(f"raw file missing for {setid} v{version}: {zpath} (run build.py fetch or ingest)")
    data = zpath.read_bytes()
    if sha256_of(data) != entry["sha256"]:
        raise BuildError(f"raw file {zpath} does not match its pinned SHA-256")
    label = spl.convert(spl_xml(data))
    if (label.setid, label.version) != (setid, version):
        raise BuildError(f"{zpath} holds {label.setid} v{label.version}, pinned as {setid} v{version}")
    return published, setid, version, inn, entry, label


def _data(payload: bytes) -> bytes:
    return b"data %d\n" % len(payload) + payload + b"\n"


def blob_hash(payload: bytes) -> str:
    """The git blob id of ``payload`` (what git ls-tree prints), so files of HEAD and new files compare."""
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()


def label_files(setid: str, inn: str, label: spl.Label) -> dict[str, bytes]:
    """The files of one label version: label.json (constants only, no status) plus one file per section."""
    meta = {"setid": setid, "inn": inn, "source": "dailymed", **label.metadata}
    files = {"label.json": json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False) + "\n", **label.files}
    return {name: text.encode("utf-8") for name, text in files.items()}


class _Writer:
    """Streams one fast-import commit per converted version. ``state`` maps a label folder to its files'
    blob ids as of the previous commit; ``last_format`` a setid to the Label-Format of its previous commit."""

    def __init__(self, proc: subprocess.Popen, state: dict, last_format: dict, parent: str | None,
                 root: dict[str, bytes] | None = None):
        self.proc, self.state, self.last_format, self.parent, self.root = proc, state, last_format, parent, root
        self.n = 0

    def label_state(self, prefix: str) -> dict[str, str]:
        return self.state.get(prefix, {})

    def write(self, published: str, setid: str, version: int, inn: str, entry: dict, label: spl.Label) -> None:
        self.n += 1
        prefix = f"{inn}/{setid}/"
        encoded = label_files(setid, inn, label)
        hashes = {name: blob_hash(b) for name, b in encoded.items()}
        before = self.label_state(prefix)
        prev = self.last_format.get(setid)
        conversion = f"{prev}-to-{label.label_format}" if prev and prev != label.label_format else None
        self.last_format[setid] = label.label_format
        stamp = f"{epoch(published)} +0000"
        msg = commit_message(label, entry, conversion).encode("utf-8")
        chunk = [b"commit refs/heads/main\n", b"mark :%d\n" % self.n,
                 f"author Label {setid} v{version} <{setid}.v{version}@labels.invalid> {stamp}\n".encode(),
                 f"committer {COMMITTER} {stamp}\n".encode(), _data(msg)]
        if self.n > 1:
            chunk.append(b"from :%d\n" % (self.n - 1))
        elif self.parent:
            chunk.append(f"from {self.parent}\n".encode())
        if self.n == 1 and self.root:
            chunk += root_chunk(self.root)
        for name in sorted(set(before) - set(encoded)):
            chunk.append(f"D {prefix}{name}\n".encode())
        for name in sorted(encoded):
            if before.get(name) != hashes[name]:
                chunk.append(f"M 100644 inline {prefix}{name}\n".encode())
                chunk.append(_data(encoded[name]))
        chunk.append(b"\n")
        self.proc.stdin.write(b"".join(chunk))
        self.state[prefix] = hashes


def root_chunk(root: dict[str, bytes]) -> list[bytes]:
    """fast-import file commands writing the corpus root files (sorted by name) into the first commit."""
    out = []
    for name in sorted(root):
        out += [f"M 100644 inline {name}\n".encode(), _data(root[name])]
    return out


def us_corpus_json() -> dict:
    """corpus.json of the US corpus, written once in the first commit: constants only (the EU corpus has the
    same four keys)."""
    return {"corpus": "us", "scope": US_SCOPE,
            "layout": "<inn>/<setid>/label.json and <loinc>-<slug>.md; one commit per DailyMed label version, dated "
                      "at DailyMed's Published-Date; the Published-Date trailer is authoritative",
            "sources": ["DailyMed, U.S. National Library of Medicine (dailymed.nlm.nih.gov)"]}


def us_root_files() -> dict[str, bytes]:
    """README.md, NOTICE.md and corpus.json at the root of the US corpus."""
    corpus = (json.dumps(us_corpus_json(), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return {"corpus.json": corpus, **notices.root_files("us")}


def _fast_import(out: Path, marks: Path) -> subprocess.Popen:
    return subprocess.Popen(["git", "-C", str(out), "fast-import", "--quiet", "--date-format=raw",
                             f"--export-marks={marks}"], stdin=subprocess.PIPE, env=git_env(out))


def _finish(proc: subprocess.Popen, out: Path, marks: Path, n: int) -> list[str]:
    proc.stdin.close()
    if proc.wait() != 0:
        raise BuildError("git fast-import failed")
    if not n:
        return []
    git(out, "reset", "-q", "--hard", "main")
    by_mark = dict(line.split() for line in marks.read_text().splitlines())
    return [by_mark[f":{i}"] for i in range(1, n + 1)]


def build(out: Path, sources_dir: Path = SOURCES, raw_dir: Path = RAW, clean: bool = False,
          workers: int | None = None, removed: set[str] | None = None) -> list[str]:
    """Build the repo with git fast-import; returns the commit hashes in order. The first commit also writes
    the corpus root files (README.md, NOTICE.md, corpus.json); ``removed`` see pinned_entries.

    Trees, messages, authors and dates are fully determined by the pinned inputs,
    so the same inputs always give the same hashes."""
    if raw_dir != RAW:
        raise BuildError("custom raw_dir is not supported by the parallel converter")
    out = Path(out).resolve()   # git runs with -C out, so the marks path must not be relative
    entries = pinned_entries(sources_dir, removed)
    if not entries:
        raise BuildError(f"no pinned versions in {sources_dir}")

    prepare_out(out, clean)
    git(out, "init", "-q", "-b", "main")
    (out / ".git" / BUILD_MARKER).write_text("created by tools/build.py\n", encoding="utf-8")
    marks = out / ".git" / "asof-marks"
    proc = _fast_import(out, marks)
    writer = _Writer(proc, {}, {}, None, us_root_files())
    try:
        with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
            for converted in pool.map(_convert_entry, entries, chunksize=16):
                writer.write(*converted)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return _finish(proc, out, marks, writer.n)


# ---------------------------------------------------------------- append

HISTORY_REFUSAL = "would change history; run a full rebuild"


def head_versions(repo: Path) -> list[tuple[str, int, str]]:
    """(setid, version, Label-Format) of every commit on main, oldest first, from the trailers."""
    out = git(repo, "log", "--reverse", "--format=%(trailers:key=Source-Id,valueonly,separator=)%x00"
                                        "%(trailers:key=Source-Version,valueonly,separator=)%x00"
                                        "%(trailers:key=Label-Format,valueonly,separator=)%x1e", "main")
    rows = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        source_id, version, fmt = (x.strip() for x in record.split("\x00"))
        if not source_id.startswith("dailymed:"):
            raise BuildError(f"commit without a dailymed Source-Id trailer in {repo}")
        rows.append((source_id.split(":", 1)[1].lower(), int(version), fmt))
    return rows


def plan_append(repo: Path, sources_dir: Path = SOURCES,
                removed: set[str] | None = None) -> tuple[list[tuple], list[tuple[str, int, str]]]:
    """(pinned versions not yet committed, in build order; HEAD's commits). Refuses with HISTORY_REFUSAL
    unless a full build from the same sources would give HEAD's commits followed by exactly these: every
    committed version is still pinned, and every new one carries ``added`` and sorts after HEAD's newest
    commit in the build order. A label removed on request (sources/removed.json) that HEAD still holds is
    refused the same way: only a full rebuild (tools/purge.py rebuild) takes it out of the history."""
    removed = removed_ids("us") if removed is None else {s.lower() for s in removed}
    committed = head_versions(repo)
    done = {(s, v) for s, v, _ in committed}
    present = sorted({s for s, _, _ in committed} & removed)
    if present:
        raise BuildError(f"{len(present)} removed label(s) still in HEAD (first: {present[0]}); {HISTORY_REFUSAL}: "
                         "run tools/purge.py rebuild --corpus us")
    entries = pinned_entries(sources_dir, removed)
    pinned = {(e[1], e[2]): e for e in entries}
    gone = sorted(done - set(pinned))
    if gone:
        raise BuildError(f"{len(gone)} committed versions are no longer pinned (first: {gone[0][0]} v{gone[0][1]}); "
                         f"{HISTORY_REFUSAL}")
    pending = [e for e in entries if (e[1], e[2]) not in done]
    if not pending:
        return [], committed
    last = pinned[committed[-1][:2]] if committed else None
    last_key = entry_key(last[4].get("added"), last[0], last[1], last[2]) if last else None
    for e in pending:
        if not e[4].get("added"):
            raise BuildError(f"{e[1]} v{e[2]} is pinned without an 'added' batch date; {HISTORY_REFUSAL}")
        if last_key is not None and entry_key(e[4]["added"], e[0], e[1], e[2]) <= last_key:
            raise BuildError(f"{e[1]} v{e[2]} (added {e[4]['added']}, published {e[0]}) sorts before HEAD's newest "
                             f"commit {last[1]} v{last[2]}; {HISTORY_REFUSAL}")
    return pending, committed


def label_tree(repo: Path, prefix: str) -> dict[str, str]:
    """File name -> blob id of one label folder at main (empty for a label not yet in the repo)."""
    out = git(repo, "ls-tree", "main", "--", prefix)
    files = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        mode, kind, blob = meta.split()
        if kind == "blob":
            files[path[len(prefix):]] = blob
    return files


def append(out: Path, sources_dir: Path = SOURCES, workers: int | None = None,
           removed: set[str] | None = None) -> list[str]:
    """Append the pinned versions that HEAD does not have yet as new commits on main (parent: the old HEAD),
    writing only files that differ from each label's tree at HEAD. Returns the new hashes (empty when
    nothing is pending; HEAD is then untouched). The result equals a full build from the same sources."""
    out = Path(out).resolve()
    if not (out / ".git").exists():
        raise BuildError(f"{out} is not a git repo; run build.py build first")
    pending, committed = plan_append(out, sources_dir, removed)
    if not pending:
        return []
    last_format: dict[str, str] = {}
    for setid, _, fmt in committed:
        last_format[setid] = fmt
    state = {}
    for e in pending:
        prefix = f"{e[3]}/{e[1]}/"
        if prefix not in state:
            state[prefix] = label_tree(out, prefix)
    marks = Path(tempfile.mkdtemp(prefix="asof-append-")) / "marks"
    proc = _fast_import(out, marks)
    writer = _Writer(proc, state, last_format, "refs/heads/main^0", None)
    try:
        with ProcessPoolExecutor(max_workers=workers or min(4, os.cpu_count() or 1)) as pool:
            for converted in pool.map(_convert_entry, pending, chunksize=4):
                writer.write(*converted)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    try:
        return _finish(proc, out, marks, writer.n)
    finally:
        shutil.rmtree(marks.parent, ignore_errors=True)


def cmd_build(args: argparse.Namespace) -> int:
    started = time.monotonic()
    if args.append:
        if args.clean:
            raise BuildError("--append and --clean exclude each other")
        hashes = append(Path(args.out), workers=args.workers)
        head = git(Path(args.out), "rev-parse", "main").strip()
        print(f"appended {len(hashes)} commits to {args.out} in {time.monotonic() - started:.1f} s; HEAD {head}")
        return 0
    hashes = build(Path(args.out), clean=args.clean, workers=args.workers)
    print(f"built {len(hashes)} commits into {args.out} in {time.monotonic() - started:.1f} s; HEAD {hashes[-1]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download every version of one current setid (network)")
    f.add_argument("--setid", required=True)
    f.add_argument("--inn", required=True)
    f.add_argument("--refresh", action="store_true", help="re-read the history even if all listed versions are pinned")
    f.set_defaults(func=cmd_fetch)
    fa = sub.add_parser("fetch-all", help="fetch every setid in molecules/*.json (network)")
    fa.add_argument("--molecules", default=str(MOLECULES))
    fa.add_argument("--limit", type=int, help="only the first N setids (for a trial run)")
    fa.add_argument("--refresh", action="store_true")
    fa.add_argument("--workers", type=int, default=4,
                    help="parallel downloads; the 2 requests/s limit applies to all of them together")
    fa.set_defaults(func=cmd_fetch_all)
    i = sub.add_parser("ingest", help="register downloaded zips in the raw store and pin them")
    i.add_argument("--inn", required=True)
    i.add_argument("--history", required=True, help="DailyMed /history JSON for the setid")
    i.add_argument("zips", nargs="+")
    i.set_defaults(func=cmd_ingest)
    fm = sub.add_parser("fetch-missing", help="re-download pinned versions whose raw zip is absent (network)")
    fm.add_argument("--limit", type=int, help="only the first N missing versions")
    fm.add_argument("--workers", type=int, default=4)
    fm.add_argument("--dry-run", action="store_true", help="only count the missing raw zips")
    fm.set_defaults(func=cmd_fetch_missing)
    c = sub.add_parser("coverage", help="listed vs fetched versions, written to reports/coverage-us.json")
    c.add_argument("--molecules", default=str(MOLECULES))
    c.set_defaults(func=cmd_coverage)
    b = sub.add_parser("build", help="convert pinned versions and commit them")
    b.add_argument("--out", default=str(ROOT / "build" / "us"))
    b.add_argument("--clean", action="store_true", help="delete and rebuild an existing build repo")
    b.add_argument("--workers", type=int, help="converter processes (default: CPU count)")
    b.add_argument("--append", action="store_true",
                   help="append the pinned versions HEAD does not have (each needs an 'added' batch date)")
    b.set_defaults(func=cmd_build)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (BuildError, spl.SplError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.modules.setdefault("build", sys.modules[__name__])  # archive.py's "import build" must reuse this module
    sys.exit(main())
