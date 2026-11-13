"""What each Stage 2 drafting call is shown, assembled from per-file digests
(app/tasks/digest_document.py) and the job's Key Facts sheet
(app/tasks/key_facts.py).

This replaces the old category-filtered dump, which stopped adding files
silently once a fixed 60k-character budget was used and truncated every
file at 20k characters. Measured on a real 58-file dataroom: financial
sections saw 4 of their 12 files, regulatory saw 0. Now:

1. Every drafting call shares one cached per-job block: the Key Facts sheet,
   an index of every document in the room (type, date, one-line summary,
   tags), and the pitch deck.
2. Every document tagged for the section's report contributes its full
   digest (summary + exhaustive key facts), so no relevant file is ever
   dropped.
3. On top of that, the relevant documents' full text, budgeted by
   water-filling: every file gets the same cap, files shorter than the cap
   give their unused share back to the rest, so many short documents all
   arrive complete and a few long ones share what's left.
"""
from app import extract, repo, storage

# Character budgets (roughly 3.3 chars/token for dense financial/legal
# text). The ceiling keeps prompt + 64k max_tokens + web-search results
# well inside the context window.
PROMPT_CHAR_CEILING = 330_000
WEB_SEARCH_RESERVE = 110_000
MIN_FULL_TEXT_BUDGET = 40_000
MAX_DIGEST_INPUT_CHARS = 300_000
PITCH_DECK_CHARS = 40_000


def text_key(job_id, file_id) -> str:
    return f"text/{job_id}/{file_id}.txt"


def file_text(f: dict) -> str:
    """A dataroom file's full text: the sidecar written at digest time
    (which includes Claude's transcription for scanned files), else a fresh
    extraction from the original bytes."""
    digest = f.get("digest") or {}
    if digest.get("text_key"):
        try:
            return storage.get_bytes(digest["text_key"]).decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        return extract.extract_text(f["original_filename"], storage.get_bytes(f["object_key"]))
    except Exception as exc:
        return f"[could not fetch this file from storage: {exc}]"


def _file_label(f: dict) -> str:
    return f["original_filename"]


def render_digest(f: dict) -> str:
    d = f.get("digest") or {}
    if not d:
        return f"--- {_file_label(f)} ---\n(no digest available)"
    lines = [
        f"--- {_file_label(f)} ---",
        f"Type: {d.get('document_type', '')} | Date: {d.get('document_date') or 'undated'} | "
        f"Status: {d.get('execution_status', '')} | Tags: {', '.join(d.get('tags', []))}",
    ]
    if d.get("parties"):
        lines.append(f"Parties: {'; '.join(d['parties'])}")
    if d.get("summary"):
        lines.append(f"Summary: {d['summary']}")
    if d.get("key_facts"):
        lines.append("Key facts:")
        lines += [f"  - {k['fact']}" + (f" [{k['location']}]" if k.get("location") else "") for k in d["key_facts"]]
    if d.get("issues"):
        lines.append("Issues noted: " + " | ".join(d["issues"]))
    return "\n".join(lines)


def document_index(files: list[dict]) -> str:
    rows = []
    for f in files:
        d = f.get("digest") or {}
        one_line = (d.get("summary") or "").split(". ")[0][:220]
        rows.append(
            f"- {_file_label(f)} | {d.get('document_type', '?')} | {d.get('document_date') or 'undated'} | "
            f"{d.get('execution_status', '?')} | tags: {', '.join(d.get('tags', [])) or '-'} | {one_line}"
        )
    return "\n".join(rows)


def render_key_facts(key_facts: dict | None) -> str:
    if not key_facts:
        return "(no Key Facts sheet available -- rely on the documents directly)"
    if key_facts.get("error"):
        return f"(Key Facts sheet could not be built: {key_facts['error']} -- rely on the documents directly)"
    lines = ["Company profile:", key_facts.get("company_profile", ""), "", "Canonical facts:"]
    for k in key_facts.get("facts", []):
        as_of = f" (as of {k['as_of']})" if k.get("as_of") else ""
        lines.append(f"- [{k['topic']}] {k['fact']}{as_of} -- {', '.join(k.get('sources', []))}")
    if key_facts.get("conflicts"):
        lines += ["", "Conflicts between sources and how to resolve them:"]
        for c in key_facts["conflicts"]:
            lines.append(f"- [{c['topic']}] {c['description']} => {c['resolution']}")
    if key_facts.get("missing_documents"):
        lines += ["", "Expected documents not found in the dataroom:"]
        lines += [f"- {m}" for m in key_facts["missing_documents"]]
    return "\n".join(lines)


