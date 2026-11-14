"""Stage 1: reading the dataroom cheaply enough to afford every file.

Each file gets a compact digest (app.models.CompactDigest) from the fast,
cheap reading model (Haiku), several documents per call. The digests of
the whole room then form the one evidence pack every drafting call shares
from the prompt cache (app/evidence.py), so the reading model decides what
the expensive drafting model sees.

What keeps this cheap, measured on the 405-file CARI Health room (6.4M
characters of text after de-duplication):
- duplicates are never read twice: identical bytes are rejected at upload
  (app/main.py), identical text is copied from the first digest here;
- each file is read up to a cap that shrinks as the room grows (so a
  60-file room is read in full and a 400-file room still fits the budget),
  weighted towards the documents that matter (financing documents,
  spreadsheets, agreements) and away from routine ones (bank statements,
  certificates, NDAs);
- long documents are read as opening + ending (parties, terms, summary
  sheet; signatures, dates, schedules) rather than an arbitrary prefix;
- scanned PDFs send at most 5 page images;
- digests are short for routine documents and long only for important ones.
"""
import re

from app import claude_client, evidence, extract, repo, storage

READ_BUDGET_CHARS = 1_200_000   # whole-room target across all files
MIN_BASE_CAP = 3_000
MAX_BASE_CAP = 30_000
MAX_FILE_CAP = 60_000
CALL_CHARS = 90_000             # text per reading call (~25k tokens)
MAX_DOCS_PER_CALL = 16
FILES_PER_TASK = 8

_SPREADSHEET = (".xlsx", ".xlsm", ".csv")
_ROUTINE = re.compile(
    r"bank[ _-]?statement|statement[ _-]?\d|invoice|receipt|\bw-?9\b|\bw-?2\b|1099|certificate of (good standing|"
    r"insurance|incorporation)|good standing|insurance|handbook|\bpolicy\b|\bnda\b|non-?disclosure|"
    r"confidentiality agreement|tax return|\bform [0-9]|logo|headshot|photo",
    re.I,
)
# The handful of documents a report is built on: read deeply.
_CORE = re.compile(
    r"term[ _-]?sheet|cap(italization)?[ _-]?table|ledger|financial[ _-]?(statement|model|projection)|"
    r"\bmodel\b|projection|forecast|budget|p&l|income[ _-]?statement|balance[ _-]?sheet|cash[ _-]?flow|"
    r"valuation|409a|pitch|deck|executive[ _-]?summary|investor[ _-]?(presentation|update)|business[ _-]?plan|"
    r"study[ _-]?report|clinical[ _-]?study|validation[ _-]?report|510\(?k|de[ _-]?novo|pre-?sub|\bfda\b|"
    r"reimburs|market[ _-]?(research|analysis|study|assessment)|competit|project[ _-]?plan|"
    r"license[ _-]?agreement|licence[ _-]?agreement|purchase[ _-]?agreement",
    re.I,
)
# Material, but usually a standard form whose terms are on the first pages
# (notes, consents, offer letters, consulting agreements).
_KEY = re.compile(
    r"convertible|\bnote\b|safe\b|warrant|subscription|license|licence|\bloi\b|\bmou\b|"
    r"letter of (intent|support)|offer[ _-]?letter|employment|patent|regulat|clinical|study|protocol|"
    r"validation|verification|design|plan|pricing|market|customer|supplier|supply|manufactur|option|grant|"
    r"charter|certificate of amendment|restated|agreement",
    re.I,
)


