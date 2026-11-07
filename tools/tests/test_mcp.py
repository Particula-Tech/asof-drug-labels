"""The shared library, the MCP server and its stdio transport.

The fixture tests build a small repo in a temp dir from the pinned sources of
the labels listed in mcp_fixture.json (needs raw/sha256/; skipped otherwise).
No test may touch the network: socket connections fail the test. The
full-corpus class runs only when ASOF_FULL_REPO points at a built repo.
"""
import atexit
import dataclasses
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import build  # noqa: E402
import cli  # noqa: E402
import labels  # noqa: E402
import mcp_server  # noqa: E402
import notices  # noqa: E402

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "mcp_fixture.json").read_text(encoding="utf-8"))
READINGS = json.loads((HERE / "known_readings.json").read_text(encoding="utf-8"))
SYN = READINGS["setid"]
Q2 = FIXTURE["query2"]
PHRASE = Q2["query"]
RENAMED = FIXTURE["renamed_section"]
MOLECULES = TOOLS.parent / "molecules"


def _refuse(*args, **kwargs):
    raise AssertionError("network access attempted during an MCP test")


def setUpModule():
    global _socket_backup
    _socket_backup = (socket.socket.connect, socket.create_connection, socket.getaddrinfo)
    socket.socket.connect = _refuse
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse


def tearDownModule():
    socket.socket.connect, socket.create_connection, socket.getaddrinfo = _socket_backup


def raw_available() -> bool:
    for setid in FIXTURE["setids"]:
        source = build.SOURCES / f"{setid}.json"
        if not source.exists():
            return False
        if not all(build.raw_path(v["sha256"]).exists() for v in build.load_sources(source)["versions"]):
            return False
    return True


_fixture: dict | None = None


def fixture() -> dict:
    """Build the fixture repo once per test process."""
    global _fixture
    if _fixture is None:
        tmp = tempfile.TemporaryDirectory()
        atexit.register(tmp.cleanup)
        sources = Path(tmp.name) / "sources"
        sources.mkdir()
        for setid in FIXTURE["setids"]:
            src = build.SOURCES / f"{setid}.json"
            (sources / src.name).write_bytes(src.read_bytes())
        repo = Path(tmp.name) / "repo"
        hashes = build.build(repo, sources_dir=sources)
        _fixture = {"repo": repo, "hashes": hashes, "index": labels.Index(repo, MOLECULES), "sources": sources}
    return _fixture


def rows(items: list[dict]) -> list[tuple]:
    return sorted((r["label_id"], r["version"], r["published_date"]) for r in items)


skip_without_raw = unittest.skipUnless(raw_available(), "pinned raw zips not present; run tools/build.py fetch first")


