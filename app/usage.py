"""Per-call API usage and cost, recorded against the job and task that made
the call, so a job's real spend is visible (GET /jobs/{id}/usage, and the
total in the UI) instead of estimated.

The worker sets the current job/task before running a handler
(`task_context`); claude_client records every response through `record`.
Recording never raises -- losing a usage row must not fail a task.

Prices are USD per million tokens (standard API list prices). Cache writes
cost 1.25x input for the 5-minute TTL and 2x for the 1-hour TTL; cache
reads cost 0.1x input; web search is $10 per 1,000 searches.
"""
import contextlib
import contextvars
import logging

log = logging.getLogger("usage")

PRICES = {  # model prefix -> (input, output) $/MTok
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4-5": (5.00, 25.00),
}
DEFAULT_PRICE = (3.00, 15.00)
WEB_SEARCH_USD = 10.00 / 1000

_current: contextvars.ContextVar[tuple | None] = contextvars.ContextVar("usage_task", default=None)


@contextlib.contextmanager
def task_context(job_id, task_id):
    token = _current.set((str(job_id), str(task_id) if task_id is not None else None))
    try:
        yield
    finally:
        _current.reset(token)


def _price(model: str) -> tuple[float, float]:
    for prefix, price in PRICES.items():
        if model.startswith(prefix):
            return price
    return DEFAULT_PRICE


def _get(obj, name, default=0):
    value = getattr(obj, name, default) if obj is not None else default
    return value if value is not None else default


def cost_of(model: str, usage) -> dict:
    """Token counts and dollar cost of one response's `usage`."""
    price_in, price_out = _price(model)
    inp = _get(usage, "input_tokens")
    out = _get(usage, "output_tokens")
    write = _get(usage, "cache_creation_input_tokens")
    read = _get(usage, "cache_read_input_tokens")
    breakdown = _get(usage, "cache_creation", None)
    write_1h = _get(breakdown, "ephemeral_1h_input_tokens")
    write_5m = max(write - write_1h, 0)
    searches = _get(_get(usage, "server_tool_use", None), "web_search_requests")
    cost = (
        inp * price_in
        + out * price_out
        + write_5m * price_in * 1.25
        + write_1h * price_in * 2.0
        + read * price_in * 0.1
    ) / 1_000_000 + searches * WEB_SEARCH_USD
    return {"input_tokens": inp, "output_tokens": out, "cache_write_tokens": write,
            "cache_read_tokens": read, "web_searches": searches, "cost_usd": round(cost, 5)}


def record(purpose: str, model: str, response) -> dict:
    stats = cost_of(model, getattr(response, "usage", None))
    ctx = _current.get()
    log.info("api call %s %s: in=%s out=%s cache_w=%s cache_r=%s web=%s $%.4f", purpose, model,
             stats["input_tokens"], stats["output_tokens"], stats["cache_write_tokens"],
             stats["cache_read_tokens"], stats["web_searches"], stats["cost_usd"])
    if ctx is None:
        return stats
    try:
        from app import repo
        repo.record_api_usage(job_id=ctx[0], task_id=ctx[1], purpose=purpose, model=model, **stats)
    except Exception:
        log.exception("could not record API usage")
    return stats
