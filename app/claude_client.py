"""Claude API calls for every stage. Request-building is split from
request-*sending* throughout this module (`build_*_request` vs. the plain
functions that call `client.messages.create` on the result) so that
app/batch_runner.py can submit the exact same requests through the Batch
API instead -- same prompts, same prompt-caching breakpoints, same
structured-output schema, just a different transport. Per the brief,
caching and batching are a Phase 7 concern; Phases 3-6 proved correctness
on the plain synchronous path first.
"""
import json

import anthropic

from app.models import DraftingOutput, GapAnalysisOutput, MitigationOutput, QAScoreOutput
from app.qa_spec import QASpec
from app.reference_report import get_full_reference_text
from app.sections_spec import SectionSpec

DRAFTING_MODEL = "claude-sonnet-5"
QA_MODEL = "claude-sonnet-5"  # per the brief's model routing: Sonnet for QA scoring
GAP_MITIGATION_MODEL = "claude-sonnet-5"

# Batch requests need the full JSON schema up front (no .parse() convenience
# wrapper on that path); the sync path uses the same schema for consistency
# between the two transports rather than relying on messages.parse()'s
# internal schema construction, which isn't available to a batch request.
def _strict_schema(schema):
    """Two fixups Anthropic's raw output_config needs that Pydantic's plain
    model_json_schema() doesn't produce on its own (both a 400 otherwise;
    the .parse() convenience wrapper apparently applies equivalent fixups
    internally, which isn't available on the Batch API's raw-request path):
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


def parse_structured_response(response, model_cls):
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude's response was cut off at the max_tokens limit before it finished "
            "(no complete JSON output) -- raise max_tokens for this call or shorten its input"
        )
    text = next(b.text for b in response.content if b.type == "text")
    return model_cls.model_validate(json.loads(text))


CACHE_TTL = {"type": "ephemeral", "ttl": "1h"}  # see docs/schema.md Phase 7 notes:
# Stage 2 (~20 calls) and Stage 4 (~23 calls) for one job each run well past
# the default 5-minute cache TTL, so the 1h TTL is what makes caching pay
# off across an entire stage rather than just the first couple of calls.

QA_SYSTEM_PROMPT = """\
You are an independent due-diligence QA reviewer auditing a due diligence \
report prepared by another analyst, together with the source dataroom the \
report was built from.

