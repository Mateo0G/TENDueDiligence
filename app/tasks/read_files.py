"""Stage 1 task: read a handful of dataroom files (app/reading.py).

Created as soon as files are uploaded, so the room is read in the
background. Never blocks the pipeline: on its last attempt, files that
still couldn't be read get a fallback digest instead of failing the task,
because Key Facts (and so every drafting task) depends on every reading
task.
"""
from app import reading, repo
from app.tasks.registry import register


@register("read_files")
def run_read_files(task: dict) -> dict:
    payload = task["payload"]
    final_attempt = task["attempts"] + 1 >= repo.MAX_TASK_ATTEMPTS
    return reading.read_files(payload["job_id"], payload["company_name"], payload["file_ids"], final_attempt)
