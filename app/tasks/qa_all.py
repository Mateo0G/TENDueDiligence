"""Stage 4 task: every DD Assessment / Life Science / master validation
check in one Claude call (they all read the same compiled report and
evidence), persisted as one qa_scores row per check exactly as the old
per-check qa_score tasks did, so the Report Card and Stage 5 are
unchanged.
"""
from app import evidence, pipeline, repo
from app.claude_client import score_all_assessments
from app.models import QAScoreOutput
from app.qa_spec import DD_ASSESSMENT_SPECS, LIFE_SCIENCE_SPECS, MASTER_VALIDATION_SPEC
from app.tasks.registry import register

_SPEC_BY_TASK_KEY = {s.task_key: s for s in DD_ASSESSMENT_SPECS + LIFE_SCIENCE_SPECS + [MASTER_VALIDATION_SPEC]}


@register("qa_all")
def run_qa_all(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    specs = [_SPEC_BY_TASK_KEY[k] for k in payload["task_keys"]]
    final_attempt = task["attempts"] + 1 >= repo.MAX_TASK_ATTEMPTS
    report_text = pipeline.render_full_report_text(job_id)
    reason = "This check was not returned by the QA model."
    try:
        name = pipeline.company_names(job_id, payload["company_name"])[1]
        output = score_all_assessments(specs, name, report_text, evidence.qa_evidence(job_id))
        by_key = {a.task_key: a for a in output.assessments}
    except Exception as exc:
        if not final_attempt:
            raise
        by_key, reason = {}, f"The QA call failed ({str(exc)[:200]}); use Retry or restart Stage 4."
    missing = [s.task_key for s in specs if s.task_key not in by_key]
    if missing and not final_attempt:
        raise RuntimeError(f"QA output is missing checks {missing}; retrying")
    for spec in specs:
        a = by_key.get(spec.task_key)
        result = (QAScoreOutput(score=a.score, summary=a.summary, findings=a.findings) if a else
                  QAScoreOutput(score=None, summary=reason, findings=[]))
        pipeline.persist_qa_output(job_id, spec, result, task["id"])
    return {"checks": len(specs), "missing": missing}
