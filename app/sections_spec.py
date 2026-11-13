"""Stage 2 report structure and task graph, modeled on the client-approved
CARI Health due diligence report (reference/style_guide.json, built by
scripts/build_style_guide.py): 15 Heading 1 reports, each opening with a
header block, a "Due Diligence Team Assessment" Summary of Findings box,
then Heading 2 subsections -- plus a consolidated open-items appendix.

Each report is a `Report`; each of its Heading 2 subsections a `Sub`. A
report's opening material (header, basis of preparation, Summary of
Findings, any lead-in) is its "_intro" subsection, stored as the Heading 1
row itself. One drafting task (`SectionSpec`) writes one or more
consecutive subsections of one report -- big reports are split across two
or three tasks so no single call has to write 10+ pages, with later tasks
depending on earlier ones so the report reads as one piece.

Keys and titles are fixed here, never taken from the model, so compile,
ordering, and the docx's TOC are deterministic.
"""
from dataclasses import dataclass, field

INTRO = "_intro"


@dataclass(frozen=True)
class Sub:
    key: str        # short key, unique within its report ("_intro" for the opening)
    title: str      # Heading 2 text; may contain {company}
    style_h2: str   # heading this corresponds to in the CARI style guide
    guidance: str   # what this subsection must establish


@dataclass(frozen=True)
class Report:
    key: str
    title: str          # Heading 1 text; may contain {company}
    header: str         # "{company}  |  <this>" line under the heading
    style_h1: str       # heading this corresponds to in the CARI style guide
    order: int
    subs: tuple[Sub, ...]

    def sub(self, key: str) -> Sub:
        return next(s for s in self.subs if s.key == key)


def section_key(report_key: str, sub_key: str) -> str:
    return report_key if sub_key == INTRO else f"{report_key}.{sub_key}"


_INTRO_GUIDANCE = (
    'The report\'s opening. Start with a level-4 heading "Due Diligence Team Assessment", then ONE '
    'callout block labeled "Summary of Findings." that gives the Team\'s bottom-line view of this '
    "whole area in 120-300 words: what is established, what is strong, what is weak or unverified, "
    "and the 2-4 things an investor most needs to know, with the key figures. {extra}"
)


def _intro(extra: str = "") -> Sub:
    return Sub(INTRO, "", "_intro", _INTRO_GUIDANCE.format(extra=extra).strip())


