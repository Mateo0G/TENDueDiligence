"""Stage 2 task graph: which Claude calls draft which of the 25 confirmed
template headings (docs/template_structure.md), in what order.

This is a deliberate *reduction* of docs/prompt_inventory.md's raw prompt
count, not a 1:1 transcription of it -- see docs/prompt_inventory.md's
structural findings (two-pass sections, the Competition chain) and the
notes below for what got collapsed and why:

- Sub-headings the source spreadsheet has no separate prompt for (patents
  table, missing-corporate-documents list, legal-findings-at-a-glance
  table, commercial-agreements table, competitor-marketing table) are
  produced as *extra* SectionContent entries in a sibling task's own JSON
  output, not as separate API calls -- the original hand-built report shows
  no evidence a human prompted those independently either.
- Sub-headings the spreadsheet DOES give a separate, dependent prompt for
  (Sales & Marketing SWOT, the Finance proforma analysis, tech risks, the
  team/board list) stay separate tasks, each depending on its own draft
  task -- honoring that the source design genuinely intended two calls.
- Competition's 6-step manual refinement chain (rows 49->57 in
  docs/prompt_inventory.md) is collapsed to 2 calls (draft, then
  advantage-over-competitors) rather than replicated call-for-call --
  6 sequential Claude calls per company for one heading was judged not
  worth the added cost/latency versus a well-scoped 2-call version. Flagged
  for reconsideration if output quality doesn't hold up.
- "Competitive Exits and ROI" and "Potential acquirers" originally read the
  *compiled* report (post Stage 3) per docs/prompt_inventory.md's own flag
  -- reinterpreted here as Stage-2-compatible by depending directly on the
  Competition draft task's output instead of waiting for full compile.
- Executive Summary is the one task that depends on every other Stage 2
  task -- its own source prompts (rows 9/11) explicitly summarize "the
  report", which only exists once everything else is drafted.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProducedSection:
    section_key: str  # exact heading text from docs/template_structure.md
    order_index: int


@dataclass(frozen=True)
class SectionSpec:
    task_key: str
    produces: tuple[ProducedSection, ...]
    dataroom_categories: tuple[str, ...]  # () means "no dataroom filter"
    depends_on: tuple[str, ...]
    instruction: str
    needs_web_search: bool = False
    style_guide_headings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if not self.style_guide_headings:
            object.__setattr__(
                self, "style_guide_headings", tuple(p.section_key for p in self.produces)
            )


_SPECS: list[SectionSpec] = [
    SectionSpec(
        task_key="sales_marketing_draft",
        produces=(ProducedSection("Sales and Marketing Report", 2),),
        dataroom_categories=("sales_marketing",),
        depends_on=(),
        instruction=(
            "Populate the Sales and Marketing section of {company}'s due diligence report: "
            "sales strategy, distribution channels, market positioning, sales compensation, "
            "and ideal customer profile. Base every claim on the attached dataroom documents "
            "and pitch deck; do not invent figures."
        ),
    ),
    SectionSpec(
        task_key="sales_marketing_swot",
        produces=(ProducedSection("Strengths and Weaknesses of the Sales and Marketing Plan", 3),),
        dataroom_categories=("sales_marketing",),
        depends_on=("sales_marketing_draft",),
        instruction=(
            "Describe the strengths and weaknesses of {company}'s Sales and Marketing plan, "
            "given the drafted Sales and Marketing Report section provided as prior context."
        ),
    ),
    SectionSpec(
        task_key="market_size",
        produces=(ProducedSection("Market Size Analysis", 4),),
        dataroom_categories=("sales_marketing",),
        depends_on=(),
        instruction=(
            "Analyze {company}'s claimed market size (TAM/SAM/SOM) and determine whether it is "
            "a reasonable estimate. Include a table comparing the market-size figures found in "
            "different source documents, if more than one estimate exists."
        ),
    ),
    SectionSpec(
        task_key="financial_draft",
        produces=(ProducedSection("Financial Report", 5),),
        dataroom_categories=("financial",),
        depends_on=(),
        instruction=(
            "Populate the Finance section of {company}'s due diligence report: financial "
            "position, use of funds by department, burn rate, and runway. Include a table "
            "breaking down operating expenses and payroll by department."
        ),
    ),
    SectionSpec(
        task_key="financial_proforma",
        produces=(ProducedSection("Analysis of the Financial Proforma — Key Metrics", 6),),
        dataroom_categories=("financial",),
        depends_on=("financial_draft",),
        instruction=(
            "Analyze {company}'s financial pro forma for key metrics (revenue, gross margin, "
            "EBITDA, headcount, cash position) across the projection period. Include a table "
            "with one row per metric and one column per projection year."
        ),
    ),
    SectionSpec(
        task_key="investment_valuation",
        produces=(ProducedSection("Investment Sufficiency & Valuation Analysis", 7),),
        dataroom_categories=("financial",),
        depends_on=("financial_draft", "financial_proforma"),
        instruction=(
            "Analyze {company}'s proposed investment ask: is it sufficient to carry the "
            "business to its next milestone? Review the valuation in terms of pre-money "
            "valuation and per-share price. Include tables for: the financing instruments "
            "offered, conversion-price scenarios, and projected operating cash flow by year."
        ),
    ),
    SectionSpec(
        task_key="science_tech_draft",
        produces=(ProducedSection("Science and Technology Report", 8),),
        dataroom_categories=("science_tech",),
        depends_on=(),
        instruction=(
            "Populate the Science and Technology section of {company}'s due diligence report. "
            "Cover what is unique and differentiated about the technology, and any issues the "
            "company might face with its suppliers and vendors, as part of the narrative."
        ),
    ),
    SectionSpec(
        task_key="tech_risks",
        produces=(ProducedSection("What are the technology risks?", 9),),
        dataroom_categories=("science_tech",),
        depends_on=("science_tech_draft",),
        instruction="Given the drafted Science and Technology section, what are {company}'s technology risks?",
    ),
    SectionSpec(
        task_key="hr_report",
        produces=(ProducedSection("HR Report & Background Check Confirmation", 10),),
        dataroom_categories=("hr",),
        depends_on=(),
        instruction=(
            "Build an HR report for {company}: headcount, org structure, and background-check "
            "status for key personnel, based on the attached HR documents."
        ),
    ),
    SectionSpec(
        task_key="team_bios",
        produces=(ProducedSection("Management Team Biographies", 11),),
        dataroom_categories=("hr",),
        depends_on=(),
        instruction=(
            "Review the attached resumes and the pitch deck. Write a summary of the skills and "
            "experience of {company}'s current management team, one biography per person."
        ),
    ),
    SectionSpec(
        task_key="team_board_members",
        produces=(ProducedSection("Team Members and Board Members", 12),),
        dataroom_categories=("hr",),
        depends_on=("team_bios",),
        instruction=(
            "List {company}'s team members and board members, with role and equity/options "
            "where the dataroom discloses it. Include two tables: one for team members, one "
            "for advisors/board members."
        ),
    ),
    SectionSpec(
        task_key="corporate_report",
        produces=(
            ProducedSection("Legal Review of Corporate and Deal Documents (Corporate Report)", 13),
            ProducedSection("Missing / Recommended Corporate Documents", 14),
            ProducedSection("Legal Review — Key Findings at a Glance", 15),
        ),
        dataroom_categories=("legal_corporate",),
        depends_on=(),
        instruction=(
            "Populate the Corporate Report section of {company}'s due diligence report: entity "
            "structure, cap table observations, and deal-document review. Also produce a list "
            "of missing or recommended corporate documents, and a table of key legal findings "
            "at a glance (Item / Finding)."
        ),
    ),
    SectionSpec(
        task_key="ip_report",
        produces=(
            ProducedSection("Intellectual Property Report", 16),
            ProducedSection("{company} Patents (Issued):", 17),
        ),
        dataroom_categories=("ip",),
        depends_on=(),
        instruction=(
            "Populate the Intellectual Property section of {company}'s due diligence report, "
            "including the strengths of the IP position and weaknesses competitors might "
            "exploit. Include a table of issued patents (Patent No. / Priority Date / Filed / "
            "Granted / Assignee / Status)."
        ),
        # The reference report bakes its company name into this heading
        # ("AccuBreath Patents (Issued):") -- look up the style guide under
        # that literal text, not under {company}'s own resolved heading.
        style_guide_headings=("Intellectual Property Report", "AccuBreath Patents (Issued):"),
    ),
    SectionSpec(
        task_key="competition_report",
        produces=(
            ProducedSection("Competition Report (Competitive Landscape)", 18),
            ProducedSection("Marketing & Sales Strategies of Competitors (vs. {company})", 19),
        ),
        dataroom_categories=("sales_marketing", "science_tech"),
        depends_on=(),
        instruction=(
            "Conduct a competitive analysis for {company}: identify the top 5-10 competitors, "
            "and analyze their market positioning, strengths, weaknesses, and marketing/sales "
            "strategies. Include a comparison table (Competitor / Category / Price / Key "
            "strength, or similar columns fitting the competitors found)."
        ),
        style_guide_headings=(
            "Competition Report (Competitive Landscape)",
            "Marketing & Sales Strategies of Competitors (vs. AccuBreath)",
        ),
    ),
    SectionSpec(
        task_key="competition_advantage",
        produces=(ProducedSection("{company}'s Advantage Over Each Competitor", 20),),
        dataroom_categories=(),
        depends_on=("competition_report",),
        instruction=(
            "Given the drafted Competition Report, analyze each competitor and show {company}'s "
            "advantage over each one."
        ),
        style_guide_headings=("AccuBreath's Advantage Over Each Competitor",),
    ),
    SectionSpec(
        task_key="exit_strategy_roi",
        produces=(ProducedSection("Exit Strategy & Comparable-Transaction ROI Analysis", 21),),
        dataroom_categories=(),
        depends_on=("competition_report",),
        needs_web_search=True,
        instruction=(
            "Using the drafted Competition Report and web search, identify comparable "
            "companies in {company}'s space that have exited (acquisition or IPO), and "
            "estimate investor ROI under comparable exit scenarios. Include two tables: exit "
            "scenarios with implied enterprise value/return, and comparable historical exits."
        ),
    ),
    SectionSpec(
        task_key="exit_strategy_definition",
        produces=(
            ProducedSection(
                "Exit Strategy — Definition, Strengths/Weaknesses, and Potential Acquirers", 22
            ),
        ),
        dataroom_categories=(),
        depends_on=(),
        needs_web_search=True,
        instruction=(
            "Define {company}'s proposed exit strategy and assess its strengths and "
            "weaknesses. Using web search, identify potential acquirers that could provide an "
            "exit for investors. Include a table of acquirer tiers (Tier / Acquirer / Why a "
            "fit / Recent signal)."
        ),
    ),
    SectionSpec(
        task_key="contracts_report",
        produces=(
            ProducedSection("Contracts Report", 23),
            ProducedSection("Commercial Agreements — Non-Binding (Material Deficiency)", 24),
        ),
        dataroom_categories=("contracts",),
        depends_on=(),
        instruction=(
            "Populate the Contracts section of {company}'s due diligence report, reviewing "
            "vendor, customer, and partnership agreements. Include a table of commercial "
            "agreements that are non-binding or otherwise a material deficiency (Agreement / "
            "Counterparty / Status / Note)."
        ),
    ),
    SectionSpec(
        task_key="regulatory_report",
        produces=(ProducedSection("Regulatory Report", 25),),
        dataroom_categories=("regulatory",),
        depends_on=(),
        instruction=(
            "Populate the Regulatory section of {company}'s due diligence report: applicable "
            "regulatory pathway, clearances/approvals held or sought, and compliance posture. "
            "Include a table of predicate devices or precedents if applicable, and a table of "
            "key regulatory findings (Item / Finding)."
        ),
    ),
]

# Executive Summary depends on every other Stage 2 task -- its own source
# prompts (docs/prompt_inventory.md rows 9/11) summarize "the report", which
# only exists once everything else is drafted. Computed rather than
# hardcoded so it can't drift out of sync with the list above.
_EXEC_SUMMARY = SectionSpec(
    task_key="executive_summary",
    produces=(ProducedSection("Executive Summary", 1),),
    dataroom_categories=(),
    depends_on=tuple(spec.task_key for spec in _SPECS),
    instruction=(
        "Write the Executive Summary for {company}'s due diligence report: one paragraph "
        "covering each of Sales and Marketing, Financial, Team, Intellectual Property, "
        "Corporate, Contracts, Science and Technology, Competition, and Regulatory, drawing "
        "on the drafted sections provided as prior context."
    ),
)

SECTION_SPECS: list[SectionSpec] = [_EXEC_SUMMARY] + _SPECS
SPEC_BY_TASK_KEY: dict[str, SectionSpec] = {s.task_key: s for s in SECTION_SPECS}
