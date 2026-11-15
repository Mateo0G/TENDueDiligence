"""Text extraction from uploaded dataroom files, so their content can go into
a Claude prompt. Anything we can't extract text from comes back as a short
placeholder rather than raising -- a due diligence dataroom always contains
some binary noise and a drafting prompt should still run on everything else.

This is the quick whole-file text used for fallbacks (a pitch deck with no
stored sidecar, files from before page-level ingestion). Data-room ingestion
itself extracts every page -- with local OCR for scans -- in app/ingest.py.
"""
import hashlib
import io
import re

import openpyxl
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from pypdf import PdfReader

# Generous ceiling so one pathological file (a 200-sheet model dump) can't
# blow a prompt on its own. Real documents almost never reach it -- the old
# 20k cap truncated a third of a real dataroom's files; this one is there
# only as a safety net. Drafting budgets per-file text separately (see
# app/evidence.py), so this is not what decides how much a section sees.
MAX_CHARS_PER_FILE = 400_000

IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

def extract_text(filename: str, data: bytes, max_chars: int = MAX_CHARS_PER_FILE) -> str:
    lower = filename.lower()
    try:
        if lower.endswith((".txt", ".md", ".csv")):
            text = data.decode("utf-8", errors="replace")
        elif lower.endswith(".pdf"):
            text = _extract_pdf(data)
        elif lower.endswith(".docx"):
            text = _extract_docx(data)
        elif lower.endswith((".xlsx", ".xlsm")):
            text = _extract_xlsx(data)
        elif lower.endswith(".pptx"):
            text = _extract_pptx(data)
        elif lower.endswith(tuple(IMAGE_MEDIA_TYPES)):
            return f"[image file: {filename}]"
        else:
            return f"[unsupported file type, {len(data)} bytes: {filename}]"
    except Exception as exc:
        return f"[could not extract text from {filename}: {exc}]"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[... truncated: file continues for {len(text) - max_chars:,} more characters]"
    return text


def is_placeholder(text: str) -> bool:
    """True for extract_text's own "[unsupported/could not extract/image]"
    notes, i.e. nothing real came out of the file."""
    return text.startswith("[") and text.endswith("]") and "\n" not in text


def text_hash(text: str) -> str:
    """Identity of a document's *content*: the same agreement exported twice
    (different bytes, same words) hashes the same."""
    return hashlib.sha256(re.sub(r"\W+", "", text.lower()).encode()).hexdigest()


def _extract_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        pages.append(f"[page {i}]\n{page.extract_text() or ''}")
    return "\n".join(pages)


def _extract_docx(data: bytes) -> str:
    """Paragraphs *and* tables, in document order -- term sheets, cap tables,
    and contract schedules are usually tables, which the old
    paragraphs-only extraction silently dropped."""
    doc = DocxDocument(io.BytesIO(data))
    lines = []
    for el in doc.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = DocxParagraph(el, doc).text.strip()
            if text:
                lines.append(text)
        elif tag == "tbl":
            lines.append(_docx_table_text(DocxTable(el, doc)))
    return "\n".join(lines)


def _docx_table_text(table) -> str:
    rows = []
    for row in table.rows:
        cells, seen = [], set()
        for cell in row.cells:
            if id(cell._tc) in seen:
                continue  # a merged cell repeats the same element
            seen.add(id(cell._tc))
            cells.append(" ".join(cell.text.split()))
        if any(cells):
            rows.append(" | ".join(cells))
    return "[table]\n" + "\n".join(rows) + "\n[/table]"


def _extract_xlsx(data: bytes) -> str:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"# Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            # Keep empty cells as blanks (not dropped) so columns stay
            # aligned with their year/period headers in a financial model.
            cells = [_cell_text(c) for c in row]
            while cells and cells[-1] == "":
                cells.pop()
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _cell_text(c) -> str:
    """Float cells from a model come back with full binary precision
    (1234567.8900000001) -- round for readability without flattening small
    ratios/percentages (0.1234 must not become 0.12)."""
    if c is None:
        return ""
    if isinstance(c, float):
        if abs(c) >= 1000:
            return f"{c:,.0f}"
        if abs(c) >= 1:
            return f"{c:,.2f}".rstrip("0").rstrip(".")
        return f"{c:.4g}"
    return str(c)


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    lines = []
    for i, slide in enumerate(prs.slides, start=1):
        lines.append(f"[slide {i}]")
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if text:
                    lines.append(text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(" ".join(c.text.split()) for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"[speaker notes] {notes}")
    return "\n".join(lines)
