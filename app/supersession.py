"""Document families and current versions (P3).

The Nov 15 draft used the original 2021 equity plan (1,333,333 shares)
instead of the amended pool (1,878,661), and called the Third A&R charter
"not confirmed as filed" although a certified Delaware copy confirms it.
This groups versions of the same instrument -- plan + amendments, charter
+ restatements, cap-table versions, financial-model versions, offer letter
+ salary adjustment, agreement + amendments -- orders each family by
effective date and marks the latest `current`; earlier members get
`superseded_by`. Government-certified copies (Secretary of State, USPTO,
SEC) are authoritative for whether something was filed.

One reading-model call on fact-card METADATA only (path, type, title,
date, parties, signature status) -- never on full text (~25k tokens for a
400-file room, under a cent). The result is written into each file's
digest; the evidence pack shows a superseded document as one line pointing
to its current version, and the current one carries a one-line history.
"""
import logging

from app import evidence, repo

log = logging.getLogger("supersession")


def _meta_line(n: int, f: dict) -> str:
    d = f.get("digest") or {}
    parties = "; ".join((d.get("parties") or [])[:3])
    return (f"{n} | {f.get('source_path') or f['original_filename']} | {d.get('document_type', '')} | "
            f"{d.get('title', '')} | {d.get('document_date') or 'undated'} | {d.get('execution_status', '')} | {parties}")


def run(job_id, company_name: str) -> dict:
    from app import claude_client

    files, _ = evidence.unique_files(job_id)
    files = [f for f in files if (f.get("digest") or {}) and not f["digest"].get("fallback")]
    if len(files) < 2:
        return {"families": 0}
    index = {n + 1: f for n, f in enumerate(files)}
    families = claude_client.group_document_families(company_name, [_meta_line(n, f) for n, f in index.items()])
    marked = 0
    for fam in families:
        members = [m for m in fam.member_docs if m in index]
        current = fam.current_doc if fam.current_doc in index else (members[-1] if members else None)
        if current is None or len(members) < 2:
            continue
        cur = index[current]
        cur_name = cur.get("source_path") or cur["original_filename"]
        for m in members:
            f = index[m]
            d = dict(f.get("digest") or {})
            d["family"] = fam.name
            if m == current:
                d["is_current"] = True
                d["family_history"] = fam.history
                if fam.authoritative_doc in index and fam.authoritative_doc != current:
                    a = index[fam.authoritative_doc]
                    d["authoritative_source"] = a.get("source_path") or a["original_filename"]
            else:
                d["superseded_by"] = cur_name
                d.pop("is_current", None)
                marked += 1
            repo.set_file_digest(str(f["id"]), d)
            f["digest"] = d
    return {"families": len(families), "superseded": marked}
