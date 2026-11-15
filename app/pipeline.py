"""Stage orchestration: turning the specs into real `tasks` rows for one
job, assembling each drafting call's inputs, persisting drafted sections,
and rendering sections back to plain text (for dependent drafting tasks,
QA, and gap analysis).

Stage 1 (intake) now does real work: every uploaded file gets a
digest_document task the moment it is stored, so the dataroom is being read
while the user is still uploading the pitch deck and logo. Stage 2 then
builds the Key Facts sheet from every digest and drafts the report against
it -- see app/evidence.py for what each drafting call sees.
"""
import re

from app import evidence, repo
from app.reference_report import reference_words, style_excerpt
from app.sections_spec import (
    INTRO,
    REPORT_BY_KEY,
    SECTION_SPECS,
    SPEC_BY_TASK_KEY,
    SectionSpec,
    section_defs,
    section_key,
)

STAGE_INTAKE = 1
STAGE_DRAFTING = 2
STAGE_COMPILE = 3
STAGE_QA = 4
STAGE_GAP_MITIGATION = 5
STAGE_FINAL_RENDER = 6

KEY_FACTS_TASK_KEY = "key_facts"
RESEARCH_TASK_KEY = "research"
WARM_CACHE_TASK_KEY = "warm_cache"
READ_TASK_TYPES = ("read_files", "digest_document")


def digest_task_key(file_id) -> str:
    return f"digest:{file_id}"


def _covered_file_ids(tasks: list[dict]) -> set[str]:
    covered = set()
    for t in tasks:
        if t["task_type"] in READ_TASK_TYPES:
            p = t["payload"] or {}
            covered |= set(p.get("file_ids") or ([p["file_id"]] if p.get("file_id") else []))
    return covered


def create_digest_tasks(job_id: str, company_name: str, file_ids: list[str]) -> list[str]:
    """Reading tasks (app/reading.py) for the files not already covered by
    one, a few files per task -- several documents are read per Claude
    call, and fewer, fuller tasks finish a 400-file room in minutes.
    Returns the ids of every reading task of the job."""
    from app.reading import FILES_PER_TASK

    tasks = repo.list_tasks_for_job(job_id)
    covered = _covered_file_ids(tasks)
    ids = [str(t["id"]) for t in tasks if t["task_type"] in READ_TASK_TYPES]
    todo = [str(f) for f in dict.fromkeys(file_ids) if str(f) not in covered]
    for i in range(0, len(todo), FILES_PER_TASK):
        chunk = todo[i:i + FILES_PER_TASK]
        ids.append(repo.create_task(
            job_id=job_id,
            stage=STAGE_INTAKE,
            task_type="read_files",
            task_key=f"read:{chunk[0]}",
            depends_on=[],
            payload={"job_id": job_id, "file_ids": chunk, "company_name": company_name},
        ))
    return ids


def create_stage2_tasks(job_id: str, company_name: str) -> list[str]:
    """The Stage 2 graph -- three short hops after reading, then one
    parallel wave:

        reading tasks --> key_facts ---\\
                     \\--> research ----+--> warm_cache --> 21 report parts --> exit summary,
                                                                              open items,
                                                                              executive summary

    key_facts and research run side by side; warm_cache writes the shared
    evidence prefix to the prompt cache; every report part then drafts in
    parallel reading it. Returns the Stage 2 task ids."""
    files = repo.list_dataroom_files(job_id)
    read_ids = create_digest_tasks(job_id, company_name, [str(f["id"]) for f in files if not f.get("digest")])
    payload = {"job_id": job_id, "company_name": company_name}

    key_facts_id = repo.create_task(job_id=job_id, stage=STAGE_DRAFTING, task_type="key_facts",
                                    task_key=KEY_FACTS_TASK_KEY, depends_on=read_ids, payload=payload)
    research_id = repo.create_task(job_id=job_id, stage=STAGE_DRAFTING, task_type="research",
                                   task_key=RESEARCH_TASK_KEY, depends_on=read_ids, payload=payload)
    warm_id = repo.create_task(job_id=job_id, stage=STAGE_DRAFTING, task_type="warm_cache",
                               task_key=WARM_CACHE_TASK_KEY, depends_on=[key_facts_id, research_id],
                               payload=payload)
    created = [key_facts_id, research_id, warm_id]
    task_key_to_id: dict[str, str] = {}
    for spec in SECTION_SPECS:  # list order already puts every dependency first
        depends_on_ids = [warm_id] + [task_key_to_id[k] for k in spec.depends_on]
        task_id = repo.create_task(
            job_id=job_id,
            stage=STAGE_DRAFTING,
            task_type="draft_section",
            task_key=spec.task_key,
            depends_on=depends_on_ids,
            payload={"job_id": job_id, "task_key": spec.task_key, "company_name": company_name},
        )
        task_key_to_id[spec.task_key] = task_id
        created.append(task_id)
    # Last: check every "missing document" claim against the data room.
    created.append(repo.create_task(job_id=job_id, stage=STAGE_DRAFTING, task_type="absence_check",
                                    task_key="absence_check", depends_on=list(task_key_to_id.values()),
                                    payload=payload))
    return created


