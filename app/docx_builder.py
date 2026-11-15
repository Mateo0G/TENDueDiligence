"""Renders a job's report_sections into a .docx matching the client-approved
CARI Health report's formatting (measured from its XML):

- Body: Calibri 10.5pt, justified, 6pt after, 1.1 line spacing; 0.5in side
  margins.
- Heading 1 (one per report): 18pt bold navy #1F3A5F, centered, starts a
  new page, navy rule underneath. Under it: the report header block (teal
  "{Company}  |  {X Section of the Due Diligence Report}", "Prepared by:
  TEN Capital Network  |  {date}", italic "Basis of preparation: ...").
- Heading 2 (subsections): 13pt bold navy. Heading 3: 11pt bold teal
  #1A7F8E.
- Run-in labels ("Due Diligence Team Assessment"): bold underlined.
- Summary of Findings box: teal left rule, #EAF2F5 shading, bold navy label.
- Tables: full width, fixed layout, light grey #BFBFBF grid, navy header
  row with white bold 9pt text, 9pt body with zebra rows, header repeats on
  each page.
- Source notes: 9pt italic grey #555555.
- Lists: numbered or bulleted with a hanging indent.
- TOC field over Heading 1-2, refreshed by Word on open.
"""
import io
import os
import re
from datetime import date

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.image.image import Image as DocxImage
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor, Twips

from app.reference_report import get_disclaimer_text

FONT_NAME = "Calibri"
BODY_PT = 10.5
TABLE_PT = 9
NAVY = "1F3A5F"
TEAL = "1A7F8E"
CALLOUT_FILL = "EAF2F5"
ZEBRA_FILL = "F5F7F8"
GRID = "BFBFBF"
NOTE_GREY = "555555"
TABLE_WIDTH_DXA = 10800  # 7.5in: the full text width at 0.5in margins

_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
_TEN_LOGO_PATH = os.path.join(_ASSETS_DIR, "ten_capital_logo.png")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _scaled_dimensions(image_bytes: bytes, max_width_in: float, max_height_in: float) -> tuple[Emu, Emu]:
    """(width, height) for image_bytes scaled to fit both caps, aspect ratio
    preserved (CSS `object-fit: contain`)."""
    image = DocxImage.from_blob(image_bytes)
    native_width, native_height = image.width, image.height
    scale = min(Inches(max_width_in) / native_width, Inches(max_height_in) / native_height, 1.0)
    return Emu(round(native_width * scale)), Emu(round(native_height * scale))


def _set_fonts(rPr_owner_font, rPr) -> None:
    """Explicit Calibri in every font slot, and drop theme-font references
    (the default template's headings point at the theme's Calibri Light,
    which would override the explicit name)."""
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if rFonts.get(qn(attr)) is not None:
            del rFonts.attrib[qn(attr)]
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rFonts.set(qn(attr), FONT_NAME)


def _style_run(run, size_pt=BODY_PT, bold=None, italic=None, color=None, underline=None):
    run.font.name = FONT_NAME
    run.font.size = Pt(size_pt)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if underline is not None:
        run.font.underline = underline
    if color:
        run.font.color.rgb = _rgb(color)
    _set_fonts(run.font, run._element.get_or_add_rPr())


def _add_rich_text(paragraph, text: str, size_pt=BODY_PT, bold=False, italic=False, color=None):
    """Adds `text` as runs, rendering **double asterisks** as bold."""
    pos = 0
    for m in _BOLD_RE.finditer(text):
        if m.start() > pos:
            _style_run(paragraph.add_run(text[pos:m.start()]), size_pt, bold, italic, color)
        _style_run(paragraph.add_run(m.group(1)), size_pt, True, italic, color)
        pos = m.end()
    if pos < len(text) or not text:
        _style_run(paragraph.add_run(text[pos:]), size_pt, bold, italic, color)


def _spacing(paragraph, before=None, after=None, line=None):
    pf = paragraph.paragraph_format
    if before is not None:
        pf.space_before = Pt(before)
    if after is not None:
        pf.space_after = Pt(after)
    if line is not None:
        pf.line_spacing = line


def _pPr_append(paragraph, xml_tag: str, attrs: dict, children: list[tuple[str, dict]] = ()):
    el = OxmlElement(xml_tag)
    for k, v in attrs.items():
        el.set(qn(k), v)
    for child_tag, child_attrs in children:
        child = OxmlElement(child_tag)
        for k, v in child_attrs.items():
            child.set(qn(k), v)
        el.append(child)
    paragraph._p.get_or_add_pPr().append(el)
    return el


