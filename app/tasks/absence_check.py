"""Stage 2, last step: the verification pass (app/verify.py) -- false
"missing document" claims, required facts not stated, contradictions with
the fact store, forbidden wording -- corrected paragraph by paragraph.
Never blocks compile: on its last attempt a failure is logged and the
draft is left as drafted."""
from app import pipeline, repo, verify
from app.tasks.registry import register


@register("absence_check")
def run_absence_check(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    name = pipeline.company_names(job_id, payload["company_name"])[1]
    try:
        return verify.run(job_id, name)
    except Exception as exc:
        if task["attempts"] + 1 < repo.MAX_TASK_ATTEMPTS:
            raise
        return {"failed": True, "error": str(exc)[:300]}
