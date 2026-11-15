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

from app import model_config, usage
from app.models import (
    DraftingOutput,
    FactCardOutput,
    GapAnalysisOutput,
    KeyFactsOutput,
    MitigationOutput,
    QAAllOutput,
    QAScoreOutput,
)
from app.qa_spec import QASpec

# Model IDs and prices: app/model_config.py (one place, env-overridable).
DRAFTING_MODEL = model_config.DRAFTING_MODEL
QA_MODEL = model_config.QA_MODEL
GAP_MITIGATION_MODEL = model_config.GAP_MITIGATION_MODEL
KEY_FACTS_MODEL = model_config.KEY_FACTS_MODEL
RESEARCH_MODEL = model_config.RESEARCH_MODEL
READ_MODEL = model_config.READ_MODEL
DRAFTING_EFFORT = os.environ.get("DRAFTING_EFFORT", "medium")
# Sonnet 5 runs adaptive thinking by default; its hidden reasoning was about
# half of all drafting output tokens. "adaptive" keeps it, "disabled" turns
# it off (see the A/B endpoint, POST /jobs/{id}/ab-draft).
DRAFTING_THINKING = os.environ.get("DRAFTING_THINKING", "adaptive")

# 5-minute TTL: every drafting call that reads the prefix refreshes it, and
# the wave runs back to back, so it never lapses in practice -- the 1-hour
# TTL costs 2x the input price to write instead of 1.25x.
CACHE_TTL = {"type": "ephemeral"}


def _client() -> anthropic.Anthropic:
    # Rate limits (429) and overloads (529) during a 24-call drafting wave
    # are retried with backoff by the SDK instead of failing the task.
    return anthropic.Anthropic(max_retries=6)


def _supports_effort(model: str) -> bool:
    return model_config.supports_effort(model)


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
You read documents from a startup's due diligence data room for an \
investor-side diligence team and write one fact card per document (or per \
numbered chunk of a long document), so the report writers can work from \
your cards instead of the documents.

Rules:
- Quote figures, dates, names, titles, share counts, prices and terms exactly \
as written. Never infer or estimate anything the text does not say.
- End every list item with its page reference in parentheses, e.g. \
"Conversion price $1.513 per share (p. 3)". Pages are marked [page N].
- A page marked as identical to another document's page was read there; do \
not guess its content.
- executed: "true" only if the signature pages show signatures, e-signature \
marks (DocuSign/Carta envelope IDs, "/s/") or the text says it was signed; \
"false" for drafts, forms and unsigned copies; "unclear" otherwise.
- Describe products and technology exactly as the document does (do not call \
a whole-blood test a fingerstick test unless the document does).
- Record people and their roles in people_mentioned -- investigators, \
advisors, directors, noteholders, consultants -- so conflicts of interest \
can be found across documents.

