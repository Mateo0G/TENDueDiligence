"""Deterministic (no-LLM) classification for Phase 2 intake.

Two independent jobs:
  1. categorize_file  -- which of the 8 dataroom categories a single file
     belongs to, from its path/filename alone.
  2. classify_vertical -- which of the 4 company verticals a job belongs
     to, from every uploaded filename (used to decide whether Stage 4's
     Life Science Assessment block runs).

Per the brief: start with keyword/filename matching, only add an LLM
classification pass (Haiku) if this proves unreliable in practice. Nothing
here calls the Claude API.
"""
import re
from collections import Counter

# The 8 dataroom categories named explicitly in the project brief's Stage 1
# description. Order matters as a tie-break: earlier categories win ties.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "financial": [
        "financial", "finance", "p&l", "profit_loss", "profitloss",
        "balance_sheet", "cash_flow", "cashflow", "proforma", "pro_forma",
        "budget", "revenue", "burn_rate", "runway", "valuation",
        "term_sheet", "termsheet", "income_statement", "forecast",
        "unit_economics", "cap_table", "captable",
    ],
    "legal_corporate": [
        "certificate_of_incorporation", "bylaws", "operating_agreement",
        "articles_of_incorporation", "board_minutes", "board_resolution",
        "stock_purchase", "safe_agreement", "convertible_note",
        "incorporation", "shareholder", "founder_agreement", "vesting",
        "corporate",
    ],
    "ip": [
        "patent", "trademark", "copyright", "intellectual_property",
        "invention_assignment", "ip_assignment", "trade_secret",
        "patent_license", "provisional_application", "uspto",
    ],
    "hr": [
        "resume", "cv_", "org_chart", "orgchart", "employee",
        "offer_letter", "employment_agreement", "background_check",
        "advisor_agreement", "equity_grant", "personnel", "biosketch",
        "bio_",
    ],
    "sales_marketing": [
        "sales", "marketing", "customer", "pipeline", "crm", "gtm",
        "go_to_market", "market_research", "market_size", "tam_sam_som",
        "brand", "campaign", "lead_gen", "channel_partner", "distributor",
    ],
    "science_tech": [
        "technical", "technology", "architecture", "product_spec",
        "prototype", "engineering", "design_doc", "clinical",
        "preclinical", "science", "study_report", "protocol", "trial",
        "biomarker", "assay", "manufacturing", "cmc", "sop", "qa_qc",
        "validation_report", "white_paper", "r&d", "rd_plan",
    ],
    "contracts": [
        "contract", "agreement", "msa", "nda", "vendor", "supplier",
        "customer_agreement", "partnership_agreement", "lease",
        "purchase_order", "statement_of_work", "sow_", "reseller",
        "distribution_agreement", "license_agreement",
    ],
    "regulatory": [
        "fda", "510k", "510_k", "ce_mark", "iso_", "regulatory",
        "compliance", "hipaa", "gdpr", "clearance", "approval_letter",
        "submission", "de_novo", "denovo", "pma_", "ind_", "predicate",
    ],
}

VERTICAL_KEYWORDS: dict[str, list[str]] = {
    "life_sciences": [
        "clinical", "fda", "patient", "biomarker", "trial", "preclinical",
        "pharma", "biotech", "drug", "therapeutic", "diagnostic",
        "medical_device", "510k", "510_k", "ind_", "cro_", "gmp", "ich",
        "in_vivo", "in_vitro", "assay", "biosketch", "irb", "predicate",
    ],
    "deep_tech": [
        "quantum", "semiconductor", "robotics", "hardware", "sensor",
        "battery", "materials_science", "photonics", "aerospace",
        "propulsion", "nanotech", "chip_design", "asic", "fpga",
    ],
    "saas": [
        "arr", "mrr", "churn", "saas", "subscription", "api_", "cloud",
        "dashboard", "b2b_software", "seat_license", "onboarding",
        "user_engagement",
    ],
}
# Tie-break priority when two verticals score equally: life sciences wins
# over deep tech (e.g. a medical *device* company shows both hardware and
# clinical/regulatory signals, but the regulatory signal is what actually
# decides whether Stage 4's Life Science Assessment block should run).
VERTICAL_PRIORITY = ["life_sciences", "deep_tech", "saas", "general_tech"]


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9&]+", "_", text.lower())


def _score(haystack: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in haystack)


def categorize_file(filename: str, path: str = "") -> tuple[str | None, str]:
    """Returns (category, method). category is None if no keyword matched."""
    haystack = _normalize(f"{path}/{filename}")

    scores = {cat: _score(haystack, kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    best_category = max(scores, key=lambda c: scores[c])
    if scores[best_category] == 0:
        return None, "deterministic"
    return best_category, "deterministic"


def classify_vertical(filenames: list[str], pitch_deck_text: str = "") -> tuple[str, dict]:
    """Returns (vertical, signals). signals records which keywords matched
    and how many times, for storage in jobs.vertical_signals so a human can
    sanity-check the call without re-running the classifier.
    """
    haystack = _normalize(" ".join(filenames) + " " + pitch_deck_text)

    matched: dict[str, list[str]] = {}
    scores: Counter = Counter()
    for vertical, keywords in VERTICAL_KEYWORDS.items():
        hits = [kw for kw in keywords if kw in haystack]
        if hits:
            matched[vertical] = hits
            scores[vertical] = len(hits)

    if not scores:
        return "general_tech", {"matched": {}, "reason": "no vertical keywords found"}

    best_score = max(scores.values())
    tied = [v for v, s in scores.items() if s == best_score]
    vertical = min(tied, key=VERTICAL_PRIORITY.index)

    return vertical, {"matched": matched, "scores": dict(scores)}
