"""Local, free extraction of every page of every dataroom file.

Each file becomes a list of Pages (one per PDF page, spreadsheet sheet, or
slide; one for a short document), each recording how its text was obtained:

- "text":   the PDF's own text layer (more than MIN_PAGE_CHARS meaningful
            characters) -- free;
- "ocr":    no usable text layer (a scan, a signature page, an image-only
            page) -> rendered at OCR_DPI and read by Tesseract locally -- free;
- "vision": Tesseract found nothing or was unsure -> that ONE page goes to
            the cheap vision model (app/reading.py; capped per data room);
- "blank":  the page is visually empty (nothing to read);
- "none":   not read (no OCR available and over the vision cap) -- reported
            as a coverage problem, never silently dropped.

The old path sent a scanned PDF to the model as a document block trimmed to
5 pages and cut text documents to a few thousand characters; that is where
the report's "reviewed 5 of 56 pages" came from. Nothing here truncates.
"""
import email
import email.policy
import hashlib
import io
import logging
import os
import re
import shutil
from dataclasses import dataclass, field

log = logging.getLogger("ingest")

MIN_PAGE_CHARS = 100          # meaningful (alphanumeric) characters for a text layer to count
OCR_DPI = 250
OCR_MIN_CONFIDENCE = 60.0     # mean Tesseract word confidence below this -> vision fallback
OCR_MIN_CHARS = 40
EXTRACT_VERSION = 1           # bump to invalidate the cross-job extraction cache

_SIGNATURE = re.compile(
    r"docusign envelope id|envelope id:|signed by:|/s/\s*\w|electronically signed|carta.{0,40}(signed|signature)|"
    r"signature:\s*\S{3,}|in witness whereof",
    re.I,
)
_SIGNED_MARK = re.compile(r"docusign envelope id|envelope id:|signed by:|/s/\s*\w|electronically signed|"
                          r"date signed|signed on", re.I)
_SIGNATURE_BLOCK = re.compile(r"\bby:\s|\bname:\s|\btitle:\s|signature", re.I)
_BLANK_LINE_SIG = re.compile(r"by:\s*_{4,}|signature:\s*_{4,}", re.I)


@dataclass
class Page:
    no: int                 # 1-based page / sheet / slide number
    text: str
    method: str             # text | ocr | vision | blank | none | native
    label: str = ""         # e.g. sheet or slide name
    confidence: float | None = None
    is_signature: bool = False
    needs_vision: bool = False
    image_png: bytes | None = field(default=None, repr=False)   # only kept for needs_vision pages

    @property
    def text_hash(self) -> str:
        return hashlib.md5(re.sub(r"\W+", "", self.text.lower()).encode()).hexdigest()

    @property
    def extracted(self) -> bool:
        return self.method in ("text", "ocr", "vision", "blank", "native")


@dataclass
class Extraction:
    kind: str               # pdf | xlsx | docx | pptx | email | image | text | unsupported
    pages: list[Page]
    page_count: int
    error: str = ""

    @property
    def pages_extracted(self) -> int:
        return sum(1 for p in self.pages if p.extracted)

    @property
    def full_text(self) -> str:
        parts = []
        for p in self.pages:
            head = f"[page {p.no}{(' -- ' + p.label) if p.label else ''}]"
            parts.append(f"{head}\n{p.text}")
        return "\n".join(parts)

    @property
    def method_summary(self) -> str:
        counts: dict[str, int] = {}
        for p in self.pages:
            counts[p.method] = counts.get(p.method, 0) + 1
        return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def _meaningful(text: str) -> int:
    return len(re.sub(r"\W+", "", text))


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


# ------------------------------------------------------------------ OCR --

