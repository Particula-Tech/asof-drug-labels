# compare_jurisdictions check

Generated 2026-11-07T09:38:23Z by `python3 -I tools/cli.py compare-check`. US corpus HEAD `52a5f8078918180d0dc32992928b18b8854ad099`, EU corpus HEAD `69d4eae5d9a7d8a1860f2c4a1ab7353dd277e31e`.

Criteria: a molecule works on a date when compare_jurisdictions answers without error; eu.product is not null with history_type in-force or current-only; us.label is not null; at least 6 of the 8 pairs have text on both sides; and every line of only_in_us and only_in_eu for the sections contraindications and warnings, passed to check_citations with the citation the answer gives, comes back with exists, quote_found and in_force_on_as_of all true (the check asks for every line, without the per-side cap of the tool answer). only_in counts are distinct lines after compare_key normalisation; a line repeated within a section is listed and counted once. A molecule whose compared US label is not the brand its comparability note names carries pairing_caveat and is counted under works_with_pairing_caveat as well.

## Summary

| Date | Molecules | Works | Works with pairing caveat | yes | unknown | no | only_in lines checked | Citation failures | Median s | Max s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-30 | 206 | 199 | 11 | 133 of 135 | 51 of 55 | 15 of 16 | 25561 | 0 | 0.076 | 0.192 |
| 2015-01-01 | 206 | 140 | 2 | 100 of 135 | 32 of 55 | 8 of 16 | 15516 | 0 | 0.067 | 0.212 |

The columns yes, unknown and no split the molecules by comparable_to_us of the EU reference product. Times are the three compare_jurisdictions calls per molecule (no section, then each checked section).

## Pairing caveats on 2026-10-30

11 molecules compare a US label other than the brand the comparability note names (11 of them count as working). Each compared label is a different US product; the full text of every caveat is in the JSON.

| Molecule | Label the note names | Why it was not compared | Compared instead |
| --- | --- | --- | --- |
| daptomycin (works) | CUBICIN `34c1faa3` | retired; last published 2023-04-24 | Daptomycin in Sodium Chloride `7ed4d986` |
| docetaxel (works) | TAXOTERE `45e6dce4` | retired; last published 2021-02-04 | Docetaxel `a0ec4bcc` |
| duloxetine (works) | CYMBALTA `2f7d4d67` | retired; last published 2025-08-11 | DRIZALMA SPRINKLE `b41423b8` |
| glycopyrronium (works) | SEEBRI `260e843d` | retired; last published 2021-08-12 | Cuvposa `d200bd44` |
| mannitol (works) | BRONCHITOL `454f092e` | retired; last published 2024-01-23 | Osmitrol `0d914965` |
| palonosetron (works) | ALOXI `1b51c34d` | retired; last published 2020-12-29 | posfrea `13a201ba` |
| ranolazine (works) | RANEXA `739d7705` | retired; last published 2013-04-09 | ASPRUZYO SPRINKLE `dd9f81e4` |
| sevelamer (works) | RENAGEL `5e30120b` | retired; last published 2023-06-23 | Renvela `e6328460` |
| sirolimus (works) | RAPAMUNE `3275b824` | retired; last published 2026-10-26 | FYARRO `0f9bb784` |
| travoprost (works) | TRAVATAN `338e7ff4` | retired; last published 2011-09-21 | Travatan Z `028455e0` |
| zoledronic-acid (works) | ZOMETA `f0cdfa57` | retired; last published 2020-06-29 | zoledronic acid `567578e6` |

## Pairing caveats on 2015-01-01

6 molecules compare a US label other than the brand the comparability note names (2 of them count as working). Each compared label is a different US product; the full text of every caveat is in the JSON.

