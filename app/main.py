"""FastAPI web service: accepts uploads, exposes job status. Long-running
pipeline work (Claude calls, docx rendering) happens in the separate worker
process (app/worker.py), never on this request/response cycle.
"""
import hashlib
import io
import json
import mimetypes
import uuid
import zipfile
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app import classify, evidence, pipeline, repo, storage
from app.docx_builder import build_report_docx
from app.models import ContentBlock

# Real datarooms arrive as a single zip almost always -- skip macOS/zip
# metadata cruft and directory entries, extract everything else as if it
# had been uploaded individually.
_ZIP_IGNORE_PREFIXES = ("__MACOSX/",)
_ZIP_IGNORE_NAMES = (".DS_Store", "Thumbs.db")


_ZIP_MAX_NESTING = 5  # Google Drive's "download folder as zip" wraps each
# subfolder as its own nested zip inside the outer one -- recurse into those,
# bounded so a malicious/corrupt zip-of-zips can't recurse forever.


def _iter_zip_entries(zip_file_like, _prefix: str = "", _depth: int = 0):
    """Yields (internal_path, bytes) for every real file in a zip archive,
    skipping directories and OS/zip metadata cruft, and recursing into any
    entry that is itself a zip (Google Drive splits large folders into
    per-subfolder zips nested inside the top-level download). A corrupt
    outer zip raises BadZipFile -- the caller turns that into a 400, not a
    500; a corrupt or too-deeply-nested inner zip is yielded as-is instead,
    so one bad nested archive doesn't fail the whole upload.

    zip_file_like is a seekable file object, not raw bytes -- the caller
    passes the uploaded file's own spooled temp file for the top-level
    call (confirmed against a real 245MB dataroom zip: reading the whole
    upload into one bytes blob just to open it, on top of this generator's
    caller previously materializing every extracted entry into a list
    before uploading any of them, pushed peak memory well past what this
    service has and it was OOM-killed -- no app-level log line, just a
    502 at the edge). A nested zip is small enough (one subfolder, not the
    whole dataroom) that io.BytesIO over its already-read bytes is fine.
    """
    with zipfile.ZipFile(zip_file_like) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            basename = name.rsplit("/", 1)[-1]
            if name.startswith(_ZIP_IGNORE_PREFIXES) or basename in _ZIP_IGNORE_NAMES or basename.startswith("."):
                continue
            full_path = f"{_prefix}{name}"
            entry_data = zf.read(info)
            if basename.lower().endswith(".zip") and _depth < _ZIP_MAX_NESTING:
                try:
                    yield from _iter_zip_entries(
                        io.BytesIO(entry_data),
                        f"{full_path.rsplit('/', 1)[0]}/" if "/" in full_path else "",
                        _depth + 1,
                    )
                    continue
                except zipfile.BadZipFile:
                    pass
            yield full_path, entry_data

def _migrate_on_startup() -> None:
    """Apply pending Alembic migrations when the web service boots, so a
    deploy that adds columns (e.g. the digest/key-facts migration) works
    without a separate manual step. A no-op when already at head; only the
    web service does this, never the worker, so two processes never race
    on it. Failure is logged, not fatal -- the existing schema still serves
    everything that doesn't touch the new columns."""
    import logging
    import os

    from alembic import command
    from alembic.config import Config

    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        command.upgrade(Config(os.path.join(root, "alembic.ini")), "head")
    except Exception:
        logging.getLogger("web").exception("alembic upgrade on startup failed")


_migrate_on_startup()

app = FastAPI(title="TEN Due Diligence Report Generator")


@app.get("/")
def serve_ui():
    """The brief's non-goals call for "a simple polling-based status page",
    not a frontend framework -- this is one static file with vanilla JS,
    served directly by the same FastAPI app, no build step or separate
    service.

    no-cache (not no-store): FileResponse already sends an ETag, so this
    doesn't disable caching, it just forces a conditional revalidation
    request on every load instead of letting the browser skip asking
    entirely -- without it, a browser's heuristic freshness (RFC 7234,
    since no Cache-Control was sent at all) can serve a stale copy of this
    page indefinitely after a deploy, which is exactly what happened here.
    """
    return FileResponse("app/static/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/ten-capital-logo.png")
