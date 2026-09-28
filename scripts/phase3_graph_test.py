"""Phase 3 proof (part 1, no Claude calls): create_stage2_tasks builds a
valid 20-task DAG in Postgres -- correct depends_on resolution, executive
summary depending on every other task, and every task claimable in an order
that never violates a dependency, exercised via the real worker claim loop
with a stub Claude call.

Run: PYTHONPATH=. ./.venv/Scripts/python.exe scripts/phase3_graph_test.py
"""
from unittest.mock import patch

from app import repo
from app.pipeline import create_stage2_tasks
from app.sections_spec import SECTION_SPECS
from app.worker import run_one_task


def main():
    job_id = repo.create_job("Phase3 Graph Test Co")
    task_ids = create_stage2_tasks(job_id, "Phase3 Graph Test Co")
    assert len(task_ids) == len(SECTION_SPECS) == 20, f"expected 20 tasks, got {len(task_ids)}"

    tasks = repo.list_tasks_for_job(job_id)
    by_task_key = {t["task_key"]: t for t in tasks}

    exec_summary = by_task_key["executive_summary"]
    assert len(exec_summary["depends_on"]) == 19, (
        f"executive_summary should depend on all 19 other tasks, got {len(exec_summary['depends_on'])}"
    )
    other_ids = {str(t["id"]) for t in tasks if t["task_key"] != "executive_summary"}
    assert {str(d) for d in exec_summary["depends_on"]} == other_ids, "exec summary deps mismatch"

    sm_swot = by_task_key["sales_marketing_swot"]
    sm_draft = by_task_key["sales_marketing_draft"]
    assert [str(d) for d in sm_swot["depends_on"]] == [str(sm_draft["id"])]

    print(f"PASS -- {len(tasks)} tasks created with correct dependency structure")

    # Drain the whole graph with a stubbed Claude call (a real one needs
    # ANTHROPIC_API_KEY, which is a separate check -- see phase3_llm_test.py).
    # This proves the *ordering* the worker will actually run under, without
    # spending real API calls: each task's dependency-section-lookup must
    # succeed by the time it runs, which only holds if claiming genuinely
    # respects the graph.
    from app.models import DraftingOutput, ParagraphBlock, SectionContent

    def fake_call_claude(spec, company_name, dataroom_text, pitch_deck_text, dependency_context):
        sections = []
        for produced in spec.produces:
            key = produced.section_key.format(company=company_name)
            sections.append(
                SectionContent(
                    section_key=key,
                    title=key,
                    blocks=[ParagraphBlock(text=f"stub content for {key}")],
                )
            )
        return DraftingOutput(sections=sections)

    with patch("app.tasks.draft_section.call_claude", side_effect=fake_call_claude):
        for _ in range(100):
            if not run_one_task():
                break

    tasks_after = repo.list_tasks_for_job(job_id)
    statuses = {t["task_key"]: t["status"] for t in tasks_after}
    failed = {k: v for k, v in statuses.items() if v != "completed"}
    assert not failed, f"expected all tasks completed, found: {failed}"
    print("PASS -- all 20 tasks completed via the real worker claim loop (stubbed Claude call)")

    sections = repo.list_report_sections(job_id)
    assert len(sections) == 25, f"expected 25 report_sections rows, got {len(sections)}"
    print(f"PASS -- {len(sections)} report_sections rows persisted, matching the 25-heading template")

    print("\nAll Phase 3 graph checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