Importance (1-5) and depth:
5 = core: financing documents (term sheets, notes, SAFEs, warrants, \
subscription/purchase agreements, investors' rights / side letters), cap \
table / securities ledger, charter and bylaws, financial statements, \
financial model, key licenses, patent filings and assignments, regulatory \
submissions and agency correspondence, pitch deck / executive summary. \
Up to 25 items per list.
4 = material: executed material contracts, offer letters / employment / \
consulting / invention-assignment agreements, board and stockholder \
consents, valuations (409A), clinical / validation / test reports, customer \
letters / LOIs, market and reimbursement studies, project plans. Up to 12.
3 = supporting: minutes, SOPs, investor updates, leases, insurance. Up to 6.
2 = routine: NDAs, forms, certificates, bank statements (record the balance \
and date), invoices. Up to 2.
1 = no substance. Empty lists.

By document type:
- Spreadsheets: for EVERY sheet, the key line items by year (revenue, \
EBITDA, net income, cash, capital needed, headcount) and, for scenario or \
sensitivity tabs, each scenario's name with its revenue, EBITDA and capital \
need -- e.g. "Scenario 'Downside 10% + 12-mo delay': 2032 revenue $14.5M; \
capital need up to $11.9M (Sheet: Scenarios)". Cap tables: holders, shares, \
% outstanding and fully diluted, option pool.
- Financing documents: amount, price/cap/discount, interest, maturity, \
conversion and anti-dilution terms, investors and amounts, warrants.
- Agreements: parties, date, term, economics, exclusivity, IP ownership, \
termination.
- Employment, offer, consulting, advisor documents: person, title, salary or \
fees, equity (shares, strike, vesting), start date, invention assignment.
- Patent documents: application numbers, titles, filing dates, priority \
claims, assignees, assignment recordation (reel/frame, date).
- Technical, clinical and regulatory documents: what was tested, how, n, \
results with numbers, dates, agency feedback.

report_tags: commercial, financial, deal_terms, corporate, contracts, team, \
ip, science_tech, manufacturing, regulatory, competition, exit.

Respond with JSON only, matching the schema.
"""


def build_fact_card_request(company_name: str, entries: list) -> dict:
    """entries: (number, dataroom path, heading note, text with [page N]
    markers). One card per entry. Same params for the synchronous call and
    a Message Batch request."""
    parts = [f"Company under diligence: {company_name}\n\nWrite one fact card per numbered document below "
             f"({len(entries)} in total), with its number in `doc`."]
    for n, path, note, text in entries:
        parts.append(f"=== Document {n} ===\nPath: {path}{(' -- ' + note) if note else ''}\n\n{text}")
    return dict(
        model=READ_MODEL,
        **model_config.no_thinking(READ_MODEL),
        max_tokens=24000,
        system=READ_SYSTEM_PROMPT,
        output_config=_output_config(FactCardOutput, "low", READ_MODEL),
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
    )


def read_fact_cards(company_name: str, entries: list) -> FactCardOutput:
    response = _create(_client(), "read", **build_fact_card_request(company_name, entries))
    return parse_structured_response(response, FactCardOutput)


# Message Batches: half price, results usually within minutes (at most 24h).
def submit_batch(requests: list[tuple[str, dict]]) -> str:
    """requests: (custom_id, params). Returns the batch id."""
    batch = _client().messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in requests])
    return batch.id


def batch_status(batch_id: str) -> str:
    return _client().messages.batches.retrieve(batch_id).processing_status


def cancel_batch(batch_id: str) -> None:
    try:
        _client().messages.batches.cancel(batch_id)
    except Exception:
        pass


def batch_fact_cards(batch_id: str, model: str = None) -> dict[str, FactCardOutput | None]:
    """custom_id -> parsed cards (None for a request that errored, expired
    or didn't parse). Each succeeded message's usage is recorded at batch
    prices."""
    out = {}
    for item in _client().messages.batches.results(batch_id):
        if item.result.type != "succeeded":
            out[item.custom_id] = None
            continue
        message = item.result.message
        usage.record("read", model or READ_MODEL, message, batch=True)
        try:
            out[item.custom_id] = parse_structured_response(message, FactCardOutput)
        except Exception:
            out[item.custom_id] = None
    return out


def _png_block(png: bytes) -> dict:
    import base64
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode()}}


def transcribe_page(png: bytes, path: str, page_no: int) -> str:
    """One page OCR could not read reliably, transcribed by the vision model."""
    response = _create(
        _client(), "read_visual",
        model=READ_MODEL,
        **model_config.no_thinking(READ_MODEL),
        max_tokens=4000,
        system="You transcribe one scanned page from a due diligence data room. Output only the page's text, "
               "tables as pipe-separated rows. For signatures write [signed: name if legible] or [signature line "
               "blank]. Do not summarize or add commentary.",
        messages=[{"role": "user", "content": [
            _png_block(png), {"type": "text", "text": f"{path}, page {page_no}. Transcribe it."}]}],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()


def check_signature_page(png: bytes, path: str, page_no: int) -> str:
    """'yes' / 'no' / 'unclear': is this signature page actually signed?"""
    response = _create(
        _client(), "read_signature",
        model=READ_MODEL,
        **model_config.no_thinking(READ_MODEL),
        max_tokens=20,
        system="You look at one signature page of an agreement. Answer with exactly one word: yes if it carries "
               "signatures (handwritten, pasted image, or e-signature marks), no if the signature lines are blank, "
               "unclear if you cannot tell.",
        messages=[{"role": "user", "content": [
            _png_block(png), {"type": "text", "text": f"{path}, page {page_no}. Is it signed?"}]}],
    )
    word = "".join(b.text for b in response.content if b.type == "text").strip().lower()
    return word if word in ("yes", "no", "unclear") else "unclear"


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
write them up as a dense research memo section. The report writers cannot \
search, so anything you do not record here will be missing from the report.

Be specific and current: names, dates, amounts, product details, regulatory \
status, prices. Prefer primary and reputable sources (company sites, SEC \
filings, FDA databases, press releases, peer-reviewed or analyst reports). \
After every fact give its source in brackets: [publisher, date, URL].

If something cannot be established, add it to a final "Follow-up items" \
list as a specific action (e.g. "Confirm ProciseDx's FDA clearance status \
for its phenytoin assay with the company"). Never mention searches, search \
tools, limits or caps -- the reader only needs facts and follow-ups.
"""

RESEARCH_BRIEF = """\
Research brief for {company} ({industry}). Write this part of the research \
memo under exactly these headings:

{topic}

{seeds}
What the company and its documents say about itself -- cover every \
competitor, customer and acquirer they name, and verify their claims about \
competitors and the market independently:

{context}
"""

# Four topics, each split into sub-calls of at most RESEARCH_SEARCHES_PER_CALL
# searches: within one call every search step re-reads all earlier results,
# so cost grows with the square of the searches per call (one call with 8
# searches measured 629k input tokens, $2.22, ~12 min). Sub-calls run in
# parallel. Results are cached per company and topic (research_cache).
RESEARCH_TOPICS = [
    ("competitors", [
        ("""## Competitors (part 1)
Profile the most important direct competitors of {company}, starting with \
the candidates listed below that are relevant. For each: name, HQ, founded, \
ownership / funding / latest valuation, product and how it works \
(technology, specimen/input, setting, time to result), regulatory status, \
pricing if public, customers and traction, and how it compares with \
{company}'s product.""", "first"),
        ("""## Competitors (part 2)
Profile further direct and adjacent competitors of {company} -- the \
remaining relevant candidates below and any important ones they omit -- \
with the same fields: name, HQ, founded, ownership / funding, product and \
how it works, regulatory status, pricing, traction, comparison.""", "second"),
    ]),
    ("acquirers_deals", [
        ("""## Potential acquirers
8 to 12 strategic or sponsor buyers for {company}, starting with the \
relevant candidates below. For each: rationale (portfolio fit, stated \
strategy) and its relevant acquisitions with dates and values.""", "acquirers"),
        ("""## Comparable transactions
Acquisitions and notable financings in {company}'s space in the last ~7 \
years: acquirer, target, date, deal value, the target's revenue or stage at \
the time, multiple if known.""", "acquirers"),
    ]),
    ("market", [
        ("""## Market size and growth
TAM/SAM figures with year and source, growth rates, demand drivers, and the \
size of the specific segment {company} sells into (number of sites/patients \
or tests, if applicable).""", None),
        ("""## Industry trends and risks
What an investor in {company} should know: technology, regulatory, \
reimbursement, competitive and consolidation trends.""", None),
    ]),
    ("reimbursement", [
        ("""## Reimbursement, pricing and regulatory landscape
Billing codes and payment rates for this kind of product or test, payer \
coverage, prices of comparable products, precedent clearances/approvals for \
similar products, and relevant agency guidance.""", None),
    ]),
]
RESEARCH_SEARCHES_PER_CALL = int(os.environ.get("RESEARCH_SEARCHES_PER_CALL", "3"))
RESEARCH_CACHE_DAYS = int(os.environ.get("RESEARCH_CACHE_DAYS", "14"))


def _search_tool(model: str) -> dict:
    kind = "web_search_20250305" if model.startswith("claude-haiku-4") else "web_search_20260209"
    return {"type": kind, "name": "web_search", "max_uses": RESEARCH_SEARCHES_PER_CALL}


def _seed_text(industries: list[dict], which: str | None) -> str:
    if not industries or which is None:
        return ""
    ind = industries[0]
    if which == "acquirers":
        names = ind["acquirers"]
    else:
        names = ind["competitors"]
        half = (len(names) + 1) // 2
        names = names[:half] if which == "first" else names[half:]
    if not names:
        return ""
    return ("Candidates the Team expects to see covered in this space (include each one that is relevant, "
            "and say briefly why any is not): " + "; ".join(names) + "\n")


def _research_call(company_name: str, context: str, industry: str, heading: str, seeds: str,
                   model: str) -> tuple[str, list[dict]]:
    client = _client()
    messages = [{"role": "user", "content": RESEARCH_BRIEF.format(
        company=company_name, industry=industry or "industry as described below",
        topic=heading.format(company=company_name), seeds=seeds, context=context)}]
    params = dict(model=model, max_tokens=10000, system=RESEARCH_SYSTEM_PROMPT, tools=[_search_tool(model)],
                  **model_config.no_thinking(model))
    if _supports_effort(model):
        params["output_config"] = {"effort": "low"}
    content = []
    for _ in range(3):  # a server-side search loop can pause; continue it
        response = _create(client, "research", messages=messages, **params)
        content += list(response.content)
        if response.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": response.content}]
    memo, sources = [], []
    for block in content:
        if block.type != "text":
            continue
        memo.append(block.text)
        for c in getattr(block, "citations", None) or []:
            if getattr(c, "url", None):
                sources.append({"url": c.url, "title": getattr(c, "title", "") or ""})
    return "".join(memo).strip(), sources


def research_market(company_name: str, context: str) -> dict:
    """The research memo: one section per topic, from cache when this
    company was researched within RESEARCH_CACHE_DAYS."""
    import concurrent.futures
    import contextvars
    import re as _re

    from app import industry_config, repo

    industries = industry_config.detect(context)
    industry = industries[0]["label"] if industries else ""
    company_key = _re.sub(r"[^a-z0-9]+", "", company_name.lower())

    def run(heading: str, which: str | None) -> tuple[str, list[dict]]:
        seeds = _seed_text(industries, which)
        try:
            return _research_call(company_name, context, industry, heading, seeds, RESEARCH_MODEL)
        except anthropic.BadRequestError:
            if RESEARCH_MODEL == DRAFTING_MODEL:
                raise
            return _research_call(company_name, context, industry, heading, seeds, DRAFTING_MODEL)

    sections: dict[str, tuple[str, list]] = {}
    jobs = []
    for topic, calls in RESEARCH_TOPICS:
        cached = repo.get_research_cache(company_key, topic, RESEARCH_CACHE_DAYS)
        if cached:
            sections[topic] = (cached["memo"], cached["sources"])
        else:
            jobs += [(topic, n, heading, which) for n, (heading, which) in enumerate(calls)]
    results: dict[tuple, tuple] = {}
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            # copy_context so each thread's API usage is recorded against this task
            futures = {(t, n): pool.submit(contextvars.copy_context().run, run, h, w) for t, n, h, w in jobs}
            for key, fut in futures.items():
                try:
                    results[key] = fut.result()
                except Exception as exc:
                    log_text = f"(research for this part failed: {type(exc).__name__})"
                    results[key] = (log_text, [])
    for topic, calls in RESEARCH_TOPICS:
        if topic in sections:
            continue
        parts = [results[(topic, n)] for n in range(len(calls)) if (topic, n) in results]
        memo = "\n\n".join(text for text, _ in parts if text)
        srcs = [s for _, ss in parts for s in ss]
        sections[topic] = (memo, srcs)
        if memo and "research for this part failed" not in memo:
            repo.put_research_cache(company_key, topic, memo, srcs)
    memo = "\n\n".join(sections[t][0] for t, _ in RESEARCH_TOPICS if sections[t][0])
    if not memo.strip():
        raise RuntimeError("web research returned no memo text")
    seen, sources = set(), []
    for t, _ in RESEARCH_TOPICS:
        for s in sections[t][1]:
            if s["url"] not in seen:
                seen.add(s["url"])
                sources.append(s)
    return {"memo": memo, "sources": sources, "industry": industry,
            "from_cache": sorted(t for t, _ in RESEARCH_TOPICS if not any(j[0] == t for j in jobs))}


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
- Before writing that a document is missing, unsigned or unconfirmed, check \
the evidence pack: every document listed there IS in the data room, with \
its signature status. Never mention search tools, search limits or caps; \
where public information is thin, name the specific follow-up instead.
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


def _drafting_params(context: str, thinking: str | None = None) -> dict:
    # Identical for every drafting call and the warm-up, so they all share
    # one cache entry: same model, same system blocks, no tools, the same
    # schema (section keys are checked in code, not pinned in the schema),
    # the same thinking setting.
    params = dict(
        model=DRAFTING_MODEL,
        system=drafting_system_blocks(context),
        output_config=_output_config(DraftingOutput, DRAFTING_EFFORT, DRAFTING_MODEL),
    )
    if (thinking or DRAFTING_THINKING) == "disabled" and model_config.supports_disabled_thinking(DRAFTING_MODEL):
        params["thinking"] = {"type": "disabled"}
    return params


def drafting_max_tokens(target_words: int, thinking: str | None = None) -> int:
    """Output ceiling for one drafting call: the parts' reference length in
    words, as JSON blocks (~2 tokens a word), plus room for reasoning when
    it is on. A ceiling, not a target -- generous enough never to cut a
    complete answer off."""
    room = 0 if (thinking or DRAFTING_THINKING) == "disabled" else 20_000
    return int(min(max(target_words * 3 + 6_000 + room, 12_000), 48_000))


def build_drafting_request(spec, company_name: str, context: str, user_content: str,
                           target_words: int = 4_000, thinking: str | None = None) -> dict:
    """kwargs for client.messages.create(**kwargs) or a Batch request."""
    return dict(
        max_tokens=drafting_max_tokens(target_words, thinking),
        messages=[{"role": "user", "content": user_content}],
        **_drafting_params(context, thinking),
    )


def draft_section(spec, company_name: str, context: str, user_content: str, target_words: int = 4_000,
                  thinking: str | None = None, purpose: str | None = None):
    params = build_drafting_request(spec, company_name, context, user_content, target_words, thinking)
    response = _create(_client(), purpose or f"draft:{spec.task_key}", **params)
    return parse_structured_response(response, DraftingOutput)


def draft_section_raw(spec, company_name: str, context: str, user_content: str, target_words: int,
                      thinking: str, purpose: str):
    """For the A/B comparison: the parsed output plus the raw response
    (usage, content block types)."""
    params = build_drafting_request(spec, company_name, context, user_content, target_words, thinking)
    response = _create(_client(), purpose, **params)
    return parse_structured_response(response, DraftingOutput), response


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


def grade_golden_facts(items: list[tuple[str, str, list[str]]], texts: dict[str, str]) -> dict[str, dict]:
    """items: (fact id, claim, report keys); texts: report key -> text. One
    Haiku call decides whether the named reports state each claim (same
    substance; wording may differ). Each report's text is included once,
    with the facts to check against it. Returns id -> {present, evidence}."""
    by_reports: dict[tuple, list] = {}
    for fid, claim, reports in items:
        by_reports.setdefault(tuple(reports), []).append((fid, claim))
    used = {r for reports in by_reports for r in reports}
    parts = [f"=== Report: {r} ===\n{texts.get(r, '')}" for r in sorted(used)]
    for reports, facts in by_reports.items():
        parts.append(f"=== Facts to check in report(s) {', '.join(reports)} ===\n"
                     + "\n".join(f"- [{fid}] {claim}" for fid, claim in facts))
    response = _create(
        _client(), "eval:golden",
        model=READ_MODEL,
        **model_config.no_thinking(READ_MODEL),
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
