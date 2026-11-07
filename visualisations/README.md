# Charts

The charts are generated from the corpora by `python3 -I tools/visualise.py all --png`, run from the clone root. Each chart is an SVG with a PNG render beside it, and each has a data file in [`data/`](data/) holding its inputs (corpus HEADs, record window, chart date), the exact selection rule in its `rule` field, every computed number and the generated title. `python3 -I tools/visualise.py all --check` regenerates them into a temporary folder and fails if any SVG or data file differs; nothing in a chart depends on the time of the run. Counts are not repeated on this page, so it cannot drift from the data files: the title of each chart, and the figures in the main [README](../README.md), are read from them.

| File | What it shows |
| --- | --- |
| [`data/01-example-queries.json`](data/01-example-queries.json) | Data only: the answers the three example CLI commands in the README give on this build. |
| [`02-life-of-a-label.png`](02-life-of-a-label.png) | One US label's sections over time, with the span of its boxed warning. The label is picked by the rule in its data file, not by hand. |
| [`03-label-churn.png`](03-label-churn.png) | Section changes per 100 live US labels per year, stacked by section. A label's first version and format conversions are not counted as changes. |
| [`04-most-rewritten.png`](04-most-rewritten.png) | The US labels with the most versions, and the section each changed most. |
| [`05-us-vs-eu-contraindications.png`](05-us-vs-eu-contraindications.png) | Molecules where only the EU SmPC or only the US label lists a contraindication beyond hypersensitivity, on the chart date. |

All dates are DailyMed Published-Dates (what DailyMed showed) or Commission decision dates, never FDA approval dates.

## How the US-versus-EU chart counts

The chart asks a presence question, not a wording one. For each molecule whose EU reference product is comparable to the US brand, and for which `compare_jurisdictions` returns text on both sides, it asks whether the US Contraindications section (`34070-3`) lists a contraindication beyond hypersensitivity on the chart date, and whether the EU SmPC section 4.3 does. Each molecule then falls in one of four cells: only the EU lists one, only the US lists one, both do, or neither does. The data file also counts the molecules whose US section reads only "None.".

Some items are set aside on both sides before the question is asked. Hypersensitivity and allergy items are set aside because every EU SmPC carries the template line on hypersensitivity to the active substance or excipients. Intro lines, cross-references and "None." are not contraindications. A few descriptive sentences that are not contraindications (a post-marketing note in the US Exelon patch label, an excipient note in the EU Zonegran SmPC) are set aside by the committed table [`sources/contraindication-descriptions.json`](../sources/contraindication-descriptions.json); the build checks every entry with `check_citations` and fails if one stops matching, and the data file records what the one-side count would be without the table.

Read the result with its limits in mind. The other label may carry the same risk under Warnings and Precautions or in a boxed warning: the chart reads the Contraindications sections only. It records presence only, so nothing is counted per item. An item that names hypersensitivity and another contraindication in one sentence counts as hypersensitivity.
