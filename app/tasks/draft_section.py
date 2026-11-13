"""Stage 2 task handler: draft one or more subsections of one report via
Claude and persist them to report_sections, per app/sections_spec.py.
Input-gathering and persistence are shared with app/batch_runner.py's Batch
API path -- see app/pipeline.py.
"""
from app import pipeline, repo
from app.claude_client import draft_section as call_claude
from app.sections_spec import SPEC_BY_TASK_KEY
from app.tasks.registry import register


@register("draft_section")
def run_draft_section(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    company_name = payload["company_name"]
    spec = SPEC_BY_TASK_KEY.get(payload["task_key"])
    if spec is None:
        raise RuntimeError(
            f"unknown drafting task {payload['task_key']!r} -- this job was started on an older report "
            f"structure; create a new job to draft it with the current one"
        )

    inputs = pipeline.build_drafting_inputs(job_id, company_name, spec)
    output = call_claude(spec, company_name, **inputs)
    final_attempt = task["attempts"] + 1 >= repo.MAX_TASK_ATTEMPTS
    persisted = pipeline.persist_drafting_output(job_id, company_name, spec, output, task["id"], final_attempt)
    return {"persisted_sections": persisted}
