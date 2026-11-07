"""Tools/daily.py: the daily append job on fixtures, its fetcher rules, the report and tags, push, and the workflow.

No network: every request goes through a fake opener. The US fixture run builds the Synthroid label from
its pinned raw zips and synthesises version 1538 from the 1537 zip (skipped when raw/sha256/ lacks them,
like test_build). The EU fixture run converts one real annex with .venv-eu (skipped without it); the older
annexes use a pre-filled conversion cache. The push test uses temporary git repos with bare remotes.
"""
import contextlib
import copy
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import daily  # noqa: E402
import eu  # noqa: E402
import labels  # noqa: E402

FIX = Path(__file__).parent / "daily_fixtures"
EU_FIX = Path(__file__).parent / "eu_fixtures"
WORKFLOW = TOOLS.parent / ".github" / "workflows" / "daily-update.yml"
SETID = "1e11ad30-1041-4520-10b0-8f9d30d30fcc"
SOURCE = build.SOURCES / f"{SETID}.json"
DAY = "2026-11-03"


def raw_available() -> bool:
    return SOURCE.exists() and all(build.raw_path(v["sha256"]).exists() for v in build.load_sources(SOURCE)["versions"])


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Serves bytes by URL; an int answers with that HTTP status; a missing URL answers 404."""

    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        assert req.get_header("User-agent") == build.USER_AGENT
        body = self.pages.get(url, 404)
        if isinstance(body, int):
            raise urllib.error.HTTPError(url, body, "x", {}, io.BytesIO(b""))
        return FakeResponse(body)


def fetcher(tmp: Path, pages: dict, **kw) -> daily.DailyFetcher:
    return daily.DailyFetcher(tmp / "day", opener=FakeOpener(pages), min_gap_s=0, backoff=(0, 0),
                              sleep=lambda s: None, **kw)


def history_json(dates: dict[int, str], title: str | None) -> bytes:
    rows = [{"spl_version": v, "published_date": build_date(d)} for v, d in sorted(dates.items(), reverse=True)]
    return json.dumps({"data": {"spl": {"title": title}, "history": rows},
                       "metadata": {"total_pages": 1}}).encode()


def build_date(iso: str) -> str:
    from datetime import date
    return date.fromisoformat(iso).strftime("%b %d, %Y")


def history_url(setid: str) -> str:
    return f"{build.DAILYMED}/services/v2/spls/{setid}/history.json?pagesize=100&page=1"


def strip_timing(rep: dict) -> dict:
    rep = copy.deepcopy(rep)
    for key in daily.TIMING_FIELDS:
        rep["run"].pop(key, None)
    return rep


class UnitTest(unittest.TestCase):
    def test_capture_sentence_separates_downloads_from_changes(self):
        cap = {"date": "2026-11-05", "previous": "2026-11-04", "products": 206, "changed": [], "pdf_changed": [],
               "new": [], "pi_downloaded": [f"EMEA-H-C-{i:06d}" for i in range(19)]}
        self.assertEqual(daily.capture_sentence(cap),
                         "Capture 2026-11-05 against 2026-11-04: 206 products, 0 changed in the EMA data (0 with a new "
                         "product-information PDF). 19 product-information PDFs downloaded because the EPAR document "
                         "date or URL moved or no earlier copy was pinned; 19 of them are byte-identical to the earlier "
                         "copy, so not a change.")
        cap.update(changed=["A", "B"], pdf_changed=["A"], new=["B"], pi_downloaded=["A", "B", "C"])
        self.assertTrue(daily.capture_sentence(cap).endswith(
            "3 product-information PDFs downloaded because the EPAR document date or URL moved or no earlier copy was "
            "pinned; 1 of them is byte-identical to the earlier copy, so not a change."))
        cap.update(previous=None, changed=[], pdf_changed=[], new=["A"], pi_downloaded=["A"])
        self.assertEqual(daily.capture_sentence(cap),
                         "Capture 2026-11-05 (first capture): 206 products, 0 changed in the EMA data (0 with a new "
                         "product-information PDF). 1 product-information PDF downloaded because the EPAR document "
                         "date or URL moved or no earlier copy was pinned.")
        cap.update(pi_downloaded=[])
        self.assertNotIn("downloaded", daily.capture_sentence(cap))

    def test_report_markdown_plurals(self):
        rep = {"date": "2026-11-05", "summary": "s", "us": {"appended_count": 1, "listing": {"rows": 1, "pages": 1}},
               "eu": {"appended_count": 1}, "run": {"mode": "network", "requests_total": 1}}
        md = daily.report_markdown(rep)
        self.assertIn("| Appended | 1 version | 1 decision |", md)
        self.assertIn("1 row in 1 page;", md)
        self.assertIn("Mode network; 1 request ", md)
        rep["us"]["appended_count"], rep["eu"]["appended_count"] = 2, 0
        self.assertIn("| Appended | 2 versions | 0 decisions |", daily.report_markdown(rep))

    def test_kind_of_urls(self):
        self.assertEqual(daily.kind_of(daily.LISTING_URL.format(day="2026-10-30", page=1)), "json")
        self.assertEqual(daily.kind_of(history_url(SETID)), "json")
        self.assertEqual(daily.kind_of(build.source_url(SETID, 3)), "zip")
        self.assertEqual(daily.kind_of(f"{build.DAILYMED}/archives/index.cfm?query=x"), "html")

    def test_next_batch(self):
        self.assertEqual(daily.next_batch(DAY, set(), set()), DAY)
        self.assertEqual(daily.next_batch(DAY, {"2026-11-02"}, {"2026-11-02"}), DAY)
        self.assertEqual(daily.next_batch(DAY, {DAY}, set()), DAY)                    # pinned, not committed yet
        self.assertEqual(daily.next_batch(DAY, {DAY}, {DAY}), f"{DAY}.2")             # a second run of the day
        self.assertEqual(daily.next_batch(DAY, {DAY, f"{DAY}.2"}, {DAY, f"{DAY}.2"}), f"{DAY}.3")
        self.assertLess(build.entry_key(DAY, "2026-01-01", "a", 1), build.entry_key(f"{DAY}.2", "2001-01-01", "a", 1))
        self.assertLess(build.entry_key(f"{DAY}.9", "2030-01-01", "a", 1), build.entry_key("2026-11-04", "2001-01-01", "a", 1))
        self.assertTrue(daily.in_day(f"{DAY}.2", DAY))
        self.assertFalse(daily.in_day("2026-11-30", "2026-11-3"))

    def test_summary_line(self):
        us = {"appended_count": 13, "record_to": "2026-11-04"}
        eup = {"appended_count": 1, "record_to": "2026-11-02"}
        self.assertEqual(daily.summary_line("2026-11-05", us, eup),
                         "Daily update 2026-11-05: 13 US versions, 1 EU decision appended; US record to 2026-11-04, "
                         "EU record to 2026-11-02")

    def test_us_candidates_ignore_the_days_own_batch(self):
        sources = {SETID: {"setid": SETID, "inn": "levothyroxine", "versions": [
            {"version": 5}, {"version": 6, "added": DAY}]}}
        rows = [{"setid": SETID, "version": 6, "published_date": DAY, "title": "x"},
                {"setid": "other", "version": 2, "published_date": DAY, "title": "y"}]
        cands = daily.us_candidates(rows, sources, DAY)
        self.assertEqual([(c["setid"], c["pinned_max"], c["action"]) for c in cands], [(SETID, 5, "fetch")])
        self.assertEqual(daily.us_candidates(rows, sources, "2026-11-04")[0]["action"], "pinned")

    def test_pi_changed_since_and_watch_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            caps = Path(tmp)
            for day, sha in (("2026-01-01", "a"), ("2026-02-01", "b"), ("2026-03-01", "b")):
                (caps / f"{day}.json").write_text(json.dumps({"products": {"EMEA-H-C-000001": {"pi_sha256": sha},
                                                                          "EMEA-H-C-000002": {"pi_sha256": "z"}}}))
            self.assertEqual(daily.pi_changed_since(caps, "2026-12-01"), {"EMEA-H-C-000001": ("2026-02-01", "b")})
            self.assertEqual(daily.pi_changed_since(caps, "2026-01-15"), {})


class RetryWindowTest(unittest.TestCase):
    """The bounded retries of versions and annexes the daily path could not download."""

    def test_us_problem_versions_are_retried_by_the_sweep_for_30_days(self):
        src = {"versions": [{"version": 20}], "problems": {"19": "HTTP 404", "5": "old", "history": "x"},
               "problem_since": {"19": "2026-11-03"}}
        raw = Path("/nonexistent")
        self.assertEqual(build.pinned_versions(src, raw, True), {20, 19, 5})              # weekday: left alone
        self.assertEqual(build.pinned_versions(src, raw, True, "2026-10-09"), {20, 5})    # sweep: 19 retried
        self.assertEqual(build.pinned_versions(src, raw, True, "2026-11-04"), {20, 19, 5})  # past 30 days
        self.assertEqual(build.sweep_retry_versions(src, "2026-10-09"), [19])
        self.assertEqual(build.sweep_retry_versions(src, "2026-11-04"), [])
        self.assertEqual(daily.sweep_retry_from("2026-11-08"), "2026-10-09")
        build.log_problem(src, 21, "HTTP 404 again", "2026-11-04.2")
        build.log_problem(src, 19, "HTTP 404 again", "2026-11-08")                        # first failure kept
        self.assertEqual(src["problem_since"], {"19": "2026-11-03", "21": "2026-11-04"})
        build.log_problem(src, 22, "x", None)                                             # full build: no date
        self.assertNotIn("22", src["problem_since"])
        build.upsert_version(src, {"version": 19, "sha256": "a"})
        self.assertNotIn("19", src["problems"])
        self.assertEqual(src["problem_since"], {"21": "2026-11-04"})
        build.upsert_version(src, {"version": 21, "sha256": "b"})
        self.assertNotIn("problem_since", src)

    def test_eu_annex_retry_window(self):
        a = {"status": "logged-http", "first_failed": "2026-11-03"}
        self.assertFalse(eu.retry_now(a, False, "2026-11-04"))
        self.assertTrue(eu.retry_now(a, True, "2026-11-04"))
        self.assertTrue(eu.retry_now(a, True, "2026-12-03.2"))                            # day 30
        self.assertFalse(eu.retry_now(a, True, "2026-12-04"))                             # day 31
        self.assertFalse(eu.retry_now({"status": "logged-http"}, True, "2026-11-04"))     # full fetch logged it
        self.assertTrue(eu.retry_now({"status": "logged-http"}, True, None))              # eu.py fetch --retry-logged


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_403_429_and_challenge_stop_without_retry(self):
        url = history_url(SETID)
        for answer in (403, 429, b"<html>Just a moment...</html>"):
            with self.subTest(answer=answer):
                f = fetcher(self.base, {url: answer})
                with self.assertRaises(daily.RunAborted):
                    f.fetch(url)
                self.assertEqual(f.opener.calls, [url])
                self.assertIsInstance(daily.RunAborted("x"), build.FetchAborted)   # build.fetch_setid re-raises it

    def test_404_and_transient_are_replayed_offline(self):
        ok, gone, flaky = history_url("a"), history_url("b"), history_url("c")
        f = fetcher(self.base, {ok: b'{"data": []}', gone: 404, flaky: 503})
        self.assertEqual(f.fetch(ok), b'{"data": []}')
        with self.assertRaises(build.NotAvailable):
            f.fetch(gone)
        with self.assertRaises(build.BuildError):
            f.fetch(flaky)
        self.assertEqual(len(f.opener.calls), 1 + 1 + 3)
        off = daily.DailyFetcher(self.base / "day", offline=True)
        self.assertEqual(off.fetch(ok), b'{"data": []}')
        with self.assertRaises(build.NotAvailable):
            off.fetch(gone)
        with self.assertRaises(build.BuildError) as ctx:
            off.fetch(flaky)
        self.assertNotIsInstance(ctx.exception, build.FetchAborted)
        with self.assertRaises(daily.RunAborted):
            off.fetch(history_url("never-asked"))
        self.assertEqual(off.requests, 0)

    def test_transient_errors_that_then_succeed_are_counted(self):
        url = history_url("t")

        class Flaky(FakeOpener):
            def __call__(self, req, timeout=None):
                self.calls.append(req.full_url)
                if len(self.calls) == 1:
                    raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b""))
                return FakeResponse(b'{"data": []}')

        f = daily.DailyFetcher(self.base / "day", opener=Flaky({}), min_gap_s=0, backoff=(0, 0), sleep=lambda s: None)
        self.assertEqual(f.fetch(url), b'{"data": []}')
        self.assertEqual(dict(f.transient), {"500": 1})
        self.assertEqual(sum(f.by_host.values()), 2)

    def test_budget_guard(self):
        pages = {history_url(str(i)): b"{}" for i in range(3)}
        f = fetcher(self.base, pages, budget=2)
        f.fetch(history_url("0"))
        f.fetch(history_url("1"))
        with self.assertRaises(daily.RunAborted) as ctx:
            f.fetch(history_url("2"))
        self.assertIn("budget of 2", str(ctx.exception))
        self.assertEqual(len(f.opener.calls), 2)

    def test_hosts_outside_the_list_are_refused(self):
        f = fetcher(self.base, {})
        with self.assertRaises(daily.DailyError):
            f.fetch("https://www.accessdata.fda.gov/scripts/cder/safetylabelingchanges/")
        self.assertEqual(f.opener.calls, [])


def synthesise_next_version(entry: dict, version: int, effective: str, edit: tuple[bytes, bytes]) -> bytes:
    """The pinned zip of ``entry`` with versionNumber, the document effectiveTime and one sentence changed
    (images dropped)."""
    data = build.raw_path(entry["sha256"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        xml = zf.read(name)
    xml = re.sub(rb'<versionNumber value="\d+"/>', b'<versionNumber value="%d"/>' % version, xml, count=1)
    xml = re.sub(rb'<effectiveTime value="\d+"/>', b'<effectiveTime value="%s"/>' % effective.encode(), xml, count=1)
    assert edit[0] in xml
    xml = xml.replace(edit[0], edit[1], 1)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo(name, date_time=(2026, 10, 29, 0, 0, 0))
        zf.writestr(info, xml)
    return out.getvalue()


@unittest.skipUnless(raw_available(), "pinned raw zips of the Synthroid label not present; run tools/build.py fetch")
class UsDailyRunTest(unittest.TestCase):
    """One fixture setid (Synthroid) with version 1538 listed: fetch, pin, append, tag, report; then a full
    rebuild, idempotent reruns (network and offline), and the sweep's retirement."""

    EDIT = (b"Consider the potential for food or drug interactions", b"Consider the potential for food, herbal "
            b"or drug interactions")

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.sources = self.tmp / "sources"
        self.sources.mkdir()
        shutil.copyfile(SOURCE, self.sources / SOURCE.name)
        self.molecules = self.tmp / "molecules"
        self.molecules.mkdir()
        (self.molecules / "levothyroxine.json").write_text(json.dumps(
            {"inn": "levothyroxine", "name": "LEVOTHYROXINE", "rank": 1,
             "us": [{"setid": SETID, "brand_names": ["SYNTHROID"], "marketing_categories": ["NDA"]}]}, indent=2))
        self.repo = self.tmp / "us"
        self.old = build.build(self.repo, sources_dir=self.sources)
        src = build.load_sources(SOURCE)
        newest = max(src["versions"], key=lambda v: v["version"])
        self.zip = synthesise_next_version(newest, 1538, "20261029", self.EDIT)
        self.zip_path = build.raw_path(build.sha256_of(self.zip))
        self.zip_existed = self.zip_path.exists()
        dates = {v["version"]: v["published_date"] for v in src["listed_versions"]}
        self.record_to = max(dates.values())
        dates[1538] = "2026-10-30"
        self.pages = {daily.LISTING_URL.format(day=self.record_to, page=1): (FIX / "spls-listing-p1.json").read_bytes(),
                      history_url(SETID): history_json(dates, "SYNTHROID (LEVOTHYROXINE SODIUM) TABLET [ABBVIE INC.]"),
                      build.source_url(SETID, 1538): self.zip}

    def tearDown(self):
        if not self.zip_existed:
            self.zip_path.unlink(missing_ok=True)
        self.tmp_dir.cleanup()

    def config(self, pages: dict | None = None, **kw) -> daily.Config:
        self.opener = FakeOpener(self.pages if pages is None else pages)
        args = dict(day=DAY, us_repo=self.repo, eu_repo=None, sources_us=self.sources, molecules=self.molecules,
                    reports=self.tmp / "reports", manifest=self.tmp / "manifest.json", daily_root=self.tmp / "daily",
                    readme=False, opener=self.opener, sleep=lambda s: None,
                    fetcher_kw={"min_gap_s": 0, "backoff": (0, 0)})
        args.update(kw)
        return daily.Config(**args)

    def report(self) -> dict:
        return json.loads((self.tmp / "reports" / f"{DAY}.json").read_text())

    def test_fixture_append_rebuild_tag_idempotence_and_offline(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(self.config())
        history = build.git(self.repo, "rev-list", "--reverse", "main").split()
        self.assertEqual(history[:-1], self.old)                                     # old hashes unchanged
        self.assertEqual(len(history), len(self.old) + 1)                            # exactly one new commit
        self.assertEqual(build.git(self.repo, "rev-parse", "main^").strip(), self.old[-1])
        pinned = {v["version"]: v for v in build.load_sources(self.sources / SOURCE.name)["versions"]}
        self.assertEqual(pinned[1538]["added"], DAY)
        self.assertEqual(pinned[1538]["published_date"], "2026-10-30")
        diff = build.git(self.repo, "show", "--format=", "--name-only", "main")
        self.assertIn("43685-7", diff)                                               # the edited section only
        self.assertEqual(rep["us"]["appended_count"], 1)
        self.assertEqual(rep["us"]["appended"][0]["commit"], history[-1])
        self.assertEqual((rep["us"]["head_before"], rep["us"]["head_after"]), (self.old[-1], history[-1]))
        self.assertEqual(rep["us"]["listing"]["rows"], 9)
        self.assertEqual([c["setid"] for c in rep["us"]["candidates"]], [SETID])
        self.assertEqual(rep["dailymed_db_published"], "Oct 30, 2026 08:40:33PM EST")
        self.assertEqual(rep["summary"], f"Daily update {DAY}: 1 US version appended; US record to 2026-10-30")
        self.assertEqual(rep["run"]["requests"], {"dailymed.nlm.nih.gov": 3})
        self.assertEqual(len(self.opener.calls), 3)
        # the tag: annotated, on HEAD, tagger the build committer at 06:00 UTC, message the summary line
        tag = f"daily/{DAY}"
        self.assertEqual(build.git(self.repo, "rev-parse", f"{tag}^{{commit}}").strip(), history[-1])
        fields = build.git(self.repo, "for-each-ref", "--format=%(objecttype)|%(taggername) %(taggeremail)|"
                                                      "%(taggerdate:iso-strict)|%(contents:subject)", f"refs/tags/{tag}")
        self.assertRegex(fields.strip(), "^" + re.escape(f"tag|{build.COMMITTER}|{DAY}T06:00:00") + r"(Z|\+00:00)"
                         + re.escape(f"|{rep['summary']}") + "$")
        self.assertEqual(build.git(self.repo, "describe", "--tags", "main").strip(), tag)
        self.assertEqual(labels.Index(self.repo, self.molecules).tags(), {"at_head": [tag], "latest_daily": tag})
        # manifest: the new HEAD with today's build date and the listing's database date
        m = json.loads((self.tmp / "manifest.json").read_text())["corpora"]["us"]
        self.assertEqual((m["head"], m["build_date"], m["record_to"], m["dailymed_db_published"]),
                         (history[-1], DAY, "2026-10-30", "Oct 30, 2026 08:40:33PM EST"))
        self.assertTrue((self.tmp / "reports" / f"{DAY}.md").read_text().startswith(f"# Daily update {DAY}\n"))
        # a full build from the same sources is the appended history, hash for hash
        self.assertEqual(build.build(self.tmp / "full", sources_dir=self.sources), history)
        # a second run (network) appends nothing and keeps the tag object; the report is the same
        tag_object = build.git(self.repo, "rev-parse", tag).strip()
        with contextlib.redirect_stdout(io.StringIO()):
            again = daily.run(self.config())
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), history)
        self.assertEqual(build.git(self.repo, "rev-parse", tag).strip(), tag_object)
        self.assertEqual({k: v for k, v in again.items() if k != "run"}, {k: v for k, v in rep.items() if k != "run"})
        self.assertEqual(again["run"]["batch"], {"us": f"{DAY}.2"})                 # a new version would be batch .2
        # three offline runs: no request, no commit, byte-identical reports apart from timing
        offline = []
        for _ in range(3):
            with contextlib.redirect_stdout(io.StringIO()):
                daily.run(self.config(pages={}, offline=True))
            offline.append(strip_timing(self.report()))
            self.assertEqual(self.opener.calls, [])
        self.assertEqual(offline[0], offline[1])
        self.assertEqual(offline[1], offline[2])
        self.assertEqual({k: v for k, v in offline[0].items() if k != "run"},
                         {k: v for k, v in rep.items() if k != "run"})
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), history)

    def test_recover_replays_a_day_whose_corpus_push_went_through(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(self.config())
        pushed = build.git(self.repo, "rev-parse", "main").strip()
        # the state of the next runner: corpus remote at the pushed HEAD, pointer and sources at the old state
        build.git(self.repo, "update-ref", "refs/remotes/origin/main", pushed)
        build.git(self.repo, "tag", "-d", f"daily/{DAY}")
        build.git(self.repo, "checkout", "-q", "--detach", self.old[-1])
        build.git(self.repo, "branch", "-f", "main", self.old[-1])
        shutil.copyfile(SOURCE, self.sources / SOURCE.name)
        with self.assertRaises(daily.DailyError) as ctx:
            daily.run(self.config(pages={}, offline=True))
        self.assertIn("origin/main has commits that main lacks", str(ctx.exception))
        with contextlib.redirect_stdout(io.StringIO()):
            again = daily.run(self.config(pages={}, offline=True, recover=True))
        self.assertEqual(build.git(self.repo, "rev-parse", "main").strip(), pushed)
        self.assertEqual({k: v for k, v in again.items() if k != "run"}, {k: v for k, v in rep.items() if k != "run"})
        # a replay that does not reproduce the remote stops red
        build.git(self.repo, "update-ref", "refs/remotes/origin/main", self.old[-2])
        build.git(self.repo, "checkout", "-q", "--detach", self.old[-1])
        build.git(self.repo, "branch", "-f", "main", self.old[-1])
        build.git(self.repo, "update-ref", "refs/remotes/origin/main",
                  build.git(self.repo, "commit-tree", "-p", self.old[-1], "-m", "elsewhere",
                            build.git(self.repo, "rev-parse", f"{self.old[-1]}^{{tree}}").strip(),
                            env={"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@labels.invalid",
                                 "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@labels.invalid"}).strip())
        shutil.copyfile(SOURCE, self.sources / SOURCE.name)
        with self.assertRaises(daily.DailyError) as ctx:
            daily.run(self.config(pages={}, offline=True, recover=True))
        self.assertIn("do not reproduce the pushed commits", str(ctx.exception))

    def test_dry_run_writes_no_pin_commit_or_tag(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(self.config(dry_run=True))
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["us"]["plan"]["fetch"], [SETID])
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), self.old)
        self.assertNotIn(1538, {v["version"] for v in build.load_sources(self.sources / SOURCE.name)["versions"]})
        self.assertEqual(build.git(self.repo, "tag", "--list").strip(), "")
        self.assertEqual(len(self.opener.calls), 1)                                  # the listing only

    def test_removed_setid_is_never_requested(self):
        removed = self.tmp / "removed.json"
        removed.write_text(json.dumps({"about": "x", "removed": [
            {"corpus": "us", "id": SETID, "reason": "rights-holder-request", "requested_by": "Example Inc.",
             "request_date": "2026-11-01", "removed_on": "2026-11-02"}]}))
        cfg = self.config(dry_run=True, removed=removed)
        self.assertNotIn(SETID, daily.us_sources(cfg))
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(cfg)
        self.assertEqual(rep["us"]["plan"]["fetch"], [])
        self.assertEqual([c for c in self.opener.calls if SETID in c], [])                 # the listing only
        with self.assertRaises(build.BuildError) as ctx:                                   # HEAD still holds it
            build.append(self.repo, self.sources, removed=build.removed_ids("us", removed))
        self.assertIn(build.HISTORY_REFUSAL, str(ctx.exception))

    def test_budget_stops_the_run_red_before_anything_is_committed(self):
        with self.assertRaises(daily.RunAborted):
            daily.run(self.config(budget=2))
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), self.old)
        self.assertEqual(build.git(self.repo, "tag", "--list").strip(), "")
        rep = self.report()
        self.assertIn("budget of 2", rep["aborted"])
        self.assertEqual(rep["run"]["requests_total"], 2)

    def test_non_json_listing_fails_red(self):
        pages = dict(self.pages)
        pages[daily.LISTING_URL.format(day=self.record_to, page=1)] = b"<html>maintenance</html>"
        with self.assertRaises(daily.RunAborted):
            daily.run(self.config(pages=pages))
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), self.old)

    def test_sweep_retirement_changes_sources_and_molecules_not_the_corpus(self):
        dates = {v["version"]: v["published_date"] for v in build.load_sources(SOURCE)["listed_versions"]}
        pages = {daily.LISTING_URL.format(day=self.record_to, page=1): (FIX / "spls-listing-empty.json").read_bytes(),
                 history_url(SETID): history_json(dates, None)}
        import archive
        saved = archive.setid_listing
        archive.setid_listing = lambda fetcher, setid, archive_dir=None: []
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rep = daily.run(self.config(pages=pages, sweep=True))
        finally:
            archive.setid_listing = saved
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), self.old)
        src = build.load_sources(self.sources / SOURCE.name)
        self.assertEqual((src["status"], src["retired_on"]), ("retired", DAY))
        mol = json.loads((self.molecules / "levothyroxine.json").read_text())
        self.assertEqual(mol["us"][0]["status"], "retired")
        self.assertEqual(rep["us"]["retired"], [SETID])
        self.assertEqual(rep["us"]["appended_count"], 0)
        self.assertEqual(labels.Index(self.repo, self.molecules).status(SETID)["status"], "retired")
        self.assertEqual(build.git(self.repo, "rev-parse", f"daily/{DAY}^{{commit}}").strip(), self.old[-1])
        self.assertEqual(rep["summary"], f"Daily update {DAY}: 0 US versions appended; US record to {self.record_to}")

    def test_sweep_retries_a_version_that_failed_below_a_newer_pin(self):
        newest = max(build.load_sources(SOURCE)["versions"], key=lambda v: v["version"])
        v1539 = synthesise_next_version(newest, 1539, "20261030", self.EDIT)
        v1538 = synthesise_next_version(newest, 1538, "20261029", (self.EDIT[0], b"Consider the potential for "
                                                                               b"food or other drug interactions"))
        made = [build.raw_path(build.sha256_of(z)) for z in (v1539, v1538)]
        existed = [m.exists() for m in made]
        try:
            dates = {v["version"]: v["published_date"] for v in build.load_sources(SOURCE)["listed_versions"]}
            dates.update({1538: "2026-10-30", 1539: "2026-10-31"})
            title = "SYNTHROID (LEVOTHYROXINE SODIUM) TABLET [ABBVIE INC.]"
            day_a, day_b = DAY, "2026-11-08"
            pages = {daily.LISTING_URL.format(day=self.record_to, page=1): (FIX / "spls-listing-p1.json").read_bytes(),
                     history_url(SETID): history_json(dates, title), build.source_url(SETID, 1539): v1539}
            with contextlib.redirect_stdout(io.StringIO()):
                daily.run(self.config(pages=pages, day=day_a))          # v1538 answers 404, v1539 is pinned
            after_a = build.git(self.repo, "rev-list", "--reverse", "main").split()
            self.assertEqual(len(after_a), len(self.old) + 1)
            src = build.load_sources(self.sources / SOURCE.name)
            self.assertIn("1538", src["problems"])
            self.assertEqual(src["problem_since"], {"1538": day_a})
            # a weekday run does not ask for v1538 again (it is below the newest pin)
            pages_b = {daily.LISTING_URL.format(day="2026-10-31", page=1): (FIX / "spls-listing-empty.json").read_bytes(),
                       history_url(SETID): history_json(dates, title), build.source_url(SETID, 1538): v1538}
            with contextlib.redirect_stdout(io.StringIO()):
                daily.run(self.config(pages=pages_b, day="2026-11-05"))
            self.assertNotIn(build.source_url(SETID, 1538), self.opener.calls)
            # the Sunday sweep retries it: pinned with the sweep's batch, appended after HEAD
            with contextlib.redirect_stdout(io.StringIO()):
                rep = daily.run(self.config(pages=pages_b, day=day_b, sweep=True))
            history = build.git(self.repo, "rev-list", "--reverse", "main").split()
            self.assertEqual(history[:-1], after_a)
            self.assertEqual(build.git(self.repo, "rev-parse", "main^").strip(), after_a[-1])
            src = build.load_sources(self.sources / SOURCE.name)
            pinned = {v["version"]: v for v in src["versions"]}
            self.assertEqual(pinned[1538]["added"], day_b)
            self.assertNotIn("1538", src["problems"])
            self.assertNotIn("problem_since", src)
            self.assertEqual(rep["run"]["sweep_retries"],
                             {"from": "2026-10-09", "retried": [f"{SETID} v1538"], "resolved": [f"{SETID} v1538"]})
            self.assertIn("Sweep retried 1 problem version logged", (self.tmp / "reports" / f"{day_b}.md").read_text())
            self.assertEqual(build.build(self.tmp / "full", sources_dir=self.sources), history)
        finally:
            for m, e in zip(made, existed):
                if not e:
                    m.unlink(missing_ok=True)


