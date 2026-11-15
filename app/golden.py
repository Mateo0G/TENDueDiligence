"""Golden-facts regression check for a generated report (tests/golden/*.yaml).

Scores one job's drafted report against facts from a client-approved
reference report:
- each fact passes on a case-insensitive regex match in the report(s) it
  belongs to; facts that don't string-match are graded by ONE batched
  Haiku call against those reports' text;
- forbidden text (test-name leakage, "search cap" wording) fails;
- a sentence naming a document that IS in the data room together with an
  absence phrase ("not in the data room", "could not be confirmed", ...) is a
  false "missing" claim;
- page coverage of every file, from the reading step's digests.

The Haiku call is recorded in api_usage with purpose "eval:golden", which
the cost report keeps separate from the report's own cost.
"""
import functools
import re

import yaml

from app import repo, usage

GOLDEN_DIR = "tests/golden"
MAX_REPORT_CHARS_FOR_GRADING = 60_000


@functools.lru_cache(maxsize=8)
def load(name: str) -> dict:
    with open(f"{GOLDEN_DIR}/{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _plain(blocks: list[dict]) -> str:
    from app.pipeline import render_blocks_as_text
    return render_blocks_as_text(blocks)


def report_texts(job_id) -> dict[str, str]:
    """report key -> that report's full text (H1 opening + every subsection)."""
    out: dict[str, list[str]] = {}
    for row in repo.list_report_sections(job_id):
        key = row["section_key"].split(".", 1)[0]
        out.setdefault(key, []).append(f"{row['title']}\n{_plain(row['content'])}")
    return {k: "\n\n".join(v) for k, v in out.items()}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;!?])\s+|\n+", text) if s.strip()]


def _coverage(job_id) -> dict:
    files = repo.list_dataroom_files(job_id)
    short = []
    for f in files:
        d = f.get("digest") or {}
        total = d.get("pages_total") or f.get("page_count")
        done = d.get("pages_extracted") or d.get("pages_read") or f.get("pages_extracted")
        if d.get("fallback"):
            short.append({"file": f["original_filename"], "problem": "unreadable"})
        elif total and done is not None and done < total:
            short.append({"file": f["original_filename"], "problem": f"{done} of {total} pages"})
    return {"files": len(files), "short": short, "complete": not short}


def evaluate(job_id, name: str = "cari_health", grade_with_llm: bool = True) -> dict:
    gold = load(name)
    texts = report_texts(job_id)
    everything = "\n\n".join(texts.values())

    results, ungraded = [], []
    for fact in gold["facts"]:
        reports = fact["report"]
        scope = "\n\n".join(texts.get(r, "") for r in reports)
        ok = bool(scope) and all(re.search(p, scope, re.I) for p in fact["all"])
        elsewhere = (not ok) and all(re.search(p, everything, re.I) for p in fact["all"])
        r = {"id": fact["id"], "report": reports, "claim": fact["claim"],
             "pass": ok, "method": "string" if ok else None, "found_elsewhere": elsewhere}
        results.append(r)
        if not ok and grade_with_llm and scope:
            ungraded.append((r, scope))

    if ungraded:
        from app import claude_client
        verdicts = claude_client.grade_golden_facts([
            (r["id"], r["claim"], scope[:MAX_REPORT_CHARS_FOR_GRADING]) for r, scope in ungraded
        ])
        for r, _ in ungraded:
            v = verdicts.get(r["id"])
            if v is not None:
                r["pass"], r["method"], r["evidence"] = v["present"], "llm", v["evidence"]

    forbidden = []
    for rule in gold.get("forbidden", []):
        hits = sorted({m.group(0) for p in rule["patterns"] for m in re.finditer(p, everything, re.I)})
        if hits:
            forbidden.append({"id": rule["id"], "hits": hits[:10]})

    absence = [re.compile(p, re.I) for p in gold.get("absence_phrases", [])]
    false_missing = []
    for doc in gold.get("present_documents", []):
        pats = [re.compile(p, re.I) for p in doc["patterns"]]
        for report, text in texts.items():
            for s in _sentences(text):
                if any(p.search(s) for p in pats) and any(a.search(s) for a in absence):
                    false_missing.append({"document": doc["id"], "report": report, "sentence": s[:300]})

    passed = sum(1 for r in results if r["pass"])
    return {
        "golden": name,
        "facts": len(results),
        "passed": passed,
        "pass_rate": round(passed / max(len(results), 1), 3),
        "results": results,
        "forbidden": forbidden,
        "false_missing_claims": false_missing,
        "coverage": _coverage(job_id),
    }


def evaluate_recorded(job_id, name: str = "cari_health") -> dict:
    """evaluate(), with the grading call's cost recorded against the job."""
    with usage.task_context(job_id, None):
        return evaluate(job_id, name)
