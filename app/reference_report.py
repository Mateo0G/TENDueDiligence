"""Reference material for Stage 2 drafting and the docx's front matter.

- reference/style_guide.json (built by scripts/build_style_guide.py from the
  client-approved CARI Health report): per-report, per-subsection examples of
  how the Due Diligence Team writes -- shown to each drafting call for the
  subsections it is writing, as a structure/voice/depth example only.
- reference/AccuBreath_Due_Diligence_Report.docx: source of the standing
  TEN Capital Network disclaimer boilerplate.
"""
import functools
import json

from docx import Document as DocxDocument

REFERENCE_DOCX_PATH = "reference/AccuBreath_Due_Diligence_Report.docx"
STYLE_GUIDE_PATH = "reference/style_guide.json"

# Per-call budget for the example text. Long subsections (CARI's competitor
# profiles, material contracts) are cut -- the model needs the pattern, not
# all ten profiles.
MAX_STYLE_CHARS_PER_SUB = 9_000
MAX_STYLE_CHARS_TOTAL = 40_000


@functools.lru_cache(maxsize=1)
def _style_guide() -> dict[str, dict[str, str]]:
    try:
        with open(STYLE_GUIDE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def style_excerpt(style_h1: str, style_h2s: list[str]) -> str:
    """The reference report's text for the given report and subsections, in
    the order asked, each labeled with its heading and capped."""
    report = _style_guide().get(style_h1, {})
    parts, total = [], 0
    for h2 in dict.fromkeys(style_h2s):  # de-dupe, keep order
        text = report.get(h2, "")
        if not text:
            continue
        if len(text) > MAX_STYLE_CHARS_PER_SUB:
            text = text[:MAX_STYLE_CHARS_PER_SUB] + "\n[... example continues in the same pattern]"
        label = f"## {style_h1} -- opening" if h2 == "_intro" else f"## {style_h1} > {h2}"
        chunk = f"{label}\n{text}"
        if total + len(chunk) > MAX_STYLE_CHARS_TOTAL:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n".join(parts)


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
