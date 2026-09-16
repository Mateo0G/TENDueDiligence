# Prompt inventory — reference/Due_Diligence_Report_Prompts.xlsx

Parsed programmatically with `openpyxl` via `scripts/parse_prompts.py`, which
writes the raw structured extraction to `docs/prompt_inventory.json` (97
records). This document is the curated read of that extraction: every
prompt, grouped by pipeline stage, with dependencies and flags called out.
Category grouping uses "carry-forward": a row with a blank Category column is
a continuation prompt under the most recently named Category above it.

The sheet is a single tab ("Due Diligence") with one repeated header row
pattern (`Status | Category | Logistics | Prompt | Example Outputs | Training
Documents | Prompts to fill the Gaps`) appearing 3 times, which is what
splits the sheet into its three real blocks:

- **Rows 7–129 — Core** (blank `Status`): section-drafting + assorted utility prompts.
- **Rows 132–158 — `DD Assessment`**: the scoring/QA block (Stage 4).
- **Rows 162–182 — `Life Science Assessment`** (+ 1 trailing `DD Assessment` row): the conditional scoring block (Stage 4, life-sciences companies only).
- **Row 190**: a single dangling note, see Flags.

All company-specific prompt text in the sheet uses "QSM" as the placeholder
company name and "Solution Medical" / "Tympanogen" as the style-guide sample
reports — every prompt below needs `{company}` substituted and the style
reference swapped for our actual reference report (AccuBreath) at
implementation time.

## 1. Meta / non-prompt rows (not part of the pipeline)

- **Row 7** — "System Prompt to provide best practices for preparing a
  dataroom" — a standing system-prompt/instructions block, not a
  per-company task. Useful as shared guidance text, not a pipeline node.
- **Row 13–14** — "Due Diligence Report" — describes the overall manual
  workflow (upload docs → select sequence → submit → store output). This is
  a description of the *whole pipeline*, i.e. this brief's job, not an
  individual prompt.
