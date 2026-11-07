"""Deterministic rebuild, trailers, known readings, and fetch logic.

The rebuild and reading tests need the pinned raw zips (raw/sha256/); they are
skipped, not failed, on a machine that has not fetched them.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import build  # noqa: E402
import cli  # noqa: E402

READINGS = json.loads((Path(__file__).parent / "known_readings.json").read_text(encoding="utf-8"))
SOURCE = build.SOURCES / f"{READINGS['setid']}.json"


def raw_available() -> bool:
    if not SOURCE.exists():
        return False
    return all(build.raw_path(v["sha256"]).exists() for v in build.load_sources(SOURCE)["versions"])


@unittest.skipUnless(raw_available(), "pinned raw zips not present; run tools/build.py fetch first")
class RebuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sources = Path(cls.tmp.name) / "sources"  # only the Synthroid label, whatever else is pinned
        cls.sources.mkdir()
        (cls.sources / SOURCE.name).write_bytes(SOURCE.read_bytes())
        cls.repo = Path(cls.tmp.name) / "a"
        cls.hashes = build.build(cls.repo, sources_dir=cls.sources)
        cls.versions = cli.versions(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_rebuild_gives_identical_commit_hashes(self):
        again = build.build(Path(self.tmp.name) / "b", sources_dir=self.sources)
        self.assertEqual(self.hashes, again)

    def test_one_commit_per_pinned_version_in_published_order(self):
        pinned = build.load_sources(SOURCE)["versions"]
        self.assertEqual(len(self.hashes), len(pinned))
        published = [v.published for v in self.versions]
        self.assertEqual(published, sorted(published))
        self.assertEqual({v.version for v in self.versions}, {p["version"] for p in pinned})

    def test_format_conversion_flag_only_where_the_format_changes(self):
        log = build.git(self.repo, "log", "--format=%(trailers:key=Source-Version,valueonly,separator=)|"
                                          "%(trailers:key=Format-Conversion,valueonly,separator=)")
        flagged = [line.split("|") for line in log.splitlines() if line.split("|")[1]]
        self.assertEqual(flagged, [["1506", "old-to-PLR"]])

    def test_author_is_the_label_version(self):
        author = build.git(self.repo, "log", "-1", "--format=%an <%ae>").strip()
        v = self.versions[-1]
        self.assertEqual(author, f"Label {v.setid} v{v.version} <{v.setid}.v{v.version}@labels.invalid>")

    def test_refuses_to_overwrite_a_foreign_directory(self):
        foreign = Path(self.tmp.name) / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("x")
        with self.assertRaises(build.BuildError):
            build.build(foreign, sources_dir=self.sources, clean=True)
        self.assertTrue((foreign / "keep.txt").exists())

    def test_known_readings(self):
        lv = cli.resolve_label(self.versions, READINGS["setid"])
        by_version = {v.version: v for v in lv}
        for r in READINGS["readings"]:
            with self.subTest(date=r["date"], section=r["section"]):
                shown, _ = cli.as_of(lv, r["date"])
                self.assertEqual(shown.version, r["expect_version"])
                path = cli.pick_section(cli.section_files(self.repo, shown), r["section"])
                text = build.git(self.repo, "show", f"{shown.commit}:{path}")
                self.assertIn(r["quote"], text.splitlines())
                blame = build.git(self.repo, "blame", "--line-porcelain", *cli.BLAME_OPTS, shown.commit, "--", path)
                writer = self._writer(blame, r["quote"])
                self.assertEqual(writer, by_version[r["written_by_version"]].commit)
                if "absent_on" in r:
                    before, _ = cli.as_of(lv, r["absent_on"])
                    before_path = cli.pick_section(cli.section_files(self.repo, before), r["section"])
                    self.assertNotIn(r["quote"], build.git(self.repo, "show", f"{before.commit}:{before_path}"))

    @staticmethod
    def _writer(porcelain: str, quote: str) -> str:
        commit = None
        for line in porcelain.splitlines():
            head = line.split(" ")
            if len(head) >= 3 and len(head[0]) == 40 and head[1].isdigit():
                commit = head[0]
            elif line == "\t" + quote:
                return commit
        raise AssertionError(f"quote not found in blame: {quote}")

    def test_label_json_carries_no_status(self):
        meta = json.loads(build.git(self.repo, "show", f"HEAD:levothyroxine/{READINGS['setid']}/label.json"))
        for key in ("status", "last_published", "successor_setids"):
            self.assertNotIn(key, meta)
        self.assertEqual(meta["setid"], READINGS["setid"])

    def _sources_with(self, tmp: Path, versions: list[dict]) -> Path:
        sources = tmp / "sources"
        sources.mkdir(exist_ok=True)
        src = build.load_sources(SOURCE)
        src["versions"] = versions
        build.save_sources(sources / SOURCE.name, src)
        return sources

    def test_append_equals_full_build(self):
        pinned = sorted(build.load_sources(SOURCE)["versions"], key=lambda v: v["published_date"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sources = self._sources_with(tmp, pinned[:-3])
            live = tmp / "live"
            old = build.build(live, sources_dir=sources)
            self.assertEqual(build.append(live, sources_dir=sources), [])      # nothing new: HEAD unchanged
            self.assertEqual(build.git(live, "rev-parse", "main").strip(), old[-1])
            # the newest version first (a late batch can carry any date), then the other two in a later batch
            batch1 = [dict(pinned[-1], added="2026-11-04")]
            sources = self._sources_with(tmp, pinned[:-3] + batch1)
            new1 = build.append(live, sources_dir=sources)
            self.assertEqual(len(new1), 1)
            self.assertEqual(build.git(live, "rev-parse", f"{new1[0]}^").strip(), old[-1])
            batch2 = [dict(v, added="2026-11-05") for v in pinned[-3:-1]]
            sources = self._sources_with(tmp, pinned[:-3] + batch1 + batch2)
            new2 = build.append(live, sources_dir=sources)
            history = build.git(live, "rev-list", "--reverse", "main").split()
            self.assertEqual(history, old + new1 + new2)
            self.assertEqual(build.build(tmp / "full", sources_dir=sources), history)
            self.assertEqual(build.append(live, sources_dir=sources), [])      # idempotent
            # a version without a batch, or a batch older than HEAD's newest, would change history
            other = tmp / "other"
            build.build(other, sources_dir=self._sources_with(tmp, pinned[:-3] + batch2))
            for bad in ([dict(pinned[-1], added="2026-11-04")], [pinned[-1]]):
                sources = self._sources_with(tmp, pinned[:-3] + batch2 + bad)
                with self.assertRaises(build.BuildError) as ctx:
                    build.append(other, sources_dir=sources)
                self.assertIn(build.HISTORY_REFUSAL, str(ctx.exception))
            sources = self._sources_with(tmp, pinned[:-4])
            with self.assertRaises(build.BuildError):                           # a committed version unpinned
                build.append(live, sources_dir=sources)
            # the out-of-order versions are read correctly by the reader
            import labels
            ix = labels.Index(live, tmp / "no-molecules")
            self.assertEqual(ix.out_of_order_setids, {READINGS["setid"]})
            self.assertEqual(ix.record_to, pinned[-1]["published_date"])
            shown = ix.as_of(READINGS["setid"], pinned[-2]["published_date"])[0]
            self.assertEqual(shown.version, pinned[-2]["version"])
            self.assertEqual(ix.label_versions(READINGS["setid"])[-1].version, pinned[-1]["version"])
            day = pinned[-2]["published_date"]
            hit = labels.search(ix, "levothyroxine", day)
            self.assertEqual([m["version"] for m in hit["matches"]], [pinned[-2]["version"]])
            self.assertIn("own version in force", hit["method"])

    def test_as_of_before_record_start_is_an_error(self):
        lv = cli.resolve_label(self.versions, READINGS["setid"][:8])
        with self.assertRaises(cli.CliError):
            cli.as_of(lv, "2006-12-31")

    def test_root_files_written_once(self):
        touched = build.git(self.repo, "log", "--format=%H", "--", "NOTICE.md", "README.md", "corpus.json").split()
        self.assertEqual(touched, [self.hashes[0]])
        root = build.git(self.repo, "ls-tree", "--name-only", "main").split()
        for name in ("NOTICE.md", "README.md", "corpus.json"):
            self.assertIn(name, root)
        self.assertFalse([n for n in root if n.upper().startswith(("LICENSE", "COPYING"))])
        self.assertEqual(build.git(self.repo, "show", "main:NOTICE.md").encode(), build.notices.root_files("us")["NOTICE.md"])
        corpus = json.loads(build.git(self.repo, "show", "main:corpus.json"))
        self.assertEqual(sorted(corpus), ["corpus", "layout", "scope", "sources"])
        self.assertEqual((corpus["corpus"], corpus["scope"]), ("us", build.US_SCOPE))

    def test_text_fidelity_trailer_on_every_commit(self):
        out = build.git(self.repo, "log", "--format=%(trailers:key=Text-Fidelity,valueonly,separator=)%x1e", "main")
        values = [r.strip() for r in out.split("\x1e")][:-1]
        self.assertEqual(len(values), len(self.hashes))
        self.assertEqual(set(values), {build.notices.TRAILER_US.split(": ", 1)[1]})
        self.assertEqual(build.git(self.repo, "log", "-1", "--format=%B", "main").rstrip().splitlines()[-1],
                         build.notices.TRAILER_US)


class RemovedListTest(unittest.TestCase):
    def write(self, tmp: Path, entries: list[dict]) -> Path:
        path = tmp / "removed.json"
        path.write_text(json.dumps({"about": "x", "removed": entries}), encoding="utf-8")
        return path

    def entry(self, **kw) -> dict:
        e = {"corpus": "us", "id": "1e11ad30-1041-4520-10b0-8f9d30d30fcc", "reason": "rights-holder-request",
             "requested_by": "Example Pharma Inc.", "request_date": "2026-11-01", "removed_on": "2026-11-02"}
        e.update(kw)
        return e

    def test_load_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build.load_removed(Path(tmp) / "absent.json"), [])
            path = self.write(Path(tmp), [self.entry(id="1E11AD30-1041-4520-10B0-8F9D30D30FCC".lower()),
                                          self.entry(corpus="eu", id="EMEA-H-C-000582", reason="other")])
            self.assertEqual(build.removed_ids("us", path), {"1e11ad30-1041-4520-10b0-8f9d30d30fcc"})
            self.assertEqual(build.removed_ids("eu", path), {"EMEA-H-C-000582"})

    def test_rejects_malformed_entries(self):
        bad = [self.entry(reason="asked nicely"), self.entry(corpus="uk"), self.entry(id="EMEA-H-C-1"),
               self.entry(corpus="eu", id="1e11ad30-1041-4520-10b0-8f9d30d30fcc"), self.entry(request_date="Nov 1"),
               {k: v for k, v in self.entry().items() if k != "requested_by"}]
        with tempfile.TemporaryDirectory() as tmp:
            for e in bad:
                with self.subTest(entry=e):
                    with self.assertRaises(build.BuildError):
                        build.load_removed(self.write(Path(tmp), [e]))
            (Path(tmp) / "removed.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(build.BuildError):
                build.load_removed(Path(tmp) / "removed.json")

    def test_committed_list_is_valid(self):
        build.load_removed()


def fixture_raw_available() -> bool:
    fixture = json.loads((Path(__file__).parent / "mcp_fixture.json").read_text(encoding="utf-8"))
    for setid in fixture["setids"]:
        src = build.SOURCES / f"{setid}.json"
        if not src.exists() or not all(build.raw_path(v["sha256"]).exists() for v in build.load_sources(src)["versions"]):
            return False
    return True


@unittest.skipUnless(fixture_raw_available(), "pinned raw zips of the MCP fixture labels not present")
class RemovedTest(unittest.TestCase):
    """A label removed on request: the build leaves it out, earlier commits keep their hashes, and an append on
    a repo that still holds it refuses."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).parent))
        import test_mcp
        cls.fx = test_mcp.fixture()
        cls.full = cls.fx["hashes"]
        log = build.head_versions(cls.fx["repo"])
        firsts = {}
        for i, (setid, _, _) in enumerate(log):
            firsts.setdefault(setid, i)
        cls.setid, cls.first = max(firsts.items(), key=lambda kv: kv[1])   # the label whose first commit is latest
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name) / "repo"
        cls.hashes = build.build(cls.repo, sources_dir=cls.fx["sources"], removed={cls.setid})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_no_commit_for_the_removed_label(self):
        self.assertGreater(self.first, 0)
        setids = {s for s, _, _ in build.head_versions(self.repo)}
        self.assertNotIn(self.setid, setids)
        self.assertEqual(build.git(self.repo, "log", "--all", "--format=%H", "--", f"*/{self.setid}/").strip(), "")
        n = sum(1 for s, _, _ in build.head_versions(self.fx["repo"]) if s == self.setid)
        self.assertEqual(len(self.hashes), len(self.full) - n)

    def test_commits_before_its_first_version_keep_their_hashes(self):
        self.assertEqual(self.hashes[:self.first], self.full[:self.first])
        if len(self.hashes) > self.first:
            self.assertNotEqual(self.hashes[self.first], self.full[self.first])

    def test_append_refuses_while_the_removed_label_is_in_head(self):
        with self.assertRaises(build.BuildError) as ctx:
            build.plan_append(self.fx["repo"], self.fx["sources"], removed={self.setid})
        self.assertIn(build.HISTORY_REFUSAL, str(ctx.exception))
        self.assertIn("purge", str(ctx.exception))
        self.assertEqual(build.plan_append(self.repo, self.fx["sources"], removed={self.setid})[0], [])


