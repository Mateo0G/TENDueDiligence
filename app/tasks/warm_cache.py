"""Stage 2 task: freeze the job's shared drafting prefix (Key Facts +
research memo + evidence pack) in storage, and write it to the prompt
cache once before the drafting wave starts.

A cache entry only exists once a request that wrote it has started
responding, so 21 drafting calls launched together would otherwise each
pay to write the same ~70k-token prefix. With this, they all read it at a
tenth of the input price. Never blocks drafting: if the warm-up call
fails, drafting still works (the first calls just write the cache
themselves).
"""
from app import claude_client, evidence, repo
from app.tasks.registry import register


@register("warm_cache")
def run_warm_cache(task: dict) -> dict:
    job_id = task["payload"]["job_id"]
    context = evidence.store_drafting_context(job_id)
    try:
        claude_client.warm_drafting_cache(context)
    except Exception as exc:
        if task["attempts"] + 1 < repo.MAX_TASK_ATTEMPTS:
            raise
        return {"context_chars": len(context), "warmed": False, "error": str(exc)[:300]}
    return {"context_chars": len(context), "warmed": True}