def serve_ten_logo():
    """The UI's own TEN Capital branding (header logo, favicon) -- a single
    named route rather than a full StaticFiles mount, matching serve_ui's
    FileResponse pattern above with no new dependency for one fixed asset."""
    return FileResponse("app/static/ten-capital-logo.png", headers={"Cache-Control": "no-cache"})


class CreateJobRequest(BaseModel):
    company_name: str


class CreateJobResponse(BaseModel):
    id: str
    company_name: str
    status: str


class DataroomFileOut(BaseModel):
    id: str
    original_filename: str
    file_kind: str
    category: str | None
    category_method: str | None
    size_bytes: int | None
    content_type: str | None
    # Filled once the file's digest task has read it (app/tasks/digest_document.py):
    # "read", "read_visually" (scanned, read from page images) or "unreadable".
    digest_status: str | None = None
    tags: list[str] = []
    page_count: int | None = None        # pages (sheets, slides) in the file
    pages_extracted: int | None = None   # of those, how many were read (text, OCR or visually)
    executed: str | None = None          # agreements: yes / no / unclear (signature evidence)


class UploadResult(BaseModel):
    uploaded: list[DataroomFileOut]
    skipped_duplicates: int = 0  # files this job already had, byte for byte


class VerticalResult(BaseModel):
    vertical: str
    signals: dict


class JobOut(BaseModel):
    id: str
    company_name: str
    status: str
    vertical: str | None
    draft_approved_at: datetime | None
    dataroom_files: list[DataroomFileOut]
    has_logo: bool
    api_cost_usd: float = 0.0  # every Claude call made for this job so far (app/usage.py)
    coverage: dict = {}        # pages extracted vs. present, by method; files not read in full


def _require_job(job_id: str) -> dict:
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _to_file_out(row: dict) -> DataroomFileOut:
    return DataroomFileOut(
        id=str(row["id"]),
        original_filename=row["original_filename"],
        file_kind=row["file_kind"],
        category=row["category"],
        category_method=row["category_method"],
        size_bytes=row["size_bytes"],
        content_type=row["content_type"],
        digest_status=_digest_status(row.get("digest")),
        tags=(row.get("digest") or {}).get("tags") or [],
        page_count=row.get("page_count"),
        pages_extracted=row.get("pages_extracted"),
        executed=row.get("executed"),
    )


def _digest_status(digest: dict | None) -> str | None:
    if not digest:
        return None
    if digest.get("duplicate_of"):
        return "duplicate"
    if digest.get("fallback"):
        return "unreadable"
    scanned = digest.get("read_visually") or {"ocr", "vision"} & set(digest.get("page_methods") or [])
    return "read_visually" if scanned else "read"


@app.post("/jobs", response_model=CreateJobResponse)
def create_job(req: CreateJobRequest):
    job_id = repo.create_job(req.company_name)
    job = repo.get_job(job_id)
    return CreateJobResponse(id=str(job["id"]), company_name=job["company_name"], status=job["status"])


class JobSummaryOut(BaseModel):
    id: str
    company_name: str
    status: str
    vertical: str | None
    created_at: datetime


@app.get("/jobs", response_model=list[JobSummaryOut])
def list_jobs():
    """Backs the UI's landing page -- the brief's "simple polling-based
    status page" needs somewhere to list existing jobs, not just look one
    up by id."""
    return [
        JobSummaryOut(
            id=str(j["id"]), company_name=j["company_name"], status=j["status"],
            vertical=j["vertical"], created_at=j["created_at"],
        )
        for j in repo.list_jobs()
    ]


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    """Deletes a job and everything under it -- dataroom files, the pitch
    deck, the logo, any rendered docx, and every task/section/score row
    (the last three cascade in Postgres; R2 objects don't, so those are
    removed here first while the job row -- and the object keys on it --
    still exist). Best-effort on storage: a single missing/already-gone
    object shouldn't block deleting the rest, since the whole point is
    letting someone clean up test jobs without a human re-checking R2."""
    job = _require_job(job_id)
    for f in repo.list_dataroom_files(job_id):
        for key in (f["object_key"], (f.get("digest") or {}).get("text_key")):
            if not key:
                continue
            try:
                storage.delete(key)
            except Exception:
                pass
    for key in (job["logo_key"], job["output_key"], evidence.drafting_context_key(job_id)):
        if key:
            try:
                storage.delete(key)
            except Exception:
                pass
    repo.delete_job(job_id)
    # Explicit Response, not a bare return -- FastAPI stamps a
    # Content-Type: application/json header on an implicit None return even
    # at 204, which has no body at all; a strict client (ours included)
    # then fails trying to JSON-parse an empty string.
    return Response(status_code=204)


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str):
    job = _require_job(job_id)
    files = repo.list_dataroom_files(job_id)
    return JobOut(
        id=str(job["id"]),
        company_name=job["company_name"],
        status=job["status"],
        vertical=job["vertical"],
        draft_approved_at=job["draft_approved_at"],
        dataroom_files=[_to_file_out(f) for f in files],
        has_logo=bool(job["logo_key"]),
        api_cost_usd=round(repo.job_total_cost(job_id), 2),
        coverage=repo.coverage_summary(job_id),
    )


