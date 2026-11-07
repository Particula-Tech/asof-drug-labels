# Daily update

A GitHub Action (`.github/workflows/daily-update.yml`) runs `tools/daily.py` every day at 06:00 UTC. It appends the label versions DailyMed published since the last run and the Commission decisions the Union Register added to the two corpus repositories, tags both, and commits the bookkeeping in the top repository. Nothing already committed changes: every run adds commits on top of the previous HEAD, and a full rebuild from the pinned sources gives the same commits, hash for hash ([reproduce.md](../reproduce.md), "Commits").

## What one run does

1. **US listing.** `https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?published_date=<record_to>&published_date_comparison=gte`, one request per 100 rows, where `<record_to>` is the newest Published-Date in the US corpus. The boundary day is listed again on purpose; pinned versions are skipped. A page that is not the expected JSON fails the run.
2. **US fetch.** A listed setid with a `sources/us/<setid>.json` and a version newer than its newest pin gets one `/history` request and one zip per new version. The setid and version inside each zip are checked; each new version is pinned with `"added": "<run date>"`.
3. **US sweep, Sundays only** (`--sweep-on-sunday`). `/history` for every current setid, about 4,000 requests and 35 minutes. It catches versions listed with an older date and marks a setid whose `/history` title is null as `status: retired` in `sources/` and `molecules/`; the corpus is not touched.
4. **US append.** `build.py build --append` commits the new versions on top of `main`.
5. **EU capture.** `eu.py capture` reads the EMA medicines and EPAR documents JSON and every reference product's product-information PDF whose URL or EPAR date changed. It adds nothing to the corpus repositories; it writes `sources/eu-capture/<date>.json`, which the top repository commits, as an early signal of what the Union Register will publish.
6. **EU register.** Every reference product's Union Register page is read, about 206 requests. A procedure not yet pinned has its English annex fetched and pinned with `"added": "<run date>"`.
7. **EU append.** `eu.py build --append` commits the new annexes that change Annex I. A corrigendum keeps the date of the decision it corrects, so it can follow newer commits; readers date by the Effective-Date trailer, not commit order ([eu-format.md](eu-format.md)). `sources/eu-products.json` and `molecules/` are then updated.
8. **Tags, manifest, report.** Each corpus HEAD gets an annotated tag `daily/<date>`, even when nothing was appended (tagger: the build identity; date: the run date at 06:00 UTC), with the summary line as message: `Daily update YYYY-MM-DD: N US versions, M EU decisions appended; US record to <date>, EU record to <date>`. `manifest.json` is rewritten (`build_date` moves only for a corpus whose HEAD moved), then the README numbers, then the report `reports/daily/<date>.json` and `.md`.
9. **Push.** Only from `refs/heads/main`. `daily.py push` pushes `main` and the day's tag of both corpus repositories, then commits `TOP_REPO_PATHS` in the top repository (`sources/us`, `sources/eu`, `sources/eu-capture`, `sources/eu-products.json`, `molecules`, `manifest.json`, `README.md`, `reports/daily`, and the `us` and `eu` pointers) with the summary line as message and pushes it, rebasing on `origin/main` and retrying up to 3 times if rejected.

Ids in `sources/removed.json` get no request of any kind. `git describe --tags` in either corpus names the day of the record; the MCP `coverage` tool reports the build date, the DailyMed database date and the tags on each HEAD.

## Limits and stop rules

- One fetcher for all requests: at most 2 per second in total, User-Agent `ParticulaTech-research/0.1 (drug-label history check)`, only `dailymed.nlm.nih.gov`, `ec.europa.eu` and `www.ema.europa.eu`.
- Stops red on HTTP 403 or 429, on a 200 answer of the wrong kind (a challenge or maintenance page), and at the request budget (2,000; on Sundays plus one per current setid).
- Stops red when an append would change history (`would change history; run a full rebuild`) and while a removed id is in HEAD (fix: `tools/purge.py rebuild`, [reproduce.md](../reproduce.md), "Removing a product on request").
- HTTP 5xx and network errors are retried with back-off; the report counts recoveries under `transient_retries`. One failed version or annex is logged, not red.
- A new US version that answers 404 is retried on every run while it is the newest listed; after a newer one is pinned, only by the Sunday sweep, for 30 days from `problem_since`.
- A new EU annex that answers 404 is retried on every run for 30 days from `first_failed`, listed under `pending_annexes`, then under `watch`.
- Versions or annexes logged by the original full build are never retried by the daily job.
- A weekday run makes about 300 requests.
- Not covered: a labeler that moves an application to a new setid.

