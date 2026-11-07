"""Visuals: the log model, the chart rules, title templates, byte-stable SVG and --check.

The unit tests run on a small synthetic git log (tools/tests/visualise_fixtures/log.txt in the
format of ``git log --reverse --no-renames --name-status --format=%x1e%H%x00%B%x00``) and a
synthetic crosswalk. The --check test needs build/us and build/eu (or ASOF_FULL_REPO and
ASOF_FULL_EU_REPO) and the committed visualisations/, and skips otherwise.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import visualise as v  # noqa: E402

HERE = Path(__file__).parent
FIXTURES = HERE / "visualise_fixtures"
ROOT = TOOLS.parent


def fixture_labels():
    return v.group_labels(v.parse_log((FIXTURES / "log.txt").read_text(encoding="utf-8")))


def fixture_cross():
    return v.load_crosswalk(FIXTURES / "molecules")


class LogModelTest(unittest.TestCase):
    def test_parse(self):
        commits = v.parse_log((FIXTURES / "log.txt").read_text(encoding="utf-8"))
        self.assertEqual(len(commits), 12)
        first = commits[0]
        self.assertEqual(first.setid, "aaaaaaaa-0000-0000-0000-000000000001")
        self.assertEqual(first.version, 1)
        self.assertEqual(first.published, "2008-03-01")
        self.assertIn(("A", "alpha/aaaaaaaa-0000-0000-0000-000000000001/34067-9-indications-and-usage.md"),
                      first.files)
        self.assertEqual(next(c for c in commits if c.format_conversion).version, 4)

    def test_root_files_of_the_first_commit_are_not_label_files(self):
        text = (FIXTURES / "log.txt").read_text(encoding="utf-8")
        first_files = "A\talpha/aaaaaaaa-0000-0000-0000-000000000001/label.json\n"
        self.assertEqual(text.count(first_files), 1)
        with_root = text.replace(first_files, "A\tNOTICE.md\nA\tREADME.md\nA\tcorpus.json\n" + first_files, 1)
        commits = v.parse_log(with_root)
        self.assertEqual(commits, v.parse_log(text))
        self.assertTrue(all(path.count("/") >= 2 for c in commits for _, path in c.files))

    def test_changed_codes_skip_label_json_and_count_rename_once(self):
        c = v.Commit("x", "s", 2, "2010-01-01", "", "", (
            ("M", "a/s/label.json"), ("D", "a/s/34067-9-indications.md"), ("A", "a/s/34067-9-indications-and-usage.md"),
            ("M", "a/s/34084-4-adverse-reactions.md")))
        self.assertEqual(v.changed_codes(c), {"34067-9", "34084-4"})

    def test_counted_changes_exclude_first_and_conversion(self):
        lab = fixture_labels()["aaaaaaaa-0000-0000-0000-000000000001"]
        counted = v.counted_changes(lab)
        self.assertEqual([c.version for c, _ in counted], [2, 3, 5, 6])   # v1 first, v4 a format conversion

    def test_groups(self):
        self.assertEqual(v.group_of("34071-1"), "Warnings and precautions")
        self.assertEqual(v.group_of("42232-9"), "Warnings and precautions")
        self.assertEqual(v.group_of("51945-4"), v.OTHER)
        self.assertEqual(v.group_of("34092-7", v.REASON_GROUPS), "Clinical studies")
        self.assertEqual(v.group_of("34092-7"), v.OTHER)


class ChurnTest(unittest.TestCase):
    def setUp(self):
        self.d = v.churn_data(fixture_labels(), fixture_cross(), "2012-06-30")
        self.rows = {r["year"]: r for r in self.d["rows"]}

    def test_years_end_at_last_complete_year(self):
        self.assertEqual(self.d["years"], [2009, 2010, 2011])
        self.assertEqual(v.last_complete_year("2025-12-31"), 2025)
        self.assertEqual(v.last_complete_year("2026-10-30"), 2025)

    def test_live_label_denominator(self):
        # A: current, first 2008. B: retired, 2008-2009 (live 2009, not 2010+). C: current, first 2011.
        self.assertEqual(self.rows[2009]["live_labels"], 2)
        self.assertEqual(self.rows[2010]["live_labels"], 1)
        self.assertEqual(self.rows[2011]["live_labels"], 2)

    def test_counts_and_rates(self):
        r9 = self.rows[2009]
        # A v2 (2009): indications + boxed warning added; B v2 (2009): adverse reactions; package label -> Other
        self.assertEqual(r9["changes"]["Indications"], 1)
        self.assertEqual(r9["changes"]["Boxed warning"], 1)
        self.assertEqual(r9["changes"]["Adverse reactions"], 1)
        self.assertEqual(r9["changes"][v.OTHER], 1)
        self.assertEqual(r9["per_100"]["Indications"], 50.0)
        self.assertEqual(r9["clinical_per_label"], 1.5)

    def test_title_template(self):
        title, peak, last = v.churn_title(self.d["rows"])
        self.assertEqual(peak["year"], 2009)
        self.assertEqual(title, "A live US label had 1.5 clinical-section changes in 2009, the busiest year since "
                                "2009, and 0.5 in 2011")


class HeroTest(unittest.TestCase):
    def test_boxed_arc(self):
        lab = fixture_labels()["aaaaaaaa-0000-0000-0000-000000000001"]
        arc = v.boxed_arc(lab)
        self.assertEqual((arc["added_version"], arc["added_date"]), (2, "2009-05-01"))
        self.assertEqual((arc["removed_version"], arc["removed_date"]), (6, "2012-02-01"))
        self.assertEqual(arc["last_present_version"], 5)
        self.assertEqual(arc["changed_while_present"], 2)   # v3 modified, v4 conversion modified
        self.assertEqual((arc["boxed_warning_changes"], arc["boxed_warning_conversion_versions"]), (1, [4]))

    def test_chantix_shaped_fixture_counts_changes_without_conversions(self):
        lab = v.group_labels(v.parse_log((FIXTURES / "chantix-shaped-log.txt").read_text(encoding="utf-8")))[
            "cccccccc-0000-0000-0000-00000000000c"]
        arc = v.boxed_arc(lab)
        self.assertEqual((arc["added_version"], arc["removed_version"]), (2, 9))
        self.assertEqual(arc["changed_while_present"], 6)
        self.assertEqual((arc["boxed_warning_changes"], arc["boxed_warning_change_versions"]), (3, [6, 7, 8]))
        self.assertEqual((arc["boxed_warning_conversions"], arc["boxed_warning_conversion_versions"]), (3, [3, 4, 5]))
        self.assertIn("changed 3 times (plus 3 format conversions)", v.life_title("Chantix-shaped", 9, 2008, arc))

    def test_rule_filters(self):
        labs, cross = fixture_labels(), fixture_cross()
        old = v.HERO_MIN_VERSIONS
        try:
            v.HERO_MIN_VERSIONS = 3
            cands = v.hero_candidates(labs, cross)
        finally:
            v.HERO_MIN_VERSIONS = old
        self.assertEqual([c["setid"] for c in cands], ["aaaaaaaa-0000-0000-0000-000000000001"])
        self.assertEqual(v.hero_candidates(labs, cross), [])   # nothing has 40 versions

    def test_title_template(self):
        arc = {"added_date": "2009-07-29", "removed_date": "2017-03-14", "boxed_warning_changes": 3,
               "boxed_warning_conversions": 3}
        self.assertEqual(v.life_title("Chantix", 43, 2007, arc),
                         "Chantix: 43 label versions since 2007. The boxed warning appeared on 29 Jul 2009, changed "
                         "3 times (plus 3 format conversions), and was gone on 14 Mar 2017.")
        arc.update(boxed_warning_changes=1, boxed_warning_conversions=0)
        self.assertIn("changed 1 time, and was gone", v.life_title("X", 2, 2007, arc))


class RewrittenTest(unittest.TestCase):
    def test_tie_rule_earlier_first_published(self):
        top = v.top_labels(fixture_labels(), 3)
        # A has 6 versions; B and C have 3 each: B first published 2008 comes before C (2011).
        self.assertEqual([lab.setid[-1] for lab in top], ["1", "2", "3"])

    def test_reason(self):
        reason, counts = v.reason_of(fixture_labels()["aaaaaaaa-0000-0000-0000-000000000001"])
        self.assertEqual(counts["Boxed warning"], 3)   # added v2, modified v3, deleted v6
        self.assertEqual(reason, "Boxed warning")

    def test_title_and_names(self):
        self.assertEqual(v.rewritten_title({"display_name": "Keytruda", "versions": 104, "first_year": 2014}, 5388),
                         "Keytruda's US label has 104 versions since 2014, the most of 5,388 labels in the corpus")
        self.assertEqual(v.brand_name("KEYTRUDA"), "Keytruda")
        self.assertEqual(v.brand_name("DEPAKOTE ER"), "Depakote ER")
        self.assertEqual(v.brand_name("CLEOCIN PHOSPHATE (0009-6582) (standalone)"), "Cleocin Phosphate")


class UsVsEuTest(unittest.TestCase):
    US = ("# 4 CONTRAINDICATIONS\n\nDRUG is contraindicated in patients with:\n\n"
          "- Known hypersensitivity to drug [see Warnings and Precautions (5.1)]\n"
          "- Severe hepatic impairment.\nThis applies to Child-Pugh C.\n\n"
          "Concomitant use of strong CYP3A4 inhibitors\n[see Drug Interactions (7.1)].\n")

    def test_us_items_group_paragraphs_and_bullets(self):
        self.assertEqual(v.us_items(self.US), [
            ["- Known hypersensitivity to drug [see Warnings and Precautions (5.1)]"],
            ["- Severe hepatic impairment.", "This applies to Child-Pugh C."],
            ["Concomitant use of strong CYP3A4 inhibitors", "[see Drug Interactions (7.1)]."]])

    def test_us_items_drop_headings_rules_and_intro_lines(self):
        text = "# 4 CONTRAINDICATIONS\n\n| a | b |\n|---|---|\nUse is contraindicated in:\n- Pregnancy\n"
        self.assertEqual(v.us_items(text), [["| a | b |"], ["- Pregnancy"]])

    def test_eu_items_one_per_line(self):
        eu = ("# 4.3 Contraindications\n\nHypersensitivity to the active substance.\nThe following:\n"
              "Severe hepatic impairment\n[Avastin 25 mg/ml]\nPregnancy (see section 4.6).\n")
        self.assertEqual(v.eu_items(eu), [["Hypersensitivity to the active substance."],
                                          ["Severe hepatic impairment"], ["Pregnancy (see section 4.6)."]])

    def test_drop_rules(self):
        self.assertEqual(v.drop_reason("none"), "none")
        self.assertEqual(v.drop_reason("see section 4.4"), "cross-reference")
        self.assertEqual(v.drop_reason("[see warnings and precautions (5.1)]"), "cross-reference")
        self.assertIsNone(v.drop_reason("pregnancy (see section 4.6)"))
        for key in ("known hypersensitivity to x", "patients allergic to peanut", "history of anaphylaxis",
                    "severe hypersensitivity and active bleeding"):   # one sentence with both counts as hypersensitivity
            self.assertEqual(v.drop_reason(key), "hypersensitivity", key)
        self.assertEqual(v.drop_reason("a described line", frozenset({"a described line"})), "descriptions table")
        self.assertIsNone(v.drop_reason("active bleeding"))

    def test_categories(self):
        self.assertEqual(v.classify_items([["None."]])["category"], "none")
        self.assertEqual(v.classify_items([["Known hypersensitivity to X."], ["See section 4.4."]])["category"],
                         "hypersensitivity-only")
        mixed = v.classify_items([["None."], ["- Active bleeding."]])   # "None." dropped, the rest classified
        self.assertEqual((mixed["category"], mixed["kept"], mixed["dropped"]),
                         ("lists", [["- Active bleeding."]], [["None."]]))
        with self.assertRaises(v.VisualError):
            v.classify_items([])

    def test_cells(self):
        expect = {("none", "lists"): "only_eu", ("hypersensitivity-only", "lists"): "only_eu",
                  ("lists", "none"): "only_us", ("lists", "hypersensitivity-only"): "only_us",
                  ("lists", "lists"): "both", ("none", "none"): "neither",
                  ("none", "hypersensitivity-only"): "neither", ("hypersensitivity-only", "none"): "neither",
                  ("hypersensitivity-only", "hypersensitivity-only"): "neither"}
        for (us, eu), cell in expect.items():
            self.assertEqual(v.cell_of(us, eu), cell)
        cells = [v.cell_of(us, eu) for us in v.CATEGORIES for eu in v.CATEGORIES]
        t = v.tally(cells)
        self.assertEqual(sum(t[name] for name in v.CELL_NAMES), len(cells))
        self.assertEqual((t["only_eu"], t["only_us"], t["both"], t["neither"], t["one_side_only"]), (2, 2, 1, 4, 4))

    def test_descriptions_table_sets_aside_one_item(self):
        us = v.us_items("# C\n\n- Allergic contact dermatitis.\n\nIsolated skin reactions were described "
                        "[see Adverse Reactions (6.2)].\n")
        eu = v.eu_items("# 4.3\n\nHypersensitivity to the active substance.\n")
        entry = {"molecule": "x", "side": "us", "doc_id": "d", "version": 1, "section": "34070-3",
                 "line": "Isolated skin reactions were described [see Adverse Reactions (6.2)].", "reason": "r"}
        res = v.classify_pair(us, eu, [entry])
        self.assertEqual((res["us"]["category"], res["cell"], res["rule_only_cell"]),
                         ("hypersensitivity-only", "neither", "only_us"))

    def test_descriptions_table_entry_must_match_exactly_one_item(self):
        eu = v.eu_items("# 4.3\n\nHypersensitivity.\nContains soya oil.\n")
        entry = {"molecule": "x", "side": "eu", "line": "Contains peanut oil."}
        with self.assertRaises(v.VisualError):
            v.classify_pair([["None."]], eu, [entry])
        twice = v.eu_items("# 4.3\n\nContains soya oil.\nContains soya oil.\n")
        with self.assertRaises(v.VisualError):
            v.match_description({**entry, "line": "Contains soya oil."}, twice)

    def test_committed_table_loads(self):
        entries = v.load_descriptions()
        self.assertEqual([(e["molecule"], e["side"]) for e in entries], [("rivastigmine", "us"), ("zonisamide", "eu")])
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "t.json"
            bad.write_text(json.dumps({"entries": [{**entries[0], "section": "4.3"}]}), encoding="utf-8")
            with self.assertRaises(v.VisualError):
                v.load_descriptions(bad)

    def test_title_template(self):
        self.assertEqual(v.us_eu_title(39, 122, "2026-10-30"),
                         "In 39 of 122 molecules, only one of the US and EU labels lists a contraindication beyond "
                         "hypersensitivity (30 Oct 2026)")


class SvgTest(unittest.TestCase):
    def test_min_font_enforced(self):
        svg = v.Svg("t", "d")
        with self.assertRaises(v.VisualError):
            svg.text(0, 0, "tiny", 20)

    def test_render_is_deterministic_and_well_formed(self):
        import xml.dom.minidom
        data = v.churn_data(fixture_labels(), fixture_cross(), "2012-06-30")
        title, peak, last = v.churn_title(data["rows"])
        payload = {"inputs": {"build_date": "2012-07-01", "us_head": "0123456789abcdef",
                              "us_record_to": "2012-06-30"}, **data,
                   "molecules": 3, "title": title, "peak": peak}
        a, b = v.render_churn(payload), v.render_churn(payload)
        self.assertEqual(a, b)
        xml.dom.minidom.parseString(a)
        self.assertIn("Built 2012-07-01 · US 0123456", a)
        self.assertIn("(1.5 per label = 150 per 100)", a)
        self.assertIn("Source: NLM DailyMed", a)
        self.assertIn("Independent project by Particula Tech", a)
        self.assertIn("as of 30 Jun 2012", a)
        self.assertIn("Method: per 100 labels", a)
        sizes = [int(s) for s in re.findall(r'font-size="(\d+)"', a)]
        self.assertGreaterEqual(min(sizes), 22)

    def test_footer_leaves_the_plot_more_than_half_the_height(self):
        data = {"inputs": {"date": "2026-10-30"}, "descriptions_table": {"entries": 1, "path": "sources/x.json"}}
        foot = v.us_eu_footnote(data)
        self.assertTrue(foot.startswith("Method: "))
        self.assertIn("does not establish absence of a risk", foot)
        for source in (v.us_source("2026-10-30"), v.eu_source("2026-10-30")):
            self.assertIn("Independent project by Particula Tech", source)
            fr = v.draw_frame(v.Svg("t", "d"), "A title with a number 39 of 122", "A subtitle line", foot, source,
                              v.stamp("2026-11-05", "0123456789abcdef", "fedcba9876543210"))
            self.assertGreater(fr.bottom - fr.top, 0.5 * v.H)

    def test_us_eu_columns_fit_long_names_whole(self):
        short = [{"molecule": "abacavir", "us_category": "lists"}] * 8
        self.assertEqual(len(set(v.us_eu_columns([("h", short, False)], 4, 22, "None.", 28))), 1)
        rows = [{"molecule": "mycophenolate-mofetil", "us_category": "lists"}] + [
            {"molecule": "abiraterone", "us_category": "none"}] * 7
        widths = v.us_eu_columns([("h", rows, True)], 4, 22, "None.", 28)
        self.assertGreaterEqual(widths[0], v.text_width("mycophenolate-mofetil", 22))
        self.assertGreaterEqual(widths[1], v.text_width("abiraterone", 22) + v.text_width("None.", 22) + 10)
        self.assertLessEqual(sum(widths) + 12 * 3, v.W - 2 * v.MARGIN + 4)

    def test_wrap(self):
        lines = v.wrap("one two three four five six seven eight nine ten", 52, 400, bold=True)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(v.text_width(line, 52, True) <= 400 for line in lines))

    def test_png_size_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (2160).to_bytes(4, "big")
                          + (2700).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")
            self.assertEqual(v.png_size(p), (2160, 2700))


def full_repos():
    us = Path(os.environ.get("ASOF_FULL_REPO") or ROOT / "build" / "us")
    eu = Path(os.environ.get("ASOF_FULL_EU_REPO") or ROOT / "build" / "eu")
    return us, eu


@unittest.skipUnless(all((p / ".git").exists() for p in full_repos())
                     and (ROOT / "visualisations" / "data").exists(), "needs build/us, build/eu and visualisations/")
class FullCheckTest(unittest.TestCase):
    def test_check_regenerates_byte_for_byte(self):
        us, eu = full_repos()
        code = v.main(["all", "--check", "--repo", str(us), "--eu-repo", str(eu)])
        self.assertEqual(code, 0)

    def test_hero(self):
        d = json.loads((ROOT / "visualisations" / "data" / "02-life-of-a-label.json").read_text(encoding="utf-8"))
        self.assertEqual(d["hero"]["setid"], "f0ff4f27-5185-4881-a749-c6b7a0ca5696")
        self.assertEqual(d["hero"]["added_date"], "2009-07-29")
        self.assertEqual(d["hero"]["removed_date"], "2017-03-14")

    def test_query3_date_is_the_us_record_end(self):
        q = json.loads((ROOT / "visualisations" / "data" / "01-example-queries.json").read_text(encoding="utf-8"))
        m = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        if q["inputs"]["us_head"] != m["corpora"]["us"]["head"]:
            self.skipTest("manifest.json is for another US HEAD than the committed queries")
        self.assertEqual(q["query3_compare"]["as_of"], m["corpora"]["us"]["record_to"])
        self.assertEqual(q["query1_blame"]["as_of"], m["corpora"]["us"]["record_to"])

    def test_us_vs_eu_split(self):
        d = json.loads((ROOT / "visualisations" / "data" / "05-us-vs-eu-contraindications.json").read_text(
            encoding="utf-8"))
        rows = d["rows"]
        self.assertEqual(sum(d["cells"].values()), len(rows))
        self.assertEqual(d["only_eu"] + d["only_us"] + d["both"] + d["neither"], len(rows))
        self.assertEqual(d["one_side_only"], d["only_eu"] + d["only_us"])
        self.assertEqual(d["funnel"]["both_sides"], len(rows))
        self.assertEqual(d["descriptions_table"]["matched"], d["descriptions_table"]["entries"])
        for r in rows:
            self.assertEqual(r["cell"], v.cell_of(r["us_category"], r["eu_category"]))
            self.assertEqual(r["us_category"] == "lists", bool(r["us_items_kept"]))
            self.assertEqual(r["eu_category"] == "lists", bool(r["eu_items_kept"]))
        self.assertEqual(d["title"], v.us_eu_title(d["one_side_only"], len(rows), d["inputs"]["date"]))

    def test_every_chart_has_numbers_source_and_stamp(self):
        for key, name in v.NAMES.items():
            if key == "queries":   # data only, no chart
                continue
            svg =(ROOT / "visualisations" / f"{name}.svg").read_text(encoding="utf-8")
            data = json.loads((ROOT / "visualisations" / "data" / f"{name}.json").read_text(encoding="utf-8"))
            self.assertRegex(data["title"], r"\d")
            self.assertIn("Source: NLM DailyMed", svg)
            self.assertIn("Independent project by Particula Tech", svg)
            if key == "us-vs-eu":
                self.assertIn("does not establish absence of a risk", svg)
            self.assertIn(f"Built {data['inputs']['build_date']}", svg)
            self.assertGreaterEqual(min(int(s) for s in re.findall(r'font-size="(\d+)"', svg)), 22)
            self.assertNotIn("approved on", svg.lower())


if __name__ == "__main__":
    unittest.main()
