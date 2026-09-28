"""Phase 3 proof (part 2, real Claude calls): upload a synthetic-but-
substantive dataroom for a fictional company, run the full Stage 2 drafting
pipeline against the real Anthropic API, and dump every section's content
so a human can inspect actual output quality -- per the brief's explicit
Phase 3 instruction to do that before moving on.

Needs real Postgres, R2, and ANTHROPIC_API_KEY -- run via:
  railway run --service worker -- <venv-python> scripts/phase3_llm_test.py

Writes a full transcript to the scratchpad for review, and prints a short
summary + a few structural sanity checks to stdout.
"""
import os
import sys
import time

from fastapi.testclient import TestClient

from app import repo
from app.main import app
from app.worker import run_one_task

client = TestClient(app)

DATAROOM_DIR = (
    r"C:\Users\mateo\AppData\Local\Temp\claude\c--Users-mateo-TENCapitalProjects-TEN-Due-Diligence"
    r"\2815135e-3d1d-4e86-bce1-695aff7ff4d8\scratchpad\test_dataroom"
)
COMPANY_NAME = "Solara Dx"
OUTPUT_PATH = (
    r"C:\Users\mateo\AppData\Local\Temp\claude\c--Users-mateo-TENCapitalProjects-TEN-Due-Diligence"
    r"\2815135e-3d1d-4e86-bce1-695aff7ff4d8\scratchpad\phase3_output.md"
)


def main():
    resp = client.post("/jobs", json={"company_name": COMPANY_NAME})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["id"]
    print(f"created job {job_id} for {COMPANY_NAME}")

    dataroom_names = [
        f for f in os.listdir(DATAROOM_DIR) if f != "pitch_deck.txt"
    ]
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
    uploaded = resp.json()["uploaded"]
    print(f"uploaded {len(uploaded)} dataroom files:")
    for u in uploaded:
        print(f"  {u['original_filename']} -> category={u['category']}")

    with open(os.path.join(DATAROOM_DIR, "pitch_deck.txt"), "rb") as f:
        resp = client.post(
            f"/jobs/{job_id}/pitch-deck",
            files={"file": ("pitch_deck.txt", f, "text/plain")},
        )
    assert resp.status_code == 200, resp.text
    print("uploaded pitch deck")

    resp = client.post(f"/jobs/{job_id}/classify-vertical")
    assert resp.status_code == 200, resp.text
    vertical_result = resp.json()
    print(f"vertical: {vertical_result['vertical']} signals={vertical_result['signals']}")

    resp = client.post(f"/jobs/{job_id}/stage2")
    assert resp.status_code == 200, resp.text
    task_ids = resp.json()["task_ids"]
    print(f"created {len(task_ids)} Stage 2 tasks, draining...")

    start = time.monotonic()
    completed = 0
    while run_one_task():
        completed += 1
        elapsed = time.monotonic() - start
        print(f"  [{elapsed:6.1f}s] completed task {completed}/{len(task_ids)}", flush=True)

    tasks = repo.list_tasks_for_job(job_id)
    failed = [t for t in tasks if t["status"] == "failed"]
    print(f"\ndrain finished in {time.monotonic() - start:.1f}s -- {len(failed)} failed tasks")
    for t in failed:
        print(f"  FAILED {t['task_key']}: {t['error']}")

    sections = repo.list_report_sections(job_id)
    print(f"\n{len(sections)} report_sections rows persisted (expected 25)")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as out:
        out.write(f"# Phase 3 drafting output -- {COMPANY_NAME} (job {job_id})\n\n")
        for s in sections:
            out.write(f"## {s['title']}  _(section_key: {s['section_key']})_\n\n")
            for block in s["content"]:
                if block["type"] == "heading":
                    out.write(f"**{block['text']}**\n\n")
                elif block["type"] == "paragraph":
                    out.write(f"{block['text']}\n\n")
                elif block["type"] == "table":
                    out.write("| " + " | ".join(block["headers"]) + " |\n")
                    out.write("|" + "---|" * len(block["headers"]) + "\n")
                    for row in block["rows"]:
                        out.write("| " + " | ".join(row) + " |\n")
                    out.write("\n")
    print(f"\nfull transcript written to {OUTPUT_PATH}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
