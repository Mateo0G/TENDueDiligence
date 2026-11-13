"""Claude API calls for every stage. Request-building is split from
request-*sending* throughout this module (`build_*_request` vs. the plain
functions that call the API on the result) so that app/batch_runner.py can
submit the exact same requests through the Batch API instead -- same
prompts, same prompt-caching breakpoints, same structured-output schema,
just a different transport.
"""
import json

import anthropic

from app.models import (
    DocumentDigest,
    DraftingOutput,
    FileCategoryOutput,
    GapAnalysisOutput,
    KeyFactsOutput,
    MitigationOutput,
    QAScoreOutput,
)
from app.qa_spec import QASpec
from app.sections_spec import INTRO, REPORT_BY_KEY, SectionSpec, section_key

DRAFTING_MODEL = "claude-sonnet-5"
QA_MODEL = "claude-sonnet-5"  # per the brief's model routing: Sonnet for QA scoring
GAP_MITIGATION_MODEL = "claude-sonnet-5"
DIGEST_MODEL = "claude-sonnet-5"  # per-file digests: extraction quality drives every section
KEY_FACTS_MODEL = "claude-sonnet-5"
CATEGORIZE_MODEL = "claude-haiku-4-5-20251001"  # intake fallback only -- cheap/fast, not quality-critical


def _strict_schema(schema):
    """Two fixups Anthropic's raw output_config needs that Pydantic's plain
    model_json_schema() doesn't produce on its own (both a 400 otherwise):
    (1) `additionalProperties: false` explicit on every object schema, and
    (2) discriminated unions (app.models.ContentBlock) come out as `oneOf` +
    a `discriminator` key -- Anthropic's schema validator rejects the
    `oneOf` keyword -- so rewrite to `anyOf` and drop `discriminator`
    (safe here since every union member has a distinct required literal
    `type`, so the variants are already mutually exclusive without it).
    Walks $defs too, since discriminated unions live there.
    """
    if isinstance(schema, dict):
        if "oneOf" in schema:
            schema["anyOf"] = schema.pop("oneOf")
            schema.pop("discriminator", None)
        if schema.get("type") == "object" and "additionalProperties" not in schema:
            schema["additionalProperties"] = False
        for value in schema.values():
            _strict_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            _strict_schema(item)
    return schema


def _output_config(model_cls) -> dict:
    return {"format": {"type": "json_schema", "schema": _strict_schema(model_cls.model_json_schema())}}


def _create(client, **params):
    """Every call in this module with max_tokens this high needs this: the
    SDK estimates a plain non-streaming create() at a high max_tokens could
    run past its 10-minute cutoff and refuses the call outright before even
    sending it. Streaming has no such cap -- same params, same parsed
    result either way."""
    with client.messages.stream(**params) as stream:
        return stream.get_final_message()


def parse_structured_response(response, model_cls):
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's response was cut off at the max_tokens limit before it finished "
            "(no complete JSON output) -- raise max_tokens for this call or shorten its input"
        )
    # With web search the response carries several text blocks (narration
    # between searches); the structured JSON is the last one.
    text = [b.text for b in response.content if b.type == "text"][-1]
    return model_cls.model_validate(json.loads(text))


CACHE_TTL = {"type": "ephemeral", "ttl": "1h"}  # a job's Stage 2 (~24 calls)
# and Stage 4 (~23 calls) each run well past the default 5-minute cache TTL,
# so the 1h TTL is what makes caching pay off across an entire stage.

QA_SYSTEM_PROMPT = """\
You are an independent due-diligence QA reviewer auditing a due diligence \
report prepared by another analyst, together with the source dataroom the \
report was built from (condensed as a Key Facts sheet plus a digest of \
every document).

Be skeptical and specific: cite what you actually found (or didn't find) \
in the report and dataroom evidence provided, rather than general \
commentary. Where the source material genuinely doesn't address something, \
say so plainly rather than inferring or guessing.

Respond with JSON only, matching the required schema exactly. Every score \
in this system is on a "higher is better" scale -- including any red-flag \
score, where a higher number means fewer red flags were found, never the \
reverse.
"""

