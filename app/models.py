"""Pydantic models for structured Claude responses. Every drafting/scoring
call is prompted for JSON-only output and validated against one of these
before being persisted -- per the project brief's "Pydantic models for
every structured Claude response" constraint.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class ParagraphBlock(BaseModel):
    # No default on `type` -- a default makes Pydantic's generated JSON
    # schema mark the field as not-required, which lets the model omit it
    # entirely under Claude's structured-outputs enforcement. Without a
    # discriminator value present in the raw JSON, discriminated-union
    # parsing then fails outright (seen in practice: regulatory_report's
    # table block came back with no "type" key). Required + no default
    # forces the schema to demand it.
    type: Literal["paragraph"]
    text: str


class HeadingBlock(BaseModel):
    """An internal sub-heading within a section (this template has no
    Heading 2 style -- see docs/template_structure.md -- so these render as
    a bolded lead-in line, not a new Word heading style)."""
    type: Literal["heading"]
    text: str


class TableBlock(BaseModel):
    type: Literal["table"]
    headers: list[str]
    rows: list[list[str]]


ContentBlock = Annotated[
    Union[ParagraphBlock, HeadingBlock, TableBlock], Field(discriminator="type")
]


class SectionContent(BaseModel):
    """One template heading's worth of content (docs/template_structure.md's
    25-section skeleton). A single drafting task may produce more than one
    of these -- e.g. the Corporate Report task also produces the "Missing /
    Recommended Corporate Documents" and "Legal Review -- Key Findings at a
    Glance" sub-sections in the same call, since the source prompt library
    has no separate prompt for those (see docs/prompt_inventory.md)."""
    section_key: str = Field(description="Matches a heading key from the template skeleton")
    title: str
    blocks: list[ContentBlock]


class DraftingOutput(BaseModel):
    """The JSON shape every Stage 2 drafting call must return."""
    sections: list[SectionContent]


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