def read_cap(filename: str, source_path: str | None, n_files: int) -> int:
    """How many characters of this file the reading model sees. The tier
    comes from the filename (folder names like "Financial/" would make every
    bank statement look central); the folder only marks routine material."""
    base = min(max(READ_BUDGET_CHARS // max(n_files, 1), MIN_BASE_CAP), MAX_BASE_CAP)
    if filename.lower().endswith(_SPREADSHEET) or _CORE.search(filename):
        weight = 3.0
    elif _ROUTINE.search(f"{source_path or ''} {filename}"):
        weight = 0.6
    elif _KEY.search(filename):
        weight = 1.3
    else:
        weight = 1.0
    return int(min(max(base * weight, 2_000), MAX_FILE_CAP))


def file_path_label(f: dict) -> str:
    return f.get("source_path") or f["original_filename"]


def _fallback_digest(f: dict, reason: str, text_key: str | None = None, text_hash: str | None = None) -> dict:
    digest = {
        "document_type": "unreadable" if text_key is None else "not digested",
        "title": f["original_filename"],
        "document_date": "",
        "parties": [],
        "execution_status": "unclear",
        "tags": [],
        "importance": 2,
        "summary": f"This document could not be read automatically ({reason[:200]}). It must be reviewed manually.",
        "key_facts": [],
        "issues": ["Not machine-readable; content not reflected in the report."],
        "fallback": True,
    }
    if text_key:
        digest["text_key"] = text_key
        digest["summary"] = f"Digest failed ({reason[:200]}); the extracted text is still available."
    if text_hash:
        digest["text_hash"] = text_hash
    return digest


def _to_digest(c, f: dict, meta: dict) -> dict:
    return {
        "document_type": c.document_type,
        "title": f["original_filename"],
        "document_date": c.document_date,
        "parties": c.parties,
        "execution_status": c.execution_status,
        "tags": list(dict.fromkeys(c.tags)),
        "importance": max(1, min(5, c.importance)),
        "summary": c.summary,
        "key_facts": [{"fact": k, "location": ""} for k in c.key_facts],
        "issues": c.issues,
        **meta,
    }


def _groups(docs: list[dict]) -> list[list[dict]]:
    groups, current, size = [], [], 0
    for d in sorted(docs, key=lambda d: -len(d["excerpt"])):
        n = len(d["excerpt"])
        if current and (size + n > CALL_CHARS or len(current) >= MAX_DOCS_PER_CALL):
            groups.append(current)
            current, size = [], 0
        current.append(d)
        size += n
    if current:
        groups.append(current)
    return groups


def read_files(job_id, company_name: str, file_ids: list[str], final_attempt: bool) -> dict:
    """Digest every file in `file_ids` that doesn't have a digest yet.
    Digests are saved as each call returns, so a retry only redoes what
    failed. Raises (to retry) on API errors unless `final_attempt`, when
    the affected files get a fallback digest instead -- Key Facts depends
    on every reading task, so a reading task must always end up complete."""
    n_files = repo.count_dataroom_files(job_id)
    text_docs, visual_docs = [], []
    stats = {"read": 0, "visual": 0, "duplicates": 0, "unreadable": 0, "failed": 0}

    for file_id in file_ids:
        f = repo.get_dataroom_file(file_id)
        if f is None or f.get("digest"):
            continue
        data = storage.get_bytes(f["object_key"])
        text = extract.extract_text(f["original_filename"], data)
        has_text = not extract.is_placeholder(text)
        visual = extract.visual_source(f["original_filename"], data, text if has_text else "")
        if visual is None and not has_text:
            repo.set_file_digest(file_id, _fallback_digest(f, text))
            stats["unreadable"] += 1
            continue

        th = extract.text_hash(text) if has_text and len(text.strip()) > 200 else None
        if th:
            twin = repo.find_digest_by_text_hash(job_id, th, file_id)
            if twin is not None:
                repo.set_file_digest(file_id, {**twin["digest"], "duplicate_of": str(twin["id"]),
                                               "duplicate_of_name": twin["original_filename"]})
                stats["duplicates"] += 1
                continue

        key = None
        if has_text:
            key = evidence.text_key(job_id, file_id)
            storage.put_bytes(key, text.encode("utf-8"), content_type="text/plain; charset=utf-8")
        doc = {"file": f, "text_key": key, "text_hash": th, "text_chars": len(text) if has_text else 0}
        if visual is not None:
            visual_docs.append({**doc, "visual": visual})
        else:
            cap = read_cap(f["original_filename"], f.get("source_path"), n_files)
            doc["excerpt"] = extract.reading_excerpt(text, cap)
            text_docs.append(doc)

    def meta(d: dict, **extra) -> dict:
        m = {"text_chars": d["text_chars"], "read_model": claude_client.READ_MODEL, **extra}
        if d["text_key"]:
            m["text_key"] = d["text_key"]
        if d["text_hash"]:
            m["text_hash"] = d["text_hash"]
        return m

    errors = []
    for group in _groups(text_docs):
        try:
            out = claude_client.read_documents(company_name, [
                (i + 1, file_path_label(d["file"]), d["file"]["original_filename"], d["excerpt"], d["text_chars"])
                for i, d in enumerate(group)
            ])
            by_doc = {c.doc: c for c in out.documents}
        except Exception as exc:
            if not final_attempt:
                errors.append(exc)
                continue
            by_doc = {}
            reason = str(exc)
        else:
            reason = "the reading model returned no entry for this document"
        for i, d in enumerate(group):
            c = by_doc.get(i + 1)
            if c is not None:
                repo.set_file_digest(str(d["file"]["id"]), _to_digest(c, d["file"], meta(d, read_chars=len(d["excerpt"]))))
                stats["read"] += 1
            elif final_attempt:
                repo.set_file_digest(str(d["file"]["id"]), _fallback_digest(d["file"], reason, d["text_key"], d["text_hash"]))
                stats["failed"] += 1
            else:
                errors.append(RuntimeError(f"no digest returned for {d['file']['original_filename']}"))

    for d in visual_docs:
        try:
            c = claude_client.read_visual_document(company_name, file_path_label(d["file"]),
                                                   d["file"]["original_filename"], d["visual"])
        except Exception as exc:
            if not final_attempt:
                errors.append(exc)
                continue
            repo.set_file_digest(str(d["file"]["id"]), _fallback_digest(d["file"], str(exc), d["text_key"], d["text_hash"]))
            stats["failed"] += 1
            continue
        repo.set_file_digest(str(d["file"]["id"]), _to_digest(c, d["file"], meta(
            d, read_visually=True, pages_read=d["visual"].get("_pages_sent"),
            pages_total=d["visual"].get("_pages_total"))))
        stats["visual"] += 1

    if errors:
        raise RuntimeError(f"{len(errors)} reading call(s) failed; retrying the rest: {errors[0]}")
    return stats