def _store_dataroom_file(job_id: str, classify_path: str, original_filename: str, data: bytes,
                         content_type: str | None) -> DataroomFileOut | None:
    """Shared by both a directly-uploaded file and one extracted from a zip.
    `classify_path` carries the zip's internal folder path (if any) so a
    file like "financial/cap_table.xlsx" gets the benefit of the "financial"
    folder name as a classification signal, same as app/classify.py already
    does for a plain filename -- `original_filename` is just the basename,
    kept clean for display.

    Returns None for a file this job already has byte-for-byte (the same
    zip uploaded twice, or one document filed in several folders): storing
    and reading it again would only double the cost and the evidence."""
    sha256 = hashlib.sha256(data).hexdigest()
    if repo.find_file_by_sha256(job_id, sha256) is not None:
        return None
    if classify.is_pitch_deck_path(classify_path):
        # A dataroom zip that bundles the pitch deck inside it, rather than
        # it being uploaded through the dedicated endpoint -- route it the
        # same way that endpoint does so pipeline.pitch_deck_text actually
        # picks it up (see classify.is_pitch_deck_path's docstring).
        object_key = f"pitch-deck/{job_id}/{uuid.uuid4()}-{original_filename}"
        storage.put_bytes(object_key, data, content_type=content_type)
        file_id = repo.add_dataroom_file(
            job_id=job_id,
            object_key=object_key,
            original_filename=original_filename,
            file_kind="pitch_deck",
            category=None,
            category_method=None,
            size_bytes=len(data),
            content_type=content_type,
            sha256=sha256,
            source_path=classify_path,
        )
        repo.set_job_pitch_deck_key(job_id, object_key)
        return _to_file_out({
            "id": file_id,
            "original_filename": original_filename,
            "file_kind": "pitch_deck",
            "category": None,
            "category_method": None,
            "size_bytes": len(data),
            "content_type": content_type,
        })

    object_key = f"dataroom/{job_id}/{uuid.uuid4()}-{original_filename}"
    storage.put_bytes(object_key, data, content_type=content_type)

    # Keyword category: a display label only until the file is read (its
    # digest then tags it for every report it is evidence for). No LLM call
    # here any more -- it ran inside the upload request, one file at a time.
    category, method = classify.categorize_file(classify_path)
    file_id = repo.add_dataroom_file(
        job_id=job_id,
        object_key=object_key,
        original_filename=original_filename,
        file_kind="dataroom",
        category=category,
        category_method=method,
        size_bytes=len(data),
        content_type=content_type,
        sha256=sha256,
        source_path=classify_path,
    )
    return _to_file_out({
        "id": file_id,
        "original_filename": original_filename,
        "file_kind": "dataroom",
        "category": category,
        "category_method": method,
        "size_bytes": len(data),
        "content_type": content_type,
    })


