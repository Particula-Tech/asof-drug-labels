#!/usr/bin/env python3
"""MCP server over the label-history repo: JSON-RPC 2.0 on stdio, read-only, no network.

One-line install into an MCP client's server list, e.g.:

  claude mcp add asof-drug-labels -- python3 /path/to/tools/mcp_server.py

Options: --repo (env ASOF_US_REPO, default us/ when it is a git repo, else build/us), --molecules (default
molecules/), --eu-repo (env ASOF_EU_REPO, an empty value meaning none; default
build/eu when tools/eu.py built it). Standard library only.
Nothing but protocol messages goes to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labels  # noqa: E402
import notices  # noqa: E402

SERVER_NAME = "asof-drug-labels"
SERVER_VERSION = "0.6.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND = -32700, -32600, -32601
INVALID_PARAMS, INTERNAL_ERROR, RESOURCE_NOT_FOUND = -32602, -32603, -32002
RESOURCE_TEMPLATE = "label://{jurisdiction}/{label_id}/{date}/{section}"
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
INSTRUCTIONS = (
    "Version history of US drug labels (DailyMed) and EU summaries of product characteristics (Annex I of "
    "Commission decisions in the Union Register), one git commit per version. US: on date X means the newest "
    "version whose Published-Date is on or before X, what DailyMed showed that day, never FDA-approved. EU: the "
    "newest Annex I whose Commission decision date is on or before X; cite it as the Union Register's Annex I of "
    "Commission decision (year) number of date, never as approved. Label ids: US setid, EU EMEA-H-C-nnnnnn (or "
    "EMEA/H/C/nnnnnn, EU/1/yy/nnn). Start with find_label, then read_label / blame / diff / section_history for "
    "one label, changes_between / search / first_appearance across a corpus (jurisdiction us, eu or all), "
    "compare_jurisdictions for US-versus-EU text of one molecule, and check_citations to verify quotes. The "
    "corpora hold selected molecules only, not all of DailyMed or every EU product; corpus-wide answers say what "
    "they cover in their scope field. Cite every "
    "answer with the label_id, version and date returned. " + notices.NO_WARRANTY + " Wording is intended to be "
    "unchanged from the source, but automated conversion can introduce errors; the source file each answer cites is "
    "authoritative and the official text prevails. " + notices.INDEPENDENCE)
AS_IS = "Text as converted, provided as is; the cited source file is authoritative."
SCOPE = ("The answer's scope field states what the corpus covers (its label and molecule counts); it is not all of "
         "DailyMed or every EU product.")


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


# ---------------------------------------------------------------- schemas

def _str(description: str) -> dict:
    return {"type": "string", "description": description}


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


DATE = ("ISO date YYYY-MM-DD. US: what DailyMed showed that day (newest version published on or before it). EU: the "
        "Annex I of the newest Commission decision dated on or before it")
LABEL_ID = ("US: DailyMed setid (or 'dailymed:<setid>' or a unique prefix). EU: EMEA-H-C-nnnnnn, EMEA/H/C/nnnnnn or "
            "the EU number EU/1/yy/nnn (optional 'eu:' prefix). Get it from find_label")
SECTION = ("US: LOINC section code (e.g. 43685-7) or a word from the section name (e.g. 'warnings', 'boxed'). EU: QRD "
           "number (e.g. 4.3), 'smpc-4.3', the file stem or a word (e.g. 'contraindications')")
JURISDICTION = {"type": "string", "enum": ["us", "eu", "all"],
                "description": "us (default), eu or all (the US answer plus an 'eu' sub-object)"}
FIND_JURISDICTION = {"type": "string", "enum": ["us", "eu", "all"],
                     "description": "all (default: US and EU matches in one list, US first), us or eu"}
CORPUS_SECTION = ("US: LOINC code, e.g. 43685-7; EU: QRD number, e.g. 4.4; with jurisdiction all either one, "
                  "translated to the other side when the pairing table has a counterpart")
LIMIT = {"type": "integer", "minimum": 1, "description": f"maximum rows returned (default {labels.DEFAULT_LIMIT})"}
COMPACT = {"type": "boolean", "description": "list section codes and line counts only, without per-section "
                                             "details or the matching sentences (default false)"}

TOOLS: list[dict] = [
    {"name": "find_label", "title": "Find a label",
     "description": "Find labels by setid (or prefix), brand or product name, INN, NDC or RxCUI, and EU products by "
                    "EMA number, EU number, name, INN or holder. Returns id, jurisdiction, title, holder, INN, record "
                    "window, version count and status (EU: history_type and comparable_to_us).",
     "inputSchema": _schema({"query": _str("setid, setid prefix, brand or product name, INN, NDC, RxCUI, EMA product "
                                           "number (EMEA/H/C/nnnnnn) or EU number (EU/1/yy/nnn)"),
                             "jurisdiction": FIND_JURISDICTION}, ["query"])},
    {"name": "read_label", "title": "Read a label as of a date",
     "description": "The label text in force on a date (US: what DailyMed showed; EU: the Annex I of the Commission "
                    "decision in force): one section, or the section index plus the text (capped, with a note when "
                    "truncated). Dates before the record start return an error naming it. " + AS_IS,
     "inputSchema": _schema({"label_id": _str(LABEL_ID), "date": _str(DATE), "section": _str(SECTION)},
                            ["label_id", "date"])},
    {"name": "section_history", "title": "Section history",
     "description": "Every version that created, changed or deleted one section, with the version date (US: "
                    "published date; EU: Commission decision date), lines added and removed, and, for US labels, "
                    "the old-format-to-PLR conversion flag.",
     "inputSchema": _schema({"label_id": _str(LABEL_ID), "section": _str(SECTION)}, ["label_id", "section"])},
    {"name": "blame", "title": "Blame a section",
     "description": "Each sentence line of a section on a date credited to the version that first wrote it "
                    "(git blame -M -C, so moved text keeps its original version); words=true adds per-word credit.",
     "inputSchema": _schema({"label_id": _str(LABEL_ID), "date": _str(DATE), "section": _str(SECTION),
                             "words": {"type": "boolean", "description": "also credit each word (default false)"}},
                            ["label_id", "date", "section"])},
    {"name": "diff", "title": "Diff two dates",
     "description": "Word-level diff between the versions in force on two dates, plus the versions in between.",
     "inputSchema": _schema({"label_id": _str(LABEL_ID), "date_a": _str(DATE), "date_b": _str(DATE),
                             "section": _str(SECTION)}, ["label_id", "date_a", "date_b"])},
    {"name": "changes_between", "title": "Changes across the corpus",
     "description": "Every label version dated in [from, to] (US: Published-Date, what DailyMed showed; EU: the "
                    "Commission decision date of the Annex I) with the sections it changed; versions whose "
                    "section text did not change (or only label.json) are counted in unchanged_versions / "
                    "metadata_only_versions, not listed. With query, only changes where a section gained (added), "
                    "lost (removed) or reworded the phrase; every such row has lines_before (phrase lines in the "
                    "previous version) and lines_after (in this version), so a removed row's deleted text is in "
                    "lines_before and its lines_after is empty. A version that is the first version of its label in "
                    "this record (no earlier version in the record; often but not always version 1, and not "
                    "necessarily new on DailyMed) has nothing to compare with: it is flagged new_label and, with "
                    "query, listed in new_labels when it already carried the phrase, not counted as added. "
                    + notices.FORMAT_CONVERSION + " " + notices.OLD_FORMAT_SECTIONS + " " + SCOPE + " Rows are large: without a query the default limit "
                    f"is {labels.DEFAULT_LIMIT_ALL}; narrow the window or use compact. Example: from 2023-01-01 to "
                    "2025-12-31, section 43685-7, query 'suicidal ideation'.",
     "inputSchema": _schema({"from": _str("start date, inclusive"), "to": _str("end date, inclusive"),
                             "jurisdiction": JURISDICTION, "section": _str(CORPUS_SECTION),
                             "query": _str("phrase to track, case- and whitespace-insensitive"),
                             "limit": {**LIMIT, "description": f"maximum rows returned (default {labels.DEFAULT_LIMIT} "
                                                               f"with a query, {labels.DEFAULT_LIMIT_ALL} without)"},
                             "compact": COMPACT}, ["from", "to"])},
    {"name": "search", "title": "Search labels as of a date",
     "description": "Labels whose text in force on a date contains the phrase (case- and whitespace-insensitive), "
                    "with the version shown that day and the matching sections. " + SCOPE,
     "inputSchema": _schema({"text": _str("phrase to find"), "date": _str(DATE), "jurisdiction": JURISDICTION,
                             "section": _str(CORPUS_SECTION), "limit": LIMIT}, ["text", "date"])},
    {"name": "first_appearance", "title": "First appearance of a phrase",
     "description": "Per label, the first version containing the phrase and the version where it left (if it did); "
                    "corpus-wide when label_id is omitted, with the earliest label first. One row (episode) per "
                    "entry/exit of the phrase, so a label can appear more than once; labels_count counts labels. "
                    "new_label: the episode starts at the first version of the label in this record. Without "
                    "label_id, " + SCOPE[0].lower() + SCOPE[1:],
     "inputSchema": _schema({"text": _str("phrase to find"), "label_id": _str(LABEL_ID + " (optional)"),
                             "limit": LIMIT,
                             "jurisdiction": {**JURISDICTION, "description": "corpus without label_id: us (default), eu "
                                              "or all; with label_id the id decides and this must agree"}},
                            ["text"])},
    {"name": "compare_jurisdictions", "title": "Compare US and EU",
     "description": "US label versus EU SmPC of one molecule on a date, aligned by topic (4.1 indications, 4.2 "
                    "dosage, 4.3 contraindications, 4.4 warnings, 4.5 interactions, 4.6 pregnancy and lactation, 4.8 "
                    "adverse reactions, 4.9 overdosage). Without section: line counts per pair. With section: both "
                    "texts, the verbatim lines only one side has, and citations ready for check_citations. Always "
                    "states comparable_to_us and its note; no text is invented. " + AS_IS,
     "inputSchema": _schema({"molecule": _str("INN, ingredient, US brand or EU product name"), "date": _str(DATE),
                             "section": _str("LOINC code, QRD number or pair word, e.g. 'contraindications'"),
                             "us_label_id": _str("US setid to compare instead of the primary NDA/BLA pick")},
                            ["molecule", "date"])},
    {"name": "check_citations", "title": "Check citations",
     "description": "For each {doc_id, version, section, quote, as_of}: whether that version exists, whether the quote "
                    "is in that section of that version (case and whitespace ignored; other sections reported), and "
                    "whether that version was in force on as_of (US: what DailyMed showed; EU: the Annex I of the "
                    "Commission decision in force). Items may mix US and EU ids. Deterministic; used as a scorer.",
     "inputSchema": _schema({"citations": {"type": "array", "minItems": 1, "items": {
         "type": "object", "properties": {"doc_id": _str("US setid (or dailymed:<setid>) or an EU id: "
                                                          "EMEA-H-C-nnnnnn, EMEA/H/C/nnnnnn, EU/1/yy/nnn"),
                                          "version": {"type": "integer", "description": "label version number "
                                                                                        "(EU: Source-Version)"},
                                          "section": _str(SECTION), "quote": _str("quoted text"),
                                          "as_of": _str("date the quote is claimed for")},
         "required": ["doc_id", "version", "quote"]}}}, ["citations"])},
    {"name": "coverage", "title": "Corpus coverage",
     "description": "Scope, record window, build date and history type of each corpus (US retired-label count; EU "
                    "products per history type: in-force, current-only, replayed), the DailyMed database date of "
                    "the last daily update and the daily/<date> tags of each corpus HEAD.",
     "inputSchema": _schema({}, [])},
]

PROMPTS: list[dict] = [
    {"name": "label-change-brief", "title": "Label change brief",
     "description": "What changed in a drug's label between two dates, with verifiable citations.",
     "arguments": [{"name": "drug", "description": "brand name, INN or setid", "required": True},
                   {"name": "from", "description": "start date YYYY-MM-DD", "required": True},
                   {"name": "to", "description": "end date YYYY-MM-DD", "required": True}]},
    {"name": "check-document-claims", "title": "Check document claims",
     "description": "Check every label claim in a pasted text against the label text in force on a date (US: what "
                    "DailyMed showed; EU: the Annex I of the Commission decision in force).",
     "arguments": [{"name": "text", "description": "the document text to check", "required": True},
                   {"name": "as_of", "description": "date the claims are made for, YYYY-MM-DD", "required": True}]},
]


CITE_WORDING = ("Cite US labels as: label_id, version N, published YYYY-MM-DD, section code, worded 'DailyMed showed "
                "...' (never 'FDA-approved'). Cite EU labels as: label_id, version N, section number, worded 'the Union "
                "Register's Annex I of Commission decision (year) number of date carried ...' (never 'approved'). ")


def prompt_messages(name: str, args: dict) -> list[dict]:
    if name == "label-change-brief":
        text = (
            f"Write a change brief for the label of {args['drug']} between {args['from']} and {args['to']}.\n\n"
            f"1. Call find_label with query {args['drug']!r} and pick the matching label_id(s) (US setids, EU "
            "EMEA-H-C-nnnnnn ids); if several labels are live, brief each one or ask which is meant.\n"
            f"2. Call diff with date_a {args['from']} and date_b {args['to']} for each label_id; list the versions in "
            "between and summarise the word-level changes per section.\n"
            "3. For each changed section call section_history, and blame on the later date for sentences that matter, "
            "so every change is credited to the version that wrote it.\n"
            f"4. {CITE_WORDING}Repeat each corpus's notice (record end; where to check the latest text) at the end.")
    else:
        text = (
            f"Check every drug-label claim in the text below against the label text in force on {args['as_of']} "
            "(US: what DailyMed showed that day; EU: the Annex I of the Commission decision in force).\n\n"
            "1. List each claim that quotes or paraphrases a label (indication, warning, dose, contraindication).\n"
            "2. For each drug named, call find_label to get the label_id; if several labels are live, check each.\n"
            f"3. Call read_label (or search with date {args['as_of']}) to find the section in force on that date, then "
            "call check_citations with the label_id, that version, the section and the exact quote, as_of "
            f"{args['as_of']}.\n"
            f"4. Report per claim: supported, contradicted or not found, with label_id, version, date and section. "
            f"{CITE_WORDING}End with the record notice.\n\n"
            f"Text to check:\n\n{args['text']}")
    return [{"role": "user", "content": {"type": "text", "text": text}}]


# ---------------------------------------------------------------- validation

def validate(schema: dict, args: Any, where: str) -> dict:
    """Check ``args`` against our own (small) JSON-schema subset; raise -32602 on a mismatch."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, f"{where}: arguments must be an object")
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    # An optional argument sent as null means "absent"; clients commonly emit null for optional fields.
    args = {k: v for k, v in args.items() if v is not None or k in required}
    unknown = sorted(set(args) - set(props))
    if unknown and not schema.get("additionalProperties", True):
        raise RpcError(INVALID_PARAMS, f"{where}: unknown argument(s) {', '.join(unknown)}; "
                                       f"accepted: {', '.join(props) or 'none'}")
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise RpcError(INVALID_PARAMS, f"{where}: missing required argument(s) {', '.join(missing)}")
    for key, value in args.items():
        _check_type(props[key], value, f"{where}: argument {key!r}")
    return args


