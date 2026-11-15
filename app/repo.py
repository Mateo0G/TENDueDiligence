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
#
# A task still 'running' long after it was claimed belongs to a worker that
# died mid-task (a redeploy kills the worker process) -- it is reclaimed
# rather than left 'running' forever with everything downstream stuck
# behind it. No task legitimately runs anywhere near this long.
STALE_LOCK_MINUTES = 30

_CLAIM_SQL = f"""
    UPDATE tasks
    SET status = 'running', locked_by = %(worker_id)s, locked_at = now(), updated_at = now()
    WHERE id = (
        SELECT id FROM tasks
        WHERE (status = 'pending'
               OR (status = 'running' AND locked_at < now() - interval '{STALE_LOCK_MINUTES} minutes'))
          AND (run_after IS NULL OR run_after <= now())
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


MAX_TASK_ATTEMPTS = 3


def fail_task(task_id, error: str) -> None:
    """Bumps attempts; while retries remain, puts the task back to
    'pending' instead of leaving it permanently 'failed'. A transient
    failure (rate limit, a truncated response) would otherwise block every
    task that depends on it forever -- e.g. Stage 4's report_card, which
    depends on every scoring task and has no other way to become ready
    again. SKIP LOCKED in claim_task means no special handling is needed
    for a row that goes back to pending here; it just gets reclaimed on
    the next poll."""
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = CASE WHEN attempts + 1 < %(max_attempts)s THEN 'pending' ELSE 'failed' END,
                error = %(error)s,
                attempts = attempts + 1,
                locked_by = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE id = %(task_id)s
            """,
            {"task_id": task_id, "error": error, "max_attempts": MAX_TASK_ATTEMPTS},
        )


def defer_task(task_id, seconds: int, payload: dict | None = None) -> None:
    """Back to pending, not to be claimed for `seconds` -- for a task
    waiting on something external (a Message Batch). Not a failure:
    attempts is unchanged. `payload`, if given, replaces the task's payload
    (so the next run knows what it is waiting for)."""
    with pool.connection() as conn:
        if payload is not None:
            conn.execute("UPDATE tasks SET payload=%s WHERE id=%s", (Json(payload), task_id))
        conn.execute(
            "UPDATE tasks SET status='pending', locked_by=NULL, locked_at=NULL, "
            "run_after = now() + make_interval(secs => %s), updated_at=now() WHERE id=%s",
            (seconds, task_id),
        )


def retry_failed_tasks(job_id) -> int:
    """Human-triggered reset for a job's 'failed' tasks (e.g. after a code
    fix lands) -- distinct from fail_task's automatic in-process retry,
    since MAX_TASK_ATTEMPTS can still be exhausted by a bug that's since
    been fixed, with nothing left to automatically retry it. Resets
    attempts to 0 so the exhausted-retries case gets a fresh budget.
    Returns the number of tasks reset."""
    with pool.connection() as conn:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status='pending', error=NULL, attempts=0, locked_by=NULL, locked_at=NULL, updated_at=now()
            WHERE job_id=%s AND status='failed'
            """,
            (job_id,),
        )
        return cur.rowcount


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


def stage_already_started(job_id, stage: int) -> bool:
    """Guard for every /jobs/{job_id}/stageN endpoint, checked before
    creating that stage's tasks -- without it, starting an already-started
    stage (a stale page that missed the first click succeeding, a
    double-click, two tabs on the same job) hits the tasks table's
    UNIQUE(job_id, task_key) constraint and 500s instead of failing
    cleanly. One stage's tasks are always created in a single request, so
    "any row at this stage" is equivalent to "already started"."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tasks WHERE job_id=%s AND stage=%s LIMIT 1", (job_id, stage))
            return cur.fetchone() is not None


def list_jobs() -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM jobs ORDER BY created_at DESC")
            return cur.fetchall()


def delete_job(job_id) -> bool:
    """Deletes the job row; tasks, report_sections, qa_scores, and
    dataroom_files all cascade (ON DELETE CASCADE, see alembic/versions).
    Returns False if the job didn't exist. Doesn't touch R2 -- the caller
    (app/main.py) deletes the job's stored objects first, since that needs
    the row's data (object keys) while it still exists."""
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE id=%s", (job_id,))
        return cur.rowcount > 0


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


def set_job_logo_key(job_id, object_key: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET logo_key=%s, updated_at=now() WHERE id=%s",
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
    sha256: Optional[str] = None,
    source_path: Optional[str] = None,
) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO dataroom_files "
            "(job_id, object_key, original_filename, file_kind, category, category_method, size_bytes, "
            "content_type, sha256, source_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                job_id,
                object_key,
                original_filename,
                file_kind,
                category,
                category_method,
                size_bytes,
                content_type,
                sha256,
                source_path,
            ),
        ).fetchone()
        return str(row[0])


def find_file_by_sha256(job_id, sha256: str) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM dataroom_files WHERE job_id=%s AND sha256=%s ORDER BY created_at LIMIT 1",
                (job_id, sha256),
            )
            return cur.fetchone()


