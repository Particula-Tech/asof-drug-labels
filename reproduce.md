# Reproducing the build

`tools/build.py` and `tools/eu.py` write the corpus repositories from the pinned inputs in `sources/` and `molecules/`. The same inputs give the same commits, hash for hash, including the commits the daily job appended ([docs/daily-update.md](docs/daily-update.md)). Builds go to `build/us` and `build/eu`; compare their HEADs with the submodules `us/` and `eu/`.

## Requirements

- Python 3.12 or newer, standard library only (tested with 3.14). Run every tool as `python3 -I`.
- For EU PDF conversion only, `.venv-eu` with PyMuPDF 1.27.2.2: `python3 -m venv .venv-eu && .venv-eu/bin/pip install --require-hashes --only-binary :all: -r tools/requirements-eu.txt` (`ASOF_EU_PYTHON` names another interpreter). The reader, CLI and MCP server never load it.
- git 2.28 or newer (tested with 2.51.0). The build ignores user and system git config, hooks and signing.
- Google Chrome, only for the PNG copies of the visuals.
- About 27 GB for the raw store (`raw/`, gitignored; `ASOF_RAW` moves it).

## Commands

```sh
# 1. Raw files for the committed pins (network, 2 requests/s shared by all workers, resumable)
python3 -I tools/build.py fetch-missing --workers 4    # US zips, kept only if the SHA-256 matches the pin
python3 -I tools/eu.py fetch --workers 4               # EU register pages and annex PDFs, same rule
git checkout -- sources/eu                             # drop pins fetch added beyond the committed history

# 2. Build (offline)
python3 -I tools/build.py build --clean --workers 4
python3 -I tools/eu.py build --clean --workers 4       # also reports/eu-coverage.json and .md
git -C build/us rev-parse HEAD; git -C us rev-parse HEAD   # equal; same for eu
python3 -I tools/eu.py rebuild-check --workers 4       # optional: rebuild EU from an empty conversion cache

# 3. Regenerate (offline)
python3 -I tools/eu.py crosswalk
python3 -I tools/build.py coverage && python3 -I tools/eu.py coverage
python3 -I tools/cli.py compare-check --date <US record end> --info-date 2015-01-01 --out reports/compare-check.json
python3 -I tools/manifest.py write --build-date <YYYY-MM-DD> --dailymed-db-published '<listing db_published_date>'
python3 -I tools/visualise.py all --png
python3 -I tools/readme_numbers.py --write

# 4. Checks
python3 -I tools/eu.py crosswalk --check
python3 -I tools/manifest.py --check
python3 -I tools/visualise.py all --check              # exit 1 on any byte difference
python3 -I tools/readme_numbers.py --check
python3 -I tools/purge.py check
cd tools && python3 -I -m unittest discover -s tests   # unit tests, no network
ASOF_FULL_REPO=../build/us ASOF_FULL_EU_REPO=../build/eu python3 -I -m unittest discover -s tests -k Full
```

`cli.py`, `visualise.py` and `manifest.py` read `us/` and `eu/` when checked out, else `build/us` and `build/eu`; with equal HEADs the outputs are the same. `--repo`/`--us-repo`, `--eu-repo`, `ASOF_US_REPO` and `ASOF_EU_REPO` choose others. `git -C build/us repack -adf --depth=50 --window=250` shrinks a fresh build about threefold without changing a hash.

SrLC validation: `tools/srlc.py fetch` (network, 1 request/s, into `raw/srlc/`), then `parse`, `select` (`reports/srlc-population.json`, `sources/srlc-sample.json`) and `check` (`reports/srlc-validation.json` and `.md`), all offline.

To renew the pins instead (this changes the history): `select_molecules.py` (inputs under `raw/m2/`, SHA-256 in `molecules/selection.json`), `archive.py discover` and `apply`, `build.py fetch-all`, `eu.py discover` and `eu.py fetch`, then build.

## Pinned inputs

- `sources/us/<setid>.json`: every US version used, with published date, URL and the SHA-256 of the raw zip (`raw/sha256/<sha256>.zip`). The build refuses a raw file whose hash differs.
- `sources/eu/<EMEA-H-C-nnnnnn>.json`: every Union Register procedure of a reference product with its annex URL and SHA-256. `sources/eu-products.json`: the EU selection and the pinned EMA and register inputs.
- `molecules/*.json` and `molecules/selection.json`: the crosswalk, the selection rules and their inputs.
- `sources/retired-setids.json`: the retired-setid rules and one decision per candidate setid; discovery pages are cached under `raw/archive/`, so `apply` reruns offline.
- `added` on a pinned version or annex is the daily batch that pinned it (`<date>.2` for a second run that day); entries without it sort first.

