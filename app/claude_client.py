"""Claude API calls for every stage. Request-building is split from
request-sending (`build_*_request` vs. the functions that call the API) so
app/batch_runner.py can submit the same requests through the Batch API.

Cost model (see also app/usage.py, which records every call's real cost):
- Reading (Stage 1) uses the cheap, fast model, several documents a call.
- Every drafting call shares ONE byte-identical prefix -- system prompt +
  the job's evidence (Key Facts, research memo, digest pack) -- with the
  same model, no tools and the same output schema, so the prompt cache
  serves the ~70k-token prefix to all ~24 calls at 0.1x the input price.
  A warm-up call writes it once before the drafting wave starts (parallel
  calls only hit a cache entry that already exists).
- Web search happens once per job (research_market), not in seven
  separate drafting calls that each paid for their own searches.
- Stage 4's ~22 checks are one call.
"""
import json
import os

import anthropic

from app import usage
from app.models import (
    CompactDigest,
    DraftingOutput,
    GapAnalysisOutput,
    KeyFactsOutput,
    MitigationOutput,
    QAAllOutput,
    QAScoreOutput,
    ReadOutput,
)
from app.qa_spec import QASpec

DRAFTING_MODEL = "claude-sonnet-5"
QA_MODEL = os.environ.get("QA_MODEL", "claude-sonnet-5")
GAP_MITIGATION_MODEL = "claude-sonnet-5"
KEY_FACTS_MODEL = "claude-sonnet-5"
RESEARCH_MODEL = "claude-sonnet-5"
# Reading the dataroom: Haiku is ~3x cheaper than Sonnet per input token and
# much faster; its job is extraction (figures, dates, parties, terms), not
# judgment -- the drafting model does the analysis. Override with READ_MODEL.
READ_MODEL = os.environ.get("READ_MODEL", "claude-haiku-4-5-20251001")
DRAFTING_EFFORT = os.environ.get("DRAFTING_EFFORT", "medium")
RESEARCH_MAX_SEARCHES = int(os.environ.get("RESEARCH_MAX_SEARCHES", "8"))

# 5-minute TTL: every drafting call that reads the prefix refreshes it, and
# the wave runs back to back, so it never lapses in practice -- the 1-hour
# TTL costs 2x the input price to write instead of 1.25x.
CACHE_TTL = {"type": "ephemeral"}


def _client() -> anthropic.Anthropic:
    # Rate limits (429) and overloads (529) during a 24-call drafting wave
    # are retried with backoff by the SDK instead of failing the task.
    return anthropic.Anthropic(max_retries=6)


def _supports_effort(model: str) -> bool:
    return not model.startswith("claude-haiku-4")


def _strict_schema(schema):
    """Two fixups Anthropic's output_config needs that Pydantic's
    model_json_schema() doesn't produce: `additionalProperties: false` on
    every object, and `oneOf` (from discriminated unions) rewritten to
    `anyOf` without the `discriminator` key."""
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


def _output_config(model_cls, effort: str | None = None, model: str = "") -> dict:
    config = {"format": {"type": "json_schema", "schema": _strict_schema(model_cls.model_json_schema())}}
    if effort and _supports_effort(model):
        config["effort"] = effort
    return config


def _create(client, purpose: str, **params):
    """Streams (the SDK refuses non-streaming calls at high max_tokens) and
    records the call's usage/cost against the current task."""
    with client.messages.stream(**params) as stream:
        response = stream.get_final_message()
    usage.record(purpose, params["model"], response)
    return response


def parse_structured_response(response, model_cls):
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's response was cut off at the max_tokens limit before it finished "
            "(no complete JSON output) -- raise max_tokens for this call or shorten its input"
        )
    text = [b.text for b in response.content if b.type == "text"][-1]
    return model_cls.model_validate(json.loads(text))


# ---------------------------------------------------------------- reading --

