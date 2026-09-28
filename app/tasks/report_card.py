"""Stage 4 task handler: Report Card of scores. Deliberately deterministic,
no Claude call -- its own source prompt ("Display a summary report card
showing all scores") has nothing left to reason about once every
individual DD Assessment / Life Science score already exists in
qa_scores; a second LLM call would only risk transcribing numbers
incorrectly. See app/qa_spec.py's module docstring for the full reasoning
behind this and the other Stage 4 design choices (Flags A2/A3/A4).
"""
from app import repo
from app.qa_spec import REPORT_CARD_ASSESSMENT_NAME
from app.tasks.registry import register


@register("report_card")
def run_report_card(task: dict) -> dict:
    job_id = task["payload"]["job_id"]

    scores = repo.list_qa_scores(job_id)
    # Exclude the master validation check (different 1-10 scale, kept
    # separate per Flag M1) and any prior Report Card row from a re-run.
    scoreable = [
        s for s in scores
        if s["block"] in ("dd_assessment", "life_science_assessment")
        and s["assessment_name"] != REPORT_CARD_ASSESSMENT_NAME
    ]

    numeric_scores = [float(s["score"]) for s in scoreable if s["score"] is not None]
    overall_average = round(sum(numeric_scores) / len(numeric_scores), 1) if numeric_scores else None

    entries = [
        {
            "block": s["block"],
            "assessment_name": s["assessment_name"],
            "scale": s["scale"],
            "score": float(s["score"]) if s["score"] is not None else None,
            "summary": s["findings"].get("summary") if s["findings"] else None,
        }
        for s in scoreable
    ]

    repo.upsert_qa_score(
        job_id=job_id,
        block="dd_assessment",
        assessment_name=REPORT_CARD_ASSESSMENT_NAME,
        scale="0-100 (average)",
        score=overall_average,
        findings={"entries": entries, "overall_average": overall_average},
        source_task_id=task["id"],
    )

    return {"overall_average": overall_average, "entry_count": len(entries)}