SYSTEM_PROMPT = """\
You are a senior analyst on TEN Capital Network's Due Diligence Team, writing \
one part of an institutional-quality venture due diligence report for \
investors. The standard is the Team's client-approved reports: precise, \
dense with verified figures, skeptical, and explicit about what is and is \
not established.

How the Team writes:
- Every factual statement is grounded in the dataroom documents, the pitch \
deck, the Key Facts sheet, or (where web search is available) a cited public \
source. Use exact figures, dates, names, titles, share counts, prices and \
terms -- "$280,000 of the $1.0 million note is funded", not "the round is \
partly funded". Never invent or estimate a number the sources don't support.
- Name the document a fact comes from when it matters, the way the Team \
does: "per the financial model dated October 25, 2026", "the signed offer \
letter", "the April 2025 Pre-Submission minutes".
- When sources disagree, use the most recent, executed, or most \
authoritative version (as the Key Facts sheet resolves it) and say what \
the other version said if the difference matters to an investor.
- Describe the product, technology, specimen/input, and business model \
exactly as the company's latest technical documents do. Do not substitute \
a familiar-sounding description (e.g. calling a whole-blood test a \
fingerstick test) unless the documents say so.
- Separate what the company states from what the Team concludes: "The \
Company states...", "The Team considers...". Assessments are the Team's.
- When the dataroom lacks something this section would normally cover, say \
so in one plain sentence ("The data room contains no executed supply \
agreement.") and make it a follow-up item. Never pad a gap with generic text.
- Unsigned, draft, or non-binding documents are identified as such.
- Depth: match the reference report. A typical subsection is 300-900 words \
plus its tables; tables are used for anything with several attributes per \
item (rosters, agreements, financials by year, comparables, timelines, \
checklists), and every row is filled from the sources.

The reference report excerpts you are shown are from a DIFFERENT company. \
Use them only for structure, voice, depth, and table design; never copy \
their facts, names, or figures.

Output format: JSON only, matching the schema. Each section is a list of \
blocks: "paragraph" (narrative); "heading" with level 3 (a sub-heading \
inside the subsection) or level 4 (a bold underlined run-in label such as \
"Due Diligence Team Assessment" or "Strengths observed:"); "table" (caption \
may be ""; every row has exactly as many cells as headers); "callout" (the \
shaded Summary of Findings box); "bullets" (numbered true for ranked or \
sequential items); "note" (a small italic source line, typically \
"Source: ..." directly after a table). Inside any text you may use \
**double asterisks** for a short bold lead-in phrase; no other markdown. \
Do not repeat the subsection's own title as a heading -- it is added for you.
"""


def _drafting_system_blocks(job_context: str) -> list[dict]:
    # Two cache breakpoints: the static system prompt (shared by every job)
    # and the per-job context (Key Facts + document index + pitch deck,
    # shared by every drafting call of this job). Everything task-specific
    # comes after, in the user turn.
    return [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": CACHE_TTL},
        {"type": "text", "text": job_context, "cache_control": CACHE_TTL},
    ]


def _build_drafting_user_content(
    spec: SectionSpec,
    company_name: str,
    style_excerpt: str,
    evidence_text: str,
    dependency_context: str,
) -> str:
    report = REPORT_BY_KEY[spec.report]
    title = report.title.format(company=company_name)
    lines = [
        "### Reference report: how the Team wrote this part for another company",
        style_excerpt or "(no reference excerpt for this part)",
        "",
        f"### Your task: {title}" + (f" -- {report.header}" if report.header else ""),
        f"Write the following parts of {company_name}'s \"{title}\". Produce exactly one entry in "
        f"`sections` per section_key below, in this order:",
    ]
    for sub_key in spec.subs:
        sub = report.sub(sub_key)
        key = section_key(spec.report, sub_key)
        if sub_key == INTRO:
            heading = "the report's opening (before its first subsection)"
        else:
            heading = f'"{sub.title.format(company=company_name)}"'
        lines.append(f'- section_key "{key}": {heading}. {sub.guidance.format(company=company_name)}')
    if spec.produces_intro():
        lines.append(
            "Also fill `basis_of_preparation`: one or two sentences in the Team's style naming the specific "
            "documents (with dates) this report relies on, whether financial statements are audited if "
            "relevant, and that the assessments are the Team's."
        )
    else:
        lines.append('Set `basis_of_preparation` to "".')
    if spec.instruction:
        lines += ["", spec.instruction.format(company=company_name)]
    if spec.needs_web_search:
        lines += ["", "Use web search to verify and extend public facts (competitors, transactions, acquirers, "
                      "market data). Cite each public source by publisher and date in the text or a note."]
    if dependency_context:
        header = ("### The drafted reports" if spec.summary_context
                  else "### Already-drafted parts of this report (stay consistent; do not repeat them)")
        lines += ["", header, dependency_context]
    lines += ["", "### Dataroom evidence for this report", evidence_text]
    return "\n".join(lines)


def _drafting_output_config(spec: SectionSpec) -> dict:
    config = _output_config(DraftingOutput)
    schema = config["format"]["schema"]
    # Pin section_key to exactly this task's keys, so the response can't
    # invent, rename, or misspell a section.
    schema["$defs"]["DraftedSection"]["properties"]["section_key"]["enum"] = list(spec.section_keys)
    return config


