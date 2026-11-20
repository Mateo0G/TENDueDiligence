"""Verification pass after drafting, before the .docx is built (P6).

1. Absence check (app/absence.py): "missing / unsigned / unconfirmed"
   claims contradicted by a document in the room.
2. Coverage (Python, no LLM): every required fact -- spreadsheet facts
   (app/structured.py), roster rows (app/rosters.py), competitor-employer
   overlaps -- must appear in its report; numbers and names are normalized.
3. Contradictions (reading model, batched): sentences with figures, dates
   or roles are checked against the fact store (Key Facts, spreadsheet
   facts, current-version histories, people's roles).
4. Forbidden phrases ("not established", "not populated", tool failures).

Missing facts are added by rewriting the single best-matching paragraph;
contradicted sentences and forbidden phrases are rewritten in place --
never whole sections -- in ONE drafting-model call. Every finding and
correction goes to verification_log.json (GET /jobs/{id}/verification-log).
The budget guard (app/usage.py) skips the rewrites if the job's spend is
already past the soft limit.
"""
import json
import logging
import re

from app import absence, evidence, repo, rosters, storage, usage

log = logging.getLogger("verify")

FORBIDDEN = re.compile(r"not established|not populated|search tool|tool (failed|was unavailable|unavailable)|"
                       r"search (cap|limit)|cap was reached|could not be (searched|researched)", re.I)
CLAIM_HINT = re.compile(r"\d|\b(director|board|advis|investigator|founder|ceo|cfo|officer|consultant|filed|"
                        r"executed|signed|pool|reserved)\b", re.I)
SENTENCES_PER_CALL = 120


def log_key(job_id) -> str:
    return f"reports/{job_id}/verification_log.json"


def _norm(s: str) -> str:
    return re.sub(r"[\s,$]", "", s.lower())


def _report_texts(sections: list[dict]) -> dict[str, str]:
    from app.pipeline import render_blocks_as_text
    out: dict[str, list[str]] = {}
    for s in sections:
        out.setdefault(s["section_key"].split(".", 1)[0], []).append(render_blocks_as_text(s["content"]))
    return {k: "\n".join(v) for k, v in out.items()}


# --------------------------------------------------------------- coverage --

def required_facts(job: dict) -> list[dict]:
    """(report, token that must appear, the fact to state if it doesn't)."""
    req = []
    sf = job.get("structured_facts") or {}
    cap = sf.get("cap_table") or {}
    facts = sf.get("facts") or []

    def fact_with(token: str) -> str:
        return next((f for f in facts if token in f), token)

    if cap.get("fully_diluted"):
        for rep in ("corp_legal", "financial"):
            for v in (cap.get("outstanding"), cap["fully_diluted"]):
                if v:
                    t = f"{v:,.0f}"
                    req.append({"report": rep, "token": t, "fact": fact_with(t)})
    if cap.get("plan_pool"):
        t = f"{cap['plan_pool']:,.0f}"
        req.append({"report": "corp_legal", "token": t, "fact": fact_with(t)})
    for f in facts:
        m = re.search(r"= \$(\d+\.\d{3}) per share", f)
        if m:
            for rep in ("financial", "corp_legal", "executive_summary"):
                req.append({"report": rep, "token": m.group(1), "fact": f})
        m = re.search(r"implies about \$(\d+\.\d{2}) per share", f)
        if m:
            req.append({"report": "financial", "token": m.group(1), "fact": f})
    for sc in sf.get("scenarios") or []:
        rev = next((v for k, v in sc["values"].items() if re.search(r"20\d\d revenue", k, re.I) and
                    re.search(r"2032|203\d", k)), None)
        try:
            token = f"{float(rev) / 1e6:.1f}" if rev is not None else None
        except (TypeError, ValueError):
            token = None
        if token:
            req.append({"report": "financial", "token": token,
                        "fact": next((f for f in facts if sc["scenario"] in f), sc["scenario"])})
    for g in sf.get("grants") or []:
        t = f"{g['amount']:,.0f}"
        req.append({"report": "financial", "token": t, "fact": fact_with(t)})
    r = job.get("rosters") or {}
    for o in r.get("employer_overlaps") or []:
        for rep in ("hr", "competition"):
            req.append({"report": rep, "token": o["person"].split()[-1], "fact":
                        f"{o['person']} previously worked at {o['employer']}, a competitor ({o['competitor']})."})
    for ev in (r.get("ip_events") or [])[:30]:
        num = re.sub(r"[^\d/,]", "", ev.get("number", ""))
        if len(re.sub(r"\D", "", num)) >= 6:
            req.append({"report": "ip", "token": num, "fact":
                        f"{ev.get('number')}: {ev.get('event')} {ev.get('date')}"
                        + (f" (reel/frame {ev['reel_frame']})" if ev.get("reel_frame") else "")})
    for a in (r.get("agreements") or [])[:40]:
        cp = (a.get("counterparty") or "").split(",")[0].strip()
        if len(cp) > 3 and "cari" not in cp.lower():
            req.append({"report": "contracts", "token": cp, "fact":
                        f"{a.get('type')} with {a.get('counterparty')} ({a.get('date')}"
                        + (f", fee {a['fee']}" if a.get("fee") else "") + f", executed: {a.get('executed')})"})
    return req


def missing_facts(texts: dict[str, str], req: list[dict]) -> list[dict]:
    out = []
    for r in req:
        text = texts.get(r["report"], "")
        if text and _norm(r["token"]) not in _norm(text):
            out.append(r)
    return out


# ----------------------------------------------------------- the pass --

