"""Every attribution, licence, contact and warranty text of the project, written once (standard library only).

build.py and eu.py write corpus_readme() and corpus_notice() into the first commit of each corpus repository;
manifest.py, labels.py, mcp_server.py, cli.py, visualise.py and purge.py import the constants. The texts hold
no counts and no dates other than the Commission's copyright years, so they never change with a build; a
change to the corpus root files is made only by a full rebuild (reproduce.md, 'Commits'). The SCOPE_* templates
are filled by labels.py at run time with counts read from the repo, so they too hold no numbers themselves.
"""
from __future__ import annotations

CONTACT = "legal@particula.tech"
CODE_REPO = "https://github.com/Particula-Tech/asof-drug-labels"

RIGHTS_HOLDERS = (
    "Rights holders and corrections: Particula Tech claims no copyright in any label or SmPC text. If you hold "
    "rights in material reproduced here and object to its inclusion, if you are a person named in label text and "
    "want that removed, or if you find a conversion error, email legal@particula.tech with the product, set-id or "
    "file path. We acknowledge within 5 business days and, for a valid request, remove the material from the "
    "current tree within 10 business days and from the published history at the next rebuild.")

INDEPENDENCE = (
    "Independent project by Particula Tech. Product names are trade marks of their respective owners and are used "
    "only to identify the medicines whose labels are reproduced. No affiliation with, or endorsement by, any "
    "manufacturer, FDA, NLM, EMA or the European Commission is claimed. No logos, pack shots or brand typography "
    "are used.")

NO_WARRANTY = (
    "The corpus, the MCP server and the CLI are provided 'as is', without warranty of any kind, express or "
    "implied, including accuracy, completeness, currency, merchantability and fitness for a particular purpose. "
    "Particula Tech is not liable for any loss arising from reliance on them. Not intended for diagnosis, "
    "treatment decisions or prescribing; for regulatory-history research and engineering use. Check the official "
    "source before acting on any label text.")

FIDELITY = (
    "Wording is intended to be unchanged; automated conversion from SPL XML and PDF can introduce errors (tables, "
    "superscripts, column order, dropped lines). The linked source file, pinned by URL and SHA-256, is "
    "authoritative; where this copy differs from the official DailyMed document or the Annex I PDF published by "
    "the European Commission, the official text prevails.")

FIDELITY_US = (
    "Wording is intended to be unchanged; automated conversion from SPL XML can introduce errors (tables, "
    "superscripts, column order, dropped lines). The source file pinned by URL and SHA-256 in each commit is "
    "authoritative; where this copy differs from the official DailyMed document, the official text prevails.")

FIDELITY_EU = (
    "Wording is intended to be unchanged; automated conversion from PDF can introduce errors (tables, "
    "superscripts, column order, dropped lines). The source file pinned by URL and SHA-256 in each commit is "
    "authoritative; where this copy differs from the Annex I PDF published by the European Commission in the "
    "Union Register, the official text prevails.")

TRAILER_US = ("Text-Fidelity: wording intended to be unchanged; automated conversion from SPL XML can introduce "
              "errors; the file at Source-URL with Source-SHA256 is authoritative and the official DailyMed document "
              "prevails")
TRAILER_EU = ("Text-Fidelity: wording intended to be unchanged; automated conversion from PDF can introduce errors; "
              "the file at Source-URL with Source-SHA256 is authoritative and the Annex I PDF in the Union Register "
              "prevails")

NLM_TERMS_URL = "https://www.nlm.nih.gov/databases/download/terms_and_conditions.html"
CC_BY_URL = "https://creativecommons.org/licenses/by/4.0/"