NOTICES = json.loads((Path(__file__).parent / "known_notices.json").read_text(encoding="utf-8"))


@unittest.skipUnless(os.environ.get("ASOF_FULL_REPO") and os.environ.get("ASOF_FULL_EU_REPO"),
                     "set ASOF_FULL_REPO and ASOF_FULL_EU_REPO to the built corpora")
class FullCorpusNoticesTest(unittest.TestCase):
    def test_known_copyright_notices_are_in_the_corpora(self):
        repos = {"us": Path(os.environ["ASOF_FULL_REPO"]), "eu": Path(os.environ["ASOF_FULL_EU_REPO"])}
        for n in NOTICES["notices"]:
            with self.subTest(path=n["path"]):
                self.assertIn(n["text"], build.git(repos[n["corpus"]], "show", f"main:{n['path']}"))

    def test_corpus_root_files(self):
        for corpus, env in (("us", "ASOF_FULL_REPO"), ("eu", "ASOF_FULL_EU_REPO")):
            repo = Path(os.environ[env])
            with self.subTest(corpus=corpus):
                root = build.git(repo, "ls-tree", "--name-only", "main").split()
                self.assertTrue({"README.md", "NOTICE.md", "corpus.json"} <= set(root))
                self.assertFalse([n for n in root if n.upper().startswith(("LICENSE", "COPYING"))])
                first = build.git(repo, "rev-list", "--max-parents=0", "main").strip()
                touched = build.git(repo, "log", "--format=%H", "--", "NOTICE.md", "README.md", "corpus.json").split()
                self.assertEqual(touched, [first])
                for name, data in build.notices.root_files(corpus).items():
                    self.assertEqual(build.git(repo, "show", f"main:{name}").encode(), data)
                trailer = build.git(repo, "log", "-1", "--format=%(trailers:key=Text-Fidelity,valueonly)", "main")
                self.assertIn("intended to be unchanged", trailer)


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


