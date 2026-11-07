# Disclaimer

**Not advice.** This repository, its MCP server and its CLI are not medical, legal or regulatory advice. They are for regulatory-history research and engineering use, not for diagnosis, treatment decisions, prescribing, or deciding what a label requires. Check the official source before acting on any label text.

**Not an official source.** The official sources are DailyMed (U.S. National Library of Medicine) for US labels, and the European Commission's Union Register of medicinal products and the European Medicines Agency for EU product information. Every answer from the MCP server and the CLI links the DailyMed file or the Union Register annex it read, so the official page can be checked.

**Dates.** US text is what DailyMed showed on a date, dated by DailyMed's Published-Date and never by an FDA approval date; EU text is the English Annex I of the Commission decision in force on a date; each corpus ends at a record end stated in `manifest.json`. The rules are in [How dates work](README.md#how-dates-work).

**Fidelity.** Wording is intended to be unchanged, but automated conversion from SPL XML and PDF can introduce errors (tables, superscripts, column order, dropped lines). The source file each version cites, pinned by URL and SHA-256, is authoritative; where this copy differs from the official DailyMed document or the Annex I PDF published by the European Commission, the official text prevails.

**Copyright and licences.** Particula Tech claims no copyright in any label or SmPC text and does not license it; copyright stays with its authors and holders. The terms each corpus is reproduced under are in its `NOTICE.md` ([US](https://github.com/Particula-Tech/asof-drug-labels-us/blob/HEAD/NOTICE.md), [EU](https://github.com/Particula-Tech/asof-drug-labels-eu/blob/HEAD/NOTICE.md)) and summarised in [Sources and licences](README.md#sources-and-licences). The code is under the [Apache License 2.0](LICENSE), with attribution in [`NOTICE`](NOTICE).

**Personal data.** The corpus holds published product labels only and contains no patient data. Label text occasionally names clinical investigators or other individuals exactly as the source authorities published them; Particula Tech processes those names on the basis of legitimate interest in an already-public professional context.

**Rights holders and corrections.** If you hold rights in material reproduced here and object to its inclusion, if you are a person named in label text and want that removed, or if you find a conversion error, email legal@particula.tech with the product, setid or file path. We acknowledge within 5 business days and, for a valid request, remove the material from the current tree within 10 business days and from the published history at the next rebuild. Conversion errors can also be reported with the Correction issue template.

**Independence and trade marks.** Independent project by Particula Tech. Product names are trade marks of their respective owners and are used only to identify the medicines whose labels are reproduced. No affiliation with, or endorsement by, any manufacturer, FDA, NLM, EMA or the European Commission is claimed. No logos, pack shots or brand typography are used.

**No warranty.** The corpus, the MCP server and the CLI are provided 'as is', without warranty of any kind, express or implied, including accuracy, completeness, currency, merchantability and fitness for a particular purpose. Particula Tech is not liable for any loss arising from reliance on them.
