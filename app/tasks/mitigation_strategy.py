"""Stage 5, step 2: Mitigation Strategy. Reads the Gap Analysis output (not
the raw report again), per docs/prompt_inventory.md row 91.
"""
from app import repo
from app.claude_client import generate_mitigations
from app.models import GapAnalysisOutput
from app.tasks.registry import register


@register("mitigation_strategy")
def run_mitigation_strategy(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    company_name = payload["company_name"]

    job = repo.get_job(job_id)
    if not job["gap_analysis"]:
        raise RuntimeError("cannot generate mitigations: gap_analysis has not run yet")
    gap_analysis = GapAnalysisOutput.model_validate(job["gap_analysis"])

    output = generate_mitigations(company_name, gap_analysis)

    repo.set_job_mitigation_strategy(job_id, output.model_dump())

    return {"mitigation_count": len(output.mitigations)}