def create_stage3_task(job_id: str, company_name: str) -> str:
    """Insert the compile_draft task, depending on every Stage 2 task for
    this job -- it can't run until every section is drafted."""
    stage2_ids = repo.list_task_ids_by_stage(job_id, STAGE_DRAFTING)
    return repo.create_task(
        job_id=job_id,
        stage=STAGE_COMPILE,
        task_type="compile_draft",
        task_key="compile_draft",
        depends_on=stage2_ids,
        payload={"job_id": job_id, "company_name": company_name},
    )


def create_stage4_tasks(job_id: str, company_name: str, vertical: str | None) -> list[str]:
    """Insert the DD Assessment block, the conditional Life Science block,
    the master validation check, and the Report Card aggregation -- see
    app/qa_spec.py for what's included and why. Report Card is created
    last, depending on every scoring task (DD + Life Science, if
    applicable) so it only ever runs once, after everything is scored.
    """
    from app.qa_spec import (
        DD_ASSESSMENT_SPECS,
        LIFE_SCIENCE_SPECS,
        MASTER_VALIDATION_SPEC,
        REPORT_CARD_TASK_KEY,
    )

    specs = list(DD_ASSESSMENT_SPECS)
    if vertical == "life_sciences":
        specs += LIFE_SCIENCE_SPECS
    specs.append(MASTER_VALIDATION_SPEC)

    # Every check reads the same ~100k-token report + evidence, so they are
    # answered in one call (app/tasks/qa_all.py) -- ~22 separate calls each
    # re-read it and each did its own reasoning, for most of Stage 4's cost.
    qa_id = repo.create_task(
        job_id=job_id,
        stage=STAGE_QA,
        task_type="qa_all",
        task_key="qa_all",
        depends_on=[],
        payload={"job_id": job_id, "company_name": company_name, "task_keys": [s.task_key for s in specs]},
    )
    report_card_id = repo.create_task(
        job_id=job_id,
        stage=STAGE_QA,
        task_type="report_card",
        task_key=REPORT_CARD_TASK_KEY,
        depends_on=[qa_id],
        payload={"job_id": job_id},
    )
    return [qa_id, report_card_id]


def create_stage5_tasks(job_id: str, company_name: str) -> list[str]:
    """gap_analysis, then mitigation_strategy (depending on gap_analysis)."""
    gap_analysis_id = repo.create_task(
        job_id=job_id,
        stage=STAGE_GAP_MITIGATION,
        task_type="gap_analysis",
        task_key="gap_analysis",
        depends_on=[],
        payload={"job_id": job_id, "company_name": company_name},
    )
    mitigation_id = repo.create_task(
        job_id=job_id,
        stage=STAGE_GAP_MITIGATION,
        task_type="mitigation_strategy",
        task_key="mitigation_strategy",
        depends_on=[gap_analysis_id],
        payload={"job_id": job_id, "company_name": company_name},
    )
    return [gap_analysis_id, mitigation_id]


def create_stage6_task(job_id: str, company_name: str) -> str:
    mitigation_ids = [
        t["id"] for t in repo.list_tasks_for_job(job_id)
        if t["task_key"] == "mitigation_strategy"
    ]
    return repo.create_task(
        job_id=job_id,
        stage=STAGE_FINAL_RENDER,
        task_type="final_render",
        task_key="final_render",
        depends_on=mitigation_ids,
        payload={"job_id": job_id, "company_name": company_name},
    )