Running a day again gives the same report apart from the `run` block (mode, requests, seconds, batch).

## Setting it up on a new repo

1. Create the two corpus repositories, push `build/us` and `build/eu` to them (branch `main`), and add them as submodules: `git submodule add <us repo URL> us`, `git submodule add <eu repo URL> eu`. The reader and the job use `us/` and `eu/` once they exist.
2. Create a fine-grained token with Contents read and write on the top repository and both corpus repositories, and store it as the Actions secret `ASOF_PUSH_TOKEN`. `actions/checkout` keeps it for every push; the job's `GITHUB_TOKEN` stays `contents: read`.
3. Keep e-mail for failed workflow runs on; the job opens no issues.
4. Run the workflow by hand with `dry_run` checked, then without. The first run catches up from the corpus record end (the newest Published-Date in `us/`, every register page for `eu/`); a long gap can reach the budget, which `daily.py run --budget` raises.

The runner is `macos-15` (Apple Silicon; moved from `macos-14` on 2026-11-07 after GitHub retired that image), so the hash-pinned `pip install` gets the same PyMuPDF wheel that built `eu/`. Before an EU append, `eu_qrd 4 (PyMuPDF 1.27.2.2)` is checked against HEAD's `Converter-Version`. CI keeps no raw store.

## Rerun a day

From the Actions tab, run the workflow with `date` set. Locally, against the corpus clones:

```bash
ASOF_US_REPO=us ASOF_EU_REPO=eu python3 -I tools/daily.py run --date YYYY-MM-DD            # network
ASOF_US_REPO=us ASOF_EU_REPO=eu python3 -I tools/daily.py run --date YYYY-MM-DD --offline  # replay, no request
ASOF_US_REPO=us ASOF_EU_REPO=eu python3 -I tools/daily.py run --date YYYY-MM-DD --dry-run  # plan only
```

`--offline` replays `raw/daily/<date>/http/`, failures included. `--dry-run` writes the report with `dry_run: true` and pins, commits and tags nothing. A rerun moves an existing day tag, and `push` force-updates that tag.

To check the appended history against a full rebuild:

```bash
python3 -I tools/build.py build --clean --out build/us-check     # compare HEAD with us/
python3 -I tools/eu.py rebuild-check --out eu --workers 4         # empty conversion cache, compared with eu/
```

## Recover raw files

Each run uploads `raw/daily/<date>/` as the artifact `raw-daily-<date>` for 90 days: listing pages, `/history` answers, zips and PDFs (`http/`, with `index.tsv`), converter outputs and the request log. Unpack artifacts into `raw/daily/<date>/`, or download the pinned files again with `build.py fetch-missing` and `eu.py fetch` ([reproduce.md](../reproduce.md), "Commands"). A file whose SHA-256 differs from its pin is reported, not stored.

## Recovery when the corpus push succeeded and the top push did not

If the top push fails after its retries, the corpus remotes are ahead of the submodule pointers, the day's pins were lost with the runner, and the next run stops with `origin/main has commits that main lacks`. On a clone with submodules:

1. Unpack the failed run's artifact `raw-daily-<date>` to `raw/daily/<date>/`.
2. `python3 -I tools/daily.py run --date <date> --offline --recover` replays the day without a request and rewrites the pins, `manifest.json` and report; its commits are the ones already pushed. It stops red unless the replayed `main` equals `origin/main`.
3. `git submodule update --remote us eu`.
4. Commit the `TOP_REPO_PATHS` above with the message `Daily update <date>: <summary line>` and push.