## Converter

`tools/spl.py`, `CONVERTER_VERSION = "2"`, recorded in every US commit's `Converter-Version`; any change to the output bumps it. The EU converter (`tools/eu_qrd.py`, `eu_qrd 4 (PyMuPDF 1.27.2.2)`) is described in [docs/eu-format.md](docs/eu-format.md).

| Rule | Why |
| --- | --- |
| One file per top-level section, `{LOINC code}-{slug}.md` | Titles vary between versions, codes do not |
| Old-format WARNINGS (`34071-1`) and PRECAUTIONS (`42232-9`) map onto `43685-7` | One file across the switch to the PLR format |
| A boxed warning (`34066-1`) gets its own file wherever it sits | Old-format labels nest it inside WARNINGS |
| Sections sharing a code are joined in document order; uncoded sections count as `42229-5` | Package-label panels repeat 1 to 19 times; numbered files would appear and vanish |
| Product data elements (`48780-1`), Highlights excerpts and images are skipped | Not label text |
| One sentence per line; pipe tables; list markers kept | Line blame approximates sentence blame |
| Whitespace-only text with a line break between tags is dropped; spaces before `) ] , . ; :` and after `( [` removed; split heading numbers (`5. 3`) joined | XML pretty-printing; DailyMed's renderer drops it too (`Ag` + `e` reads `Age`) |
| Copyright notices are kept character for character; whitespace rules never insert a space | `CopyrightNoticeTest` (`test_spl.py`, `test_eu_qrd.py`) and `FullCorpusNoticesTest` (`known_notices.json`) fail if one is stripped |
| EU: lines repeated as running headers or footers are dropped | Page furniture; a notice printed only in a running footer is therefore lost |

## Commits

One US commit per label version, in order (`added`, published date, setid, version), so a full build and a chain of daily appends agree. Author `Label <setid> v<n> <setid.v<n>@labels.invalid>`, committer `asof-drug-labels build <build@labels.invalid>`, both dated at the published date, 00:00 UTC. Trailers: `Source-Id`, `Source-Version`, `Published-Date`, `Effective-Time`, `Application-Numbers`, `Source-URL`, `Source-SHA256`, `Label-Format`, `Converter-Version`, `History`, `Format-Conversion` where the format changes, and `Text-Fidelity` last (`tools/notices.py`). Unchanged versions still get a commit. `git fast-import` writes them, with the same hashes as one `git commit` per version.