READ_SYSTEM_PROMPT = """\
You read documents from a startup's due diligence dataroom for an \
investor-side diligence team and record what each one establishes, so the \
report writers can work from your notes instead of the documents.

Rules:
- Quote figures, dates, names, titles, share counts, prices and terms exactly \
as written. Never infer or estimate anything the document does not say.
- A document may be an excerpt (opening and ending, with the omitted middle \
marked). Record only what you can see.
- Say whether agreements and instruments are executed (signed by the parties) \
or unsigned/draft.
- Describe products and technology exactly as the document does (e.g. do not \
call a whole-blood test a fingerstick test unless the document does).

Importance (1-5) and how much to record:
5 = core: financing documents (term sheet, notes, SAFEs, warrants, \
subscription/purchase agreements), cap table / securities ledger, financial \
statements, financial model/budget, charter, key licenses and IP \
assignments, regulatory submissions and agency correspondence, pitch deck / \
executive summary. Up to 18 key facts.
4 = material: executed material contracts, offer letters / employment and \
consulting agreements with compensation or equity, board and stockholder \
consents (financings, option grants, valuations), patents and applications, \
clinical / validation / test reports, customer letters of support / LOIs, \
market and reimbursement studies, development and project plans. Up to 10.
3 = supporting: minutes, SOPs, investor updates, leases, insurance, \
supplier quotes. Up to 5.
2 = routine: NDAs, forms, certificates, bank statements (record the balance \
and date), invoices. 0-2.
1 = no substance (blank templates, logos). 0.

What to capture by document type:
- Spreadsheets (models, budgets, ledgers, cap tables): the key line items by \
year or holder as facts, e.g. "Revenue: 2026 $0.4M; 2027 $3.1M; 2028 $9.8M"; \
totals (fully diluted shares, option pool, cash, burn, headcount).
- Financing documents: amount, price/cap/discount, interest, maturity, \
conversion terms, investors and amounts, warrants, closing status.
- Agreements: parties, date, term, economics (fees, royalties, milestones, \
equity), exclusivity, IP ownership, termination.
- Employment, offer, consulting and advisor documents: person, title, \
salary/fees, bonus, equity (shares, strike, vesting), start date, IP \
assignment.
- Technical, clinical and regulatory documents: what was tested, how, n, \
results with numbers, dates, agency feedback.

Tags = every report the document is evidence for (usually 1-4): commercial, \
financial, deal_terms, corporate, contracts, team, ip, science_tech, \
manufacturing, regulatory, competition, exit. A convertible note is \
deal_terms + financial + contracts; an offer letter is team + contracts; a \
financial model is financial + commercial; a supply agreement is contracts + \
manufacturing; a customer LOI is commercial + contracts.

Respond with JSON only, matching the schema.
"""


def read_documents(company_name: str, docs: list[tuple[int, str, str, str, int]]) -> ReadOutput:
    """docs: (number, dataroom path, filename, text or excerpt, full length)."""
    parts = [f"Company under diligence: {company_name}\n\nRead each document below and return one entry "
             f"per document in `documents` ({len(docs)} in total), with its number in `doc`."]
    for n, path, filename, text, full_len in docs:
        note = f" (excerpt of a {full_len:,}-character document)" if len(text) < full_len else ""
        parts.append(f"=== Document {n} ===\nPath: {path}\nFile: {filename}{note}\n\n{text}")
    response = _create(
        _client(), "read",
        model=READ_MODEL,
        max_tokens=16000,
        system=READ_SYSTEM_PROMPT,
        output_config=_output_config(ReadOutput, "low", READ_MODEL),
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )
    return parse_structured_response(response, ReadOutput)


def read_visual_document(company_name: str, path: str, filename: str, visual: dict) -> CompactDigest:
    from app.extract import api_block

    sent, total = visual.get("_pages_sent"), visual.get("_pages_total")
    pages = (f" Pages attached: the first {sent - 1} and the last of {total}." if sent and total and sent < total else "")
    response = _create(
        _client(), "read_visual",
        model=READ_MODEL,
        max_tokens=8000,
        system=READ_SYSTEM_PROMPT,
        output_config=_output_config(ReadOutput, "low", READ_MODEL),
        messages=[{"role": "user", "content": [
            api_block(visual),
            {"type": "text", "text": (
                f"Company under diligence: {company_name}\nPath: {path}\nFile: {filename}\n\n"
                f"This document has little or no text layer, so it is attached as page images.{pages} "
                f"Return exactly one entry in `documents`, with doc = 1."
            )},
        ]}],
    )
    out = parse_structured_response(response, ReadOutput)
    if not out.documents:
        raise RuntimeError("the reading model returned no entry for this document")
    return out.documents[0]


# -------------------------------------------------------------- key facts --

