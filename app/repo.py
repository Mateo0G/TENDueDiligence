"""Raw-SQL data access for jobs/tasks. No ORM -- every query here is meant to
be read top to bottom and reasoned about directly.
"""
from typing import Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.db import pool

# The core claiming query: atomically pick one pending task whose
# dependencies (if any) have all completed, and mark it running in the same
# statement. FOR UPDATE SKIP LOCKED means concurrent workers never block on
# each other and never double-claim the same row.
_CLAIM_SQL = """
    UPDATE tasks
    SET status = 'running', locked_by = %(worker_id)s, locked_at = now(), updated_at = now()
    WHERE id = (
        SELECT id FROM tasks
        WHERE status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM tasks dep
              WHERE dep.id = ANY(tasks.depends_on) AND dep.status <> 'completed'
          )
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, job_id, stage, task_type, task_key, payload, depends_on, attempts
"""


def claim_task(worker_id: str) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_CLAIM_SQL, {"worker_id": worker_id})
            return cur.fetchone()


def complete_task(task_id, result: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='completed', result=%s, error=NULL, updated_at=now() WHERE id=%s",
            (Json(result), task_id),
        )


def fail_task(task_id, error: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='failed', error=%s, attempts=attempts+1, updated_at=now() WHERE id=%s",
            (error, task_id),
        )


def release_task(task_id) -> None:
    """Put a claimed-but-not-finished task back to pending, e.g. on shutdown."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE tasks SET status='pending', locked_by=NULL, locked_at=NULL, updated_at=now() WHERE id=%s",
            (task_id,),
        )


def create_job(company_name: str, vertical: Optional[str] = None) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO jobs (company_name, vertical) VALUES (%s, %s) RETURNING id",
            (company_name, vertical),
        ).fetchone()
        return str(row[0])


def create_task(
    job_id,
    stage: int,
    task_type: str,
    task_key: str,
    depends_on: Optional[list] = None,
    payload: Optional[dict] = None,
) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO tasks (job_id, stage, task_type, task_key, depends_on, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (job_id, stage, task_type, task_key, depends_on or [], Json(payload or {})),
        ).fetchone()
        return str(row[0])


def get_task(task_id) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
            return cur.fetchone()


def list_tasks_for_job(job_id) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE job_id=%s ORDER BY created_at", (job_id,)
            )
            return cur.fetchall()