US_LICENCE = (
    "Courtesy of the U.S. National Library of Medicine (DailyMed). NLM does not endorse this project. This record "
    "may not reflect the most current or accurate data available from NLM; the record ends on the date in "
    "manifest.json of the top repository; check DailyMed for the current label. Particula Tech does not license "
    "the label text and claims no copyright in it. Label text is reproduced under the NLM Terms and Conditions "
    f"({NLM_TERMS_URL}); copyright in label text stays with its authors and holders, whose notices are preserved "
    "in the text. The software that builds this repository is Apache-2.0 and lives at "
    "github.com/Particula-Tech/asof-drug-labels; that licence does not cover the label text. US label text is not "
    "public domain, CC0 or openly licensed.")

EU_LICENCE = (
    "English Annex I (summary of product characteristics) of Commission decisions, reproduced from the European "
    "Commission's Union Register of medicinal products, with product metadata from the European Medicines Agency. "
    "© European Union, 1995-2026. The Commission's own content (decisions, register pages, open dataset) is "
    "reused under Commission Decision 2011/833/EU and the Creative Commons Attribution 4.0 International licence "
    f"the Commission applies to it ({CC_BY_URL}). EMA content is reused with acknowledgement under the EMA legal "
    "notice. The summary of product characteristics is drafted by the marketing authorisation holder and adopted "
    "by the Commission as Annex I of a legally binding decision that Member States must make publicly available "
    "(Article 21(3) of Directive 2001/83/EC); any rights a holder may have in that wording are not licensed by "
    "this project and stay with the holder. Changes: converted from PDF to Markdown and split into QRD sections; "
    "wording intended to be unchanged. The European Commission and the European Medicines Agency do not endorse "
    "this project and are not responsible for it.")

EU_SOURCES_READ = (
    "Union Register pages were read at https://ec.europa.eu/health/documents/community-register/ and EMA product "
    "metadata at https://www.ema.europa.eu/; the EMA legal notice is at "
    "https://www.ema.europa.eu/en/about-us/legal-notice and the Commission legal notice at "
    "https://commission.europa.eu/legal-notice_en.")

US_CHANGES = ("Converted from SPL XML to Markdown and split into one file per LOINC section, one sentence per line; "
              "wording intended to be unchanged; automated conversion can introduce errors; the pinned source file "
              "(Source-URL, Source-SHA256) is authoritative; where this copy differs from the official DailyMed "
              "document, the official text prevails.")
EU_CHANGES = ("Converted from PDF to Markdown and split into one file per QRD section, one sentence per line; "
              "wording intended to be unchanged; automated conversion can introduce errors; the pinned source file "
              "(Source-URL, Source-SHA256) is authoritative; where this copy differs from the Annex I PDF published "
              "by the European Commission in the Union Register, the official text prevails.")

ATTRIBUTION_US = (
    "Courtesy of the U.S. National Library of Medicine (DailyMed); NLM does not endorse this project. Particula Tech "
    "does not license the label text and claims no copyright in it; copyright stays with its authors and holders. "
    "US label text is not public domain, CC0 or openly licensed.")
ATTRIBUTION_EU = (
    "Source: European Commission, Union Register of medicinal products, and the European Medicines Agency (EMA); "
    "© European Union, 1995-2026; the Commission content is reused under Commission Decision 2011/833/EU and CC BY "
    "4.0, EMA content with acknowledgement under the EMA legal notice; neither endorses this project. Changes: "
    "converted from PDF to Markdown and split into QRD sections. Any rights a marketing authorisation holder may "
    "have in the Annex I wording are not licensed by this project and stay with the holder.")

TOOLS_SENTENCE = (f"Scope, record window, limits and the tools that build this repository are at {CODE_REPO}; this "
                  "repository carries no counts or dates of its own.")
SEE_NOTICE = ("See `NOTICE.md` for licence terms, the rights-holder and corrections contact, trade marks and the "
              "warranty disclaimer. Not medical advice.")

SCOPE_US = ("Covers this record only: {labels} US labels of {molecules} selected molecules, as DailyMed showed "
            "them; not all of DailyMed and not every label of these molecules.")