@app.post("/jobs/{job_id}/dataroom", response_model=UploadResult)
async def upload_dataroom_files(job_id: str, files: list[UploadFile]):
    """Accepts plain documents and/or .zip archives, mixed freely in one
    upload -- real datarooms almost always arrive as a single zip, so each
    .zip is transparently expanded and every entry inside it is stored and
    classified exactly as if it had been uploaded on its own."""
    job = _require_job(job_id)
    uploaded, skipped = [], 0

    def store(path, name, data, content_type):
        nonlocal skipped
        out = _store_dataroom_file(job_id, path, name, data, content_type)
        if out is None:
            skipped += 1
        else:
            uploaded.append(out)

    for f in files:
        if f.filename.lower().endswith(".zip"):
            # Stream straight from the upload's own spooled temp file and
            # store each entry as it's yielded, instead of reading the
            # whole upload into memory and then materializing every
            # extracted file into a list before storing any of them --
            # peak memory now tracks the largest single file, not the
            # whole (possibly multi-hundred-MB) dataroom at once.
            f.file.seek(0)
            try:
                for internal_path, entry_data in _iter_zip_entries(f.file):
                    basename = internal_path.rsplit("/", 1)[-1]
                    store(internal_path, basename, entry_data, mimetypes.guess_type(basename)[0])
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail=f"{f.filename} is not a valid zip file")
        else:
            store(f.filename, f.filename, await f.read(), f.content_type)
    # Start reading every file now, in the background, so digests are
    # (mostly) done by the time Stage 2 is started.
    pipeline.create_digest_tasks(job_id, job["company_name"], [u.id for u in uploaded])
    return UploadResult(uploaded=uploaded, skipped_duplicates=skipped)


@app.post("/jobs/{job_id}/pitch-deck", response_model=DataroomFileOut)
async def upload_pitch_deck(job_id: str, file: UploadFile):
    job = _require_job(job_id)
    data = await file.read()
    sha256 = hashlib.sha256(data).hexdigest()
    existing = repo.find_file_by_sha256(job_id, sha256)
    if existing is not None:
        return _to_file_out(existing)  # already uploaded (e.g. it was inside the dataroom zip)
    object_key = f"pitch-deck/{job_id}/{uuid.uuid4()}-{file.filename}"
    storage.put_bytes(object_key, data, content_type=file.content_type)

    file_id = repo.add_dataroom_file(
        job_id=job_id,
        object_key=object_key,
        original_filename=file.filename,
        file_kind="pitch_deck",
        category=None,
        category_method=None,
        size_bytes=len(data),
        content_type=file.content_type,
        sha256=sha256,
        source_path=file.filename,
    )
    repo.set_job_pitch_deck_key(job_id, object_key)
    pipeline.create_digest_tasks(job_id, job["company_name"], [file_id])
    row = {
        "id": file_id,
        "original_filename": file.filename,
        "file_kind": "pitch_deck",
        "category": None,
        "category_method": None,
        "size_bytes": len(data),
        "content_type": file.content_type,
    }
    return _to_file_out(row)


class LogoUploadResult(BaseModel):
    uploaded: bool


@app.post("/jobs/{job_id}/logo", response_model=LogoUploadResult)
async def upload_logo(job_id: str, file: UploadFile):
    """The client's own logo for the generated report's cover page (see
    app/docx_builder.py's _build_cover_and_front_matter) -- optional, and
    not dataroom content, so it gets no dataroom_files row and no
    classification; just an object key on the job itself, mirroring
    pitch_deck_key."""
    _require_job(job_id)
    data = await file.read()
    object_key = f"logo/{job_id}/{uuid.uuid4()}-{file.filename}"
    storage.put_bytes(object_key, data, content_type=file.content_type)
    repo.set_job_logo_key(job_id, object_key)
    return LogoUploadResult(uploaded=True)


@app.post("/jobs/{job_id}/classify-vertical", response_model=VerticalResult)
def classify_vertical(job_id: str):
    _require_job(job_id)
    files = repo.list_dataroom_files(job_id)
    filenames = [f["original_filename"] for f in files]
    vertical, signals = classify.classify_vertical(filenames)
    repo.set_job_vertical(job_id, vertical, signals)
    return VerticalResult(vertical=vertical, signals=signals)


class Stage2Result(BaseModel):
    task_ids: list[str]


