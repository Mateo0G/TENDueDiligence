"""Loads reference/AccuBreath_Due_Diligence_Report.docx once and splits it
into per-heading text, so Stage 2 drafting prompts can include "here is how
the reference report handled this section" as a style guide -- per the
brief's "using the sample report's completed sections as a style/format
guide for each generated section."
"""
import functools

from docx import Document as DocxDocument

REFERENCE_DOCX_PATH = "reference/AccuBreath_Due_Diligence_Report.docx"


@functools.lru_cache(maxsize=1)
def _sections_by_heading() -> dict[str, str]:
    doc = DocxDocument(REFERENCE_DOCX_PATH)
    sections: dict[str, list[str]] = {}
    current_heading = None

    for p in doc.paragraphs:
        text = p.text.strip()
        if p.style.name == "Heading 1":
            if not text:
                continue  # spacer heading, see docs/template_structure.md
            current_heading = text
            sections.setdefault(current_heading, [])
        elif current_heading is not None and text:
            sections[current_heading].append(text)

    return {heading: "\n".join(paras) for heading, paras in sections.items()}


@functools.lru_cache(maxsize=1)
def get_full_reference_text() -> str:
    """The entire reference report, section by section, as one block --
    used as a single shared prompt-caching target for every Stage 2 call
    (see app/claude_client.py). This replaced per-task heading excerpts:
    excerpting a different substring per task meant a different cache
    prefix per task, defeating cross-call caching entirely, whereas one
    constant blob is byte-identical (and therefore cache-hit-eligible)
    across every Stage 2 call for every job, not just within one job."""
    return "\n\n".join(f"## {heading}\n{text}" for heading, text in _sections_by_heading().items())


def get_style_guide(heading: str, max_chars: int = 6000) -> str:
    """Full text of one reference-report section, truncated for prompt
    budget. Returns "" if the heading doesn't exist in the reference (e.g.
    a heading text that doesn't match exactly -- callers should treat that
    as "no example available" rather than fail)."""
    text = _sections_by_heading().get(heading, "")
    return text[:max_chars]


@functools.lru_cache(maxsize=1)
def get_disclaimer_text() -> str:
    """The standing "TEN Capital Network Disclaimer" boilerplate from the
    reference report's front matter. This is legal boilerplate that should
    be identical across reports, unlike section content -- reused verbatim
    rather than regenerated per company."""
    doc = DocxDocument(REFERENCE_DOCX_PATH)
    found_label = False
    lines = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if p.style.name == "Heading 1" and text:
            break  # front matter ends at the first real heading
        if found_label:
            if text:
                lines.append(text)
        elif text == "TEN Capital Network Disclaimer:":
            found_label = True
    return "\n\n".join(lines)
