# EU corpus format

This describes the EU corpus repository (`eu/`, written by `python3 -I tools/eu.py build`) for anyone reading it with git or their own code. `tools/labels.py`, `tools/cli.py` and `tools/mcp_server.py` read it by these rules. Counts for the current build are in `manifest.json` and `reports/eu-coverage.json`.

## Repository

One branch, `main`, with a linear history written by `git fast-import`; no merges. The daily job tags `main` as `daily/YYYY-MM-DD` after each run ([daily-update.md](daily-update.md)). Author, committer, dates and trees depend only on the pinned inputs (`sources/eu-products.json`, `sources/eu/*.json`, the annex PDFs by SHA-256 and the converter version), so a rebuild gives the same commit hashes; `tools/eu.py rebuild-check` proves it by building again from an empty conversion cache.

Commits of all products are interleaved in one history, ordered by (`added` batch or empty, decision date, procedure id as a number, product folder). Entries pinned by the initial build have no `added` and come first, in date order; each daily batch follows in its own date order. A corrigendum appended by a daily run therefore carries an earlier date than commits before it. The committer is `asof-drug-labels build <build@labels.invalid>`; author and committer dates are the decision date at 00:00:00 +0000.

## Paths

```
corpus.json                                   corpus, scope, layout, sources; written once in the first commit
README.md                                     purpose, layout, trailers and the fidelity rule; written once
NOTICE.md                                     attribution, licence terms, contact, trade marks, warranty; written once
<inn>/<EMEA-H-C-nnnnnn>/product.json          product constants
<inn>/<EMEA-H-C-nnnnnn>/smpc-<n>-<slug>.md    one file per SmPC section of Annex I
```

`<inn>` is the molecule key of `molecules/<inn>.json`, the same folder name as in the US corpus (`insulin-lispro`). `<EMEA-H-C-nnnnnn>` is the EMA product number with hyphens for slashes. Each molecule folder holds one product, the reference product; other central products are recorded as alternates in `sources/eu-products.json` and not built.

Section file names come from a fixed table (`SECTIONS` in `tools/eu_qrd.py`), never from the printed heading, so a section keeps its name across QRD template versions:

```
smpc-0-additional-monitoring        the black-triangle statement, wherever printed; heading "# 0 Additional monitoring"
smpc-0-front-matter                 text before the first section 1 (rare); heading "# 0 Front matter"
smpc-1-name-of-the-medicinal-product
smpc-2-qualitative-and-quantitative-composition
smpc-3-pharmaceutical-form
smpc-4-clinical-particulars         only when text sits under the group heading (rare)
smpc-4.1-therapeutic-indications
smpc-4.2-posology-and-method-of-administration
smpc-4.3-contraindications
smpc-4.4-special-warnings-and-precautions-for-use
smpc-4.5-interaction-with-other-medicinal-products-and-other-forms-of-interaction
smpc-4.6-fertility-pregnancy-and-lactation
smpc-4.7-effects-on-ability-to-drive-and-use-machines
smpc-4.8-undesirable-effects
smpc-4.9-overdose
smpc-5-pharmacological-properties   only when text sits under the group heading (rare)
smpc-5.1-pharmacodynamic-properties
smpc-5.2-pharmacokinetic-properties
smpc-5.3-preclinical-safety-data
smpc-6-pharmaceutical-particulars   only when text sits under the group heading (rare)
smpc-6.1-list-of-excipients
smpc-6.2-incompatibilities
smpc-6.3-shelf-life
smpc-6.4-special-precautions-for-storage
smpc-6.5-nature-and-contents-of-container
smpc-6.6-special-precautions-for-disposal-and-other-handling
smpc-7-marketing-authorisation-holder
smpc-8-marketing-authorisation-numbers
smpc-9-date-of-first-authorisation-renewal-of-the-authorisation
smpc-10-date-of-revision-of-the-text
smpc-11-dosimetry                   radiopharmaceuticals only
smpc-12-instructions-for-preparation-of-radiopharmaceuticals
```

The tools name a section by its QRD number, the part between `smpc-` and the next hyphen (`4.3`). `compare_jurisdictions` pairs 4.1 with US 34067-9 (indications), 4.2 with 34068-7 (dosage), 4.3 with 34070-3 (contraindications), 4.4 with 43685-7 (warnings and precautions), 4.5 with 34073-7 (drug interactions), 4.6 with the pregnancy and lactation sections, 4.8 with 34084-4 (adverse reactions) and 4.9 with 34088-5 (overdosage).

## Section file content

