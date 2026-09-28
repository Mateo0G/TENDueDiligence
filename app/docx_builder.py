"""Renders a job's report_sections into a .docx matching the reference
report's exact styling (fonts, sizes, table borders) and structure (cover
page, disclaimer, live TOC field, 25-section body) -- per the Phase 0
decision to match the reference exactly, not just structurally.

Exact styling facts this replicates (extracted via scripts/inspect_docx_
styles*.py, see docs/template_structure.md):
- Default font Open Sans 11pt, docDefaults line spacing 1.15 / 10pt after.
- Heading 1: Open Sans, bold, 15pt, no space-after.
- Title: Open Sans, bold, 25pt. Subtitle: Open Sans, 20pt.
  (The reference file's own Title/Subtitle *paragraphs* carry a direct
  13pt run-level override that contradicts their own style definitions --
  judged to be an authoring artifact, not intentional design, so this
  generator uses the style-defined sizes instead of replicating it.)
- Tables: plain single black 0.5pt borders (outer + inside), header row
  bold + vertically centered, body rows vertically centered, no shading.
  No named table style is used (the reference's "Table1" style reference
  doesn't resolve to any defined style in its own styles.xml) -- borders
  are applied as direct table-level formatting instead.
"""
import io
from datetime import date

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, Inches

from app.reference_report import get_disclaimer_text

FONT_NAME = "Open Sans"


def _set_run_font(run, size_pt, bold=False):
    run.font.name = FONT_NAME
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    # East-Asian/complex-script font slots also need setting or some
    # renderers fall back to a theme default for those slots.
    # Note: `rFonts.find(...) or OxmlElement(...)` would be unsafe here --
    # lxml elements are falsy when childless (via __len__), not by identity,
    # so an existing-but-empty <w:rFonts/> would be silently replaced.
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rFonts.set(qn(attr), FONT_NAME)


def _configure_styles(doc):
    normal = doc.styles["Normal"]
    normal.font.name = FONT_NAME
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(10)

    heading1 = doc.styles["Heading 1"]
    heading1.font.name = FONT_NAME
    heading1.font.size = Pt(15)
    heading1.font.bold = True
    heading1.paragraph_format.space_after = Pt(0)

    title = doc.styles["Title"]
    title.font.name = FONT_NAME
    title.font.size = Pt(25)
    title.font.bold = True

    subtitle = doc.styles["Subtitle"]
    subtitle.font.name = FONT_NAME
    subtitle.font.size = Pt(20)
    subtitle.font.bold = False


def _configure_page(doc):
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)
    section.top_margin = Inches(0.6)
    section.bottom_margin = Inches(0.5)


def _add_footer(doc, company_name):
    footer = doc.sections[0].footer
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    run = p.add_run(f"{company_name} - Due Diligence Report")
    _set_run_font(run, 11)


def _add_toc_field(doc):
    """Inserts a live Word TOC field (Heading 1 only). Word regenerates the
    entries and page numbers when the user presses "Update Field" / F9 on
    first open -- python-docx cannot compute page numbers itself, and this
    is what the reference report itself does (see docs/template_structure.md).
    """
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()

    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")

    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = 'TOC \\h \\u \\z \\t "Heading 1,1"'

    fld_separate = OxmlElement("w:fldChar")
    fld_separate.set(qn("w:fldCharType"), "separate")

    placeholder = OxmlElement("w:t")
    placeholder.text = "Right-click and choose Update Field to generate the table of contents."

    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")

    r_element = run._element
    r_element.append(fld_begin)
    r_element.append(instr_text)
    r_element.append(fld_separate)
    r_element.append(placeholder)
    r_element.append(fld_end)


_TABLE_BORDER_XML = """
<w:tblBorders {nsdecls}>
  <w:top w:val="single" w:sz="4" w:space="0" w:color="000000"/>
  <w:left w:val="single" w:sz="4" w:space="0" w:color="000000"/>
  <w:bottom w:val="single" w:sz="4" w:space="0" w:color="000000"/>
  <w:right w:val="single" w:sz="4" w:space="0" w:color="000000"/>
  <w:insideH w:val="single" w:sz="4" w:space="0" w:color="000000"/>
  <w:insideV w:val="single" w:sz="4" w:space="0" w:color="000000"/>
</w:tblBorders>
"""


def _apply_table_borders(table):
    from docx.oxml.ns import nsdecls
    from lxml import etree

    tblPr = table._tbl.tblPr
    borders_xml = _TABLE_BORDER_XML.format(nsdecls=nsdecls("w"))
    borders_element = etree.fromstring(borders_xml)
    tblPr.append(borders_element)


def _set_cell_valign_center(cell):
    tcPr = cell._tc.get_or_add_tcPr()
    vAlign = OxmlElement("w:vAlign")
    vAlign.set(qn("w:val"), "center")
    tcPr.append(vAlign)


