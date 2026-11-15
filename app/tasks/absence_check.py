"""Stage 2, last step: check every "missing / unsigned / unconfirmed
document" claim in the draft against the data room and correct the ones a
document contradicts (app/absence.py). Never blocks compile: on its last
attempt a failure is logged and the draft is left as drafted."""
from app import absence, pipeline, repo
from app.tasks.registry import register


@register("absence_check")
def run_absence_check(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    name = pipeline.company_names(job_id, payload["company_name"])[1]
    try:
        return absence.run(job_id, name)
    except Exception as exc:
        if task["attempts"] + 1 < repo.MAX_TASK_ATTEMPTS:
            raise
        return {"failed": True, "error": str(exc)[:300]}