The first line is the printed heading with a normalised number (`# 4.3 Contraindications`), then a blank line, then the text. Text is one sentence per line (the sentence rules of `tools/spl.py`, plus no break after `e.g.`, `i.e.`, `vs.`, `approx.`, `cf.`, `incl.` or after a number followed by a dash), paragraphs separated by a blank line, list items as `- ` lines (nested ones indented two spaces), and unambiguous tables as pipe tables. Figures and unclear grids stay as line text, one visual line per line.

A word broken at a line end with a hyphen is joined without a space (`KEYNOTE-` + `811` gives `KEYNOTE-811`), unless the annex itself uses the hyphen-final form as a word (`HER2- negative`). Subscripts and superscripts stay in reading order inside their word (`Cmax`, `AUC0-24`); a superscript footnote mark is glued to its word (`p-valuea`), a known limit.

Tables come from full cell borders (PyMuPDF `find_tables`) or from horizontal rules of one common width (at least three rules, two to eight columns found from vertical white strips, no other drawings in the area). A table cut by a page break continues across it. An adverse-reaction row that stacks several frequency groups is split into one pipe row per group when the blank bands line up in at least two cells; otherwise the groups are joined by spaces. Two-dimensional formulas stay as line text. Running page headers and footers are dropped with the page numbers.

When one annex holds several SmPCs (strengths or forms), each section file holds the first SmPC's text in full, then, for every later SmPC, a line `[<product name>]` followed only by the sentences or whole tables not already in the file (compared after lowercasing and normalising list markers, white space, hyphen spacing and trailing punctuation). Repeated names become `[<name> (SmPC 2)]` and so on. If no later SmPC adds anything to a section, the file holds the first SmPC's text only; otherwise every later SmPC gets its line, and one with nothing new gets `(all sentences already above)`. Every sentence of any SmPC of the annex is in the file, and a line under `[...]` belongs to that SmPC only.

## product.json

Written with the product's first commit and identical in every later one; keys sorted. It holds only constants: `corpus` (`eu`), `name` (EMA medicines JSON), `register_name` (Union Register), `ema_product_number`, `eu_number`, `molecule` (equal to the top folder), `molecule_name`, `active_substance`, `marketing_authorisation_date`, `history_type` (`in-force`: Union Register annexes; `current-only`: one captured EMA product-information PDF), `source` (`ec-union-register` or `ema-product-information`), `source_h_page_url`, `medicine_url` and `scope`.

Facts that change over time live outside the corpus: the holder, `comparable_to_us`, `comparability_note` and alternates in `molecules/<inn>.json` (`eu[]`) and `sources/eu-products.json`; version counts and the record window in the commits and `manifest.json`.

## Commit message

In-force commit, one per procedure with an English Annex I that converted:

```
Author: Annex EU/1/15/1024 132284 <EMEA-H-C-003820.132284@labels.invalid>
Date:   2015-07-17 00:00:00 +0000

Keytruda — Commission decision (2015) 5144

Source-Id: ec-union-register:EU/1/15/1024:132284
Source-Version: 1
Effective-Date: 2015-07-17
Decision-Number: (2015) 5144
Decision-Date: 2015-07-17
EMA-Procedure: EMEA/H/C/3820
Procedure-Type: Centralised - Authorisation
Source-URL: https://ec.europa.eu/health/documents/community-register/2015/20150717132284/anx_132284_en.pdf
Source-SHA256: d12e8d4818da384129d0f23164735bb09838bc64f362d48b8e1aa6e072cf232f
Converter-Version: eu_qrd 4 (PyMuPDF 1.27.2.2)
History: in-force
Text-Fidelity: wording intended to be unchanged; automated conversion from PDF can introduce errors; the file at Source-URL with Source-SHA256 is authoritative and the Annex I PDF in the Union Register prevails
```

- `Source-Id`: `ec-union-register:<EU number>:<procedure id>`; the procedure id is the register's id and the folder name of the annex file.
- `Source-Version`: the 1-based sequence of the product's commits, in commit order.
- `Effective-Date`: the Commission decision date, authoritative for dating. `Decision-Date` repeats it.
- `Decision-Number`: the register's string (`(2015) 5144` is decision C(2015) 5144). `EMA-Procedure`, `Procedure-Type`: the register's procedure number and type.
- `Procedure-Closed` (when the register gives one): the date the register closed the procedure; for a corrigendum, when the corrected annex was published, which can be months after its Effective-Date.
- `Corrigendum-Of` on a corrigendum or rectificative decision: the procedure it corrects (the latest earlier procedure of the product with the same decision number, else the same decision date, else `not identified`).
- `Annex-Identical-To`: the annex file is byte-identical to that earlier procedure's annex; that text is restored on this decision's date.
- `Text-Fidelity`: the same on every commit (`TRAILER_EU` in `tools/notices.py`).