def _add_table_block(doc, headers, rows):
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    _apply_table_borders(table)

    for col_idx, header_text in enumerate(headers):
        cell = table.rows[0].cells[col_idx]
        cell.text = ""
        run = cell.paragraphs[0].add_run(header_text)
        _set_run_font(run, 11, bold=True)
        _set_cell_valign_center(cell)

    for row_idx, row in enumerate(rows, start=1):
        for col_idx, value in enumerate(row):
            if col_idx >= len(headers):
                break
            cell = table.rows[row_idx].cells[col_idx]
            cell.text = ""
            run = cell.paragraphs[0].add_run(value)
            _set_run_font(run, 11)
            _set_cell_valign_center(cell)

    doc.add_paragraph()  # spacer after table, matching reference spacing


def _add_content_block(doc, block: dict):
    btype = block.get("type")
    if btype == "paragraph":
        p = doc.add_paragraph()
        run = p.add_run(block["text"])
        _set_run_font(run, 11)
    elif btype == "heading":
        # No Heading 2 style in the reference template (see
        # docs/template_structure.md) -- internal sub-headings render as a
        # bolded lead-in line within Normal, matching the source exactly.
        p = doc.add_paragraph()
        run = p.add_run(block["text"])
        _set_run_font(run, 11, bold=True)
    elif btype == "table":
        _add_table_block(doc, block["headers"], block["rows"])


def _build_cover_and_front_matter(doc, company_name: str):
    _add_footer(doc, company_name)

    cover_title = doc.add_paragraph(style="Title")
    cover_title.add_run(company_name)
    doc.add_paragraph()
    title = doc.add_paragraph(style="Title")
    title.add_run("Due Diligence Report")
    subtitle = doc.add_paragraph(style="Subtitle")
    subtitle.add_run("Confidential")
    date_para = doc.add_paragraph()
    _set_run_font(date_para.add_run(date.today().strftime("%B %Y")), 11)

    _add_toc_field(doc)
    doc.add_paragraph()

    disclaimer_heading = doc.add_paragraph()
    _set_run_font(disclaimer_heading.add_run("TEN Capital Network Disclaimer:"), 11, bold=True)
    disclaimer_body = doc.add_paragraph()
    _set_run_font(disclaimer_body.add_run(get_disclaimer_text()), 11)

    doc.add_page_break()


def _add_report_sections(doc, report_sections: list[dict]):
    for section in report_sections:
        doc.add_heading(section["title"], level=1)
        for block in section["content"]:
            _add_content_block(doc, block)


_BLOCK_LABELS = {
    "dd_assessment": "DD Assessment",
    "life_science_assessment": "Life Science Assessment",
    "master_validation": "Master Validation",
}


def _add_qa_report_card(doc, qa_scores: list[dict]):
    """Stage 6 appendix: every Stage 4 score, grouped by block, plus each
    assessment's narrative findings. Report Card of scores (the aggregate,
    see app/tasks/report_card.py) is rendered as the summary table; the
    master validation check is kept on its own 1-10 scale, never blended
    into the same table (see docs/prompt_inventory.md Flag M1)."""
    doc.add_page_break()
    doc.add_heading("Due Diligence Assessment Report Card", level=1)

    report_card = next((s for s in qa_scores if s["assessment_name"] == "Report Card of scores"), None)
    if report_card:
        p = doc.add_paragraph()
        _set_run_font(
            p.add_run(f"Overall average score: {report_card['findings'].get('overall_average')}"),
            11,
            bold=True,
        )

    for block_key in ("dd_assessment", "life_science_assessment", "master_validation"):
        rows = [
            s for s in qa_scores
            if s["block"] == block_key and s["assessment_name"] != "Report Card of scores"
        ]
        if not rows:
            continue
        doc.add_heading(f"{_BLOCK_LABELS[block_key]} Scores", level=1)
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
    """Stage 6 appendix folding in Stage 5's output (docs/prompt_inventory.md
    §6): each identified gap paired with its proposed mitigation."""
    if not gap_analysis:
        return
    doc.add_page_break()
    doc.add_heading("Gap Analysis & Mitigation Strategy", level=1)

    summary_p = doc.add_paragraph()
    _set_run_font(summary_p.add_run(gap_analysis.get("summary", "")), 11)

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


def build_report_docx(company_name: str, report_sections: list[dict]) -> bytes:
    """The Stage 3/4 review-checkpoint draft: cover page + the 25 drafted
    sections only. report_sections: rows from repo.list_report_sections,
    already ordered by order_index."""
    doc = docx.Document()
    _configure_styles(doc)
    _configure_page(doc)
    _build_cover_and_front_matter(doc, company_name)
    _add_report_sections(doc, report_sections)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def build_final_report_docx(
    company_name: str,
    report_sections: list[dict],
    qa_scores: list[dict],
    gap_analysis: dict | None,
    mitigation_strategy: dict | None,
) -> bytes:
    """The Stage 6 final render: the same 25 sections, plus the QA Report
    Card and Gap Analysis/Mitigation appendices folded in, per the brief's
    "fold the QA report card and mitigation content into the document"
    instruction for Stage 6."""
    doc = docx.Document()
    _configure_styles(doc)
    _configure_page(doc)
    _build_cover_and_front_matter(doc, company_name)
    _add_report_sections(doc, report_sections)
    _add_qa_report_card(doc, qa_scores)
    _add_gap_analysis_and_mitigation(doc, gap_analysis, mitigation_strategy)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
