"""Stage 6: fold the Stage 4 QA report card and Stage 5 gap analysis /
mitigation content into the document and re-render the final docx, per the
brief's Stage 6 description. Reuses jobs.output_key (the same key Stage 3's
compile_draft wrote to) -- the final docx simply overwrites the draft.
"""
from app import repo, storage
from app.docx_builder import build_final_report_docx
from app.tasks.registry import register


@register("final_render")
def run_final_render(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    company_name = payload["company_name"]

    job = repo.get_job(job_id)
    sections = repo.list_report_sections(job_id)
    qa_scores = repo.list_qa_scores(job_id)

    docx_bytes = build_final_report_docx(
        company_name=company_name,
        report_sections=sections,
        qa_scores=qa_scores,
        gap_analysis=job["gap_analysis"],
        mitigation_strategy=job["mitigation_strategy"],
    )

    object_key = job["output_key"] or f"reports/{job_id}/draft.docx"
    storage.put_bytes(
        object_key,
        docx_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    repo.set_job_output_key(job_id, object_key)
    repo.set_job_status(job_id, "completed")

    return {"output_key": object_key, "section_count": len(sections), "qa_score_count": len(qa_scores)}