# ---------------------------------------------------------------- styles --

def _configure_style(style, size_pt, bold=False, color=None, italic=False):
    style.font.name = FONT_NAME
    style.font.size = Pt(size_pt)
    style.font.bold = bold
    style.font.italic = italic
    style.font.color.rgb = _rgb(color) if color else None
    _set_fonts(style.font, style.element.get_or_add_rPr())


def _configure_styles(doc):
    normal = doc.styles["Normal"]
    _configure_style(normal, BODY_PT)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    h1 = doc.styles["Heading 1"]
    _configure_style(h1, 18, bold=True, color=NAVY)
    pf = h1.paragraph_format
    pf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pf.page_break_before = True
    pf.space_before = Pt(0)
    pf.space_after = Pt(4)
    pf.keep_with_next = True
    pPr = h1.element.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in {"w:val": "single", "w:sz": "8", "w:space": "4", "w:color": NAVY}.items():
        bottom.set(qn(k), v)
    pBdr.append(bottom)
    pPr.append(pBdr)

    h2 = doc.styles["Heading 2"]
    _configure_style(h2, 13, bold=True, color=NAVY)
    h2.paragraph_format.space_before = Pt(12)
    h2.paragraph_format.space_after = Pt(4)
    h2.paragraph_format.keep_with_next = True
    h2.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT

    h3 = doc.styles["Heading 3"]
    _configure_style(h3, 11, bold=True, color=TEAL)
    h3.paragraph_format.space_before = Pt(9)
    h3.paragraph_format.space_after = Pt(3)
    h3.paragraph_format.keep_with_next = True
    h3.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT

    title = doc.styles["Title"]
    _configure_style(title, 28, bold=True, color=NAVY)
    title.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    # The default Title style carries a blue bottom rule; the cover has none.
    t_pPr = title.element.get_or_add_pPr()
    for el in t_pPr.findall(qn("w:pBdr")):
        t_pPr.remove(el)

    # TOC entry styles, so the generated contents page matches the body font.
    for name, size, bold in (("TOC 1", 11, True), ("TOC 2", 10, False)):
        try:
            style = doc.styles[name]
        except KeyError:
            from docx.enum.style import WD_STYLE_TYPE
            style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
            style.base_style = normal
        _configure_style(style, size, bold=bold)
        style.paragraph_format.space_after = Pt(2)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        if name == "TOC 2":
            style.paragraph_format.left_indent = Inches(0.25)


def _configure_page(doc):
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)
    section.top_margin = Inches(0.6)
    section.bottom_margin = Inches(0.5)


def _add_field(paragraph, instruction: str, placeholder: str = "", size_pt=BODY_PT):
    run = paragraph.add_run()
    _style_run(run, size_pt)
    r = run._element
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    r.append(begin)
    r.append(instr)
    r.append(separate)
    if placeholder:
        t = OxmlElement("w:t")
        t.text = placeholder
        r.append(t)
    r.append(end)


TOC_INSTRUCTION = r'TOC \o "1-2" \h \z \u'


def _fld(kind: str):
    el = OxmlElement("w:fldChar")
    el.set(qn("w:fldCharType"), kind)
    return el


def _add_toc(doc, entries: list[tuple[int, str]]):
    """A TOC field whose result is already filled in with the report's
    headings (level 1 and 2), so the contents page is never empty -- even in
    viewers that don't update fields (previews, Google Docs) or when Word's
    "update fields?" prompt is declined. Word still refreshes it on open
    (w:updateFields), adding page numbers."""
    if not entries:
        toc = doc.add_paragraph()
        _add_field(toc, TOC_INSTRUCTION, "Right-click and choose Update Field to generate the table of contents.")
        return
    paragraphs = []
    for level, title in entries:
        p = doc.add_paragraph(style="TOC 1" if level <= 1 else "TOC 2")
        paragraphs.append((p, title))
    first, _ = paragraphs[0]
    run = first.add_run()
    r = run._element
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = TOC_INSTRUCTION
    r.append(_fld("begin"))
    r.append(instr)
    r.append(_fld("separate"))
    for p, title in paragraphs:
        p.add_run(title)
    end_run = paragraphs[-1][0].add_run()
    end_run._element.append(_fld("end"))


