"""Pydantic models for structured Claude responses. Every drafting/scoring
call is prompted for JSON-only output and validated against one of these
before being persisted -- per the project brief's "Pydantic models for
every structured Claude response" constraint.

Note on `type` fields: no defaults on any discriminator. A default makes
Pydantic's generated JSON schema mark the field as not-required, which lets
the model omit it entirely under Claude's structured-outputs enforcement;
without a discriminator value present in the raw JSON, discriminated-union
parsing then fails outright (seen in practice: a table block came back with
no "type" key). Required + no default forces the schema to demand it.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# What a dataroom document is evidence *for*. A file usually supports more
# than one report (a convertible note is deal terms *and* financial *and* a
# contract), so digests carry a list of these rather than the single
# upload-time category -- that single category was why most files never
# reached the sections that needed them.
EvidenceTag = Literal[
    "commercial",       # customers, pricing, sales plan, LOIs, market size, surveys
    "financial",        # statements, model, budget, cap table, valuation, use of funds
    "deal_terms",       # term sheet, notes, SAFEs, warrants, subscription docs, side letters
    "corporate",        # charter, bylaws, operating agreement, consents, good standing
    "contracts",        # material agreements: supplier, consulting, customer, lease, NDA, grant
    "team",             # resumes, bios, employment/offer letters, org chart, comp, options
    "ip",               # patents, applications, assignments, licenses, FTO
    "science_tech",     # technical docs, test data, product specs, development plan
    "manufacturing",    # suppliers, CMOs, BOM, COGS, supply chain
    "regulatory",       # FDA/agency path, submissions, QMS, clinical studies, IRB, timeline
    "competition",      # competitor lists/analyses, landscape
    "exit",             # exit strategy, comparable transactions, acquirer interest
]

EVIDENCE_TAGS: tuple[str, ...] = EvidenceTag.__args__


# ---------------------------------------------------------------- blocks --

class ParagraphBlock(BaseModel):
    type: Literal["paragraph"]
    text: str


class HeadingBlock(BaseModel):
    """A sub-heading inside a report subsection. level 3 renders as the
    report's teal Heading 3; level 4 as a bold, underlined run-in label
    ("Strengths observed:", "Due Diligence Team Assessment")."""
    type: Literal["heading"]
    level: Literal[3, 4]
    text: str


class TableBlock(BaseModel):
    type: Literal["table"]
    caption: str = Field(description='Short bold caption shown above the table, or "" for none')
    headers: list[str]
    rows: list[list[str]]


class CalloutBlock(BaseModel):
    """The shaded, left-ruled "Summary of Findings." box that opens every
    report."""
    type: Literal["callout"]
    label: str = Field(description='Bold lead-in, e.g. "Summary of Findings."')
    text: str


class BulletsBlock(BaseModel):
    type: Literal["bullets"]
    numbered: bool
    items: list[str]


class NoteBlock(BaseModel):
    """Small italic grey line -- a table's source note or a caveat."""
    type: Literal["note"]
    text: str


ContentBlock = Annotated[
    Union[ParagraphBlock, HeadingBlock, TableBlock, CalloutBlock, BulletsBlock, NoteBlock],
    Field(discriminator="type"),
]


class DraftedSection(BaseModel):
    section_key: str = Field(description="One of the section_key values you were asked to produce")
    blocks: list[ContentBlock]


class DraftingOutput(BaseModel):
    """The JSON shape every Stage 2 drafting call must return."""
    basis_of_preparation: str = Field(
        description=(
            'Only when you are producing a report\'s "_intro" section: the "Basis of preparation:" '
            'sentence(s) naming the specific documents (with dates) this report relied on. '
            'Otherwise "".'
        )
    )
    sections: list[DraftedSection]


# Kept for the section editor's PATCH payload (app/main.py) and anything
# else that still speaks the old one-heading-per-section shape.
class SectionContent(BaseModel):
    section_key: str
    title: str
    blocks: list[ContentBlock]


# --------------------------------------------------------------- digests --

class DigestFact(BaseModel):
    fact: str = Field(description="One specific, self-contained fact, with the exact figure/name/date/term")
    location: str = Field(description='Where in the document, e.g. "p. 4", "Sheet: P&L, 2028 column", "§3.2"')


