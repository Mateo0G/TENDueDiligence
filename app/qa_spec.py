"""Stage 4 task graph: the DD Assessment block, the conditional Life Science
Assessment block, the master validation check, and the Report Card
aggregation -- per docs/prompt_inventory.md §7/§8/§9.

Deliberate reductions from the raw prompt list (flagged, not silent):

- "Check for missing data" (prompt_inventory.md Flag A4) is dropped -- it
  reads as a near-duplicate of "Key Information Check" (both scan the
  compiled report + dataroom for missing information), and running both
  would spend a second call to re-derive the same signal.
- "Create minimum doc list" is dropped -- its own prompt text is generic
  and not company-specific ("determine the list of 'must have' documents
  to create a due diligence report"); it reads as a one-time reference
  utility, not a per-report QA task, so running it per job would just
  regenerate the same generic content every time.
- "Red Flag Forensics Detector"'s polarity was unstated in the source
  (Flag A2), while the Life Science block's own red-flag prompt explicitly
  says "lower is worse". Resolved here by using that same convention for
  both: higher score = fewer red flags = better, so Report Card can
  aggregate every score on one consistent "higher is better" axis.
- "Report Card of scores" appeared twice in the source sheet (once after
  the DD Assessment block, once after Life Science -- Flag A3). Resolved
  by making it a single task that depends on every DD Assessment task
  *and* every Life Science task when the block applies, so it only ever
  runs once, after everything is done -- and by making it deterministic
  (pure aggregation of already-computed qa_scores rows) rather than a
  second Claude call, since its own source prompt ("Display a summary
  report card showing all scores") has nothing left to reason about once
  the individual scores already exist.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class QASpec:
    task_key: str
    assessment_name: str
    block: str  # 'dd_assessment' | 'life_science_assessment' | 'master_validation'
    scale: str  # '0-100' | '1-10'
    has_score: bool
    instruction: str


DD_ASSESSMENT_SPECS: list[QASpec] = [
    QASpec(
        task_key="dd_comprehensiveness_validator",
        assessment_name="Comprehensiveness Validator",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Act as a due diligence reviewer. Assess whether the report covers all core "
            "areas: market, business model, financials, legal/IP, technology, risk, team, "
            "and operations. Identify gaps, missing data, and underdeveloped sections, and "
            "score comprehensiveness 0-100."
        ),
    ),
    QASpec(
        task_key="dd_evidence_traceability_checker",
        assessment_name="Evidence Traceability Checker",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Review the report and map each claim to supporting evidence in the dataroom. "
            "Flag unverified statements, weak evidence, missing files, or claims requiring "
            "validation. Score evidence strength 0-100."
        ),
    ),
    QASpec(
        task_key="dd_internal_consistency_analyzer",
        assessment_name="Internal Consistency Analyzer",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Scan the report and dataroom for contradictions in financials, timelines, "
            "terminology, customer numbers, or technical claims. Flag inconsistencies and "
            "rate overall internal consistency 0-100."
        ),
    ),
    QASpec(
        task_key="dd_key_information_check",
        assessment_name="Key Information Check",
        block="dd_assessment",
        scale="0-100",
        has_score=False,  # Flag A1: this one prompt has no numeric score in the source
        instruction=(
            "Review the due diligence report for key information and scan the dataroom to "
            "check for missing data. List the missing information. Do not assign a numeric "
            "score for this check."
        ),
    ),
    QASpec(
        task_key="dd_financial_rigor_stress_test",
        assessment_name="Financial Rigor Stress Test",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Analyze all financial projections, assumptions, and unit economics in the "
            "report and dataroom. Stress-test the model, flag unrealistic assumptions, and "
            "score financial rigor 0-100."
        ),
    ),
    QASpec(
        task_key="dd_market_reality_alignment_audit",
        assessment_name="Market Reality Alignment Audit",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Evaluate the market section for data-supported claims, competitor grounding, "
            "realistic adoption curves, and third-party corroboration. Score market accuracy "
            "and realism 0-100."
        ),
    ),
    QASpec(
        task_key="dd_risk_disclosure_depth_test",
        assessment_name="Risk Disclosure Depth Test",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Review the report's risk disclosures. Identify missing risks, weak mitigation "
            "plans, unfounded optimism, or disguised risks. Score disclosure completeness "
            "0-100."
        ),
    ),
    QASpec(
        task_key="dd_technical_validity_review",
        assessment_name="Technical Validity & Plausibility Review",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Evaluate technical claims against scientific plausibility, benchmarking, known "
            "failure modes, and validation data. Flag exaggerated claims and score technical "
            "validity 0-100."
        ),
    ),
    QASpec(
        task_key="dd_dataroom_integrity_check",
        assessment_name="Dataroom Integrity & Organization Check",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Assess dataroom structure, completeness, version history, file organization, "
            "and alignment with items referenced in the report. Flag missing or outdated "
            "items. Score integrity 0-100."
        ),
    ),
    QASpec(
        task_key="dd_clarity_structure_writing_scan",
        assessment_name="Clarity, Structure & Writing Quality Scan",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Review the report for clarity, logical flow, concise explanations, definitions "
            "of key terms, and readability. Flag unclear sections and score clarity 0-100."
        ),
    ),
    QASpec(
        task_key="dd_red_flag_forensics_detector",
        assessment_name="Red Flag Forensics Detector",
        block="dd_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Perform a forensic review to detect red flags: inconsistent metrics, "
            "unverifiable claims, inflated market sizes, missing risks, or over-polished "
            "narratives. Provide findings and a red-flag score 0-100, where a HIGHER score "
            "means FEWER red flags (i.e. higher is better, consistent with every other score "
            "in this report)."
        ),
    ),
]

LIFE_SCIENCE_SPECS: list[QASpec] = [
    QASpec(
        task_key="ls_biological_plausibility_validator",
        assessment_name="Biological Plausibility Validator",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Evaluate the biological rationale including mechanism of action, target "
            "validity, biomarker logic, and disease-pathway relevance. Verify all biological "
            "claims with data or references. Score 0-100."
        ),
    ),
    QASpec(
        task_key="ls_preclinical_evidence_strength_checker",
        assessment_name="Preclinical Evidence Strength Checker",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Analyze preclinical data for robustness: controls, sample sizes, p-values, "
            "model selection, translational relevance, and dose-response consistency. Score "
            "0-100."
        ),
    ),
    QASpec(
        task_key="ls_clinical_readiness_feasibility_audit",
        assessment_name="Clinical Readiness & Feasibility Audit",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Assess trial design, endpoints, feasibility, CRO readiness, regulatory input, "
            "inclusion/exclusion criteria, comparator selection, and enrollment risk. Score "
            "0-100."
        ),
    ),
    QASpec(
        task_key="ls_regulatory_pathway_validation_tool",
        assessment_name="Regulatory Pathway Validation Tool",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Review regulatory assumptions, precedents, classification, endpoints, safety "
            "burden, approval roadmap, and evidence requirements. Score 0-100."
        ),
    ),
    QASpec(
        task_key="ls_cmc_manufacturability_risk_scanner",
        assessment_name="CMC & Manufacturability Risk Scanner",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Analyze CMC readiness: process robustness, release testing, scalability, QC "
            "methods, supply chain risk, and validation status. Score 0-100."
        ),
    ),
    QASpec(
        task_key="ls_ip_strength_fto_review",
        assessment_name="IP Strength & Freedom-to-Operate Review",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Review the IP estate for breadth, composition of matter, method claims, "
            "geography, expirations, litigation exposure, and freedom-to-operate searches. "
            "Score 0-100."
        ),
    ),
    QASpec(
        task_key="ls_commercial_pricing_realism_scan",
        assessment_name="Commercial & Pricing Realism Scan",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Assess pricing assumptions, reimbursement barriers, payer evidence, real-world "
            "utilization, market access challenges, and competitive benchmarks. Score 0-100."
        ),
    ),
    QASpec(
        task_key="ls_competitive_differentiation_forensics",
        assessment_name="Competitive Differentiation Forensics",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Compare product attributes vs. competitors: efficacy, safety, durability, "
            "convenience, and cost-effectiveness. Identify overstated advantages. Score "
            "0-100."
        ),
    ),
    QASpec(
        task_key="ls_data_integrity_traceability_audit",
        assessment_name="Data Integrity & Traceability Audit",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Trace every scientific and clinical claim to dataroom files: raw data, study "
            "reports, protocols, and QA logs. Flag missing documentation. Score 0-100."
        ),
    ),
    QASpec(
        task_key="ls_red_flag_clinical_scientific_risk_detector",
        assessment_name="Red Flag Clinical & Scientific Risk Detector",
        block="life_science_assessment",
        scale="0-100",
        has_score=True,
        instruction=(
            "Perform a forensic scan to identify scientific, clinical, regulatory, or "
            "commercial red flags. Provide a red-flag score 0-100 where a HIGHER score means "
            "FEWER red flags (lower is worse)."
        ),
    ),
]

MASTER_VALIDATION_SPEC = QASpec(
    task_key="master_validation",
    assessment_name="Master Validation",
    block="master_validation",
    scale="1-10",
    has_score=True,
    instruction=(
        "Validate and verify this report by cross-checking evidence, assessing consistency, "
        "and evaluating reasoning quality. Score factual integrity, logical soundness, and "
        "overall reliability on a 1-10 scale."
    ),
)

REPORT_CARD_TASK_KEY = "report_card"
REPORT_CARD_ASSESSMENT_NAME = "Report Card of scores"