@app.post("/jobs/{job_id}/stage2", response_model=Stage2Result)
def start_stage2(job_id: str):
    """Creates the Stage 2 (section drafting) tasks for a job. The worker
    process picks them up and runs the actual Claude calls -- this endpoint
    only enqueues, per the brief's request/response vs. worker split."""
    job = _require_job(job_id)
    if repo.stage_already_started(job_id, pipeline.STAGE_DRAFTING):
        raise HTTPException(status_code=409, detail="Stage 2 has already been started for this job")
    task_ids = pipeline.create_stage2_tasks(job_id, job["company_name"])
    return Stage2Result(task_ids=task_ids)


@app.post("/jobs/{job_id}/stage2/restart", response_model=Stage2Result)
def restart_stage2(job_id: str):
    """Discards the drafted report and everything after it (QA, gap
    analysis, approval) and runs Stage 2 again on the documents already
    read -- for a job whose draft failed or predates a pipeline change.
    Reading is not repeated, so this costs only Stage 2 itself."""
    job = _require_job(job_id)
    running = [t for t in repo.list_tasks_for_job(job_id)
               if t["stage"] >= pipeline.STAGE_DRAFTING and t["status"] == "running"]
    if running:
        raise HTTPException(status_code=409, detail="Stage 2+ tasks are still running; wait for them to finish")
    return Stage2Result(task_ids=pipeline.restart_stage2(job_id, job["company_name"]))


class UsageRow(BaseModel):
    purpose: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    web_searches: int
    cost_usd: float


class UsageOut(BaseModel):
    total_cost_usd: float
    rows: list[UsageRow]


def _corrected_cost(c: dict) -> float:
    """A usage row's cost at current prices. Rows recorded before prices
    moved to app/model_config.py (price_version 1) charged Sonnet 5 at
    $3/$15; its price is $2/$10, exactly 2/3 of every token cost (web
    search fees unchanged)."""
    cost = float(c["cost_usd"])
    if (c.get("price_version") or 1) == 1 and c["model"].startswith("claude-sonnet-5")             and not c["model"].startswith("claude-sonnet-5-5"):
        search = (c.get("web_searches") or 0) * 0.01
        cost = (cost - search) * 2 / 3 + search
    return round(cost, 5)


def _usage_stage(purpose: str) -> str:
    if purpose.startswith("eval:"):
        return "evaluation (not report cost)"
    if purpose.startswith(("read", "ocr", "fact_card")):
        return "1 ingestion"
    if purpose.startswith(("research",)):
        return "2a research"
    if purpose.startswith(("key_facts", "cache_warmup", "draft:", "absence")):
        return "2b drafting"
    if purpose.startswith("qa"):
        return "4 QA"
    return "5 gap analysis"


@app.get("/jobs/{job_id}/cost_report.json")
def cost_report(job_id: str):
    """Per-run cost report: every Claude call of the job with its tokens and
    dollar cost, totals by stage and by purpose (section), and the report's
    total excluding evaluation calls."""
    job = _require_job(job_id)
    calls = repo.list_api_usage(job_id)
    stages: dict[str, dict] = {}
    purposes: dict[str, dict] = {}
    keys = ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "web_searches")
    for c in calls:
        cost = _corrected_cost(c)
        for bucket, name in ((stages, _usage_stage(c["purpose"])), (purposes, c["purpose"])):
            b = bucket.setdefault(name, {"calls": 0, "cost_usd": 0.0, **{k: 0 for k in keys}})
            b["calls"] += 1
            b["cost_usd"] = round(b["cost_usd"] + cost, 5)
            for k in keys:
                b[k] += c[k]
    report_total = sum(v["cost_usd"] for k, v in stages.items() if not k.startswith("evaluation"))
    return {
        "job_id": job_id,
        "company_name": job["company_name"],
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "report_total_usd": round(report_total, 4),
        "evaluation_total_usd": round(sum(v["cost_usd"] for k, v in stages.items() if k.startswith("evaluation")), 4),
        "by_stage": dict(sorted(stages.items())),
        "by_purpose": dict(sorted(purposes.items(), key=lambda kv: -kv[1]["cost_usd"])),
        "calls": [{"purpose": c["purpose"], "model": c["model"], **{k: c[k] for k in keys},
                   "cost_usd": _corrected_cost(c), "batch": c.get("batch", False),
                   "at": c["created_at"].isoformat()} for c in calls],
    }


