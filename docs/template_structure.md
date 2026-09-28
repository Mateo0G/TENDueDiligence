# Template structure — reference/AccuBreath_Due_Diligence_Report.docx

Parsed programmatically with `python-docx` via `scripts/inspect_docx.py` /
`inspect_docx2.py` / `inspect_docx3.py` / `inspect_docx4.py`. This is the
target shape for the Phase 4 "compile draft" render step: heading skeleton,
paragraph styles, table placement, and front matter.

Source facts: 890 paragraphs, 17 tables, 1 section (single page setup), 1
inline image, 27 `Heading 1` paragraphs (2 are empty — spacer headings used
purely to force a page break before the next real heading, at doc indices 37
and 85).

## Paragraph styles

| Style | Count | Size | Bold | Notes |
|---|---|---|---|---|
| Title | 2 | 25pt | yes | Used once on the cover page ("Due Diligence Report") and once for the company/contact block above it — both literally styled `Title`, distinguished only by position. |
| Subtitle | 1 | 20pt | no | "Confidential", directly under the Title on the cover page. `space_before`≈18pt, `space_after`≈4pt. |
| Heading 1 | 27 (25 real + 2 empty spacers) | 15pt | yes | Every top-level section, including sub-headings within a section (see note below — this template does **not** use Heading 2; sub-sections are also `Heading 1`). |
| Normal | 860 | default | no | Body text, table-adjacent captions, and all blank spacer paragraphs. |

No `Heading 2`/`Heading 3` styles are used anywhere in the document — every
visual sub-heading inside a section (e.g. "What are the technology risks?"
under Science and Technology) is styled identically to a top-level `Heading
1`. This means heading *level* in this template is really a two-tier system
(Title/Subtitle for cover matter, Heading 1 for everything else) and the
outline/TOC nesting we generate must be inferred from document order and
grouping, not from style hierarchy. **Flag:** if the final render wants a
visually distinct sub-heading tier, we'll need to introduce a `Heading 2`
style ourselves — the reference doesn't give us one to imitate.

## Front matter (paragraphs 0–37, before "Executive Summary")

1. Para 0: `Title` — blank in this sample (the slot where a company name/logo caption would go).
2. Paras 1–5: blank spacers.
3. Paras 6–8: `Normal` — contact block (name, title, email) for the primary company contact.
4. Paras 9–12: blank spacers.
5. Para 13: `Normal`, text "DRAFT" — a plain-text draft-status marker, not a real Word watermark.
6. Para 14: blank.
7. Para 15: `Title` — "Due Diligence Report".
8. Para 16: `Subtitle` — "Confidential".
9. Para 17: `Normal` — "August 2026" (report date).
10. Para 18: **`<sdt>` content control — this is the Table of Contents.** It wraps a genuine Word `TOC` field (`TOC \h \u \z \t "Heading 1,1,Heading 2,2,Heading 3,3,Heading 4,4,Heading 5,5,Hea...`) with a cached rendering of entries + page numbers from the last time Word updated fields. python-docx cannot regenerate this field's cached text; the field will show stale/blank entries until a human opens the doc in Word and presses "Update Field" (or we regenerate the field XML ourselves at render time — see Flag below).
11. Paras 19–32: blank spacers (space reserved for the TOC to expand into visually).
12. Paras 33–36: `Normal` — "TEN Capital Network Disclaimer:" heading line followed by the boilerplate disclaimer paragraph text.
13. Para 37: `Heading 1`, empty — spacer that forces "Executive Summary" onto its own page.

**Flag — TOC generation:** python-docx has no built-in API to regenerate a
`TOC` field's cached page numbers (that computation only happens inside
Word's layout engine). Two options for Phase 4: (a) emit the same
`TOC \h \u \z \t "Heading 1,1,..."` field code with no cached text and rely
on the user pressing "Update Field" / "Update Table" on first open in Word
(simplest, matches how the reference doc itself was almost certainly
produced), or (b) hand-build a static list of heading text + manually
tracked page estimates as plain paragraphs (loses live page numbers, gains
zero-dependency rendering). Recommend (a) — it's exactly what the reference
file does.