def render_qa_scores_text(job_id: str) -> str:
    """All Stage 4 qa_scores, as plain text, for Gap Analysis's prompt."""
    scores = repo.list_qa_scores(job_id)
    parts = []
    for s in scores:
        findings = s["findings"] or {}
        line = f"- {s['assessment_name']} ({s['block']}, scale {s['scale']}): score={s['score']}"
        if findings.get("summary"):
            line += f" -- {findings['summary']}"
        parts.append(line)
    return "\n".join(parts)


# ----------------------------------------------------------- rendering --

def _heading_marker(level: int) -> str:
    return "#" * max(1, min(level, 4))


def render_blocks_as_text(blocks: list[dict]) -> str:
    """Turn a section's blocks back into plain text, for feeding a completed
    section to a dependent drafting task, QA, or gap analysis."""
    lines = []
    for block in blocks:
        btype = block.get("type")
        if btype == "heading":
            lines.append(f"\n{_heading_marker(block.get('level', 3))} {block['text']}")
        elif btype == "paragraph":
            lines.append(block["text"])
        elif btype == "callout":
            lines.append(f"[{block.get('label', '').strip()}] {block['text']}")
        elif btype == "bullets":
            for i, item in enumerate(block.get("items", []), start=1):
                lines.append(f"{i}. {item}" if block.get("numbered") else f"- {item}")
        elif btype == "note":
            lines.append(f"({block['text']})")
        elif btype == "table":
            if block.get("caption"):
                lines.append(block["caption"])
            lines.append(" | ".join(block["headers"]))
            for row in block["rows"]:
                lines.append(" | ".join(row))
    return "\n".join(lines)


def _render_section(row: dict) -> str:
    level = row.get("heading_level") or 1
    return f"{_heading_marker(level)} {row['title']}\n{render_blocks_as_text(row['content'])}"


def render_full_report_text(job_id: str) -> str:
    """The full compiled draft, as plain text, in section order -- what a
    Stage 4 QA task reads as "the due diligence report"."""
    return "\n\n".join(_render_section(s) for s in repo.list_report_sections(job_id))


# Subsections a summary-mode reader (executive summary, open items) needs in
# full rather than condensed: they *are* the findings and follow-ups.
_FULL_IN_SUMMARY = {"followup", "summary", "next_steps", "findings", "checklist", "recs", "conclusion"}
_SUMMARY_SECTION_CHARS = 700
_SUMMARY_FULL_SECTION_CHARS = 3_000


def _summarize_section(row: dict, sub_key: str) -> str:
    blocks = row["content"]
    if sub_key == INTRO or sub_key in _FULL_IN_SUMMARY:
        text = _render_section(row)
        return text if len(text) <= _SUMMARY_FULL_SECTION_CHARS else text[:_SUMMARY_FULL_SECTION_CHARS] + " [...]"
    callouts = [b for b in blocks if b.get("type") == "callout"]
    rest = [b for b in blocks if b.get("type") != "callout"]
    text = render_blocks_as_text(callouts) + "\n" + render_blocks_as_text(rest)
    if len(text) > _SUMMARY_SECTION_CHARS:
        text = text[:_SUMMARY_SECTION_CHARS] + " [...]"
    return f"{_heading_marker(row.get('heading_level') or 2)} {row['title']}\n{text.strip()}"


def render_summary_report_text(job_id: str) -> str:
    """The compiled report condensed the way the executive summary sees it:
    every section's Summary of Findings and opening, with findings,
    follow-ups and conclusions in full (capped). For Stage 5's gap analysis,
    which is about what the report concludes and leaves open -- a third of
    the full report's length."""
    out = []
    for row in repo.list_report_sections(job_id):
        sub_key = row["section_key"].split(".", 1)[1] if "." in row["section_key"] else INTRO
        out.append(_summarize_section(row, sub_key))
    return "\n\n".join(out)