def find_digest_by_text_hash(job_id, text_hash: str, exclude_file_id) -> Optional[dict]:
    """Another file in this job whose extracted text is identical (same
    document saved twice with different bytes, e.g. re-exported PDFs) and
    already has a real digest."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, original_filename, digest FROM dataroom_files "
                "WHERE job_id=%s AND id<>%s AND digest->>'text_hash'=%s AND digest->>'duplicate_of' IS NULL "
                "LIMIT 1",
                (job_id, exclude_file_id, text_hash),
            )
            return cur.fetchone()


def count_dataroom_files(job_id) -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM dataroom_files WHERE job_id=%s", (job_id,))
            return cur.fetchone()[0]


def set_job_research(job_id, research: dict) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET research=%s, updated_at=now() WHERE id=%s", (Json(research), job_id))


PRICE_VERSION = 2  # prices from app/model_config.py


def record_api_usage(job_id, task_id, purpose: str, model: str, input_tokens: int, output_tokens: int,
                     cache_write_tokens: int, cache_read_tokens: int, web_searches: int, cost_usd: float,
                     batch: bool = False) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO api_usage (job_id, task_id, purpose, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, web_searches, cost_usd, price_version, batch) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (job_id, task_id, purpose, model, input_tokens, output_tokens, cache_write_tokens,
             cache_read_tokens, web_searches, cost_usd, PRICE_VERSION, batch),
        )


def get_research_cache(company_key: str, topic: str, max_age_days: int) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM research_cache WHERE company_key=%s AND topic=%s "
                "AND created_at > now() - make_interval(days => %s)",
                (company_key, topic, max_age_days),
            )
            return cur.fetchone()


def put_research_cache(company_key: str, topic: str, memo: str, sources: list) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO research_cache (company_key, topic, memo, sources) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (company_key, topic) DO UPDATE SET memo=EXCLUDED.memo, sources=EXCLUDED.sources, "
            "created_at=now()",
            (company_key, topic, memo, Json(sources)),
        )


def job_usage_summary(job_id) -> list[dict]:
    """Per purpose: call count, token totals and cost."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT purpose, model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
                "sum(output_tokens) AS output_tokens, sum(cache_write_tokens) AS cache_write_tokens, "
                "sum(cache_read_tokens) AS cache_read_tokens, sum(web_searches) AS web_searches, "
                "sum(CASE WHEN price_version = 1 AND model LIKE 'claude-sonnet-5%%' AND model NOT LIKE 'claude-sonnet-5-5%%' THEN (cost_usd - web_searches * 0.01) * 2 / 3 + web_searches * 0.01 ELSE cost_usd END) AS cost_usd FROM api_usage WHERE job_id=%s "
                "GROUP BY purpose, model ORDER BY 9 DESC",
                (job_id,),
            )
            return cur.fetchall()


def list_api_usage(job_id) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM api_usage WHERE job_id=%s ORDER BY created_at, id", (job_id,))
            return cur.fetchall()


def job_total_cost(job_id) -> float:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(sum(CASE WHEN price_version = 1 AND model LIKE 'claude-sonnet-5%%' AND model NOT LIKE 'claude-sonnet-5-5%%' THEN (cost_usd - web_searches * 0.01) * 2 / 3 + web_searches * 0.01 ELSE cost_usd END), 0) FROM api_usage WHERE job_id=%s", (job_id,))
            return float(cur.fetchone()[0])


def delete_stage_tasks(job_id, min_stage: int) -> int:
    """Remove every task of stage >= min_stage (and the drafted sections),
    so the stage can be started again from scratch. Everything produced
    downstream of the draft (QA scores, gap analysis, approval) goes too --
    it described the old draft."""
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE job_id=%s AND stage>=%s", (job_id, min_stage))
        conn.execute("DELETE FROM report_sections WHERE job_id=%s", (job_id,))
        conn.execute("DELETE FROM qa_scores WHERE job_id=%s", (job_id,))
        conn.execute(
            "UPDATE jobs SET status='pending', draft_approved_at=NULL, key_facts=NULL, research=NULL, "
            "gap_analysis=NULL, mitigation_strategy=NULL, updated_at=now() WHERE id=%s",
            (job_id,),
        )
        return cur.rowcount


def upsert_report_section(
    job_id,
    section_key: str,
    title: str,
    order_index: int,
    blocks: list[dict],
    source_task_id,
    heading_level: int = 1,
    meta: Optional[dict] = None,
) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO report_sections "
            "(job_id, section_key, title, order_index, content, source_task_id, heading_level, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id, section_key) DO UPDATE SET "
            "title=EXCLUDED.title, order_index=EXCLUDED.order_index, content=EXCLUDED.content, "
            "source_task_id=EXCLUDED.source_task_id, heading_level=EXCLUDED.heading_level, "
            "meta=EXCLUDED.meta, updated_at=now() "
            "RETURNING id",
            (job_id, section_key, title, order_index, Json(blocks), source_task_id, heading_level, Json(meta or {})),
        ).fetchone()
        return str(row[0])


def set_file_digest(file_id, digest: dict) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE dataroom_files SET digest=%s WHERE id=%s", (Json(digest), file_id))


