"""Model IDs and prices in one place.

Prices: USD per million tokens, from Anthropic's pricing page
(platform.claude.com/docs/en/about-claude/pricing), checked 2026-11-15.
Update this file when prices change -- app/usage.py computes every call's
recorded cost from it.

- Cache write (5-minute TTL) = 1.25x input; (1-hour TTL) = 2x input.
- Cache read = 0.1x input, except Sonnet 5.5 / Opus 5.5 at 0.05x.
- Message Batches API = 50% of everything.
- Haiku 5.5 is priced by prompt length: a request whose prompt (all input
  tokens, cached or not) is over 100,000 tokens pays the higher row.
- Web search: $10 per 1,000 searches.
"""
import os

# --------------------------------------------------------------- routing --
# Sonnet tier writes the report; Haiku tier reads, extracts and checks.
DRAFTING_MODEL = os.environ.get("DRAFTING_MODEL", "claude-sonnet-5")
KEY_FACTS_MODEL = os.environ.get("KEY_FACTS_MODEL", DRAFTING_MODEL)
# QA scores the finished report for the Report Card appendix; on Haiku 5.5
# it costs ~$0.09 instead of $0.41 (decided 2026-11-20). QA_MODEL overrides.
QA_MODEL = os.environ.get("QA_MODEL", "claude-haiku-5-5")
GAP_MITIGATION_MODEL = os.environ.get("GAP_MITIGATION_MODEL", DRAFTING_MODEL)
# Haiku 5.5 is the current Haiku tier: $0.10/$0.50 per MTok for prompts up
# to 100k tokens, 10x cheaper than Haiku 4.5. Measured on the CARI room
# (2.34M input / 515k output tokens of fact cards): Haiku 4.5 $4.92
# standard / $2.46 batched; Haiku 5.5 $0.49 / ~$0.25. Reading calls stay
# well under the 100k-token tier boundary (~25k tokens each).
READ_MODEL = os.environ.get("READ_MODEL", "claude-haiku-5-5")
RESEARCH_MODEL = os.environ.get("RESEARCH_MODEL", "claude-haiku-5-5")


# ---------------------------------------------------------------- prices --
# model prefix -> (input, output, cache_read_multiplier); longest prefix wins.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.1),          # prompts <= 100k tokens
    "claude-haiku-4-5": (1.00, 5.00, 0.1),
    "claude-sonnet-5-5": (2.00, 10.00, 0.05),
    "claude-sonnet-5": (2.00, 10.00, 0.1),
    "claude-sonnet-4": (3.00, 15.00, 0.1),
    "claude-opus-5-5": (4.00, 20.00, 0.05),
    "claude-opus-5": (5.00, 25.00, 0.1),
    "claude-opus-4": (5.00, 25.00, 0.1),
    "claude-fable-5-2": (8.00, 40.00, 0.1),
}
LONG_PROMPT_PRICES = {  # model prefix -> (threshold tokens, input, output)
    "claude-haiku-5-5": (100_000, 0.50, 2.50),
}
DEFAULT_PRICE = (2.00, 10.00, 0.1)
WEB_SEARCH_USD = 10.00 / 1000
BATCH_DISCOUNT = 0.5


def price(model: str, prompt_tokens: int = 0) -> tuple[float, float, float]:
    """(input $/MTok, output $/MTok, cache-read multiplier) for a model and
    the request's total prompt size."""
    for prefix in sorted(LONG_PROMPT_PRICES, key=len, reverse=True):
        if model.startswith(prefix) and prompt_tokens > LONG_PROMPT_PRICES[prefix][0]:
            _, inp, out = LONG_PROMPT_PRICES[prefix]
            return inp, out, PRICES[prefix][2]
    for prefix in sorted(PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICES[prefix]
    return DEFAULT_PRICE


def supports_effort(model: str) -> bool:
    return not model.startswith("claude-haiku-4")


def no_thinking(model: str) -> dict:
    """Request params that turn off adaptive thinking where it is on by
    default (Haiku 5.5, Sonnet 5): extraction and checking calls don't need
    hidden reasoning, and it is billed as output."""
    if model.startswith(("claude-haiku-5", "claude-sonnet-5", "claude-opus-5")) and supports_disabled_thinking(model):
        return {"thinking": {"type": "disabled"}}
    return {}


def supports_disabled_thinking(model: str) -> bool:
    """Sonnet 5.5 / Opus 5.5 reject thinking {type: disabled} (400)."""
    return not model.startswith(("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4"))