@app.post("/jobs/{job_id}/ab-draft")
def ab_draft(job_id: str, task_key: str, thinking: str = "adaptive"):
    """Draft one report part (without saving it) with reasoning on
    ("adaptive", Sonnet 5's default) or off ("disabled"), to compare quality
    and cost. Returns the text, token usage and cost; recorded as evaluation
    spend, not report cost."""
    job = _require_job(job_id)
    from app import claude_client, usage
    from app.sections_spec import SPEC_BY_TASK_KEY

    spec = SPEC_BY_TASK_KEY.get(task_key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown task {task_key!r}")
    if thinking not in ("adaptive", "disabled"):
        raise HTTPException(status_code=400, detail="thinking must be adaptive or disabled")
    name = pipeline.company_names(job_id, job["company_name"])[1]
    inputs = pipeline.build_drafting_inputs(job_id, name, spec)
    with usage.task_context(job_id, None):
        output, response = claude_client.draft_section_raw(
            spec, name, inputs["context"], inputs["user_content"], inputs["target_words"],
            thinking, f"eval:ab:{task_key}:{thinking}")
    stats = usage.cost_of(claude_client.DRAFTING_MODEL, response.usage)
    text = "\n\n".join(f"## {s.section_key}\n" + pipeline.render_blocks_as_text([b.model_dump() for b in s.blocks])
                       for s in output.sections)
    return {"task_key": task_key, "thinking": thinking, "stop_reason": response.stop_reason,
            "content_block_types": [b.type for b in response.content], "usage": stats,
            "visible_chars": len(text), "text": text}


@app.get("/jobs/{job_id}/absence-log")
def absence_log(job_id: str):
    """Every "missing / unsigned / unconfirmed document" claim the absence
    check found in the draft, and the ones it overturned and corrected."""
    _require_job(job_id)
    from app import absence
    try:
        return json.loads(storage.get_bytes(absence.log_key(job_id)).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=404, detail="no absence check has run for this job")


@app.post("/jobs/{job_id}/golden-eval")
def golden_eval(job_id: str, name: str = "cari_health"):
    """Score this job's drafted report against tests/golden/<name>.yaml (see
    app/golden.py). Costs one small Haiku call, recorded as evaluation."""
    _require_job(job_id)
    from app import golden
    try:
        return golden.evaluate_recorded(job_id, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no golden set named {name!r}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"golden eval failed: {type(exc).__name__}: {exc}"[:1000])


@app.get("/jobs/{job_id}/usage", response_model=UsageOut)
def job_usage(job_id: str):
    """What this job's Claude calls actually cost, grouped by purpose (read,
    key_facts, research, cache_warmup, draft:<part>, qa_all, ...)."""
    _require_job(job_id)
    rows = [UsageRow(**{k: (float(v) if k == "cost_usd" else v) for k, v in r.items()})
            for r in repo.job_usage_summary(job_id)]
    return UsageOut(total_cost_usd=round(sum(r.cost_usd for r in rows), 4), rows=rows)


class TaskOut(BaseModel):
    id: str
    stage: int
    task_type: str
    task_key: str
    status: str
    error: str | None


@app.get("/jobs/{job_id}/tasks", response_model=list[TaskOut])
def list_tasks(job_id: str):
    """Backs the UI's progress bars -- e.g. "14/20 sections drafted" during
    Stage 2, polled client-side rather than pushed."""
    _require_job(job_id)
    return [
        TaskOut(
            id=str(t["id"]), stage=t["stage"], task_type=t["task_type"],
            task_key=t["task_key"], status=t["status"], error=t["error"],
        )
        for t in repo.list_tasks_for_job(job_id)
    ]


class RetryFailedTasksResult(BaseModel):
    retried: int


@app.post("/jobs/{job_id}/retry-failed-tasks", response_model=RetryFailedTasksResult)
def retry_failed_tasks(job_id: str):
    """Resets every 'failed' task on this job back to 'pending' so the
    worker picks them up again -- e.g. after a bug (like a max_tokens
    truncation) that caused the failure has since been fixed. Without this,
    a failed task with no further automatic retries left (see
    repo.fail_task's MAX_TASK_ATTEMPTS) stays failed forever, and anything
    depending on it (Stage 4's report_card depends on every scoring task)
    stays stuck pending right along with it."""
    _require_job(job_id)
    return RetryFailedTasksResult(retried=repo.retry_failed_tasks(job_id))


class Stage3Result(BaseModel):
    task_id: str


@app.post("/jobs/{job_id}/stage3", response_model=Stage3Result)
def start_stage3(job_id: str):
    """Creates the Stage 3 (compile draft) task, which depends on every
    Stage 2 task and, once it runs, moves the job to awaiting_review."""
    job = _require_job(job_id)
    if repo.stage_already_started(job_id, pipeline.STAGE_COMPILE):
        raise HTTPException(status_code=409, detail="Stage 3 has already been started for this job")
    task_id = pipeline.create_stage3_task(job_id, job["company_name"])
    return Stage3Result(task_id=task_id)


@app.get("/jobs/{job_id}/draft.docx")
def get_draft_docx(job_id: str):
    """Renders the current draft docx fresh from report_sections every
    time -- so edits made during the human review checkpoint (see PATCH
    /jobs/{job_id}/sections/{section_key} below) are reflected immediately,
    without needing to re-run the Stage 3 compile task."""
    job = _require_job(job_id)
    sections = repo.list_report_sections(job_id)
    if not sections:
        raise HTTPException(status_code=404, detail="no sections drafted yet")
    logo_bytes = storage.get_bytes(job["logo_key"]) if job["logo_key"] else None
    legal_name, _ = pipeline.company_names(job_id, job["company_name"])
    docx_bytes = build_report_docx(legal_name, sections, logo_bytes, coverage=repo.coverage_summary(job_id))
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{job["company_name"]}_draft.docx"'},
    )