REPORTS: tuple[Report, ...] = (
    Report("executive_summary", "Executive Summary", "", "Executive Summary", 1, (
        Sub("sales", "Sales and Marketing", "Sales and Marketing",
            "Customers and channel, pricing and reimbursement/payment, the revenue forecast and its key "
            "driver, market size, and the Team's view of how credible the commercial plan is."),
        Sub("financial", "Financial", "Financial",
            "Historical revenue/losses and balance sheet position, cash and runway, the current round "
            "(exact size, instrument, terms, amount funded vs. committed vs. remaining), the pro forma "
            "headline figures and downside cases, valuation and the Team's view of it."),
        Sub("team", "Team", "Team",
            "Who leads the company, depth and gaps, board composition, key consultants, compensation "
            "and equity observations, any conflicts or background-check issues."),
        Sub("science_tech", "Science, Technology and Regulatory", "Financial",
            "What the product is and how it works (precisely), development stage and performance "
            "evidence, regulatory path and timeline, and the principal technical/regulatory risks."),
        Sub("ip", "Intellectual Property", "Intellectual Property",
            "Patent families and status, ownership/assignment chain, key dates, strength and gaps."),
        Sub("competition_exit", "Competition and Exit", "Sales and Marketing",
            "The real competitive alternatives, the company's advantage and its deficiencies, the exit "
            "thesis, most likely acquirers, and the risk-weighted return view."),
        Sub("corporate", "Corporate Documents", "Corporate Documents",
            "Entity, charter and capitalization, governance documents, what is executed vs. missing, "
            "and the material corporate findings."),
        Sub("contracts", "Contracts", "Contracts",
            "Material agreements by type, what is signed vs. unsigned, key terms and risks, and the "
            "documents requested before closing."),
    )),
    Report("commercial", "Commercial and Marketing Report", "Sales and Marketing Section of the Due Diligence Report",
           "Commercial and Marketing Report", 2, (
        _intro(),
        Sub("sales", "Sales and Customer Acquisition", "Sales and Customer Acquisition",
            "Target customer and how concentrated the market is, the sales motion (direct team sizing by "
            "year, distributors/partners and when each channel opens), pre-launch customer building "
            "(pilots, studies, research sales), and what gates commercial revenue. Include an H3 "
            "'Customer letters' (or 'Customer evidence') with a table of EVERY letter/LOI/MOU/customer "
            "document in the dataroom: Organization | Document and date | Setting | What the document "
            "actually states (binding or not, what is committed). Name distribution LOIs explicitly."),
        Sub("models", "Business and Revenue Models", "Business and Revenue Models",
            "Pricing (per unit, per test, subscription, hardware), who pays and how (reimbursement codes, "
            "payer coverage, budgets), unit economics/gross margin, and the revenue forecast: a table of "
            "revenue by year and by line/channel exactly as in the financial model (e.g. the 5-8 year "
            "plan through its final year), the penetration/volume assumptions behind it, and the model's "
            "downside scenarios if any. Add H3s as needed (e.g. 'Pricing and reimbursement', 'Revenue "
            "forecast', 'Customer economics') and test any retention/ROI claims made to customers."),
        Sub("marketing", "Marketing Plan", "Marketing Plan",
            "Marketing activities, budget by year, conferences/KOLs/publications, positioning, and "
            "whether the plan matches the sales model."),
        Sub("customer_input", "Provider Survey and Customer Input", "Provider Survey and Customer Input",
            "Any survey, interviews, pilot feedback, or customer data: sample, method, results, and how "
            "much weight it can bear. If none exists, say so and state what should be obtained."),
        Sub("market_size", "Market Size Analysis", "Market Size Analysis",
            "The company's TAM/SAM/SOM with its stated method and sources, then the Team's own check: H3 "
            "'Assessment of the TAM/SAM/SOM' recomputing each tier from primary data (counts x price x "
            "frequency), a table comparing every market-size figure found across documents, and H3 "
            "'Conclusion on market size'."),
        Sub("swot", "Strengths and Weaknesses of the Sales and Marketing Plan", "Strengths and Weaknesses of the Sales and Marketing Plan",
            "H3 'Strengths' and H3 'Weaknesses', each a numbered list of specific, evidenced points (bold "
            "lead-in, then explanation with figures)."),
        Sub("conclusion", "Commercial Conclusion", "Commercial Conclusion",
            "The Team's overall commercial verdict in 1-3 paragraphs, and what would change it."),
    )),

    Report("financial", "Financial Report", "Finance Section of the Due Diligence Report", "Financial Report", 3, (
        _intro("Name in basis of preparation every statement, tax return, bank statement, model version, "
               "cap table and deal document used, with dates, and whether statements are audited."),
        Sub("assumptions", "Assumptions", "Assumptions",
            "The model's key assumptions (pricing, volumes, penetration, headcount, COGS, opex, timing of "
            "approvals/launch), each with its source and the Team's view of whether it is supported."),
        Sub("historical", "Historical Financial Performance", "Historical Financial Performance",
            "A table of historical income statement and balance sheet figures by period from the actual "
            "financial statements (revenue incl. grants, opex, net income/loss, cash, assets, "
            "liabilities, equity), total capital raised to date by source, and commentary."),
        Sub("proforma", "Pro Forma Key Metrics", "Pro Forma Key Metrics",
            "A table with one row per metric (revenue, gross profit/margin, opex, EBITDA, net income, "
            "headcount, cash, capital required) and one column per projection year, taken exactly from "
            "the model; then H3 'Derived metrics' (growth, margin trajectory, breakeven year, revenue per "
            "employee, capital efficiency) and the downside/scenario cases."),
        Sub("investment", "Proposed Investment", "Proposed Investment",
            "The current round precisely: instrument, total size, amount funded / committed / soft-"
            "circled / remaining, cap, discount, interest, maturity, warrants, side letters and "
            "conditions; and the planned future rounds (size, timing, pre-money). Use a terms table."),
        Sub("use_of_funds", "Use of Funds", "Use of Funds",
            "Use of proceeds by category/department in a table, and whether it matches the milestones "
            "the round is meant to reach."),
        Sub("sufficiency", "Are the Funds Sufficient?", "Are the Funds Sufficient?",
            "Monthly burn, runway on cash plus this round, what milestone the money reaches, the gap to "
            "the next round, and sufficiency of later rounds (H3 per future round as relevant)."),
        Sub("valuation", "Valuation", "Valuation",
            "H3 'Pre-money valuation and price per share' (cap / pre-money, fully diluted shares, price "
            "per share vs. prior rounds' prices), H3 'Effect of conversion' at the next round, H3 "
            "'Assessment of the valuation' (comparables and stage), and H3 'Investor returns' (ownership, "
            "dilution through later rounds, MOIC/IRR at plausible exit values)."),
        Sub("reasons", "Reasons to Invest", "Reasons to Invest", "Numbered, specific, evidenced reasons."),
        Sub("risks", "Risks When Investing", "Risks When Investing", "Numbered, specific risks with figures."),
        Sub("conclusion", "Financial Conclusion", "Financial Conclusion", "The Team's financial verdict."),
    )),
    Report("scitech", "Science and Technology Report",
           "Technical, Manufacturing and Supply Chain Sections of the Due Diligence Report", "Science and Technology Report", 4, (
        _intro(),
        Sub("technical", "Technical Report", "Technical Report",
            "H3 'Technical Summary'; H3 'How the Technology Works' -- describe the product, sample/input, "
            "and mechanism EXACTLY as the most recent technical documents do (do not paraphrase the "
            "specimen type, measurement principle, or form factor into something else); H3 'Development "
            "Timeline' (table of milestones, done vs. planned, incl. design freeze/verification); H3 "
            "'Performance Data to Date' (every study/test result with n, metrics and conditions); H3 "
            "'Requirements Versus Evidence' (table: requirement | target | evidence to date | status). "
            "Include a level-4 label 'Details Provided by {company}' before company-supplied claims."),
        Sub("unique", "What Is Unique and Differentiated", "What Is Unique and Differentiated",
            "What is genuinely novel vs. what alternatives do, with H3 'Competitive context'."),
        Sub("risks", "Technology Risks", "Technology Risks",
            "Each material technical risk with why it matters and its mitigation; H3 'Technology "
            "readiness by workstream' as a table (workstream | status | readiness | key risk)."),
        Sub("manufacturing", "Manufacturing and Supply Chain Report", "Manufacturing and Supply Chain Report",
            "How the product is or will be made: every supplier/CMO/component source named in the "
            "dataroom with what it supplies and the agreement status, scale-up plan, COGS. H3 'Supply "
            "Chain Map' as a table (component/material | supplier | location | agreement status | "
            "single-source?)."),
        Sub("suppliers", "Issues the Company Might Face with Suppliers and Vendors", "Issues the Company Might Face with Suppliers and Vendors",
            "Supplier concentration, single sources, unexecuted supply agreements, change-of-control and "
            "IP-in-supplier-hands risks, lead times; H3 'Overall view on suppliers'."),
        Sub("followup", "Recommended Technical Follow-Up", "Recommended Technical Follow-Up",
            "Numbered list of specific technical diligence actions and documents to obtain."),
    )),
    Report("hr", "HR Report & Background Check Confirmation", "Management Team Section of the Due Diligence Report",
           "HR Report & Background Check Confirmation", 5, (
        _intro("After the callout, cover the roster with level-3 headings: 'Board of Directors and Board "
               "Observers', 'Management and Employees', 'Consultants, Technical and Clinical Advisors', "
               "'Advisory Board', 'Former Members' (if any), 'Organization Chart', 'Compensation and "
               "Agreements' (a table of each person's salary/fees, equity and option grants with vesting, "
               "and whether a signed employment/consulting/CIIAA agreement is in the dataroom), and "
               "'Planned Hires in the Financial Model' (table: role | timing | cost | note). Flag any "
               "conflict of interest (e.g. an investigator or advisor with equity in the company, "
               "related-party arrangements), and state what background checks were or were not done."),
        Sub("mgmt_bios", "Management Team Biographies", "Management Team Biographies",
            "One H3 per member of management (name, title), each a substantive biography from the "
            "resumes/bios: prior roles with dates, relevant exits/achievements, education."),
        Sub("board_bios", "Board Biographies", "Board Biographies",
            "One H3 per director/observer with biography and whom they represent."),
        Sub("advisory_bios", "Advisory Board Biographies", "Advisory Board Biographies",
            "One H3 per advisor with biography and their role/compensation. If none, say so."),
    )),
    Report("corp_legal", "Legal Review of Corporate and Deal Documents", "Corporate Report Section of the Due Diligence Report",
           "Legal Review of Corporate and Deal Documents", 6, (
        _intro("After the callout, add an H3 'Corporate snapshot' table (item | detail): legal name, "
               "entity type and state, formation date, registered agent, authorized and outstanding "
               "shares by class, fully diluted count, option pool, directors, officers."),
        Sub("corporate", "Corporate Documents", "Corporate Documents",
            "Review of the charter (and every amendment/restatement), bylaws, board and stockholder "
            "consents, stock ledger/cap table, option plan, good standing: what each says that matters "
            "(classes, preferences, protective provisions, conversion), and whether executed."),
        Sub("deal", "Deal Documents", "Deal Documents",
            "Review of the current round's documents (note/SAFE/purchase agreement, warrants, side "
            "letters, subscription docs) and their key terms; inconsistencies between documents."),
        Sub("findings", "Legal Findings and Recommended Actions", "Legal Findings and Recommended Actions",
            "Table: finding | significance | recommended action."),
        Sub("checklist", "Documentation Checklist", "Documentation Checklist",
            "Table of expected corporate/deal documents: document | in dataroom? | executed? | note."),
        Sub("followup", "Recommended Legal Follow-Up Before Closing", "Recommended Legal Follow-Up Before Closing",
            "Numbered list of specific actions and documents to obtain before closing."),
    )),
    Report("ip", "Intellectual Property Report", "Intellectual Property Section of the Due Diligence Report",
           "Intellectual Property Report", 7, (
        _intro("After the callout, add an H3 'IP Review Summary', an H3 'Detailed IP Review', and one "
               "H3 per patent family / application / license with a table (number | title | priority "
               "date | filing | status | jurisdictions | assignee | expiry | what it covers), plus "
               "trademarks, trade secrets, licenses in/out and the assignment chain from inventors."),
        Sub("dates", "Critical Dates", "Critical Dates",
            "Table of upcoming deadlines: PCT/national phase entries, office action responses, "
            "maintenance fees, expirations."),
        Sub("strengths", "Strengths of the Intellectual Property", "Strengths of the Intellectual Property",
            "Numbered list of specific, evidenced strengths."),
        Sub("weaknesses", "Weaknesses That Competitors May Exploit", "Weaknesses That Competitors May Exploit",
            "Numbered list: claim scope, pending vs. granted, design-arounds, ownership gaps, prior art."),
        Sub("followup", "Recommended IP Follow-Up", "Recommended IP Follow-Up",
            "Numbered list of IP diligence actions (FTO, assignments, prosecution steps)."),
    )),
    Report("competition", "Competition Report", "Competition Section of the Due Diligence Report",
           "Competition Report", 8, (
        _intro(),
        Sub("landscape", "Competitive Landscape", "Competitive Landscape",
            "How customers solve the problem today and the categories of competition (including the "
            "status quo), with a table of categories."),
        Sub("profiles", "Competitor Profiles", "Competitor Profiles",
            "8-12 numbered profiles (H3 '1. Name' etc.) covering every competitor or alternative named in "
            "the dataroom plus the important ones found by web search: what they offer, ownership/"
            "funding, regulatory status, pricing, market presence, strengths, weaknesses, and threat to "
            "the company. Use web search to verify current facts."),
        Sub("comparison", "Comparison of Offerings", "Comparison of Offerings",
            "Feature/attribute comparison table of the company vs. each competitor."),
        Sub("threat", "Threat Assessment", "Threat Assessment",
            "Table: competitor | threat level | why | outlook."),
        Sub("trends", "Industry Trends", "Industry Trends",
            "Trends affecting the market (regulation, reimbursement, consolidation, technology), cited."),
        Sub("opps_threats", "Opportunities and Threats", "Opportunities and Threats",
            "Bulleted opportunities and threats arising from the landscape."),
        Sub("strategic", "Strategic Implications", "Strategic Implications",
            "What the company must do to win, in the Team's view."),
        Sub("sources", "Sources", "Sources",
            "Numbered list of the public sources used (publisher, title, URL, date retrieved)."),
    )),
    Report("competition_supp", "Competition Report Supplement",
           "Competitive Advantage, Industry Overview and Completeness Review", "Competition Report Supplement", 9, (
        _intro(),
        Sub("part_a", "Part A. Advantage of the {company} Solution Over Each Competitor",
            "Part A. Advantage of the CARI Solution Over Each Competitor",
            "For each competitor, the company's specific advantage; H3 'Head-to-head scorecard' table; H3 "
            "'Which advantages are demonstrated' separating proven advantages from claimed ones."),
        Sub("part_b", "Part B. Industry Overview and Major Trends by Sector", "Part B. Industry Overview and Major Trends by Sector",
            "The industry's structure, size and major trends sector by sector, with citations."),
        Sub("part_c", "Part C. Completeness Review of the Competition Report", "Part C. Completeness Review of the Competition Report",
            "H3 'Items in the report that need verification' and H3 'Omitted content that affects the "
            "analysis' -- an honest audit of the company's own competitive claims and materials."),
        Sub("part_d", "Part D. Deficiencies of {company} Relative to Competitors", "Part D. Deficiencies of CARI Health Relative to Competitors",
            "Where competitors are ahead (scale, clearance, funding, distribution, menu, data)."),
        Sub("next_steps", "Recommended Next Steps", "Recommended Next Steps",
            "Numbered list of competitive diligence and strategy actions."),
        Sub("sources", "Sources", "Sources", "Numbered list of public sources used."),
    )),
    Report("exit_summary", "Executive Summary of Exit Strategy Findings", "Exit Strategy Section of the Due Diligence Report",
           "Executive Summary of Exit Strategy Findings", 10, (
        _intro(),
        Sub("key_facts", "Key Facts Relevant to Exit", "Key Facts Relevant to Exit",
            "Table or bullets of the facts that drive exit value (stage, clearance timing, revenue, "
            "capital needed, cap structure, IP)."),
        Sub("findings", "Key Findings", "Key Findings", "Numbered key findings of the exit analysis."),
        Sub("reasons", "Reasons to Invest (Exit Perspective)", "Reasons to Invest (Exit Perspective)",
            "Numbered reasons."),
        Sub("risks", "Risks When Investing (Exit Perspective)", "Risks When Investing (Exit Perspective)",
            "Numbered risks."),
    )),
    Report("exit_report", "Exit Strategy Report", "Exit Strategy Section of the Due Diligence Report",
           "Exit Strategy Report", 11, (
        _intro(),
        Sub("proposed", "Proposed Exit Strategy", "Proposed Exit Strategy",
            "The company's stated exit plan (type, timing, target value, likely buyers) and its basis."),
        Sub("cited_comps", "Comparable Transactions Cited by {company}", "Comparable Transactions Cited by CARI Health",
            "Table of every comparable the company cites (target | acquirer | year | value | multiple | "
            "relevance), verified by web search, with the Team's view of each one's relevance."),
        Sub("more_comps", "Five Additional Comparable Exits", "Five Additional Comparable Exits",
            "Five further relevant exits found by web search, in a table with the same columns, sourced."),
        Sub("roi", "Estimated ROI to {company} Investors Based on Comparable Exits",
            "Estimated ROI to CARI Health Investors Based on Comparable Exits",
            "Exit value scenarios (low/base/high) from the comparables, the investor's ownership after "
            "projected dilution, resulting MOIC and IRR by scenario in a table, and an H3 'Risk-Weighted "
            "View' applying explicit probabilities."),
        Sub("swot", "Strengths and Weaknesses of the Proposed Exit Plan", "Strengths and Weaknesses of the Proposed Exit Plan",
            "Numbered strengths and weaknesses."),
    )),
    Report("acquirers", "Potential Acquirers Report", "Exit Strategy Section of the Due Diligence Report",
           "Potential Acquirers Report", 12, (
        _intro("After the callout, give a table of 8-15 named potential acquirers found by web search "
               "(acquirer | type | why a fit | relevant recent deals | likelihood), grouped by tier."),
        Sub("paths", "Most Likely Exit Paths", "Most Likely Exit Paths",
            "The 2-4 most likely exit paths, each with buyer type, timing trigger and indicative value."),
    )),
    Report("exit_recs", "Exit Strategy Recommendations", "Exit Strategy Section of the Due Diligence Report",
           "Exit Strategy Recommendations", 13, (
        _intro(),
        Sub("recs", "Recommendations", "Recommendations",
            "Specific, actionable recommendations to the company and investors to maximize exit value "
            "(board-approved triggers, buyer relationship-building, closing diligence gaps acquirers "
            "will probe, evidence to generate, return reporting). One callout block per recommendation "
            "is acceptable, as is a numbered list."),
    )),
    Report("contracts", "Legal Review of Corporate, Contract and Deal Documents", "Contracts Section of the Due Diligence Report",
           "Legal Review of Corporate, Contract and Deal Documents", 14, (
        _intro(),
        Sub("corporate", "Corporate Documents", "Corporate Documents",
            "Brief contract-focused review of the corporate documents that govern contracting authority, "
            "equity grants and approvals."),
        Sub("material", "Material Contracts", "Material Contracts",
            "Every agreement in the dataroom, grouped by H3: 'Employment Agreements', 'Consulting and "
            "Development Agreements', 'Advisory Agreements', 'Separation Agreements', 'Option Holders "
            "Without Agreements', 'Supplier, Manufacturing and Facility Agreements', 'Non-Disclosure "
            "Agreements', 'Government Grants', 'Customer Letters, MOUs and Site Documents' (omit a group "
            "only if nothing exists, and say so). For each group a table: agreement | parties | date | "
            "executed? | key terms (fees, term, IP assignment, termination, change of control) | issue."),
        Sub("deal", "Deal Documents", "Deal Documents",
            "Key terms of the round's documents in a table (term | note | warrant | side letter), executed "
            "copies on file, side letters, and inconsistencies."),
        Sub("summary", "Summary of Findings and Documents Requested", "Summary of Findings and Documents Requested",
            "Summary of the contract findings, then a table of documents requested: document | reason | priority."),
    )),
    Report("regulatory", "Regulatory and Project Plan Report", "Regulatory Section of the Due Diligence Report",
           "Regulatory and Project Plan Report", 15, (
        _intro("If the business is not regulated by a product-approval agency, say so plainly and use "
               "this report for licensing, compliance, data-privacy and certification requirements "
               "instead, keeping the same subsection structure."),
        Sub("background", "Project Background", "Project Background",
            "How the product program got here (pivots, prior programs, funding of the program)."),
        Sub("product", "Product Description", "Product Description",
            "H3 'Finished Product Description' (exactly as the latest documents describe it) and H3 "
            "'Proposed Intended Use' (the intended-use / indications statement, user, setting)."),
        Sub("path", "Regulatory Path", "Regulatory Path",
            "Classification, pathway (e.g. 510(k), De Novo, PMA, CLIA waiver, CE), predicates, and the "
            "agency interactions to date (pre-subs and what the agency said)."),
        Sub("strategy", "Regulatory Strategy", "Regulatory Strategy",
            "The sequencing and the Team's assessment of risk in the strategy."),
        Sub("completed", "Completed Activities", "Completed Activities",
            "Table of completed regulatory/development activities with dates and evidence (incl. design "
            "freeze, verification/validation, submissions)."),
        Sub("qms", "Quality Management System", "Quality Management System",
            "QMS status (ISO 13485/21 CFR 820 or equivalent), SOPs, design controls, gaps."),
        Sub("clinical", "Clinical Studies and IRB", "Clinical Studies and IRB",
            "Each planned/ongoing study: design, n, sites, IRB approval and date, endpoints, timing."),
        Sub("timeline", "Project Timeline", "Project Timeline",
            "Table of the project plan from now to clearance/launch: milestone | target date | status | "
            "dependency, from the company's own plan, with the Team's view of slippage risk."),
        Sub("conclusion", "Conclusion/Summary", "Conclusion/Summary",
            "H3 'Regulatory' and H3 'Project Plan' conclusions."),
    )),
    Report("open_items", "Open Items and Documents Requested", "Consolidated Diligence Follow-Up", "Exit Strategy Recommendations", 16, (
        _intro("After the callout, a table consolidating EVERY open item raised anywhere in the report "
               "and every expected document missing from the dataroom: # | item / document | report "
               "area | why it matters | requested from | priority (before closing / after closing). "
               "Then a short numbered list of the 5 items that most affect the investment decision."),
    )),
)

