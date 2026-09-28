"""Best-effort text extraction from uploaded dataroom files, so their
content can go into a Claude prompt. Anything we can't extract text from
comes back as a short placeholder rather than raising -- a due diligence
dataroom always contains some binary noise (scans, images) and a drafting
prompt should still run on everything else.
"""
import io

import openpyxl
from docx import Document as DocxDocument
from pypdf import PdfReader

# Hard cap per file so one huge document can't blow the whole prompt's
# context budget. Real chunking/summarization is future work -- see
# docs/prompt_inventory.md's non-goals (no RAG for v1).
MAX_CHARS_PER_FILE = 20_000


def extract_text(filename: str, data: bytes) -> str:
    lower = filename.lower()
    try:
        if lower.endswith((".txt", ".md", ".csv")):
            text = data.decode("utf-8", errors="replace")
        elif lower.endswith(".pdf"):
            text = _extract_pdf(data)
        elif lower.endswith(".docx"):
            text = _extract_docx(data)
        elif lower.endswith(".xlsx"):
            text = _extract_xlsx(data)
        else:
            return f"[unsupported file type, {len(data)} bytes: {filename}]"
    except Exception as exc:
        return f"[could not extract text from {filename}: {exc}]"

    return text[:MAX_CHARS_PER_FILE]


def _extract_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(data: bytes) -> str:
    doc = DocxDocument(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_xlsx(data: bytes) -> str:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"# Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)
