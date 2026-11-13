"""Builds reference/style_guide.json from a finished, client-approved due
diligence report (.docx) -- the per-section style examples every Stage 2
drafting call is shown (see app/reference_report.py).

Each Heading 1 report is split at its Heading 2 subsections. The text of a
report before its first Heading 2 (header block, basis of preparation,
Summary of Findings box and any lead-in material) is stored under the
special subsection key "_intro". Tables are flattened to pipe-separated
rows and Heading 3s to "### " lines so the model sees each section's real
shape -- which tables it uses, where the callout box goes, how risks and
follow-ups are laid out -- not just its prose.

Usage: python scripts/build_style_guide.py "<path to reference .docx>"
"""
import json
import re
import sys

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

OUT_PATH = "reference/style_guide.json"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p: Paragraph) -> str:
    text = p.text.strip()
    if not text:
        return ""
    xml = p._p.xml
    style = p.style.name
    if style == "Heading 3":
        return f"### {text}"
    if 'w:fill="eaf2f5"' in xml or ('w:pBdr' in xml and 'w:shd w:fill' in xml and style == "normal"):
        return f"[CALLOUT BOX] {text}"
    if "<w:numPr>" in xml:
        return f"- {text}"
    if 'w:color w:val="555555"' in xml:
        return f"[SOURCE NOTE] {text}"
    if '<w:u w:val="single"/>' in xml and len(text) < 80:
        return f"[LABEL] {text}"
    return text


def _table_text(t: Table) -> str:
    rows = []
    for r in t.rows:
        cells = []
        seen = set()
        for c in r.cells:
            if id(c._tc) in seen:
                continue  # merged cells repeat the same element
            seen.add(id(c._tc))
            cells.append(" ".join(c.text.split()))
        rows.append(" | ".join(cells))
    return "[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]"


def build(path: str) -> dict:
    document = docx.Document(path)
    guide: dict[str, dict[str, list[str]]] = {}
    h1 = h2 = None
    for el in document.element.body.iterchildren():
        if el.tag == f"{W}p":
            p = Paragraph(el, document)
            style = p.style.name
            text = p.text.strip()
            if style == "Heading 1" and text:
                h1, h2 = text, "_intro"
                guide.setdefault(h1, {}).setdefault(h2, [])
                continue
            if style == "Heading 2" and text and h1:
                h2 = text
                guide[h1].setdefault(h2, [])
                continue
            if h1 is None:
                continue  # cover page, table of contents, disclaimer
            line = _para_text(p)
            # Version-update notices describe that report's own revision
            # history, not a pattern to imitate.
            if line and not re.match(r"^[A-Z][a-z]+ \d{1,2}, \d{4} update\.", line):
                guide[h1][h2].append(line)
        elif el.tag == f"{W}tbl" and h1:
            guide[h1][h2].append(_table_text(Table(el, document)))
    return {h1: {h2: "\n".join(lines) for h2, lines in subs.items()} for h1, subs in guide.items()}


if __name__ == "__main__":
    result = build(sys.argv[1])
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    total = sum(len(t) for subs in result.values() for t in subs.values())
    print(f"wrote {OUT_PATH}: {len(result)} reports, {total:,} chars")
    for h1, subs in result.items():
        print(f"  {h1}: {', '.join(subs)}")