def eu_python_ready() -> bool:
    py = eu.eu_python()
    if not py.exists():
        return False
    try:
        eu.converter_identity(py)
    except eu.EUError:
        return False
    return True


@unittest.skipUnless(eu_python_ready(), ".venv-eu with PyMuPDF not present (ASOF_EU_PYTHON)")
class EuDailyRunTest(unittest.TestCase):
    """The Ozempic page (h1251) with one extra procedure whose English annex is a real fixture PDF: the daily
    EU step pins it with added, converts it and appends one commit; a full build gives the same history."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmp_dir.name)
        self.raw, self.cache_root = self.tmp / "raw", self.tmp / "cache"
        self.sources, self.molecules = self.tmp / "sources", self.tmp / "molecules"
        self.molecules.mkdir()
        self.ref = {"dir": "EMEA-H-C-004174", "ema_product_number": "EMEA/H/C/004174", "name": "Ozempic",
                    "eu_number": "EU/1/17/1251", "h_page_url": f"{eu.REGISTER}/html/h1251.htm", "mah": "Novo Nordisk A/S",
                    "medicine_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/ozempic",
                    "history_type": "in-force", "comparable_to_us": "yes"}
        self.products_path = self.tmp / "products.json"
        eu.write_json(self.products_path, {"rule": eu.SELECTION_RULE_NAME, "molecules": {
            "semaglutide": {"name": "SEMAGLUTIDE", "reference": self.ref, "alternates": []}}})
        (self.molecules / "semaglutide.json").write_text(json.dumps({"inn": "semaglutide", "name": "SEMAGLUTIDE",
                                                                     "us": []}))
        page = (EU_FIX / "h1251.htm").read_bytes()
        plan = [a for a in eu.plan_annexes(eu.parse_hpage(page)["procedures"]) if a["status"] == "pending"]
        self.pages = {self.ref["h_page_url"]: page}
        self.python = eu.eu_python()
        ident = eu.converter_identity(self.python)
        cache = eu.cache_dir_for(ident, self.cache_root)
        cache.mkdir(parents=True)
        for i, a in enumerate(plan):   # older annexes: fake PDFs with pre-filled conversion results
            body = b"%PDF-1.4 fixture annex " + str(i).encode()
            self.pages[a["url"]] = body
            sha = build.sha256_of(body)
            text = "# 4.3 Contraindications\n\n" + "\n".join(f"Line {j}." for j in range(i + 1)) + "\n"
            (cache / f"{sha}.json").write_text(json.dumps({"status": "converted", "sha256": sha, "warnings": [],
                                                           "smpcs": [{}], "sections": {"smpc-4.3-contraindications": text}}))
        f = eu.EUFetcher(opener=FakeOpener(self.pages), min_gap_s=0, backoff=(0, 0), sleep=lambda s: None)
        eu.fetch_product("semaglutide", self.ref, f, self.sources, self.raw)
        self.repo = self.tmp / "eu"
        res = eu.build_eu(self.repo, self.products_path, self.sources, self.raw, self.tmp / "captures", self.cache_root,
                          workers=1, python=self.python, log=lambda *a, **k: None)
        eu.sync_products(res["outcomes"], self.products_path)
        self.old = res["hashes"]
        # the next day's page: one more procedure, its English annex a real PDF
        proc = json.loads(json.dumps(eu.parse_hpage(page)["procedures"][-1]))
        proc.update(id="999001", closed="2026-11-02", type="Centralised - Variation", ema_number="EMEA/H/C/004174/II/0099",
                    decision={"number": "(2026) 9999", "date": "2026-11-02"})
        text = page.decode("utf-8")
        m = re.search(r"\bvar\s+dataSet_proc\s*=\s*\[", text)
        self.new_page = (text[:m.end()] + json.dumps(proc) + "," + text[m.end():]).encode("utf-8")
        self.new_url = eu.annex_url("999001", "2026-11-02")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def config(self, pages: dict, **kw) -> daily.Config:
        self.opener = FakeOpener(pages)
        args = dict(day=DAY, us_repo=None, eu_repo=self.repo, products=self.products_path, sources_eu=self.sources,
                    captures=self.tmp / "captures", molecules=self.molecules, reports=self.tmp / "reports",
                    manifest=None, daily_root=self.tmp / "daily", raw_eu=self.raw, converted=self.cache_root,
                    eu_python=self.python, capture=False, readme=False, opener=self.opener, sleep=lambda s: None,
                    fetcher_kw={"min_gap_s": 0, "backoff": (0, 0)})
        args.update(kw)
        return daily.Config(**args)

    def test_new_decision_is_appended_and_equals_a_full_build(self):
        pages = {self.ref["h_page_url"]: self.new_page, self.new_url: (EU_FIX / "eliquis-2011-trim.pdf").read_bytes()}
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(self.config(pages))
        history = build.git(self.repo, "rev-list", "--reverse", "main").split()
        self.assertEqual(history[:-1], self.old)
        self.assertEqual(len(history), len(self.old) + 1)
        self.assertEqual(self.opener.calls, [self.ref["h_page_url"], self.new_url])
        src = eu.read_json(self.sources / "EMEA-H-C-004174.json")
        new = next(a for a in src["annexes"] if a["procedure_id"] == "999001")
        self.assertEqual((new["status"], new["added"]), ("fetched", DAY))
        self.assertEqual(rep["eu"]["appended_count"], 1)
        self.assertEqual(rep["eu"]["decisions"][0]["commit"], history[-1])
        self.assertEqual(rep["eu"]["record_to"], "2026-11-02")
        msg = build.git(self.repo, "log", "-1", "--format=%B", "main")
        self.assertIn("Decision-Number: (2026) 9999\n", msg)
        self.assertIn(f"Source-Version: {len(self.old) + 1}\n", msg)
        products = eu.read_json(self.products_path)["molecules"]["semaglutide"]["reference"]
        self.assertEqual(products["build_versions"], len(history))                    # synced for the crosswalk
        mol = json.loads((self.molecules / "semaglutide.json").read_text())
        self.assertEqual(mol["eu"][0]["versions"], len(history))
        self.assertTrue(list((self.tmp / "daily" / DAY / "converted").glob("*.json")))
        rebuilt = eu.build_eu(self.tmp / "full", self.products_path, self.sources, self.raw, self.tmp / "captures",
                              self.cache_root, workers=1, python=self.python, log=lambda *a, **k: None)
        self.assertEqual(rebuilt["hashes"], history)                                   # append == full build
        # the same day again from the cache: no request, no commit, same report
        with contextlib.redirect_stdout(io.StringIO()):
            again = daily.run(self.config({}, offline=True))
        self.assertEqual(self.opener.calls, [])
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), history)
        self.assertEqual({k: v for k, v in again.items() if k != "run"}, {k: v for k, v in rep.items() if k != "run"})

    def test_removed_product_is_never_requested(self):
        removed = self.tmp / "removed.json"
        removed.write_text(json.dumps({"about": "x", "removed": [
            {"corpus": "eu", "id": self.ref["dir"], "reason": "other", "requested_by": "Example Inc.",
             "request_date": "2026-11-01", "removed_on": "2026-11-02"}]}))
        cfg = self.config({self.ref["h_page_url"]: self.new_page}, dry_run=True, removed=removed)
        self.assertEqual(daily.eu_sources(cfg, eu.load_products(self.products_path)), {})
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(cfg)
        self.assertEqual(rep["eu"]["plan"]["product_pages"], 0)
        self.assertEqual(self.opener.calls, [])
        with contextlib.redirect_stdout(io.StringIO()):
            plain = daily.run(self.config({self.ref["h_page_url"]: self.new_page}, dry_run=True))
        self.assertEqual(plain["eu"]["plan"]["product_pages"], 1)

    def test_annex_404_is_retried_the_next_day_and_appended(self):
        day_a, day_b = DAY, "2026-11-04"
        with contextlib.redirect_stdout(io.StringIO()):
            rep_a = daily.run(self.config({self.ref["h_page_url"]: self.new_page}, day=day_a))
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), self.old)
        new = next(a for a in eu.read_json(self.sources / "EMEA-H-C-004174.json")["annexes"]
                   if a["procedure_id"] == "999001")
        self.assertEqual((new["status"], new["first_failed"]), ("logged-http", day_a))
        self.assertEqual([r["procedure_id"] for r in rep_a["eu"]["pending_annexes"]], ["999001"])
        self.assertIn("procedure 999001", (self.tmp / "reports" / f"{day_a}.md").read_text())
        pdf = (EU_FIX / "eliquis-2011-trim.pdf").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            rep_b = daily.run(self.config({self.ref["h_page_url"]: self.new_page, self.new_url: pdf}, day=day_b))
        self.assertIn(self.new_url, self.opener.calls)
        history = build.git(self.repo, "rev-list", "--reverse", "main").split()
        self.assertEqual(history[:-1], self.old)
        self.assertEqual(len(history), len(self.old) + 1)
        new = next(a for a in eu.read_json(self.sources / "EMEA-H-C-004174.json")["annexes"]
                   if a["procedure_id"] == "999001")
        self.assertEqual((new["status"], new["added"]), ("fetched", day_b))
        self.assertNotIn("first_failed", new)
        self.assertEqual(rep_b["eu"]["pending_annexes"], [])
        self.assertEqual(rep_b["eu"]["appended_count"], 1)
        rebuilt = eu.build_eu(self.tmp / "full", self.products_path, self.sources, self.raw, self.tmp / "captures",
                              self.cache_root, workers=1, python=self.python, log=lambda *a, **k: None)
        self.assertEqual(rebuilt["hashes"], history)

    def test_annex_failing_for_more_than_30_days_moves_to_watch(self):
        with contextlib.redirect_stdout(io.StringIO()):
            daily.run(self.config({self.ref["h_page_url"]: self.new_page}, day=DAY))
        late = "2026-12-04"
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(self.config({self.ref["h_page_url"]: self.new_page}, day=late))
        self.assertNotIn(self.new_url, self.opener.calls)                     # no longer retried
        self.assertEqual(rep["eu"]["pending_annexes"], [])
        self.assertEqual([(w["procedure_id"], w["annex_unavailable_since"]) for w in rep["eu"]["watch"]],
                         [("999001", DAY)])

    def test_unchanged_page_leaves_the_sources_file_alone(self):
        path = self.sources / "EMEA-H-C-004174.json"
        before = path.read_bytes()
        changed_page = self.pages[self.ref["h_page_url"]].replace(b"Regeneration time:", b"Regeneration time: x", 1)
        with contextlib.redirect_stdout(io.StringIO()):
            rep = daily.run(self.config({self.ref["h_page_url"]: changed_page}))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(rep["eu"]["appended_count"], 0)
        self.assertEqual(build.git(self.repo, "rev-list", "--reverse", "main").split(), self.old)
        self.assertEqual(build.git(self.repo, "rev-parse", f"daily/{DAY}^{{commit}}").strip(), self.old[-1])


class CaptureCarryForwardTest(unittest.TestCase):
    """eu.capture_day downloads only the product-information PDFs whose URL or EPAR date changed."""

    def test_unchanged_pdf_is_carried_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            refs = {}
            rows, docs, pages = [], [], {}
            for n, (name, updated) in enumerate((("Alpha", "2026-10-01T10:00:00Z"), ("Beta", "2026-10-02T10:00:00Z"))):
                number = f"EMEA/H/C/00000{n + 1}"
                refs[name.lower()] = {"name": name.upper(), "reference": {"dir": number.replace("/", "-"), "name": name,
                                                                          "ema_product_number": number}}
                rows.append({"category": "Human", "name_of_medicine": name, "ema_product_number": number,
                             "medicine_status": "Authorised", "generic": "No", "biosimilar": "No",
                             "international_non_proprietary_name_common_name": name.lower(),
                             "marketing_authorisation_date": "01/01/2010", "medicine_url": "u", "revision_number": "1",
                             "last_updated_date": "01/10/2026"})
                url = f"https://www.ema.europa.eu/en/documents/product-information/{name.lower()}-epar-product-information_en.pdf"
                docs.append({"type": "product-information", "ema_product_number": number, "document_url": url,
                             "last_updated_date": updated})
                pages[url] = b"%PDF-1.7 " + name.encode()
            products = {"molecules": refs}
            products_path = tmp / "products.json"
            eu.write_json(products_path, products)
            pages[eu.EMA_MEDICINES_URL] = json.dumps({"data": rows}).encode()
            pages[eu.EMA_EPAR_DOCUMENTS_URL] = json.dumps({"data": docs}).encode()
            caps = tmp / "captures"
            f1 = fetcher(tmp, pages)
            with contextlib.redirect_stdout(io.StringIO()):
                eu.capture_day("2026-11-01", products, f1, caps, tmp / "raw", products_path)
            self.assertEqual(len(f1.opener.calls), 4)
            docs[1]["last_updated_date"] = "2026-11-02T09:00:00Z"                 # Beta's PDF was updated
            pages[eu.EMA_EPAR_DOCUMENTS_URL] = json.dumps({"data": docs}).encode()
            pages[docs[1]["document_url"]] = b"%PDF-1.7 Beta, revised"
            f2 = daily.DailyFetcher(tmp / "day2", opener=FakeOpener(pages), min_gap_s=0, backoff=(0, 0),
                                    sleep=lambda s: None)
            with contextlib.redirect_stdout(io.StringIO()):
                cap = eu.capture_day("2026-11-02", products, f2, caps, tmp / "raw", products_path, trust_pins=True)
            self.assertEqual(f2.opener.calls, [eu.EMA_MEDICINES_URL, eu.EMA_EPAR_DOCUMENTS_URL, docs[1]["document_url"]])
            self.assertEqual(cap["products"]["EMEA-H-C-000001"]["pi_carried_from"], "2026-11-01")
            self.assertNotIn("pi_carried_from", cap["products"]["EMEA-H-C-000002"])
            self.assertEqual(sorted(cap["changes"]["changed"]), ["EMEA-H-C-000002"])
            self.assertIn("pi_sha256", cap["changes"]["changed"]["EMEA-H-C-000002"])


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


class PushTest(unittest.TestCase):
    """daily.push on temporary repos: both corpus repos push main and the day's tag, the top repo commits the
    changed paths with the summary as message and pushes."""

    def test_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            remotes = {}
            for name in ("us", "eu", "top"):
                remotes[name] = tmp / f"{name}.git"
                run_git(tmp, "init", "-q", "--bare", "-b", "main", str(remotes[name]))
            top = tmp / "top"
            run_git(tmp, "init", "-q", "-b", "main", str(top))
            for repo in (top,):
                run_git(repo, "config", "user.name", "daily test")
                run_git(repo, "config", "user.email", "daily@labels.invalid")
                run_git(repo, "config", "commit.gpgsign", "false")
            (top / "README.md").write_text("x\n")
            run_git(top, "add", "README.md")
            run_git(top, "commit", "-q", "-m", "init")
            run_git(top, "remote", "add", "origin", str(remotes["top"]))
            corpora = {}
            for name in ("us", "eu"):
                repo = tmp / name
                run_git(tmp, "init", "-q", "-b", "main", str(repo))
                (repo / "f.md").write_text(name)
                run_git(repo, "add", "f.md")
                run_git(repo, "-c", "user.name=b", "-c", "user.email=b@labels.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-q", "-m", "c")
                run_git(repo, "remote", "add", "origin", str(remotes[name]))
                daily.tag_day(repo, DAY, f"Daily update {DAY}: test")
                corpora[name] = repo
            reports = top / "reports" / "daily"
            reports.mkdir(parents=True)
            summary = f"Daily update {DAY}: 1 US version, 0 EU decisions appended; US record to 2026-11-02, EU record to 2026-10-19"
            (reports / f"{DAY}.json").write_text(json.dumps({"date": DAY, "dry_run": False, "summary": summary}))
            (top / "manifest.json").write_text("{}\n")
            done = daily.push(top, corpora["us"], corpora["eu"], reports, branch="main")
            self.assertIn(f"top repo: committed '{summary}'", done)
            for name in ("us", "eu"):
                self.assertEqual(run_git(remotes[name], "rev-parse", f"refs/tags/daily/{DAY}^{{commit}}"),
                                 run_git(corpora[name], "rev-parse", "main"))
            self.assertEqual(run_git(remotes["top"], "log", "-1", "--format=%s", "main").strip(), summary)
            files = run_git(remotes["top"], "show", "--name-only", "--format=", "main").split()
            self.assertEqual(sorted(files), ["manifest.json", f"reports/daily/{DAY}.json"])
            # nothing new: no second commit, still pushes cleanly
            again = daily.push(top, corpora["us"], corpora["eu"], reports, branch="main")
            self.assertIn("top repo: nothing to commit", again)
            (reports / f"{DAY}.json").write_text(json.dumps({"date": DAY, "dry_run": True, "summary": summary}))
            with self.assertRaises(daily.DailyError):
                daily.push(top, corpora["us"], corpora["eu"], reports, branch="main")


class PushGuardTest(unittest.TestCase):
    """push refuses any branch but the default one, and retries a rejected top push after a rebase."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.top = Path(self.tmp_dir.name)
        self.reports = self.top / "reports" / "daily"
        self.reports.mkdir(parents=True)
        (self.reports / f"{DAY}.json").write_text(json.dumps({"date": DAY, "dry_run": False, "summary": "s"}))
        self.saved = (daily.sh, daily.staged_changes, daily.default_branch)
        self.calls: list[list[str]] = []
        daily.staged_changes = lambda top: True
        daily.default_branch = lambda top, remote="origin": "main"

    def tearDown(self):
        daily.sh, daily.staged_changes, daily.default_branch = self.saved
        self.tmp_dir.cleanup()

    def fake_sh(self, reject: int):
        def sh(args, cwd):
            self.calls.append(args)
            if args[:2] == ["git", "push"] and sum(1 for c in self.calls if c[:2] == ["git", "push"]) <= reject:
                raise daily.DailyError("git push failed: ! [rejected] HEAD -> main (fetch first)")
            return ""
        return sh

    def test_refuses_a_branch_that_is_not_the_default(self):
        daily.sh = self.fake_sh(0)
        with self.assertRaises(daily.DailyError) as ctx:
            daily.push(self.top, self.top / "us", None, self.reports, DAY, branch="feature")
        self.assertIn("default branch 'main'", str(ctx.exception))
        self.assertEqual(self.calls, [])                                       # nothing pushed

    def test_rejected_top_push_is_rebased_and_retried(self):
        daily.sh = self.fake_sh(1)
        done = daily.push(self.top, self.top / "us", None, self.reports, DAY, branch="main")
        tops = [c for c in self.calls if c[:2] in (["git", "push"], ["git", "pull"])]
        self.assertEqual(tops, [["git", "push", "origin", "HEAD:refs/heads/main"],
                                ["git", "pull", "--rebase", "origin", "main"],
                                ["git", "push", "origin", "HEAD:refs/heads/main"]])
        self.assertIn("top repo: pushed to main", done)
        self.assertTrue(any("rebased" in d for d in done))

    def test_gives_up_red_after_three_retries_with_the_recovery_pointer(self):
        daily.sh = self.fake_sh(99)
        with self.assertRaises(daily.DailyError) as ctx:
            daily.push(self.top, self.top / "us", None, self.reports, DAY, branch="main")
        self.assertIn("Recovery when the corpus push succeeded", str(ctx.exception))
        self.assertEqual(sum(1 for c in self.calls if c[:2] == ["git", "push"]), 4)
        self.assertEqual(sum(1 for c in self.calls if c[:2] == ["git", "pull"]), 3)


