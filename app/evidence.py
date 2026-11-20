"""What the Stage 2 drafting calls (and Stage 4 QA) are shown.

One evidence *pack* per job: the digest of every unique document in the
room (app/reading.py), grouped by subject, plus the pitch deck's text. It
is bounded by construction (PACK_CHARS) however big the room is -- the old
per-section assembly added every relevant digest with no ceiling, which on
an 810-file room produced 1.4-1.8M-token prompts that the API rejected.

Every drafting call starts with the same prefix -- the Team's writing rules,
the Key Facts sheet, the web research memo, and this pack -- stored once
per job (drafting_context) so the bytes are identical and the prompt cache
serves it to ~24 calls at a tenth of the input price.
"""
import re

from app import extract, repo, storage

PACK_CHARS = 170_000          # ~47k tokens of digests
DECK_CHARS = 30_000
MAX_DIGEST_CHARS = 6_000      # one document's digest, however long the old-style digest was
QA_PACK_CHARS = 40_000

_TAG_ORDER = ("deal_terms", "financial", "corporate", "team", "commercial", "competition", "science_tech",
              "manufacturing", "regulatory", "ip", "contracts", "exit")
_TAG_HEADINGS = {
    "deal_terms": "Financing and deal documents", "financial": "Financial documents",
    "corporate": "Corporate and governance documents", "team": "Team, employment and advisor documents",
    "commercial": "Commercial, customer and market documents", "competition": "Competition documents",
    "science_tech": "Science and technology documents", "manufacturing": "Manufacturing and supply documents",
    "regulatory": "Regulatory and quality documents", "ip": "Intellectual property documents",
    "contracts": "Other agreements", "exit": "Exit-related documents", "": "Unclassified documents",
}


def text_key(job_id, file_id) -> str:
    return f"text/{job_id}/{file_id}.txt"


def drafting_context_key(job_id) -> str:
    return f"context/{job_id}/drafting.txt"


def file_text(f: dict) -> str:
    """A file's full extracted text (the sidecar written when it was read),
    else a fresh extraction."""
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


def _label(f: dict) -> str:
    return f.get("source_path") or f["original_filename"]


def unique_files(job_id) -> tuple[list[dict], int]:
    """Every dataroom file and pitch deck once: duplicates (same bytes, same
    text, or -- for files uploaded before hashes were kept -- same name and
    size) are dropped. Returns (files, number dropped)."""
    files = repo.list_dataroom_files(job_id)
    seen, out = set(), []
    for f in files:
        d = f.get("digest") or {}
        if d.get("duplicate_of"):
            continue
        keys = {("name", f["original_filename"].strip().lower(), f.get("size_bytes"))}
        if f.get("sha256"):
            keys.add(("sha", f["sha256"]))
        if d.get("text_hash"):
            keys.add(("text", d["text_hash"]))
        if keys & seen:
            continue
        seen |= keys
        out.append(f)
    return out, len(files) - len(out)


def _primary_tag(f: dict) -> str:
    tags = (f.get("digest") or {}).get("tags") or []
    return tags[0] if tags else ""


def _importance(f: dict) -> int:
    return int((f.get("digest") or {}).get("importance") or 3)