def toc_entries(report_sections: list[dict], appendices: list[tuple[int, str]] = ()) -> list[tuple[int, str]]:
    out = [((s.get("heading_level") or 1), s["title"]) for s in report_sections if (s.get("heading_level") or 1) <= 2]
    return out + list(appendices)


def _add_footer(doc, company_name):
    """TEN Capital Network mark left, report title centered, page number
    right -- on every page."""
    section = doc.sections[0]
    footer = section.footer
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    usable_width_in = (section.page_width - section.left_margin - section.right_margin) / 914400
    with open(_TEN_LOGO_PATH, "rb") as f:
        logo_bytes = f.read()
    width, height = _scaled_dimensions(logo_bytes, max_width_in=1.1, max_height_in=0.4)
    p.add_run().add_picture(io.BytesIO(logo_bytes), width=width, height=height)
    tab_stops = p.paragraph_format.tab_stops
    tab_stops.add_tab_stop(Inches(usable_width_in / 2), alignment=WD_TAB_ALIGNMENT.CENTER)
    tab_stops.add_tab_stop(Inches(usable_width_in), alignment=WD_TAB_ALIGNMENT.RIGHT)
    _style_run(p.add_run(f"\t{company_name} – Due Diligence Report\t"), 9, color=NAVY)
    _add_field(p, "PAGE", size_pt=9)


def _force_field_update_on_open(doc):
    """Word recomputes the TOC (and page numbers in it) when the file is
    opened, instead of showing the field's placeholder text."""
    update_fields = OxmlElement("w:updateFields")
    update_fields.set(qn("w:val"), "true")
    doc.settings.element.append(update_fields)


# ---------------------------------------------------------------- blocks --

def _shade_cell(cell, fill: str):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _set_cell_width(cell, dxa: int):
    tcPr = cell._tc.get_or_add_tcPr()
    tcW = tcPr.find(qn("w:tcW"))
    if tcW is None:
        tcW = OxmlElement("w:tcW")
        tcPr.insert(0, tcW)
    tcW.set(qn("w:w"), str(dxa))
    tcW.set(qn("w:type"), "dxa")


def _column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    """Fixed-layout column widths proportional to each column's typical
    content length (clamped, so one long cell can't starve the others)."""
    n = len(headers)
    weights = []
    for c in range(n):
        lengths = [len(headers[c])] + [len(r[c]) for r in rows if c < len(r)]
        avg = sum(lengths) / len(lengths)
        weights.append(min(max(avg, 8), 70))
    total = sum(weights)
    widths = [int(TABLE_WIDTH_DXA * w / total) for w in weights]
    widths[-1] += TABLE_WIDTH_DXA - sum(widths)
    return widths


def _add_table_block(doc, headers: list[str], rows: list[list[str]], caption: str = ""):
    if not headers:
        return
    n = len(headers)
    rows = [(list(r) + [""] * n)[:n] for r in rows]
    if caption:
        cap = doc.add_paragraph()
        _spacing(cap, before=6, after=3)
        cap.paragraph_format.keep_with_next = True
        cap.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _add_rich_text(cap, caption, size_pt=BODY_PT, bold=True, color=NAVY)

    table = doc.add_table(rows=1 + len(rows), cols=n)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl = table._tbl
    tblPr = tbl.tblPr
    tblW = tblPr.find(qn("w:tblW"))
    if tblW is None:
        tblW = OxmlElement("w:tblW")
        tblPr.append(tblW)
    tblW.set(qn("w:w"), str(TABLE_WIDTH_DXA))
    tblW.set(qn("w:type"), "dxa")
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        for k, v in {"w:val": "single", "w:sz": "4", "w:space": "0", "w:color": GRID}.items():
            el.set(qn(k), v)
        borders.append(el)
    tblPr.append(borders)
    cell_mar = OxmlElement("w:tblCellMar")
    for edge, w in (("left", 80), ("right", 80), ("top", 30), ("bottom", 30)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:w"), str(w))
        el.set(qn("w:type"), "dxa")
        cell_mar.append(el)
    tblPr.append(cell_mar)

    widths = _column_widths(headers, rows)
    for gc, w in zip(tbl.tblGrid.findall(qn("w:gridCol")), widths):
        gc.set(qn("w:w"), str(w))

    # Header row: repeats on each page, never split.
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    for tag in ("w:tblHeader", "w:cantSplit"):
        el = OxmlElement(tag)
        tr_pr.append(el)

    def fill_cell(cell, text, width, header, zebra):
        _set_cell_width(cell, width)
        if header:
            _shade_cell(cell, NAVY)
        elif zebra:
            _shade_cell(cell, ZEBRA_FILL)
        para = cell.paragraphs[0]
        para.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _spacing(para, before=0, after=1, line=1.0)
        lines = (text or "").split("\n")
        for i, line in enumerate(lines):
            if i:
                para = cell.add_paragraph()
                para.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
                _spacing(para, before=0, after=1, line=1.0)
            _add_rich_text(para, line, size_pt=TABLE_PT, bold=header, color="FFFFFF" if header else None)

    for c, h in enumerate(headers):
        fill_cell(table.rows[0].cells[c], h, widths[c], True, False)
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row):
            fill_cell(table.rows[r].cells[c], value, widths[c], False, r % 2 == 0)

    spacer = doc.add_paragraph()
    _spacing(spacer, before=0, after=2)


