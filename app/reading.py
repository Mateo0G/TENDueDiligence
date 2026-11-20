"""Stage 1: ingest the data room once -- every page extracted locally, one
fact card per document from the cheap model -- and cache the result by file
hash so it is never paid for twice.

Per file (app/ingest.py does the extraction):
1. Cross-job cache by sha256: a file seen before (same bytes) reuses its
   pages and fact card at no cost.
2. Every page is extracted: text layer, else local OCR, else -- only for
   pages OCR can't read -- the vision model, one page at a time, capped per
   data room (VISION_PAGE_LIMIT). Pages stored in document_pages; coverage
   (pages extracted vs. pages present) recorded on the file.
3. Execution status from signature evidence; a signature page that is
   unclear is checked visually (one page, capped).
4. Fact card (FactCard) from the reading model, at hybrid depth: every
   page of legal, financial, IP, HR, regulatory and investor documents (in
   ~9k-token chunks); first, last and signature pages of routine material
   (bank statements, certificates, insurance, invoices). A page whose text
   is identical to one already read in this data room (the same note,
   warrant or consent form signed by different investors) is not re-read.
"""
import hashlib
import json
import logging
import os
import re
import time

from app import claude_client, evidence, ingest, repo, storage

log = logging.getLogger("reading")

CARD_VERSION = 3                 # bump when the fact-card prompt/schema/model changes (3: typed rosters)
CHUNK_CHARS = 32_000             # ~9k tokens per chunk of a long document
CALL_CHARS = 90_000              # text per reading call
MAX_DOCS_PER_CALL = 12
FILES_PER_TASK = 6
SAMPLE_CHARS = 12_000            # routine documents: first/last/signature pages up to this
LONG_DOC_PAGES = int(os.environ.get("LONG_DOC_PAGES", "25"))  # longer documents are summarized from selected pages
LONG_HEAD_PAGES = max(LONG_DOC_PAGES // 2, 3)
LONG_TAIL_PAGES = max(LONG_DOC_PAGES // 6, 1)
VISION_PAGE_LIMIT = int(os.environ.get("VISION_PAGE_LIMIT", "60"))          # per data room
SIGNATURE_VISION_LIMIT = int(os.environ.get("SIGNATURE_VISION_LIMIT", "40"))  # per data room
# Fact cards via the Message Batches API (half price; results usually in minutes).
READ_BATCH = os.environ.get("READ_BATCH", "1") != "0"
BATCH_POLL_SECONDS = 30
BATCH_MAX_WAIT_SECONDS = int(os.environ.get("BATCH_MAX_WAIT_SECONDS", str(90 * 60)))

_ROUTINE = re.compile(
    r"bank[ _-]?statement|statement[ _-]?\d|invoice|receipt|\bw-?9\b|\bw-?2\b|1099|certificate of (good standing|"
    r"insurance)|good standing|insurance|handbook|\bpolicy\b|\bnda\b|non-?disclosure|confidentiality agreement|"
    r"logo|headshot|photo|brochure",
    re.I,
)


def file_path_label(f: dict) -> str:
    return f.get("source_path") or f["original_filename"]


def read_mode(f: dict) -> str:
    """'full' (every page to the fact-card model) or 'sample'."""
    return "sample" if _ROUTINE.search(f"{f.get('source_path') or ''} {f['original_filename']}") else "full"


_STAMP = re.compile(r"(?im)^.*(envelope id|docusign|carta|page \d+ of \d+).*$")


def content_hash(text: str) -> str:
    """Identity of a page's (or document's) content, ignoring what differs
    between otherwise identical copies: e-signature envelope stamps on every
    page, page numbers, and dates or amounts typed into a form. Seven
    warrants on one form, or a signed and an unsigned copy of the same
    agreement, then share their boilerplate pages instead of each being read."""
    t = _STAMP.sub("", text.lower())
    t = re.sub(r"\d+", "", t)
    return hashlib.md5(re.sub(r"\W+", "", t).encode()).hexdigest()


def _page_dict(p: ingest.Page) -> dict:
    text = (p.text or "").replace("\x00", "")
    return {"no": p.no, "label": p.label, "method": p.method, "text": text, "confidence": p.confidence,
            "is_signature": p.is_signature, "text_hash": content_hash(text) if text.strip() else None}


_DENSE = re.compile(
    r"\$\s?\d|\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?\s?%|\b(?:19|20)\d{2}\b|authoriz|assign|record|reel|frame|convert|"
    r"valuation|vest|salary|option|warrant|anti-?dilution|liquidation|protective|waive|exercise price|"
    r"principal|interest|maturity|revenue|ebitda|scenario",
    re.I,
)


def _density(text: str) -> float:
    return len(_DENSE.findall(text)) / max(len(text), 500) * 1000


def _select_pages(pages: list[dict], mode: str) -> list[dict]:
    """Which pages the fact-card model reads (all pages are extracted and
    searchable either way). Routine documents: first two, last, signature
    pages. Documents over LONG_DOC_PAGES: the opening and closing pages,
    signature pages, and the pages densest in figures and key terms."""
    sig = {p["no"] for p in pages if p["is_signature"]}
    if mode == "sample":
        if len(pages) <= 3:
            return pages
        keep = {pages[0]["no"], pages[1]["no"], pages[-1]["no"], *sig}
        return [p for p in pages if p["no"] in keep]
    if len(pages) <= LONG_DOC_PAGES:
        return pages
    keep = {p["no"] for p in pages[:LONG_HEAD_PAGES]} | {p["no"] for p in pages[-LONG_TAIL_PAGES:]} | sig
    rest = sorted((p for p in pages if p["no"] not in keep), key=lambda p: -_density(p["text"]))
    for p in rest:
        if len(keep) >= LONG_DOC_PAGES:
            break
        keep.add(p["no"])
    return [p for p in pages if p["no"] in keep]


def _model_text(f: dict, pages: list[dict], seen: dict, mode: str) -> tuple[str, int]:
    """The text the fact-card model reads, with [page N] markers; repeated
    pages replaced by a pointer. Returns (text, pages included)."""
    total = len(pages)
    pages = _select_pages(pages, mode)
    if len(pages) < total:
        skipped = total - len(pages)
        note = (f"[{skipped} of this document's {total} pages are not shown here: they were extracted and are "
                f"searchable, but only the opening, closing, signature and most fact-dense pages are summarized]")
        pages = pages + [{"no": total + 1, "label": "", "text": note, "text_hash": None, "is_signature": False}]
    out, used, n = [], 0, 0
    for p in pages:
        head = f"[page {p['no']}{(' -- ' + p['label']) if p['label'] else ''}]"
        h = p["text_hash"]
        if h and len(p["text"]) > 200 and h in seen and seen[h][0] != f["original_filename"]:
            out.append(f"{head} (identical to {seen[h][0]} page {seen[h][1]}; read there)")
            continue
        if h and len(p["text"]) > 200:
            seen.setdefault(h, (f["original_filename"], p["no"]))
        if mode == "sample" and used + len(p["text"]) > SAMPLE_CHARS:
            out.append(f"{head} [...]")
            continue
        out.append(f"{head}\n{p['text']}")
        used += len(p["text"])
        n += 1
    return "\n".join(out), n


def _chunks(text: str) -> list[str]:
    """Split on page markers into pieces of at most ~CHUNK_CHARS."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    pieces = re.split(r"(?m)^(?=\[page \d+)", text)
    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + len(piece) > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        while len(piece) > CHUNK_CHARS:  # one enormous page (a big spreadsheet sheet)
            chunks.append(piece[:CHUNK_CHARS])
            piece = piece[CHUNK_CHARS:]
        cur += piece
    if cur:
        chunks.append(cur)
    return chunks


def _merge(cards: list) -> dict:
    """One fact card from the cards of a document's chunks."""
    first = cards[0]
    merged = first.model_dump()
    for c in cards[1:]:
        d = c.model_dump()
        for k in ("parties", "signatories", "key_terms", "key_numbers", "dates_and_deadlines", "ip_items",
                  "page_refs", "report_tags", "issues"):
            merged[k] = list(dict.fromkeys(merged[k] + d[k]))
        for k in ("people", "agreements", "performance_metrics", "ip_events"):
            have = {json.dumps(x, sort_keys=True) for x in merged[k]}
            merged[k] += [x for x in d[k] if json.dumps(x, sort_keys=True) not in have]
        for k in ("doc_type", "title", "date"):
            merged[k] = merged[k] or d[k]
        merged["importance"] = max(merged["importance"], d["importance"])
        if d["executed"] == "true" or merged["executed"] == "unclear":
            merged["executed"] = d["executed"] if d["executed"] != "not_applicable" else merged["executed"]
        if len(merged["summary"]) < 600 and d["summary"]:
            merged["summary"] = (merged["summary"] + " " + d["summary"]).strip()
    merged.pop("doc", None)
    return merged


_EXEC_TO_STATUS = {"true": "executed", "false": "unsigned_or_draft", "unclear": "unclear",
                   "not_applicable": "not_applicable"}


def card_to_digest(card: dict, f: dict, meta: dict) -> dict:
    """The digest stored on the file (what the evidence pack renders): the
    fact card plus the fields older code reads."""
    facts = card["key_terms"] + card["key_numbers"] + card["dates_and_deadlines"] + card["ip_items"]
    executed = card.get("executed", "unclear")
    local = meta.get("executed_local")
    if local == "yes" and executed in ("unclear", "false"):
        executed = "true"
    return {
        "doc_id": str(f["id"]),
        "file_name": f["original_filename"],
        "document_type": card["doc_type"],
        "title": card["title"] or f["original_filename"],
        "document_date": card["date"],
        "parties": card["parties"],
        "execution_status": _EXEC_TO_STATUS.get(executed, "unclear"),
        "signatories": card["signatories"],
        "tags": list(dict.fromkeys(card["report_tags"])),
        "importance": max(1, min(5, int(card["importance"]))),
        "summary": card["summary"],
        "key_facts": [{"fact": x, "location": ""} for x in facts],
        "people_mentioned": card.get("people_mentioned") or [
            f"{p['name']} -- {p['role']}" for p in card.get("people") or []],
        "people": card.get("people") or [],
        "agreements": card.get("agreements") or [],
        "performance_metrics": card.get("performance_metrics") or [],
        "ip_events": card.get("ip_events") or [],
        "page_refs": card["page_refs"],
        "issues": card["issues"],
        "fact_card": card,
        **meta,
    }


def _fallback_digest(f: dict, reason: str, meta: dict) -> dict:
    return {
        "document_type": "unreadable" if not meta.get("text_key") else "not digested",
        "title": f["original_filename"], "document_date": "", "parties": [], "execution_status": "unclear",
        "tags": [], "importance": 2,
        "summary": f"This document could not be read automatically ({reason[:200]}). It must be reviewed manually.",
        "key_facts": [], "issues": ["Not machine-readable; content not reflected in the report."],
        "fallback": True, "extraction_error": reason[:300], **meta,
    }


def _render_page_png(data: bytes, page_no: int) -> bytes | None:
    try:
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        return ingest.render(doc[page_no - 1], 150).tobytes("png")
    except Exception:
        return None


def _extract(job_id, f: dict, data: bytes) -> tuple[ingest.Extraction | None, list[dict], str, list[int], str]:
    """Pages for one file (cached or freshly extracted, with the vision
    fallback applied). Returns (extraction or None if cached, page dicts,
    kind, signature pages, executed_local)."""
    ext = ingest.extract_document(f["original_filename"], data)
    path = file_path_label(f)
    for p in ext.pages:
        if not p.needs_vision:
            continue
        if repo.count_api_calls(job_id, "read_visual") >= VISION_PAGE_LIMIT:
            log.warning("vision page limit reached; %s page %s left unread", path, p.no)
            continue
        try:
            text = claude_client.transcribe_page(p.image_png, path, p.no)
        except Exception as exc:
            log.warning("vision transcription failed for %s page %s: %s", path, p.no, exc)
            continue
        if text.strip():
            p.text, p.method = (p.text + "\n" + text).strip(), "vision"
            p.is_signature = p.is_signature or "[signed" in text.lower()
        else:
            p.method = "blank"
    executed, sig_pages = ingest.execution_status(f["original_filename"], ext.pages)
    if executed == "unclear" and sig_pages and ext.kind == "pdf" \
            and repo.count_api_calls(job_id, "read_signature") < SIGNATURE_VISION_LIMIT:
        png = _render_page_png(data, sig_pages[-1])
        if png:
            try:
                verdict = claude_client.check_signature_page(png, path, sig_pages[-1])
                executed = verdict if verdict in ("yes", "no") else "unclear"
            except Exception as exc:
                log.warning("signature check failed for %s: %s", path, exc)
    return ext, [_page_dict(p) for p in ext.pages], ext.kind, sig_pages, executed


def _prepare(job_id, file_ids: list[str], stats: dict) -> list[dict]:
    """Extract (or load from cache) every not-yet-read file, store its
    pages and coverage, settle cached/duplicate/unreadable files directly,
    and return the rest as fact-card work: dicts with file_id, path, sha,
    kind, page_count, executed, meta, chunks."""
    seen = repo.job_page_hashes(job_id)
    todo = []
    for file_id in file_ids:
        f = repo.get_dataroom_file(file_id)
        if f is None or f.get("digest"):
            continue
        data = storage.get_bytes(f["object_key"])
        sha = f.get("sha256") or hashlib.sha256(data).hexdigest()
        cached = repo.get_extraction_cache(sha, ingest.EXTRACT_VERSION)

        error = ""
        if cached is not None:
            pages, kind, executed, page_count = cached["pages"], cached["kind"], cached["executed"], cached["page_count"]
        else:
            ext, pages, kind, _, executed = _extract(job_id, f, data)
            page_count, error = ext.page_count or len(pages) or 1, ext.error
            # Cache the pages now, so a retry never repeats OCR or vision calls.
            if not error:
                repo.put_extraction_cache(sha, ingest.EXTRACT_VERSION, kind, page_count, pages, executed, None, None)
        extracted = sum(1 for p in pages if p["method"] in ("text", "ocr", "vision", "blank", "native"))
        methods = sorted({p["method"] for p in pages})
        repo.replace_document_pages(job_id, file_id, pages)
        repo.set_file_extraction(file_id, page_count, extracted, ",".join(methods) or "none", executed)
        stats["pages"] += page_count
        stats["pages_extracted"] += extracted
        stats["ocr"] += sum(p["method"] == "ocr" for p in pages)
        stats["vision"] += sum(p["method"] == "vision" for p in pages)

        full_text = "\n".join(f"[page {p['no']}{(' -- ' + p['label']) if p['label'] else ''}]\n{p['text']}"
                              for p in pages if p["text"].strip())
        meta = {"pages_total": page_count, "pages_extracted": extracted, "page_methods": methods,
                "executed_local": executed, "text_chars": len(full_text), "read_model": claude_client.READ_MODEL}
        if error:
            meta["extraction_error"] = error
        if full_text.strip():
            key = evidence.text_key(job_id, file_id)
            storage.put_bytes(key, full_text.encode("utf-8"), content_type="text/plain; charset=utf-8")
            meta["text_key"] = key
            meta["text_hash"] = content_hash(full_text)

        if cached is not None and cached.get("card") and cached.get("card_version") == CARD_VERSION:
            repo.set_file_digest(file_id, card_to_digest(cached["card"], f, {**meta, "from_cache": True}))
            stats["cached"] += 1
            continue
        if not full_text.strip():
            repo.set_file_digest(file_id, _fallback_digest(f, error or "no readable text on any page", meta))
            stats["unreadable"] += 1
            continue
        if meta.get("text_hash") and len(full_text) > 300:
            twin = repo.find_digest_by_text_hash(job_id, meta["text_hash"], file_id)
            if twin is not None:
                repo.set_file_digest(file_id, {**twin["digest"], **meta, "duplicate_of": str(twin["id"]),
                                               "duplicate_of_name": twin["original_filename"]})
                stats["duplicates"] += 1
                continue
        mode = read_mode(f)
        text, n_read = _model_text(f, pages, seen, mode)
        meta.update({"read_mode": mode, "pages_read": n_read})
        todo.append({"file_id": str(file_id), "path": file_path_label(f), "sha": sha, "kind": kind,
                     "page_count": page_count, "executed": executed, "meta": meta, "chunks": _chunks(text)})
    return todo


def _groups(todo: list[dict]) -> list[list[list]]:
    """Chunks of all files packed into as few calls as possible; each entry
    is [todo index, chunk index, text]."""
    entries = [[t, i, c] for t, d in enumerate(todo) for i, c in enumerate(d["chunks"])]
    groups, cur, size = [], [], 0
    for e in sorted(entries, key=lambda e: -len(e[2])):
        if cur and (size + len(e[2]) > CALL_CHARS or len(cur) >= MAX_DOCS_PER_CALL):
            groups.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += len(e[2])
    if cur:
        groups.append(cur)
    return groups


def _group_entries(todo: list[dict], group: list[list]) -> list[tuple]:
    out = []
    for n, (t, i, text) in enumerate(group):
        d = todo[t]
        note = f"part {i + 1} of {len(d['chunks'])}" if len(d["chunks"]) > 1 else ""
        out.append((n + 1, d["path"], note, text))
    return out


def _absorb(cards: dict, group: list[list], out) -> None:
    by_num = {c.doc: c for c in out.cards}
    for n, (t, i, _) in enumerate(group):
        if (n + 1) in by_num:
            cards.setdefault(t, {})[i] = by_num[n + 1]


def _cards_sync(company_name: str, todo: list[dict], groups: list, cards: dict, errors: list) -> None:
    for group in groups:
        try:
            _absorb(cards, group, claude_client.read_fact_cards(company_name, _group_entries(todo, group)))
        except Exception as exc:
            errors.append(exc)


def _finalize(job_id, todo: list[dict], cards: dict, errors: list, final_attempt: bool, stats: dict) -> dict:
    for t, d in enumerate(todo):
        got = cards.get(t, {})
        f = repo.get_dataroom_file(d["file_id"])
        if f is None or f.get("digest"):
            continue
        if len(got) < len(d["chunks"]):
            if not final_attempt:
                continue  # the task is retried
            if not got:
                repo.set_file_digest(d["file_id"], _fallback_digest(
                    f, str(errors[0]) if errors else "the reading model returned no card", d["meta"]))
                stats["failed"] += 1
                continue
        card = _merge([got[i] for i in sorted(got)])
        repo.set_file_digest(d["file_id"], card_to_digest(card, f, d["meta"]))
        cached = repo.get_extraction_cache(d["sha"], ingest.EXTRACT_VERSION)
        repo.put_extraction_cache(d["sha"], ingest.EXTRACT_VERSION, d["kind"], d["page_count"],
                                  cached["pages"] if cached else [], d["executed"], card, CARD_VERSION)
        stats["read"] += 1
    missing = [d for t, d in enumerate(todo) if len(cards.get(t, {})) < len(d["chunks"])]
    if missing and not final_attempt:
        raise RuntimeError(f"{len(missing)} file(s) not fully read; retrying" + (f": {errors[0]}" if errors else ""))
    return stats


def _new_stats() -> dict:
    return {"cached": 0, "read": 0, "duplicates": 0, "unreadable": 0, "failed": 0,
            "pages": 0, "pages_extracted": 0, "ocr": 0, "vision": 0}


def read_files(job_id, company_name: str, file_ids: list[str], final_attempt: bool, task: dict | None = None) -> dict:
    """Ingest every file in `file_ids` that has no digest yet.

    With READ_BATCH on (the default) and a task to defer, the fact cards go
    to the Message Batches API at half price: the task submits the batch,
    saves what it needs to storage, and defers itself; each later run
    checks the batch and, once it has ended, writes the cards (any request
    that failed is redone synchronously). A batch still running after
    BATCH_MAX_WAIT_SECONDS is cancelled and finished synchronously, so a
    slow batch can delay reading but never stall it.

    Raises (so the task retries) on API errors unless `final_attempt`, when
    affected files get a fallback digest -- Key Facts depends on every
    reading task, so a reading task must always end up complete."""
    from app.tasks.registry import TaskDeferred

    payload = (task or {}).get("payload") or {}
    if payload.get("batch"):
        return _collect(job_id, company_name, payload["batch"], final_attempt)

    stats = _new_stats()
    todo = _prepare(job_id, file_ids, stats)
    groups = _groups(todo)
    if not groups:
        return stats
    if READ_BATCH and task is not None and not final_attempt:
        key = f"batches/{job_id}/{task['id']}.json"
        storage.put_bytes(key, json.dumps({"todo": todo, "groups": groups, "stats": stats}).encode("utf-8"),
                          content_type="application/json")
        batch_id = claude_client.submit_batch([
            (f"g{n}", claude_client.build_fact_card_request(company_name, _group_entries(todo, g)))
            for n, g in enumerate(groups)])
        raise TaskDeferred(BATCH_POLL_SECONDS, {**payload, "batch": {"id": batch_id, "key": key,
                                                                     "submitted": time.time()}},
                           reason=f"fact cards submitted as batch {batch_id}")
    cards, errors = {}, []
    _cards_sync(company_name, todo, groups, cards, errors)
    return _finalize(job_id, todo, cards, errors, final_attempt, stats)


def _collect(job_id, company_name: str, batch: dict, final_attempt: bool) -> dict:
    from app.tasks.registry import TaskDeferred

    spec = json.loads(storage.get_bytes(batch["key"]).decode("utf-8"))
    todo, groups, stats = spec["todo"], spec["groups"], spec["stats"]
    status = claude_client.batch_status(batch["id"])
    timed_out = time.time() - batch["submitted"] > BATCH_MAX_WAIT_SECONDS
    if status != "ended" and not timed_out and not final_attempt:
        raise TaskDeferred(BATCH_POLL_SECONDS, reason=f"batch {batch['id']} {status}")

    cards, errors = {}, []
    if status == "ended":
        results = claude_client.batch_fact_cards(batch["id"])
        redo = []
        for n, g in enumerate(groups):
            out = results.get(f"g{n}")
            if out is None:
                redo.append(g)
                continue
            _absorb(cards, g, out)
            # A reply can come back without some of its documents' cards:
            # re-request just those (the old code waited for retries that
            # re-read the same batch result, then gave up on them).
            missing = [e for e in g if e[1] not in cards.get(e[0], {})]
            if missing:
                redo.append(missing)
    else:
        log.warning("batch %s still %s after %.0f min; finishing synchronously", batch["id"], status,
                    (time.time() - batch["submitted"]) / 60)
        claude_client.cancel_batch(batch["id"])
        redo = groups
    _cards_sync(company_name, todo, redo, cards, errors)
    result = _finalize(job_id, todo, cards, errors, final_attempt, stats)
    try:
        storage.delete(batch["key"])
    except Exception:
        pass
    return {**result, "batch": batch["id"], "batch_redone_groups": len(redo)}
