#!/usr/bin/env python3
"""Daily update job: append the day's new label versions to both corpus repos, tag them, and report.

  python3 -I tools/daily.py run [--date YYYY-MM-DD] [--offline] [--dry-run] [--sweep | --sweep-on-sunday]
                                [--budget N] [--raw DIR] [--us-repo DIR] [--eu-repo DIR]
  python3 -I tools/daily.py push [--date YYYY-MM-DD]

run, in order:
  US  1. DailyMed's daily listing (services/v2/spls.json, published_date >= the corpus record_to, 100 rows a
         page). Rows whose setid has a sources/us/<setid>.json are candidates; a candidate whose listed
         version is newer than its newest pinned version is fetched through build.fetch_setid (one /history
         request plus one zip per new version, setid and version checked inside the zip) and pinned with
         "added": <batch>.
      2. --sweep (Sundays in CI): /history of every current setid, the safety net for a version listed with an
         older date and the detector of retirements (null title: status retired in sources/us and in
         molecules/, archived versions fetched; the corpus itself is not touched).
      3. build.py build --append: one commit per new version on top of HEAD.
  EU  4. eu.py capture (EMA medicines and EPAR documents JSON, product-information PDFs that changed).
      5. Every reference product's Union Register page again; new English annexes pinned with "added": <batch>.
      6. eu.py build --append (PyMuPDF from ASOF_EU_PYTHON, else .venv-eu), then the crosswalk.
  Both 7. an annotated tag daily/<date> on each corpus HEAD (even when nothing was appended), manifest.json,
         the README numbers block (tools/readme_numbers.py --write, when present), reports/daily/<date>.json
         and .md.

The batch id is the run date (a second run of a day that already committed versions uses <date>.2, ...), so a
full rebuild from sources commits the same versions in the same order as the chain of appends.

Every request goes through one fetcher: 2 requests per second in total, User-Agent
"ParticulaTech-research/0.1 (drug-label history check)", only dailymed.nlm.nih.gov, ec.europa.eu and
www.ema.europa.eu. Every answer is kept under <raw>/daily/<date>/http/ (the CI artifact), so --offline
replays a day without any request. The run stops red on HTTP 403 or 429, on a page that is not the expected
kind of file (a challenge page), when the request budget is exhausted (default 2,000; with --sweep 2,000 plus
one per current setid), and when an append would change history.

The report describes the day's batch (what sources and the repos hold for it), so running a day again gives
the same report; only the "run" block (mode, requests, seconds) belongs to the run itself.

push (CI only, default branch only): pushes main and daily/<date> of both corpus repos, then commits sources,
molecules, manifest.json, README.md, reports/daily and the submodule pointers in the top repo and pushes it
(a rejected push is rebased and retried 3 times).

Standard library only. Requires Python 3.12+.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import eu  # noqa: E402
import labels  # noqa: E402
import manifest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports" / "daily"
ALLOWED_HOSTS = frozenset({"dailymed.nlm.nih.gov", "ec.europa.eu", "www.ema.europa.eu"})
DEFAULT_BUDGET = 2000
WATCH_DAYS = 90
TAG_PREFIX = "daily/"
TAG_HOUR_S = 6 * 3600       # tag date: the run date at 06:00:00 UTC
LISTING_URL = (f"{build.DAILYMED}/services/v2/spls.json?published_date={{day}}&published_date_comparison=gte"
               f"&pagesize=100&page={{page}}")
TIMING_FIELDS = ("started", "seconds")
TOP_REPO_PATHS = ("sources/us", "sources/eu", "sources/eu-capture", "sources/eu-products.json", "molecules",
                  "manifest.json", "README.md", "reports/daily", "us", "eu")


class DailyError(build.BuildError):
    """The run cannot go on (bad input, history refusal, repo state)."""


class RunAborted(build.FetchAborted):
    """Stop now and fail red: HTTP 403 or 429, a challenge page, the request budget, or an offline miss."""


# ---------------------------------------------------------------- fetcher

class _AllowedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname not in ALLOWED_HOSTS:
            raise urllib.error.HTTPError(newurl, code, f"redirect to a host outside the allowed list: {newurl}",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def kind_of(url: str) -> str:
    """The kind of file a URL must return (checked before it is accepted)."""
    path = urllib.parse.urlsplit(url).path
    if path.endswith(".json"):
        return "json"
    if path.endswith(("getFile.cfm", "getArchivalFile.cfm")):
        return "zip"
    if "/archives/" in path:
        return "html"
    return ""


def looks_like(kind: str, body: bytes) -> bool:
    if kind == "zip":
        return body[:4] == b"PK\x03\x04"
    return eu.looks_like(kind, body)


class DailyFetcher(build.Fetcher):
    """One fetcher for the whole run (US and EU): build.Fetcher's shared 2 requests/s slot, User-Agent and
    request log, plus the daily rules. Online, every accepted answer is stored under ``cache_dir``/http
    (index.tsv maps URL to file); offline, answers come only from there and a URL not stored stops the run.
    HTTP 403, 429 and a 200 answer of the wrong kind stop the run at once (no retry); 404 and 410 are
    NotAvailable; 5xx and network errors retry with back-off, then raise BuildError (logged by the caller).
    ``budget`` caps the requests of the run. Both ``get`` (build.py, archive.py) and ``fetch`` (eu.py) work."""

    def __init__(self, cache_dir: Path, offline: bool = False, budget: int = DEFAULT_BUDGET, opener=None, **kw):
        if opener is None:
            opener = urllib.request.build_opener(_AllowedRedirect()).open
        cache_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(opener=opener, log_path=None if offline else cache_dir / "requests.tsv", **kw)
        self.cache_dir, self.offline, self.budget = cache_dir, offline, budget
        self.http = cache_dir / "http"
        self.by_host: collections.Counter = collections.Counter()
        self.transient: collections.Counter = collections.Counter()   # retried statuses of requests that then succeeded
        self.reserved = 0
        self.index: dict[str, str] = {}
        index = self.http / "index.tsv"
        if index.exists():
            for line in index.read_text(encoding="utf-8").splitlines():
                url, _, name = line.partition("\t")
                if name:
                    self.index[url] = name

    def _check(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname
        if host not in ALLOWED_HOSTS:
            raise DailyError(f"refusing a request to {host}: only {sorted(ALLOWED_HOSTS)} are allowed")

    def _store(self, url: str, kind: str, body: bytes) -> None:
        digest = hashlib.sha256(body).hexdigest()
        if kind in ("json", "html", "hpage"):
            name, data = f"{digest}.{kind}.gz", gzip.compress(body, mtime=0)
        else:
            name, data = f"{digest}.{kind or 'bin'}", body
        with self._lock:
            path = self.http / name
            if not path.exists():
                self.http.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(data)
                tmp.replace(path)
            if self.index.get(url) != name:
                self.index[url] = name
                with (self.http / "index.tsv").open("a", encoding="utf-8") as fh:
                    fh.write(f"{url}\t{name}\n")

    def _store_failure(self, url: str, status: int | str) -> None:
        """A request that failed (404/410 or transient after retries) is replayed offline as the same failure."""
        with self._lock:
            self.index[url] = f"ERR:{status}"
            self.http.mkdir(parents=True, exist_ok=True)
            with (self.http / "index.tsv").open("a", encoding="utf-8") as fh:
                fh.write(f"{url}\tERR:{status}\n")

    def _replay(self, url: str) -> bytes:
        name = self.index.get(url)
        if name and name.startswith("ERR:"):
            code = name[4:]
            if code in ("404", "410") or (code.isdigit() and int(code) not in build.TRANSIENT):
                raise build.NotAvailable(f"HTTP {code} for {url}")
            raise build.BuildError(f"gave up after retries: {url} ({code})")
        if not name or not (self.http / name).exists():
            raise RunAborted(f"--offline: {url} is not in {self.http}")
        data = (self.http / name).read_bytes()
        return gzip.decompress(data) if name.endswith(".gz") else data

    def fetch(self, url: str, kind: str = "") -> bytes:
        self._check(url)
        kind = kind or kind_of(url)
        if self.offline:
            return self._replay(url)
        host = urllib.parse.urlsplit(url).hostname
        last, retried = "", []
        for attempt in range(len(self.backoff) + 1):
            with self._lock:
                if self.reserved >= self.budget:
                    raise RunAborted(f"request budget of {self.budget} reached before {url}")
                self.reserved += 1
                self.by_host[host] += 1
            status, body, note = self._once(url)
            if status == 200 and looks_like(kind, body):
                self._store(url, kind, body)
                if retried:
                    with self._lock:
                        self.transient.update(retried)
                return body
            if status in (403, 429):
                raise RunAborted(f"HTTP {status} for {url}; the run stops (403 and 429 are never retried)")
            if status == 200:
                raise RunAborted(f"HTTP 200 but not a {kind} file for {url} ({len(body)} bytes): a challenge "
                                 "or error page; the run stops")
            if status in (404, 410) or (status and status not in build.TRANSIENT):
                self._store_failure(url, status)
                raise build.NotAvailable(f"HTTP {status} for {url}")
            last = f"HTTP {status}" if status else note
            retried.append(str(status) if status else "network")
            if attempt < len(self.backoff):
                self.sleep(self.backoff[attempt])
        self._store_failure(url, status or "network")
        raise build.BuildError(f"gave up after retries: {url} ({last})")

    get = fetch


# ---------------------------------------------------------------- configuration

@dataclass
class Config:
    day: str
    us_repo: Path | None
    eu_repo: Path | None
    sources_us: Path = build.SOURCES
    products: Path = eu.PRODUCTS
    sources_eu: Path = eu.SOURCES_EU
    captures: Path = eu.CAPTURES
    molecules: Path = build.MOLECULES
    removed: Path = build.REMOVED      # sources/removed.json: ids removed on request are never requested again
    reports: Path = REPORTS
    manifest: Path | None = manifest.MANIFEST
    daily_root: Path = build.RAW / "daily"
    raw_eu: Path = eu.RAW_EU
    converted: Path = eu.CONVERTED
    eu_python: Path | None = None
    offline: bool = False
    dry_run: bool = False
    sweep: bool = False
    recover: bool = False          # replay a day whose corpus push went through (docs/daily-update.md, Recovery)
    budget: int = DEFAULT_BUDGET
    workers: int = 4
    capture: bool = True
    readme: bool = True
    opener: object = None          # tests: a fake urlopen
    sleep: object = time.sleep
    fetcher_kw: dict = field(default_factory=dict)

    @property
    def day_dir(self) -> Path:
        return self.daily_root / self.day


# ---------------------------------------------------------------- repo state

def git(repo: Path, *args: str, env: dict | None = None) -> str:
    return build.git(repo, *args, env=env)


def us_log(repo: Path) -> list[tuple[str, str, int, str]]:
    """(commit, setid, version, Published-Date) of every commit on main, oldest first."""
    out = git(repo, "log", "--reverse", "--format=%H%x00%(trailers:key=Source-Id,valueonly,separator=)%x00"
                                        "%(trailers:key=Source-Version,valueonly,separator=)%x00"
                                        "%(trailers:key=Published-Date,valueonly,separator=)%x1e", "main")
    rows = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if record:
            commit, source_id, version, published = (x.strip() for x in record.split("\x00"))
            rows.append((commit, source_id.split(":", 1)[-1].lower(), int(version), published))
    return rows


def eu_log(repo: Path) -> list[tuple[str, str, str, str]]:
    """(commit, product folder, procedure id, Effective-Date) of every commit on main, oldest first."""
    out = git(repo, "log", "--reverse", "--format=%H%x00%ae%x00%(trailers:key=Effective-Date,valueonly,separator=)%x1e",
              "main")
    rows = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if record:
            commit, email, day = (x.strip() for x in record.split("\x00"))
            folder, _, key = email.split("@", 1)[0].partition(".")
            rows.append((commit, folder, key, day))
    return rows


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "main").strip()


def ensure_main(repo: Path, recover: bool = False) -> None:
    """A CI submodule checkout is a detached HEAD at the commit the top repo records: make it branch main.
    Refuses when the corpus remote is ahead of that commit (the top repo would fork the corpus history),
    unless ``recover`` (the replay of a day whose corpus push went through; see check_recovered)."""
    detached = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode
    if detached:
        git(repo, "checkout", "-q", "-B", "main")
    remote = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "refs/remotes/origin/main"],
                            capture_output=True, text=True)
    if remote.returncode == 0:
        ok = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", remote.stdout.strip(), "main"])
        if ok.returncode != 0 and not recover:
            raise DailyError(f"{repo.name}: origin/main has commits that main lacks; the top repo's submodule pointer "
                             "is behind the corpus remote. Update the pointer first.")


def check_recovered(repo: Path) -> str:
    """After a --recover replay: main must now be exactly origin/main (the replay reproduced the pushed
    commits); anything else stops the run."""
    remote = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "refs/remotes/origin/main"],
                            capture_output=True, text=True)
    if remote.returncode != 0:
        raise DailyError(f"{repo.name}: --recover needs refs/remotes/origin/main (fetch the corpus remote first)")
    if remote.stdout.strip() != head(repo):
        raise DailyError(f"{repo.name}: the replay gave {head(repo)[:12]}, but origin/main is "
                         f"{remote.stdout.strip()[:12]}; the day's files do not reproduce the pushed commits")
    return head(repo)


def next_batch(day: str, added: set[str], committed: set[str]) -> str:
    """The batch id of this run: the date, unless versions of that date's batch are already committed (a
    second run of the same day), then <date>.2, <date>.3, ..."""
    mine = {a for a in added if a == day or a.startswith(day + ".")}
    if not mine & committed:
        return max(mine, default=day, key=batch_key) if mine else day
    return f"{day}.{max(batch_key(a)[1] for a in mine) + 1}"


def batch_key(added: str) -> tuple[str, int]:
    day, _, n = added.partition(".")
    return day, int(n) if n else 1


def in_day(added: str | None, day: str) -> bool:
    return bool(added) and (added == day or added.startswith(day + "."))


# ---------------------------------------------------------------- US

def us_sources(cfg: Config) -> dict[str, dict]:
    """sources/us by setid, without the setids removed on request (no listing candidate, fetch or sweep
    request is made for them)."""
    removed = build.removed_ids("us", cfg.removed)
    out = {}
    for p in sorted(cfg.sources_us.glob("*.json")):
        src = build.load_sources(p)
        if src["setid"].lower() not in removed:
            out[p.stem] = src
    return out


def listing(cfg: Config, fetcher: DailyFetcher, since: str) -> tuple[list[dict], dict]:
    """Every row DailyMed lists with published_date >= since, and the listing facts. A page that is not the
    listing JSON fails the run (no fallback)."""
    rows, page, meta0 = [], 1, None
    while True:
        url = LISTING_URL.format(day=since, page=page)
        body = fetcher.fetch(url, "json")
        try:
            doc = json.loads(body)
        except ValueError as err:
            raise RunAborted(f"the DailyMed listing page {page} is not JSON: {err}") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("data"), list) or not isinstance(doc.get("metadata"), dict):
            raise RunAborted(f"the DailyMed listing page {page} does not have the data/metadata shape")
        meta0 = meta0 or doc["metadata"]
        for r in doc["data"]:
            try:
                rows.append({"setid": str(r["setid"]).lower(), "version": int(r["spl_version"]),
                             "published_date": build.parse_published(r["published_date"]), "title": r.get("title")})
            except (KeyError, TypeError, ValueError) as err:
                raise RunAborted(f"the DailyMed listing page {page} has a row without setid, spl_version or a "
                                 f"published_date like 'Oct 30, 2026': {r!r} ({err})") from None
        total = int(doc["metadata"].get("total_pages") or 0)
        if page >= total:
            break
        page += 1
    return rows, {"from": since, "pages": page, "rows": len(rows),
                  "total_elements": int(meta0.get("total_elements") or 0),
                  "db_published_date": meta0.get("db_published_date")}


def us_candidates(rows: list[dict], sources: dict[str, dict], day: str) -> list[dict]:
    """One row per in-corpus setid (its highest listed version), with the action: fetch when the listed
    version is newer than every version pinned before the day's batch, else pinned. (Relative to the pins
    before the batch, so a second run of the day plans the same, and fetches nothing new.)"""
    best: dict[str, dict] = {}
    for r in rows:
        if r["setid"] in sources and (r["setid"] not in best or r["version"] > best[r["setid"]]["version"]):
            best[r["setid"]] = r
    out = []
    for setid, r in sorted(best.items()):
        pinned = max((v["version"] for v in sources[setid]["versions"] if not in_day(v.get("added"), day)),
                     default=0)
        out.append(dict(r, inn=sources[setid]["inn"], pinned_max=pinned,
                        action="fetch" if r["version"] > pinned else "pinned"))
    return out


def fetch_many(cfg: Config, fetcher: DailyFetcher, items: list[tuple[str, str]], batch: str,
               retry_from: str | None = None) -> dict[str, list[str]]:
    """fetch_setid (refresh, trust pins, this batch, ``retry_from`` on a sweep) for every (setid, inn); problems
    per setid. A RunAborted or FetchAborted stops all workers and is raised."""
    problems: dict[str, list[str]] = {}
    stop = threading.Event()

    def work(setid: str, inn: str) -> list[str]:
        if stop.is_set():
            return []
        try:
            return build.fetch_setid(setid, inn, fetcher, cfg.sources_us, build.RAW, refresh=True, added=batch,
                                     trust_pins=True, retry_from=retry_from)
        except build.FetchAborted:
            raise
        except (build.BuildError, ValueError, KeyError) as err:   # the history request failed after retries
            return [f"{setid}: {err}"]

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(work, s, i): s for s, i in items}
        for fut in as_completed(futures):
            try:
                found = fut.result()
            except build.FetchAborted:
                stop.set()
                for other in futures:
                    other.cancel()
                raise
            if found:
                problems[futures[fut]] = found
    return dict(sorted(problems.items()))


def retire_in_molecules(molecules_dir: Path, setids: set[str]) -> list[str]:
    """Set status retired on the molecules/<inn>.json us[] entries of these setids; returns the files changed."""
    changed = []
    for path in sorted(p for p in molecules_dir.glob("*.json") if p.name != "selection.json"):
        raw = path.read_text(encoding="utf-8")
        mol = json.loads(raw)
        hit = False
        for entry in mol.get("us") or []:
            if isinstance(entry, dict) and entry.get("setid", "").lower() in setids and entry.get("status") != "retired":
                entry["status"] = "retired"
                hit = True
        if hit:
            path.write_text(eu.molecule_text(mol), encoding="utf-8")
            changed.append(path.name)
    return changed


def us_phase(cfg: Config, fetcher: DailyFetcher, out: dict) -> str:
    """US steps 1-3; fills ``out`` and returns the batch id used."""
    log = us_log(cfg.us_repo)
    if not log:
        raise DailyError(f"{cfg.us_repo} has no commits")
    sources = us_sources(cfg)
    in_repo = {(r[1], r[2]) for r in log}
    today = {(s, v["version"]) for s, src in sources.items() for v in src["versions"] if in_day(v.get("added"), cfg.day)}
    # the listing starts at the record end before the day's batch, so every run of a day lists the same pages
    record_to = max((r[3] for r in log if (r[1], r[2]) not in today), default=log[0][3])
    added = {v["added"] for src in sources.values() for v in src["versions"] if v.get("added")}
    committed = {v["added"] for s, src in sources.items() for v in src["versions"]
                 if v.get("added") and (s, v["version"]) in in_repo}
    batch = next_batch(cfg.day, added, committed)
    rows, facts = listing(cfg, fetcher, record_to)
    cands = us_candidates(rows, sources, cfg.day)
    out.update(listing=facts, record_to_before=record_to, candidates=cands, problems={})
    if cfg.dry_run:
        out["plan"] = {"fetch": [c["setid"] for c in cands if c["action"] == "fetch"],
                       "sweep_setids": sum(1 for s in sources.values() if s.get("status") != "retired") if cfg.sweep else 0}
        return batch
    todo = [(c["setid"], c["inn"]) for c in cands if c["action"] == "fetch"]
    retry_from = sweep_retry_from(cfg.day) if cfg.sweep else None
    eligible = ({s: build.sweep_retry_versions(src, retry_from) for s, src in sources.items()
                 if src.get("status") != "retired"} if cfg.sweep else {})
    problems = fetch_many(cfg, fetcher, todo, batch, retry_from)
    if cfg.sweep:
        before = {s: src.get("status") for s, src in sources.items()}
        done = {s for s, _ in todo}
        rest = [(s, src["inn"]) for s, src in sources.items() if src.get("status") != "retired" and s not in done]
        problems.update(fetch_many(cfg, fetcher, rest, batch, retry_from))
        after = us_sources(cfg)
        retried = [f"{s} v{v}" for s, vs in sorted(eligible.items()) for v in vs]
        resolved = [f"{s} v{v}" for s, vs in sorted(eligible.items()) for v in vs
                    if any(x["version"] == v for x in after[s]["versions"])]
        out["sweep_retries"] = {"from": retry_from, "retried": retried, "resolved": resolved}
        retired = set()
        for s, src in us_sources(cfg).items():
            if src.get("status") == "retired" and before.get(s) != "retired":
                src["retired_on"] = cfg.day          # the day the sweep saw /history's null title
                build.save_sources(cfg.sources_us / f"{s}.json", src)
                retired.add(s)
        retire_in_molecules(cfg.molecules, retired)
    out["problems"] = dict(sorted(problems.items()))
    try:
        build.append(cfg.us_repo, cfg.sources_us, workers=cfg.workers, removed=build.removed_ids("us", cfg.removed))
    except build.BuildError as err:
        if build.HISTORY_REFUSAL in str(err):
            raise DailyError(f"US append refused: {err}") from None
        raise
    return batch


def sweep_retry_from(day: str) -> str:
    """The oldest problem_since a sweep on ``day`` retries (build.RETRY_DAYS before the run date)."""
    return (date.fromisoformat(day) - timedelta(days=build.RETRY_DAYS)).isoformat()


def us_state(cfg: Config) -> dict:
    """What sources/us and the US repo hold for the run date's batch (the same however often the day runs)."""
    log = us_log(cfg.us_repo)
    commit_of = {(s, v): (c, i) for i, (c, s, v, _) in enumerate(log)}
    batch, retired = [], []
    for src in us_sources(cfg).values():
        if src.get("retired_on") == cfg.day:
            retired.append(src["setid"])
        for v in src["versions"]:
            if in_day(v.get("added"), cfg.day):
                c = commit_of.get((src["setid"], v["version"]))
                batch.append({"setid": src["setid"], "inn": src["inn"], "version": v["version"],
                              "published_date": v["published_date"], "added": v["added"],
                              "title": src.get("history_title"), "commit": c[0] if c else None,
                              "_pos": c[1] if c else len(log)})
    batch.sort(key=lambda r: (r["_pos"], r["setid"], r["version"]))
    for r in batch:
        r.pop("_pos")
    first = next((r["commit"] for r in batch if r["commit"]), None)
    before = git(cfg.us_repo, "rev-parse", f"{first}^").strip() if first else log[-1][0]
    return {"appended": batch, "appended_count": sum(1 for r in batch if r["commit"]),
            "batches": sorted({r["added"] for r in batch}, key=batch_key), "retired": sorted(retired),
            "not_committed": [f"{r['setid']} v{r['version']}" for r in batch if not r["commit"]],
            "head_before": before, "head_after": log[-1][0], "commits": len(log),
            "record_to": max(r[3] for r in log)}