A corrigendum carries the decision date of the decision it corrects, so it shares that `Effective-Date` and, on the same date, is the later commit. Several procedures can share one (EU number, decision number); each gets its own commit. A decision that reprints Annex I unchanged still gets a commit that changes no file, so `git log -- <path>` skips it while `git log --grep` on the Source-Id does not. Counts of corrigenda and of decisions with several commits are in `reports/eu-coverage.json`.

Current-only commit (fallback for a product whose annexes all failed to convert; no product needs it in the current build):

```
Author: Product information EMEA/H/C/006139 2026-11-01 <EMEA-H-C-006139.pi-2026-11-01@labels.invalid>

<Name> — EMA product information captured 2026-11-01

Source-Id: ema-product-information:EMEA/H/C/006139:2026-11-01
Source-Version: 1
Effective-Date: 2026-11-01
Captured-Date: 2026-11-01
EMA-Last-Updated: <last_updated_date from the EMA medicines JSON>
Source-URL: <the product-information PDF URL>
Source-SHA256: <its SHA-256>
Converter-Version: eu_qrd 4 (PyMuPDF 1.27.2.2)
History: current-only
Text-Fidelity: <as above>
```

## Reading "on date X"

For a product folder `P = <inn>/<EMEA-H-C-nnnnnn>`:

1. List the product's commits with their dates: `git log --format='%H %(trailers:key=Effective-Date,valueonly,separator=)' main -- P/`, or, to include commits that changed no file, `--grep='^Source-Id: ec-union-register:<EU number>:'` instead of the path.
2. The text in force on X is the commit with the greatest `Effective-Date` on or before X; on a tie, the later commit. Do not take the first match in `git log` order: a corrigendum appended later sits above newer decisions in the log. For the same reason `git rev-list --before` is not a substitute; the trailer is the authority. Read files with `git show <commit>:P/<section>.md`.
3. Before the product's first `Effective-Date` there is no EU text. After the corpus record end the text is the latest known one; say where the record ends.

This returns the corrected text from the decision date on, including the days before the corrigendum was published. To answer "what was published on X" instead, step back from a selected commit with `Corrigendum-Of` whose `Procedure-Closed` is after X to the procedure it names. The tools add a `corrigendum_note` to such answers.

`git blame -M -C main -- P/<section>.md` credits each sentence to the decision that first wrote it. Each decision replaces Annex I in full; git's line diff keeps unchanged sentences credited to their first decision.

## Comparability with the US label

The reference product is chosen by the rule in `tools/eu.py` (`SELECTION_RULE`, summarised in [reproduce.md](../reproduce.md), "Selection rules and manual decisions"), with two references set by hand: duloxetine is Cymbalta, not the incontinence product Yentreve, and umeclidinium is Incruse Ellipta, not its duplicate Rolufta Ellipta. Some references still differ from the US innovator label: a non-prescription product (Nexium Control, Pantozol Control), another formulation (Protopic ointment, Ozurdex implant, Abraxane, Caelyx) or a duplicate authorisation under a second name (Iscover for Plavix, Azomyr for Aerius). `comparable_to_us` records this in `molecules/<inn>.json` and `sources/eu-products.json`:

- `no`, or `yes` with a hand-written note, for the products in `REFERENCE_CAVEATS` in `tools/eu.py`;
- `yes` when the first word of the EU name equals the first word of a single-ingredient US NDA or BLA brand of the molecule;
- `unknown` otherwise (often a different brand for the same product, such as Forxiga and Farxiga).

`compare_jurisdictions` shows the note whenever the value is not `yes`. Counts are in `sources/eu-products.json` under `counts.references_comparable_to_us`.

## Products per molecule

- `git ls-tree -d --name-only main` lists the molecule folders; `git ls-tree --name-only main <inn>/` the product folder of one.
- `sources/eu-products.json`, key `molecules.<inn>`: `reference` (the built product, with `dir`, `ema_product_number`, `eu_number`, `selected_by`, `history_type`, `record_from`, `record_to`, `build_versions`), `alternates` (not built), `combinations` (excluded), `near_misses_not_matched` (single-substance products whose INN differs by a salt or hydrate word, a US adopted name or another word, with `kind` `salt`, `alias` or `variant`) and `no_reference_reason` for molecules without a reference. `selected_by` is `exact-inn`, `salt-alias-v2` or `override`.
- `find_label` matches the EMA number, the EU number or the name.
- Annexes belong to the product whose Union Register page (h-page) lists them, not to the EMA number of each procedure. The fetch checks that the product's own EMA number occurs among the h-page's procedure numbers, not that it is the only one: some h-pages list procedures under another product's number (Iscover lists Plavix's `EMEA/H/C/174/...`), and those annexes belong to the h-page's product.