def _units(sections: list[dict]):
    """(section_key, path, text) for every paragraph-level unit."""
    yield from absence._units(sections)


def _best_paragraph(sections: list[dict], report: str, fact: str):
    """The paragraph in `report` sharing the most words with `fact`."""
    words = set(re.findall(r"[a-z]{4,}", fact.lower()))
    best, score = None, -1
    for key, path, text in _units(sections):
        if key.split(".", 1)[0] != report or path[1] != "text" or len(text) < 80:
            continue
        s = len(words & set(re.findall(r"[a-z]{4,}", text.lower())))
        if s > score:
            best, score = (key, tuple(path), text), s
    return best


def fact_store(job: dict) -> str:
    from app import structured
    parts = [evidence.render_key_facts(job.get("key_facts")), structured.render(job.get("structured_facts"))]
    files, _ = evidence.unique_files(job["id"])
    hist = [f"- {(f['digest'] or {}).get('family')}: {(f['digest'] or {}).get('family_history')}"
            for f in files if (f.get("digest") or {}).get("is_current") and (f["digest"] or {}).get("family_history")]
    if hist:
        parts.append("### Current versions of document families\n" + "\n".join(hist))
    people = (job.get("rosters") or {}).get("people") or []
    if people:
        parts.append("### People and their roles as the documents state them\n" + "\n".join(
            f"- {p['name']}: {'; '.join(p['roles'])[:240]}" for p in people[:120]))
    return "\n\n".join(p for p in parts if p)


def run(job_id, company_name: str) -> dict:
    from app import claude_client

    result = {"absence": absence.run(job_id, company_name)}  # rewrites its own corrections
    job = repo.get_job(job_id)
    sections = repo.list_report_sections(job_id)
    texts = _report_texts(sections)

    # 2. coverage
    req = required_facts(job)
    missing = missing_facts(texts, req)
    # 3. contradictions
    store = fact_store(job)
    claims = []
    for key, path, text in _units(sections):
        for s in re.split(r"(?<=[.;!?])\s+", text):
            if CLAIM_HINT.search(s) and len(s) > 30:
                claims.append({"id": f"s{len(claims)}", "section_key": key, "path": list(path), "unit": text,
                               "sentence": s[:400]})
    contradictions = []
    for i in range(0, len(claims), SENTENCES_PER_CALL):
        chunk = claims[i:i + SENTENCES_PER_CALL]
        found = claude_client.check_contradictions(company_name, store, [(c["id"], c["sentence"]) for c in chunk])
        by_id = {c["id"]: c for c in chunk}
        contradictions += [{**by_id[f["id"]], **f} for f in found if f["id"] in by_id]
    # 4. forbidden phrases
    phrases = [{"section_key": k, "path": list(p), "unit": t, "issue": FORBIDDEN.search(t).group(0)}
               for k, p, t in _units(sections) if FORBIDDEN.search(t)]

    # One rewrite call for everything that needs changing, unit by unit.
    edits: dict[tuple, dict] = {}
    for c in contradictions:
        e = edits.setdefault((c["section_key"], tuple(c["path"])), {"unit": c["unit"], "fixes": []})
        e["fixes"].append(f"Correct: \"{c['sentence'][:200]}\" -> {c['correction']}")
    for p in phrases:
        e = edits.setdefault((p["section_key"], tuple(p["path"])), {"unit": p["unit"], "fixes": []})
        e["fixes"].append(f"Remove the wording \"{p['issue']}\": give the best-supported figure from the fact "
                          f"store, marked (unverified) if needed, or a specific follow-up item instead.")
    by_report: dict[str, list[dict]] = {}
    for m in missing:
        by_report.setdefault(m["report"], []).append(m)
    for report, ms in by_report.items():
        target = _best_paragraph(sections, report, " ".join(m["fact"] for m in ms))
        if target:
            e = edits.setdefault((target[0], target[1]), {"unit": target[2], "fixes": []})
            e["fixes"] += [f"Add this fact (exact figures): {m['fact']}" for m in ms[:12]]

    skipped = usage.over_soft_limit(job_id)
    patched = 0
    if edits and not skipped:
        units = [(f"u{n}", e["unit"], e["fixes"], k[1][1] == "rows") for n, (k, e) in enumerate(edits.items())]
        rewrites = claude_client.rewrite_absence_units(company_name, units, context=store[:40_000],
                                                       purpose="verify_rewrite")
        rows = {s["section_key"]: s for s in sections}
        for n, (key, e) in enumerate(edits.items()):
            new = rewrites.get(f"u{n}")
            if not new:
                continue
            s = rows[key[0]]
            absence._patch(s["content"], list(key[1]), new)
            repo.upsert_report_section(job_id, key[0], s["title"], s["order_index"], s["content"],
                                       s.get("source_task_id"), heading_level=s.get("heading_level") or 1,
                                       meta=s.get("meta") or {})
            patched += 1
    log_doc = {"required_facts": len(req), "missing_facts": missing, "contradictions": [
        {k: c[k] for k in ("section_key", "sentence", "issue", "correction")} for c in contradictions],
        "forbidden_phrases": [{k: p[k] for k in ("section_key", "issue")} for p in phrases],
        "units_rewritten": patched, "skipped_for_budget": skipped, "absence": result["absence"]}
    storage.put_bytes(log_key(job_id), json.dumps(log_doc, indent=1).encode("utf-8"), content_type="application/json")
    return {**result, "required": len(req), "missing": len(missing), "contradictions": len(contradictions),
            "phrases": len(phrases), "rewritten": patched, "skipped_for_budget": skipped}