# ---------------------------------------------------------------- EU

def eu_sources(cfg: Config, products: dict) -> dict[str, dict]:
    out = {}
    for inn, ref in eu.active_reference_products(products, build.removed_ids("eu", cfg.removed)):
        path = cfg.sources_eu / f"{ref['dir']}.json"
        if path.exists():
            out[ref["dir"]] = eu.read_json(path)
    return out


def eu_phase(cfg: Config, fetcher: DailyFetcher, out: dict) -> str:
    """EU steps 4-6; fills ``out`` and returns the batch id used."""
    products = eu.load_products(cfg.products)
    refs = eu.active_reference_products(products, build.removed_ids("eu", cfg.removed))
    log = eu_log(cfg.eu_repo)
    committed = {(f, k) for _, f, k, _ in log}
    srcs = eu_sources(cfg, products)
    added = {a.get("added") for s in srcs.values() for a in s.get("annexes", []) if a.get("added")}
    done_batches = {a.get("added") for d, s in srcs.items() for a in s.get("annexes", [])
                    if a.get("added") and ((d, str(a["procedure_id"])) in committed or a.get("build_skip"))}
    batch = next_batch(cfg.day, added, done_batches)
    out.update(problems={})
    if cfg.dry_run:
        out["plan"] = {"capture": cfg.capture, "product_pages": len(refs)}
        return batch
    if cfg.capture:
        cap = eu.capture_day(cfg.day, products, fetcher, cfg.captures, cfg.raw_eu, cfg.products, trust_pins=True,
                             removed=build.removed_ids("eu", cfg.removed))
        out["capture_problems"] = cap.get("problems", {})
    problems: dict[str, list[str]] = {}
    stop = threading.Event()

    def work(inn: str, ref: dict) -> list[str]:
        if stop.is_set():
            return []
        try:
            return eu.fetch_product(inn, ref, fetcher, cfg.sources_eu, cfg.raw_eu, retry_logged=True,
                                    refresh_page=True, added=batch, trust_pins=True)
        except build.FetchAborted:
            raise
        except (build.BuildError, ValueError) as err:
            return [f"{ref['dir']}: product page failed: {err}"]

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(work, inn, ref): ref["dir"] for inn, ref in refs}
        for fut in as_completed(futures):
            try:
                found = fut.result()
            except build.FetchAborted:
                stop.set()
                for other in futures:
                    other.cancel()
                raise
            if found:
                problems[futures[fut]] = found
    out["problems"] = dict(sorted(problems.items()))
    try:
        res = eu.append_eu(cfg.eu_repo, cfg.products, cfg.sources_eu, cfg.raw_eu, cfg.converted, workers=2,
                           python=cfg.eu_python, log=lambda *a, **k: None,
                           removed=build.removed_ids("eu", cfg.removed))
    except build.BuildError as err:
        if build.HISTORY_REFUSAL in str(err):
            raise DailyError(f"EU append refused: {err}") from None
        raise
    if res["hashes"] or res["skipped"]:
        out["products_synced"] = eu.sync_after_append(cfg.eu_repo, cfg.products)
        cw = eu.crosswalk(cfg.molecules, cfg.products, cfg.eu_repo)
        out["crosswalk"] = {"changed": cw["changed"], "problems": cw["problems"]}
    if res["hashes"]:
        conv = cfg.day_dir / "converted"
        conv.mkdir(parents=True, exist_ok=True)
        ident = eu.converter_identity(cfg.eu_python or eu.eu_python())
        cache = eu.cache_dir_for(ident, cfg.converted)
        for c in res["commits"]:
            src = cache / f"{c['sha256']}.json"
            if src.exists():
                shutil.copyfile(src, conv / src.name)
    return batch


