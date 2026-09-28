"""Stage 2 task handler: draft one or more report sections via Claude,
persist them to report_sections, per the graph in app/sections_spec.py.
Input-gathering and persistence are shared with app/batch_runner.py's Batch
API path -- see app/pipeline.py.
"""
from app import pipeline
from app.claude_client import draft_section as call_claude
from app.sections_spec import SPEC_BY_TASK_KEY
from app.tasks.registry import register


@register("draft_section")
def run_draft_section(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    task_key = payload["task_key"]
    company_name = payload["company_name"]
    spec = SPEC_BY_TASK_KEY[task_key]

    dataroom_text = pipeline.dataroom_text_for_categories(job_id, spec.dataroom_categories)
    pitch_deck_text = pipeline.pitch_deck_text(job_id)
    dependency_context = pipeline.build_dependency_context(job_id, company_name, spec)

    output = call_claude(spec, company_name, dataroom_text, pitch_deck_text, dependency_context)
    persisted = pipeline.persist_drafting_output(job_id, company_name, spec, output, task["id"])

    return {"persisted_sections": persisted}
