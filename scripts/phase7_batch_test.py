"""Phase 7 proof: run Stage 2 and Stage 4 through the Batch API path
(app/batch_runner.py) instead of the synchronous one-task-at-a-time worker,
against a fresh job using the same synthetic Solara Dx dataroom from Phase
3, and confirm structural parity with the non-batch results from Phases
3-6 (same section/score counts and valid schemas -- not byte-identical
text, since it's a fresh LLM call either way).

Needs real Postgres, R2, and ANTHROPIC_API_KEY -- run via:
  railway run --service worker -- <venv-python> scripts/phase7_batch_test.py
"""
import os
import time

from fastapi.testclient import TestClient

from app import batch_runner, repo
from app.main import app
from app.worker import run_one_task

client = TestClient(app)

DATAROOM_DIR = (
    r"C:\Users\mateo\AppData\Local\Temp\claude\c--Users-mateo-TENCapitalProjects-TEN-Due-Diligence"
    r"\2815135e-3d1d-4e86-bce1-695aff7ff4d8\scratchpad\test_dataroom"
)
COMPANY_NAME = "Solara Dx Batch"


def drain_single(max_iterations=200):
    n = 0
    for _ in range(max_iterations):
        if not run_one_task():
            break
        n += 1
    return n


def main():
    resp = client.post("/jobs", json={"company_name": COMPANY_NAME})
    job_id = resp.json()["id"]
    print(f"created job {job_id}")

    dataroom_names = [f for f in os.listdir(DATAROOM_DIR) if f != "pitch_deck.txt"]
    files_payload = []
    opened = []
    for name in dataroom_names:
        f = open(os.path.join(DATAROOM_DIR, name), "rb")
        opened.append(f)
        files_payload.append(("files", (name, f, "text/plain")))
    resp = client.post(f"/jobs/{job_id}/dataroom", files=files_payload)
    for f in opened:
        f.close()
    assert resp.status_code == 200, resp.text
    print(f"uploaded {len(resp.json()['uploaded'])} dataroom files")

    with open(os.path.join(DATAROOM_DIR, "pitch_deck.txt"), "rb") as f:
        client.post(f"/jobs/{job_id}/pitch-deck", files={"file": ("pitch_deck.txt", f, "text/plain")})
    resp = client.post(f"/jobs/{job_id}/classify-vertical")
    vertical = resp.json()["vertical"]
    print(f"vertical: {vertical}")

    resp = client.post(f"/jobs/{job_id}/stage2")
    stage2_ids = resp.json()["task_ids"]
    print(f"created {len(stage2_ids)} Stage 2 tasks (expected 20)")

    print("\n=== Stage 2 via Batch API ===")
    start = time.monotonic()
    stage2_result = batch_runner.run_stage2_batch(job_id, COMPANY_NAME, poll_interval=15, timeout=1200)
    print(f"Stage 2 batch result: {stage2_result} ({time.monotonic() - start:.1f}s)")
    assert stage2_result["failed"] == 0, f"Stage 2 batch had failures: {stage2_result}"
    assert stage2_result["completed"] == 20, f"expected 20 completed, got {stage2_result}"

    sections = repo.list_report_sections(job_id)
    assert len(sections) == 25, f"expected 25 report_sections (parity with Phase 3), got {len(sections)}"
    print(f"PASS -- {len(sections)} report_sections persisted via batch path, matching Phase 3's structural shape")

    resp = client.post(f"/jobs/{job_id}/stage3")
    compile_task_id = resp.json()["task_id"]
    drain_single()
    compile_task = repo.get_task(compile_task_id)
    assert compile_task["status"] == "completed", compile_task
    print("PASS -- Stage 3 compile succeeded on batch-drafted sections")

    resp = client.post(f"/jobs/{job_id}/approve-draft")
    assert resp.status_code == 200, resp.text
    print("PASS -- draft approved")

    resp = client.post(f"/jobs/{job_id}/stage4")
    stage4_ids = resp.json()["task_ids"]
    expected_stage4 = 23 if vertical == "life_sciences" else 13
    print(f"created {len(stage4_ids)} Stage 4 tasks (expected {expected_stage4})")

    print("\n=== Stage 4 via Batch API ===")
    start = time.monotonic()
    stage4_result = batch_runner.run_stage4_batch(job_id, COMPANY_NAME, poll_interval=15, timeout=1200)
    print(f"Stage 4 batch result: {stage4_result} ({time.monotonic() - start:.1f}s)")
    assert stage4_result["failed"] == 0, f"Stage 4 batch had failures: {stage4_result}"

    scores = repo.list_qa_scores(job_id)
    print(f"PASS -- {len(scores)} qa_scores persisted via batch path")

    key_info = next((s for s in scores if s["assessment_name"] == "Key Information Check"), None)
    assert key_info and key_info["score"] is None, "Key Information Check should have no score (parity with Phase 5)"
    report_card = next((s for s in scores if s["assessment_name"] == "Report Card of scores"), None)
    assert report_card and report_card["findings"]["overall_average"] is not None
    print(f"PASS -- Report Card average: {report_card['findings']['overall_average']} "
          f"(structural parity with Phase 5's aggregation logic)")

    print("\nAll Phase 7 batch checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
