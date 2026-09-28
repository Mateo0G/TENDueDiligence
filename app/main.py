"""FastAPI web service: accepts uploads, exposes job status. Long-running
pipeline work (Claude calls, docx rendering) happens in the separate worker
process (app/worker.py), never on this request/response cycle.
"""
import uuid

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app import classify, pipeline, repo, storage
from app.docx_builder import build_report_docx
from app.models import ContentBlock

app = FastAPI(title="TEN Due Diligence Report Generator")


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


class UploadResult(BaseModel):
    uploaded: list[DataroomFileOut]


class VerticalResult(BaseModel):
    vertical: str
    signals: dict


class JobOut(BaseModel):
    id: str
    company_name: str
    status: str
    vertical: str | None
    dataroom_files: list[DataroomFileOut]


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
    )


@app.post("/jobs", response_model=CreateJobResponse)
def create_job(req: CreateJobRequest):
    job_id = repo.create_job(req.company_name)
    job = repo.get_job(job_id)
    return CreateJobResponse(id=str(job["id"]), company_name=job["company_name"], status=job["status"])


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str):
    job = _require_job(job_id)
    files = repo.list_dataroom_files(job_id)
    return JobOut(
        id=str(job["id"]),
        company_name=job["company_name"],
        status=job["status"],
        vertical=job["vertical"],
        dataroom_files=[_to_file_out(f) for f in files],
    )


@app.post("/jobs/{job_id}/dataroom", response_model=UploadResult)
async def upload_dataroom_files(job_id: str, files: list[UploadFile]):
    _require_job(job_id)
    uploaded = []
    for f in files:
        data = await f.read()
        object_key = f"dataroom/{job_id}/{uuid.uuid4()}-{f.filename}"
        storage.put_bytes(object_key, data, content_type=f.content_type)

        category, method = classify.categorize_file(f.filename)
        file_id = repo.add_dataroom_file(
            job_id=job_id,
            object_key=object_key,
            original_filename=f.filename,
            file_kind="dataroom",
            category=category,
            category_method=method,
            size_bytes=len(data),
            content_type=f.content_type,
        )
        row = {
            "id": file_id,
            "original_filename": f.filename,
            "file_kind": "dataroom",
            "category": category,
            "category_method": method,
            "size_bytes": len(data),
            "content_type": f.content_type,
        }
        uploaded.append(_to_file_out(row))
    return UploadResult(uploaded=uploaded)


@app.post("/jobs/{job_id}/pitch-deck", response_model=DataroomFileOut)
async def upload_pitch_deck(job_id: str, file: UploadFile):
    _require_job(job_id)
    data = await file.read()
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
    )
    repo.set_job_pitch_deck_key(job_id, object_key)
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
    task_ids = pipeline.create_stage2_tasks(job_id, job["company_name"])
    return Stage2Result(task_ids=task_ids)


class Stage3Result(BaseModel):
    task_id: str


@app.post("/jobs/{job_id}/stage3", response_model=Stage3Result)
def start_stage3(job_id: str):
    """Creates the Stage 3 (compile draft) task, which depends on every
    Stage 2 task and, once it runs, moves the job to awaiting_review."""
    job = _require_job(job_id)
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
    docx_bytes = build_report_docx(job["company_name"], sections)
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


@app.get("/jobs/{job_id}/sections", response_model=list[ReportSectionOut])
def list_sections(job_id: str):
    _require_job(job_id)
    sections = repo.list_report_sections(job_id)
    return [
        ReportSectionOut(
            section_key=s["section_key"],
            title=s["title"],
            order_index=s["order_index"],
            content=s["content"],
            status=s["status"],
        )
        for s in sections
    ]


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
    updated = repo.get_report_section(job_id, section_key)
    return ReportSectionOut(
        section_key=updated["section_key"],
        title=updated["title"],
        order_index=updated["order_index"],
        content=updated["content"],
        status=updated["status"],
    )


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