Be skeptical and specific: cite what you actually found (or didn't find) \
in the report and dataroom excerpts provided, rather than general \
commentary. Where the source material genuinely doesn't address something, \
say so plainly rather than inferring or guessing.

Respond with JSON only, matching the required schema exactly. Every score \
in this system is on a "higher is better" scale -- including any red-flag \
score, where a higher number means fewer red flags were found, never the \
reverse.
"""

SYSTEM_PROMPT = """\
You are a senior due-diligence analyst preparing an institutional-quality \
venture due diligence report, in the house style of TEN Capital.

Ground every factual claim in the provided dataroom excerpts and pitch \
deck. Do not invent financial figures, customer names, patent numbers, or \
dates that are not supported by the source material -- if the dataroom is \
silent on something the section would normally cover, say so explicitly \
rather than filling the gap with plausible-sounding text.

You will be shown a full example due diligence report as a STYLE GUIDE \
ONLY: match its tone, structure, and level of detail for the section(s) \
you are asked to produce, but never copy its facts, company names, or \
figures into your output -- those belong to a different company.

Respond with JSON only, matching the required schema exactly. Use "table" \
blocks for any content that is naturally tabular (financials, comparisons, \
lists of items with several attributes each); use "paragraph" blocks for \
narrative text; use "heading" blocks sparingly, only for a genuine internal \
sub-heading within the section.
"""


def _drafting_system_blocks() -> list[dict]:
    return [
        {"type": "text", "text": SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"### Full reference report (style guide)\n{get_full_reference_text()}",
            "cache_control": CACHE_TTL,
        },
    ]


def _build_drafting_user_content(
    spec: SectionSpec,
    company_name: str,
    dataroom_text: str,
    pitch_deck_text: str,
    dependency_context: str,
) -> str:
    resolved_produces = [
        (p.section_key.format(company=company_name), p.order_index) for p in spec.produces
    ]
    parts = [
        spec.instruction.format(company=company_name),
        "",
        f"Produce exactly these section(s) in your JSON output, using these exact "
        f"section_key values: {[key for key, _ in resolved_produces]}",
        "",
        f"For style/tone/format, model your response on the reference report's "
        f"section(s): {list(spec.style_guide_headings)}.",
    ]
    if dependency_context:
        parts += ["", "### Prior context from earlier drafted sections", dependency_context]
    if pitch_deck_text:
        parts += ["", "### Pitch deck", pitch_deck_text]
    if dataroom_text:
        parts += ["", "### Dataroom excerpts", dataroom_text]
    else:
        parts += ["", "### Dataroom excerpts", "(no matching dataroom documents were uploaded for this category)"]

    return "\n".join(parts)


def build_drafting_request(
    spec: SectionSpec,
    company_name: str,
    dataroom_text: str,
    pitch_deck_text: str,
    dependency_context: str,
) -> dict:
    """Returns kwargs suitable for either client.messages.create(**kwargs)
    or a Batch Request's `params` -- see app/batch_runner.py."""
    user_content = _build_drafting_user_content(
        spec, company_name, dataroom_text, pitch_deck_text, dependency_context
    )
    kwargs = dict(
        model=DRAFTING_MODEL,
        # Real datarooms push far more source text into the prompt than the
        # fixtures Phases 3-6 were built against (confirmed against a real
        # life-sciences dataroom: "effort": "high" reasoning plus a dense
        # answer regularly ran past the old 16000-token cap, cutting the
        # response off mid-JSON). 32000 gives that headroom back.
        max_tokens=32000,
        system=_drafting_system_blocks(),
        output_config={"effort": "high", **_output_config(DraftingOutput)},
        messages=[{"role": "user", "content": user_content}],
    )
    if spec.needs_web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]
    return kwargs


def draft_section(
    spec: SectionSpec,
    company_name: str,
    dataroom_text: str,
    pitch_deck_text: str,
    dependency_context: str,
) -> DraftingOutput:
    client = anthropic.Anthropic()
    params = build_drafting_request(spec, company_name, dataroom_text, pitch_deck_text, dependency_context)
    response = client.messages.create(**params)
    return parse_structured_response(response, DraftingOutput)


def _qa_system_blocks(report_text: str, dataroom_text: str) -> list[dict]:
    return [
        {"type": "text", "text": QA_SYSTEM_PROMPT},
        {
            "type": "text",
            "text": (
                f"### Compiled due diligence report\n{report_text}\n\n"
                f"### Dataroom excerpts\n{dataroom_text or '(no dataroom documents were uploaded)'}"
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
        max_tokens=16000,
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
    response = client.messages.create(**params)
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
    # Stage 5 is a low-call-count, sequential step (per the brief, kept on
    # the standard API regardless of Phase 7 -- there's nothing to batch
    # here), so it isn't part of the build_*_request/batch_runner split.
    client = anthropic.Anthropic()
    prompt = (
        f"Based on the provided {company_name} due diligence report and QA review below, "
        f"what are the potential gaps or weaknesses that investors might perceive in this "
        f"investment?\n\n"
        f"### Compiled due diligence report\n{report_text}\n\n"
        f"### QA assessment scores and findings\n{qa_summary_text}"
    )
    response = client.messages.create(
        model=GAP_MITIGATION_MODEL,
        max_tokens=16000,
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
    response = client.messages.create(
        model=GAP_MITIGATION_MODEL,
        max_tokens=16000,
        system=MITIGATION_SYSTEM_PROMPT,
        output_config={"effort": "high", **_output_config(MitigationOutput)},
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_structured_response(response, MitigationOutput)