def build_dependency_context(job_id: str, spec: SectionSpec) -> str:
    parts = []
    for dep_task_key in spec.depends_on:
        dep = SPEC_BY_TASK_KEY[dep_task_key]
        for sub_key in dep.subs:
            row = repo.get_report_section(job_id, section_key(dep.report, sub_key))
            if row is None:
                continue
            parts.append(_summarize_section(row, sub_key) if spec.summary_context else _render_section(row))
    return "\n\n".join(parts)


# Hard ceiling on the per-call (uncached) part of a drafting prompt, so no
# combination of inputs can ever approach the context window again.
MAX_USER_CONTENT_CHARS = 220_000
_TAG_LABELS = {
    "commercial": "commercial", "financial": "financial", "deal_terms": "financing and deal",
    "corporate": "corporate", "contracts": "agreements", "team": "team", "ip": "IP",
    "science_tech": "science and technology", "manufacturing": "manufacturing and supply",
    "regulatory": "regulatory", "competition": "competition", "exit": "exit",
}


def _sibling_parts(spec: SectionSpec, company_name: str) -> list[str]:
    """The other parts of the same report, drafted in parallel by other
    calls -- so this call knows what not to repeat."""
    report = REPORT_BY_KEY[spec.report]
    out = []
    for other in SECTION_SPECS:
        if other.report != spec.report or other.task_key == spec.task_key:
            continue
        for sub_key in other.subs:
            sub = report.sub(sub_key)
            name = "the report's opening" if sub_key == INTRO else f'"{sub.title.format(company=company_name)}"'
            out.append(f"{name}: {sub.guidance.format(company=company_name)[:220]}")
    return out


def build_user_content(job_id: str, company_name: str, spec: SectionSpec) -> str:
    report = REPORT_BY_KEY[spec.report]
    title = report.title.format(company=company_name)
    excerpt = style_excerpt(report.style_h1, [report.sub(s).style_h2 for s in spec.subs])
    lines = [
        "### Reference report: how the Team wrote this part for another company",
        excerpt or "(no reference excerpt for this part)",
        "",
        f"### Your task: {title}" + (f" -- {report.header}" if report.header else ""),
        f"Write the following parts of {company_name}'s \"{title}\". Produce exactly one entry in "
        f"`sections` per section_key below, in this order:",
    ]
    for sub_key in spec.subs:
        sub = report.sub(sub_key)
        key = section_key(spec.report, sub_key)
        heading = ("the report's opening (before its first subsection)" if sub_key == INTRO
                   else f'"{sub.title.format(company=company_name)}"')
        words = reference_words(report.style_h1, sub.style_h2)
        length = (f" Length: about {min(max(words, 150), 1800):,} words including tables -- the reference "
                  f"report's length for this part; go shorter only where the evidence is thin, and no longer."
                  if words else "")
        lines.append(f'- section_key "{key}": {heading}. {sub.guidance.format(company=company_name)}{length}')
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
    siblings = _sibling_parts(spec, company_name)
    if siblings:
        lines += ["", "Other parts of this same report are being written separately -- do not repeat their "
                      "content (refer to them by name if useful):"] + [f"- {s}" for s in siblings]
    if spec.evidence_tags:
        tags = ", ".join(_TAG_LABELS[t] for t in spec.evidence_tags)
        lines += ["", f"Evidence: the documents that matter most here are in the evidence pack's {tags} groups, "
                      f"but use anything relevant anywhere in the pack, the Key Facts sheet and the research memo."]

    excerpts = evidence.source_excerpts(job_id, spec.evidence_tags, spec.source_chars)
    if excerpts:
        lines += ["", "### Primary-source text (for exact figures; the evidence pack has the digests)", excerpts]
    if not spec.summary_context:
        query = " ".join([title] + [f"{report.sub(s).title} {report.sub(s).guidance}" for s in spec.subs])
        query = query.format(company=company_name) if "{company}" in query else query
        pages = evidence.retrieve_pages(job_id, query)
        if pages:
            lines += ["", "### Verbatim data-room pages most relevant to this part (from a search over every page; "
                          "use them for exact terms, figures and dates)", pages]
    dependency_context = build_dependency_context(job_id, spec)
    if dependency_context:
        header = ("### The drafted reports" if spec.summary_context
                  else "### Already-drafted related parts (stay consistent; do not repeat them)")
        lines += ["", header, dependency_context]
    text = "\n".join(lines)
    if len(text) > MAX_USER_CONTENT_CHARS:
        text = text[:MAX_USER_CONTENT_CHARS] + "\n[... remaining context omitted for length]"
    return text


