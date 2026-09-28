"""Stage 2 orchestration: turning app.sections_spec's task graph into real
`tasks` rows for one job, and rendering a completed section's content back
into plain text so a dependent task can use it as prior context.
"""
from app import repo
from app.sections_spec import SECTION_SPECS, SectionSpec

STAGE_DRAFTING = 2
STAGE_COMPILE = 3
STAGE_QA = 4
STAGE_GAP_MITIGATION = 5
STAGE_FINAL_RENDER = 6


def create_stage2_tasks(job_id: str, company_name: str) -> list[str]:
    """Insert one `tasks` row per SectionSpec, with depends_on resolved to
    real task UUIDs. Returns the created task ids in creation order.
    _SPECS-derived specs come first (in the list order defined in
    sections_spec.py, which already puts every dependency before its
    dependent); executive_summary is created last since it depends on all
    of them.
    """
    task_key_to_id: dict[str, str] = {}
    created_ids: list[str] = []

    ordered = [s for s in SECTION_SPECS if s.task_key != "executive_summary"]
    exec_summary = next(s for s in SECTION_SPECS if s.task_key == "executive_summary")

    for spec in ordered + [exec_summary]:
        depends_on_ids = [task_key_to_id[k] for k in spec.depends_on]
        task_id = repo.create_task(
            job_id=job_id,
            stage=STAGE_DRAFTING,
            task_type="draft_section",
            task_key=spec.task_key,
            depends_on=depends_on_ids,
            payload={"job_id": job_id, "task_key": spec.task_key, "company_name": company_name},
        )
        task_key_to_id[spec.task_key] = task_id
        created_ids.append(task_id)

    return created_ids


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
    applicable) so it only ever runs once, after everything is scored --
    resolving prompt_inventory.md's Flag A3 (the source sheet lists it
    twice).
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
        # validation check (which is on a different 1-10 scale, kept
        # separate per Flag M1 rather than folded into the same aggregate).
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
    """Insert the two sequential Stage 5 tasks: gap_analysis, then
    mitigation_strategy (depending on gap_analysis). Low call count, kept
    on the standard API per the brief."""
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
    """Insert the final_render task, depending on mitigation_strategy (and
    therefore transitively on every Stage 4 QA task and gap_analysis)."""
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


def full_dataroom_text(job_id: str, max_total_chars: int = 150_000) -> str:
    """Every dataroom file's extracted text, unfiltered by category -- QA
    tasks need to cross-check the whole room, unlike Stage 2 drafting tasks
    which only need one category's worth of source material.

    A single object that can't be fetched (deleted from storage, transient
    error) is skipped with a placeholder note rather than failing the whole
    task -- an expensive QA call for the other 20+ files shouldn't be lost
    over one bad file.
    """
    from app import extract, storage

    files = repo.list_dataroom_files(job_id, file_kind="dataroom")
    parts = []
    total = 0
    for f in files:
        try:
            data = storage.get_bytes(f["object_key"])
            text = extract.extract_text(f["original_filename"], data)
        except Exception as exc:
            text = f"[could not fetch this file from storage: {exc}]"
        chunk = f"--- {f['original_filename']} (category: {f['category']}) ---\n{text}"
        if total + len(chunk) > max_total_chars:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n".join(parts)


def render_full_report_text(job_id: str) -> str:
    """The full compiled draft, as plain text, in section order -- what a
    Stage 4 QA task reads as "the due diligence report" per
    docs/prompt_inventory.md."""
    sections = repo.list_report_sections(job_id)
    parts = []
    for s in sections:
        parts.append(f"## {s['title']}\n{render_blocks_as_text(s['content'])}")
    return "\n\n".join(parts)


def render_blocks_as_text(blocks: list[dict]) -> str:
    """Turn a SectionContent.blocks JSON list back into plain text, for
    feeding a completed section to a dependent drafting task as context."""
    lines = []
    for block in blocks:
        btype = block.get("type")
        if btype == "heading":
            lines.append(f"\n**{block['text']}**")
        elif btype == "paragraph":
            lines.append(block["text"])
        elif btype == "table":
            lines.append(" | ".join(block["headers"]))
            for row in block["rows"]:
                lines.append(" | ".join(row))
    return "\n".join(lines)


MAX_DRAFTING_DATAROOM_CHARS = 60_000


def dataroom_text_for_categories(job_id: str, categories: tuple[str, ...]) -> str:
    """Category-filtered dataroom text for one Stage 2 drafting task. Shared
    by app/tasks/draft_section.py (single-task path) and app/batch_runner.py
    (Batch API path) so both gather identical inputs for identical prompts."""
    if not categories:
        return ""
    from app import extract, storage

    files = [f for f in repo.list_dataroom_files(job_id, file_kind="dataroom") if f["category"] in categories]
    parts = []
    total = 0
    for f in files:
        try:
            data = storage.get_bytes(f["object_key"])
            text = extract.extract_text(f["original_filename"], data)
        except Exception as exc:
            text = f"[could not fetch this file from storage: {exc}]"
        chunk = f"--- {f['original_filename']} ---\n{text}"
        if total + len(chunk) > MAX_DRAFTING_DATAROOM_CHARS:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n".join(parts)


def pitch_deck_text(job_id: str) -> str:
    from app import extract, storage

    files = repo.list_dataroom_files(job_id, file_kind="pitch_deck")
    if not files:
        return ""
    f = files[0]
    try:
        data = storage.get_bytes(f["object_key"])
        return extract.extract_text(f["original_filename"], data)
    except Exception as exc:
        return f"[could not fetch pitch deck from storage: {exc}]"


def persist_drafting_output(job_id: str, company_name: str, spec: SectionSpec, output, source_task_id) -> list[str]:
    """Shared by the single-task and batch execution paths: write every
    SectionContent in a DraftingOutput to report_sections."""
    persisted = []
    for section in output.sections:
        produced = next(
            (p for p in spec.produces if p.section_key.format(company=company_name) == section.section_key),
            None,
        )
        order_index = produced.order_index if produced else 999
        blocks = [b.model_dump() for b in section.blocks]
        repo.upsert_report_section(
            job_id=job_id,
            section_key=section.section_key,
            title=section.title,
            order_index=order_index,
            blocks=blocks,
            source_task_id=source_task_id,
        )
        persisted.append(section.section_key)
    return persisted


def persist_qa_output(job_id: str, spec, output, source_task_id) -> None:
    """Shared by the single-task and batch execution paths."""
    if not spec.has_score and output.score is not None:
        # Model ignored the "don't score this" instruction -- keep the
        # note but don't let a stray number sneak into Report Card's
        # aggregate for a check the source spreadsheet says shouldn't score.
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


def build_dependency_context(job_id: str, company_name: str, spec: SectionSpec) -> str:
    from app.sections_spec import SPEC_BY_TASK_KEY

    parts = []
    for dep_task_key in spec.depends_on:
        dep_spec = SPEC_BY_TASK_KEY[dep_task_key]
        for produced in dep_spec.produces:
            section_key = produced.section_key.format(company=company_name)
            row = repo.get_report_section(job_id, section_key)
            if row is None:
                continue
            parts.append(f"#### {row['title']}\n{render_blocks_as_text(row['content'])}")
    return "\n\n".join(parts)
