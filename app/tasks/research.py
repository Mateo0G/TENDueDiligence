"""Stage 2 task: one web-research memo per job (competitors, comparable
transactions, potential acquirers, market and reimbursement data) that
every drafting call reads from the shared prefix. Replaces web search
inside seven separate drafting calls, each of which paid for its own
searches and re-read their results. Never blocks drafting: on its last
attempt a failure stores an error marker and the report says public data
could not be gathered.
"""
from app import claude_client, evidence, pipeline, repo
from app.tasks.registry import register


@register("research")
def run_research(task: dict) -> dict:
    payload = task["payload"]
    job_id = payload["job_id"]
    try:
        name = pipeline.company_names(job_id, payload["company_name"])[1]
        result = claude_client.research_market(name, evidence.research_context(job_id))
    except Exception as exc:
        if task["attempts"] + 1 < repo.MAX_TASK_ATTEMPTS:
            raise
        repo.set_job_research(job_id, {"error": str(exc)[:500]})
        return {"fallback": True}
    repo.set_job_research(job_id, result)
    return {"memo_chars": len(result["memo"]), "sources": len(result["sources"]),
            "searches": result.get("searches"), "search_errors": result.get("search_errors"),
            "from_cache": result.get("from_cache")}
