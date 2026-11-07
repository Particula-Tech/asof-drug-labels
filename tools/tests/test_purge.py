"""Tools/purge.py: recording a removal on request, and checking that a corpus repo no longer holds it.

add runs on a temporary copy of a few sources and molecule files. check runs on the MCP fixture repo (built
from pinned raw zips; skipped without them) and on the same labels built without the removed setid.
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import purge  # noqa: E402

ROOT = TOOLS.parent
SYN = "1e11ad30-1041-4520-10b0-8f9d30d30fcc"
AVASTIN_EU = "EMEA-H-C-000582"


def run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = purge.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class AddTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for sub in ("sources/us", "sources/eu", "molecules"):
            (self.root / sub).mkdir(parents=True)
        shutil.copyfile(ROOT / "sources" / "us" / f"{SYN}.json", self.root / "sources" / "us" / f"{SYN}.json")
        shutil.copyfile(ROOT / "sources" / "eu" / f"{AVASTIN_EU}.json",
                        self.root / "sources" / "eu" / f"{AVASTIN_EU}.json")
        for inn in ("levothyroxine", "bevacizumab"):
            shutil.copyfile(ROOT / "molecules" / f"{inn}.json", self.root / "molecules" / f"{inn}.json")
        shutil.copyfile(ROOT / "sources" / "removed.json", self.root / "sources" / "removed.json")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, corpus, ident, *extra):
        return run("--root", str(self.root), "add", "--corpus", corpus, "--id", ident, "--reason",
                   "rights-holder-request", "--requested-by", "Example Pharma Inc.", "--request-date", "2026-11-01",
                   "--date", "2026-11-02", *extra)

    def test_add_writes_the_list_and_flags_the_molecules(self):
        code, out, err = self.add("us", SYN.upper(), "--note", "test entry")
        self.assertEqual(code, 0, err)
        self.assertIn("purge.py rebuild --corpus us", out)
        code, out, err = self.add("eu", "EMEA/H/C/582")
        self.assertEqual(code, 0, err)
        doc = json.loads((self.root / "sources" / "removed.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["about"], purge.ABOUT)
        self.assertEqual([(e["corpus"], e["id"]) for e in doc["removed"]], [("eu", AVASTIN_EU), ("us", SYN)])
        us = next(e for e in doc["removed"] if e["corpus"] == "us")
        self.assertEqual(us, {"corpus": "us", "id": SYN, "reason": "rights-holder-request",
                              "requested_by": "Example Pharma Inc.", "request_date": "2026-11-01",
                              "removed_on": "2026-11-02", "note": "test entry"})
        self.assertEqual(build.removed_ids("us", self.root / "sources" / "removed.json"), {SYN})
        levo = json.loads((self.root / "molecules" / "levothyroxine.json").read_text(encoding="utf-8"))
        self.assertEqual([e["status"] for e in levo["us"] if e["setid"] == SYN], ["removed"])
        self.assertTrue(all(e.get("status") != "removed" for e in levo["us"] if e["setid"] != SYN))
        beva = json.loads((self.root / "molecules" / "bevacizumab.json").read_text(encoding="utf-8"))
        self.assertTrue(next(e for e in beva["eu"] if e["label_id"] == AVASTIN_EU)["removed"])
        self.assertFalse(any(e.get("removed") for e in beva["eu"] if e["label_id"] != AVASTIN_EU))

    def test_refuses_a_duplicate_and_an_unknown_id(self):
        self.assertEqual(self.add("us", SYN)[0], 0)
        code, _, err = self.add("us", SYN)
        self.assertEqual(code, 2)
        self.assertIn("already", err)
        code, _, err = self.add("us", "00000000-0000-0000-0000-000000000000")
        self.assertEqual(code, 2)
        self.assertIn("no pinned sources file", err)
        code, _, err = self.add("eu", "Avastin")
        self.assertEqual(code, 2)
        self.assertIn("not a EU id", err)
        before = (self.root / "sources" / "removed.json").read_text(encoding="utf-8")
        self.assertEqual(len(json.loads(before)["removed"]), 1)

    def test_bad_date_is_refused(self):
        code, _, err = run("--root", str(self.root), "add", "--corpus", "us", "--id", SYN, "--reason", "other",
                           "--requested-by", "named individual", "--request-date", "1 Nov 2026")
        self.assertEqual(code, 2)
        self.assertIn("YYYY-MM-DD", err)


@unittest.skipUnless(__import__("test_build").fixture_raw_available(), "pinned raw zips of the MCP fixture labels "
                                                                        "not present")
class CheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_mcp
        cls.fx = test_mcp.fixture()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "sources").mkdir()
        (cls.root / "sources" / "removed.json").write_text(json.dumps({"about": "x", "removed": [
            {"corpus": "us", "id": SYN, "reason": "rights-holder-request", "requested_by": "Example Pharma Inc.",
             "request_date": "2026-11-01", "removed_on": "2026-11-02"}]}), encoding="utf-8")
        cls.without = cls.root / "without"
        build.build(cls.without, sources_dir=cls.fx["sources"], removed={SYN})

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_passes_on_a_repo_built_without_the_id(self):
        self.assertEqual(purge.check(self.root, self.without, None), [])
        code, out, _ = run("--root", str(self.root), "check", "--us-repo", str(self.without), "--eu-repo",
                           str(self.root / "absent"))
        self.assertEqual(code, 0)
        self.assertIn("1 removed id(s) checked; 0 daily/* tag(s); ok", out)

    def test_fails_on_a_repo_that_still_holds_it(self):
        failures = purge.check(self.root, self.fx["repo"], None)
        self.assertTrue(any("still in the history" in f for f in failures))
        self.assertTrue(any("tree at main" in f for f in failures))
        self.assertTrue(any("Source-Id dailymed:" + SYN in f for f in failures))
        code, _, err = run("--root", str(self.root), "check", "--us-repo", str(self.fx["repo"]), "--eu-repo", "")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", err)

    def test_a_daily_tag_left_from_the_old_history_fails(self):
        repo = self.root / "tagged"
        build.build(repo, sources_dir=self.fx["sources"], removed={SYN})
        old = build.git(repo, "rev-parse", "main~1").strip()
        build.git(repo, "tag", "daily/2026-11-01", "main")
        self.assertEqual(purge.check(self.root, repo, None), [])           # reachable from main
        build.git(repo, "checkout", "-q", "--orphan", "other")
        build.git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated", env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@labels.invalid", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@labels.invalid"})
        build.git(repo, "tag", "daily/2026-11-02", "other")
        self.assertNotEqual(old, "")
        failures = purge.check(self.root, repo, None)
        self.assertEqual(len(failures), 1)
        self.assertIn("daily/2026-11-02 is not reachable from main", failures[0])


class RemotesTest(unittest.TestCase):
    def test_remotes_are_saved_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r"
            repo.mkdir()
            build.git(repo, "init", "-q", "-b", "main")
            build.git(repo, "remote", "add", "origin", "https://example.invalid/corpus.git")
            saved = purge.remotes(repo)
            self.assertEqual(saved, [("origin", "https://example.invalid/corpus.git")])
            shutil.rmtree(repo / ".git")
            build.git(repo, "init", "-q", "-b", "main")
            purge.restore_remotes(repo, saved)
            self.assertEqual(purge.remotes(repo), saved)
            self.assertEqual(build.git(repo, "config", "branch.main.remote").strip(), "origin")


if __name__ == "__main__":
    unittest.main()