@skip_without_raw
class LibraryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f = fixture()
        cls.repo, cls.ix = f["repo"], f["index"]

    def test_index_is_linear_and_in_published_order(self):
        self.assertEqual(len(self.ix.versions), len(fixture()["hashes"]))
        self.assertEqual(self.ix.published, sorted(self.ix.published))
        self.assertEqual(set(self.ix.by_setid), set(FIXTURE["setids"]))
        self.assertEqual(self.ix.git("rev-list", "--min-parents=2", "--count", "HEAD").strip(), "0")
        self.assertEqual(set(self.ix.folders), set(FIXTURE["setids"]))

    def test_envelope_carries_notice_and_record_window(self):
        env = self.ix.envelope()
        self.assertIn("Courtesy of the U.S. National Library of Medicine", env["notice"])
        self.assertIn(env["build_date"], env["notice"])
        self.assertIn(f"Record ends {env['record_to']} (built {env['build_date']})", env["notice"])
        self.assertIn("later versions may exist on DailyMed", env["notice"])
        self.assertEqual(env["record_from"], self.ix.versions[0].published)
        self.assertEqual(env["record_to"], self.ix.versions[-1].published)

    def test_find_label_by_every_key(self):
        meta = self.ix.metadata(SYN)
        cases = {"setid prefix": SYN[:8], "dailymed id": f"dailymed:{SYN}", "brand": "synthroid",
                 "inn": "levothyroxine", "ndc": meta["package_ndcs"][0], "ndc no dashes": meta["package_ndcs"][0].replace("-", "")}
        for how, query in cases.items():
            with self.subTest(how=how):
                ids = [m["id"] for m in labels.find_label(self.ix, query)["matches"]]
                self.assertIn(SYN, ids)
        entry = next(e for inn, e in self.ix.molecule_entries("34496b43-05a2-45fb-a769-52b12e099341") if e.get("rxcui"))
        found = labels.find_label(self.ix, str(entry["rxcui"][0]))
        self.assertIn("34496b43-05a2-45fb-a769-52b12e099341", [m["id"] for m in found["matches"]])
        self.assertEqual(found["matches"][0]["matched_on"], ["rxcui"])
        brand = labels.find_label(self.ix, "Plaquenil")["matches"]
        self.assertEqual([m["id"] for m in brand], ["34496b43-05a2-45fb-a769-52b12e099341"])
        self.assertEqual(brand[0]["versions"], len(self.ix.label_versions(brand[0]["id"])))
        self.assertEqual(brand[0]["status"], "current")

    def test_find_label_eu_is_honest(self):
        for query, jurisdiction in (("EU/1/15/1024", "us"), ("Synthroid", "eu")):
            with self.subTest(query=query, jurisdiction=jurisdiction):
                out = labels.find_label(self.ix, query, jurisdiction)
                self.assertFalse(out["eu_available"])
                self.assertEqual(out["eu_reason"], labels.EU_REASON)
                self.assertEqual((out["matches"], out["count"]), ([], 0))
                self.assertIn("hint", out)
        eu_changes = labels.changes_between(self.ix, Q2["from"], Q2["to"], jurisdiction="eu", query=PHRASE)
        self.assertEqual((eu_changes["eu_available"], eu_changes["changes"]), (False, []))
        eu_search = labels.search(self.ix, PHRASE, "2024-08-01", jurisdiction="eu")
        self.assertEqual((eu_search["eu_available"], eu_search["matches"]), (False, []))
        both = labels.search(self.ix, PHRASE, "2024-08-01", jurisdiction="all", section="43685-7")
        self.assertFalse(both["eu_available"])
        self.assertGreater(both["count"], 0)

    def test_read_label_matches_known_readings(self):
        for r in READINGS["readings"]:
            with self.subTest(date=r["date"], section=r["section"]):
                out = labels.read_label(self.ix, SYN, r["date"], r["section"])
                self.assertEqual(out["version"], r["expect_version"])
                self.assertIn(r["quote"], out["text"].splitlines())
                self.assertEqual(out["section"]["code"], r["section"])
                for key in ("label_id", "title", "published_date", "effective_time", "effective_window", "source_url"):
                    self.assertIn(key, out)
                self.assertTrue(out["wording"].startswith("DailyMed showed"))
                self.assertNotIn("approved", json.dumps(out).lower())
                if "absent_on" in r:
                    before = labels.read_label(self.ix, SYN, r["absent_on"], r["section"])
                    self.assertNotIn(r["quote"], before["text"])

    def test_read_label_whole_label_has_index_and_cap(self):
        out = labels.read_label(self.ix, SYN, "2023-01-01")
        codes = [s["code"] for s in out["sections"]]
        self.assertIn("43685-7", codes)
        self.assertIn("34066-1", codes)
        self.assertTrue(all(s["lines"] > 0 for s in out["sections"]))
        capped = labels.read_label(self.ix, SYN, "2023-01-01", cap=5000)
        self.assertTrue(capped["truncated"])
        self.assertIn("one section at a time", capped["note"])

    def test_read_label_before_record_start_names_the_start(self):
        start = self.ix.label_versions(SYN)[0].published
        with self.assertRaises(labels.LabelError) as ctx:
            labels.read_label(self.ix, SYN, "2006-12-31")
        self.assertIn(start, str(ctx.exception))
        with self.assertRaises(labels.LabelError):
            labels.read_label(self.ix, SYN, "2023-13-01")

    def test_section_resolution_by_code_word_and_error(self):
        v, _ = self.ix.as_of(SYN, "2023-01-01")
        files = self.ix.section_files(v)
        self.assertTrue(labels.pick_section(files, "warnings").endswith("43685-7-warnings-and-precautions.md"))
        self.assertTrue(labels.pick_section(files, "boxed").endswith("34066-1-boxed-warning.md"))
        self.assertEqual(labels.pick_section(files, "43685-7"), labels.pick_section(files, "warnings and precautions"))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_section(files, "nonexistent")
        self.assertIn("43685-7-warnings-and-precautions", str(ctx.exception))

    def test_section_whose_file_name_changed_resolves_by_code(self):
        setid, code = RENAMED["setid"], RENAMED["section"]
        files = self.ix.all_section_files(setid)
        group = labels.pick_section_group(files, code)
        self.assertEqual([Path(f).name for f in group], sorted([RENAMED["old_file"], RENAMED["new_file"]]))
        with self.assertRaises(labels.LabelError) as ctx:
            labels.pick_section(files, code)
        self.assertIn("several file names", str(ctx.exception))
        history = labels.section_history(self.ix, setid[:8], code)
        renamed = [c for c in history["changes"] if c["status"] == "renamed"]
        self.assertEqual([(c["version"], c["file"], c["renamed_from"]) for c in renamed],
                         [(RENAMED["renamed_in_version"], RENAMED["new_file"], RENAMED["old_file"])])
        self.assertEqual(history["changes"][0]["status"], "created")
        self.assertEqual(history["section"]["file"], RENAMED["new_file"])
        self.assertEqual(sorted(history["file_names"]), sorted([RENAMED["old_file"], RENAMED["new_file"]]))
        self.assertLess(renamed[0]["lines_added"] + renamed[0]["lines_removed"], 10)  # the real change, not D + A
        one = labels.diff(self.ix, setid, RENAMED["date_before"], RENAMED["date_after"], code)
        self.assertEqual([(s["file"], s["renamed_from"]) for s in one["sections"]], [(RENAMED["new_file"], RENAMED["old_file"])])
        self.assertGreater(one["sections"][0]["words_added"], 0)
        self.assertIn("[-", one["sections"][0]["word_diff"])
        whole = labels.diff(self.ix, setid, RENAMED["date_before"], RENAMED["date_after"])
        self.assertEqual([s["file"] for s in whole["sections"] if s["code"] == code], [RENAMED["new_file"]])
        day = RENAMED["date_after"]
        for query, kind in (("controlled room temperature", "added"), ("spraying in or around", "removed"),
                            ("tightly closed", "reworded")):
            with self.subTest(query=query):
                out = labels.changes_between(self.ix, day, day, section=code, query=query)
                mine = [c for c in out["changes"] if c["label_id"] == setid]
                self.assertEqual([(c["change"], c["renamed"], c["renamed_from"]) for c in mine],
                                 [(kind, True, RENAMED["old_file"])])
        unchanged = labels.changes_between(self.ix, day, day, section=code, query="avoid freezing")
        self.assertEqual([c for c in unchanged["changes"] if c["label_id"] == setid], [])
        everything = labels.changes_between(self.ix, day, day, section=code)
        row = next(c for c in everything["changes"] if c["label_id"] == setid)
        self.assertEqual([(s["status"], s["renamed_from"]) for s in row["sections"]], [("renamed", RENAMED["old_file"])])
        out = io.StringIO()
        sys.stdout, saved = out, sys.stdout
        try:
            code_ = cli.main(["--repo", str(self.repo), "diff", RENAMED["date_before"], RENAMED["date_after"], setid[:8],
                              "--section", code])
        finally:
            sys.stdout = saved
        self.assertEqual(code_, 0)
        self.assertIn("{+", out.getvalue())
        self.assertNotIn("No changes", out.getvalue())

    def test_same_day_superseded_version_is_flagged(self):
        ix = labels.Index(self.repo, MOLECULES)
        lv = ix.by_setid[SYN]
        older, newer = lv[5], lv[6]
        twin = dataclasses.replace(older, published=newer.published)
        lv[5] = twin
        head = ix.header(twin, newer.published)
        self.assertTrue(head["superseded_same_day"])
        self.assertEqual(head["effective_window"], {"from": newer.published, "to": newer.published})
        self.assertIn("showed it on no date", head["wording"])
        self.assertIn(f"by version {newer.version}", head["wording"])
        shown, _ = ix.as_of(SYN, newer.published)
        self.assertEqual(shown.version, newer.version)
        self.assertNotIn("superseded_same_day", ix.header(newer))

    def test_dates_outside_the_record_are_flagged(self):
        inside = labels.read_label(self.ix, SYN, "2023-01-01", "34066-1")
        self.assertNotIn("record_note", inside)
        after = labels.read_label(self.ix, SYN, "2099-01-01", "34066-1")
        self.assertIn(self.ix.record_to, after["record_note"])
        self.assertIn("later versions may exist", after["record_note"])
        self.assertIn(f"until the record end on {self.ix.record_to}", after["wording"])
        self.assertIsNone(after["effective_window"]["to"])
        with self.assertRaises(labels.LabelError) as ctx:
            labels.changes_between(self.ix, "1990-01-01", "1991-01-01")
        self.assertIn(self.ix.record_from, str(ctx.exception))
        future = labels.changes_between(self.ix, "2099-01-01", "2099-12-31")
        self.assertEqual(future["count"], 0)
        self.assertIn("after the record end", future["record_note"])
        self.assertNotIn("record_note", labels.changes_between(self.ix, "2024-02-29", "2024-02-29"))

    def test_client_facing_messages_carry_no_filesystem_paths(self):
        with self.assertRaises(labels.LabelError) as ctx:
            self.ix.resolve_molecule("nonexistentmolecule")
        self.assertNotIn(str(MOLECULES), str(ctx.exception))
        self.assertIn("in the crosswalk", str(ctx.exception))
        self.assertEqual(labels.coverage(self.ix)["corpora"][0]["repo"], self.repo.name)
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(labels.LabelError) as ctx:
                labels.Index(Path(empty) / "not-a-repo", MOLECULES)
            self.assertNotIn(empty, str(ctx.exception))
            self.assertIn("'not-a-repo'", str(ctx.exception))

    def test_empty_submodule_directory_names_the_submodule_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            us = Path(tmp) / "us"
            us.mkdir()
            with self.assertRaises(labels.LabelError) as ctx:
                labels.Index(us, MOLECULES)
            self.assertEqual(str(ctx.exception), "repo 'us' is empty; run: git submodule update --init --recursive "
                                                 "(or build it with python3 tools/build.py build)")
            self.assertNotIn(tmp, str(ctx.exception))

    def test_cli_header_of_the_newest_version_names_the_record_end(self):
        lv = self.ix.label_versions(SYN)
        line = cli.text_header(self.ix, lv[-1], None, lv[-1].published)
        self.assertIn(f"newest in the record (record ends {self.ix.record_to})", line)
        self.assertNotIn("current to the build", line)

    def test_blame_credits_the_version_that_first_wrote_each_line(self):
        for r in READINGS["readings"]:
            with self.subTest(date=r["date"], section=r["section"]):
                out = labels.blame(self.ix, SYN, r["date"], r["section"])
                line = next(l for l in out["lines"] if l["text"] == r["quote"])
                self.assertEqual(line["version"], r["written_by_version"])
                self.assertEqual(out["version"], r["expect_version"])

    def test_word_blame_is_consistent_with_line_blame(self):
        out = labels.blame(self.ix, SYN, "2023-01-01", "43685-7", words=True)
        self.assertIn("difflib", out["word_method"])
        by_version = {v.version: v for v in self.ix.label_versions(SYN)}
        checked = 0
        for line in out["lines"]:
            if not line["text"].strip():
                continue
            self.assertEqual(" ".join(w["text"] for w in line["words"]), " ".join(line["text"].split()))
            for w in line["words"]:
                self.assertIn(w["version"], by_version)
                self.assertLessEqual(w["published_date"], line["published_date"])
                checked += 1
            if line["version"] == 1:
                self.assertTrue(all(w["version"] == 1 for w in line["words"]))
        self.assertGreater(checked, 100)
        quote = next(r["quote"] for r in READINGS["readings"] if r["date"] == "2023-01-01")
        line = next(l for l in out["lines"] if l["text"] == quote)
        self.assertEqual({w["version"] for w in line["words"]}, {line["version"]})

    def test_section_history_marks_creation_and_format_conversion(self):
        out = labels.section_history(self.ix, SYN[:8], "warnings")
        self.assertEqual(out["section"]["code"], "43685-7")
        self.assertEqual(out["changes"][0]["status"], "created")
        self.assertEqual(out["changes"][0]["version"], 1)
        flagged = [c for c in out["changes"] if c["format_conversion"]]
        self.assertEqual([(c["version"], c["format_conversion"]) for c in flagged], [(1506, "old-to-PLR")])
        self.assertTrue(all(isinstance(c["lines_added"], int) for c in out["changes"]))
        self.assertEqual(out["count"], len(out["changes"]))

    def test_diff_lists_versions_between_and_word_changes(self):
        out = labels.diff(self.ix, SYN, "2016-01-01", "2018-01-01", "43685-7")
        self.assertIn((1506, "old-to-PLR"), [(v["version"], v["format_conversion"]) for v in out["versions_between"]])
        self.assertEqual(len(out["sections"]), 1)
        self.assertIn("{+", out["sections"][0]["word_diff"])
        self.assertGreater(out["sections"][0]["words_added"], 0)
        same = labels.diff(self.ix, SYN, "2023-01-01", "2023-01-15")
        self.assertTrue(same["same_version"])
        whole = labels.diff(self.ix, SYN, "2022-01-01", "2024-06-01")
        self.assertNotIn("label.json", [s["file"] for s in whole["sections"]])
        self.assertGreater(whole["changed_sections"], 1)
        with self.assertRaises(labels.LabelError):
            labels.diff(self.ix, SYN, "2018-01-01", "2016-01-01")

    def test_changes_between_answers_example_query_2(self):
        out = labels.changes_between(self.ix, Q2["from"], Q2["to"], section=Q2["section"], query=Q2["query"])
        by_kind = {k: [c for c in out["changes"] if c["change"] == k] for k in ("added", "removed", "reworded")}
        self.assertEqual(rows(by_kind["added"]), rows(Q2["added"]))
        self.assertEqual(rows(by_kind["removed"]), rows(Q2["removed"]))
        self.assertEqual(rows(by_kind["reworded"]), rows(Q2["reworded"]))
        self.assertEqual(rows(out["new_labels"]), rows(Q2["new_labels"]))
        self.assertTrue(all(c["new_label"] for c in out["new_labels"]))
        self.assertNotIn(SYN, [c["label_id"] for c in out["changes"]])
        for c in by_kind["added"]:
            self.assertTrue(any(PHRASE in l.lower() for l in c["lines_after"]))
            self.assertEqual(c["section"]["code"], "43685-7")
        self.assertEqual(out["summary"], {"added": 3, "removed": 1, "reworded": 1})

    def test_changes_rows_carry_lines_before_and_after(self):
        out = labels.changes_between(self.ix, Q2["from"], Q2["to"], section=Q2["section"], query=Q2["query"])
        has = lambda lines: any(PHRASE in l.lower() for l in lines)   # noqa: E731
        for c in out["changes"]:
            with self.subTest(change=c["change"], label=c["label_id"][:8], version=c["version"]):
                self.assertNotIn("lines", c)
                self.assertIn("lines_before", c)
                self.assertIn("lines_after", c)
                if c["change"] == "added":
                    self.assertEqual(c["lines_before"], [])
                    self.assertTrue(has(c["lines_after"]))
                elif c["change"] == "removed":    # the deleted text is before, never after
                    self.assertTrue(has(c["lines_before"]))
                    self.assertEqual(c["lines_after"], [])
                else:
                    self.assertTrue(has(c["lines_before"]) and has(c["lines_after"]))
                    self.assertNotEqual(c["lines_before"], c["lines_after"])
        self.assertEqual({c["change"] for c in out["changes"]}, {"added", "removed", "reworded"})
        for n in out["new_labels"]:
            self.assertIsNone(n["lines_before"])
            self.assertTrue(has(n["lines_after"]))
        self.assertIn("a removed row only lines_before (the deleted text", out["note"])
        self.assertIn("'old-to-PLR'", out["note"])
        compact = labels.changes_between(self.ix, Q2["from"], Q2["to"], section=Q2["section"], query=PHRASE, compact=True)
        removed = next(c for c in compact["changes"] if c["change"] == "removed")
        self.assertGreater(removed["lines_before"], 0)
        self.assertEqual(removed["lines_after"], 0)

    def test_new_labels_are_first_versions_in_the_record(self):
        out = labels.changes_between(self.ix, Q2["from"], Q2["to"], section=Q2["section"], query=Q2["query"])
        self.assertTrue(out["new_labels"])
        for n in out["new_labels"]:
            with self.subTest(label=n["label_id"][:8]):
                first = self.ix.label_versions(n["label_id"])[0]
                self.assertEqual((n["version"], n["published_date"]), (first.version, first.published))
                self.assertTrue(Q2["from"] <= n["published_date"] <= Q2["to"])
        # A label whose first version in the record predates the window is compared, never listed as new.
        ids = {n["label_id"] for n in out["new_labels"]}
        for c in out["changes"]:
            self.assertLess(self.ix.label_versions(c["label_id"])[0].published, c["published_date"])
            if c["label_id"] in ids:   # later versions of a new label are classified as usual
                self.assertGreater(c["version"], next(n["version"] for n in out["new_labels"]
                                                      if n["label_id"] == c["label_id"]))
        self.assertIn("first version of its label in this record", out["note"])
        self.assertIn("not necessarily new on DailyMed", out["note"])
        self.assertNotIn("first version in the window", out["note"])

    def test_label_headings_name_old_format_sections(self):
        self.assertEqual(labels.label_headings("# WARNINGS\n\n## Embryofetal Toxicity\ntext\n# PRECAUTIONS\n"),
                         ["WARNINGS", "PRECAUTIONS"])
        self.assertEqual(labels.label_headings("## 5.1 Sub\ntext\n"), [])
        self.assertEqual(labels.label_headings("# 5 WARNINGS AND PRECAUTIONS\n"), ["WARNINGS AND PRECAUTIONS"])
        out = labels.changes_between(self.ix, Q2["from"], Q2["to"], query=PHRASE)
        self.assertIn(notices.OLD_FORMAT_SECTIONS, out["note"])
        for row in out["changes"] + out["new_labels"]:
            headings = row["section"].get("label_headings")
            if headings is not None:
                self.assertNotEqual(headings, [row["section"]["title"].upper()])

    def test_search_style_answers_state_their_scope(self):
        molecules = self.ix.molecule_count()
        expected = (f"Covers this record only: {len(self.ix.by_setid):,} US labels of {molecules:,} selected "
                    "molecules, as DailyMed showed them; not all of DailyMed and not every label of these molecules.")
        self.assertEqual(self.ix.scope(), expected)
        last = self.ix.record_to
        answers = {"changes": labels.changes_between(self.ix, last, last),
                   "changes_query": labels.changes_between(self.ix, Q2["from"], Q2["to"], query=PHRASE),
                   "search": labels.search(self.ix, PHRASE, last),
                   "first_appearance": labels.first_appearance(self.ix, PHRASE)}
        for name, out in answers.items():
            with self.subTest(name):
                self.assertEqual(out["scope"], expected)
                self.assertNotIn("approved", out["scope"].lower())
        self.assertNotIn("scope", labels.first_appearance(self.ix, PHRASE, "6a9e2fc8"))   # one label, no scope

    def test_changes_between_without_query_lists_changed_sections(self):
        out = labels.changes_between(self.ix, "2024-02-29", "2024-02-29")
        syn = next(c for c in out["changes"] if c["label_id"] == SYN)
        self.assertEqual(syn["version"], 1537)
        self.assertIn("34068-7", [s["code"] for s in syn["sections"]])
        self.assertFalse(syn["new_label"])
        self.assertEqual(out["count"] + out["unchanged_versions"], out["versions_in_window"])
        self.assertLessEqual(out["metadata_only_versions"], out["unchanged_versions"])
        limited = labels.changes_between(self.ix, "2007-01-01", "2026-12-31", limit=2)
        self.assertTrue(limited["truncated"])
        self.assertEqual(len(limited["changes"]), 2)
        self.assertGreater(limited["count"], 2)
        everything = labels.changes_between(self.ix, "2007-01-01", "2026-12-31")
        self.assertEqual(everything["count"] + everything["unchanged_versions"], len(self.ix.versions))
        self.assertLessEqual(len(everything["changes"]), labels.DEFAULT_LIMIT_ALL)
        compact = labels.changes_between(self.ix, "2024-02-29", "2024-02-29", compact=True)
        syn = next(c for c in compact["changes"] if c["label_id"] == SYN)
        self.assertTrue(compact["compact"])
        self.assertTrue(all(isinstance(s, str) for s in syn["sections"]))
        self.assertIn("34068-7", syn["sections"])
        compact_q = labels.changes_between(self.ix, Q2["from"], Q2["to"], section=Q2["section"], query=PHRASE, compact=True)
        self.assertEqual(compact_q["summary"], {"added": 3, "removed": 1, "reworded": 1})
        self.assertTrue(all(isinstance(c["lines_after"], int) and isinstance(c["lines_before"], int)
                            for c in compact_q["changes"]))

    def test_root_files_do_not_reach_search_or_changes(self):
        def paths(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("path", "previous_path") and isinstance(v, str):
                        yield v
                    else:
                        yield from paths(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from paths(v)

        root = self.ix.git("ls-tree", "--name-only", self.ix.versions[0].commit).split()
        self.assertTrue({"NOTICE.md", "README.md", "corpus.json"} <= set(root))     # the fixture has them
        last = self.ix.record_to
        answers = [labels.search(self.ix, "DailyMed", last), labels.search(self.ix, "National Library", last),
                   labels.first_appearance(self.ix, "National Library"),
                   labels.changes_between(self.ix, self.ix.record_from, last),
                   labels.changes_between(self.ix, self.ix.record_from, last, compact=True)]
        self.assertEqual(answers[1]["count"], 0)          # NOTICE.md says it; no label section does
        self.assertEqual(answers[2]["count"], 0)
        for out in answers:
            for path in paths(out):
                self.assertGreaterEqual(path.count("/"), 2, path)
        first = self.ix.versions[0]
        self.assertTrue(all(labels.is_label_path(f["path"]) for f in self.ix.log_numstat([first])[first.commit]))
        hits = self.ix.grep_files("National Library", [self.ix.head])[self.ix.head]
        self.assertEqual(hits, {})

    def test_search_reads_the_tree_in_force_on_the_date(self):
        before = labels.search(self.ix, PHRASE, "2024-07-01", section="43685-7")
        ids = {m["label_id"] for m in before["matches"]}
        self.assertIn("6a9e2fc8-bedf-46d4-9973-328dbcb18061", ids)
        self.assertIn("7e09aeca-0cec-4a88-9dde-a2098a08b877", ids)
        self.assertNotIn("84b00366-96ef-41e1-bac6-5d24acdb9e1d", ids)
        after = labels.search(self.ix, "SUICIDAL   ideation", "2024-08-01", section="43685-7")
        self.assertIn("84b00366-96ef-41e1-bac6-5d24acdb9e1d", {m["label_id"] for m in after["matches"]})
        match = next(m for m in after["matches"] if m["label_id"] == "84b00366-96ef-41e1-bac6-5d24acdb9e1d")
        self.assertEqual(match["version"], 8)
        self.assertEqual(match["sections"][0]["code"], "43685-7")
        with self.assertRaises(labels.LabelError):
            labels.search(self.ix, PHRASE, "1999-01-01")

    def test_first_appearance_per_label_and_corpus_wide(self):
        # Label-wide: this label carried the phrase in Adverse Reactions from version 2, long before
        # Warnings and Precautions gained it in version 5 (2024-06-21).
        one = labels.first_appearance(self.ix, PHRASE, "6a9e2fc8")
        self.assertEqual((one["earliest"]["first_version"], one["earliest"]["first_published"]), (2, "2021-09-01"))
        self.assertEqual([s["code"] for s in one["earliest"]["sections"]], ["34084-4"])
        biotin = labels.first_appearance(self.ix, "biotin", SYN)
        self.assertEqual(biotin["earliest"]["first_published"], "2024-02-29")
        self.assertIn("34068-7", [s["code"] for s in biotin["earliest"]["sections"]])
        corpus = labels.first_appearance(self.ix, PHRASE)
        by_label = {e["label_id"]: e for e in corpus["episodes"]}
        self.assertNotIn(SYN, by_label)
        self.assertEqual(by_label["f350afed-120b-422d-9e6e-3aaa0f72a5fb"]["left_version"], 23)
        self.assertFalse(by_label["f350afed-120b-422d-9e6e-3aaa0f72a5fb"]["present_in_latest_version"])
        self.assertTrue(by_label["7e09aeca-0cec-4a88-9dde-a2098a08b877"]["new_label"])
        self.assertEqual(corpus["earliest"]["first_published"], min(e["first_published"] for e in corpus["episodes"]))
        self.assertEqual(corpus["count"], len(corpus["episodes"]))
        self.assertEqual(corpus["labels_count"], len(by_label))
        self.assertIn("episode", corpus["note"])
        none = labels.first_appearance(self.ix, "zzz-never-written-anywhere")
        self.assertEqual(none["count"], 0)
        self.assertIn("never appeared", none["note"])

    def test_compare_jurisdictions_never_invents_eu_text(self):
        out = labels.compare_jurisdictions(self.ix, "Plaquenil", "2024-08-01", "43685-7")
        self.assertEqual(out["molecule"], "hydroxychloroquine")
        self.assertGreaterEqual(out["us"]["count"], 3)
        self.assertFalse(out["eu_available"])
        self.assertEqual(out["eu"], {"available": False, "reason": labels.EU_REASON, "labels": []})
        self.assertTrue(all(PHRASE in r["section"]["text"].lower() for r in out["us"]["labels"]
                            if r["label_id"] in {"6a9e2fc8-bedf-46d4-9973-328dbcb18061", "7e09aeca-0cec-4a88-9dde-a2098a08b877"}))
        with self.assertRaises(labels.LabelError):
            labels.compare_jurisdictions(self.ix, "not-a-molecule", "2024-08-01")

    def test_check_citations_is_strict(self):
        r = next(x for x in READINGS["readings"] if x["date"] == "2023-01-01")
        cites = [{"doc_id": SYN, "version": r["expect_version"], "section": r["section"], "quote": r["quote"].upper(),
                  "as_of": r["date"]},
                 {"doc_id": f"dailymed:{SYN}", "version": r["expect_version"], "section": "34066-1",
                  "quote": r["quote"], "as_of": r["date"]},
                 {"doc_id": SYN, "version": r["written_by_version"], "section": r["section"], "quote": r["quote"],
                  "as_of": r["date"]},
                 {"doc_id": SYN, "version": 999999, "quote": "x", "as_of": r["date"]},
                 {"doc_id": SYN, "version": r["expect_version"], "section": r["section"], "quote": r["quote"],
                  "as_of": "2000-01-01"}]
        out = labels.check_citations(self.ix, cites)
        flags = [(x["exists"], x["quote_found"], x["in_force_on_as_of"]) for x in out["results"]]
        self.assertEqual(flags, [(True, True, True), (True, False, True), (True, True, False), (False, False, False),
                                 (True, True, False)])
        self.assertIn("43685-7", out["results"][1]["problems"][0])
        self.assertEqual(out["results"][2]["in_force"]["version"], r["expect_version"])
        self.assertTrue(any("record for this label starts" in p for p in out["results"][4]["problems"]))
        self.assertEqual(out["passed"], 1)

    def test_coverage(self):
        out = labels.coverage(self.ix)
        us, eu = out["corpora"]
        self.assertEqual(us["labels"], len(FIXTURE["setids"]))
        self.assertNotIn("molecules", us)
        self.assertEqual(us["molecule_folders"], len({self.ix.folder(s).split("/")[0] for s in self.ix.by_setid}))
        self.assertIn("molecule_files", us)
        self.assertEqual(us["versions"], len(self.ix.versions))
        self.assertEqual(us["history_type"], "in-force")
        self.assertEqual(us["retired_labels"], 0)
        self.assertFalse(eu["built"])
        self.assertFalse(out["eu_available"])

    def test_retired_label_is_flagged_after_last_published(self):
        ix = labels.Index(self.repo, MOLECULES)
        setid = "f350afed-120b-422d-9e6e-3aaa0f72a5fb"
        # Status comes from the molecules/ crosswalk, never from label.json; last_published is the newest version.
        self.assertNotIn("status", ix.metadata(setid))
        lp = ix.label_versions(setid)[-1].published
        after = "2099-01-01"
        ix.molecules()
        ix._setid_molecules[setid] = [("doxepin", {"setid": setid, "status": "retired",
                                                   "successor_setids": ["00000000-0000-0000-0000-000000000000"]})]
        self.assertEqual(ix.status(setid)["last_published"], lp)
        live = labels.read_label(ix, setid, lp, "43685-7")
        self.assertEqual(live["status"], "retired")
        self.assertNotIn("status_note", live)
        stale = labels.read_label(ix, setid, after, "43685-7")
        self.assertIn(f"stopped updating this setid after {lp}", stale["status_note"])
        self.assertIn("00000000-0000-0000-0000-000000000000", stale["status_note"])
        last = ix.header(ix.label_versions(setid)[-1], after)
        self.assertEqual(last["effective_window"]["to"], lp)
        self.assertIn(f"until DailyMed stopped updating this setid on {lp}", last["wording"])
        self.assertNotIn("record end", last["wording"])
        found = labels.find_label(ix, setid[:8])["matches"][0]
        self.assertEqual(found["status"], "retired")
        self.assertEqual(labels.coverage(ix)["corpora"][0]["retired_labels"], 1)
        hit = next(m for m in labels.search(ix, "doxepin", after)["matches"] if m["label_id"] == setid)
        self.assertIn("status_note", hit)

    def test_cli_mirrors_the_tools(self):
        out = io.StringIO()
        sys.stdout, saved = out, sys.stdout
        try:
            code = cli.main(["--repo", str(self.repo), "--molecules", str(MOLECULES), "changes", Q2["from"], Q2["to"],
                             "--section", Q2["section"], "--query", Q2["query"]])
        finally:
            sys.stdout = saved
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["summary"], {"added": 3, "removed": 1, "reworded": 1})
        out = io.StringIO()
        sys.stdout = out
        try:
            code = cli.main(["--repo", str(self.repo), "show", "2023-01-01", SYN[:8], "--section", "boxed"])
        finally:
            sys.stdout = saved
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith(f"DailyMed showed {SYN} version 1534"))
        self.assertIn("=== 34066-1-boxed-warning.md", out.getvalue())