- **Rows 76–80** — "Build near final report" — a checklist ("review all
  reports for accuracy", "compile chosen sections into template", "add
  graphs and charts") describing Stage 3 (Compile), not an LLM call.

## 2. Stage 2 — Section-drafting prompts (independent, parallel-eligible)

Each row below takes: the dataroom subset for that category + the pitch
deck + the matching section of the reference report as a style guide, and
is independent of every other row in this table — good Batch API
candidates once correctness is proven (per the brief's Stage 2 design).

| Category (row) | Prompt gist | Gap-fill prompt exists? | Maps to template heading |
|---|---|---|---|
| Company Summary (9) | Exec-summary paragraph per topic, from full dataroom zip | – | (folds into Executive Summary) |
| Exec Summary w/ DD Findings & Deal Terms (11) | Same section, framed around findings + deal terms | – | Executive Summary |
| Sales and Marketing (15) | Populate S&M section | yes — full S&M plan (channel, positioning, forecast, etc.) | Sales and Marketing Report |
| Market size (19) | Analyze market size, sanity-check the estimate | – | Market Size Analysis (Table 1) |
| Finance (21) | Populate Finance section | yes — 5-yr forecast w/ pricing & adoption assumptions | Financial Report (Table 2) |
| Investment Ask (24) | Is the raise sufficient? | – | Investment Sufficiency & Valuation Analysis (merged, Tables 4–6) |
| Valuation (25) | Pre-money valuation, price/share | – | ″ (merged with Investment Ask) |
| Science and Technology (27) | Populate Sci/Tech section | – | Science and Technology Report |
| HR Report (35) | Build HR report from sample template | – | HR Report & Background Check Confirmation |
| Team (37) | Populate Team section | yes — resume + pitch-deck skills summary | Management Team Biographies |
| Corporate Report (41) | Populate Corporate/legal-docs section | – | Legal Review of Corporate and Deal Documents |
| Intellectual Property (45) | Populate IP section | – | Intellectual Property Report (+ patents table) |
| Competition (49) | Populate Competition section | yes — *(see Flag C1: gap-fill text is a duplicate of the Competitive-Exits-and-ROI prompt, not a competition-specific fallback)* | Competition Report |
| Exit Strategy (67) | Define exit strategy + strengths/weaknesses | – | Exit Strategy — Definition, Strengths/Weaknesses… (merged) |
| Potential acquirers (68) | **Requires live web search** to identify acquirers | – | ″ (merged, Table 14) |
| Contracts (70) | Populate Contracts section | yes — startup legal-structure primer | Contracts Report |
| Regulatory (72) | Populate Regulatory section | – | Regulatory Report (Tables 16–17) |
| FAQ (74) | 20-Q FAQ, styled on sample FAQ | – | *not in reference report — optional/appendix* |
| Risk Factors (116) | Standalone risk-factors report, styled on sample | – | *not in reference report — optional/appendix* |
| Site Visit (43) | Structured site-visit form (preparer, location, date, equipment, attendees) | – | *not in reference report — optional, only when a visit occurred* |

**Structural finding — two-pass sections, not all-independent (Flag C2):**
Several categories are actually a *main draft* followed by a *second,
dependent prompt* that reads the just-drafted section and produces what
becomes its own template sub-heading. These pairs must run sequentially
within Stage 2, not in the same independent-parallel batch as everything
else:

| Main draft (row) | Dependent follow-up (row) | → template heading |
|---|---|---|
| Sales and Marketing (15) | "Describe strengths/weaknesses of the S&M plan" (17) | Strengths and Weaknesses of the Sales and Marketing Plan |
| Finance (21) | "Analyze financial pro forma for key metrics" (23) | Analysis of the Financial Proforma — Key Metrics (Table 3) |
| Science and Technology (27) | "What are the technology risks?" (29) | What are the technology risks? |
| Science and Technology (27) | "What is unique/differentiated about the technology?" (31) | *no distinct heading — folds into S&T body* |
| Science and Technology (27) | "What issues with suppliers/vendors?" (33) | *no distinct heading — folds into S&T body* |
| Team (37) | "List team members as well as board members" (39, + own gap-fill) | Team Members and Board Members (Tables 7–8) |
| Intellectual Property (45) | "List strengths/weaknesses of the IP that competitors may exploit" (47) | *no distinct heading — folds into IP Report body (Flag I1)* |

**Structural finding — Competition is a 6-step refinement chain, not one
prompt (Flag C3):** rows 49→51→53→54→55→56→57 form a strict sequential
chain: draft Competition section → broad top-5–10 competitor analysis →
analyze a specific competitor list → *combine* the previous two outputs →
add a level of detail → add one more level of detail → write a one-page
summary per competitor. Rows 54–57 are explicitly iterative refinements of
the immediately preceding row's output, not independent asks. Then
Competitive Advantage (59) depends on the finished Competition output ("show
the advantage of `{company}` over each competitor"), and row 61 is a
section-local QA check ("perform analysis on the competition report for
completeness, list deficiencies") distinct from the global Stage-4 QA
block. Rows 60 (industry trends) is a looser, less clearly dependent
sub-call. → template headings: Competition Report, Marketing & Sales
Strategies of Competitors (Table 11), `{company}`'s Advantage Over Each
Competitor.

## 3. Post-compile utilities (read the assembled draft — Stage 4/5/6-adjacent, not Stage 2)

These explicitly say "the due diligence report" / "the attached diligence
report" / "based on the provided … report" in their prompt text — per the
brief's own dependency rule, that's a hard signal they belong *after* Stage
3 compile, even though most sit in the sheet's "Core" block alongside the
Stage-2 prompts above.

| Category (row) | Reads | Notes |
|---|---|---|
| Competitive Exits and ROI (63, 65) | compiled report | "Review the attached due diligence… list competitive companies that sold" — despite living among Stage-2 rows, this is post-compile. Folds into Exit Strategy & Comparable-Transaction ROI Analysis (Tables 12–13). |
| Terms Sheet (81) | compiled report + an external Keiretsu terms-sheet template | Cross-references terms-sheet line items against the DD report. Not in reference report — optional appendix. |
| Cap Table (85) | compiled report | Analyzes cap table vs. comparable companies, flags unusual terms. Not in reference report. |
| Analysis by Persona (87) | compiled report + a named investor's LinkedIn profile | **Flag P1** — the LinkedIn URL is a hardcoded worked example (a specific real person), not a generalizable template. Must become a parameterized "investor persona" input at build time — never fetch a specific stranger's profile as a literal instruction. |
| Quality check (100) | compiled report | Per-chapter strengths/weaknesses + improvement recommendations. **Flag Q1** — heavily overlaps the Stage-4 "Comprehensiveness Validator" and "Clarity, Structure & Writing Quality Scan" prompts (§4); likely redundant, evaluate whether to drop in favor of the DD Assessment block. |
| Due Diligence report compare (102) | two compiled reports | Cross-deal benchmarking between two different companies' reports — out of scope for a single-company pipeline run; a separate future feature. |
| Review and advise on improvements (118) | compiled report | **Flag R1** — prompt text says "Print3d Due Diligence Report", a third placeholder company name inconsistent with "QSM"/"Solution Medical" used everywhere else; treat as copy-paste residue, generalize to `{company}`. |
| Gap Analysis (89) | compiled report | Stage 5, step 1 — see §6. |
| Mitigation strategy (91) | Gap Analysis output | Stage 5, step 2 — see §6. |

## 4. Stage-1-adjacent utilities (read the dataroom, not the compiled report)

| Category (row) | Notes |
|---|---|
| Due Diligence Completeness Checklist (120) | Assesses fundraising-readiness completeness of the raw dataroom/pitch materials — conceptually belongs in Stage 1 (Intake), not Stage 2, despite its location in the sheet. **Flag D1** — its "Example Outputs" cell contains a fuller, better-specified alternate prompt (~300+ chars, itself reading like a complete system prompt for "Fundraising-First Data Room Completeness Assessment") that may be the more useful version to actually implement, vs. the terser main "Prompt" cell. |
| Find key documents (122) | Depends on row 120's checklist output — lists which required documents are missing from the dataroom. Sequential after 120, both pre-compile. |

## 5. Ambiguous / likely-optional / parameterized prompts

| Category (row) | Issue |
|---|---|
| Scalability (93) | Generic business-scalability essay prompt with no company placeholder at all — unclear if meant to be company-specific or a general reference essay. Not mapped to any reference-report heading. **Flag S1.** |
| Worst Case Scenarios (95–98) | Explicitly parameterized: main prompt + 3 follow-up rows that are literal fill-in slots (`Company Type/Industry: [Insert]`, `Key Areas of Concern: [Insert]`, `Company Description: [Insert]`), not separate prompts. Not in reference report. |
| Customer Analysis chain (104, 106, 108, 109, 110) | Revenue-growth analysis of *existing* customers, pipeline forecast, prospect list, points of contact. Not in reference report — plausibly because AccuBreath is pre-revenue ("AccuBreath is a pre-revenue, early-stage medical-device company" per the sample report's Financial section) and has no customers to analyze yet. **Should be conditional on the company having a customer base**, not run unconditionally. |
| Product roadmap (112) | Depends on the Customer Analysis chain's output — same pre-revenue conditionality applies. |
| Analysis Report (114) | Style-guided by a live Google Finance quote-page URL — not a fetchable/replicable document structure; treat as a loose "public-market analyst tone" pointer only. |

## 6. Stage 5 — Gap Analysis → Mitigation (sequential, low call count, standard API)

1. **Gap Analysis** (row 89) — reads the compiled `{company}` DD report, lists potential investor-perceived gaps/weaknesses.
2. **Mitigation strategy** (row 91) — reads Gap Analysis's output, builds a mitigation strategy per identified weakness. (Prompt text literally says "each weakness in the due diligence report" rather than naming Gap Analysis directly, but the brief confirms — and sheet ordering supports — that this is meant to consume step 1's output, not re-derive weaknesses from scratch.)

## 7. Stage 4 — DD Assessment block (14 prompts, all read compiled report + dataroom)

All score 0–100 **except** row 138, which just lists missing information
with no numeric score — flagged below. Report Card must run last (it
aggregates every other score in this table).

| # | Category (row) | Scores what |
|---|---|---|
| 1 | Comprehensiveness Validator (132) | Domain coverage (market/financial/tech/legal/IP/risk/team/ops) |
| 2 | Evidence Traceability Checker (134) | Claims-to-dataroom evidence mapping |
| 3 | Internal Consistency Analyzer (136) | Contradictions in financials/timelines/terminology/numbers |
| 4 | Key Information Check (138) | **No 0–100 score** — just lists missing key info. **Flag A1.** |
| 5 | Financial Rigor Stress Test (140) | Projection/assumption/unit-economics defensibility |
| 6 | Market Reality Alignment Audit (142) | Market-claim grounding vs. real data/competitor benchmarks |
| 7 | Risk Disclosure Depth Test (144) | Risk transparency, categorization, mitigation quality |
| 8 | Technical Validity & Plausibility Review (146) | Scientific/technical claim plausibility |
| 9 | Dataroom Integrity & Organization Check (148) | Dataroom completeness/structure/version control |
| 10 | Clarity, Structure & Writing Quality Scan (150) | Readability, logical flow, jargon |
| 11 | Red Flag Forensics Detector (152) | Red-flag risk score — **polarity unstated** (does higher = more risk, or fewer red flags?). **Flag A2.** |
| 12 | **Report Card of scores** (154) | Aggregates all of the above — must run last, and depends on 1–11 (+ Life Science block, if applicable — see Flag A3). |
| 13 | Check for missing data (156) | Cross-check compiled report's data elements vs. dataroom — **overlaps #4 and "Find key documents" (§4). Flag A4 (near-duplicate).** |
| 14 | Create minimum doc list (158) | Generic "what's the minimum DD doc set" utility — not company-specific; reads as a one-time reference utility rather than a per-report pipeline task. |

## 8. Stage 4 (conditional) — Life Science Assessment block (10 prompts, life-sciences companies only)

All score 0–100, all read the compiled report (+ dataroom for source data).

| # | Category (row) | Scores what |
|---|---|---|
| 1 | Biological Plausibility Validator (162) | MOA/target validity/biomarker logic |
| 2 | Preclinical Evidence Strength Checker (164) | Preclinical data rigor (controls, power, translational relevance) |
| 3 | Clinical Readiness & Feasibility Audit (166) | Trial design/endpoints/feasibility/regulatory input |
| 4 | Regulatory Pathway Validation Tool (168) | FDA/EMA strategy vs. precedent/guidance |
| 5 | CMC & Manufacturability Risk Scanner (170) | Manufacturability/scalability/QC/supply chain |
| 6 | IP Strength & Freedom-to-Operate Review (172) | Patent breadth, claims, FTO risk |
| 7 | Commercial & Pricing Realism Scan (174) | Pricing/reimbursement/payer realism |
| 8 | Competitive Differentiation Forensics (176) | Efficacy/safety/durability differentiation vs. competitors |
| 9 | Data Integrity & Traceability Audit (178) | Scientific/clinical claim traceability to raw data |
| 10 | Red Flag Clinical & Scientific Risk Detector (180) | Red-flag score, **explicitly "lower is worse"** — i.e. higher = fewer red flags = good, same polarity as a normal score. |

**Flag A2 detail:** note that this block's red-flag prompt (#10) explicitly
states its polarity ("lower is worse"), while the DD Assessment block's own
Red Flag Forensics Detector (§7 #11) does not state a direction at all. Pick
one consistent polarity convention across both blocks before wiring up the
Report Card aggregation — this is exactly the "scoring bands" open question
flagged in the project brief, and it's worse than just "no defined bands":
the two blocks may currently disagree on which direction is good.

**Flag A3 — duplicate Report Card placement:** "Report Card of scores"
appears twice — once inside the DD Assessment block (row 154) and again
immediately after the Life Science block (row 182, still tagged `Status =
DD Assessment`). Read as: the same aggregation prompt is meant to run once
more, over the combined DD + Life Science scores, when the Life Science
block applies. Not explicit in the source — confirm before building the
report-card renderer around this assumption.

## 9. Master validation prompt (row 190 cross-reference, resolved)

**Row 190:** `"See TEN AI Prompts (Row 375) for master validation and
verification prompt"` pointed at a row in some other workbook not present
in this repo. Resolved directly by the user (2026-09-04) — the master
prompt is:

> Validate and verify AI or human reports by cross-checking evidence,
> assessing consistency, and evaluating reasoning quality—scoring factual
> integrity, logical soundness, and overall reliability on a 1–10 scale.

This reads as a general-purpose report-auditing prompt, one level more
abstract than any single Stage-4 DD Assessment prompt (§7) — it isn't
scoped to one domain (financial rigor, market reality, etc.) but to the
report's overall evidentiary and logical soundness, and it could equally be
pointed at either an AI-generated or human-written report.

**Flag M1 — scale mismatch:** every other score in this pipeline (all 14 DD
Assessment prompts + all 10 Life Science Assessment prompts) is defined on
a 0–100 scale; this one is explicitly 1–10. Needs a decision at Stage 4/6
design time: keep it as a separate 1–10 "meta-reliability" score reported
alongside (not blended into) the Report Card's 0–100 figures, or rescale it
×10 to fold into the same aggregate. Recommend keeping it separate and
clearly labeled, since collapsing a 1–10 judgment into a 0–100 scale by
simple multiplication implies a false precision the prompt doesn't actually
support.

**Open question — where does this run?** Nothing in the sheet says whether
this master validator runs once per report (like Report Card, after all
other Stage-4/Life-Science scoring), or per-section (like a lighter-weight
version of the individual DD Assessment checks). Given its "reports" (not
"sections") framing, treat it as a single whole-report call, positioned
alongside Report Card of scores at the end of Stage 4 — confirm at Stage 4
implementation time rather than assuming.

## Summary of flags needing a decision (not guessed at)

- **C1** — Competition's gap-fill prompt text is identical to the
  Competitive Exits and ROI prompt — looks like a copy-paste error, not a
  real Competition-specific fallback.
- **I1** — IP's "strengths/weaknesses" follow-up (row 47) has no clear
  template heading to land in; likely folds into IP Report body text.
- **P1** — Analysis-by-Persona hardcodes a specific real investor's LinkedIn
  URL; must be parameterized, never used literally.
- **Q1** — "Quality check" (row 100) likely redundant with two Stage-4 DD
  Assessment prompts; consider dropping.
- **R1** — "Review and advise" (row 118) references a third placeholder
  company name ("Print3d"); treat as copy-paste residue.
- **D1** — Completeness Checklist's "Example Outputs" cell may be a better
  spec than its own "Prompt" cell; consider using that text instead.
- **S1** — Scalability prompt has no company binding at all; unclear intent.
- **A1** — Key Information Check is the one DD Assessment prompt with no
  numeric score, inconsistent with its 13 siblings.
- **A2** — Red-flag score polarity is inconsistent/unstated between the two
  assessment blocks — needs one convention before Report Card aggregation.
- **A3** — Report Card of scores appears twice; interpreted as "run again
  after Life Science block," not confirmed.
- **A4** — "Check for missing data" (row 156) likely duplicates "Key
  Information Check" (row 138) and "Find key documents" (row 122).
- **M1** — the master validation prompt (§9) scores 1–10 while every other
  Stage-4/Life-Science prompt scores 0–100; keep it separate rather than
  rescaling into the shared aggregate.

### Resolved (2026-09-04)

- **Section list**: exactly the 25 confirmed headings from
  `docs/template_structure.md`, no optional superset, for v1.
- **U1**: row 190's "TEN AI Prompts" cross-reference is resolved — see §9
  for the recovered master validation prompt text.