KEY_FACTS_SYSTEM_PROMPT = """\
You are the lead analyst on a venture due diligence engagement. From the \
evidence pack (a digest of every document in the dataroom), build the Key \
Facts sheet that every section of the report will be written against, so \
that the whole report states the same round size, valuation, share counts, \
dates, people, product description and milestones. Where documents \
disagree, record the conflict and resolve it: prefer the most recent \
document, executed over unsigned, and primary records (charter, executed \
agreements, financial statements, the latest model) over summaries (decks, \
one-pagers). Include every material figure. Respond with JSON only, \
matching the schema.
"""


def extract_key_facts(company_name: str, pack_text: str) -> KeyFactsOutput:
    response = _create(
        _client(), "key_facts",
        model=KEY_FACTS_MODEL,
        max_tokens=32000,
        system=KEY_FACTS_SYSTEM_PROMPT,
        output_config=_output_config(KeyFactsOutput, "medium", KEY_FACTS_MODEL),
        messages=[{"role": "user", "content": (
            f"Company under diligence: {company_name}\n\n### Evidence pack\n{pack_text}"
        )}],
    )
    return parse_structured_response(response, KeyFactsOutput)


# --------------------------------------------------------------- research --

RESEARCH_SYSTEM_PROMPT = """\
You are the market-research analyst on TEN Capital Network's due diligence \
team. Using web search, gather the public facts the report writers need and \
write them up as a dense research memo. Report writers cannot search, so \
anything you do not record here will be missing from the report.

Be specific and current: names, dates, amounts, product details, regulatory \
status, prices. Prefer primary and reputable sources (company sites, SEC \
filings, FDA databases, press releases, peer-reviewed or analyst reports). \
After every fact give its source in brackets: [publisher, date, URL]. If \
something cannot be found, say so in one line rather than guessing.
"""

RESEARCH_BRIEF = """\
Research brief for {company}. Write the memo with exactly these headings:

1. Market size and growth -- TAM/SAM figures with year and source, growth \
rates, the key demand drivers, and the size of the specific segment this \
company sells into.
2. Competitors -- 8 to 12 direct and adjacent competitors. For each: name, \
HQ, founded, funding raised / public status and latest valuation if known, \
product and how it works (technology, specimen/input, setting), regulatory \
status, pricing if public, customers/traction, and how it compares with \
{company}'s product.
3. Comparable transactions -- recent (last ~7 years) acquisitions and notable \
financings in this space: acquirer, target, date, deal value, revenue or \
stage at the time, multiple if known.
4. Potential acquirers -- 8 to 12 strategic buyers with the rationale for \
each (portfolio fit, relevant past acquisitions, stated strategy).
5. Reimbursement, pricing and regulatory landscape -- codes, payment rates, \
precedent clearances/approvals for similar products, relevant guidance.
6. Industry trends and risks relevant to the investment.

What the company and its documents say about itself (use this to aim your \
searches; verify claims about competitors and the market independently):

{context}
"""


def research_market(company_name: str, context: str) -> dict:
    client = _client()
    messages = [{"role": "user", "content": RESEARCH_BRIEF.format(company=company_name, context=context)}]
    params = dict(
        model=RESEARCH_MODEL,
        max_tokens=24000,
        system=RESEARCH_SYSTEM_PROMPT,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": RESEARCH_MAX_SEARCHES}],
        output_config={"effort": "medium"},
    )
    content = []
    for _ in range(4):  # a long server-side search loop can pause; continue it
        response = _create(client, "research", messages=messages, **params)
        content += list(response.content)
        if response.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": response.content}]
    memo, sources, seen = [], [], set()
    for block in content:
        if block.type != "text":
            continue
        memo.append(block.text)
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "title": getattr(c, "title", "") or ""})
    text = "".join(memo).strip()
    if not text:
        raise RuntimeError("web research returned no memo text")
    return {"memo": text, "sources": sources}


# --------------------------------------------------------------- drafting --