def build_drafting_request(
    spec: SectionSpec,
    company_name: str,
    job_context: str,
    style_excerpt: str,
    evidence_text: str,
    dependency_context: str,
) -> dict:
    """Returns kwargs suitable for either client.messages.create(**kwargs)
    or a Batch Request's `params` -- see app/batch_runner.py."""
    user_content = _build_drafting_user_content(spec, company_name, style_excerpt, evidence_text, dependency_context)
    kwargs = dict(
        model=DRAFTING_MODEL,
        # A drafting call now writes several pages (a whole report's worth
        # of subsections with their tables) after high-effort reasoning.
        max_tokens=64000,
        system=_drafting_system_blocks(job_context),
        output_config={"effort": "high", **_drafting_output_config(spec)},
        messages=[{"role": "user", "content": user_content}],
    )
    if spec.needs_web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    return kwargs


def draft_section(
    spec: SectionSpec,
    company_name: str,
    job_context: str,
    style_excerpt: str,
    evidence_text: str,
    dependency_context: str,
) -> DraftingOutput:
    client = anthropic.Anthropic()
    params = build_drafting_request(spec, company_name, job_context, style_excerpt, evidence_text, dependency_context)
    response = _create(client, **params)
    return parse_structured_response(response, DraftingOutput)


DIGEST_SYSTEM_PROMPT = """\
You are a due diligence analyst reading one document from a startup's \
dataroom so that report writers never have to re-read it. Extract \
everything an investor-side diligence team could need from it: exact \
figures, dates, names and titles, parties, terms, results, and anything \
missing, unsigned, inconsistent or unusual. Prefer precision over brevity; \
quote figures exactly as the document states them. Never infer facts the \
document does not contain. Respond with JSON only, matching the schema.
"""

_DIGEST_TAG_GUIDE = (
    "Tags: list every report this document is evidence for -- commercial, financial, deal_terms, corporate, "
    "contracts, team, ip, science_tech, manufacturing, regulatory, competition, exit. A convertible note is "
    "deal_terms + financial + contracts; an employment agreement is team + contracts; a financial model is "
    "financial + commercial (and team if it has a hiring plan); a supply agreement is contracts + "
    "manufacturing; a customer LOI is commercial + contracts."
)


def digest_document(company_name: str, filename: str, path: str, text: str, visual_block: dict | None) -> DocumentDigest:
    client = anthropic.Anthropic()
    content: list[dict] = []
    if visual_block is not None:
        content.append(visual_block)
        intro = ("The document is attached above as page images (it has little or no text layer). "
                 "Read it visually and fill `transcribed_text`.")
        body = ""
    else:
        intro = 'The document\'s extracted text follows. Set `transcribed_text` to "".'
        body = f"\n\n### Document text\n{text}"
    content.append({"type": "text", "text": (
        f"Company under diligence: {company_name}\nDataroom path: {path}\nFilename: {filename}\n\n"
        f"{intro}\n\n{_DIGEST_TAG_GUIDE}{body}"
    )})
    response = _create(
        client,
        model=DIGEST_MODEL,
        max_tokens=32000,
        system=DIGEST_SYSTEM_PROMPT,
        output_config={"effort": "medium", **_output_config(DocumentDigest)},
        messages=[{"role": "user", "content": content}],
    )
    return parse_structured_response(response, DocumentDigest)


KEY_FACTS_SYSTEM_PROMPT = """\
You are the lead analyst on a venture due diligence engagement. From the \
digests of every document in the dataroom, build the Key Facts sheet that \
every section of the report will be written against, so that the whole \
report states the same round size, valuation, share counts, dates, people, \
product description and milestones. Where documents disagree, record the \
conflict and resolve it: prefer the most recent document, executed over \
unsigned, and primary records (charter, executed agreements, financial \
statements, the latest model) over summaries (decks, one-pagers). Include \
every material figure. Respond with JSON only, matching the schema.
"""


def extract_key_facts(company_name: str, digests_text: str) -> KeyFactsOutput:
    client = anthropic.Anthropic()
    response = _create(
        client,
        model=KEY_FACTS_MODEL,
        max_tokens=48000,
        system=KEY_FACTS_SYSTEM_PROMPT,
        output_config={"effort": "high", **_output_config(KeyFactsOutput)},
        messages=[{"role": "user", "content": (
            f"Company under diligence: {company_name}\n\n### Digests of every dataroom document\n{digests_text}"
        )}],
    )
    return parse_structured_response(response, KeyFactsOutput)


def _qa_system_blocks(report_text: str, dataroom_text: str) -> list[dict]:
    return [
        {"type": "text", "text": QA_SYSTEM_PROMPT},
        {
            "type": "text",
            "text": (
                f"### Compiled due diligence report\n{report_text}\n\n"
                f"### Dataroom evidence\n{dataroom_text or '(no dataroom documents were uploaded)'}"
            ),
            "cache_control": CACHE_TTL,
        },
    ]