SCOPE_EU = ("Covers this record only: the English Annex I of {products} EU reference products, one per molecule; not "
            "every product authorised in the EU.")

OLD_FORMAT_SECTIONS = ("An old-format US label has no Warnings and Precautions section; its WARNINGS and PRECAUTIONS "
                       "sections are filed under 43685-7 so its history continues across the switch to the PLR format, "
                       "and section.label_headings then gives the headings the label itself used.")

FORMAT_CONVERSION = ("format_conversion is set when a US version moved the label between the old format and the "
                     "Physician Labeling Rule (PLR) format ('old-to-PLR' or 'PLR-to-old'), so its sections were "
                     "reorganised and a change there may be restructuring rather than new wording; it is null "
                     "otherwise.")

CORPORA = ("us", "eu")


def scope(corpus: str, labels: int, molecules: int) -> str:
    """The one-sentence scope statement of a search-style answer over one corpus, from counts read at run time."""
    _check(corpus)
    if corpus == "us":
        return SCOPE_US.format(labels=f"{labels:,}", molecules=f"{molecules:,}")
    return SCOPE_EU.format(products=f"{labels:,}")


def _check(corpus: str) -> None:
    if corpus not in CORPORA:
        raise ValueError(f"unknown corpus {corpus!r}; expected one of {', '.join(CORPORA)}")


def corpus_readme(corpus: str) -> str:
    """README.md at the root of a corpus repository (constants only): what the repository holds, the fidelity
    rule and the short attribution; the full legal text is in corpus_notice()."""
    _check(corpus)
    if corpus == "us":
        body = (
            "US prescription drug labels as DailyMed (U.S. National Library of Medicine) showed them, as a git "
            "history: one commit per published label version, one folder `<inn>/<setid>/` per DailyMed setid with "
            "`label.json` and one Markdown file per LOINC section, one sentence per line. Commit dates are "
            "DailyMed's Published-Date, never FDA approval dates; the trailers `Source-Id`, `Source-Version`, "
            "`Published-Date`, `Source-URL` and `Source-SHA256` name each commit's source file and `Text-Fidelity` "
            "its conversion rule.")
        fidelity = FIDELITY_US
        attribution = ATTRIBUTION_US
    else:
        body = (
            "The English Annex I (summary of product characteristics) of EU centrally authorised products as the "
            "European Commission's Union Register carries them, as a git history: one commit per Commission "
            "decision whose English Annex I was converted, one folder `<inn>/<EMEA-H-C-nnnnnn>/` per product with "
            "`product.json` and one Markdown file per QRD section, one sentence per line. Commit dates are the "
            "Commission decision date, never an approval or Official Journal date; the trailers `Source-Id`, "
            "`Source-Version`, `Effective-Date`, `Decision-Number`, `Source-URL` and `Source-SHA256` name each "
            "commit's source file and `Text-Fidelity` its conversion rule.")
        fidelity = FIDELITY_EU
        attribution = ATTRIBUTION_EU
    parts = [f"# As-of Drug Labels: {corpus.upper()} corpus", body, fidelity, attribution, TOOLS_SENTENCE, SEE_NOTICE]
    return "\n\n".join(parts) + "\n"


def corpus_notice(corpus: str) -> str:
    """NOTICE.md at the root of a corpus repository: attribution and licence terms, contact, trade marks,
    warranty (constants only; no counts)."""
    _check(corpus)
    head = [US_LICENCE] if corpus == "us" else [EU_LICENCE, EU_SOURCES_READ]
    parts = ["# Notice", *head, RIGHTS_HOLDERS, INDEPENDENCE, NO_WARRANTY]
    return "\n\n".join(parts) + "\n"


def root_files(corpus: str) -> dict[str, bytes]:
    """{file name: UTF-8 bytes} of the notice files written at a corpus root in its first commit."""
    return {"README.md": corpus_readme(corpus).encode("utf-8"), "NOTICE.md": corpus_notice(corpus).encode("utf-8")}