def _add_callout(doc, label: str, text: str):
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.left_indent = Twips(200)
    p.paragraph_format.right_indent = Twips(100)
    _spacing(p, before=4, after=8)
    _pPr_append(p, "w:pBdr", {}, [("w:left", {"w:val": "single", "w:sz": "24", "w:space": "8", "w:color": TEAL})])
    _pPr_append(p, "w:shd", {"w:val": "clear", "w:color": "auto", "w:fill": CALLOUT_FILL})
    label = (label or "").strip()
    if label:
        _style_run(p.add_run(label + "  "), BODY_PT, bold=True, color=NAVY)
    _add_rich_text(p, text)


def _add_label(doc, text: str):
    """Bold underlined run-in label. The two report-level labels are 12pt,
    like the reference; inline ones ("Strengths observed:") stay body size."""
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.keep_with_next = True
    major = text.startswith("Due Diligence Team Assessment") or text.startswith("Details Provided by")
    _spacing(p, before=10 if major else 6, after=5 if major else 3)
    _style_run(p.add_run(text), 12 if major else BODY_PT, bold=True, underline=True)


def _add_list(doc, items: list[str], numbered: bool):
    for i, item in enumerate(items, start=1):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.left_indent = Twips(360)
        pf.first_line_indent = Twips(-240)
        pf.tab_stops.add_tab_stop(Twips(360))
        _spacing(p, before=0, after=3)
        marker = f"{i}." if numbered else "•"
        _style_run(p.add_run(f"{marker}\t"), BODY_PT, bold=numbered, color=NAVY if numbered else None)
        _add_rich_text(p, item)


def _add_note(doc, text: str):
    p = doc.add_paragraph()
    p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _spacing(p, before=6, after=6)
    _add_rich_text(p, text, size_pt=TABLE_PT, italic=True, color=NOTE_GREY)


def _add_content_block(doc, block: dict):
    btype = block.get("type")
    if btype == "paragraph":
        _add_rich_text(doc.add_paragraph(), block.get("text", ""))
    elif btype == "heading":
        level = block.get("level", 3)
        if level == 4:
            _add_label(doc, block.get("text", ""))
        else:
            p = doc.add_paragraph(style="Heading 3")
            _style_run(p.add_run(block.get("text", "")), 11, bold=True, color=TEAL)
    elif btype == "table":
        _add_table_block(doc, block.get("headers", []), block.get("rows", []), block.get("caption", ""))
    elif btype == "callout":
        _add_callout(doc, block.get("label", ""), block.get("text", ""))
    elif btype == "bullets":
        _add_list(doc, block.get("items", []), bool(block.get("numbered")))
    elif btype == "note":
        _add_note(doc, block.get("text", ""))


# ----------------------------------------------------------- structure --

def _report_date() -> str:
    today = date.today()
    return f"{today.strftime('%B')} {today.day}, {today.year}"


