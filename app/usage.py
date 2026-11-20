"""Per-call API usage and cost, recorded against the job and task that made
the call, so a job's real spend is visible (GET /jobs/{id}/usage, and the
total in the UI) instead of estimated.

The worker sets the current job/task before running a handler
(`task_context`); claude_client records every response through `record`.
Recording never raises -- losing a usage row must not fail a task.

Prices come from app/model_config.py (verified against Anthropic's pricing
page): cache writes 1.25x input (5-minute TTL) or 2x (1-hour), cache reads
0.1x (0.05x on Sonnet/Opus 5.5), batches half price, web search $10/1,000.
"""
import contextlib
import contextvars
import logging

log = logging.getLogger("usage")

from app import model_config

_current: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("usage_task", default=None)


@contextlib.contextmanager
def task_context(job_id, task_id):
    token = _current.set((str(job_id), str(task_id) if task_id is not None else None))
    try:
        yield
    finally:
        _current.reset(token)


def _get(obj, name, default=0):
    value = getattr(obj, name, default) if obj is not None else default
    return value if value is not None else default


def cost_of(model: str, usage, batch: bool = False) -> dict:
    """Token counts and dollar cost of one response's `usage`."""
    inp = _get(usage, "input_tokens")
    out = _get(usage, "output_tokens")
    write = _get(usage, "cache_creation_input_tokens")
    read = _get(usage, "cache_read_input_tokens")
    price_in, price_out, read_mult = model_config.price(model, inp + write + read)
    breakdown = _get(usage, "cache_creation", None)
    write_1h = _get(breakdown, "ephemeral_1h_input_tokens")
    write_5m = max(write - write_1h, 0)
    searches = _get(_get(usage, "server_tool_use", None), "web_search_requests")
    cost = (
        inp * price_in
        + out * price_out
        + write_5m * price_in * 1.25
        + write_1h * price_in * 2.0
        + read * price_in * read_mult
    ) / 1_000_000
    if batch:  # Message Batches API: 50% of standard prices
        cost *= model_config.BATCH_DISCOUNT
    cost += searches * model_config.WEB_SEARCH_USD
    return {"input_tokens": inp, "output_tokens": out, "cache_write_tokens": write,
            "cache_read_tokens": read, "web_searches": searches, "cost_usd": round(cost, 5)}


def record(purpose: str, model: str, response, batch: bool = False) -> dict:
    stats = cost_of(model, getattr(response, "usage", None), batch)
    ctx = _current.get()
    log.info("api call %s %s: in=%s out=%s cache_w=%s cache_r=%s web=%s $%.4f", purpose, model,
             stats["input_tokens"], stats["output_tokens"], stats["cache_write_tokens"],
             stats["cache_read_tokens"], stats["web_searches"], stats["cost_usd"])
    if ctx is None:
        return stats
    try:
        from app import repo
        repo.record_api_usage(job_id=ctx[0], task_id=ctx[1], purpose=purpose, model=model, batch=batch, **stats)
    except Exception:
        log.exception("could not record API usage")
    return stats


# ------------------------------------------------------------ budget guard --
# Per-report limits. The soft limit stops optional work (verification
# rewrites); the hard limit refuses any call whose estimated cost would take
# the job past it, so a run can never exceed it.
SOFT_LIMIT_USD = float(__import__("os").environ.get("JOB_SOFT_LIMIT_USD", "4.50"))
HARD_LIMIT_USD = float(__import__("os").environ.get("JOB_HARD_LIMIT_USD", "5.00"))


class BudgetExceeded(RuntimeError):
    pass


def _job_spend(job_id) -> float:
    from app import repo
    try:
        return repo.job_total_cost(job_id)
    except Exception:
        return 0.0


def over_soft_limit(job_id) -> bool:
    spent = _job_spend(job_id)
    if spent > SOFT_LIMIT_USD:
        log.warning("job %s spend $%.2f is past the soft limit $%.2f: skipping optional work", job_id, spent,
                    SOFT_LIMIT_USD)
        return True
    return False


def estimate_call(params: dict) -> float:
    """A deliberately high estimate of one call's cost: uncached input at
    full price (cached system blocks at the cache-read rate), output at up
    to 8k tokens (calls rarely use their whole max_tokens)."""
    price_in, price_out, read_mult = model_config.price(params.get("model", ""))
    chars = 0.0
    for block in params.get("system") or []:
        if isinstance(block, dict):
            chars += len(block.get("text", "")) * (read_mult if block.get("cache_control") else 1)
        else:
            chars += len(str(block))
    for m in params.get("messages") or []:
        c = m.get("content")
        chars += len(c) if isinstance(c, str) else sum(len(str(x)) for x in c or [])
    out = min(params.get("max_tokens", 4000), 8000)
    return chars / 3.5 * price_in / 1e6 + out * price_out / 1e6


def check_budget(purpose: str, params: dict) -> None:
    ctx = _current.get()
    if ctx is None or purpose.startswith("eval:"):
        return
    spent, est = _job_spend(ctx[0]), estimate_call(params)
    if spent + est > HARD_LIMIT_USD:
        raise BudgetExceeded(f"{purpose} refused: job spend ${spent:.2f} + estimated ${est:.2f} would pass the "
                             f"${HARD_LIMIT_USD:.2f} per-report limit")