SYSTEM_PROMPT = """\
You are a senior analyst on TEN Capital Network's Due Diligence Team, writing \
one part of an institutional-quality venture due diligence report for \
investors. The standard is the Team's client-approved reports: precise, \
dense with verified figures, skeptical, and explicit about what is and is \
not established.

How the Team writes:
- Every factual statement is grounded in the dataroom evidence pack, the \
pitch deck, the Key Facts sheet, or the web research memo (cite public \
sources by publisher and date). Use exact figures, dates, names, titles, \
share counts, prices and terms -- "$280,000 of the $1.0 million note is \
funded", not "the round is partly funded". Never invent or estimate a \
number the sources don't support.
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
- When the evidence lacks something this section would normally cover, say \
so in one plain sentence ("The data room contains no executed supply \
agreement.") and make it a follow-up item. Never pad a gap with generic text.
- Unsigned, draft, or non-binding documents are identified as such.
- Depth: match the reference report -- each part states its target length; \
reach it with substance, not padding. Tables are used for anything with several attributes per \
item (rosters, agreements, financials by year, comparables, timelines, \
checklists), and every row is filled from the sources.

The reference report excerpts you are shown are from a DIFFERENT company. \
Use them only for structure, voice, depth, and table design; never copy \
their facts, names, or figures.

Output format: JSON only, matching the schema. Return one entry in \
`sections` for each section_key you are asked for, and no others. Each \
section is a list of blocks: "paragraph" (narrative); "heading" with level \
3 (a sub-heading inside the subsection) or level 4 (a bold underlined \
run-in label such as "Due Diligence Team Assessment" or "Strengths \
observed:"); "table" (caption may be ""; every row has exactly as many cells \
as headers); "callout" (the shaded Summary of Findings box); "bullets" \
(numbered true for ranked or sequential items); "note" (a small italic \
source line, typically "Source: ..." directly after a table). Inside any \
text you may use **double asterisks** for a short bold lead-in phrase; no \
other markdown. Do not repeat the subsection's own title as a heading -- it \
is added for you.
"""


def drafting_system_blocks(context: str) -> list[dict]:
    """The shared, cached prefix. Must be byte-identical across a job's
    calls -- `context` comes from storage (evidence.drafting_context)."""
    return [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": context, "cache_control": CACHE_TTL},
    ]


def _drafting_params(context: str) -> dict:
    # Identical for every drafting call and the warm-up, so they all share
    # one cache entry: same model, same system blocks, no tools, the same
    # schema (section keys are checked in code, not pinned in the schema).
    return dict(
        model=DRAFTING_MODEL,
        system=drafting_system_blocks(context),
        output_config=_output_config(DraftingOutput, DRAFTING_EFFORT, DRAFTING_MODEL),
    )


def build_drafting_request(spec, company_name: str, context: str, user_content: str) -> dict:
    """kwargs for client.messages.create(**kwargs) or a Batch request."""
    return dict(
        max_tokens=48000,
        messages=[{"role": "user", "content": user_content}],
        **_drafting_params(context),
    )


def draft_section(spec, company_name: str, context: str, user_content: str) -> DraftingOutput:
    params = build_drafting_request(spec, company_name, context, user_content)
    response = _create(_client(), f"draft:{spec.task_key}", **params)
    return parse_structured_response(response, DraftingOutput)


def warm_drafting_cache(context: str) -> None:
    """Write the shared prefix to the prompt cache once, so the parallel
    drafting calls that follow all read it instead of each writing it."""
    _create(
        _client(), "cache_warmup",
        max_tokens=16,
        messages=[{"role": "user", "content": "Cache warm-up only; no answer is needed."}],
        **_drafting_params(context),
    )


# --------------------------------------------------------------------- QA --

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


def _qa_system_blocks(report_text: str, dataroom_text: str) -> list[dict]:
    return [
        {"type": "text", "text": QA_SYSTEM_PROMPT},
        {"type": "text", "text": (
            f"### Compiled due diligence report\n{report_text}\n\n"
            f"### Dataroom evidence\n{dataroom_text or '(no dataroom documents were uploaded)'}"
        )},
    ]


def score_all_assessments(specs: list[QASpec], company_name: str, report_text: str,
                          dataroom_text: str) -> QAAllOutput:
    checks = []
    for s in specs:
        scale = f"score on {s.scale}" if s.has_score else "no numeric score -- score must be null"
        checks.append(f"- task_key \"{s.task_key}\" -- {s.assessment_name} ({scale}): "
                      f"{s.instruction.format(company=company_name)}")
    config = _output_config(QAAllOutput, "low", QA_MODEL)
    item = config["format"]["schema"]["$defs"]["QAAssessment"]["properties"]["task_key"]
    item["enum"] = [s.task_key for s in specs]
    response = _create(
        _client(), "qa_all",
        model=QA_MODEL,
        max_tokens=48000,
        system=_qa_system_blocks(report_text, dataroom_text),
        output_config=config,
        messages=[{"role": "user", "content": (
            f"Run every one of the following {len(specs)} checks on the {company_name} report and return one "
            f"entry per check in `assessments`. For each: a 2-4 sentence summary and 3-8 specific findings.\n\n"
            + "\n".join(checks)
        )}],
    )
    return parse_structured_response(response, QAAllOutput)


