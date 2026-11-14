"""Stage 2 task: build the job's Key Facts sheet from the evidence pack (the
digest of every unique document), so all drafting calls state the same
round size, valuation, dates, people and product description, and resolve
conflicting sources the same way. Never blocks drafting: on its last
attempt a failure stores an error marker and drafting proceeds from the
evidence pack directly.
"""
from app import claude_client, evidence, repo
from app.tasks.registry import register


@register("key_facts")
def run_key_facts(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    try:
        result = claude_client.extract_key_facts(payload["company_name"], evidence.build_pack(job_id))
    except Exception as exc:
        if task["attempts"] + 1 < repo.MAX_TASK_ATTEMPTS:
            raise
        repo.set_job_key_facts(job_id, {"error": str(exc)[:500]})
        return {"fallback": True}
    repo.set_job_key_facts(job_id, result.model_dump())
    return {"facts": len(result.facts), "conflicts": len(result.conflicts),
            "missing_documents": len(result.missing_documents)}
