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
from app import evidence, repo
from app.reference_report import style_excerpt
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


def digest_task_key(file_id) -> str:
    return f"digest:{file_id}"


def create_digest_tasks(job_id: str, company_name: str, file_ids: list[str]) -> list[str]:
    """One digest_document task per file that doesn't already have one.
    Returns every digest task id for those files (new or existing)."""
    existing = repo.existing_task_keys(job_id)
    ids = []
    for file_id in file_ids:
        key = digest_task_key(file_id)
        if key in existing:
            ids.append(existing[key])
            continue
        ids.append(repo.create_task(
            job_id=job_id,
            stage=STAGE_INTAKE,
            task_type="digest_document",
            task_key=key,
            depends_on=[],
            payload={"job_id": job_id, "file_id": str(file_id), "company_name": company_name},
        ))
    return ids


def create_stage2_tasks(job_id: str, company_name: str) -> list[str]:
    """Key Facts (depending on every file's digest -- creating any digest
    tasks still missing, e.g. for jobs uploaded before digests existed),
    then one draft_section task per SectionSpec, each depending on Key
    Facts plus its own spec dependencies. Returns the Stage 2 task ids."""
    files = repo.list_dataroom_files(job_id)
    digest_ids = create_digest_tasks(job_id, company_name, [str(f["id"]) for f in files])

    key_facts_id = repo.create_task(
        job_id=job_id,
        stage=STAGE_DRAFTING,
        task_type="key_facts",
        task_key=KEY_FACTS_TASK_KEY,
        depends_on=digest_ids,
        payload={"job_id": job_id, "company_name": company_name},
    )
    created = [key_facts_id]
    task_key_to_id: dict[str, str] = {}
    for spec in SECTION_SPECS:  # list order already puts every dependency first
        depends_on_ids = [key_facts_id] + [task_key_to_id[k] for k in spec.depends_on]
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

    created_ids: list[str] = []
    scoring_task_ids: list[str] = []

    specs = list(DD_ASSESSMENT_SPECS)
    if vertical == "life_sciences":
        specs += LIFE_SCIENCE_SPECS
    specs.append(MASTER_VALIDATION_SPEC)

    for spec in specs:
        task_id = repo.create_task(
            job_id=job_id,
            stage=STAGE_QA,
            task_type="qa_score",
            task_key=spec.task_key,
            depends_on=[],
            payload={"job_id": job_id, "task_key": spec.task_key, "company_name": company_name},
        )
        created_ids.append(task_id)
        # Report Card aggregates DD + Life Science scores, not the master
        # validation check (which is on a different 1-10 scale).
        if spec.block in ("dd_assessment", "life_science_assessment"):
            scoring_task_ids.append(task_id)

    report_card_id = repo.create_task(
        job_id=job_id,
        stage=STAGE_QA,
        task_type="report_card",
        task_key=REPORT_CARD_TASK_KEY,
        depends_on=scoring_task_ids,
        payload={"job_id": job_id},
    )
    created_ids.append(report_card_id)
    return created_ids


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
_SUMMARY_SECTION_CHARS = 1_500


def _summarize_section(row: dict, sub_key: str) -> str:
    blocks = row["content"]
    if sub_key == INTRO or sub_key in _FULL_IN_SUMMARY:
        return _render_section(row)
    callouts = [b for b in blocks if b.get("type") == "callout"]
    rest = [b for b in blocks if b.get("type") != "callout"]
    text = render_blocks_as_text(callouts) + "\n" + render_blocks_as_text(rest)
    if len(text) > _SUMMARY_SECTION_CHARS:
        text = text[:_SUMMARY_SECTION_CHARS] + " [...]"
    return f"{_heading_marker(row.get('heading_level') or 2)} {row['title']}\n{text.strip()}"


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


def build_drafting_inputs(job_id: str, company_name: str, spec: SectionSpec) -> dict:
    """Everything one drafting call needs besides the spec -- shared by the
    single-task path (app/tasks/draft_section.py) and the Batch API path
    (app/batch_runner.py) so both send identical prompts."""
    report = REPORT_BY_KEY[spec.report]
    job_context = evidence.job_context_block(job_id)
    excerpt = style_excerpt(report.style_h1, [report.sub(s).style_h2 for s in spec.subs])
    dependency_context = build_dependency_context(job_id, spec)
    used = len(job_context) + len(excerpt) + len(dependency_context) + 15_000  # + system prompt & instructions
    evidence_text = evidence.evidence_for(job_id, spec.evidence_tags, used, spec.needs_web_search) \
        if spec.evidence_tags else "(this part is written from the Key Facts sheet and the drafted reports above)"
    return {
        "job_context": job_context,
        "style_excerpt": excerpt,
        "evidence_text": evidence_text,
        "dependency_context": dependency_context,
    }


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
