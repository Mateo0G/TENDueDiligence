"""Stage 1 task: read one dataroom file in full and store its structured
digest (app.models.DocumentDigest) plus a full-text sidecar in storage.

Created as soon as a file is uploaded (app/main.py), so the room is read in
the background. Never blocks the pipeline: on its last attempt a failure
stores a minimal fallback digest instead of failing, because Key Facts
(and so every drafting task) depends on every digest task.
"""
from app import claude_client, evidence, extract, repo, storage
from app.tasks.registry import register

_CATEGORY_TAGS = {
    "financial": ["financial"], "legal_corporate": ["corporate", "deal_terms"], "ip": ["ip"],
    "hr": ["team"], "sales_marketing": ["commercial"], "science_tech": ["science_tech"],
    "contracts": ["contracts"], "regulatory": ["regulatory"],
}


@register("digest_document")
def run_digest_document(task: dict) -> dict:
    payload = task["payload"]
    job_id, file_id = payload["job_id"], payload["file_id"]
    f = repo.get_dataroom_file(file_id)
    if f is None:
        return {"skipped": "file no longer exists"}

    data = storage.get_bytes(f["object_key"])
    text = extract.extract_text(f["original_filename"], data)
    visual = extract.visual_source(f["original_filename"], data, "" if extract.is_placeholder(text) else text)
    key = evidence.text_key(job_id, file_id)
    final_attempt = task["attempts"] + 1 >= repo.MAX_TASK_ATTEMPTS

    try:
        if visual is None and extract.is_placeholder(text):
            raise ValueError(text)  # nothing readable: unsupported type or unreadable file
        digest_input = text[:evidence.MAX_DIGEST_INPUT_CHARS]
        result = claude_client.digest_document(
            payload["company_name"], f["original_filename"], f["object_key"], digest_input, visual
        )
    except Exception as exc:
        if not final_attempt and not isinstance(exc, ValueError):
            raise
        digest = {
            "document_type": "unreadable" if isinstance(exc, ValueError) else "not digested",
            "title": f["original_filename"],
            "document_date": "",
            "parties": [],
            "execution_status": "unclear",
            "tags": _CATEGORY_TAGS.get(f.get("category") or "", []),
            "importance": 2,
            "summary": f"This document could not be read automatically ({exc}). It must be reviewed manually.",
            "key_facts": [],
            "issues": ["Not machine-readable; content not reflected in the report."],
            "fallback": True,
        }
        if not extract.is_placeholder(text):
            storage.put_bytes(key, text.encode("utf-8"), content_type="text/plain; charset=utf-8")
            digest["text_key"] = key
            digest["summary"] = (f"Digest failed ({exc}); the extracted text is still used as evidence.")
        repo.set_file_digest(file_id, digest)
        return {"fallback": True, "error": str(exc)[:300]}

    digest = result.model_dump()
    transcription = digest.pop("transcribed_text", "") or ""
    full_text = text if visual is None else (transcription or text)
    if full_text and not extract.is_placeholder(full_text):
        storage.put_bytes(key, full_text.encode("utf-8"), content_type="text/plain; charset=utf-8")
        digest["text_key"] = key
        digest["text_chars"] = len(full_text)
    digest["read_visually"] = visual is not None
    repo.set_file_digest(file_id, digest)
    return {"tags": digest["tags"], "facts": len(digest["key_facts"]), "read_visually": visual is not None}