def pi_changed_since(captures_dir: Path, day: str) -> dict[str, tuple[str, str]]:
    """{product folder: (date the current product-information PDF first appeared after a different one, that
    PDF's SHA-256)} over the captures up to ``day``; a product whose PDF never changed is not listed."""
    since: dict[str, tuple[str, str]] = {}
    last: dict[str, str] = {}
    for path in sorted(captures_dir.glob("*.json")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem) or path.stem > day:
            continue
        for folder, entry in (eu.read_json(path).get("products") or {}).items():
            sha = entry.get("pi_sha256")
            if not sha:
                continue
            if folder in last and last[folder] != sha:
                since[folder] = (path.stem, sha)
            last[folder] = sha
    return since


def eu_state(cfg: Config) -> dict:
    """What sources/eu, the captures and the EU repo hold for the run date's batch."""
    products = eu.load_products(cfg.products)
    log = eu_log(cfg.eu_repo)
    commit_of = {(f, k): (c, i) for i, (c, f, k, _) in enumerate(log)}
    srcs = eu_sources(cfg, products)
    decisions, newest, pending_annexes, stale_annexes = [], {}, [], []
    today = date.fromisoformat(cfg.day)
    for d, src in srcs.items():
        dates = [a["decision_date"] for a in src.get("annexes", []) if a.get("decision_date")]
        newest[d] = max(dates, default=None)
        for a in src.get("annexes", []):
            if a.get("status") == "logged-http" and a.get("first_failed") and a["first_failed"] <= cfg.day:
                row = {"dir": d, "name": src.get("name"), "procedure_id": str(a["procedure_id"]),
                       "decision_date": a.get("decision_date"), "first_failed": a["first_failed"],
                       "reason": a.get("reason")}
                age = (today - date.fromisoformat(a["first_failed"])).days
                (pending_annexes if age <= eu.RETRY_DAYS else stale_annexes).append(row)
            if in_day(a.get("added"), cfg.day):
                c = commit_of.get((d, str(a["procedure_id"])))
                decisions.append({"dir": d, "name": src.get("name"), "inn": src.get("inn"),
                                  "procedure_id": str(a["procedure_id"]), "decision_date": a.get("decision_date"),
                                  "decision_number": a.get("decision_number"), "type": a.get("type"),
                                  "added": a["added"], "commit": c[0] if c else None,
                                  "skipped": a.get("build_skip"), "_pos": c[1] if c else len(log)})
    decisions.sort(key=lambda r: (r["_pos"], r["dir"], int(r["procedure_id"])))
    for r in decisions:
        r.pop("_pos")
    first = next((r["commit"] for r in decisions if r["commit"]), None)
    state = {"decisions": decisions, "appended_count": sum(1 for r in decisions if r["commit"]),
             "batches": sorted({r["added"] for r in decisions}, key=batch_key),
             "head_before": git(cfg.eu_repo, "rev-parse", f"{first}^").strip() if first else (log[-1][0] if log else None),
             "head_after": log[-1][0] if log else None, "commits": len(log),
             "record_to": max((r[3] for r in log), default=None)}
    cap_path = cfg.captures / f"{cfg.day}.json"
    if cap_path.exists():
        cap = eu.read_json(cap_path)
        ch = cap.get("changes") or {}
        pdf_changed = sorted(k for k, v in (ch.get("changed") or {}).items() if "pi_sha256" in v)
        with_decision = {r["dir"] for r in decisions}
        state["capture"] = {"date": cfg.day, "previous": ch.get("previous"), "products": len(cap.get("products", {})),
                            "new": ch.get("new", []), "removed": ch.get("removed", []),
                            "changed": sorted(ch.get("changed") or {}), "pdf_changed": pdf_changed,
                            "fetch_failed": ch.get("fetch_failed", []),
                            "pi_downloaded": sorted(k for k, e in cap.get("products", {}).items()
                                                    if e.get("pi_sha256") and not e.get("pi_carried_from")
                                                    and (e.get("pi_fetched") or "").startswith(cfg.day))}
        state["pending"] = [d for d in pdf_changed if d not in with_decision]
    else:
        state["capture"] = None
        state["pending"] = []
    watch = []
    for d, (since, _) in sorted(pi_changed_since(cfg.captures, cfg.day).items()):
        if (newest.get(d) or "") < since and (today - date.fromisoformat(since)).days > WATCH_DAYS:
            watch.append({"dir": d, "pdf_changed_on": since, "newest_decision": newest.get(d)})
    # an annex the daily path could not download: retried for eu.RETRY_DAYS (pending_annexes), then watched
    for r in sorted(stale_annexes, key=lambda r: (r["dir"], int(r["procedure_id"]))):
        watch.append({"dir": r["dir"], "annex_unavailable_since": r["first_failed"],
                      "procedure_id": r["procedure_id"], "newest_decision": newest.get(r["dir"])})
    state["watch"] = watch
    state["pending_annexes"] = sorted(pending_annexes, key=lambda r: (r["dir"], int(r["procedure_id"])))
    return state