def build_qa_request(spec: QASpec, company_name: str, report_text: str, dataroom_text: str) -> dict:
    """Single-check request -- used by app/batch_runner.py and by the
    legacy per-check qa_score task."""
    scale_note = (f"Score scale for this check: {spec.scale}." if spec.has_score
                  else "This check does not produce a numeric score -- leave `score` null.")
    return dict(
        model=QA_MODEL,
        max_tokens=32000,
        system=_qa_system_blocks(report_text, dataroom_text),
        output_config=_output_config(QAScoreOutput, "medium", QA_MODEL),
        messages=[{"role": "user", "content": f"{spec.instruction.format(company=company_name)}\n\n{scale_note}"}],
    )


def score_assessment(spec: QASpec, company_name: str, report_text: str, dataroom_text: str) -> QAScoreOutput:
    response = _create(_client(), f"qa:{spec.task_key}", **build_qa_request(spec, company_name, report_text, dataroom_text))
    return parse_structured_response(response, QAScoreOutput)


# ------------------------------------------------------------- golden eval --

class _GoldenVerdict(__import__("pydantic").BaseModel):
    id: str
    present: bool
    evidence: str


class _GoldenVerdicts(__import__("pydantic").BaseModel):
    verdicts: list[_GoldenVerdict]


def grade_golden_facts(items: list[tuple[str, str, str]]) -> dict[str, dict]:
    """items: (fact id, claim, report text). One Haiku call decides whether
    each report states the claim (same substance; wording may differ).
    Returns id -> {present, evidence}."""
    parts = []
    for fid, claim, text in items:
        parts.append(f"=== Fact {fid} ===\nClaim: {claim}\nReport text:\n{text}")
    response = _create(
        _client(), "eval:golden",
        model=READ_MODEL,
        max_tokens=8000,
        system=("You check whether a due diligence report states specific facts. A fact is present only if the "
                "report states its substance correctly (figures, names and dates must match; wording may differ). "
                "A report that says the fact is missing, unconfirmed or unknown does NOT state it. "
                "Respond with JSON only."),
        output_config=_output_config(_GoldenVerdicts, None, READ_MODEL),
        messages=[{"role": "user", "content": (
            "For each fact below, return a verdict with its id, whether the report text states it, and a short "
            "quote (<= 30 words) as evidence (\"\" if absent).\n\n" + "\n\n".join(parts))}],
    )
    out = parse_structured_response(response, _GoldenVerdicts)
    return {v.id: {"present": v.present, "evidence": v.evidence} for v in out.verdicts}


# --------------------------------------------------------- gap/mitigation --

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
    prompt = (
        f"Based on the provided {company_name} due diligence report and QA review below, "
        f"what are the potential gaps or weaknesses that investors might perceive in this "
        f"investment?\n\n"
        f"### Compiled due diligence report\n{report_text}\n\n"
        f"### QA assessment scores and findings\n{qa_summary_text}"
    )
    response = _create(
        _client(), "gap_analysis",
        model=GAP_MITIGATION_MODEL,
        max_tokens=32000,
        system=GAP_ANALYSIS_SYSTEM_PROMPT,
        output_config=_output_config(GapAnalysisOutput, "medium", GAP_MITIGATION_MODEL),
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_structured_response(response, GapAnalysisOutput)


def generate_mitigations(company_name: str, gap_analysis: GapAnalysisOutput) -> MitigationOutput:
    gaps_text = "\n\n".join(f"- **{g.title}** ({g.severity}): {g.description}" for g in gap_analysis.gaps)
    prompt = (
        f"Build a mitigation strategy for each weakness identified in the {company_name} "
        f"due diligence report's gap analysis below.\n\n### Gap analysis\n{gap_analysis.summary}\n\n{gaps_text}"
    )
    response = _create(
        _client(), "mitigation",
        model=GAP_MITIGATION_MODEL,
        max_tokens=32000,
        system=MITIGATION_SYSTEM_PROMPT,
        output_config=_output_config(MitigationOutput, "medium", GAP_MITIGATION_MODEL),
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_structured_response(response, MitigationOutput)
