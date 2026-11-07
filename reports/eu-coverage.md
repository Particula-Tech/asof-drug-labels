# EU corpus coverage

Generated 2026-11-07T09:37:29Z by `python3 -I tools/eu.py build`. Converter eu_qrd 4 (PyMuPDF 1.27.2.2). Scope: Annex I (summary of product characteristics), English annexes only.

## Totals

| Measure | Value |
| --- | ---: |
| Reference products | 206 |
| Products built | 206 |
| In-force products (Union Register annexes) | 206 |
| Current-only products (captured EMA product information) | 0 |
| Products not built | 0 |
| Molecules with in-force history | 206 |
| Commission decisions listed | 4,714 |
| English annexes listed | 4,592 |
| English annexes fetched | 4,592 |
| of which byte-identical to an earlier procedure's annex | 72 |
| Annexes converted and committed | 4,402 |
| Fetched annexes skipped after conversion | 190 |
| Annex entries not fetched (logged in the fetch step) | 90 |
| Annexes with more than one SmPC | 2,649 |
| Commits | 4,402 |
| Corrigendum commits (trailer Corrigendum-Of) | 59 |
| of which published (Procedure-Closed) after their decision date | 59 |
| Decisions (EU number, decision number) with more than one commit | 51 (103 commits) |
| Earliest decision date | 1996-02-14 |
| Product pages last read (not the record end, which is the newest decision date) | 2026-11-03 |

Decisions listed counts procedures on the product pages that carry a Commission decision. English annexes listed counts annex entries whose language list includes English; corrigenda and annexes without an English file are counted under annex entries not fetched. An annex byte-identical to an earlier one is still committed for its own decision, because it restores that text on its decision date.

## Skipped annexes

| Reason | Annexes |
| --- | ---: |
| the annex holds only Annex II or III (the decision did not change the SmPC) | 156 |
| not fetched: corrigendum without an English annex file | 87 |
| Annex I has no section 1 (for example the scientific conclusions of a referral) | 18 |
| Annex I title followed directly by Annex II (the decision did not change the SmPC) | 16 |
| not fetched: no English annex file | 3 |

## Build

| Measure | Value |
| --- | ---: |
| Build time this run (on a warm conversion cache, 0 annexes converted) | 7.5 s |
| Conversion workers | 4 |
| Commits | 4,402 |
| HEAD | `69d4eae5d9a7d8a1860f2c4a1ab7353dd277e31e` |
| .git after fast-import | 68.6 MB |
| .git after `git repack -adf --depth=50 --window=250` | 18.7 MB |
| Files in the working tree | 5,368 |
| Peak memory, build process | 152.4 MB |
| Peak memory, largest child (converter or git) | 102.5 MB |

## Products

One row per reference product (206) is in `reports/eu-coverage.json` under `products`: molecule, product, EU number, history type, earliest decision date (`record_from`), the date its product page was read (`record_to`, not a record end), decisions and English annexes listed, annexes fetched and converted, commits, skip reasons and any problem.
