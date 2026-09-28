"""Phase 5 proof: run the real Stage 4 QA/scoring pipeline against the
already-approved Solara Dx draft from Phases 3/4 (a life_sciences job, so
this exercises the DD Assessment block, the Life Science block, and master
validation all in one run), plus the approval gate's failure path.

Needs real Postgres, R2, and ANTHROPIC_API_KEY -- run via:
  railway run --service worker -- <venv-python> scripts/phase5_test.py
"""
import time

from fastapi.testclient import TestClient

from app import repo
from app.main import app
from app.worker import run_one_task

client = TestClient(app)

JOB_ID = "70ab7c4f-015e-4f5f-be49-2f5df083731b"  # Solara Dx, approved in phase4_test.py
OUTPUT_PATH = (
    r"C:\Users\mateo\AppData\Local\Temp\claude\c--Users-mateo-TENCapitalProjects-TEN-Due-Diligence"
    r"\2815135e-3d1d-4e86-bce1-695aff7ff4d8\scratchpad\phase5_qa_output.md"
)


def main():
    job = repo.get_job(JOB_ID)
    assert job is not None
    assert job["draft_approved_at"] is not None, "expected Solara Dx draft to already be approved"
    assert job["vertical"] == "life_sciences"
    print(f"using job {JOB_ID} ({job['company_name']}), vertical={job['vertical']}")

    # Gate check: an unapproved job must be refused.
    unapproved_job_id = repo.create_job("Phase5 Unapproved Test Co")
    resp = client.post(f"/jobs/{unapproved_job_id}/stage4")
    assert resp.status_code == 409, resp.text
    print("PASS -- stage4 refuses a job whose draft was never approved (409)")

    # Idempotency for repeat runs of this script.
    with __import__("app.db", fromlist=["pool"]).pool.connection() as conn:
        conn.execute("DELETE FROM tasks WHERE job_id=%s AND stage=4", (JOB_ID,))
        conn.execute("DELETE FROM qa_scores WHERE job_id=%s", (JOB_ID,))

    resp = client.post(f"/jobs/{JOB_ID}/stage4")
    assert resp.status_code == 200, resp.text
    task_ids = resp.json()["task_ids"]
    print(f"created {len(task_ids)} Stage 4 tasks (expected 23 for a life_sciences job)")

    start = time.monotonic()
    completed = 0
    while run_one_task():
        completed += 1
        print(f"  [{time.monotonic() - start:6.1f}s] completed task {completed}/{len(task_ids)}", flush=True)

    tasks = repo.list_tasks_for_job(JOB_ID)
    stage4_tasks = [t for t in tasks if t["stage"] == 4]
    failed = [t for t in stage4_tasks if t["status"] == "failed"]
    print(f"\ndrain finished in {time.monotonic() - start:.1f}s -- {len(failed)} failed tasks")
    for t in failed:
        print(f"  FAILED {t['task_key']}: {t['error']}")
    assert not failed, "expected all Stage 4 tasks to complete"

    scores = repo.list_qa_scores(JOB_ID)
    print(f"\n{len(scores)} qa_scores rows persisted")

    by_block = {}
    for s in scores:
        by_block.setdefault(s["block"], []).append(s)
    for block, rows in by_block.items():
        print(f"  {block}: {len(rows)} rows")

    key_info = next(s for s in scores if s["assessment_name"] == "Key Information Check")
    assert key_info["score"] is None, f"expected Key Information Check to have no score, got {key_info['score']}"
    print("PASS -- Key Information Check correctly has no numeric score")

    report_card = next(s for s in scores if s["assessment_name"] == "Report Card of scores")
    assert report_card["findings"]["overall_average"] is not None
    print(f"PASS -- Report Card overall average: {report_card['findings']['overall_average']}")
    print(f"        Report Card entries: {len(report_card['findings']['entries'])}")

    master = next(s for s in scores if s["block"] == "master_validation")
    assert master["scale"] == "1-10"
    print(f"PASS -- Master Validation scored {master['score']} on its own 1-10 scale (kept separate from Report Card)")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as out:
        out.write(f"# Phase 5 QA output -- Solara Dx (job {JOB_ID})\n\n")
        for s in scores:
            out.write(f"## {s['assessment_name']}  _(block: {s['block']}, scale: {s['scale']})_\n\n")
            out.write(f"**Score: {s['score']}**\n\n")
            findings = s["findings"] or {}
            if "summary" in findings:
                out.write(f"{findings['summary']}\n\n")
            if findings.get("findings"):
                for f in findings["findings"]:
                    out.write(f"- {f}\n")
                out.write("\n")
            if "entries" in findings:
                for e in findings["entries"]:
                    out.write(f"- {e['assessment_name']}: {e['score']}\n")
                out.write("\n")
    print(f"\nfull transcript written to {OUTPUT_PATH}")

    print("\nAll Phase 5 checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
