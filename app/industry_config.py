"""Seed lists for web research, by industry (config/industry_candidates.yaml):
the competitors, acquirers and comparable exits the Team expects a report
in that space to cover. Research starts from these plus the companies the
data room itself names, then verifies and extends them -- so major players
aren't left out in favour of whatever a few generic searches return.

An industry applies when enough of its keywords appear in the company's
own materials (pitch deck and document summaries).
"""
import functools
import re

import yaml

CONFIG_PATH = "config/industry_candidates.yaml"


@functools.lru_cache(maxsize=1)
def industries() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def detect(text: str) -> list[dict]:
    """Industries whose keywords appear often enough in `text`, best first."""
    low = text.lower()
    found = []
    for key, ind in industries().items():
        hits = sum(1 for k in ind.get("keywords", []) if re.search(re.escape(str(k).lower()), low))
        if hits >= ind.get("min_hits", 3):
            found.append({"key": key, "hits": hits, "competitors": [], "acquirers": [], "exits": [], **ind})
    return sorted(found, key=lambda d: -d["hits"])