def spl_zip(setid, version):
    xml = (f'<document xmlns="urn:hl7-org:v3"><setId root="{setid}"/><versionNumber value="{version}"/>'
           '<effectiveTime value="20200101"/><component><structuredBody/></component></document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("label.xml", xml)
    return buf.getvalue()


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(b""))


class FetchTest(unittest.TestCase):
    SETID = "aaaaaaaa-0000-0000-0000-000000000001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.requests = []
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def opener(self, served, title="TEST LABEL [TEST INC.]"):
        def open_(req, timeout):
            self.requests.append((req.full_url, req.get_header("User-agent")))
            if "history.json" in req.full_url:
                hist = {"metadata": {"total_pages": 1},
                        "data": {"spl": {"title": title, "setid": self.SETID},
                                 "history": [{"spl_version": 2, "published_date": "Feb 01, 2020"},
                                             {"spl_version": 1, "published_date": "Jan 01, 2020"}]}}
                return FakeResponse(json.dumps(hist).encode())
            return served(req.full_url, int(req.full_url.rsplit("=", 1)[1]))
        return open_

    def fetch(self, served, abort_after=3, **kw):
        fetcher = build.Fetcher(opener=self.opener(served, **kw), min_gap_s=0, backoff=(1, 2), abort_after=abort_after,
                                sleep=self.sleeps.append)
        return fetcher, build.fetch_setid(self.SETID, "testinn", fetcher, self.dir / "sources", self.dir / "raw")

    def sources(self):
        return build.load_sources(self.dir / "sources" / f"{self.SETID}.json")

    def test_fetches_missing_versions_then_resumes_without_any_request(self):
        _, problems = self.fetch(lambda url, v: FakeResponse(spl_zip(self.SETID, v)))
        self.assertEqual(problems, [])
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(ua == build.USER_AGENT for _, ua in self.requests))
        self.assertEqual([v["version"] for v in self.sources()["versions"]], [1, 2])
        self.requests.clear()
        self.fetch(lambda url, v: FakeResponse(spl_zip(self.SETID, v)))
        self.assertEqual(self.requests, [])

    def test_rejects_a_zip_that_holds_another_version(self):
        _, problems = self.fetch(lambda url, v: FakeResponse(spl_zip(self.SETID, 99)))
        self.assertEqual(len(problems), 2)
        self.assertIn("got", problems[0])
        self.assertEqual(sorted(self.sources()["problems"]), ["1", "2"])

    def test_404_is_logged_without_retry(self):
        def served(url, v):
            if v == 1:
                raise http_error(url, 404)
            return FakeResponse(spl_zip(self.SETID, v))
        _, problems = self.fetch(served)
        self.assertEqual(len(problems), 1)
        self.assertIn("HTTP 404", self.sources()["problems"]["1"])
        self.assertEqual(self.sleeps, [])

    def test_transient_errors_retry_with_backoff_then_succeed(self):
        calls = {"n": 0}
        def served(url, v):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise http_error(url, 503)
            return FakeResponse(spl_zip(self.SETID, v))
        _, problems = self.fetch(served)
        self.assertEqual(problems, [])
        self.assertEqual(self.sleeps, [1, 2])

    def test_repeated_transient_failures_abort_the_run(self):
        with self.assertRaises(build.FetchAborted):
            self.fetch(lambda url, v: (_ for _ in ()).throw(http_error(url, 503)), abort_after=2)
        self.assertEqual(self.sources()["versions"], [])

    def test_rate_limit_holds_across_threads(self):
        import threading, time
        starts, lock = [], threading.Lock()
        def open_(req, timeout):
            with lock:
                starts.append(time.monotonic())
            time.sleep(0.03)  # slow server: without a shared limiter, threads would overlap
            return FakeResponse(b"ok")
        fetcher = build.Fetcher(opener=open_, min_gap_s=0.05)
        threads = [threading.Thread(target=lambda: [fetcher.get("https://example.invalid/x") for _ in range(5)])
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 20)
        # Start slots are reserved 0.05 s apart; a busy machine can wake one thread a few ms late,
        # which shortens a single gap but never the overall rate. Without a shared limiter the
        # four threads would overlap: gaps near 0 and a span near 5 x 0.03 s.
        self.assertGreaterEqual(starts[-1] - starts[0], 19 * 0.05 * 0.98)
        self.assertGreaterEqual(min(gaps), 0.025)

    def test_retired_setid_is_fetched_through_the_archive(self):
        # /history lists v1 and v2; the archive has a file for v2 (archive_id 202) and v3 (303) only.
        listing = archive_page([("Feb 01, 2020", 202, 2), ("Mar 01, 2020", 303, 3)])
        def served(url, n):
            if "archives/index.cfm" in url:
                return FakeResponse(listing.encode())
            return FakeResponse(spl_zip(self.SETID, {202: 2, 303: 3}[n]))
        _, problems = self.fetch(served, title=None)
        src = self.sources()
        self.assertEqual(src["status"], "retired")
        self.assertEqual([v["version"] for v in src["versions"]], [2, 3])
        self.assertEqual(src["versions"][1]["published_date"], "2020-03-01")   # from the archive row
        self.assertTrue(src["versions"][0]["url"].endswith("getArchivalFile.cfm?archive_id=202"))
        self.assertEqual(list(src["problems"]), ["1"])
        self.assertIn(build.NO_ARCHIVED_FILE, problems[0])
        self.assertEqual(build.label_status(src),
                         {"status": "retired", "last_published": "2020-03-01", "successor_setids": []})
        self.requests.clear()
        self.fetch(served, title=None)                     # resume: only /history is asked again
        self.assertEqual(len(self.requests), 1)

    def test_label_status_of_a_current_setid(self):
        self.assertEqual(build.label_status({"status": "current", "versions": []}), {"status": "current"})


def archive_page(rows):
    """A minimal archive search page with the markup tools/archive.py parses."""
    items = "".join(
        f'<li><div class="desktop"><div class="date">{d}<a download href="/dailymed/getArchivalFile.cfm?'
        f'archive_id={a}" class="download">download</a></div></div><div class="desc">'
        f'<a href="/dailymed/archives/fdaDrugInfo.cfm?archiveid={a}">TEST (test) tablet<br></a>'
        f'<div class="packager">Packager: <span>Test Inc.</span></div>'
        f'<div class="version">Version: <span>{v}</span></div></div></li>' for d, a, v in rows)
    return f'<h2>Search results <span>for: <span class="title">x ({len(rows)} results)</span></span></h2><ul>{items}</ul>'


if __name__ == "__main__":
    unittest.main()