| Molecule | Label the note names | Why it was not compared | Compared instead |
| --- | --- | --- | --- |
| glucagon (does not work) | BAQSIMI `f1f5df9b` | not yet published (first version 2024-02-02) | GLUCAGEN `f09feb8e` |
| glycopyrronium (works) | SEEBRI `260e843d` | not yet published (first version 2017-11-17) | Cuvposa `d200bd44` |
| hydroxyurea (works) | Siklos `76957c0d` | not yet published (first version 2018-02-12) | DROXIA `740e054b` |
| irinotecan (does not work) | ONIVYDE `c8b58efa` | not yet published (first version 2017-08-14) | Camptosar `e518dfc6` |
| leuprolide (does not work) | CAMCEVI `d7a8761a` | not yet published (first version 2022-03-07) | ELIGARD `fe3228a7` |
| mannitol (does not work) | BRONCHITOL `454f092e` | not yet published (first version 2021-02-01) | Osmitrol `0d914965` |

## Not working on 2026-10-30

7 molecules. Reasons (a molecule can have several):

- DailyMed showed no single-ingredient NDA/BLA label of the molecule on the date in the US corpus: 6
- fewer than 6 of 8 pairs have text on both sides: 1

- decitabine: DailyMed showed no single-ingredient NDA/BLA label of decitabine on 2026-10-30 in the US corpus; DACOGEN acaf6e50 retired, last published 2017-02-10, DACOGEN deb4a13c retired, last published 2024-04-12
- ketoconazole: DailyMed showed no single-ingredient NDA/BLA label of ketoconazole on 2026-10-30 in the US corpus; NIZORAL 090660c1 retired, last published 2017-01-24, EXTINA 36629578 retired, last published 2023-03-13, XOLEGEL 65e5572e retired, last published 2011-01-10, XOLEGEL 855e42bb retired, last published 2023-01-16, NIZORAL aee87a23 retired, last published 2012-07-09, XOLEGEL bc920a25 retired, last published 2011-08-01, EXTINA ceaee038 retired, last published 2019-01-25, EXTINA eb9ee667 retired, last published 2013-06-28, NIZORAL efbc6388 retired, last published 2022-02-07
- levofloxacin: DailyMed showed no single-ingredient NDA/BLA label of levofloxacin on 2026-10-30 in the US corpus; IQUIX 067ea8ec retired, last published 2017-07-31, QUIXIN 09b3a912 retired, last published 2017-07-27, LEVAQUIN 4aa2bfa2 retired, last published 2016-12-06, LEVAQUIN a1f01e8e retired, last published 2021-01-11, LEVAQUIN d8034b31 retired, last published 2012-06-05
- memantine: DailyMed showed no single-ingredient NDA/BLA label of memantine on 2026-10-30 in the US corpus; NAMENDA 710f523f retired, last published 2026-01-02, NAMENDA b9f27baf retired, last published 2024-12-20
- mercaptopurine: PURIXAN 9fd27952: 3 of 8 pairs have text on both sides
- pramipexole: DailyMed showed no single-ingredient NDA/BLA label of pramipexole on 2026-10-30 in the US corpus; MIRAPEX 688fa4d7 retired, last published 2021-09-22, MIRAPEX e2902ed1 retired, last published 2025-02-24
- telmisartan: DailyMed showed no single-ingredient NDA/BLA label of telmisartan on 2026-10-30 in the US corpus; MICARDIS 36d69907 retired, last published 2025-06-09, MICARDIS 36d6f15a retired, last published 2025-06-09, MICARDIS cfb9309f retired, last published 2025-05-23, MICARDIS dcfc733c retired, last published 2013-05-20

## Not working on 2015-01-01

66 molecules. Reasons (a molecule can have several):

- EU record starts after the date: 53
- DailyMed showed no single-ingredient NDA/BLA label of the molecule on the date in the US corpus: 33
- fewer than 6 of 8 pairs have text on both sides: 3

## Molecules

One entry per molecule (206) is in `compare-check.json` under `molecules`: the EU product, comparable_to_us, the US label compared, pairs with text on both sides, only_in lines checked, citation failures and the verdict with its reasons on each date.
