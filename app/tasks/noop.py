"""Phase 1 stand-in handlers: prove the DAG execution order works before any
node does real work (document parsing, Claude calls, docx rendering, ...).
"""
from app.tasks.registry import register


@register("noop")
def run_noop(task: dict) -> dict:
    return {"echo": task["payload"]}


@register("noop_fail")
def run_noop_fail(task: dict) -> dict:
    raise RuntimeError(task["payload"].get("message", "simulated failure"))
