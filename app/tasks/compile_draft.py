"""Stage 3 task handler: assemble the compiled draft docx from Stage 2's
report_sections, upload it, and move the job into the human review
checkpoint (awaiting_review).

The actual docx bytes are also re-renderable on demand (GET
/jobs/{id}/draft.docx in app/main.py) from whatever report_sections
currently holds, so edits made during human review are reflected
immediately without re-running this task.
"""
from app import repo, storage
from app.docx_builder import build_report_docx
from app.sections_spec import section_defs
from app.tasks.registry import register


@register("compile_draft")
def run_compile_draft(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    company_name = payload["company_name"]

    expected_keys = {d.section_key for d in section_defs(company_name)}
    sections = repo.list_report_sections(job_id)
    missing = expected_keys - {s["section_key"] for s in sections}
    if missing:
        raise RuntimeError(f"cannot compile: missing sections {sorted(missing)}")

    job = repo.get_job(job_id)
    logo_bytes = None
    if job.get("logo_key"):
        try:
            logo_bytes = storage.get_bytes(job["logo_key"])
        except Exception:
            logo_bytes = None

    docx_bytes = build_report_docx(company_name, sections, logo_bytes=logo_bytes)
    object_key = f"reports/{job_id}/draft.docx"
    storage.put_bytes(
        object_key,
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    repo.set_job_output_key(job_id, object_key)
    repo.set_job_status(job_id, "awaiting_review")

    return {"output_key": object_key, "section_count": len(sections)}
