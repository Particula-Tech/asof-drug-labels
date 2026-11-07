# Validation of the US label history against FDA's Safety-related Labeling Changes (SrLC): pre-registered check

Generated 2026-11-01. Sample: `sources/srlc-sample.json` (50 items, item list sha256 `a9aa9777351d7de969f64d55bf2ccb48070a72d55f1f010d67e30cb90e9ebc4d`, fixed 2026-11-01 before any matching, revised once after results as described below, parse version 2). Run on the US corpus build of 2026-11-01 (pre-release; its commit hashes are not in the published us/ history): 47,174 label versions of 5,388 setids, record 2006-07-13 to 2026-10-30. Setids walked: 61 covered setid entries (60 distinct) and 31 uncovered setids of the same applications for diagnostics (31 distinct); 0 missing from the repo. The rules, constants and section groups are those recorded in the sample file; this report applies them without change.

Sample revision (parse version 2, 2026-11-01). Parse version 1 had four faults: it read only the first content div of each SrLC section (3,495 of 15,982 section regions in the cache have two or more divs), took an <h4> nested inside a content div for a new section (21 phantom items), let all-bold headings longer than 8 tokens without sentence punctuation (boxed-warning titles, table captions) into the quote, and split sentences after abbreviations such as 'e.g.', 'vs.' and 'St.'. Parse version 2 fixes all four; the selection rules (6, 9, 10), the hash key and every constant are unchanged. The corrected pool has 6,271 eligible items (was 5,774); the unchanged hash order replaces three items, and 12 retained items carry longer or shorter quotes than before (listed under retained_quote_changes). Items replaced by rerunning the unchanged selection rule on the corrected pool: `srlc-NDA021692-20161216-SUPPL14-BW` -> `srlc-NDA021306-20161216-SUPPL24-AR` (2016), `srlc-NDA021924-20170425-SUPPL12-WP` -> `srlc-NDA203094-20170505-SUPPL7-DI` (2017), `srlc-BLA125433-20200929-SUPPL31-WP` -> `srlc-NDA202106-20200512-SUPPL7-WP` (2020). Previous item list sha256 `0ecf8c8cff52f01b527446bb7fdb69b5316e85413bafa7185adb358b676821fb`.

## Headline

**37 of 50 sampled SrLC changes match (74%) under the pre-registered rule.** An item matches when some covered setid has a transition (quote present, previous version not) published within 183 days either side of the SrLC approval date. CBE-0 supplements let a company implement a safety change before FDA approves it, and DailyMed publication can trail approval by months, so a transition up to 183 days either side of the approval date counts as a match.

## Counts

By category:

| Category | Items |
| --- | --- |
| match | 37 |
| late | 3 |
| early | 5 |
| absent | 5 |

By year:

| Year | Items | Match | Late | Early | Absent |
| --- | --- | --- | --- | --- | --- |
| 2016 | 5 | 4 | 0 | 1 | 0 |
| 2017 | 5 | 2 | 1 | 2 | 0 |
| 2018 | 5 | 5 | 0 | 0 | 0 |
| 2019 | 5 | 4 | 0 | 0 | 1 |
| 2020 | 5 | 4 | 0 | 0 | 1 |
| 2021 | 5 | 5 | 0 | 0 | 0 |
| 2022 | 5 | 4 | 0 | 0 | 1 |
| 2023 | 5 | 3 | 2 | 0 | 0 |
| 2024 | 5 | 3 | 0 | 2 | 0 |
| 2025 | 5 | 3 | 0 | 0 | 2 |

By SrLC section:

| Section | Items | Match | Late | Early | Absent |
| --- | --- | --- | --- | --- | --- |
| AR | 14 | 10 | 0 | 2 | 2 |
| BW | 3 | 2 | 1 | 0 | 0 |
| C | 2 | 2 | 0 | 0 | 0 |
| DI | 4 | 3 | 1 | 0 | 0 |
| USP | 5 | 4 | 0 | 0 | 1 |
| WP | 22 | 16 | 1 | 3 | 2 |

## Lag of the matches