REPORT_BY_KEY: dict[str, Report] = {r.key: r for r in REPORTS}


@dataclass(frozen=True)
class SectionSpec:
    """One Stage 2 drafting task: writes `subs` (in order) of one report."""
    task_key: str
    report: str
    subs: tuple[str, ...]
    evidence_tags: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    needs_web_search: bool = False
    summary_context: bool = False   # dependencies rendered as summaries, not in full
    instruction: str = ""

    @property
    def section_keys(self) -> tuple[str, ...]:
        return tuple(section_key(self.report, s) for s in self.subs)

    def produces_intro(self) -> bool:
        return INTRO in self.subs


_DRAFT_SPECS: list[SectionSpec] = [
    SectionSpec("commercial_1", "commercial", (INTRO, "sales", "models", "marketing", "customer_input"),
                ("commercial", "financial", "competition", "regulatory")),
    SectionSpec("commercial_2", "commercial", ("market_size", "swot", "conclusion"),
                ("commercial", "financial", "competition"), depends_on=("commercial_1",), needs_web_search=True),
    SectionSpec("financial_1", "financial", (INTRO, "assumptions", "historical", "proforma"),
                ("financial", "deal_terms", "commercial")),
    SectionSpec("financial_2", "financial", ("investment", "use_of_funds", "sufficiency", "valuation"),
                ("financial", "deal_terms", "corporate"), depends_on=("financial_1",)),
    SectionSpec("financial_3", "financial", ("reasons", "risks", "conclusion"),
                ("financial", "deal_terms"), depends_on=("financial_1", "financial_2")),
    SectionSpec("scitech_1", "scitech", (INTRO, "technical", "unique", "risks"),
                ("science_tech", "regulatory", "manufacturing", "ip")),
    SectionSpec("scitech_2", "scitech", ("manufacturing", "suppliers", "followup"),
                ("manufacturing", "science_tech", "contracts"), depends_on=("scitech_1",)),
    SectionSpec("hr_1", "hr", (INTRO,), ("team", "contracts", "corporate", "financial")),
    SectionSpec("hr_2", "hr", ("mgmt_bios", "board_bios", "advisory_bios"), ("team",)),
    SectionSpec("corp_legal", "corp_legal", (INTRO, "corporate", "deal", "findings", "checklist", "followup"),
                ("corporate", "deal_terms", "financial")),
    SectionSpec("ip", "ip", (INTRO, "dates", "strengths", "weaknesses", "followup"),
                ("ip", "science_tech", "contracts", "team")),
    SectionSpec("competition_1", "competition", (INTRO, "landscape", "profiles"),
                ("competition", "commercial", "science_tech"), needs_web_search=True),
    SectionSpec("competition_2", "competition",
                ("comparison", "threat", "trends", "opps_threats", "strategic", "sources"),
                ("competition", "commercial"), depends_on=("competition_1",), needs_web_search=True),
    SectionSpec("competition_supp", "competition_supp",
                (INTRO, "part_a", "part_b", "part_c", "part_d", "next_steps", "sources"),
                ("competition", "commercial", "science_tech"),
                depends_on=("competition_1", "competition_2"), needs_web_search=True),
    SectionSpec("exit_report", "exit_report", (INTRO, "proposed", "cited_comps", "more_comps", "roi", "swot"),
                ("exit", "financial", "deal_terms", "competition"),
                depends_on=("financial_2",), needs_web_search=True),
    SectionSpec("acquirers", "acquirers", (INTRO, "paths"), ("exit", "competition", "commercial"),
                depends_on=("exit_report", "competition_1"), needs_web_search=True),
    SectionSpec("exit_recs", "exit_recs", (INTRO, "recs"), ("exit", "contracts", "manufacturing"),
                depends_on=("exit_report", "acquirers")),
    SectionSpec("exit_summary", "exit_summary", (INTRO, "key_facts", "findings", "reasons", "risks"),
                ("exit", "financial"), depends_on=("exit_report", "acquirers", "exit_recs")),
    SectionSpec("contracts_1", "contracts", (INTRO, "corporate", "material"),
                ("contracts", "team", "manufacturing", "corporate", "commercial")),
    SectionSpec("contracts_2", "contracts", ("deal", "summary"),
                ("deal_terms", "contracts", "corporate"), depends_on=("contracts_1",)),
    SectionSpec("regulatory_1", "regulatory", (INTRO, "background", "product", "path", "strategy", "completed"),
                ("regulatory", "science_tech")),
    SectionSpec("regulatory_2", "regulatory", ("qms", "clinical", "timeline", "conclusion"),
                ("regulatory", "science_tech", "contracts"), depends_on=("regulatory_1",)),
]