"On date X" is the newest version with `Published-Date` on or before X. `Effective-Time` is never used for dating; it is not always in order (SYNTHROID v1531 is effective 2020-07-01, before v1529's 2020-07-22). EU commits: [docs/eu-format.md](docs/eu-format.md).

`README.md`, `NOTICE.md` and `corpus.json` are written in the first commit and never changed; neither corpus has a `LICENSE` file. Every commit must be a label version or a decision, so changing a root file takes a full rebuild, which gives every commit a new hash, and a force-push, as a removal does. `cli.py blame` runs `git blame -M -C`, so a moved sentence stays credited to the version that first wrote it.

## Selection rules and manual decisions

- US molecules: 250 by breadth of labels, 250 by innovator approvals; labels live on DailyMed only, repackagers and relabelers excluded, at most 10 NDA/BLA and 6 generic setids each (`molecules/selection.json`). Apixaban, dupilumab and osimertinib were added by hand (`--add`, `selected_by: manual`, ranks 501 to 503) under the same rules.
- A combination label (Caduet, Epclusa) is filed once, under the first of its molecules the fetch reached, and listed under every ingredient in `molecules/*.json`; velpatasvir has no folder. Look labels up through `molecules/*.json`.
- Retired setids (`tools/archive.py`): kept when not live today, not from a repackager (bracketed source NDC, `REPACK`/`RELABEL`, openFDA's `is_original_packager` share, or a known name), and carrying one of the molecule's own NDA/BLA numbers. One with no application number is kept only when its brand or bare brand stem is the molecule's and its title names the molecule. Search terms are first brand words of 4 or more letters; terms over 150 result pages are skipped.
- EU reference (`tools/eu.py`, `salt-alias-v2 + overrides`): the human, authorised, non-generic, non-biosimilar central product whose INN field is exactly the molecule; failing that, one whose INN adds salt or hydrate words or is the US adopted name (Reyataz for atazanavir); earliest authorisation, then lowest EMA number. `REFERENCE_OVERRIDES` sets duloxetine to Cymbalta (EMEA/H/C/000572, not Yentreve) and umeclidinium to Incruse Ellipta (EMEA/H/C/002809, not its duplicate Rolufta Ellipta). Other products are alternates, not built; combinations are excluded. `REFERENCE_CAVEATS` sets `comparable_to_us` for references that are not the US label's counterpart.
- EU dating: `Effective-Date` is the Commission decision date; a corrigendum carries the date of the decision it corrects.
- Compare: the US single-ingredient NDA/BLA label in force on the date (never a retired one) against the EU reference, by topic (SmPC 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.8, 4.9).
- Visual 5: two contraindication lines are set aside by hand in `sources/contraindication-descriptions.json` (US Exelon patch, EU Zonegran); the build fails if either stops matching exactly one item.
- SrLC sample: rules and constants were fixed in `sources/srlc-sample.json` before matching and the 50 items drawn by hash order; one later revision (parse version 2) is recorded under `revisions`. `sources/srlc-miss-notes.json` explains misses and never changes an outcome. Each month is searched separately because a search lists a drug only at its latest change.
- Known readings: the 5 in `tools/tests/known_readings.json` were checked on 2026-10-31 against DailyMed's rendered pages (`verified_by_hand: false`).

## Removing a product on request

A rights holder, a named person or a correction can require a US label (setid) or EU product (`EMEA-H-C-nnnnnn`) to leave the corpora. Requests go to legal@particula.tech; the published promise is an acknowledgement within 5 business days and, for a valid request, removal from the current tree within 10 business days. `sources/removed.json` holds one entry per id: corpus, reason (`rights-holder-request`, `personal-data-request` or `other`), who asked (a company, or `named individual`; no personal data), request and removal dates. Builds and appends exclude listed ids, and the daily job requests nothing for them and stops red while one is in HEAD. Pins and raw files stay; they hold URLs and hashes, not text.

1. `python3 -I tools/purge.py add --corpus <us|eu> --id <id> --reason <reason> --requested-by "<company or named individual>" --request-date YYYY-MM-DD`
2. `python3 -I tools/purge.py rebuild --corpus <us|eu|all> --workers 4`: full build without the removed ids, then crosswalk, check, `manifest.py write` and `readme_numbers.py --write`. It keeps the `origin` remote of `build/us` or `build/eu`; add one first if the folder has none (`git -C build/us remote add origin <us repo URL>`).
3. `python3 -I tools/purge.py check`: exit 1 if a removed id is in any ref, the tree at `main`, a `Source-Id` trailer (US) or commit author (EU), or a `daily/*` tag is unreachable from `main`.
4. `python3 -I tools/visualise.py all --png`, `python3 -I tools/cli.py compare-check --date <record end> --info-date 2015-01-01 --out reports/compare-check.json`, `python3 -I tools/readme_numbers.py --write`.
5. By hand: `git -C build/<corpus> push --force origin main` and `git -C build/<corpus> push origin --prune '+refs/tags/*:refs/tags/*'`; `git submodule update --remote us eu`; commit `sources/removed.json`, `molecules/`, `manifest.json`, `README.md`, `reports/`, `visualisations/` and the pointers.
6. Ask GitHub Support to clear cached views of the old commits (forks may keep them), and replace any known reading that cites the removed label.

Commits before the removed label's first version keep their hashes; later ones get new hashes with unchanged dates, messages and trees otherwise. No `git filter-repo` is needed: the deterministic build gives the history without the id, and the check proves it.

## Measurements

| What | Value |
| --- | --- |
| US build | about 80 to 90 s with 4 workers (Apple Silicon laptop); a second build gives the same HEAD |
| EU build | about 30 min (9 workers) to 55 min (4 workers) from an empty conversion cache; 6 to 15 s warm |
| US raw fetch | one request per pinned version at 2 requests/s, about 6.5 h |
| SrLC fetch | 1,647 requests: 131 search pages, 1,513 detail pages, 3 start pages for the session cookie |
| Build of 2026-11-03 | raw US zips 22 GB, `raw/eu` 3.6 GB; repacked `.git` US 173 MB, EU 18 MB |
