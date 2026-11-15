"""Fix E: check every "missing document" claim before the draft is compiled.

The drafts said the executed IRA, the warrants, offer letters and more were
"not in the data room" when they were. After drafting, this:

1. finds every sentence claiming something is missing, unsigned,
   unconfirmed or not provided (table cells and list items included);
2. matches each against the fact card of every document in the room
   (BM25 over title, type, parties and summary -- local, free);
3. asks the reading model, in ONE batched call, whether any candidate
   document actually contradicts the claim;
4. rewrites only the overturned passages, in ONE drafting-model call, with
   the contradicting documents' facts attached, and patches them in place;
5. logs every claim checked and every one overturned (storage JSON,
   GET /jobs/{id}/absence-log).
"""
import json
import re

from app import evidence, repo, storage

ABSENCE = re.compile(
    r"not (?:in|included in|present in|provided in|found in|located in|available in) the data ?room|"
    r"not (?:been )?(?:provided|included|located|found|seen|produced)|\bmissing\b|\babsent\b|"
    r"could not (?:be )?(?:confirm|verif|locat|review)|\bunconfirmed\b|\bunverified\b|"
    r"\bno (?:executed|signed|copy of)\b|does not contain|were not reviewed|not (?:yet )?(?:executed|signed)|"
    r"\bnot found\b|no evidence of",
    re.I,
)
MAX_CLAIMS = 200
CLAIMS_PER_CALL = 40   # keeps each verification prompt under Haiku 5.5's 100k-token price tier
CANDIDATES_PER_CLAIM = 3


def log_key(job_id) -> str:
    return f"reports/{job_id}/absence_check.json"


def _units(sections: list[dict]):
    """Every text unit of the report: (section_key, path, text). path
    locates it inside the section's blocks for patching."""
    for s in sections:
        for bi, b in enumerate(s["content"]):
            t = b.get("type")
            if t in ("paragraph", "callout", "note"):
                yield s["section_key"], (bi, "text"), b.get("text") or ""
            elif t == "bullets":
                for ii, item in enumerate(b.get("items") or []):
                    yield s["section_key"], (bi, "items", ii), item
            elif t == "table":
                for ri, row in enumerate(b.get("rows") or []):
                    # a table row is checked as one unit (claims like "IRA | Not found | ...")
                    yield s["section_key"], (bi, "rows", ri), " | ".join(row)


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.;!?])\s+", text) if x.strip()]


def find_claims(sections: list[dict]) -> list[dict]:
    claims = []
    for key, path, text in _units(sections):
        for sent in _sentences(text):
            if ABSENCE.search(sent):
                claims.append({"id": f"c{len(claims)}", "section_key": key, "path": list(path), "unit": text,
                               "claim": sent[:500]})
                if len(claims) >= MAX_CLAIMS:
                    return claims
    return claims


def _card_text(f: dict) -> str:
    d = f.get("digest") or {}
    facts = "; ".join((k["fact"] if isinstance(k, dict) else str(k)) for k in (d.get("key_facts") or [])[:10])
    return (f"[{f.get('source_path') or f['original_filename']}] {d.get('document_type', '')} | "
            f"{d.get('document_date') or 'undated'} | {d.get('execution_status', '')} | "
            f"signed by: {'; '.join(d.get('signatories') or []) or '-'}\n{d.get('summary', '')}\n{facts}")


def candidates(job_id, claims: list[dict]) -> dict[str, list[str]]:
    """claim id -> the fact cards of the documents most likely to answer it."""
    from rank_bm25 import BM25Okapi

    files, _ = evidence.unique_files(job_id)
    files = [f for f in files if (f.get("digest") or {}) and not (f["digest"].get("fallback"))]
    if not files:
        return {}
    docs = [f"{f.get('source_path') or ''} {f['original_filename']} {(f['digest'] or {}).get('document_type', '')} "
            f"{(f['digest'] or {}).get('title', '')} {' '.join((f['digest'] or {}).get('parties') or [])} "
            f"{(f['digest'] or {}).get('summary', '')}" for f in files]
    bm25 = BM25Okapi([evidence._tokens(d) or ["_"] for d in docs])
    out = {}
    for c in claims:
        scores = bm25.get_scores(evidence._tokens(c["claim"]))
        top = [i for i in sorted(range(len(files)), key=lambda i: -scores[i])[:CANDIDATES_PER_CLAIM] if scores[i] > 1.0]
        if top:
            out[c["id"]] = [_card_text(files[i]) for i in top]
    return out


def _patch(blocks: list[dict], path: list, new_text: str) -> None:
    bi, kind = path[0], path[1]
    if kind == "text":
        blocks[bi]["text"] = new_text
    elif kind == "items":
        blocks[bi]["items"][path[2]] = new_text
    elif kind == "rows":
        row = blocks[bi]["rows"][path[2]]
        cells = [c.strip() for c in new_text.split("|")]
        if len(cells) == len(row):
            blocks[bi]["rows"][path[2]] = cells


def run(job_id, company_name: str) -> dict:
    from app import claude_client

    sections = repo.list_report_sections(job_id)
    claims = find_claims(sections)
    cands = candidates(job_id, claims) if claims else {}
    checked = [c for c in claims if c["id"] in cands]
    verdicts = {}
    for i in range(0, len(checked), CLAIMS_PER_CALL):
        verdicts.update(claude_client.verify_absence_claims(company_name, [
            (c["id"], c["claim"], cands[c["id"]]) for c in checked[i:i + CLAIMS_PER_CALL]]))
    overturned = [c for c in checked if verdicts.get(c["id"], {}).get("overturned")]

    # Rewrite each affected unit once, with all of its overturned claims.
    by_unit: dict[tuple, dict] = {}
    for c in overturned:
        u = by_unit.setdefault((c["section_key"], tuple(c["path"])), {"unit": c["unit"], "corrections": []})
        u["corrections"].append(f"{c['claim']} -> {verdicts[c['id']]['correction']} "
                                f"(document: {verdicts[c['id']]['document']})")
    rewrites = claude_client.rewrite_absence_units(company_name, [
        (f"u{n}", u["unit"], u["corrections"], " | " in u["unit"] and k[1][1] == "rows")
        for n, (k, u) in enumerate(by_unit.items())]) if by_unit else {}
    patched = 0
    rows = {s["section_key"]: s for s in sections}
    for n, (key, u) in enumerate(by_unit.items()):
        new = rewrites.get(f"u{n}")
        if not new:
            continue
        section_key, path = key
        s = rows[section_key]
        _patch(s["content"], list(path), new)
        repo.upsert_report_section(job_id, section_key, s["title"], s["order_index"], s["content"],
                                   s.get("source_task_id"), heading_level=s.get("heading_level") or 1,
                                   meta=s.get("meta") or {})
        patched += 1
    log = {"claims_found": len(claims), "claims_checked": len(checked), "overturned": [
        {**c, "verdict": verdicts[c["id"]]} for c in overturned], "units_rewritten": patched,
        "stood": [{"claim": c["claim"], "section_key": c["section_key"]} for c in checked if c not in overturned]}
    storage.put_bytes(log_key(job_id), json.dumps(log, indent=1).encode("utf-8"), content_type="application/json")
    return {"claims_found": len(claims), "checked": len(checked), "overturned": len(overturned),
            "units_rewritten": patched}