def _check_type(spec: dict, value: Any, where: str) -> None:
    kinds = {"string": str, "boolean": bool, "integer": int, "array": list, "object": dict}
    want = spec.get("type")
    if want and (not isinstance(value, kinds[want]) or (want == "integer" and isinstance(value, bool))):
        raise RpcError(INVALID_PARAMS, f"{where} must be a {want}, got {type(value).__name__}")
    if "enum" in spec and value not in spec["enum"]:
        raise RpcError(INVALID_PARAMS, f"{where} must be one of {', '.join(spec['enum'])}, got {value!r}")
    if want == "integer" and "minimum" in spec and value < spec["minimum"]:
        raise RpcError(INVALID_PARAMS, f"{where} must be at least {spec['minimum']}")
    if want == "array":
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise RpcError(INVALID_PARAMS, f"{where} must have at least {spec['minItems']} item(s)")
        for i, item in enumerate(value):
            if "items" in spec:
                _check_type(spec["items"], item, f"{where}[{i}]")
    if want == "object" and "properties" in spec:
        validate(spec, value, where)


# ---------------------------------------------------------------- server

class Server:
    """Dispatches JSON-RPC messages; the repo indexes are built on first use. ``eu_repo`` None means no
    EU corpus (every answer is then the US-only answer apart from the EU_REASON text)."""

    def __init__(self, repo: Path, molecules: Path, eu_repo: Path | None = None):
        self.repo, self.molecules, self.eu_repo = repo, molecules, eu_repo
        self._index: labels.Index | None = None
        self._eu: labels.EuIndex | None = None
        self._eu_loaded = False
        self.protocol_version = PROTOCOL_VERSIONS[0]
        self.initialized = False
        self.methods: dict[str, Callable[[dict], Any]] = {
            "initialize": self.initialize, "ping": lambda params: {},
            "tools/list": lambda params: {"tools": [{k: v for k, v in t.items()} | {"annotations": READ_ONLY}
                                                    for t in TOOLS]},
            "tools/call": self.tools_call,
            "prompts/list": lambda params: {"prompts": PROMPTS}, "prompts/get": self.prompts_get,
            "resources/list": lambda params: {"resources": []},
            "resources/templates/list": lambda params: {"resourceTemplates": [{
                "name": "label-text", "title": "Label text as of a date", "uriTemplate": RESOURCE_TEMPLATE,
                "description": "Text of one label section in force on a date; jurisdiction is 'us' (setid, LOINC "
                               "code) or 'eu' (EMEA-H-C-nnnnnn, QRD number). Examples: "
                               "label://us/1e11ad30-1041-4520-10b0-8f9d30d30fcc/2023-01-01/43685-7, "
                               "label://eu/EMEA-H-C-003820/2023-01-01/4.3",
                "mimeType": "text/markdown"}]},
            "resources/read": self.resources_read,
        }

    def index(self) -> labels.Index:
        if self._index is None:
            try:
                self._index = labels.Index(self.repo, self.molecules)
            except labels.LabelError as err:
                log(f"cannot index repo {self.repo} (crosswalk {self.molecules}): {err}")
                raise
            log(f"indexed {len(self._index.versions)} versions of {len(self._index.by_setid)} labels from {self.repo} "
                f"(crosswalk {self.molecules})")
        return self._index

    def eu(self) -> labels.EuIndex | None:
        """The EU index, loaded once on first use; None when no EU repo is configured."""
        if not self._eu_loaded:
            try:
                self._eu = labels.open_eu(self.eu_repo, self.molecules)
            except labels.LabelError as err:
                log(f"cannot index EU repo {self.eu_repo}: {err}")
                raise
            self._eu_loaded = True
            if self._eu is not None:
                log(f"indexed {len(self._eu.versions)} EU versions of {len(self._eu.by_setid)} products from "
                    f"{self.eu_repo}")
            elif self.eu_repo is not None:
                log(f"warning: {self.eu_repo} is not an EU repo built by tools/eu.py build; EU answers are unavailable")
        return self._eu

    def accepts_batches(self) -> bool:
        """JSON-RPC batches were removed in protocol 2025-06-18; older negotiated versions keep them."""
        return not (self.initialized and self.protocol_version == PROTOCOL_VERSIONS[0])

    # ---- methods

    def initialize(self, params: dict) -> dict:
        asked = params.get("protocolVersion")
        self.protocol_version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        self.initialized = True
        return {"protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                                 "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "AsOf drug labels", "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS}

    def tools_call(self, params: dict) -> dict:
        name = params.get("name")
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name!r}; available: {', '.join(t['name'] for t in TOOLS)}")
        args = validate(tool["inputSchema"], params.get("arguments"), f"tools/call {name}")
        try:
            result = self.run_tool(name, args)
        except labels.LabelError as err:
            return tool_result({"error": str(err), "tool": name}, is_error=True)
        return tool_result(result)

    def run_tool(self, name: str, a: dict) -> dict:
        ix, eu = self.index(), self.eu()
        if name == "find_label":
            return labels.find_label(ix, a["query"], a.get("jurisdiction"), eu=eu)
        if name == "read_label":
            return labels.read_label(ix, a["label_id"], a["date"], a.get("section"), eu=eu)
        if name == "section_history":
            return labels.section_history(ix, a["label_id"], a["section"], eu=eu)
        if name == "blame":
            return labels.blame(ix, a["label_id"], a["date"], a["section"], a.get("words", False), eu=eu)
        if name == "diff":
            return labels.diff(ix, a["label_id"], a["date_a"], a["date_b"], a.get("section"), eu=eu)
        if name == "changes_between":
            return labels.changes_between(ix, a["from"], a["to"], a.get("jurisdiction"), a.get("section"),
                                          a.get("query"), a.get("limit"), a.get("compact", False), eu=eu)
        if name == "search":
            return labels.search(ix, a["text"], a["date"], a.get("jurisdiction"), a.get("section"),
                                 a.get("limit", labels.DEFAULT_LIMIT), eu=eu)
        if name == "first_appearance":
            return labels.first_appearance(ix, a["text"], a.get("label_id"), a.get("limit", labels.DEFAULT_LIMIT),
                                           a.get("jurisdiction"), eu=eu)
        if name == "compare_jurisdictions":
            return labels.compare_jurisdictions(ix, a["molecule"], a["date"], a.get("section"), eu=eu,
                                                us_label_id=a.get("us_label_id"))
        if name == "check_citations":
            return labels.check_citations(ix, a["citations"], eu=eu)
        if name == "coverage":
            return labels.coverage(ix, eu)
        raise RpcError(INTERNAL_ERROR, f"tool {name} is listed but not implemented")

    def prompts_get(self, params: dict) -> dict:
        name = params.get("name")
        prompt = next((p for p in PROMPTS if p["name"] == name), None)
        if prompt is None:
            raise RpcError(INVALID_PARAMS, f"Unknown prompt: {name!r}; available: {', '.join(p['name'] for p in PROMPTS)}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(INVALID_PARAMS, f"prompts/get {name}: arguments must be an object")
        missing = [a["name"] for a in prompt["arguments"] if a["required"] and not str(args.get(a["name"], "")).strip()]
        if missing:
            raise RpcError(INVALID_PARAMS, f"prompts/get {name}: missing required argument(s) {', '.join(missing)}")
        return {"description": prompt["description"], "messages": prompt_messages(name, {k: str(v) for k, v in args.items()})}

    def resources_read(self, params: dict) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("label://"):
            raise RpcError(INVALID_PARAMS, f"uri must match {RESOURCE_TEMPLATE}, got {uri!r}")
        parts = uri[len("label://"):].strip("/").split("/")
        if len(parts) not in (3, 4):
            raise RpcError(RESOURCE_NOT_FOUND, f"Resource not found: uri must match {RESOURCE_TEMPLATE}", {"uri": uri})
        jurisdiction, label_id, day = parts[:3]
        section = parts[3] if len(parts) == 4 and parts[3] else None
        j = jurisdiction.lower()
        if j not in ("us", "eu"):
            raise RpcError(RESOURCE_NOT_FOUND, f"Resource not found: jurisdiction must be 'us' or 'eu', not "
                                               f"{jurisdiction!r}", {"uri": uri})
        if (j == "eu") != labels.is_eu_id(label_id):
            kind = "an EU id (EMEA-H-C-nnnnnn)" if j == "eu" else "a US setid"
            raise RpcError(RESOURCE_NOT_FOUND, f"Resource not found: label://{j}/ needs {kind}, got {label_id!r}",
                           {"uri": uri})
        eu = self.eu() if j == "eu" else None
        if j == "eu" and eu is None:
            raise RpcError(RESOURCE_NOT_FOUND, f"Resource not found: {labels.EU_REASON}", {"uri": uri})
        try:
            result = labels.read_label(self.index(), label_id, day, section, eu=eu)
        except labels.LabelError as err:
            raise RpcError(RESOURCE_NOT_FOUND, f"Resource not found: {err}", {"uri": uri}) from err
        if j == "eu":
            head = labels.eu_header_line(result)
        else:
            head = (f"{result['wording']} (label {result['label_id']}, version {result['version']}, published "
                    f"{result['published_date']}, SPL effective {result['effective_time']}). "
                    f"Record {result['record_from']} to {result['record_to']}.")
            if result.get("status_note"):
                head += f" {result['status_note']}"
        if result.get("note"):
            head += f" {result['note']}"
        text = f"{head}\n\n{result['text']}\n\n{result['notice']}\n"
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}

    # ---- dispatch

    def handle(self, message: Any) -> dict | None:
        """One JSON-RPC message in, one response out (None for notifications)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return error_response(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST,
                                  "Invalid Request: expected a JSON-RPC 2.0 request with a method")
        method, msg_id, params = message["method"], message.get("id"), message.get("params")
        if params is None:
            params = {}
        if "id" not in message:
            if method not in ("notifications/initialized", "notifications/cancelled", "notifications/roots/list_changed"):
                log(f"ignoring notification {method}")
            return None
        try:
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, f"params must be an object, got {type(params).__name__}")
            handler = self.methods.get(method)
            if handler is None:
                raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": handler(params)}
        except RpcError as err:
            return error_response(msg_id, err.code, err.message, err.data)
        except labels.LabelError as err:
            return error_response(msg_id, INTERNAL_ERROR, str(err))
        except Exception as err:  # a bug, reported to the client and logged with its traceback
            log(traceback.format_exc())
            return error_response(msg_id, INTERNAL_ERROR, f"internal error: {type(err).__name__}: {err}")


def tool_result(payload: dict, is_error: bool = False) -> dict:
    # The text copy is compact JSON: the payload travels twice (text and structuredContent).
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out = {"content": [{"type": "text", "text": text}], "structuredContent": payload}
    if is_error:
        out["isError"] = True
    return out


def error_response(msg_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def log(text: str) -> None:
    print(f"[{SERVER_NAME}] {text}", file=sys.stderr, flush=True)


def serve(server: Server, stdin=None, stdout=None) -> int:
    """Newline-delimited JSON-RPC until EOF."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        batch = False
        try:
            message = json.loads(line)
        except ValueError as err:
            responses: list[dict | None] = [error_response(None, PARSE_ERROR, f"Parse error: {err}")]
        else:
            batch = isinstance(message, list)
            messages = message if batch else [message]
            if not messages:  # an empty array is answered with a single error object
                responses = [error_response(None, INVALID_REQUEST, "Invalid Request: empty batch")]
                batch = False
            elif batch and not server.accepts_batches():
                responses = [error_response(None, INVALID_REQUEST, "Invalid Request: JSON-RPC batches are not "
                                            f"supported in protocol {server.protocol_version}; send one message per line")]
                batch = False
            else:
                responses = [server.handle(m) for m in messages]
        replies = [r for r in responses if r is not None]
        if not replies:
            continue
        payload = replies if batch else replies[0]  # a batch is answered with an array, even of one reply
        stdout.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=labels.resolve_us_repo(),
                   help="built US repo (env ASOF_US_REPO; default us/ when it is a git repo, else build/us)")
    p.add_argument("--molecules", type=Path, default=labels.DEFAULT_MOLECULES, help="crosswalk directory")
    p.add_argument("--eu-repo", default=None,
                   help="built EU repo (env ASOF_EU_REPO, empty for none; default build/eu when tools/eu.py built it)")
    args = p.parse_args(argv)
    eu_repo = labels.resolve_eu_repo(args.eu_repo)
    if not (args.repo / ".git").exists():
        log(f"warning: {labels.not_built_message(args.repo, labels.Index.BUILD_HINT)}; tool calls will fail "
            f"until then, or point ASOF_US_REPO at a built repo")
    return serve(Server(args.repo, args.molecules, eu_repo))


if __name__ == "__main__":
    sys.exit(main())
