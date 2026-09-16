"""Phase 1 stand-in handlers: prove the DAG execution order works before any
node does real work (document parsing, Claude calls, docx rendering, ...).
"""
from app.tasks.registry import register


@register("noop")
def run_noop(payload: dict) -> dict:
    return {"echo": payload}


@register("noop_fail")
def run_noop_fail(payload: dict) -> dict:
    raise RuntimeError(payload.get("message", "simulated failure"))