Lag = published date of the repo version that first carries the quote minus the SrLC approval date, in days, over the 37 matches (the closest matching setid per item). Min -128, first quartile 7, median 11, third quartile 15, max 164, mean 6.1. A negative lag means DailyMed carried the text before the SrLC approval date. The transition reported is the one nearest the approval date (rule 7); when the setid carried the quote in an earlier version, lost it and regained it, the earlier appearance is listed below and in the JSON as first_present_version.

Matches whose setid carried the quote before the chosen transition: `srlc-NDA021262-20250918-SUPPL28-WP` (ALPHAGAN P) first present in version 22 (2025-04-07, lag -164), chosen transition version 24 (lag +15).

## Sensitivity

Reported, never the headline. Covered setids stay as pinned in the sample; only the presence rule or the window changes.

| Variant | Presence | Window (days) | Matches | Rate | Late | Early | Absent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| preregistered | substring or line coverage >= 0.90; found sentences hold >= 0.80 of the tokens | +-183 | 37 | 74% | 3 | 5 | 5 |
| strict | substring only; found sentences hold 1.00 of the tokens | +-183 | 33 | 66% | 2 | 3 | 12 |
| loose | substring or line coverage >= 0.80; found sentences hold >= 0.60 of the tokens | +-183 | 39 | 78% | 3 | 6 | 2 |
| window_365 | pre-registered presence | +-365 | 40 | 80% | 2 | 3 | 5 |

## Items