def _ocr(png: bytes) -> tuple[str, float]:
    """Tesseract text and mean word confidence for one page image."""
    import pytesseract
    from PIL import Image

    os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # many pages OCR in parallel across worker threads
    img = Image.open(io.BytesIO(png))
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 3")
    confs = [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    text = pytesseract.image_to_string(img, config="--psm 3")
    return text.strip(), (sum(confs) / len(confs) if confs else 0.0)


def _is_blank(pix) -> bool:
    """A page whose rendered image is (almost) all white."""
    samples = pix.samples
    step = max(1, len(samples) // 20000)
    sample = samples[::step]
    dark = sum(1 for b in sample if b < 200)
    return dark < len(sample) * 0.002


# ------------------------------------------------------------------ PDF --

def _table_markdown(page) -> str:
    try:
        tables = page.find_tables()
    except Exception:
        return ""
    out = []
    for t in getattr(tables, "tables", []) or []:
        try:
            rows = [[(c or "").replace("\n", " ").strip() for c in row] for row in t.extract()]
        except Exception:
            continue
        rows = [r for r in rows if any(r)]
        if len(rows) < 2 or len(rows[0]) < 2:
            continue
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        out.append("\n".join(lines))
    return "\n\n".join(out)


def extract_pdf(data: bytes, ocr: bool | None = None) -> Extraction:
    import pymupdf

    ocr = tesseract_available() if ocr is None else ocr
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        return Extraction("pdf", [], 0, error=f"could not open PDF: {exc}")
    if doc.needs_pass and not doc.authenticate(""):
        return Extraction("pdf", [], doc.page_count, error="password-protected PDF")
    pages = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text") or ""
        if _meaningful(text) > MIN_PAGE_CHARS:
            tables = _table_markdown(page) if "\t" in text or text.count("  ") > 20 else ""
            body = text.strip() + (f"\n\n[tables on this page]\n{tables}" if tables else "")
            pages.append(Page(i, body, "text", is_signature=bool(_SIGNATURE.search(text))))
            continue
        pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
        if _is_blank(pix):
            pages.append(Page(i, text.strip(), "blank"))
            continue
        png = pix.tobytes("png")
        if ocr:
            try:
                ocr_text, conf = _ocr(png)
            except Exception as exc:
                log.warning("OCR failed on page %s: %s", i, exc)
                ocr_text, conf = "", 0.0
            merged = (text.strip() + "\n" + ocr_text).strip()
            if _meaningful(ocr_text) >= OCR_MIN_CHARS and conf >= OCR_MIN_CONFIDENCE:
                pages.append(Page(i, merged, "ocr", confidence=round(conf, 1),
                                  is_signature=bool(_SIGNATURE.search(merged) or _SIGNATURE_BLOCK.search(merged))))
                continue
            pages.append(Page(i, merged, "none", confidence=round(conf, 1), needs_vision=True,
                              image_png=page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")))
        else:
            pages.append(Page(i, text.strip(), "none", needs_vision=True,
                              image_png=page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY).tobytes("png")))
    return Extraction("pdf", pages, doc.page_count)


# ----------------------------------------------------------- other kinds --

def _xlsx_pages(data: bytes) -> list[Page]:
    """Every sheet, as a compact pipe table with empty rows and columns
    dropped -- scenario tabs keep their names so they can be told apart."""
    import openpyxl

    from app.extract import _cell_text

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pages = []
    for i, ws in enumerate(wb.worksheets, start=1):
        rows = [[_cell_text(c) for c in row] for row in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c.strip() for c in r)]
        if rows:
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            keep = [j for j in range(width) if any(r[j].strip() for r in rows)]
            rows = [[r[j] for j in keep] for r in rows]
        text = "\n".join(" | ".join(r) for r in rows)
        pages.append(Page(i, text, "native", label=f"Sheet: {ws.title}"))
    return pages


def _pptx_pages(data: bytes) -> list[Page]:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    pages = []
    for i, slide in enumerate(prs.slides, start=1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if t:
                    lines.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(" ".join(c.text.split()) for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"[speaker notes] {notes}")
        pages.append(Page(i, "\n".join(lines), "native", label=f"Slide {i}"))
    return pages


def _email_text(data: bytes) -> str:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    head = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if msg[h])
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    return head + "\n\n" + text.strip() + (f"\n\n[attachments: {', '.join(attachments)}]" if attachments else "")


def _msg_text(data: bytes) -> str:
    import extract_msg

    m = extract_msg.Message(io.BytesIO(data))
    try:
        head = f"From: {m.sender}\nTo: {m.to}\nDate: {m.date}\nSubject: {m.subject}"
        return head + "\n\n" + (m.body or "").strip()
    finally:
        m.close()


def _image_pages(data: bytes, ocr: bool) -> list[Page]:
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    if ocr:
        try:
            text, conf = _ocr(png)
        except Exception:
            text, conf = "", 0.0
        if _meaningful(text) >= OCR_MIN_CHARS and conf >= OCR_MIN_CONFIDENCE:
            return [Page(1, text, "ocr", confidence=round(conf, 1))]
    if max(img.size) > 1600:
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
    return [Page(1, "", "none", needs_vision=True, image_png=png)]


def _chunk_pages(text: str, size: int = 12_000) -> list[Page]:
    """Long flowing documents (docx, email, txt) as pseudo-pages, so page
    references and the fact-card chunking work the same as for PDFs."""
    if not text.strip():
        return [Page(1, "", "blank")]
    pages, start, n = [], 0, 1
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            end = cut if cut > start else end
        pages.append(Page(n, text[start:end].strip(), "native", label=f"part {n}"))
        start, n = end, n + 1
    return pages


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp")


def extract_document(filename: str, data: bytes, ocr: bool | None = None) -> Extraction:
    """Every page of one file. Never raises: failures come back as an
    Extraction with `error` set and no pages (a coverage problem)."""
    ocr = tesseract_available() if ocr is None else ocr
    lower = filename.lower()
    try:
        if lower.endswith(".pdf"):
            return extract_pdf(data, ocr)
        if lower.endswith((".xlsx", ".xlsm")):
            pages = _xlsx_pages(data)
            return Extraction("xlsx", pages, len(pages))
        if lower.endswith(".pptx"):
            pages = _pptx_pages(data)
            return Extraction("pptx", pages, len(pages))
        if lower.endswith(".docx"):
            from app.extract import _extract_docx
            pages = _chunk_pages(_extract_docx(data))
            return Extraction("docx", pages, len(pages))
        if lower.endswith(".eml"):
            pages = _chunk_pages(_email_text(data))
            return Extraction("email", pages, len(pages))
        if lower.endswith(".msg"):
            pages = _chunk_pages(_msg_text(data))
            return Extraction("email", pages, len(pages))
        if lower.endswith((".txt", ".md", ".csv")):
            pages = _chunk_pages(data.decode("utf-8", errors="replace"))
            return Extraction("text", pages, len(pages))
        if lower.endswith(IMAGE_EXTS):
            return Extraction("image", _image_pages(data, ocr), 1)
    except Exception as exc:
        return Extraction("unsupported", [], 1, error=f"could not extract: {exc}"[:300])
    return Extraction("unsupported", [], 1, error=f"unsupported file type: {filename}")


# ------------------------------------------------------------ signatures --

AGREEMENT_HINT = re.compile(r"agreement|\bnote\b|warrant|consent|resolution|offer letter|side letter|"
                            r"in witness whereof|certificate|license|assignment|amendment|term sheet", re.I)


def execution_status(filename: str, pages: list[Page]) -> tuple[str, list[int]]:
    """'yes' / 'no' / 'unclear' / 'n/a' for whether an agreement-type
    document appears executed, and the signature page numbers. Evidence:
    e-signature markers (DocuSign/Carta envelope IDs, "/s/", "Signed by")
    or a filename saying signed/executed are 'yes'. A form, draft or
    template by name, with no signing marks, is 'no'. Anything else is
    'unclear' -- including blank "By: ____" lines, because a wet-ink or
    pasted signature is an image the text layer cannot show, and calling
    a signed agreement unsigned is exactly the false claim to avoid.
    Unclear signature pages are checked visually (app/reading.py)."""
    head = filename + "\n" + "\n".join(p.text for p in pages[:2])
    if not AGREEMENT_HINT.search(head):
        return "n/a", []
    sig_pages = [p.no for p in pages if p.is_signature or _SIGNATURE_BLOCK.search(p.text[-1500:] if p.text else "")]
    tail_text = "\n".join(p.text for p in pages if p.no in sig_pages[-3:])
    if (_SIGNED_MARK.search(tail_text) or re.search(r"\bsigned\b|executed|countersigned", filename, re.I)) \
            and not re.search(r"to be (signed|executed)|unsigned", filename, re.I):
        return "yes", sig_pages
    if re.search(r"\bdraft\b|\bform of\b|template|unsigned|to be (signed|executed)|redline", filename, re.I):
        return "no", sig_pages
    return "unclear", sig_pages