class UpdateSectionRequest(BaseModel):
    title: str
    blocks: list[ContentBlock]


class ReportSectionOut(BaseModel):
    section_key: str
    title: str
    order_index: int
    content: list[dict]
    status: str
    heading_level: int = 1
    meta: dict = {}


def _to_section_out(s: dict) -> ReportSectionOut:
    return ReportSectionOut(
        section_key=s["section_key"],
        title=s["title"],
        order_index=s["order_index"],
        content=s["content"],
        status=s["status"],
        heading_level=s.get("heading_level") or 1,
        meta=s.get("meta") or {},
    )


@app.get("/jobs/{job_id}/sections", response_model=list[ReportSectionOut])
def list_sections(job_id: str):
    _require_job(job_id)
    return [_to_section_out(s) for s in repo.list_report_sections(job_id)]


@app.patch("/jobs/{job_id}/sections/{section_key}", response_model=ReportSectionOut)
def update_section(job_id: str, section_key: str, req: UpdateSectionRequest):
    """Human-in-the-loop edit during the Stage 3/4 review checkpoint -- the
    brief calls for the draft to be "inspectable/editable" before the more
    expensive QA pass runs against it."""
    _require_job(job_id)
    existing = repo.get_report_section(job_id, section_key)
    if existing is None:
        raise HTTPException(status_code=404, detail="section not found")
    blocks = [b.model_dump() for b in req.blocks]
    repo.update_report_section_content(job_id, section_key, req.title, blocks)
    return _to_section_out(repo.get_report_section(job_id, section_key))


class ApproveDraftResult(BaseModel):
    status: str
    draft_approved: bool


@app.post("/jobs/{job_id}/approve-draft", response_model=ApproveDraftResult)
def approve_draft(job_id: str):
    """The human review checkpoint gate itself: marks the draft approved so
    Stage 4/5 QA tasks may be created. Requires the job to have reached
    awaiting_review (i.e. Stage 3 compile has run) first."""
    job = _require_job(job_id)
    if job["status"] != "awaiting_review":
        raise HTTPException(
            status_code=409,
            detail=f"job status is '{job['status']}', expected 'awaiting_review'",
        )
    repo.approve_draft(job_id)
    updated = repo.get_job(job_id)
    return ApproveDraftResult(status=updated["status"], draft_approved=updated["draft_approved_at"] is not None)


class Stage4Result(BaseModel):
    task_ids: list[str]


