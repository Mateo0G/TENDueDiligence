"""Phase 2 proof: upload a synthetic dataroom + pitch deck through the real
FastAPI app, confirm deterministic file categorization and vertical
classification land correctly, and confirm the bytes actually made it to
R2 (not just the DB rows).

Needs real Postgres (via the dev tunnel, already in .env) and real R2
credentials -- run via:
  railway run --service worker -- <venv-python> scripts/phase2_test.py
"""
from fastapi.testclient import TestClient

from app import storage
from app.main import app

client = TestClient(app)

# (filename, expected_category) -- content bytes don't matter, only names,
# since Phase 2 classification is filename/path based per the brief.
DATAROOM_FILES = [
    ("cap_table_2026.xlsx", "financial"),
    ("certificate_of_incorporation.pdf", "legal_corporate"),
    ("patent_US123456.pdf", "ip"),
    ("spencer_madsen_resume.pdf", "hr"),
    ("sales_pipeline_q3.xlsx", "sales_marketing"),
    ("clinical_trial_protocol.pdf", "science_tech"),
    ("vendor_supply_agreement.pdf", "contracts"),
    ("fda_510k_clearance_letter.pdf", "regulatory"),
    ("random_notes.txt", None),
]
PITCH_DECK_FILENAME = "accubreath_pitch_deck.pdf"


def main():
    resp = client.post("/jobs", json={"company_name": "Phase2 Test Co"})
    assert resp.status_code == 200, resp.text
    job = resp.json()
    job_id = job["id"]
    print(f"created job {job_id}")

    files_payload = [
        ("files", (name, f"content of {name}".encode(), "application/octet-stream"))
        for name, _ in DATAROOM_FILES
    ]
    resp = client.post(f"/jobs/{job_id}/dataroom", files=files_payload)
    assert resp.status_code == 200, resp.text
    uploaded = resp.json()["uploaded"]
    by_name = {u["original_filename"]: u for u in uploaded}

    for name, expected_category in DATAROOM_FILES:
        actual = by_name[name]["category"]
        assert actual == expected_category, (
            f"{name}: expected category {expected_category!r}, got {actual!r}"
        )
    print(f"PASS -- all {len(DATAROOM_FILES)} dataroom files categorized correctly")

    resp = client.post(
        f"/jobs/{job_id}/pitch-deck",
        files={"file": (PITCH_DECK_FILENAME, b"pitch deck bytes", "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    print("PASS -- pitch deck uploaded")

    resp = client.post(f"/jobs/{job_id}/classify-vertical")
    assert resp.status_code == 200, resp.text
    vertical_result = resp.json()
    assert vertical_result["vertical"] == "life_sciences", (
        f"expected life_sciences vertical, got {vertical_result}"
    )
    print(f"PASS -- vertical classified as life_sciences: {vertical_result['signals']}")

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    job_detail = resp.json()
    assert job_detail["vertical"] == "life_sciences"
    assert len(job_detail["dataroom_files"]) == len(DATAROOM_FILES) + 1  # + pitch deck
    print("PASS -- GET /jobs/{id} reflects uploads and vertical")

    # Confirm bytes actually landed in R2, not just DB rows, then clean up.
    all_keys = storage.list_prefix(f"dataroom/{job_id}/")
    assert len(all_keys) == len(DATAROOM_FILES), f"expected {len(DATAROOM_FILES)} objects in R2, found {len(all_keys)}"
    sample_key = next(k for k in all_keys if "cap_table_2026.xlsx" in k)
    content = storage.get_bytes(sample_key)
    assert content == b"content of cap_table_2026.xlsx"
    print(f"PASS -- {len(all_keys)} dataroom objects confirmed present in R2 with correct content")

    pitch_keys = storage.list_prefix(f"pitch-deck/{job_id}/")
    assert len(pitch_keys) == 1

    for key in all_keys + pitch_keys:
        storage.delete(key)
    print("cleaned up test objects from R2")

    print("\nAll Phase 2 checks passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        from app.db import pool

        pool.close()