# ---------------------------------------------------------------- tags, manifest, report

def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_line(day: str, us: dict | None, eu_part: dict | None) -> str:
    """'Daily update 2026-11-05: 13 US versions, 2 EU decisions appended; US record to 2026-11-04, EU record
    to 2026-11-04' (the tag message and the top-repo commit subject)."""
    counts = ", ".join(plural(part["appended_count"], word) for part, word in ((us, "US version"),
                                                                               (eu_part, "EU decision")) if part)
    tail = ", ".join(f"{name} record to {part['record_to']}" for name, part in (("US", us), ("EU", eu_part)) if part)
    return f"Daily update {day}: {counts} appended; {tail}"


def committer_env(day: str) -> dict:
    name, _, email = build.COMMITTER.rpartition(" <")
    stamp = build.epoch(day.split(".")[0]) + TAG_HOUR_S
    return {"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email.rstrip(">"),
            "GIT_COMMITTER_DATE": f"@{stamp} +0000"}


def tag_day(repo: Path, day: str, message: str) -> dict:
    """Annotated tag daily/<day> on main (tagger: the build committer, date: day 06:00:00 UTC). A tag that
    exists with the same target and message is kept; otherwise it is made again (status moved)."""
    name = TAG_PREFIX + day
    target = head(repo)
    have = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/tags/{name}"],
                          capture_output=True, text=True)
    status = "created"
    if have.returncode == 0:
        points = git(repo, "rev-parse", f"refs/tags/{name}^{{commit}}").strip()
        msg = git(repo, "for-each-ref", "--format=%(contents)", f"refs/tags/{name}").strip()
        if points == target and msg == message:
            return {"tag": name, "commit": target, "object": have.stdout.strip(), "status": "kept"}
        git(repo, "tag", "-d", name)
        status = "moved"
    git(repo, "tag", "-a", name, "-m", message, target, env=committer_env(day))
    obj = git(repo, "rev-parse", f"refs/tags/{name}").strip()
    return {"tag": name, "commit": target, "object": obj, "status": status}