def render_digest(f: dict, compact: bool = False) -> str:
    d = f.get("digest") or {}
    status = d.get("execution_status", "")
    head = (f"[{_label(f)}] {d.get('document_type', '?')} | {d.get('document_date') or 'undated'}"
            f"{' | ' + status if status and status != 'not_applicable' else ''}")
    if not d:
        return head + " | not read"
    if d.get("superseded_by"):
        # An older version: one line, so the current one is what gets cited.
        return f"{head} | SUPERSEDED by {d['superseded_by']} -- cite the current version, not this one"
    summary = (d.get("summary") or "").strip()
    if compact:
        first = re.split(r"(?<=[.;])\s", summary, maxsplit=1)[0][:240]
        return f"{head} | {first}"
    lines = [head + f" | importance {_importance(f)} | tags: {', '.join(d.get('tags') or []) or '-'}"]
    if d.get("is_current"):
        lines[0] += f" | CURRENT version ({d.get('family', 'document family')})"
        if d.get("family_history"):
            lines.append(f"Version history: {d['family_history']}")
        if d.get("authoritative_source"):
            lines.append(f"Filing confirmed by the government-certified copy: {d['authoritative_source']}")
    if d.get("pages_total"):
        if d.get("read_mode") == "sample":
            lines[0] += f" | routine document: key pages of {d['pages_total']} summarized"
        elif d.get("pages_extracted", d["pages_total"]) < d["pages_total"]:
            lines[0] += f" | only {d['pages_extracted']} of {d['pages_total']} pages could be read"
        else:
            lines[0] += f" | all {d['pages_total']} pages read"
    elif d.get("read_chars") and d.get("text_chars") and d["read_chars"] < d["text_chars"]:
        lines[0] += f" | read {d['read_chars']:,} of {d['text_chars']:,} chars"
    if d.get("parties"):
        lines.append("Parties: " + "; ".join(d["parties"][:12]))
    if d.get("signatories"):
        lines.append("Signed by: " + "; ".join(d["signatories"][:12]))
    if d.get("people_mentioned"):
        lines.append("People: " + "; ".join(d["people_mentioned"][:15]))
    if summary:
        lines.append(summary)
    for k in d.get("key_facts") or []:
        fact = k["fact"] if isinstance(k, dict) else str(k)
        loc = k.get("location") if isinstance(k, dict) else ""
        lines.append(f"- {fact}" + (f" ({loc})" if loc else ""))
    if d.get("issues"):
        lines.append("Issues: " + " | ".join(d["issues"]))
    text = "\n".join(lines)
    if len(text) > MAX_DIGEST_CHARS:
        text = text[:MAX_DIGEST_CHARS] + " [...]"
    return text


def _pack_body(files: list[dict], budget: int) -> str:
    """Digests grouped by subject, most important first. If the full
    digests don't fit, the least important documents are reduced to one
    line each (least important first), and only if even that doesn't fit
    are the least important one-liners dropped -- with a count, so the
    reader knows."""
    order = sorted(files, key=lambda f: (_TAG_ORDER.index(_primary_tag(f)) if _primary_tag(f) in _TAG_ORDER
                                         else len(_TAG_ORDER), -_importance(f), _label(f).lower()))
    full = {id(f): render_digest(f) for f in order}
    short = {id(f): render_digest(f, compact=True) for f in order}
    mode = {id(f): "full" for f in order}
    size = sum(len(v) + 2 for v in full.values())
    for f in sorted(order, key=lambda f: (_importance(f), -len(full[id(f)]))):
        if size <= budget:
            break
        size -= len(full[id(f)]) - len(short[id(f)])
        mode[id(f)] = "short"
    dropped = 0
    for f in sorted(order, key=lambda f: _importance(f)):
        if size <= budget:
            break
        size -= len(short[id(f)]) + 2
        mode[id(f)] = "drop"
        dropped += 1

    parts, current = [], None
    for f in order:
        if mode[id(f)] == "drop":
            continue
        tag = _primary_tag(f) if _primary_tag(f) in _TAG_ORDER else ""
        if tag != current:
            parts.append(f"\n## {_TAG_HEADINGS[tag]}")
            current = tag
        parts.append(full[id(f)] if mode[id(f)] == "full" else short[id(f)])
    if dropped:
        parts.append(f"\n({dropped} low-importance documents omitted for length.)")
    return "\n\n".join(parts).strip()


