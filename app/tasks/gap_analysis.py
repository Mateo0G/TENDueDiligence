"""Stage 5, step 1: Gap Analysis. Reads the compiled report + Stage 4 QA
scores, per docs/prompt_inventory.md row 89.
"""
from app import pipeline, repo
from app.claude_client import analyze_gaps
from app.tasks.registry import register


@register("gap_analysis")
def run_gap_analysis(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    company_name = payload["company_name"]

    report_text = pipeline.render_full_report_text(job_id)
    qa_summary_text = pipeline.render_qa_scores_text(job_id)

    output = analyze_gaps(company_name, report_text, qa_summary_text)

    repo.set_job_gap_analysis(job_id, output.model_dump())

    return {"gap_count": len(output.gaps)}