def write_manifest(cfg: Config, db_published: str | None) -> dict:
    old = json.loads(cfg.manifest.read_text(encoding="utf-8")) if cfg.manifest.exists() else None
    fresh = manifest.compute(cfg.us_repo, cfg.eu_repo, cfg.molecules, cfg.sources_us)
    data = manifest.merge(old, fresh, cfg.day, db_published)
    manifest.write_manifest(cfg.manifest, data)
    return data


def run_readme(cfg: Config) -> str:
    tool = ROOT / "tools" / "readme_numbers.py"
    if not tool.exists():
        return "skipped: tools/readme_numbers.py not present"
    proc = subprocess.run([sys.executable, "-I", str(tool), "--write"], capture_output=True, text=True, cwd=str(ROOT))
    if proc.returncode != 0:
        raise DailyError(f"readme_numbers.py --write failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return "written"


def capture_sentence(cap: dict) -> str:
    """The EMA capture line of the daily report. A product-information PDF is downloaded again whenever its
    EPAR document date or URL differs from the newest earlier capture (or none was pinned), but it counts as
    changed only when its SHA-256 differs, so downloads and changes are reported apart."""
    against = f"against {cap['previous']}" if cap.get("previous") else "(first capture)"
    changed, new_pdf = len(cap["changed"]), len(cap["pdf_changed"])
    text = (f"Capture {cap['date']} {against}: {plural(cap['products'], 'product')}, {changed} changed in the EMA "
            f"data ({new_pdf} with a new product-information PDF).")
    downloaded = cap.get("pi_downloaded") or []
    if downloaded:
        fresh = set(cap["pdf_changed"]) | set(cap.get("new") or [])
        same = sum(1 for d in downloaded if d not in fresh)
        text += (f" {plural(len(downloaded), 'product-information PDF')} downloaded because the EPAR document date "
                 f"or URL moved or no earlier copy was pinned")
        text += (f"; {same} of them {'is' if same == 1 else 'are'} byte-identical to the earlier copy, so not a "
                 "change." if same else ".")
    return text


def report_markdown(rep: dict) -> str:
    us, eup, run = rep.get("us") or {}, rep.get("eu"), rep["run"]
    lines = [f"# Daily update {rep['date']}", "", rep["summary"], ""]
    if rep.get("dry_run"):
        lines += ["Dry run: nothing was pinned, committed or tagged.", ""]
    if rep.get("aborted"):
        lines += [f"Stopped: {rep['aborted']}", ""]
    lines += ["| | US | EU |", "| --- | --- | --- |",
              f"| HEAD before | `{(us.get('head_before') or '')[:12]}` | `{((eup or {}).get('head_before') or '')[:12]}` |",
              f"| HEAD after | `{(us.get('head_after') or '')[:12]}` | `{((eup or {}).get('head_after') or '')[:12]}` |",
              f"| Appended | {plural(us.get('appended_count', 0), 'version')} | "
              f"{plural((eup or {}).get('appended_count', 0), 'decision')} |",
              f"| Record to | {us.get('record_to')} | {(eup or {}).get('record_to')} |",
              f"| Tag | {(rep.get('tags') or {}).get('us', {}).get('tag', 'none')} | "
              f"{(rep.get('tags') or {}).get('eu', {}).get('tag', 'none')} |", ""]
    lst = us.get("listing") or {}
    if rep.get("us"):
        fetch = sum(1 for c in us.get("candidates") or [] if c["action"] == "fetch")
        lines += ["## US (DailyMed)", "",
                  f"Listing from {lst.get('from')}: {plural(lst.get('rows', 0), 'row')} in "
                  f"{plural(lst.get('pages', 0), 'page')}; DailyMed database published "
                  f"{rep.get('dailymed_db_published')}. In-corpus setids listed: {len(us.get('candidates') or [])} "
                  f"({fetch} with a version newer than the pinned one).", ""]
    if us.get("appended"):
        lines += ["| Setid | Molecule | Version | Published | Commit |", "| --- | --- | ---: | --- | --- |"]
        for r in us["appended"]:
            lines.append(f"| `{r['setid']}` | {r['inn']} | {r['version']} | {r['published_date']} | "
                         f"`{(r['commit'] or 'not committed')[:12]}` |")
        lines.append("")
    if us.get("retired"):
        lines += [f"Retired in the sweep (status set in sources/us and molecules/): {', '.join(us['retired'])}", ""]
    for setid, probs in (us.get("problems") or {}).items():
        lines += [f"- Problem `{setid}`: {'; '.join(probs)}"]
    if us.get("problems"):
        lines.append("")
    if eup:
        lines += ["## EU (Union Register, EMA)", ""]
        cap = eup.get("capture")
        if cap:
            lines += [capture_sentence(cap), ""]
        if eup.get("decisions"):
            lines += ["| Product | Procedure | Decision | Decision date | Result |", "| --- | --- | --- | --- | --- |"]
            for r in eup["decisions"]:
                result = f"`{r['commit'][:12]}`" if r["commit"] else f"skipped ({r['skipped'] or 'not committed'})"
                lines.append(f"| {r['name']} ({r['dir']}) | {r['procedure_id']} | {r['decision_number']} | "
                             f"{r['decision_date']} | {result} |")
            lines.append("")
        if eup.get("pending"):
            lines += [f"Product-information PDF changed with no new Commission decision yet (information only): "
                      f"{', '.join(eup['pending'])}", ""]
        if eup.get("pending_annexes"):
            lines += ["Annexes listed on the register but not downloadable yet (retried daily for 30 days from the "
                      "first failure):", ""]
            lines += [f"- {r['name']} ({r['dir']}) procedure {r['procedure_id']}, decision {r['decision_date']}, "
                      f"failing since {r['first_failed']}: {r['reason']}" for r in eup["pending_annexes"]]
            lines.append("")
        if eup.get("watch"):
            lines += ["Watch (no longer retried): " + ", ".join(
                f"{w['dir']} PDF changed {w['pdf_changed_on']}, still no decision" if "pdf_changed_on" in w
                else f"{w['dir']} procedure {w['procedure_id']} annex unavailable since {w['annex_unavailable_since']}"
                for w in eup["watch"]), ""]
        for d, probs in (eup.get("problems") or {}).items():
            lines += [f"- Problem {d}: {'; '.join(probs)}"]
        if eup.get("problems"):
            lines.append("")
    req = run.get("requests") or {}
    tr = run.get("transient_retries") or {}
    lines += ["## Run", "", f"Mode {run['mode']}; {plural(run.get('requests_total', 0), 'request')} "
              f"({', '.join(f'{h} {n}' for h, n in sorted(req.items())) or 'none'}), budget {run.get('budget')}; "
              f"{run.get('seconds')} s. Transient errors retried successfully: {tr.get('count', 0)}"
              + (f" ({', '.join(f'HTTP {c}: {n}' for c, n in sorted(tr['statuses'].items()))})" if tr.get("count") else "")
              + "."]
    sr = run.get("sweep_retries")
    if sr:
        lines += ["", f"Sweep retried {plural(len(sr['retried']), 'problem version')} logged since {sr['from']}; "
                      f"{len(sr['resolved'])} now pinned" + (f": {', '.join(sr['resolved'])}" if sr["resolved"] else "")
                  + "."]
    return "\n".join(lines) + "\n"


def write_report(cfg: Config, rep: dict) -> Path:
    cfg.reports.mkdir(parents=True, exist_ok=True)
    path = cfg.reports / f"{cfg.day}.json"
    path.write_text(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (cfg.reports / f"{cfg.day}.md").write_text(report_markdown(rep), encoding="utf-8")
    cfg.day_dir.mkdir(parents=True, exist_ok=True)
    with (cfg.day_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rep["run"], sort_keys=True) + "\n")
    return path


# ---------------------------------------------------------------- run

def run(cfg: Config) -> dict:
    """One daily run; returns the report (also written to reports/daily/<day>.json and .md). Raises after
    writing the report when the run was stopped."""
    labels.check_date(cfg.day, "date")
    started, t0 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), time.monotonic()
    fetcher = DailyFetcher(cfg.day_dir, offline=cfg.offline, budget=cfg.budget, opener=cfg.opener,
                           sleep=cfg.sleep, **cfg.fetcher_kw)
    mode = "dry-run" if cfg.dry_run else ("offline" if cfg.offline else "network")
    rep: dict = {"date": cfg.day, "dry_run": cfg.dry_run, "sweep": cfg.sweep, "us": None, "eu": None}
    batches: dict[str, str] = {}
    sweep_retries = None
    synced: dict = {}
    error: BaseException | None = None
    try:
        for repo in filter(None, (cfg.us_repo, cfg.eu_repo)):
            if not (repo / ".git").exists():
                raise DailyError(f"{repo} is not a git repo")
            if not cfg.dry_run:
                ensure_main(repo, recover=cfg.recover)
        us_run: dict | None = None
        if cfg.us_repo is not None:
            us_run = {}
            batches["us"] = us_phase(cfg, fetcher, us_run)
            rep["dailymed_db_published"] = us_run["listing"]["db_published_date"]
        eu_run: dict | None = None
        if cfg.eu_repo is not None:
            eu_run = {}
            batches["eu"] = eu_phase(cfg, fetcher, eu_run)
        us = dict(us_run, **us_state(cfg)) if us_run is not None else None
        eup = dict(eu_run, **eu_state(cfg)) if eu_run is not None else None
        if eup:   # what this run's append changed in the crosswalk belongs to the run, not to the day
            synced.update({k: eup.pop(k) for k in ("products_synced", "crosswalk") if k in eup})
        sweep_retries = us.pop("sweep_retries", None) if us else None
        rep.update(us=us, eu=eup, summary=summary_line(cfg.day, us, eup))
        if cfg.recover and not cfg.dry_run:
            for repo in filter(None, (cfg.us_repo, cfg.eu_repo)):
                check_recovered(repo)
        if not cfg.dry_run:
            rep["tags"] = {name: tag_day(repo, cfg.day, rep["summary"])
                           for name, repo in (("us", cfg.us_repo), ("eu", cfg.eu_repo)) if repo is not None}
            for t in rep["tags"].values():
                t.pop("status", None)
            if cfg.manifest is not None:
                m = write_manifest(cfg, rep.get("dailymed_db_published"))
                rep["manifest"] = {name: {k: c.get(k) for k in ("build_date", "head", "record_to", "commits")}
                                   for name, c in m["corpora"].items()}
            if cfg.readme:
                rep["readme"] = run_readme(cfg)
    except (build.BuildError, labels.LabelError, OSError) as err:
        error = err
        rep["aborted"] = f"{type(err).__name__}: {err}"
        rep.setdefault("summary", f"Daily update {cfg.day}: stopped ({type(err).__name__})")
    by_host = dict(sorted(fetcher.by_host.items()))
    rep["run"] = {"mode": mode, "started": started, "seconds": round(time.monotonic() - t0, 1),
                  "requests": by_host, "requests_total": sum(by_host.values()), "budget": cfg.budget,
                  "date": cfg.day, "batch": batches, "eu_crosswalk": synced,
                  "transient_retries": {"count": sum(fetcher.transient.values()),
                                        "statuses": dict(sorted(fetcher.transient.items()))}}
    if sweep_retries is not None:
        rep["run"]["sweep_retries"] = sweep_retries
    write_report(cfg, rep)
    if error is not None:
        raise error
    return rep