def build_qa_request(spec: QASpec, company_name: str, report_text: str, dataroom_text: str) -> dict:
    scale_note = (
        f"Score scale for this check: {spec.scale}."
        if spec.has_score
        else "This check does not produce a numeric score -- leave `score` null."
    )
    user_content = f"{spec.instruction.format(company=company_name)}\n\n{scale_note}"
    return dict(
        model=QA_MODEL,
        max_tokens=32000,
        system=_qa_system_blocks(report_text, dataroom_text),
        output_config={"effort": "high", **_output_config(QAScoreOutput)},
        messages=[{"role": "user", "content": user_content}],
    )


def score_assessment(
    spec: QASpec,
    company_name: str,
    report_text: str,
    dataroom_text: str,
) -> QAScoreOutput:
    client = anthropic.Anthropic()
    params = build_qa_request(spec, company_name, report_text, dataroom_text)
    response = _create(client, **params)
    return parse_structured_response(response, QAScoreOutput)


GAP_ANALYSIS_SYSTEM_PROMPT = """\
You are a venture investor's diligence lead identifying gaps and \
weaknesses in a due diligence report before it goes to the investment \
committee. Be specific and concrete -- name the actual missing document, \
unverified claim, or unresolved risk, not a generic category. Ground your \
analysis in what the report and QA review actually say, not general \
startup-investing commentary.

Respond with JSON only, matching the required schema exactly.
"""

MITIGATION_SYSTEM_PROMPT = """\
You are a venture investor's diligence lead proposing concrete mitigation \
steps for each gap identified in a prior gap analysis. For each gap, \
propose a specific, actionable step the company or the investor could take \
before or as a condition of closing -- not a vague recommendation to \
"gather more information."

Respond with JSON only, matching the required schema exactly. Your \
`gap_title` values must exactly match the gap titles you were given.
"""


def analyze_gaps(company_name: str, report_text: str, qa_summary_text: str) -> GapAnalysisOutput:
    client = anthropic.Anthropic()
    prompt = (
        f"Based on the provided {company_name} due diligence report and QA review below, "
        f"what are the potential gaps or weaknesses that investors might perceive in this "
        f"investment?\n\n"
        f"### Compiled due diligence report\n{report_text}\n\n"
        f"### QA assessment scores and findings\n{qa_summary_text}"
    )
    response = _create(
        client,
        model=GAP_MITIGATION_MODEL,
        max_tokens=32000,
        system=GAP_ANALYSIS_SYSTEM_PROMPT,
        output_config={"effort": "high", **_output_config(GapAnalysisOutput)},
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_structured_response(response, GapAnalysisOutput)


def generate_mitigations(company_name: str, gap_analysis: GapAnalysisOutput) -> MitigationOutput:
    client = anthropic.Anthropic()
    gaps_text = "\n\n".join(
        f"- **{g.title}** ({g.severity}): {g.description}" for g in gap_analysis.gaps
    )
    prompt = (
        f"Build a mitigation strategy for each weakness identified in the {company_name} "
        f"due diligence report's gap analysis below.\n\n### Gap analysis\n{gap_analysis.summary}\n\n{gaps_text}"
    )
    response = _create(
        client,
        model=GAP_MITIGATION_MODEL,
        max_tokens=32000,
        system=MITIGATION_SYSTEM_PROMPT,
        output_config={"effort": "high", **_output_config(MitigationOutput)},
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_structured_response(response, MitigationOutput)


CATEGORY_NAMES = (
    "financial", "legal_corporate", "ip", "hr", "sales_marketing",
    "science_tech", "contracts", "regulatory",
)


def categorize_file_llm(filename: str, path: str) -> str | None:
    """Upload-time fallback for a file app/classify.py's keyword matching
    couldn't place -- the category is now only a display label in the UI
    (drafting routes by each file's digest tags instead), so this stays a
    cheap Haiku call. Returns None on a genuine "none" answer or if the call
    itself fails -- intake shouldn't fail an otherwise-good upload over one
    file's classification.
    """
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=CATEGORIZE_MODEL,
            max_tokens=200,
            output_config={"effort": "low", **_output_config(FileCategoryOutput)},
            messages=[{
                "role": "user",
                "content": (
                    "Classify this due-diligence dataroom file into exactly one category "
                    "from its filename and folder path alone.\n\n"
                    f"Path: {path}\nFilename: {filename}\n\n"
                    f"Categories: {', '.join(CATEGORY_NAMES)}. "
                    "Use \"none\" only if it genuinely fits none of them."
                ),
            }],
        )
        result = parse_structured_response(response, FileCategoryOutput)
    except Exception:
        return None
    return None if result.category == "none" else result.category