class DocumentDigest(BaseModel):
    document_type: str = Field(description='e.g. "convertible promissory note", "5-year financial model", "resume"')
    title: str
    document_date: str = Field(description='Date the document bears or was last updated, as written; "" if none')
    parties: list[str] = Field(description="People/organizations who are parties to, authors of, or the subject of the document")
    execution_status: Literal["executed", "unsigned_or_draft", "not_applicable", "unclear"] = Field(
        description="For agreements/legal instruments: is it signed by all parties? Otherwise not_applicable"
    )
    tags: list[EvidenceTag] = Field(description="Every report this document is evidence for -- usually 1-4")
    importance: int = Field(description="1-5: how central this document is to an investor's diligence")
    summary: str = Field(description="Dense 120-400 word summary of what the document says and establishes")
    key_facts: list[DigestFact] = Field(
        description="Every material fact an analyst would cite: amounts, prices, valuations, shares, dates, "
                    "names and titles, terms, metrics, results, counts. Be exhaustive; 10-80 items."
    )
    issues: list[str] = Field(
        description="Problems visible in the document itself: unsigned, blank fields, internal "
                    "inconsistencies, expired dates, missing exhibits, unusual terms"
    )
    transcribed_text: str = Field(
        description="ONLY if the document was provided to you as page images (scanned): a faithful "
                    "transcription of its substantive text, tables as pipe-separated rows, up to ~12,000 "
                    'words. If you were given the document as text, "".'
    )


# ------------------------------------------------------------- key facts --

class KeyFact(BaseModel):
    topic: EvidenceTag
    fact: str = Field(description="The fact, stated precisely with figures/dates")
    sources: list[str] = Field(description="Filenames that support it")
    as_of: str = Field(description='Date the fact is current as of, if known, else ""')


class FactConflict(BaseModel):
    topic: EvidenceTag
    description: str = Field(description="What disagrees, with each version and its source file and date")
    resolution: str = Field(
        description="Which version the report should use and why (most recent / executed / most "
                    "authoritative), or that it must be flagged as unresolved"
    )


class KeyFactsOutput(BaseModel):
    company_profile: str = Field(
        description="Legal name, entity type and state, formation date, HQ, stage, and a precise "
                    "one-paragraph description of the product and its intended use"
    )
    facts: list[KeyFact] = Field(description="The canonical facts every report section must agree on; 40-150 items")
    conflicts: list[FactConflict]
    missing_documents: list[str] = Field(
        description="Documents a diligence team would expect for a company like this that are not in "
                    "the dataroom (e.g. audited financials, executed employment agreements, IP assignments)"
    )


# --------------------------------------------------------------- QA etc. --

class QAScoreOutput(BaseModel):
    """The JSON shape every Stage 4 QA/scoring call must return. `score` is
    optional because one DD Assessment prompt (Key Information Check) has
    no numeric score in the source spreadsheet -- see docs/prompt_inventory.md
    Flag A1. Every score in this pipeline is on a "higher is better" axis,
    including the two red-flag detectors (see app/qa_spec.py)."""
    score: float | None = None
    summary: str
    findings: list[str] = Field(default_factory=list)


class GapItem(BaseModel):
    title: str
    description: str
    severity: Literal["high", "medium", "low"]


class GapAnalysisOutput(BaseModel):
    """Stage 5, step 1 (docs/prompt_inventory.md row 89): reads the compiled
    report + Stage 4 QA scores, lists gaps/weaknesses an investor would
    perceive."""
    summary: str
    gaps: list[GapItem]


class MitigationItem(BaseModel):
    gap_title: str = Field(description="Must exactly match one GapItem.title from the gap analysis")
    mitigation: str


class MitigationOutput(BaseModel):
    """Stage 5, step 2 (docs/prompt_inventory.md row 91): reads the Gap
    Analysis output, proposes one mitigation per identified gap."""
    summary: str
    mitigations: list[MitigationItem]


class FileCategoryOutput(BaseModel):
    """Intake fallback (app/claude_client.py categorize_file_llm): only
    called for a file app/classify.py's keyword matching couldn't place --
    "none" is a real, valid answer here (a file may genuinely fit none of
    the 8 categories), not a parsing escape hatch."""
    category: Literal[
        "financial", "legal_corporate", "ip", "hr", "sales_marketing",
        "science_tech", "contracts", "regulatory", "none",
    ]