def get_dataroom_file(file_id) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM dataroom_files WHERE id=%s", (file_id,))
            return cur.fetchone()


def set_job_key_facts(job_id, key_facts: dict) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE jobs SET key_facts=%s, updated_at=now() WHERE id=%s", (Json(key_facts), job_id)
        )


def existing_task_keys(job_id) -> dict[str, str]:
    """task_key -> task id, for every task this job already has."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT task_key, id FROM tasks WHERE job_id=%s", (job_id,))
            return {row[0]: str(row[1]) for row in cur.fetchall()}


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


# ------------------------------------------------- pages, coverage, cache --

def replace_document_pages(job_id, file_id, pages: list[dict]) -> None:
    """pages: dicts with no, label, method, text, confidence, is_signature, text_hash."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM document_pages WHERE file_id=%s", (file_id,))
            if pages:
                cur.executemany(
                    "INSERT INTO document_pages (job_id, file_id, page_no, label, method, chars, confidence, "
                    "is_signature, text_hash, text) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(job_id, file_id, p["no"], p.get("label") or "", p["method"], len(p.get("text") or ""),
                      p.get("confidence"), bool(p.get("is_signature")), p.get("text_hash"),
                      (p.get("text") or "").replace("\x00", "")) for p in pages],
                )


def job_page_hashes(job_id) -> dict[str, tuple[str, int]]:
    """text hash -> (filename, page_no) of the first page with that text in
    this job, for reading each repeated boilerplate page only once."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (p.text_hash) p.text_hash, f.original_filename, p.page_no "
                "FROM document_pages p JOIN dataroom_files f ON f.id = p.file_id "
                "WHERE p.job_id=%s AND p.text_hash IS NOT NULL AND p.chars > 200 "
                "ORDER BY p.text_hash, p.id",
                (job_id,),
            )
            return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def count_api_calls(job_id, purpose: str) -> int:
    """How many calls of one purpose this job has made (caps per data room)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM api_usage WHERE job_id=%s AND purpose=%s", (job_id, purpose))
            return cur.fetchone()[0]


def count_pages_by_method(job_id, method: str) -> int:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM document_pages WHERE job_id=%s AND method=%s", (job_id, method))
            return cur.fetchone()[0]


def list_document_pages(job_id) -> list[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT p.file_id, f.original_filename, f.source_path, p.page_no, p.label, p.method, p.text "
                "FROM document_pages p JOIN dataroom_files f ON f.id = p.file_id "
                "WHERE p.job_id=%s AND p.chars > 0 ORDER BY f.created_at, p.page_no",
                (job_id,),
            )
            return cur.fetchall()


def set_file_extraction(file_id, page_count: int, pages_extracted: int, method: str, executed: str | None) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE dataroom_files SET page_count=%s, pages_extracted=%s, extraction_method=%s, executed=%s "
            "WHERE id=%s",
            (page_count, pages_extracted, method, executed, file_id),
        )


def coverage_summary(job_id) -> dict:
    """Pages extracted vs. pages present for every file of the job, by method."""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, original_filename, source_path, page_count, pages_extracted, extraction_method, digest "
                "FROM dataroom_files WHERE job_id=%s ORDER BY created_at",
                (job_id,),
            )
            files = cur.fetchall()
            cur.execute("SELECT method, count(*) FROM document_pages WHERE job_id=%s GROUP BY method", (job_id,))
            methods = {r["method"]: r["count"] for r in cur.fetchall()}
    short, unread = [], 0
    for f in files:
        d = f.get("digest") or {}
        if d.get("duplicate_of"):
            continue
        if f["page_count"] is None:
            unread += 1
            continue
        if (f["pages_extracted"] or 0) < f["page_count"] or d.get("fallback"):
            short.append({"file": f["source_path"] or f["original_filename"], "pages_total": f["page_count"],
                          "pages_extracted": f["pages_extracted"] or 0,
                          "problem": d.get("extraction_error") or "pages not readable"})
    return {
        "files": len(files),
        "files_not_yet_read": unread,
        "pages_total": sum(f["page_count"] or 0 for f in files),
        "pages_extracted": sum(f["pages_extracted"] or 0 for f in files),
        "pages_by_method": methods,
        "short": short,
        "complete": not short and not unread,
    }


def get_extraction_cache(sha256: str, extract_version: int) -> Optional[dict]:
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM extraction_cache WHERE sha256=%s AND extract_version=%s",
                        (sha256, extract_version))
            return cur.fetchone()


def put_extraction_cache(sha256: str, extract_version: int, kind: str, page_count: int, pages: list[dict],
                         executed: str | None, card: dict | None, card_version: int | None) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO extraction_cache (sha256, extract_version, card_version, kind, page_count, pages, executed, card) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (sha256, extract_version) DO UPDATE SET card_version=EXCLUDED.card_version, "
            "kind=EXCLUDED.kind, page_count=EXCLUDED.page_count, pages=EXCLUDED.pages, executed=EXCLUDED.executed, "
            "card=EXCLUDED.card, updated_at=now()",
            (sha256, extract_version, card_version, kind, page_count, Json(pages), executed,
             Json(card) if card is not None else None),
        )