_SUMMARY_DEPS = tuple(s.task_key for s in _DRAFT_SPECS)

_FINAL_SPECS: list[SectionSpec] = [
    SectionSpec("open_items", "open_items", (INTRO,), (), depends_on=_SUMMARY_DEPS, summary_context=True,
                instruction="Collect the open items from the drafted reports (provided as prior context) "
                            "and the Key Facts sheet's missing-documents list."),
    SectionSpec("executive_summary", "executive_summary",
                tuple(s.key for s in REPORT_BY_KEY["executive_summary"].subs), (),
                depends_on=_SUMMARY_DEPS, summary_context=True,
                instruction="Summarize the drafted reports (provided as prior context) -- every figure must "
                            "match them and the Key Facts sheet. 1-3 dense paragraphs per subsection."),
]

SECTION_SPECS: list[SectionSpec] = _DRAFT_SPECS + _FINAL_SPECS
SPEC_BY_TASK_KEY: dict[str, SectionSpec] = {s.task_key: s for s in SECTION_SPECS}


@dataclass(frozen=True)
class SectionDef:
    """One report_sections row's fixed identity."""
    section_key: str
    title: str
    heading_level: int
    order_index: int
    report: Report
    sub: Sub


def section_defs(company: str) -> list[SectionDef]:
    """Every row a complete report has, in order: each report's Heading 1
    row (its _intro, or an empty heading row if it has none), then its
    Heading 2 subsections."""
    defs = []
    for r in sorted(REPORTS, key=lambda r: r.order):
        has_intro = any(s.key == INTRO for s in r.subs)
        if not has_intro:
            defs.append(SectionDef(r.key, r.title.format(company=company), 1, r.order * 100, r,
                                   Sub(INTRO, "", "_intro", "")))
        for i, s in enumerate(r.subs):
            if s.key == INTRO:
                defs.append(SectionDef(r.key, r.title.format(company=company), 1, r.order * 100, r, s))
            else:
                defs.append(SectionDef(section_key(r.key, s.key), s.title.format(company=company), 2,
                                       r.order * 100 + i + 1, r, s))
    return defs


def _validate() -> None:
    """Fail at import, not mid-job, if the graph is inconsistent."""
    produced = {}
    for spec in SECTION_SPECS:
        report = REPORT_BY_KEY[spec.report]
        for s in spec.subs:
            report.sub(s)  # raises if unknown
            key = section_key(spec.report, s)
            assert key not in produced, f"{key} produced by both {produced[key]} and {spec.task_key}"
            produced[key] = spec.task_key
        for dep in spec.depends_on:
            assert dep in SPEC_BY_TASK_KEY, f"{spec.task_key} depends on unknown {dep}"
    # A report with no _intro sub still gets a bare Heading 1 row, which
    # no task writes -- everything else must have exactly one producer.
    expected = {d.section_key for d in section_defs("X") if d.sub.guidance}
    missing = expected - set(produced)
    assert not missing, f"no task produces {sorted(missing)}"


_validate()
