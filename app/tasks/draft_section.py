"""Stage 2 task handler: draft one or more subsections of one report via
Claude and persist them to report_sections, per app/sections_spec.py.

Never fails permanently: on the last attempt an error leaves visible
placeholders for its sections and the task completes, so the summary
parts, compile and the rest of the pipeline still run (one failed part
used to leave everything downstream pending forever).
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
            f"structure; use Restart Stage 2"
        )
    final_attempt = task["attempts"] + 1 >= repo.MAX_TASK_ATTEMPTS
    try:
        inputs = pipeline.build_drafting_inputs(job_id, company_name, spec)
        output = call_claude(spec, company_name, **inputs)
        persisted = pipeline.persist_drafting_output(job_id, company_name, spec, output, task["id"], final_attempt)
    except Exception as exc:
        if not final_attempt:
            raise
        pipeline.persist_failed_draft(job_id, company_name, spec, str(exc), task["id"])
        return {"failed": True, "error": str(exc)[:300]}
    return {"persisted_sections": persisted}
