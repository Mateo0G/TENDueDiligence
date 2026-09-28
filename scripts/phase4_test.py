"""Phase 4 proof: compile the existing Phase 3 Solara Dx report_sections
into a docx via the real pipeline (Stage 3 task -> awaiting_review status),
exercise the human review checkpoint (edit a section, confirm the re-
rendered docx reflects it without re-running compile), and approve the
draft.

Needs real Postgres and R2 -- run via:
  railway run --service worker -- <venv-python> scripts/phase4_test.py
"""
import io

import docx
from fastapi.testclient import TestClient

from app import repo, storage
from app.main import app
from app.worker import run_one_task

client = TestClient(app)

JOB_ID = "70ab7c4f-015e-4f5f-be49-2f5df083731b"  # Solara Dx, from phase3_llm_test.py
OUT_PATH = (
    r"C:\Users\mateo\AppData\Local\Temp\claude\c--Users-mateo-TENCapitalProjects-TEN-Due-Diligence"
    r"\2815135e-3d1d-4e86-bce1-695aff7ff4d8\scratchpad\phase4_draft.docx"
)


def main():
    job = repo.get_job(JOB_ID)
    assert job is not None, "expected the Phase 3 Solara Dx job to still exist"
    print(f"using job {JOB_ID} ({job['company_name']}), current status={job['status']}")

    # Idempotency: a prior run of this script may have already created and
    # completed compile_draft for this job (task_key is unique per job).
    from app.db import pool as _pool

    with _pool.connection() as conn:
        conn.execute(
            "DELETE FROM tasks WHERE job_id=%s AND task_key='compile_draft'", (JOB_ID,)
        )
    print("cleared any prior compile_draft task for this job")

    resp = client.post(f"/jobs/{JOB_ID}/stage3")
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["task_id"]
    print(f"created compile_draft task {task_id}")

    ran = 0
    while run_one_task():
        ran += 1
    print(f"drained {ran} task(s)")

    task = repo.get_task(task_id)
    assert task["status"] == "completed", f"compile_draft task did not complete: {task}"

    job = repo.get_job(JOB_ID)
    assert job["status"] == "awaiting_review", f"expected awaiting_review, got {job['status']}"
    assert job["output_key"] == f"reports/{JOB_ID}/draft.docx"
    print(f"PASS -- job status is awaiting_review, output_key={job['output_key']}")

    resp = client.get(f"/jobs/{JOB_ID}/draft.docx")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    with open(OUT_PATH, "wb") as f:
        f.write(resp.content)
    print(f"PASS -- downloaded draft.docx ({len(resp.content)} bytes) to {OUT_PATH}")

    resp = client.get(f"/jobs/{JOB_ID}/sections")
    assert resp.status_code == 200
    sections = resp.json()
    assert len(sections) == 25
    print(f"PASS -- GET /sections returns all {len(sections)} sections")

    # Human review checkpoint: edit one section, confirm the re-rendered
    # docx reflects the edit without re-running the Stage 3 task.
    exec_summary = next(s for s in sections if s["section_key"] == "Executive Summary")
    edited_blocks = exec_summary["content"] + [
        {"type": "paragraph", "text": "PHASE4_REVIEW_EDIT_MARKER: reviewed and annotated by a human."}
    ]
    resp = client.patch(
        f"/jobs/{JOB_ID}/sections/Executive Summary",
        json={"title": exec_summary["title"], "blocks": edited_blocks},
    )
    assert resp.status_code == 200, resp.text
    print("PASS -- edited Executive Summary section via PATCH")

    resp = client.get(f"/jobs/{JOB_ID}/draft.docx")
    assert resp.status_code == 200
    # .docx is a zip archive -- searching resp.content directly would search
    # compressed bytes and never match; parse it properly instead.
    reopened = docx.Document(io.BytesIO(resp.content))
    full_text = "\n".join(p.text for p in reopened.paragraphs)
    assert "PHASE4_REVIEW_EDIT_MARKER" in full_text, "edit not reflected in re-rendered docx"
    print("PASS -- re-rendered docx reflects the human edit without re-running compile_draft")

    resp = client.post(f"/jobs/{JOB_ID}/approve-draft")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["draft_approved"] is True
    print(f"PASS -- draft approved, status={result['status']}")

    # Failure path: compile_draft on a job with no sections should fail
    # cleanly (missing-sections guard), not produce a broken docx.
    empty_job_id = repo.create_job("Phase4 Empty Test Co")
    resp = client.post(f"/jobs/{empty_job_id}/stage3")
    assert resp.status_code == 200
    empty_task_id = resp.json()["task_id"]
    run_one_task()
    empty_task = repo.get_task(empty_task_id)
    assert empty_task["status"] == "failed", f"expected failure on empty job, got {empty_task}"
    assert "missing sections" in empty_task["error"]
    print("PASS -- compile_draft fails cleanly on a job with no drafted sections")

    print("\nAll Phase 4 checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