@skip_without_raw
class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f = fixture()
        cls.server = mcp_server.Server(f["repo"], MOLECULES)
        cls.server._index = f["index"]

    def call(self, method, params=None, msg_id=1):
        return self.server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})

    def fresh_server(self) -> "mcp_server.Server":
        server = mcp_server.Server(fixture()["repo"], MOLECULES)
        server._index = fixture()["index"]
        return server

    @staticmethod
    def serve_lines(server, messages: list) -> list:
        """Run newline-delimited messages through serve() and return the decoded stdout lines."""
        stdout = io.BytesIO()
        stdin = io.BytesIO("".join(json.dumps(m) + "\n" for m in messages).encode())
        mcp_server.serve(server, stdin, stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_batches_are_answered_with_arrays_on_older_protocols(self):
        ping = {"jsonrpc": "2.0", "id": "b1", "method": "ping"}
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        out = self.serve_lines(self.fresh_server(), [init, [ping], [ping, note], [ping, {**ping, "id": "b2"}], ping])
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual([m["id"] for m in out[1]], ["b1"])  # one-request batch -> one-element array
        self.assertEqual([m["id"] for m in out[2]], ["b1"])  # the notification gets no reply
        self.assertEqual([m["id"] for m in out[3]], ["b1", "b2"])
        self.assertIsInstance(out[4], dict)
        self.assertEqual(self.serve_lines(self.fresh_server(), [[]])[0]["error"]["code"], -32600)

    def test_batches_are_rejected_after_negotiating_2025_06_18(self):
        ping = {"jsonrpc": "2.0", "id": "b1", "method": "ping"}
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        out = self.serve_lines(self.fresh_server(), [[ping], init, [ping], ping])
        self.assertEqual([m["id"] for m in out[0]], ["b1"])  # before initialize the version is unknown: accepted
        self.assertEqual(out[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(out[2]["error"]["code"], -32600)
        self.assertIn("2025-06-18", out[2]["error"]["message"])
        self.assertEqual(out[3]["result"], {})

    def test_params_must_be_an_object_or_absent(self):
        for params in ([], 0, False, "str", [1]):
            with self.subTest(params=params):
                res = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params})
                self.assertEqual(res["error"]["code"], -32602)
                self.assertIn(type(params).__name__, res["error"]["message"])
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"], {})
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None})["result"], {})

    def test_null_optional_arguments_mean_absent(self):
        res = self.call("tools/call", {"name": "search", "arguments": {"text": PHRASE, "date": "2024-08-01",
                                                                      "section": None, "limit": None}})["result"]
        self.assertNotIn("isError", res)
        self.assertGreater(res["structuredContent"]["count"], 0)
        res = self.call("tools/call", {"name": "read_label", "arguments": {"label_id": SYN, "date": "2023-01-01",
                                                                          "section": None}})["result"]
        self.assertIn("sections", res["structuredContent"])
        res = self.call("tools/call", {"name": "first_appearance", "arguments": {"text": "biotin", "label_id": None}})["result"]
        self.assertNotIn("isError", res)
        res = self.call("tools/call", {"name": "read_label", "arguments": {"label_id": SYN, "date": None}})
        self.assertEqual(res["error"]["code"], -32602)  # a required argument may not be null

    def test_initialize_negotiates_protocol_version(self):
        for asked, expect in (("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
                              ("2099-01-01", "2025-06-18"), (None, "2025-06-18")):
            with self.subTest(asked=asked):
                params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
                if asked:
                    params["protocolVersion"] = asked
                res = self.call("initialize", params)["result"]
                self.assertEqual(res["protocolVersion"], expect)
                self.assertEqual(res["serverInfo"]["name"], "asof-drug-labels")
                self.assertEqual(set(res["capabilities"]), {"tools", "prompts", "resources"})
                self.assertIn("DailyMed showed", res["instructions"])

    def test_notifications_get_no_response_and_ping_is_empty(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.call("ping")["result"], {})

    def test_tools_list_has_the_eleven_tools(self):
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["find_label", "read_label", "section_history", "blame", "diff", "changes_between", "search",
                          "first_appearance", "compare_jurisdictions", "check_citations", "coverage"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["annotations"]["readOnlyHint"])
            self.assertTrue(t["description"])
        by = {t["name"]: t["description"] for t in tools}
        self.assertIn("removed row's deleted text is in lines_before", by["changes_between"])
        self.assertIn("first version of its label in this record", by["changes_between"])
        self.assertIn("'old-to-PLR'", by["changes_between"])
        for name in ("changes_between", "search", "first_appearance"):
            self.assertIn("scope field", by[name], name)

    def test_tools_call_result_shape(self):
        res = self.call("tools/call", {"name": "read_label", "arguments": {"label_id": SYN, "date": "2023-01-01",
                                                                           "section": "43685-7"}})["result"]
        self.assertNotIn("isError", res)
        self.assertEqual(res["content"][0]["type"], "text")
        self.assertEqual(json.loads(res["content"][0]["text"]), res["structuredContent"])
        self.assertEqual(res["structuredContent"]["version"], 1534)
        self.assertIn("notice", res["structuredContent"])

    def test_tool_level_failures_are_is_error_with_guidance(self):
        res = self.call("tools/call", {"name": "read_label", "arguments": {"label_id": SYN, "date": "2006-01-01"}})["result"]
        self.assertTrue(res["isError"])
        self.assertIn("starts 2007-04-13", res["structuredContent"]["error"])
        res = self.call("tools/call", {"name": "blame", "arguments": {"label_id": SYN, "date": "2023-01-01",
                                                                      "section": "nope"}})["result"]
        self.assertTrue(res["isError"])
        self.assertIn("available:", res["structuredContent"]["error"])

    def test_malformed_requests_get_json_rpc_errors(self):
        self.assertEqual(self.call("tools/call", {"name": "no_such_tool", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.call("tools/call", {"name": "read_label", "arguments": {"label_id": SYN}})["error"]["code"], -32602)
        self.assertEqual(self.call("tools/call", {"name": "read_label", "arguments": {"label_id": SYN, "date": 2023}})["error"]["code"], -32602)
        self.assertEqual(self.call("tools/call", {"name": "coverage", "arguments": {"bogus": 1}})["error"]["code"], -32602)
        self.assertEqual(self.call("nope/method")["error"]["code"], -32601)
        self.assertEqual(self.call("prompts/get", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(self.call("prompts/get", {"name": "label-change-brief", "arguments": {"drug": "x"}})["error"]["code"], -32602)
        bad = self.server.handle({"id": 1, "method": "ping"})
        self.assertEqual(bad["error"]["code"], -32600)
        stdout = io.BytesIO()
        mcp_server.serve(self.server, io.BytesIO(b"{not json}\n"), stdout)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], -32700)

    def test_prompts(self):
        names = [p["name"] for p in self.call("prompts/list")["result"]["prompts"]]
        self.assertEqual(names, ["label-change-brief", "check-document-claims"])
        res = self.call("prompts/get", {"name": "label-change-brief",
                                        "arguments": {"drug": "Synthroid", "from": "2022-01-01", "to": "2024-06-01"}})["result"]
        text = res["messages"][0]["content"]["text"]
        self.assertEqual(res["messages"][0]["role"], "user")
        for needle in ("find_label", "diff", "section_history", "DailyMed showed", "2022-01-01"):
            self.assertIn(needle, text)
        res = self.call("prompts/get", {"name": "check-document-claims",
                                        "arguments": {"text": "Synthroid treats hypothyroidism.", "as_of": "2023-01-01"}})["result"]
        text = res["messages"][0]["content"]["text"]
        for needle in ("check_citations", "DailyMed showed", "Synthroid treats hypothyroidism."):
            self.assertIn(needle, text)

    def test_resources(self):
        self.assertEqual(self.call("resources/list")["result"], {"resources": []})
        templates = self.call("resources/templates/list")["result"]["resourceTemplates"]
        self.assertEqual(templates[0]["uriTemplate"], "label://{jurisdiction}/{label_id}/{date}/{section}")
        uri = f"label://us/{SYN}/2023-01-01/43685-7"
        res = self.call("resources/read", {"uri": uri})["result"]
        self.assertEqual(res["contents"][0]["uri"], uri)
        self.assertEqual(res["contents"][0]["mimeType"], "text/markdown")
        self.assertIn("DailyMed showed version 1534", res["contents"][0]["text"])
        self.assertIn("Courtesy of the U.S. National Library of Medicine", res["contents"][0]["text"])
        self.assertEqual(self.call("resources/read", {"uri": f"label://eu/{SYN}/2023-01-01/43685-7"})["error"]["code"], -32002)
        self.assertEqual(self.call("resources/read", {"uri": f"label://us/{SYN}/2000-01-01/43685-7"})["error"]["code"], -32002)
        self.assertEqual(self.call("resources/read", {"uri": "file:///etc/passwd"})["error"]["code"], -32602)

    def test_every_tool_runs_through_the_server(self):
        calls = {
            "find_label": {"query": "Synthroid"},
            "section_history": {"label_id": SYN, "section": "43685-7"},
            "blame": {"label_id": SYN, "date": "2023-01-01", "section": "43685-7", "words": True},
            "diff": {"label_id": SYN, "date_a": "2016-01-01", "date_b": "2018-01-01", "section": "43685-7"},
            "changes_between": {"from": Q2["from"], "to": Q2["to"], "section": Q2["section"], "query": Q2["query"]},
            "search": {"text": PHRASE, "date": "2024-08-01", "section": "43685-7", "limit": 2},
            "first_appearance": {"text": PHRASE},
            "compare_jurisdictions": {"molecule": "hydroxychloroquine", "date": "2024-08-01"},
            "check_citations": {"citations": [{"doc_id": SYN, "version": 1534, "section": "43685-7", "quote": "x", "as_of": "2023-01-01"}]},
            "coverage": {},
        }
        for name, args in calls.items():
            with self.subTest(tool=name):
                res = self.call("tools/call", {"name": name, "arguments": args})["result"]
                self.assertNotIn("isError", res, res["content"][0]["text"][:300])
                self.assertEqual(res["structuredContent"]["record_from"], self.server.index().record_from)


@skip_without_raw
class StdioEndToEndTest(unittest.TestCase):
    """Spawns the server the way the one-line install (`claude mcp add asof-drug-labels -- python3
    tools/mcp_server.py`) does, with the socket module disabled inside the child so any network attempt fails."""

    BOOTSTRAP = ("import socket, sys, runpy\n"
                 "def refuse(*a, **k): raise SystemExit('network access attempted')\n"
                 "socket.socket.connect = refuse; socket.create_connection = refuse; socket.getaddrinfo = refuse\n"
                 "sys.argv = [sys.argv[1]] + sys.argv[2:]\n"
                 "runpy.run_path(sys.argv[0], run_name='__main__')\n")

    def test_initialize_list_call_prompt_resource(self):
        repo = fixture()["repo"]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "blame", "arguments": {"label_id": SYN, "date": "2024-06-01", "section": "34068-7"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
             "params": {"name": "label-change-brief", "arguments": {"drug": "Synthroid", "from": "2023-01-01", "to": "2024-06-01"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": f"label://us/{SYN}/2023-01-01/43685-7"}},
            {"jsonrpc": "2.0", "id": 6, "method": "ping"},
        ]
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        env = {**os.environ, "ASOF_US_REPO": str(repo)}
        proc = subprocess.run([sys.executable, "-I", "-c", self.BOOTSTRAP, str(TOOLS / "mcp_server.py"),
                               "--molecules", str(MOLECULES)], input=stdin, capture_output=True, text=True, env=env,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        replies = {}
        for line in lines:
            msg = json.loads(line)  # every stdout line must be a JSON-RPC message
            self.assertEqual(msg["jsonrpc"], "2.0")
            replies[msg["id"]] = msg
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5, 6])
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(replies[2]["result"]["tools"]), 11)
        blame = replies[3]["result"]["structuredContent"]
        reading = next(r for r in READINGS["readings"] if r["date"] == "2024-06-01")
        line = next(l for l in blame["lines"] if l["text"] == reading["quote"])
        self.assertEqual(line["version"], reading["written_by_version"])
        self.assertIn("find_label", replies[4]["result"]["messages"][0]["content"]["text"])
        self.assertIn("DailyMed showed version 1534", replies[5]["result"]["contents"][0]["text"])
        self.assertEqual(replies[6]["result"], {})
        self.assertNotIn("network access attempted", proc.stderr)


@unittest.skipUnless(os.environ.get("ASOF_FULL_REPO"), "set ASOF_FULL_REPO to a built repo to run the full-corpus checks")
class FullCorpusTest(unittest.TestCase):
    """Example queries on a full build, with query 2 cross-checked by an independent scan."""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(os.environ["ASOF_FULL_REPO"])
        started = time.perf_counter()
        cls.ix = labels.Index(cls.repo, MOLECULES)
        cls.index_seconds = time.perf_counter() - started

    def test_history_is_linear_and_sorted(self):
        self.assertEqual(self.ix.published, sorted(self.ix.published))
        self.assertEqual(self.ix.git("rev-list", "--min-parents=2", "--count", "HEAD").strip(), "0")
        self.assertEqual(set(self.ix.by_setid), set(self.ix.folders))
        # Every commit is a label version: a metadata-only commit on top would make git HEAD diverge from the index.
        self.assertEqual(self.ix.git("rev-parse", "HEAD").strip(), self.ix.head)
        self.assertEqual(int(self.ix.git("rev-list", "--count", "HEAD")), len(self.ix.versions))

    def test_section_whose_file_name_changed_is_one_section(self):
        # brimonidine 865f88b0: 44425-7 was 'storage-and-handling.md' in v1 (2007-12-12) and
        # 'storage-and-handling-section.md' from v2 (2010-06-03), deleted + created in one commit.
        setid, code, day = "865f88b0-b13e-4607-a314-939f1b331e6f", "44425-7", "2010-06-03"
        history = labels.section_history(self.ix, setid, code)
        self.assertEqual([(c["version"], c["status"]) for c in history["changes"]][:2], [(1, "created"), (2, "renamed")])
        self.assertEqual(history["changes"][1]["renamed_from"], "44425-7-storage-and-handling.md")
        out = labels.diff(self.ix, setid, "2007-12-12", day, code)
        self.assertEqual([(s["file"], s["renamed_from"]) for s in out["sections"]],
                         [("44425-7-storage-and-handling-section.md", "44425-7-storage-and-handling.md")])
        for query, kinds in (("between", []), ("mortar and pestle", ["added"]), ("rx only", ["removed"]),
                             ("protective seal", ["reworded"])):
            with self.subTest(query=query):
                rows = [c for c in labels.changes_between(self.ix, day, day, section=code, query=query)["changes"]
                        if c["label_id"] == setid]
                self.assertEqual([c["change"] for c in rows], kinds)
                self.assertTrue(all(c["renamed"] for c in rows))

    def test_same_day_superseded_version_is_flagged(self):
        pairs = [(a, b) for lv in self.ix.by_setid.values() for a, b in zip(lv, lv[1:]) if a.published == b.published]
        self.assertTrue(pairs, "the corpus has no same-day version pair")
        for older, newer in pairs:
            with self.subTest(label=older.setid[:8], version=older.version):
                head = self.ix.header(older, older.published)
                self.assertTrue(head["superseded_same_day"])
                self.assertIn("showed it on no date", head["wording"])
                shown, _ = self.ix.as_of(older.setid, older.published)
                self.assertNotEqual(shown.version, older.version)

    def test_example_query_1_matches_known_readings(self):
        for r in READINGS["readings"]:
            with self.subTest(date=r["date"], section=r["section"]):
                out = labels.blame(self.ix, SYN, r["date"], r["section"])
                self.assertEqual(out["version"], r["expect_version"])
                line = next(l for l in out["lines"] if l["text"] == r["quote"])
                self.assertEqual(line["version"], r["written_by_version"])
        first = labels.first_appearance(self.ix, "biotin", SYN)
        self.assertEqual(first["earliest"]["first_version"], 1537)

    def test_example_query_2_matches_an_independent_scan(self):
        out = labels.changes_between(self.ix, Q2["from"], Q2["to"], section=Q2["section"], query=Q2["query"])
        expected = self.scan_section_versions()
        for kind in ("added", "removed", "reworded"):
            with self.subTest(kind=kind):
                self.assertEqual(rows([c for c in out["changes"] if c["change"] == kind]), rows(expected[kind]))
        self.assertEqual(rows(out["new_labels"]), rows(expected["new_label"]))

    def scan_section_versions(self) -> dict[str, list[dict]]:
        """Walk every label's 43685-7 versions with plain git log/cat-file; shares nothing with labels.py.

        Each version's section text is the concatenation of the 43685-7 files present after replaying
        the commit's additions and deletions, so a renamed file (D + A in one commit) or several files
        under one code are compared as one text."""
        git = ["git", "-C", str(self.repo)]
        log = subprocess.run([*git, "log", "--reverse", "--format=%x00%H", "--name-status", "--",
                              f"*/{Q2['section']}-*.md"], capture_output=True, text=True, check=True).stdout
        touched: dict[str, list[tuple[str, str]]] = {}
        commit = ""
        for line in log.splitlines():
            if line.startswith("\x00"):
                commit = line[1:]
                touched[commit] = []
            elif line.strip():
                status, _, path = line.partition("\t")
                touched[commit].append((status.strip()[0], path.strip()))
        specs = [f"{c}:{p}" for c, files in touched.items() for status, p in files if status != "D"]
        proc = subprocess.run([*git, "cat-file", "--batch"], input="\n".join(specs).encode() + b"\n",
                              capture_output=True, check=True)
        blobs, pos, out = {}, 0, proc.stdout
        for spec in specs:
            nl = out.index(b"\n", pos)
            head = out[pos:nl].decode()
            pos = nl + 1
            if head.endswith("missing"):
                blobs[spec] = None
                continue
            size = int(head.rsplit(" ", 1)[1])
            blobs[spec] = out[pos:pos + size].decode()
            pos += size + 1
        meta = subprocess.run([*git, "log", "--reverse", "--format=%H %(trailers:key=Source-Id,valueonly,separator=) "
                               "%(trailers:key=Source-Version,valueonly,separator=) "
                               "%(trailers:key=Published-Date,valueonly,separator=)"],
                              capture_output=True, text=True, check=True).stdout
        per_label: dict[str, list[tuple]] = {}
        for line in meta.splitlines():
            h, source_id, version, published = line.split()
            per_label.setdefault(source_id.split(":")[1], []).append((h, int(version), published))
        norm = lambda t: re.sub(r"\s+", " ", t).lower()
        result = {"added": [], "removed": [], "reworded": [], "new_label": []}
        for setid, vs in per_label.items():
            current: dict[str, str] = {}
            text = None
            for i, (h, version, published) in enumerate(vs):
                for status, path in touched.get(h, []):
                    if status == "D":
                        current.pop(path, None)
                    else:
                        current[path] = blobs[f"{h}:{path}"]
                text = "\n".join(current[p] for p in sorted(current)) if current else None
                now = text is not None and PHRASE in norm(text)
                in_window = Q2["from"] <= published <= Q2["to"]
                row = {"label_id": setid, "version": version, "published_date": published}
                if i == 0:
                    if now and in_window:
                        result["new_label"].append(row)
                elif in_window and h in touched:
                    before_text = prev
                    was = before_text is not None and PHRASE in norm(before_text)
                    if now and not was:
                        result["added"].append(row)
                    elif was and not now:
                        result["removed"].append(row)
                    elif was and now:
                        old = [l for l in before_text.splitlines() if PHRASE in norm(l)]
                        new = [l for l in text.splitlines() if PHRASE in norm(l)]
                        if old != new:
                            result["reworded"].append(row)
                prev = text
        return result

    def test_example_query_3_is_honest_about_eu(self):
        out = labels.compare_jurisdictions(self.ix, "atorvastatin", "2024-06-01", "34070-3")
        self.assertFalse(out["eu_available"])
        self.assertGreater(out["us"]["count"], 0)
        self.assertEqual(out["eu"]["labels"], [])

    def test_corpus_wide_first_appearance_is_fast_enough(self):
        started = time.perf_counter()
        out = labels.first_appearance(self.ix, PHRASE)
        elapsed = time.perf_counter() - started
        self.assertGreater(out["count"], 0)
        self.assertLess(elapsed, 30, f"corpus-wide first_appearance took {elapsed:.1f} s")


if __name__ == "__main__":
    unittest.main()