**Flag — cover page company identity:** the blank `Title` at para 0 plus the
contact block at paras 6–8 suggests the cover page is meant to carry the
target company's name/logo and a primary contact, distinct from the
"Due Diligence Report / Confidential / <date>" block at paras 15–17. The
inline image (1 image in the whole doc, `WD_INLINE_SHAPE_TYPE.PICTURE`) is
almost certainly a logo — need to confirm its paragraph anchor if we want to
template a logo slot (not yet pinned down; low priority since AccuBreath's
sample happens to have this slot blank of visible text).

## Body section skeleton (25 real Heading 1 sections, in document order)

| # | Heading text | Tables in section |
|---|---|---|
| 1 | Executive Summary | — |
| 2 | Sales and Marketing Report | — |
| 3 | Strengths and Weaknesses of the Sales and Marketing Plan | — |
| 4 | Market Size Analysis | Table 1 (Source document / Stated TAM–market / Stated growth) |
| 5 | Financial Report | Table 2 (Department / OpEx / Payroll / Share of Use of Funds) |
| 6 | Analysis of the Financial Proforma — Key Metrics | Table 3 (Metric × years 2026–2030) |
| 7 | Investment Sufficiency & Valuation Analysis | Table 4 (financing instruments), Table 5 (conversion-price scenarios), Table 6 (operating cash flow by year) |
| 8 | Science and Technology Report | — |
| 9 | What are the technology risks? | — |
| 10 | HR Report & Background Check Confirmation | — |
| 11 | Management Team Biographies | — |
| 12 | Team Members and Board Members | Table 7 (team equity/options), Table 8 (advisors) |
| 13 | Legal Review of Corporate and Deal Documents (Corporate Report) | — |
| 14 | Missing / Recommended Corporate Documents | — |
| 15 | Legal Review — Key Findings at a Glance | Table 9 (Item / Finding, 15 rows) |
| 16 | Intellectual Property Report | — |
| 17 | AccuBreath Patents (Issued): | Table 10 (patent no. / dates / assignee / status) |
| 18 | Competition Report (Competitive Landscape) | — |
| 19 | Marketing & Sales Strategies of Competitors (vs. AccuBreath) | Table 11 (competitor comparison, 7 cols) |
| 20 | AccuBreath's Advantage Over Each Competitor | — |
| 21 | Exit Strategy & Comparable-Transaction ROI Analysis | Table 12 (exit scenarios/ROI), Table 13 (comparable exits) |
| 22 | Exit Strategy — Definition, Strengths/Weaknesses, and Potential Acquirers | Table 14 (acquirer tiers) |
| 23 | Contracts Report | — |
| 24 | Commercial Agreements — Non-Binding (Material Deficiency) | Table 15 (Agreement / Counterparty / Status / Note, 15 rows) |
| 25 | Regulatory Report | Table 16 (predicate devices), Table 17 (Item / Finding, 12 rows) |

Every table's header row was extracted directly (see dump); full per-cell
content wasn't dumped here since the header row is sufficient to fix each
table's *shape* (column semantics) for the render step. Re-extract full
table content on demand if a given section's builder needs a concrete
worked example.

**Note — company-name-specific headings:** several headings and table
labels bake in the literal company name ("AccuBreath Patents (Issued):",
"AccuBreath's Advantage Over Each Competitor", "vs. AccuBreath"). The render
step must template these as `f"{company_name} Patents (Issued):"` etc.,
not copy them verbatim.

**Flag — spreadsheet-to-template coverage gap:** the prompt spreadsheet
(see `docs/prompt_inventory.md`) defines substantially more categories than
this reference report actually renders as sections. Present in the
spreadsheet but **absent** as a Heading 1 here: Site Visit, FAQ, Terms
Sheet, Cap Table, Analysis by Persona, Gap Analysis, Mitigation Strategy,
Scalability, Worst Case Scenarios, Quality Check, DD-report Compare,
Customer Analysis, Product Roadmap, Analyst-style Analysis Report, Risk
Factors, Review-and-Advise, Completeness Checklist, Find-Key-Documents.
Some of these map to later pipeline stages by design (Gap Analysis /
Mitigation / Report Card are stage 5–6 outputs, not stage-2 body sections,
per the brief), but others (FAQ, Cap Table, Site Visit, Risk Factors,
Terms Sheet) look like they could plausibly be body sections that this
particular sample report simply didn't need or didn't have source data
for. **Decided (2026-09-04): exactly these 25 sections, no superset** — see
"Decisions" below.