def _add_report_header(doc, meta: dict):
    header = (meta or {}).get("header")
    basis = ((meta or {}).get("basis") or "").strip()
    if not header and not basis:
        return
    if header:
        p = doc.add_paragraph()
        p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _spacing(p, before=4, after=0)
        _style_run(p.add_run(header), 12, bold=True, color=TEAL)
        p2 = doc.add_paragraph()
        p2.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _spacing(p2, before=0, after=6)
        _style_run(p2.add_run(f"Prepared by: TEN Capital Network  |  {_report_date()}"), BODY_PT, color=NOTE_GREY)
    if basis:
        basis = re.sub(r"^\**basis of preparation[:.]?\**\s*", "", basis, flags=re.I)
        p3 = doc.add_paragraph()
        p3.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _spacing(p3, before=0, after=6)
        _style_run(p3.add_run("Basis of preparation: "), BODY_PT, bold=True, italic=True)
        _add_rich_text(p3, basis, italic=True)


def _build_cover_and_front_matter(doc, company_name: str, logo_bytes: bytes | None = None,
                                  toc: list[tuple[int, str]] | None = None):
    _add_footer(doc, company_name)

    for _ in range(4):
        doc.add_paragraph()
    if logo_bytes:
        logo_para = doc.add_paragraph()
        logo_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        width, height = _scaled_dimensions(logo_bytes, max_width_in=4.5, max_height_in=1.75)
        logo_para.add_run().add_picture(io.BytesIO(logo_bytes), width=width, height=height)
        doc.add_paragraph()

    name = doc.add_paragraph(style="Title")
    _style_run(name.add_run(company_name), 28, bold=True, color=NAVY)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _spacing(sub, before=6, after=24)
    _style_run(sub.add_run("Due Diligence Report"), 20, color=TEAL)
    for text, size, color in (
        ("Prepared by TEN Capital Network", 12, NAVY),
        (_report_date(), 12, NOTE_GREY),
        ("Confidential", 11, NOTE_GREY),
    ):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _spacing(p, before=0, after=4)
        _style_run(p.add_run(text), size, bold=(text == "Confidential"), color=color)

    # Contents on its own page.
    brk = doc.add_paragraph()
    brk.add_run().add_break(WD_BREAK.PAGE)
    toc_title = doc.add_paragraph()
    toc_title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _spacing(toc_title, before=0, after=8)
    _style_run(toc_title.add_run("Contents"), 18, bold=True, color=NAVY)
    _add_toc(doc, toc or [])

    disclaimer = get_disclaimer_text()
    if disclaimer:
        brk = doc.add_paragraph()
        brk.add_run().add_break(WD_BREAK.PAGE)
        label = doc.add_paragraph()
        label.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _style_run(label.add_run("TEN Capital Network Disclaimer"), 12, bold=True, color=NAVY)
        for para in disclaimer.split("\n\n"):
            p = doc.add_paragraph()
            _add_rich_text(p, para, size_pt=9.5)
    # No trailing page break: every Heading 1 starts its own page.


def _add_coverage(doc, coverage: dict | None):
    """Front-matter statement of what was read, and a table of any file
    that could not be read in full -- so a coverage gap is visible on page
    one instead of surfacing as a wrong "missing document" finding."""
    if not coverage or not coverage.get("pages_total"):
        return
    brk = doc.add_paragraph()
    brk.add_run().add_break(WD_BREAK.PAGE)
    label = doc.add_paragraph()
    _style_run(label.add_run("Data Room Coverage"), 12, bold=True, color=NAVY)
    by = coverage.get("pages_by_method") or {}
    text_pages = by.get("text", 0) + by.get("native", 0)
    p = doc.add_paragraph()
    summary = (f"The Due Diligence Team's review covered {coverage['files']} files and "
               f"{coverage['pages_extracted']:,} of {coverage['pages_total']:,} pages: {text_pages:,} from the "
               f"documents' text, {by.get('ocr', 0):,} by OCR of scanned pages and {by.get('vision', 0):,} read "
               f"visually; {by.get('blank', 0):,} pages were blank.")
    if not coverage.get("short"):
        summary += " Every document was read in full."
    _add_rich_text(p, summary, size_pt=9.5)
    if coverage.get("short"):
        _add_table_block(doc, ["Document", "Pages read", "Problem"],
                         [[s["file"], f"{s['pages_extracted']} of {s['pages_total']}", s["problem"]]
                          for s in coverage["short"]],
                         "Documents that could not be read in full (review manually)")


def _add_report_sections(doc, report_sections: list[dict]):
    for section in report_sections:
        level = section.get("heading_level") or 1
        if level == 1:
            doc.add_heading(section["title"], level=1)
            _add_report_header(doc, section.get("meta") or {})
        else:
            doc.add_heading(section["title"], level=2)
        for block in section["content"]:
            _add_content_block(doc, block)