def job_context_block(job_id) -> str:
    """The cached prefix every drafting call for this job shares."""
    job = repo.get_job(job_id)
    dataroom = repo.list_dataroom_files(job_id, file_kind="dataroom")
    decks = repo.list_dataroom_files(job_id, file_kind="pitch_deck")
    parts = [
        f"### Key Facts sheet for {job['company_name']}",
        "Every figure, name, date and term in your output must agree with this sheet unless a document "
        "you cite shows it is wrong (then say so explicitly).",
        render_key_facts(job.get("key_facts")),
        "",
        "### Index of every document in the dataroom",
        document_index(dataroom + decks) or "(empty dataroom)",
    ]
    if decks:
        deck_text = file_text(decks[0])
        parts += ["", f"### Pitch deck ({_file_label(decks[0])})", deck_text[:PITCH_DECK_CHARS]]
    return "\n".join(parts)


def water_fill(lengths: list[int], budget: int) -> int:
    """The per-file cap c such that sum(min(L, c)) <= budget, as large as
    possible -- short files are included whole and their unused share goes
    to the long ones."""
    if sum(lengths) <= budget:
        return max(lengths, default=0)
    remaining, n = budget, len(lengths)
    for i, length in enumerate(sorted(lengths)):
        share = remaining // (n - i)
        if length > share:
            return share
        remaining -= length
    return remaining


def relevant_files(job_id, tags: tuple[str, ...]) -> list[dict]:
    files = repo.list_dataroom_files(job_id, file_kind="dataroom")
    if not tags:
        return []
    out = []
    for f in files:
        d = f.get("digest") or {}
        file_tags = set(d.get("tags") or [])
        if not d:
            file_tags = {_LEGACY_CATEGORY_TAGS.get(f.get("category"), "")}
        if file_tags & set(tags):
            out.append(f)
    # Most important first, then those matching the report's primary tag.
    primary = tags[0]
    out.sort(key=lambda f: (-(f.get("digest") or {}).get("importance", 3),
                            primary not in ((f.get("digest") or {}).get("tags") or [])))
    return out


# Fallback for a file with no digest at all (shouldn't happen -- digest
# tasks always complete, with a fallback digest on their last attempt).
_LEGACY_CATEGORY_TAGS = {
    "financial": "financial", "legal_corporate": "corporate", "ip": "ip", "hr": "team",
    "sales_marketing": "commercial", "science_tech": "science_tech", "contracts": "contracts",
    "regulatory": "regulatory",
}


def evidence_for(job_id, tags: tuple[str, ...], used_chars: int, web_search: bool) -> str:
    """Digests + water-filled full text of every file relevant to `tags`.
    `used_chars` is what the rest of the prompt already takes."""
    files = relevant_files(job_id, tags)
    if not files:
        return "(no dataroom documents were tagged as relevant to this report -- say so where it matters)"
    digests = "\n\n".join(render_digest(f) for f in files)
    budget = PROMPT_CHAR_CEILING - used_chars - len(digests) - (WEB_SEARCH_RESERVE if web_search else 0)
    budget = max(budget, MIN_FULL_TEXT_BUDGET)
    texts = [(f, file_text(f)) for f in files]
    texts = [(f, t) for f, t in texts if not extract.is_placeholder(t)]
    cap = water_fill([len(t) for _, t in texts], budget)
    chunks = []
    for f, t in texts:
        body = t if len(t) <= cap else t[:cap] + f"\n[... {len(t) - cap:,} more characters omitted; see the digest above for the whole document]"
        chunks.append(f"=== {_file_label(f)} (full text) ===\n{body}")
    return (
        f"#### Digests of the {len(files)} documents relevant to this report\n{digests}\n\n"
        f"#### Full text of those documents\n" + "\n\n".join(chunks)
    )


QA_EVIDENCE_CHARS = 160_000


def _compact_digest(f: dict) -> str:
    d = f.get("digest") or {}
    facts = "; ".join(k["fact"] for k in (d.get("key_facts") or [])[:8])
    return (f"--- {_file_label(f)} --- {d.get('document_type', '')} | {d.get('document_date') or 'undated'} | "
            f"{d.get('execution_status', '')}\n{(d.get('summary') or '')[:600]}" + (f"\nKey facts: {facts}" if facts else ""))


def qa_evidence(job_id) -> str:
    """For Stage 4 QA: the Key Facts sheet plus every document's digest --
    the whole room, condensed. QA also reads the full compiled report (now
    CARI-length), so digests are full for the most important documents and
    compact for the rest once the budget is reached; every file still
    appears."""
    job = repo.get_job(job_id)
    files = sorted(repo.list_dataroom_files(job_id),
                   key=lambda f: -((f.get("digest") or {}).get("importance") or 3))
    head = "### Key Facts sheet\n" + render_key_facts(job.get("key_facts")) + "\n\n### Digest of every dataroom document\n"
    used, parts = len(head), []
    for i, f in enumerate(files):
        full = render_digest(f)
        compact_rest = sum(len(_compact_digest(g)) for g in files[i + 1:])
        chunk = full if used + len(full) + compact_rest <= QA_EVIDENCE_CHARS else _compact_digest(f)
        parts.append(chunk)
        used += len(chunk) + 2
    return head + "\n\n".join(parts)