def build_drafting_inputs(job_id: str, company_name: str, spec: SectionSpec) -> dict:
    """Everything one drafting call needs besides the spec -- shared by the
    single-task path (app/tasks/draft_section.py) and the Batch API path
    (app/batch_runner.py) so both send identical prompts. `context` is the
    job's shared, cached prefix; `user_content` is this call's own part."""
    return {
        "context": evidence.drafting_context(job_id),
        "user_content": build_user_content(job_id, company_name, spec),
        "target_words": target_words(spec),
    }


def target_words(spec: SectionSpec) -> int:
    """The reference report's length for the parts one call writes."""
    report = REPORT_BY_KEY[spec.report]
    total = 0
    for sub_key in spec.subs:
        words = reference_words(report.style_h1, report.sub(sub_key).style_h2)
        total += min(max(words, 150), 1800) if words else 600
    return total


# ----------------------------------------------------------------- names --

_ENTITY = re.compile(r",?\s+(inc|llc|l\.l\.c|corp|corporation|ltd|limited|pbc|co)\.?$", re.I)
LEAK_PATTERNS = [r"\bZZ\b", r"delete me", r"Stage 2 Test", r"\(test\)"]


def _short_name(legal: str) -> str:
    return _ENTITY.sub("", legal.strip()).strip(" ,")


def _legal_from_digests(job_id, hint: str) -> str:
    """The company's legal name as the documents state it: the entity name
    (", Inc.", "LLC", ...) that appears most often among the parties the
    fact cards recorded, preferring one sharing a word with `hint`."""
    counts: dict[str, int] = {}
    for f in repo.list_dataroom_files(job_id):
        for party in (f.get("digest") or {}).get("parties") or []:
            name = " ".join(str(party).split())
            if _ENTITY.search(name) and len(name) < 80:
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    words = {w for w in re.findall(r"[a-z]{3,}", hint.lower())} - {"test", "delete", "stage", "inc", "llc"}
    ranked = sorted(counts, key=lambda n: (-(bool(words & set(re.findall(r"[a-z]{3,}", n.lower())))), -counts[n]))
    return ranked[0]


def company_names(job_id, fallback: str) -> tuple[str, str]:
    """(legal name, short name) for everything the report prints -- from the
    Key Facts sheet (charter / cap table), else the fact cards, never the
    job's own label (which may be a test name like "ZZ ... (delete me)")."""
    job = repo.get_job(job_id) or {}
    kf = job.get("key_facts") or {}
    if kf.get("legal_name"):
        return kf["legal_name"].strip(), (kf.get("short_name") or _short_name(kf["legal_name"])).strip()
    legal = _legal_from_digests(job_id, fallback)
    return (legal, _short_name(legal)) if legal else (fallback, fallback)


def section_texts(sections: list[dict]) -> list[str]:
    return [s["title"] + "\n" + render_blocks_as_text(s["content"]) for s in sections]


def leaked_names(job_id, job_label: str, texts: list[str]) -> list[str]:
    """Test/project labels that must never appear in a report."""
    legal, short = company_names(job_id, job_label)
    patterns = list(LEAK_PATTERNS)
    if job_label and job_label.strip().lower() not in (legal.lower(), short.lower())             and job_label.strip().lower() not in legal.lower():
        patterns.append(re.escape(job_label.strip()))
    joined = "\n".join(texts)
    return sorted({m.group(0) for p in patterns for m in re.finditer(p, joined, re.I)})


# ---------------------------------------------------------- persistence --

def _missing_placeholder(title: str) -> list[dict]:
    return [{"type": "paragraph",
             "text": f"[{title}: this subsection was not returned by the drafting model after repeated "
                     f"attempts. Edit it here or re-run the draft.]"}]