@app.post("/jobs/{job_id}/stage4", response_model=Stage4Result)
def start_stage4(job_id: str):
    """Creates the Stage 4 QA/scoring tasks -- the DD Assessment block,
    the conditional Life Science block, master validation, and Report
    Card. Gated on the human review checkpoint: refuses unless the draft
    has been explicitly approved via POST /jobs/{id}/approve-draft."""
    job = _require_job(job_id)
    if job["draft_approved_at"] is None:
        raise HTTPException(
            status_code=409,
            detail="draft has not been approved yet -- call POST /jobs/{id}/approve-draft first",
        )
    if repo.stage_already_started(job_id, pipeline.STAGE_QA):
        raise HTTPException(status_code=409, detail="Stage 4 has already been started for this job")
    task_ids = pipeline.create_stage4_tasks(job_id, job["company_name"], job["vertical"])
    return Stage4Result(task_ids=task_ids)


class QAScoreOut(BaseModel):
    block: str
    assessment_name: str
    scale: str
    score: float | None
    findings: dict


@app.get("/jobs/{job_id}/qa-scores", response_model=list[QAScoreOut])
def list_qa_scores(job_id: str):
    _require_job(job_id)
    scores = repo.list_qa_scores(job_id)
    return [
        QAScoreOut(
            block=s["block"],
            assessment_name=s["assessment_name"],
            scale=s["scale"],
            score=float(s["score"]) if s["score"] is not None else None,
            findings=s["findings"] or {},
        )
        for s in scores
    ]


class Stage5Result(BaseModel):
    task_ids: list[str]


@app.post("/jobs/{job_id}/stage5", response_model=Stage5Result)
def start_stage5(job_id: str):
    """Creates the sequential Stage 5 tasks (gap_analysis, then
    mitigation_strategy). Gated on Stage 4 having produced a Report Card,
    since Gap Analysis reads the QA scores."""
    job = _require_job(job_id)
    report_card = next(
        (s for s in repo.list_qa_scores(job_id) if s["assessment_name"] == "Report Card of scores"),
        None,
    )
    if report_card is None:
        raise HTTPException(
            status_code=409,
            detail="Stage 4 has not produced a Report Card yet -- call POST /jobs/{id}/stage4 first",
        )
    if repo.stage_already_started(job_id, pipeline.STAGE_GAP_MITIGATION):
        raise HTTPException(status_code=409, detail="Stage 5 has already been started for this job")
    task_ids = pipeline.create_stage5_tasks(job_id, job["company_name"])
    return Stage5Result(task_ids=task_ids)


class Stage6Result(BaseModel):
    task_id: str


@app.post("/jobs/{job_id}/stage6", response_model=Stage6Result)
def start_stage6(job_id: str):
    """Creates the final_render task. Gated on Stage 5's mitigation
    strategy having been generated."""
    job = _require_job(job_id)
    if not job["mitigation_strategy"]:
        raise HTTPException(
            status_code=409,
            detail="mitigation strategy has not been generated yet -- call POST /jobs/{id}/stage5 first",
        )
    if repo.stage_already_started(job_id, pipeline.STAGE_FINAL_RENDER):
        raise HTTPException(status_code=409, detail="Stage 6 has already been started for this job")
    task_id = pipeline.create_stage6_task(job_id, job["company_name"])
    return Stage6Result(task_id=task_id)


@app.get("/jobs/{job_id}/report.docx")
def get_final_report_docx(job_id: str):
    """Serves the final rendered docx bytes directly from storage (unlike
    /draft.docx, which always re-renders live from report_sections alone --
    the final report also includes QA/gap-analysis content that only exists
    once Stage 6 has actually run)."""
    job = _require_job(job_id)
    if job["status"] != "completed" or not job["output_key"]:
        raise HTTPException(status_code=409, detail="final report has not been rendered yet")
    docx_bytes = storage.get_bytes(job["output_key"])
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{job["company_name"]}_due_diligence_report.docx"'},
    )


class GapAnalysisOut(BaseModel):
    gap_analysis: dict | None
    mitigation_strategy: dict | None


@app.get("/jobs/{job_id}/gap-analysis", response_model=GapAnalysisOut)
def get_gap_analysis(job_id: str):
    job = _require_job(job_id)
    return GapAnalysisOut(gap_analysis=job["gap_analysis"], mitigation_strategy=job["mitigation_strategy"])
