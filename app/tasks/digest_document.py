"""Legacy Stage 1 task (one file per task), kept so tasks queued before the
switch to app/tasks/read_files.py still run -- now through the same cheap
reading path."""
from app import reading, repo
from app.tasks.registry import register


@register("digest_document")
def run_digest_document(task: dict) -> dict:
    payload = task["payload"]
    final_attempt = task["attempts"] + 1 >= repo.MAX_TASK_ATTEMPTS
    return reading.read_files(payload["job_id"], payload["company_name"], [payload["file_id"]], final_attempt)
