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


def ping() -> None:
    """Cheap keep-alive query. app/batch_runner.py calls this during its
    Anthropic-batch polling wait, which can otherwise leave the DB
    connection idle long enough to trip a local dev SSH tunnel's idle
    timeout (see app/db.py) -- not needed once Phase 8 connects to Postgres
    over Railway's internal network directly."""
    with pool.connection() as conn:
        conn.execute("SELECT 1")


def claim_task(worker_id: str) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_CLAIM_SQL, {"worker_id": worker_id})
            return cur.fetchone()


# Batch-mode variant of _CLAIM_SQL: claims up to `limit` ready rows of one
# task_type within one job in a single atomic statement, for submitting as
# one Anthropic Batch request instead of one task = one synchronous call.
# `locked_by` holds "batch:<pending>" as a placeholder until the real batch
# id is known (see app/batch_runner.py), analogous to how the single-task
# path stores a worker id there -- either way it's just "who/what has this
# task claimed right now".
_CLAIM_BATCH_SQL = """
    UPDATE tasks
    SET status = 'running', locked_by = %(worker_id)s, locked_at = now(), updated_at = now()
    WHERE id IN (
        SELECT id FROM tasks
        WHERE job_id = %(job_id)s
          AND task_type = %(task_type)s
          AND status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM tasks dep
              WHERE dep.id = ANY(tasks.depends_on) AND dep.status <> 'completed'
          )
        ORDER BY created_at
        FOR UPDATE SKIP LOCKED
        LIMIT %(limit)s
    )
    RETURNING id, job_id, stage, task_type, task_key, payload, depends_on, attempts
"""


def claim_ready_tasks(job_id, task_type: str, worker_id: str, limit: int = 100) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _CLAIM_BATCH_SQL,
                {"job_id": job_id, "task_type": task_type, "worker_id": worker_id, "limit": limit},
            )
            return cur.fetchall()


def set_task_locked_by(task_id, locked_by: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE tasks SET locked_by=%s WHERE id=%s", (locked_by, task_id))


def count_pending_tasks(job_id, task_type: str) -> int:
    """Used by app/batch_runner.py to tell "every wave is done" apart from
    "some tasks are stuck pending forever because a dependency failed" --
    both look like "claim_ready_tasks returned nothing" from the caller's
    side, so the loop needs this separate check to distinguish them."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM tasks WHERE job_id=%s AND task_type=%s AND status='pending'",
                (job_id, task_type),
            )
            return cur.fetchone()[0]


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


def list_task_ids_by_stage(job_id, stage: int) -> list[str]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM tasks WHERE job_id=%s AND stage=%s", (job_id, stage)
            )
            return [str(row[0]) for row in cur.fetchall()]


def list_tasks_for_job(job_id) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE job_id=%s ORDER BY created_at", (job_id,)
            )
            return cur.fetchall()


def list_jobs() -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            return cur.fetchall()


def get_job(job_id) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
            return cur.fetchone()


def set_job_vertical(job_id, vertical: str, signals: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET vertical=%s, vertical_signals=%s, updated_at=now() WHERE id=%s",
            (vertical, Json(signals), job_id),
        )


def set_job_pitch_deck_key(job_id, object_key: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET pitch_deck_key=%s, updated_at=now() WHERE id=%s",
            (object_key, job_id),
        )


def set_job_status(job_id, status: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET status=%s, updated_at=now() WHERE id=%s",
            (status, job_id),
        )


def set_job_output_key(job_id, object_key: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET output_key=%s, updated_at=now() WHERE id=%s",
            (object_key, job_id),
        )


def approve_draft(job_id) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET draft_approved_at=now(), updated_at=now() WHERE id=%s",
            (job_id,),
        )


def update_report_section_content(job_id, section_key: str, title: str, blocks: list[dict]) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE report_sections SET title=%s, content=%s, updated_at=now() "
            "WHERE job_id=%s AND section_key=%s",
            (title, Json(blocks), job_id, section_key),
        )


def upsert_qa_score(
    job_id,
    block: str,
    assessment_name: str,
    scale: str,
    score: Optional[float],
    findings: dict,
    source_task_id,
) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO qa_scores (job_id, block, assessment_name, scale, score, findings, source_task_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id, block, assessment_name) DO UPDATE SET "
            "scale=EXCLUDED.scale, score=EXCLUDED.score, findings=EXCLUDED.findings, "
            "source_task_id=EXCLUDED.source_task_id "
            "RETURNING id",
            (job_id, block, assessment_name, scale, score, Json(findings), source_task_id),
        ).fetchone()
        return str(row[0])


def set_job_gap_analysis(job_id, gap_analysis: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET gap_analysis=%s, updated_at=now() WHERE id=%s",
            (Json(gap_analysis), job_id),
        )


def set_job_mitigation_strategy(job_id, mitigation_strategy: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET mitigation_strategy=%s, updated_at=now() WHERE id=%s",
            (Json(mitigation_strategy), job_id),
        )


def list_qa_scores(job_id, block: Optional[str] = None) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if block:
                cur.execute(
                    "SELECT * FROM qa_scores WHERE job_id=%s AND block=%s ORDER BY assessment_name",
                    (job_id, block),
                )
            else:
                cur.execute(
                    "SELECT * FROM qa_scores WHERE job_id=%s ORDER BY block, assessment_name",
                    (job_id,),
                )
            return cur.fetchall()


def add_dataroom_file(
    job_id,
    object_key: str,
    original_filename: str,
    file_kind: str,
    category: Optional[str],
    category_method: Optional[str],
    size_bytes: int,
    content_type: Optional[str],
) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO dataroom_files "
            "(job_id, object_key, original_filename, file_kind, category, category_method, size_bytes, content_type) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                job_id,
                object_key,
                original_filename,
                file_kind,
                category,
                category_method,
                size_bytes,
                content_type,
            ),
        ).fetchone()
        return str(row[0])


def upsert_report_section(
    job_id,
    section_key: str,
    title: str,
    order_index: int,
    blocks: list[dict],
    source_task_id,
) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO report_sections (job_id, section_key, title, order_index, content, source_task_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id, section_key) DO UPDATE SET "
            "title=EXCLUDED.title, order_index=EXCLUDED.order_index, content=EXCLUDED.content, "
            "source_task_id=EXCLUDED.source_task_id, updated_at=now() "
            "RETURNING id",
            (job_id, section_key, title, order_index, Json(blocks), source_task_id),
        ).fetchone()
        return str(row[0])


def get_report_section(job_id, section_key: str) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM report_sections WHERE job_id=%s AND section_key=%s",
                (job_id, section_key),
            )
            return cur.fetchone()


def list_report_sections(job_id) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM report_sections WHERE job_id=%s ORDER BY order_index",
                (job_id,),
            )
            return cur.fetchall()


def list_dataroom_files(job_id, file_kind: Optional[str] = None) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if file_kind:
                cur.execute(
                    "SELECT * FROM dataroom_files WHERE job_id=%s AND file_kind=%s ORDER BY created_at",
                    (job_id, file_kind),
                )
            else:
                cur.execute(
                    "SELECT * FROM dataroom_files WHERE job_id=%s ORDER BY created_at",
                    (job_id,),
                )
            return cur.fetchall()