def coverage_statement(job_id) -> str:
    """What was read, for the drafting model: so it neither hedges about
    pages it actually has nor claims to have read what it couldn't."""
    try:
        c = repo.coverage_summary(job_id)
    except Exception:
        return ""
    if not c["pages_total"]:
        return ""
    by = c["pages_by_method"]
    line = (f"Coverage: {c['pages_extracted']:,} of {c['pages_total']:,} pages extracted "
            f"({by.get('ocr', 0)} by OCR of scanned pages, {by.get('vision', 0)} read visually). ")
    if c["short"]:
        line += ("Not fully readable: " + "; ".join(f"{s['file']} ({s['pages_extracted']}/{s['pages_total']} pages)"
                                                     for s in c["short"][:12]) + ". ")
    line += ("A document listed in this pack IS in the data room: never describe it as missing, unconfirmed or "
             "not provided. Before saying any document is absent, check this list.")
    return line


def build_pack(job_id, budget: int = PACK_CHARS, include_deck: bool = True) -> str:
    files, n_dupes = unique_files(job_id)
    room = [f for f in files if f["file_kind"] == "dataroom"]
    decks = [f for f in files if f["file_kind"] == "pitch_deck"]
    head = (f"{len(files)} unique documents"
            + (f" ({n_dupes} duplicate copies omitted)" if n_dupes else "")
            + ". Each entry: [dataroom path] type | date | signature status | importance 1-5 | tags | pages read, "
              "then what the document establishes. Cite documents by name.\n" + coverage_statement(job_id))
    parts = [head, _pack_body(room + decks, budget)]
    if include_deck and decks:
        deck_text = file_text(decks[0])
        if not extract.is_placeholder(deck_text):
            parts.append(f"\n## Pitch deck full text ({decks[0]['original_filename']})\n{deck_text[:DECK_CHARS]}")
    return "\n\n".join(parts)


def render_key_facts(key_facts: dict | None) -> str:
    if not key_facts:
        return "(no Key Facts sheet available -- rely on the evidence pack directly)"
    if key_facts.get("error"):
        return f"(Key Facts sheet could not be built: {key_facts['error']} -- rely on the evidence pack directly)"
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


def render_research(research: dict | None) -> str:
    if not research:
        return "(no web research memo -- use only the dataroom for public-market facts and say so)"
    if research.get("error"):
        return f"(web research failed: {research['error']} -- use only the dataroom and say so)"
    memo = research.get("memo", "")
    sources = research.get("sources") or []
    if sources:
        memo += "\n\nSources consulted:\n" + "\n".join(f"- {s.get('title') or s['url']} -- {s['url']}" for s in sources[:60])
    return memo


def _structured_block(job: dict) -> str:
    from app import structured
    return structured.render(job.get("structured_facts")) or "(no spreadsheet facts parsed)"


def build_drafting_context(job_id) -> str:
    job = repo.get_job(job_id)
    return "\n".join([
        f"# Evidence for the {job['company_name']} due diligence report",
        "",
        "### Key Facts sheet",
        "Every figure, name, date and term in your output must agree with this sheet unless a document "
        "shows it is wrong (then say so explicitly).",
        render_key_facts(job.get("key_facts")),
        "",
        _structured_block(job),
        "",
        "### Web research memo (public sources, gathered for this report)",
        render_research(job.get("research")),
        "",
        "### Dataroom evidence pack: a digest of every document in the dataroom",
        build_pack(job_id),
    ])


def store_drafting_context(job_id) -> str:
    text = build_drafting_context(job_id)
    storage.put_bytes(drafting_context_key(job_id), text.encode("utf-8"), content_type="text/plain; charset=utf-8")
    return text


def drafting_context(job_id) -> str:
    """The stored prefix (identical bytes for every call = cache hits); built
    and stored on first use if the warm-up task didn't."""
    try:
        return storage.get_bytes(drafting_context_key(job_id)).decode("utf-8")
    except Exception:
        return store_drafting_context(job_id)


_SHEET_PRIORITY = re.compile(r"summary|p&l|p & l|income|profit|statement|forecast|projection|financial|cap|"
                             r"ledger|valuation|use of funds|balance|cash", re.I)