**Note — category-to-heading merges:** a few spreadsheet categories that
look distinct on their own combine into one template heading: "Investment
Ask" + "Valuation" → "Investment Sufficiency & Valuation Analysis";
"Competitive Advantage" → folded into "AccuBreath's Advantage Over Each
Competitor"; "Competitive Exits and ROI" → folded into "Exit Strategy &
Comparable-Transaction ROI Analysis". The section-drafting stage (Phase 3)
should treat these as *one* Claude call whose prompt concatenates the
source spreadsheet prompts for the merged categories, not one call per
spreadsheet row.

## Tables — general shape

- No named table style is applied (`table.style` reports `None` for all 17
  tables) — formatting is either inherited from a direct/manual border
  definition or from the Word "Normal Table" default with explicit
  per-cell borders set at the XML level rather than a reusable style.
  **Decided: match exactly** — Phase 4 needs a follow-up XML-level pass
  (reading raw `<w:tblBorders>`/`<w:tcBorders>`/run-formatting from this
  file) to replicate it in the generator; the header-row-only extraction
  captured so far is not sufficient for that and will need to be redone
  with full cell/run formatting once Phase 4 starts.
- Table shapes vary widely (2–7 columns, 3–15 rows) and are clearly
  hand-built per section rather than from one shared template — the render
  step needs a per-section table builder, not one generic table renderer.

## Page setup / branding

- Page size: US Letter (8.5" × 11"). Margins: 0.5" left/right, 0.6" top,
  0.5" bottom.
- Header: empty (2 blank paragraphs — reserved space, no visible content).
- Footer: single paragraph, tab-aligned text `AccuBreath - Due Diligence
  Report` — company-name-specific, must be templated per report. (Footer
  text extraction here doesn't confirm whether a `PAGE` field also lives in
  the footer; treat as likely but re-check if page numbers matter.)
- One inline image (logo, presumed) — anchor paragraph not yet pinned down;
  low priority per the cover-page flag above.

## Decisions (2026-09-04)

1. **Styling fidelity — match exactly.** The generated docx must replicate
   this reference's fonts, colors, and table borders/structure, not just its
   heading outline. Phase 4 will need to read the raw `<w:tblBorders>` /
   `<w:tcBorders>` and run/paragraph-level formatting XML from this file (not
   just the header-row text already extracted here) and either reuse it
   directly (copy style/formatting objects) or rebuild it property-by-
   property in the generator. Revisit this file's "Tables — general shape"
   section with a deeper XML pass once Phase 4 starts.
2. **Section list — exact 25, no superset.** The render template targets
   exactly the 25 confirmed Heading 1 sections listed above. Categories from
   the prompt spreadsheet with no matching heading here (FAQ, Site Visit,
   Terms Sheet, Cap Table, Customer Analysis chain, Product Roadmap, Worst
   Case Scenarios, Scalability, Analysis by Persona, Risk Factors) are out of
   scope for v1's Stage 2/3/4 section set.

## Resolved at Phase 4

- **TOC strategy** — went with a live `TOC` field (`app/docx_builder.py::
  _add_toc_field`), matching what the reference report itself does. Word
  regenerates entries and page numbers on "Update Field" (F9) at first
  open; python-docx can't compute page numbers itself so there was no way
  around that step either way.
- **Styling fidelity, exact values** — deep XML inspection (`scripts/
  inspect_docx_styles*.py`) found the true defaults: Open Sans 11pt body
  (docDefaults, not python-docx's default Calibri), Heading 1 bold 15pt,
  tables using plain single black 0.5pt borders with bold+centered header
  cells and no shading (the reference's own `Table1` table-style reference
  doesn't resolve to anything in its styles.xml, so direct border
  formatting was used instead of a named style). One quirk found and
  deliberately *not* replicated: the reference's own Title/Subtitle
  paragraphs carry a direct 13pt run-level override that contradicts their
  own style definitions (25pt/20pt) — judged an authoring artifact, not
  intent, so the generator uses the style-defined sizes.