class WorkflowTest(unittest.TestCase):
    """The GitHub Action calls commands that exist, with the pinned converter requirements."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_schedule_permissions_runner_and_secrets(self):
        t = self.text
        self.assertIn("cron: '0 6 * * *'", t)
        self.assertIn("workflow_dispatch:", t)
        self.assertRegex(t, r"permissions:\s*\n\s*contents: read")
        self.assertNotIn("contents: write", t)
        self.assertRegex(t, r"group: daily-update")
        self.assertIn("cancel-in-progress: false", t)
        self.assertIn("runs-on: macos-15", t)
        self.assertIn("${{ secrets.ASOF_PUSH_TOKEN }}", t)
        self.assertIn("submodules: recursive", t)
        self.assertIn("fetch-depth: 0", t)
        self.assertIn("actions/checkout@v4", t)
        self.assertIn("actions/setup-python@v5", t)
        self.assertIn("actions/upload-artifact@v4", t)
        self.assertIn("retention-days: 90", t)
        self.assertIn("GITHUB_STEP_SUMMARY", t)
        self.assertNotIn("actions/cache", t)

    def test_run_date_is_validated_and_never_pasted_into_shell(self):
        self.assertIn("[[ $day =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo 'bad date'; exit 1; }", self.text)
        blocks = re.findall(r"\n\s+run: (\|\n(?:\s{10,}.*\n?)+|.*)", self.text)
        self.assertGreaterEqual(len(blocks), 5)
        for block in blocks:
            self.assertNotIn("${{ steps.day", block)
        self.assertIn("DAY: ${{ steps.day.outputs.day }}", self.text)
        self.assertIn("github.ref == 'refs/heads/main'", self.text)

    def test_run_date_check_rejects_an_injected_date(self):
        check = "[[ $day =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo 'bad date'; exit 1; }; echo ok"
        good = subprocess.run(["bash", "-c", check], env={"day": "2026-11-03"}, capture_output=True, text=True)
        bad = subprocess.run(["bash", "-c", check], env={"day": '2026-11-03"; echo INJECTED; echo "'},
                             capture_output=True, text=True)
        self.assertEqual((good.returncode, good.stdout.strip()), (0, "ok"))
        self.assertEqual(bad.returncode, 1)
        self.assertNotIn("INJECTED", bad.stdout)

    def test_plain_scalars_hold_no_colon_space(self):
        # YAML reads ': ' inside an unquoted value as a new key, and GitHub rejects the whole workflow
        # ("You have an error in your yaml syntax"); multi-word commands go in a block scalar (run: |).
        for n, line in enumerate(self.text.splitlines(), 1):
            m = re.match(r"\s*(?:-\s+)?[\w-]+:\s+(.+)$", line)
            if not m or line.lstrip().startswith("#"):
                continue
            value = m.group(1).split(" #")[0]
            if value[0] in "|>'\"[{" or value.startswith("${{"):
                continue
            self.assertNotIn(": ", value, f"line {n}: quote it or use a block scalar")

    def test_requirements_file_is_the_pinned_one(self):
        m = re.search(r"pip install (.+)", self.text)
        self.assertIsNotNone(m)
        self.assertIn("--require-hashes", m.group(1))
        self.assertIn("--only-binary :all:", m.group(1))
        req = re.search(r"-r (\S+)", m.group(1)).group(1)
        self.assertEqual(req, "tools/requirements-eu.txt")
        self.assertIn("pymupdf==1.27.2.2", (TOOLS.parent / req).read_text())
        self.assertIn("--hash=sha256:", (TOOLS.parent / req).read_text())

    def test_daily_commands_exist_with_their_flags(self):
        calls = re.findall(r"python3 -I tools/daily\.py (\w+)([^\n]*)", self.text)
        self.assertEqual(sorted({c for c, _ in calls}), ["push", "run"])
        flags = {"run": {"--dry-run"}, "push": set()}           # --dry-run is passed through a shell variable
        for cmd, rest in calls:
            flags[cmd] |= set(re.findall(r"(--[a-z-]+)", rest))
        self.assertEqual(flags["run"], {"--date", "--sweep-on-sunday", "--dry-run"})
        for cmd, wanted in flags.items():
            with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as ctx:
                daily.main([cmd, "--help"])
            self.assertEqual(ctx.exception.code, 0)
            for flag in wanted:
                self.assertIn(flag, out.getvalue(), f"{cmd} {flag}")
        for env in ("ASOF_US_REPO", "ASOF_EU_REPO", "ASOF_EU_PYTHON", "ASOF_RAW"):
            self.assertIn(env, self.text)
        self.assertEqual(build.RAW_ENV, "ASOF_RAW")
        self.assertEqual(labels.US_REPO_ENV, "ASOF_US_REPO")
        self.assertEqual(eu.EU_PYTHON_ENV, "ASOF_EU_PYTHON")


if __name__ == "__main__":
    unittest.main()