# ---------------------------------------------------------------- push (CI)

def sh(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise DailyError(f"{' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout


def latest_report(reports: Path, day: str | None) -> dict:
    if day:
        path = reports / f"{day}.json"
    else:
        found = sorted(p for p in reports.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
        if not found:
            raise DailyError(f"no daily report in {reports}; run 'daily.py run' first")
        path = found[-1]
    if not path.exists():
        raise DailyError(f"{path} does not exist")
    return json.loads(path.read_text(encoding="utf-8"))


def default_branch(top: Path, remote: str = "origin") -> str:
    """The remote's default branch (refs/remotes/<remote>/HEAD), else main."""
    proc = subprocess.run(["git", "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"], cwd=str(top),
                          capture_output=True, text=True)
    name = proc.stdout.strip()
    return name.split("/", 1)[1] if proc.returncode == 0 and "/" in name else "main"


def staged_changes(top: Path) -> bool:
    return subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(top)).returncode != 0


TOP_PUSH_RETRIES = 3
RECOVERY = ("the corpus repos are already pushed; follow 'Recovery when the corpus push succeeded and the top push "
            "did not' in docs/daily-update.md")


def push(top: Path, us_repo: Path, eu_repo: Path | None, reports: Path, day: str | None = None,
         remote: str = "origin", branch: str | None = None, retries: int = TOP_PUSH_RETRIES) -> list[str]:
    """Push both corpus repos (main and daily/<date>), then commit and push the top repo. Returns what was
    done. Refuses a dry-run or a stopped run's report, and a top-repo checkout that is not on the default
    branch (the corpus remotes have one history, recorded by the default branch only). A rejected top push is
    rebased on the remote branch and retried up to ``retries`` times before the run fails red."""
    rep = latest_report(reports, day)
    if rep.get("dry_run") or rep.get("aborted"):
        raise DailyError(f"the report of {rep['date']} is a dry run or a stopped run; nothing is pushed")
    branch = branch or os.environ.get("GITHUB_REF_NAME") or \
        sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], top).strip()
    want = default_branch(top, remote)
    if branch != want:
        raise DailyError(f"push runs only on the default branch {want!r}; this checkout is on "
                         f"{branch if branch not in ('', 'HEAD') else 'a detached HEAD'}; nothing is pushed")
    tag = TAG_PREFIX + rep["date"]
    done = []
    for repo in filter(None, (us_repo, eu_repo)):
        sh(["git", "-C", str(repo), "push", "--follow-tags", remote, "main", f"+refs/tags/{tag}:refs/tags/{tag}"], top)
        done.append(f"pushed {repo.name} main and {tag}")
    paths = [p for p in TOP_REPO_PATHS if (top / p).exists()]
    sh(["git", "add", "--", *paths], top)
    if not staged_changes(top):
        done.append("top repo: nothing to commit")
    else:
        sh(["git", "commit", "-q", "-m", rep["summary"]], top)
        done.append(f"top repo: committed '{rep['summary']}'")
    for attempt in range(retries + 1):
        try:
            sh(["git", "push", remote, f"HEAD:refs/heads/{branch}"], top)
            done.append(f"top repo: pushed to {branch}")
            return done
        except DailyError as err:
            if attempt == retries:
                raise DailyError(f"top repo push failed after {retries} rebase retries ({err}); {RECOVERY}") from None
        try:
            sh(["git", "pull", "--rebase", remote, branch], top)
        except DailyError as err:
            subprocess.run(["git", "rebase", "--abort"], cwd=str(top), capture_output=True)
            raise DailyError(f"top repo: rebase on {remote}/{branch} failed ({err}); {RECOVERY}") from None
        done.append(f"top repo: push rejected, rebased on {remote}/{branch} (retry {attempt + 1} of {retries})")
    return done