_BLOCK_LABELS = {
    "dd_assessment": "DD Assessment",
    "life_science_assessment": "Life Science Assessment",
    "master_validation": "Master Validation",
}


def _add_qa_report_card(doc, qa_scores: list[dict]):
    """Stage 6 appendix: every Stage 4 score, grouped by block; the master
    validation check stays on its own 1-10 scale, never blended in."""
    doc.add_heading("Due Diligence Assessment Report Card", level=1)
    report_card = next((s for s in qa_scores if s["assessment_name"] == "Report Card of scores"), None)
    if report_card:
        _add_callout(doc, "Overall average score.", str(report_card["findings"].get("overall_average")))

    for block_key in ("dd_assessment", "life_science_assessment", "master_validation"):
        rows = [
            s for s in qa_scores
            if s["block"] == block_key and s["assessment_name"] != "Report Card of scores"
        ]
        if not rows:
            continue
        doc.add_heading(f"{_BLOCK_LABELS[block_key]} Scores", level=2)
        _add_table_block(
            doc,
            headers=["Assessment", "Score", "Scale", "Summary"],
            rows=[
                [
                    r["assessment_name"],
                    str(r["score"]) if r["score"] is not None else "N/A",
                    r["scale"],
                    (r["findings"] or {}).get("summary", ""),
                ]
                for r in rows
            ],
        )


def _add_gap_analysis_and_mitigation(doc, gap_analysis: dict | None, mitigation_strategy: dict | None):
    """Stage 6 appendix folding in Stage 5's output: each identified gap
    paired with its proposed mitigation."""
    if not gap_analysis:
        return
    doc.add_heading("Gap Analysis & Mitigation Strategy", level=1)
    _add_callout(doc, "Summary.", gap_analysis.get("summary", ""))

    mitigations_by_gap = {}
    if mitigation_strategy:
        for m in mitigation_strategy.get("mitigations", []):
            mitigations_by_gap[m["gap_title"]] = m["mitigation"]

    _add_table_block(
        doc,
        headers=["Gap", "Severity", "Description", "Mitigation"],
        rows=[
            [
                g["title"],
                g["severity"],
                g["description"],
                mitigations_by_gap.get(g["title"], "(mitigation not yet generated)"),
            ]
            for g in gap_analysis.get("gaps", [])
        ],
    )


def _new_document():
    doc = docx.Document()
    _configure_styles(doc)
    _configure_page(doc)
    _force_field_update_on_open(doc)
    return doc


def _to_bytes(doc) -> bytes:
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def build_report_docx(company_name: str, report_sections: list[dict], logo_bytes: bytes | None = None,
                      coverage: dict | None = None) -> bytes:
    """The Stage 3/4 review-checkpoint draft: cover, contents, disclaimer,
    and every drafted report. report_sections: rows from
    repo.list_report_sections, already ordered by order_index."""
    doc = _new_document()
    _build_cover_and_front_matter(doc, company_name, logo_bytes, toc_entries(report_sections))
    _add_coverage(doc, coverage)
    _add_report_sections(doc, report_sections)
    return _to_bytes(doc)


def build_final_report_docx(
    company_name: str,
    report_sections: list[dict],
    qa_scores: list[dict],
    gap_analysis: dict | None,
    mitigation_strategy: dict | None,
    logo_bytes: bytes | None = None,
    coverage: dict | None = None,
) -> bytes:
    """The Stage 6 final render: the same reports plus the QA Report Card
    and Gap Analysis/Mitigation appendices."""
    doc = _new_document()
    appendices = [(1, "Due Diligence Assessment Report Card")]
    appendices += [(2, f"{_BLOCK_LABELS[b]} Scores") for b in ("dd_assessment", "life_science_assessment",
                                                               "master_validation")
                   if any(s["block"] == b and s["assessment_name"] != "Report Card of scores" for s in qa_scores)]
    if gap_analysis:
        appendices.append((1, "Gap Analysis & Mitigation Strategy"))
    _build_cover_and_front_matter(doc, company_name, logo_bytes, toc_entries(report_sections, appendices))
    _add_coverage(doc, coverage)
    _add_report_sections(doc, report_sections)
    _add_qa_report_card(doc, qa_scores)
    _add_gap_analysis_and_mitigation(doc, gap_analysis, mitigation_strategy)
    return _to_bytes(doc)