def persist_drafting_output(job_id: str, company_name: str, spec: SectionSpec, output, source_task_id,
                            final_attempt: bool = True) -> list[str]:
    """Write every section a drafting call produced, with fixed keys,
    titles, levels and ordering from the spec. If the model left out one of
    its sections, raise (so the task is retried) unless this was the last
    attempt, in which case a visible placeholder is stored instead."""
    by_key: dict[str, list[dict]] = {}
    for section in output.sections:
        by_key.setdefault(section.section_key, []).extend(b.model_dump() for b in section.blocks)
    missing = [k for k in spec.section_keys if not by_key.get(k)]
    if missing and not final_attempt:
        raise RuntimeError(f"drafting output is missing sections {missing}; retrying")

    defs = {d.section_key: d for d in section_defs(company_name)}
    report = REPORT_BY_KEY[spec.report]
    persisted = []
    for key in spec.section_keys:
        d = defs[key]
        meta = {}
        if d.heading_level == 1:
            meta = {"header": f"{company_name}  |  {report.header}" if report.header else "",
                    "basis": (output.basis_of_preparation or "").strip()}
        repo.upsert_report_section(
            job_id=job_id,
            section_key=key,
            title=d.title,
            order_index=d.order_index,
            blocks=by_key.get(key) or _missing_placeholder(d.title),
            source_task_id=source_task_id,
            heading_level=d.heading_level,
            meta=meta,
        )
        persisted.append(key)

    # A report no task writes an opening for (the Executive Summary) still
    # needs its Heading 1 row.
    if INTRO not in spec.subs:
        bare = defs[report.key]
        if repo.get_report_section(job_id, bare.section_key) is None:
            repo.upsert_report_section(job_id, bare.section_key, bare.title, bare.order_index, [],
                                       source_task_id, heading_level=1, meta={})
    return persisted


def persist_failed_draft(job_id: str, company_name: str, spec: SectionSpec, error: str, source_task_id) -> None:
    """Last-attempt failure of a drafting call: store a visible placeholder
    for each of its sections (keeping any that were already drafted) and
    let the task complete, so the summaries, compile and the rest of the
    pipeline still run. The placeholder says how to fix it."""
    defs = {d.section_key: d for d in section_defs(company_name)}
    report = REPORT_BY_KEY[spec.report]
    for key in spec.section_keys:
        if repo.get_report_section(job_id, key) is not None:
            continue
        d = defs[key]
        meta = {"header": f"{company_name}  |  {report.header}" if report.header else "", "basis": "",
                "draft_failed": True} if d.heading_level == 1 else {"draft_failed": True}
        repo.upsert_report_section(
            job_id=job_id, section_key=key, title=d.title, order_index=d.order_index,
            blocks=[{"type": "paragraph", "text": (
                f"[{d.title}: this part could not be drafted automatically ({error[:200]}). "
                f"Write it here in the editor, or use \"Restart Stage 2\" to draft the report again.]")}],
            source_task_id=source_task_id, heading_level=d.heading_level, meta=meta,
        )
    if INTRO not in spec.subs:
        bare = defs[report.key]
        if repo.get_report_section(job_id, bare.section_key) is None:
            repo.upsert_report_section(job_id, bare.section_key, bare.title, bare.order_index, [],
                                       source_task_id, heading_level=1, meta={})


def restart_stage2(job_id: str, company_name: str) -> list[str]:
    """Throw away the drafted report (and everything after it) and run
    Stage 2 again on the documents already read -- reading is not repeated."""
    from app import storage

    repo.delete_stage_tasks(job_id, STAGE_DRAFTING)
    try:
        storage.delete(evidence.drafting_context_key(job_id))
    except Exception:
        pass
    return create_stage2_tasks(job_id, company_name)


def persist_qa_output(job_id: str, spec, output, source_task_id) -> None:
    """Shared by the single-task and batch execution paths."""
    if not spec.has_score and output.score is not None:
        # Model ignored the "don't score this" instruction -- keep the note
        # but keep a stray number out of the Report Card aggregate.
        output.score = None
    repo.upsert_qa_score(
        job_id=job_id,
        block=spec.block,
        assessment_name=spec.assessment_name,
        scale=spec.scale,
        score=output.score,
        findings={"summary": output.summary, "findings": output.findings},
        source_task_id=source_task_id,
    )