Version (setid): the DailyMed version number and the first 8 characters of its setid; for a match, the version that first carries the quote, with its Published-Date; for a late or early item, the nearest version that carries it (marked `first` when the quote is already in the setid's first version); blank for absent. Lag in days from the SrLC approval date.

| # | Item | Drug | SrLC date | Section | Outcome | Version (setid) | Published | Lag |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `srlc-NDA019892-20161216-SUPPL29-WP` | DILAUDID | 2016-12-16 | WP | match | 5 (3afae8b9) | 2016-12-23 | 7 |
| 2 | `srlc-NDA021560-20161012-SUPPL19-WP` | ZORTRESS | 2016-10-12 | WP | early | 12 (e082a024) | 2015-02-04 | -616 |
| 3 | `srlc-NDA021306-20161216-SUPPL24-AR` | BUTRANS | 2016-12-16 | AR | match | 16 (794aa355) | 2016-12-22 | 6 |
| 4 | `srlc-NDA207932-20161216-SUPPL2-BW` | BELBUCA | 2016-12-16 | BW | match | 7 (69df676c) | 2016-12-16 | 0 |
| 5 | `srlc-NDA203993-20161216-SUPPL5-DI` | ONFI | 2016-12-16 | DI | match | 14 (de03bd69) | 2017-01-06 | 21 |
| 6 | `srlc-NDA021574-20170405-SUPPL20-DI` | FORTAMET | 2017-04-05 | DI | late | 9 (ed71d7ea) | 2017-12-21 | 260 |
| 7 | `srlc-NDA021086-20170223-SUPPL44-WP` | ZYPREXA ZYDIS | 2017-02-23 | WP | match | 51 (d5051fbc) | 2017-01-23 | -31 |
| 8 | `srlc-NDA020626-20171214-SUPPL27-WP` | IMITREX | 2017-12-14 | WP | early | 10 (cc11b09d) | 2013-12-13 | -1462 |
| 9 | `srlc-NDA022341-20170425-SUPPL28-AR` | VICTOZA | 2017-04-25 | AR | early | 16 (5a9ef4ea) | 2016-05-11 | -349 |
| 10 | `srlc-NDA203094-20170505-SUPPL7-DI` | TYBOST | 2017-05-05 | DI | match | 4 (3784c35c) | 2017-05-16 | 11 |
| 11 | `srlc-NDA012151-20180306-SUPPL75-DI` | ALDACTONE | 2018-03-06 | DI | match | 14 (0fed2822) | 2018-03-21 | 15 |
| 12 | `srlc-NDA021999-20180727-SUPPL35-WP` | INVEGA | 2018-07-27 | WP | match | 28 (7b8e5b26) | 2018-08-07 | 11 |
| 13 | `srlc-NDA006134-20180201-SUPPL45-BW` | DOLOPHINE HYDROCHLORIDE | 2018-02-01 | BW | match | 14 (7a4840d6) | 2018-02-01 | 0 |
| 14 | `srlc-BLA125514-20180820-SUPPL35-WP` | KEYTRUDA | 2018-08-20 | WP | match | 51 (9333c79b) | 2018-08-24 | 4 |
| 15 | `srlc-NDA050753-20181005-SUPPL22-AR` | TOBI | 2018-10-05 | AR | match | 15 (94f9e516) | 2018-10-25 | 20 |
| 16 | `srlc-BLA125514-20190730-SUPPL55-AR` | KEYTRUDA | 2019-07-30 | AR | match | 73 (9333c79b) | 2019-08-07 | 8 |
| 17 | `srlc-NDA210806-20190919-SUPPL2-AR` | PIFELTRO | 2019-09-19 | AR | match | 7 (76ce1f00) | 2019-09-27 | 8 |
| 18 | `srlc-NDA022196-20190206-SUPPL11-WP` | ZOLPIMIST | 2019-02-06 | WP | absent |  |  |  |
| 19 | `srlc-NDA022249-20191121-SUPPL24-WP` | TREANDA | 2019-11-21 | WP | match | 23 (39d53698) | 2019-12-04 | 13 |
| 20 | `srlc-NDA204150-20191029-SUPPL10-WP` | DESVENLAFAXINE | 2019-10-29 | WP | match | 28 (91cf39c9) | 2019-11-27 | 29 |
| 21 | `srlc-NDA020427-20200124-SUPPL21-USP` | SABRIL | 2020-01-24 | USP | absent |  |  |  |
| 22 | `srlc-NDA202106-20200512-SUPPL7-WP` | MEROPENEM AND SODIUM CHLORIDE IN DUPLEX CONTAINER | 2020-05-12 | WP | match | 21 (3b952a55) | 2020-05-27 | 15 |
| 23 | `srlc-NDA021446-20200403-SUPPL40-WP` | LYRICA | 2020-04-03 | WP | match | 46 (60185c88) | 2020-04-20 | 17 |
| 24 | `srlc-BLA761064-20200505-SUPPL10-USP` | RITUXAN HYCELA | 2020-05-05 | USP | match | 12 (3e5b7e82) | 2020-10-16 | 164 |
| 25 | `srlc-NDA022433-20200528-SUPPL28-WP` | BRILINTA | 2020-05-28 | WP | match | 40 (f7b3f443) | 2020-06-02 | 5 |
| 26 | `srlc-NDA020375-20210930-SUPPL37-C` | MENOSTAR | 2021-09-30 | C | match | 14 (1e9702c4) | 2021-10-14 | 14 |
| 27 | `srlc-BLA761086-20210909-SUPPL1-USP` | AVSOLA | 2021-09-09 | USP | match | 8 (de34bcb7) | 2021-09-23 | 14 |
| 28 | `srlc-NDA018956-20210805-SUPPL112-AR` | OMNIPAQUE 140 | 2021-08-05 | AR | match | 13 (eee15ebe) | 2021-08-12 | 7 |
| 29 | `srlc-NDA050065-20210514-SUPPL65-AR` | MAXITROL | 2021-05-14 | AR | match | 2 (22c6d2db) | 2021-05-26 | 12 |
| 30 | `srlc-BLA208673-20210728-SUPPL11-WP` | SOLIQUA 100/33 | 2021-07-28 | WP | match | 16 (4bba538b) | 2021-08-09 | 12 |
| 31 | `srlc-BLA761036-20220304-SUPPL41-AR` | DARZALEX | 2022-03-04 | AR | match | 28 (a4d0efe9) | 2022-03-11 | 7 |
| 32 | `srlc-NDA022301-20221116-SUPPL21-WP` | APRISO | 2022-11-16 | WP | match | 30 (7951139c) | 2022-11-23 | 7 |
| 33 | `srlc-BLA761105-20220121-SUPPL14-C` | SKYRIZI | 2022-01-21 | C | match | 130 (7148c8eb) | 2022-01-28 | 7 |
| 34 | `srlc-NDA050818-20220816-SUPPL7-WP` | TOBRADEX ST | 2022-08-16 | WP | match | 5 (c2d7325e) | 2022-08-31 | 15 |
| 35 | `srlc-NDA204629-20220224-SUPPL33-AR` | JARDIANCE | 2022-02-24 | AR | absent |  |  |  |
| 36 | `srlc-NDA020712-20230411-SUPPL38-AR` | CARBATROL | 2023-04-11 | AR | match | 38 (bc03e499) | 2023-04-24 | 13 |
| 37 | `srlc-NDA204223-20231215-SUPPL25-WP` | MORPHINE SULFATE | 2023-12-15 | WP | late | 7 (1f53de80) | 2025-04-14 | 486 |
| 38 | `srlc-BLA761034-20230512-SUPPL49-AR` | TECENTRIQ | 2023-05-12 | AR | match | 34 (6fa682c9) | 2023-05-23 | 11 |
| 39 | `srlc-NDA021419-20231013-SUPPL23-WP` | METHYLIN | 2023-10-13 | WP | match | 30 (9e3c22d9) | 2023-10-30 | 17 |
| 40 | `srlc-NDA021475-20231013-SUPPL12-BW` | METHYLIN | 2023-10-13 | BW | late | 17 (09f13452) | 2025-01-15 | 460 |
| 41 | `srlc-BLA761304-20240621-SUPPL6-USP` | VYVGART HYTRULO | 2024-06-21 | USP | match | 4 (7104d9e5) | 2024-02-14 | -128 |
| 42 | `srlc-NDA206473-20241108-SUPPL10-AR` | LINEZOLID IN SODIUM CHLORIDE 0.9% IN PLASTIC CONTAINER | 2024-11-08 | AR | early | 15 (c401f1a8) | 2023-10-03 | -402 |
| 43 | `srlc-NDA020608-20241008-SUPPL47-AR` | OMNIPAQUE 350 | 2024-10-08 | AR | match | 11 (164b27b9) | 2024-10-21 | 13 |
| 44 | `srlc-NDA217003-20240222-SUPPL2-WP` | IMBRUVICA | 2024-02-22 | WP | early | 47 (0dfd0279) | 2023-06-02 | -265 |
| 45 | `srlc-NDA019339-20240207-SUPPL68-USP` | HEPARIN SODIUM 10,000 UNITS IN DEXTROSE 5% IN PLASTIC CONTAINER | 2024-02-07 | USP | match | 28 (eede8a0c) | 2023-12-19 | -50 |
| 46 | `srlc-NDA019079-20250923-SUPPL41-AR` | FLAREX | 2025-09-23 | AR | absent |  |  |  |
| 47 | `srlc-NDA020825-20250122-SUPPL65-WP` | GEODON | 2025-01-22 | WP | match | 46 (8326928a) | 2025-01-29 | 7 |
| 48 | `srlc-NDA011719-20250528-SUPPL142-WP` | METHOTREXATE LPF | 2025-05-28 | WP | absent |  |  |  |
| 49 | `srlc-BLA761269-20251215-SUPPL15-WP` | LEQEMBI | 2025-12-15 | WP | match | 18 (9d1ff786) | 2025-09-15 | -91 |
| 50 | `srlc-NDA021262-20250918-SUPPL28-WP` | ALPHAGAN P | 2025-09-18 | WP | match | 24 (264c4494) | 2025-10-03 | 15 |

## Caveats on individual items

- Short quotes (3 items with at most 15 kept tokens; valid under rule 2 but weak evidence, since a cross-reference or a lead-in sentence can match any edit of the section): `srlc-NDA022433-20200528-SUPPL28-WP` (BRILINTA, 12 tokens, match): '(PLATO and PEGASUS) to 21% (THEMIS)' / 'and 6.9% (THEMIS) of patients.'; `srlc-NDA020375-20210930-SUPPL37-C` (MENOSTAR, 12 tokens, match): '[see Warnings and Precautions (5.2)]' / '[see Warnings and Precautions (5.1)]'; `srlc-NDA020712-20230411-SUPPL38-AR` (CARBATROL, 13 tokens, match): 'There have been rare reports of impaired male fertility and/or abnormal spermatogenesis.'.
- Labels without the SrLC brand (1 item): rule 6 maps an item to every setid under its application number, so the version shown can belong to an authorized generic or a sibling product of the same sponsor. `srlc-NDA020375-20210930-SUPPL37-C` (SrLC brand MENOSTAR) shown in `1e9702c4` 'CLIMARA (ESTRADIOL) PATCH [BAYER HEALTHCARE PHARMACEUTICALS INC.]'.

## Misses explained

- `srlc-NDA021560-20161012-SUPPL19-WP` (ZORTRESS, 2016-10-12, WP), early: Text already present earlier. The underlined sentence has been in 5.10 since version 12 (2015-02-04, lag -616); version 13 (2015-10-26) expanded it with a pulmonary-hypertension clause, which still covers every token of the quote, so the pre-registered rule sees no new transition. The October 2016 supplement re-added the plain sentence as a second copy in version 14 (2016-11-02, lag +21), which is why the strict variant records a match. Evidence: e082a024 43685-7: v12 line 69 'Cases of ILD, implying lung intraparenchymal inflammation (pneumonitis) and/or fibrosis of non-infectious etiology, have occurred in patients receiving rapamycins and their derivatives, including Zortress.'; v13 line 69 adds 'some reported with pulmonary hypertension (including pulmonary arterial hypertension [PAH]) as a secondary event'; v14 lines 66-67 carry both forms. Versions in the window: v14 only.
- `srlc-NDA021574-20170405-SUPPL20-DI` (FORTAMET, 2017-04-05, DI), late: No label version in the window. The retired Shionogi setid went from version 8 (2013-02-20) straight to version 9 (2017-12-21, lag +260), which carries all nine sentences of the three Drug Interactions subsections (Alcohol, Carbonic Anhydrase Inhibitors, Drugs that reduce metformin clearance) verbatim. Version 8 already had three of them (the alcohol-lactate sentence and the two pharmacokinetic sentences SrLC underlines, 73 of 191 tokens, share 0.38) and 'should be warned against' instead of 'Warn patients' (coverage 8/9). Match under the +-365 window. Evidence: ed71d7ea 43685-7: v9 lines 39 and 128 (alcohol), 123-125 (carbonic anhydrase inhibitors), 103-104, 110 and 112 (drugs that reduce metformin clearance); v8 lines 27, 109 and 111 carry the three pre-existing sentences. Versions in the window: none.
- `srlc-NDA020626-20171214-SUPPL27-WP` (IMITREX, 2017-12-14, WP), early: Text already present earlier; SrLC repeats it as a new subsection. The four sentences of 5.9 Local Irritation have been in the IMITREX nasal spray label since the PLR conversion in version 10 (2013-12-13, lag -1462), and the old-format label had a 'Local Irritation' warning before that. The only change in the window, version 15 (2017-12-21, lag +7), replaced 'Nasal Spray' with 'nasal spray' throughout. Evidence: cc11b09d 43685-7: v10 line 66 'Local irritative symptoms such as burning, numbness, paresthesia, discharge, and pain or soreness were reported in approximately 5% of patients in controlled clinical trials and were noted to be severe in about 1%.' under '## 5.9 Local Irritation'; v9 (2012-09-13, old format) has '## Local Irritation' under WARNINGS; word diff v14 to v15: 48 words added, 49 removed, all 'nasal spray' casing. SrLC comment: '(new subsection added)'.
- `srlc-NDA022341-20170425-SUPPL28-AR` (VICTOZA, 2017-04-25, AR), early: Text already present. SrLC underlines nearly all of 6.1 for SUPPL-28, but all 34 kept sentences are already in version 16 (2016-05-11, lag -349; 33 verbatim, the calcitonin sentence at 0.90 coverage), the version that added 1,036 words to Adverse Reactions and corresponds to SrLC's SUPPL-25 entry of 2016-04-22 (an FDA summary without text). The in-window version 18 (2017-08-31, lag +128) is the LEADER update: it reworded 'clinical trials' to 'glycemic control trials' and removed or rewrote the Malignancy and Saxenda cholelithiasis paragraphs, so 26 of 34 sentences remain and the share drops to 0.76. Match under +-365. Evidence: 5a9ef4ea 34084-4: v15 share 0.28; v16 share 1.00 (line 17 'The data in Table 1 are derived from 5 placebo-controlled clinical trials [see Clinical Studies (14)].' and onwards); v17 1.00; v18 and v19 0.76 (26 of 34 found). raw/srlc/items.json has an NDA022341 SUPPL-25 entry of 2016-04-22 (AR) with comment type none and no quote. Versions in the window: v17 (-106), v18 (+128).
- `srlc-NDA022196-20190206-SUPPL11-WP` (ZOLPIMIST, 2019-02-06, WP), absent: Text added under a different setid. NDA022196 changed labeler: the covered setid b2e26965 (Magna Pharmaceuticals) never carried the falls sentence, not even in its last two versions (2019-09-11, 2019-10-28). The Aytu BioPharma setid 7b99e2ac carries it verbatim from its first version (2019-03-05, lag +27), but rule 6 excludes that setid because its first version is after approval minus 183 days. Evidence: b2e26965 (ZolpiMIST, Magna, retired): versions 2016-03-23, 2019-09-11, 2019-10-28, no line containing 'risk of falls' or 'drowsiness' in 43685-7. 7b99e2ac (ZOLPIMIST, Aytu, 2019-03-05 to 2022-07-01): v3 43685-7 line 55 'Because Zolpimist can cause drowsiness and a decreased level of consciousness, patients, particularly the elderly, are at higher risk of falls.' Earlier labeler ECR (2facd90f) retired 2017-12-28.
- `srlc-NDA020427-20200124-SUPPL21-USP` (SABRIL, 2020-01-24, USP), absent: Text filed under another section by SrLC. The SrLC 'Use in Specific Populations' block for SUPPL-21 holds four subsections: 8.1 Pregnancy, 8.2 Lactation, 8.4 Pediatric Use and '17 PATIENT COUNSELING INFORMATION'. The five specific-population sentences (124 of 220 tokens) entered the label at version 26 (2020-02-10, lag +17) and are found verbatim; the six counseling sentences (96 tokens) live in the label's section 17 file (34076-0), which rule 4 does not put in the USP group, so the share is 0.56 (< 0.80) at version 26 and in every later version. Under parse version 1 the quote held the first subsection only (124 tokens) and the item was a match at +17; the loose variant (share 0.60) records that match. Evidence: a5d389d2: v24 (2019-08-15, -162) share 0.00; v26 (2020-02-10, +17) share 0.56, 5 of 11 sentences found at 43684-0 lines 47 and 49 (lactation) and 53, 58, 64 (pediatric use); the unfound counseling sentences are at 34076-0 lines 35, 37, 42-43 of the same version, and 'Administration Instructions for SABRIL Powder for Oral Solution' is a heading (34068-7 line 105, 34076-0 line 5). Versions in the window: v24 (-162), v26 (+17), v27 (+115).
- `srlc-NDA204629-20220224-SUPPL33-AR` (JARDIANCE, 2022-02-24, AR), absent: Wording in the label differs from SrLC's text: SrLC spells out 'less than or equal to 40%' where the label prints '≤40%', and normalisation drops the symbol, so the 33-token sentence covers 28 of 33 tokens (0.85, under 0.90) and the quote's share stays at 0.48. The heart-failure paragraph itself entered the label in version 19 (2022-03-14, lag +18); the loose variant records that match. Evidence: faf3dd6a 34084-4 v19 line 112 'The EMPEROR-Reduced study included 3730 patients with heart failure and left ventricular ejection fraction (LVEF) ≤40% followed for a median of 16 months, and EMPEROR-Preserved included 5988 patients with heart failure and LVEF >40% followed for a median of 26 months.'; SrLC HTML: '(LVEF) less than or equal to 40% followed for a median of 16 months'. Other three sentences found verbatim at v19 (lines 110, 113, 114).
- `srlc-NDA204223-20231215-SUPPL25-WP` (MORPHINE SULFATE, 2023-12-15, WP), late: Change implemented outside the window. The December 2023 opioid class update (5.6 Opioid-Induced Hyperalgesia and Allodynia, 5.13 seizures, 5.14 withdrawal) is missing from the versions published in or near the window, 2024-01-10 (lag +26, setid 1f53de80) and 2024-10-21 (lag +311, setid 582f42e5), which carry only the older benzodiazepine and neonatal-withdrawal paragraphs (share 0.53). The full text arrives in version 7 of 1f53de80 (2025-04-14, lag +486) and version 6 of 582f42e5 (2025-03-21, lag +462). Evidence: 1f53de80 43685-7 v6 (2024-01-10): line 49 has 'Profound sedation, respiratory depression, coma, and death may result from the concomitant use of Morphine Sulfate Injection with benzodiazepines', no line contains 'opioid-induced hyperalgesia'; v7 (2025-04-14) line 65 '## 5.6 Opioid-Induced Hyperalgesia and Allodynia' and every quote sentence verbatim. 582f42e5: v5 2024-10-21 share 0.53, v6 2025-03-21 share 1.00.
- `srlc-NDA021475-20231013-SUPPL12-BW` (METHYLIN, 2023-10-13, BW), late: No label version published in the window. The METHYLIN chewable tablet setid (Shionogi, retired) has nothing between version 16 (2017-01-17, old format, no boxed warning section) and version 17 (2025-01-15, lag +460), the PLR conversion that carries the new abuse, misuse and addiction boxed warning verbatim. Evidence: 09f13452 versions: 2017-01-17 (v16, label format old, 34066-1 empty), 2025-01-15 (v17, PLR). v17 34066-1 lines 3-8: 'Methylin has a high potential for abuse and misuse, which can lead to the development of a substance use disorder, including addiction.' through 'Throughout Methylin treatment, reassess each patient’s risk of abuse, misuse, and addiction'. Strict variant is absent only because the label cites '[see Overdosage (10)]' where SrLC has '(see OVERDOSAGE)'.
- `srlc-NDA206473-20241108-SUPPL10-AR` (LINEZOLID IN SODIUM CHLORIDE 0.9% IN PLASTIC CONTAINER, 2024-11-08, AR), early: Change implemented outside the window (lag -402). Hospira's linezolid injection took the sentence into 6.2 and 5.1 in version 15 (2023-10-03), 77 days after SrLC's identical ZYVOX entries of 2023-07-18 (NDA021130, NDA021131, NDA021132, SUPPL-44/41/43); FDA's approval of the same wording for this application is recorded 13 months later, 2024-11-08. The versions in the window (2024-07-02, 2024-10-02, 2025-05-08) leave the sentence unchanged. Evidence: c401f1a8 34084-4: v14 (2023-06-19) no match; v15 (2023-10-03) line 140 'Thrombocytopenia has been reported more often in patients with severe renal impairment and in patients with moderate to severe hepatic impairment [see Warnings and Precautions (5.1)]; sideroblastic anemia.' and 43685-7 line 7. raw/srlc items srlc-NDA021130-20230718-SUPPL44-AR, srlc-NDA021131-20230718-SUPPL41-AR and srlc-NDA021132-20230718-SUPPL43-AR carry the same quote; no AR diff between v17 (2024-10-02) and v18 (2025-05-08).
- `srlc-NDA217003-20240222-SUPPL2-WP` (IMBRUVICA, 2024-02-22, WP), early: SrLC repeats an earlier change. IMBRUVICA has one label for NDA205552, NDA210563 and NDA217003. The hypertension long-term safety text was approved under NDA205552 SUPPL-40 and NDA210563 SUPPL-17 on 2023-05-18 and entered the shared label in version 47 (2023-06-02, lag +15 against those entries, -265 against this one). The sampled NDA217003 SUPPL-2 entry of 2024-02-22 records the later approval of the same wording for the third application. Matches under the +-365 window. Evidence: 0dfd0279 (Imbruvica, Pharmacyclics, application numbers NDA205552, NDA210563, NDA217003) 43685-7 v47 line 46 'In a long-term safety analysis over 5 years of 1,284 patients with B-cell malignancies treated for a median of 36 months (range, 0 to 98 months), the cumulative rate of hypertension increased over time.'; raw/srlc items srlc-NDA205552-20230518-SUPPL40-WP and srlc-NDA210563-20230518-SUPPL17-WP contain the same text. Five versions in the window (2023-09-22 to 2024-07-30) all carry it.
- `srlc-NDA019079-20250923-SUPPL41-AR` (FLAREX, 2025-09-23, AR), absent: No setid in the crosswalk carries the change. The entry is the PLR conversion (the quote is the new 6 Adverse Reactions cross-reference list); the covered Eyevance setid has no version after 2025-01-20 (lag -246), and the Harrow (2025-04-03, excluded by rule 6) and retired Alcon (2026-01-02) setids are still old format. Either DailyMed had not published the converted label by the record end (2026-10-30) or it was published under a newer setid the crosswalk does not list; the record cannot tell the two apart. Evidence: 19918ea5, f060dec4 and 6ae122c0 all have Label-Format old in their last versions, with the unnumbered '# ADVERSE REACTIONS' starting 'Glaucoma with optic nerve damage, visual acuity and field defects, cataract formation'; none contains '[see Warnings and Precautions (5.1)]'. SrLC text: 'The following clinically significant adverse reactions are described elsewhere in the labeling: Intraocular Pressure (IOP) Increase [see Warnings and Precautions (5.1)] ...'. Best score over every version of 19918ea5: 0.14.
- `srlc-NDA011719-20250528-SUPPL142-WP` (METHOTREXATE LPF, 2025-05-28, WP), absent: Wording in the label differs from SrLC's text: SrLC spells out 'SPF greater than or equal to 30' where the label prints 'SPF ≥30', and normalisation drops the symbol, so the 38-token sentence covers 33 of 38 tokens (0.87, under 0.90) and the quote's share is 0.21 in all three covered setids. The sun-exposure text itself arrived in the window in every setid: 0d63ba29 version 19 (2025-06-12, lag +15), 0e30eaef version 16 (2025-07-07, lag +40) and bd287e89 version 6 (2025-07-07, lag +40); the loose variant records the match at +15. Evidence: 0d63ba29 43685-7 v19 line 127 'Advise patients to limit sun exposure while taking Methotrexate Injection.' (found) and line 128 'Advise patients when outdoors to wear a hat and protective clothing and use a broad-spectrum Ultraviolet A (UVA)/Ultraviolet B (UVB) sunscreen and lip balm (SPF ≥30) to help protect against sunburn.'; SrLC HTML: 'lip balm (SPF greater than or equal to 30) to help protect against sunburn'. Same two lines in 0e30eaef v16 and bd287e89 v6.

## Method notes

- Section groups (rule 4), by LOINC code of the section file: BW {34066-1}; C {34070-3}; WP {43685-7}; AR {34084-4}; DI {34073-7, 43685-7}; USP {43684-0, 43685-7, 42228-7, 34080-2, 77290-5, 77291-3, 34081-0, 34082-8, 88828-9, 88829-7}. PCI, PI and MG items were counted but not sampled.
- Presence (rule 5): a kept quote sentence is found when its normalised form is a substring of the normalised section-group text, or when one line or a pair of consecutive lines covers at least 0.90 of its tokens (difflib matching blocks); the quote is present when found sentences hold at least 0.80 of the kept tokens. Section files hold one sentence per line. Lines are the non-empty lines of the section-group files, so a pair of consecutive lines may span a blank line between two paragraphs, and the last line of one file pairs with the first line of the next file of the group. Cited line text is verbatim, indentation included.
- Quote (rule 2) as parsed from the SrLC detail pages: every content div of a section is read (one per subsection); headings are paragraphs in a heading tag or all-bold paragraphs that are numbered, have at most 8 tokens, or carry no sentence punctuation (boxed-warning titles, table captions); sentences do not end after 'e.g.', 'i.e.', 'vs.', 'et al.', 'cf.', 'approx.', 'Fig.', 'Figs.', 'St.', 'No.', 'U.S.', 'Inc.', 'Ltd.', 'Co.', 'Dr.', 'Mr.', 'Mrs.', 'spp.'. After a 'Newly added' or 'Addition of the following' note every paragraph up to the next FDA note is taken as the addition, which can include context the site did not mark; SrLC does not delimit the scope of such a note, and the alternative reading (stop at the first underlined paragraph) would drop the body of a new subsection wherever its heading is underlined, so the wider reading is kept and recorded here.
- Transition (rule 7): a version where the quote is present and the previous version's is not; the transition with the smallest |lag| is chosen, the earlier one on a tie. Categories are tested in the order match, early, late, absent.
- Dates are Published-Date trailers of the US corpus (DailyMed publication), never commit dates and never label text.
- The full evidence (every version walked, every transition with the matched lines, scores and diagnostics) is in reports/srlc-validation.json; the explanations come from sources/srlc-miss-notes.json.