def _prioritize_sheets(text: str) -> str:
    """Spreadsheet text with the summary/P&L/cap-table sheets first, so a
    budgeted excerpt shows the numbers that matter rather than an
    assumptions tab."""
    sheets = re.split(r"(?m)^(?=# Sheet: )", text)
    if len(sheets) <= 1:
        return text
    return "".join(sorted(sheets, key=lambda s: 0 if _SHEET_PRIORITY.search(s.split("\n", 1)[0]) else 1))


def source_excerpts(job_id, tags: tuple[str, ...], budget: int) -> str:
    """Primary-source text for the reports where exact numbers are the
    point (financial statements, model, cap table): the most important
    matching documents, spreadsheets first, up to `budget` characters."""
    if budget <= 0:
        return ""
    files, _ = unique_files(job_id)
    cands = [f for f in files if set((f.get("digest") or {}).get("tags") or []) & set(tags) and _importance(f) >= 4
             and not (f.get("digest") or {}).get("superseded_by")]
    cands.sort(key=lambda f: (not f["original_filename"].lower().endswith((".xlsx", ".xlsm", ".csv")), -_importance(f)))
    parts, used = [], 0
    for f in cands:
        remaining = budget - used
        if remaining < 2_000:
            break
        text = file_text(f)
        if extract.is_placeholder(text):
            continue
        if f["original_filename"].lower().endswith((".xlsx", ".xlsm")):
            text = _prioritize_sheets(text)
        chunk = text[: min(remaining, max(budget // 2, 8_000))]
        if len(chunk) < len(text):
            chunk += f"\n[... excerpt; the document continues for {len(text) - len(chunk):,} more characters]"
        parts.append(f"=== {_label(f)} (primary source text) ===\n{chunk}")
        used += len(chunk)
    return "\n\n".join(parts)


_ENTITY_NAME = re.compile(r"\b(inc|llc|l\.l\.c|corp|corporation|ltd|limited|gmbh|plc|lp|l\.p|co|company|"
                          r"laboratories|labs|health|diagnostics|sciences|therapeutics|bio\w*)\.?$", re.I)


def named_companies(files: list[dict], limit: int = 60) -> list[str]:
    """Organizations the data room names (fact-card parties and the
    employers on people's records), most often first."""
    counts: dict[str, int] = {}
    for f in files:
        d = f.get("digest") or {}
        names = list(d.get("parties") or [])
        for p in (d.get("fact_card") or {}).get("people") or []:
            names += list(p.get("prior_employers") or []) if isinstance(p, dict) else []
        for n in names:
            n = " ".join(str(n).split())
            if 2 < len(n) < 80 and _ENTITY_NAME.search(n):
                counts[n] = counts.get(n, 0) + 1
    return sorted(counts, key=lambda n: -counts[n])[:limit]


def research_context(job_id) -> str:
    """What the web-research call needs to know about the company: the deck
    and the summaries of the documents that describe the product, market
    and competitors."""
    files, _ = unique_files(job_id)
    decks = [f for f in files if f["file_kind"] == "pitch_deck"]
    parts = []
    named = named_companies(files)
    if named:
        parts.append("## Companies named in the data room (counterparties, investors, employers, competitors)\n"
                     + "; ".join(named))
    if decks:
        deck = file_text(decks[0])
        if not extract.is_placeholder(deck):
            parts.append(f"## Pitch deck\n{deck[:20_000]}")
    rel = [f for f in files if set((f.get("digest") or {}).get("tags") or [])
           & {"competition", "commercial", "exit", "science_tech", "regulatory"}]
    rel.sort(key=lambda f: -_importance(f))
    used, lines = 0, []
    for f in rel:
        line = render_digest(f, compact=True)
        if used + len(line) > 20_000:
            break
        lines.append(line)
        used += len(line)
    if lines:
        parts.append("## Relevant dataroom documents\n" + "\n".join(lines))
    return "\n\n".join(parts) or "(no dataroom context available)"


def qa_evidence(job_id) -> str:
    """For Stage 4 QA: the Key Facts sheet plus a condensed pack."""
    job = repo.get_job(job_id)
    return ("### Key Facts sheet\n" + render_key_facts(job.get("key_facts"))
            + "\n\n### Dataroom evidence pack (condensed)\n" + build_pack(job_id, QA_PACK_CHARS, include_deck=False))


# ------------------------------------------------------------- retrieval --
# Every page of every file is extracted (app/ingest.py) but fact cards
# summarize; for exact clauses and figures each drafting call also gets the
# verbatim pages that best match what it is writing, from a BM25 keyword
# search over every page in the data room (local, free).

RETRIEVAL_CHARS = 20_000      # ~5.5k tokens of verbatim pages per drafting call
_CHUNK = 3_500
_STOP = set("the a an and or of to in on for by with at from as is are was were be been this that these those it its "
            "their his her our your any all each per not no shall will may must which who whom such other than then "
            "into upon under over between including include includes table section report company".split())
_index_cache: dict = {}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9$%][a-z0-9$%.,/-]*[a-z0-9%]|[a-z0-9]", text.lower()) if t not in _STOP]


def _page_index(job_id):
    """BM25 index over page chunks of the job (built once per worker per
    page count, so the drafting wave shares it)."""
    from rank_bm25 import BM25Okapi

    pages = repo.list_document_pages(job_id)
    key = (str(job_id), len(pages))
    if key in _index_cache:
        return _index_cache[key]
    chunks = []
    for p in pages:
        text = p["text"] or ""
        for start in range(0, max(len(text), 1), _CHUNK):
            piece = text[start:start + _CHUNK].strip()
            if len(piece) > 80:
                chunks.append({"path": p["source_path"] or p["original_filename"], "page": p["page_no"],
                               "label": p["label"], "text": piece})
    index = (BM25Okapi([_tokens(c["text"]) or ["_"] for c in chunks]), chunks) if chunks else (None, [])
    _index_cache.clear()  # one job at a time per worker is the norm; keep memory flat
    _index_cache[key] = index
    return index


MAX_PAGES_PER_FILE = 2


def retrieve_pages(job_id, queries: list[str], budget: int = RETRIEVAL_CHARS, exclude: set | None = None) -> str:
    """Top-matching page chunks for each query (one per subsection being
    written), sharing the budget, at most MAX_PAGES_PER_FILE pages from any
    one file so a single long document can't crowd out the rest. `exclude`:
    file paths already given verbatim (must-include)."""
    try:
        bm25, chunks = _page_index(job_id)
    except Exception:
        return ""
    if bm25 is None or not queries:
        return ""
    per_query = budget // len(queries)
    out, used, seen, per_file = [], 0, set(), {}
    for q in queries:
        scores = bm25.get_scores(_tokens(q))
        q_used = 0
        for i in sorted(range(len(chunks)), key=lambda i: -scores[i])[:40]:
            c = chunks[i]
            if scores[i] <= 0 or (c["path"], c["page"]) in seen or per_file.get(c["path"], 0) >= MAX_PAGES_PER_FILE:
                continue
            if exclude and c["path"] in exclude:
                continue
            if q_used + len(c["text"]) > per_query or used + len(c["text"]) > budget:
                continue
            seen.add((c["path"], c["page"]))
            per_file[c["path"]] = per_file.get(c["path"], 0) + 1
            out.append(f"--- {c['path']}, page {c['page']}{(' (' + c['label'] + ')') if c['label'] else ''} ---\n"
                       f"{c['text']}")
            q_used += len(c["text"])
            used += len(c["text"])
    return "\n\n".join(out)


# --------------------------------------------------------- must-include --
# config/section_must_include.yaml: the documents and clauses each report
# must always receive verbatim (the anti-dilution article, the latest bank
# statements, the board minutes, the offer letters, ...), placed BEFORE the
# BM25 retrieval fills the rest -- the Nov 15 legal section fell back on
# "typical" anti-dilution terms because the clause never reached it.

MUST_INCLUDE_PATH = "config/section_must_include.yaml"
_HEADING = re.compile(r"(?m)^\s*(ARTICLE\b|Article\b|Section \d|SECTION \d|\d{1,2}\.\s+[A-Z]|[A-Z][A-Z &,\-]{8,}$)")


def _must_include_config() -> dict:
    import yaml
    try:
        with open(MUST_INCLUDE_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _current_files(job_id) -> list[dict]:
    files, _ = unique_files(job_id)
    return [f for f in files if not (f.get("digest") or {}).get("superseded_by")]


def _matches(f: dict, pattern: str) -> bool:
    d = f.get("digest") or {}
    hay = f"{f.get('source_path') or ''} {f['original_filename']} {d.get('title', '')} {d.get('document_type', '')}"
    return re.search(pattern, hay, re.I) is not None


_AUTHORITATIVE = re.compile(r"certified|executed|filed|signed|final", re.I)
_PROVISIONAL = re.compile(r"redline|draft|form of|template|unsigned", re.I)


def _newest_first(files: list[dict]) -> list[dict]:
    """Current, executed/certified versions first (redlines and drafts
    last), then newest by year."""
    def key(f):
        d = f.get("digest") or {}
        name = f"{f.get('source_path') or ''} {f['original_filename']}"
        m = re.findall(r"(?:19|20)\d{2}", f"{d.get('document_date', '')} {f['original_filename']}")
        return (0 if _PROVISIONAL.search(name) else 1, 1 if d.get("is_current") else 0,
                1 if _AUTHORITATIVE.search(name) or d.get("execution_status") == "executed" else 0,
                max(m) if m else "0000", f["original_filename"])
    return sorted(files, key=key, reverse=True)


def _clause(text: str, anchor: str, max_chars: int) -> str:
    """The clause containing `anchor`: from the heading before it to the
    next heading, capped."""
    m = re.search(anchor, text, re.I)
    if not m:
        return ""
    heads = [h.start() for h in _HEADING.finditer(text)]
    start = max([h for h in heads if h <= m.start()] or [max(0, m.start() - 300)])
    if m.start() - start > max_chars // 2:
        start = max(0, m.start() - 300)
    nxt = [h for h in heads if h > m.end() + 200]
    end = min(nxt[0] if nxt else len(text), start + max_chars)
    return text[start:end].strip()


def must_include(job_id, report_key: str) -> tuple[str, int]:
    """Verbatim required material for one report and its size in chars."""
    cfg = _must_include_config().get(report_key) or {}
    if not cfg:
        return "", 0
    budget = int(cfg.get("budget", 20_000))
    files = _current_files(job_id)
    parts, used, seen = [], 0, set()

    def add(label: str, text: str) -> None:
        nonlocal used
        text = text.strip()
        if not text or used >= budget:
            return
        text = text[: budget - used]
        parts.append(f"--- {label} ---\n{text}")
        used += len(text)

    for rule in cfg.get("clauses") or []:
        docs = _newest_first([f for f in files if _matches(f, rule["document"])])
        for anchor in rule.get("anchors") or []:
            for f in docs:
                clause = _clause(file_text(f), anchor, int(rule.get("max_chars", 3000)))
                if clause and (f["id"], clause[:80]) not in seen:
                    seen.add((f["id"], clause[:80]))
                    add(f"{_label(f)} -- clause matching '{anchor}' (verbatim)", clause)
                    break
    for rule in cfg.get("documents") or []:
        docs = _newest_first([f for f in files if _matches(f, rule["match"]) and f["id"] not in seen])
        for f in docs[: int(rule.get("max_docs", 2))]:
            seen.add(f["id"])
            text = file_text(f)
            if extract.is_placeholder(text):
                continue
            add(f"{_label(f)} (verbatim excerpt)", text[: int(rule.get("max_chars", 2500))])
    return "\n\n".join(parts), used