# ---------------------------------------------------------------- CLI

def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def count_current(sources_us: Path) -> int:
    return sum(1 for p in sources_us.glob("*.json") if build.load_sources(p).get("status") != "retired")


def cmd_run(args: argparse.Namespace) -> int:
    day = args.date or today_utc()
    labels.check_date(day, "date")
    sweep = args.sweep or (args.sweep_on_sunday and date.fromisoformat(day).isoweekday() == 7)
    budget = args.budget or (DEFAULT_BUDGET + count_current(build.SOURCES) if sweep else DEFAULT_BUDGET)
    eu_repo = labels.resolve_eu_repo(args.eu_repo)
    raw = Path(args.raw).resolve() if args.raw else build.RAW
    if raw != build.RAW:
        raise DailyError(f"--raw {raw} differs from the raw store {build.RAW}; set ASOF_RAW={raw} instead "
                         "(the converter processes read it)")
    cfg = Config(day=day, us_repo=labels.resolve_us_repo(args.us_repo).resolve(),
                 eu_repo=eu_repo.resolve() if eu_repo else None, daily_root=raw / "daily", offline=args.offline,
                 dry_run=args.dry_run, sweep=sweep, budget=budget, workers=args.workers, readme=args.readme,
                 recover=args.recover)
    try:
        rep = run(cfg)
    except (build.BuildError, labels.LabelError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        print(f"report: {cfg.reports / (day + '.json')}", file=sys.stderr)
        return 1
    print(rep["summary"])
    print(f"requests {rep['run']['requests_total']} {rep['run']['requests']}; {rep['run']['seconds']} s; "
          f"report {cfg.reports / (day + '.json')}")
    if rep.get("dry_run"):
        print(json.dumps({"us": rep["us"].get("plan"), "eu": (rep.get("eu") or {}).get("plan")}, indent=2))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    eu_repo = labels.resolve_eu_repo(args.eu_repo)
    try:
        for line in push(ROOT, labels.resolve_us_repo(args.us_repo), eu_repo, REPORTS, args.date):
            print(line)
    except DailyError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="append the day's versions to both corpora, tag, report")
    r.add_argument("--date", help="run date YYYY-MM-DD (default: today, UTC)")
    r.add_argument("--offline", action="store_true", help="replay <raw>/daily/<date>/http only; no request")
    r.add_argument("--dry-run", action="store_true", help="list and plan only: no pin, commit or tag")
    r.add_argument("--sweep", action="store_true", help="also /history for every current setid")
    r.add_argument("--sweep-on-sunday", action="store_true", help="--sweep when the run date is a Sunday")
    r.add_argument("--budget", type=int, help=f"request cap (default {DEFAULT_BUDGET}; with a sweep plus one per "
                                              "current setid)")
    r.add_argument("--raw", help="raw store root (must equal ASOF_RAW, default raw/)")
    r.add_argument("--us-repo", help="US corpus repo (default: env ASOF_US_REPO, else us/, else build/us)")
    r.add_argument("--eu-repo", help="EU corpus repo (default: env ASOF_EU_REPO, else eu/, else build/eu; "
                                     "empty for none)")
    r.add_argument("--workers", type=int, default=4, help="parallel fetch workers (one shared 2 requests/s limit)")
    r.add_argument("--no-readme", dest="readme", action="store_false", help="skip readme_numbers.py --write")
    r.add_argument("--recover", action="store_true",
                   help="replay a day whose corpus push went through but whose top push did not (with --offline): "
                        "allows origin/main ahead of the pointer, then requires main == origin/main")
    r.set_defaults(func=cmd_run)
    q = sub.add_parser("push", help="CI: push both corpus repos, then commit and push the top repo")
    q.add_argument("--date", help="the run date whose report to push (default: the newest report)")
    q.add_argument("--us-repo")
    q.add_argument("--eu-repo")
    q.set_defaults(func=cmd_push)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (build.BuildError, labels.LabelError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.modules.setdefault("daily", sys.modules[__name__])
    sys.exit(main())
