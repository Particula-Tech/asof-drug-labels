"""Manifest.json: fields, build_date carry-over, --check, and the reader taking build_date from it.

The fixture test builds the US fixture repo of test_mcp (needs raw/sha256/, skipped otherwise). The
real-manifest check runs only when ASOF_FULL_REPO and ASOF_FULL_EU_REPO point at the built repos.
No network.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import labels  # noqa: E402
import manifest  # noqa: E402
import test_mcp  # noqa: E402


class ListingCountsTest(unittest.TestCase):
    def test_counts_follow_the_coverage_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.json").write_text(json.dumps({
                "listed_versions": [{"version": n} for n in (1, 2, 3, 4)],
                "versions": [{"version": 1}, {"version": 2}, {"version": 4}],
                "problems": {"3": "HTTP 404", "history": "x", "pin:4": "y"}}))
            (d / "b.json").write_text(json.dumps({"listed_versions": [{"version": 1}], "versions": [{"version": 1}]}))
            got = manifest.us_listing_counts(["a", "b"], d)
            self.assertEqual(got, {"listed_versions": 5, "logged_versions": 1, "fetched_pct": 80.0})
            with self.assertRaises(manifest.ManifestError):
                manifest.us_listing_counts(["missing"], d)


class MergeTest(unittest.TestCase):
    FRESH = {"project": manifest.PROJECT, "server_version": "9.9", "molecules": 3,
             "corpora": {"us": {"head": "a" * 40, "commits": 2}, "eu": {"head": "b" * 40, "commits": 1}}}

    def test_build_date_kept_while_head_is_unchanged(self):
        first = manifest.merge(None, self.FRESH, "2026-11-03", "Nov 03, 2026", generated="2026-11-03T06:00:00Z")
        self.assertEqual(first["corpora"]["us"]["build_date"], "2026-11-03")
        self.assertEqual(first["corpora"]["us"]["dailymed_db_published"], "Nov 03, 2026")
        self.assertNotIn("dailymed_db_published", first["corpora"]["eu"])
        moved = json.loads(json.dumps(self.FRESH))
        moved["corpora"]["us"]["head"] = "c" * 40
        second = manifest.merge(first, moved, "2026-11-04")
        self.assertEqual(second["corpora"]["us"]["build_date"], "2026-11-04")      # HEAD moved
        self.assertEqual(second["corpora"]["eu"]["build_date"], "2026-11-03")      # unchanged
        self.assertEqual(second["corpora"]["us"]["dailymed_db_published"], "Nov 03, 2026")

    def test_check_reports_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            self.assertIn("missing", manifest.check(path, self.FRESH)[0])
            manifest.write_manifest(path, manifest.merge(None, self.FRESH, "2026-11-03"))
            self.assertEqual(manifest.check(path, self.FRESH), [])
            moved = json.loads(json.dumps(self.FRESH))
            moved["corpora"]["eu"]["commits"] = 5
            self.assertEqual(manifest.check(path, moved), ["corpora.eu.commits: manifest 1, repo 5"])


@test_mcp.skip_without_raw
class FixtureManifestTest(unittest.TestCase):
    def test_write_then_check_and_reader_build_date(self):
        repo = test_mcp.fixture()["repo"]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "manifest.json"
            sources = test_mcp.fixture()["sources"]
            argv = ["--us-repo", str(repo), "--eu-repo", "", "--molecules", str(test_mcp.MOLECULES), "--out", str(out),
                    "--sources", str(sources)]
            self.assertEqual(manifest.main(["write", "--build-date", "2031-02-03", "--dailymed-db-published",
                                            "Feb 03, 2031 08:40:33PM EST", *argv]), 0)
            data = json.loads(out.read_text())
            us = data["corpora"]["us"]
            ix = labels.Index(repo, test_mcp.MOLECULES)
            self.assertEqual((us["head"], us["commits"], us["labels"], us["record_to"], us["build_date"]),
                             (ix.head, len(ix.versions), len(ix.by_setid), ix.record_to, "2031-02-03"))
            self.assertIn("Courtesy of the U.S. National Library of Medicine", us["licence"])
            self.assertNotIn("to be confirmed", manifest.EU_TERMS["licence"])
            self.assertIn("not licensed by this project", manifest.EU_TERMS["licence"])
            self.assertEqual(us["removed_labels"], 0)
            self.assertIn("intended to be unchanged", us["changes"])
            self.assertIn("the official text prevails", us["changes"])
            self.assertIn("the official text prevails", manifest.EU_TERMS["changes"])
            self.assertEqual(us["contact"], "legal@particula.tech")
            self.assertEqual(manifest.EU_TERMS["contact"], "legal@particula.tech")
            self.assertEqual(us["notice_file"], "NOTICE.md at the corpus root, written in the first commit")
            self.assertEqual(data["contact"], "legal@particula.tech")
            for key in ("rights_and_corrections", "independence", "warranty"):
                self.assertIn(key, data)
            self.assertNotIn("particula.tech/book", json.dumps(data))
            self.assertIn("not the FDA approval date", us["date_rule"])
            self.assertTrue(us["converter"].startswith("spl "))
            self.assertNotIn("molecules", us)
            self.assertEqual(us["molecule_folders"], len({ix.folder(s).split("/")[0] for s in ix.by_setid}))
            # every listed version is either committed or logged, so listed - logged is the commit count
            self.assertEqual(us["listed_versions"] - us["logged_versions"], us["commits"])
            self.assertEqual(us["fetched_pct"], round(100 * us["commits"] / us["listed_versions"], 2))
            self.assertEqual(manifest.main(["--check", *argv]), 0)
            self.assertEqual(manifest.main(["write", *argv]), 0)                 # same HEAD: date kept
            self.assertEqual(json.loads(out.read_text())["corpora"]["us"]["build_date"], "2031-02-03")
            saved = labels.MANIFEST
            try:
                labels.MANIFEST = out
                fresh = labels.Index(repo, test_mcp.MOLECULES)
                self.assertEqual(fresh.build_date, "2031-02-03")
                cov = labels.coverage(fresh)
                self.assertEqual(cov["corpora"][0]["build_date"], "2031-02-03")
                self.assertIn("2031-02-03", cov["notice"])
                self.assertEqual(cov["corpora"][0]["dailymed_db_published"], "Feb 03, 2031 08:40:33PM EST")
                self.assertEqual(cov["corpora"][0]["tags"], {"at_head": [], "latest_daily": None})
            finally:
                labels.MANIFEST = saved
            data["corpora"]["us"]["commits"] += 1
            out.write_text(json.dumps(data))
            self.assertEqual(manifest.main(["--check", *argv]), 1)


@unittest.skipUnless(os.environ.get("ASOF_FULL_REPO") and os.environ.get("ASOF_FULL_EU_REPO"),
                     "set ASOF_FULL_REPO and ASOF_FULL_EU_REPO to check the real manifest.json")
class FullManifestTest(unittest.TestCase):
    def test_manifest_matches_the_repos(self):
        if not manifest.MANIFEST.exists():
            self.skipTest("manifest.json not written yet")
        argv = ["--check", "--us-repo", os.environ["ASOF_FULL_REPO"], "--eu-repo", os.environ["ASOF_FULL_EU_REPO"]]
        self.assertEqual(manifest.main(argv), 0)

    def test_listing_counts_equal_the_coverage_report(self):
        ix = labels.Index(Path(os.environ["ASOF_FULL_REPO"]), manifest.MOLECULES)
        totals = json.loads((manifest.ROOT / "reports" / "coverage-us.json").read_text(encoding="utf-8"))["totals"]
        got = manifest.us_listing_counts(ix.by_setid)
        if got["listed_versions"] != totals["listed"]:
            self.skipTest("sources/us lists versions that reports/coverage-us.json predates (a daily run since)")
        self.assertEqual((got["listed_versions"], got["logged_versions"], got["fetched_pct"]),
                         (totals["listed"], totals["logged"], totals["fetched_pct"]))
        self.assertEqual(got["listed_versions"] - got["logged_versions"], len(ix.versions))


if __name__ == "__main__":
    unittest.main()
