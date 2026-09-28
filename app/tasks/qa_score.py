"""Stage 4 task handler: run one DD Assessment / Life Science Assessment /
master validation check against the compiled report + dataroom, per
app/qa_spec.py. Input-gathering and persistence are shared with
app/batch_runner.py's Batch API path -- see app/pipeline.py.
"""
from app import pipeline
from app.claude_client import score_assessment
from app.qa_spec import (
    DD_ASSESSMENT_SPECS,
    LIFE_SCIENCE_SPECS,
    MASTER_VALIDATION_SPEC,
)
from app.tasks.registry import register

_SPEC_BY_TASK_KEY = {
    s.task_key: s for s in DD_ASSESSMENT_SPECS + LIFE_SCIENCE_SPECS + [MASTER_VALIDATION_SPEC]
}


@register("qa_score")
def run_qa_score(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    company_name = payload["company_name"]
    spec = _SPEC_BY_TASK_KEY[payload["task_key"]]

    report_text = pipeline.render_full_report_text(job_id)
    dataroom_text = pipeline.full_dataroom_text(job_id)

    output = score_assessment(spec, company_name, report_text, dataroom_text)
    pipeline.persist_qa_output(job_id, spec, output, task["id"])

    return {"assessment_name": spec.assessment_name, "score": output.score}
