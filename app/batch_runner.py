"""Batch API execution for Stage 2 and Stage 4 -- the brief's Phase 7 cost
lever alongside prompt caching (app/claude_client.py). Not integrated into
app/worker.py's continuous single-task poll loop: batch turnaround is
minutes to (per Anthropic) up to 24 hours, which doesn't fit that loop's
claim-one-task-and-run-it shape. This runs as its own step instead --
scripts/phase7_batch_test.py shows the pattern end to end.

Both Stage 2 and Stage 4 have real internal dependency structure (Stage 2's
two-pass sections and the Competition chain; Stage 4's Report Card
depending on every score), so "batch the whole stage in one shot" isn't
possible -- only the tasks with no *unmet* dependency at a given moment are
batchable together. The loop below submits one Anthropic Batch per "wave"
of currently-ready tasks (the same depends_on-satisfied condition
app/repo.py's single-task claim query uses, just claiming many rows at
once), then re-checks for the next wave once results land -- naturally
draining the whole dependency graph over however many waves it takes.
"""
import logging
import time

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from app import claude_client, pipeline, repo
from app.models import DraftingOutput, QAScoreOutput
from app.qa_spec import DD_ASSESSMENT_SPECS, LIFE_SCIENCE_SPECS, MASTER_VALIDATION_SPEC
from app.sections_spec import SPEC_BY_TASK_KEY
from app.tasks.registry import get_handler

log = logging.getLogger("batch_runner")

_QA_SPEC_BY_TASK_KEY = {
    s.task_key: s for s in DD_ASSESSMENT_SPECS + LIFE_SCIENCE_SPECS + [MASTER_VALIDATION_SPEC]
}


def _submit_and_await(requests: list, poll_interval: float, timeout: float):
    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)
    log.info("submitted batch %s with %d requests", batch.id, len(requests))

    deadline = time.monotonic() + timeout
    while batch.processing_status != "ended":
        if time.monotonic() > deadline:
            raise TimeoutError(f"batch {batch.id} did not finish within {timeout}s")
        time.sleep(poll_interval)
        try:
            repo.ping()  # keep the DB connection warm through a multi-minute wait -- see app/db.py
        except Exception as exc:
            # Non-fatal: the batch itself lives on Anthropic's side for 29
            # days regardless of local DB connectivity, so a dead ping here
            # only matters if the DB is *still* unreachable once we come to
            # persist results below -- no need to abort the whole wait over it.
            log.warning("keep-alive ping failed (continuing): %s", exc)
        batch = client.messages.batches.retrieve(batch.id)
        log.info("batch %s status=%s counts=%s", batch.id, batch.processing_status, batch.request_counts)

    return list(client.messages.batches.results(batch.id))


def run_stage2_batch(job_id: str, company_name: str, poll_interval: float = 10.0, timeout: float = 3600.0) -> dict:
    """Drains every ready draft_section task for a job, wave by wave."""
    worker_id = f"batch-runner-stage2-{job_id}"
    completed = failed = wave = 0

    while True:
        claimed = repo.claim_ready_tasks(job_id, "draft_section", worker_id, limit=100)
        if not claimed:
            remaining = repo.count_pending_tasks(job_id, "draft_section")
            if remaining:
                log.warning("stage2 batch: %d tasks stuck pending (a dependency likely failed)", remaining)
            break

        wave += 1
        log.info("stage2 batch wave %d: %d ready task(s)", wave, len(claimed))

        requests = []
        task_by_custom_id = {}
        for task in claimed:
            spec = SPEC_BY_TASK_KEY[task["task_key"]]
            dataroom_text = pipeline.dataroom_text_for_categories(job_id, spec.dataroom_categories)
            deck_text = pipeline.pitch_deck_text(job_id)
            dependency_context = pipeline.build_dependency_context(job_id, company_name, spec)
            params = claude_client.build_drafting_request(spec, company_name, dataroom_text, deck_text, dependency_context)
            custom_id = str(task["id"])
            requests.append(Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(**params)))
            task_by_custom_id[custom_id] = task

        for result in _submit_and_await(requests, poll_interval, timeout):
            task = task_by_custom_id[result.custom_id]
            if result.result.type == "succeeded":
                try:
                    output = claude_client.parse_structured_response(result.result.message, DraftingOutput)
                    pipeline.persist_drafting_output(
                        job_id, company_name, SPEC_BY_TASK_KEY[task["task_key"]], output, task["id"]
                    )
                    repo.complete_task(task["id"], {"persisted": True})
                    completed += 1
                except Exception as exc:
                    repo.fail_task(task["id"], f"batch result parse/persist failed: {exc}")
                    failed += 1
            else:
                repo.fail_task(task["id"], f"batch request {result.result.type}")
                failed += 1

    return {"waves": wave, "completed": completed, "failed": failed}


def run_stage4_batch(job_id: str, company_name: str, poll_interval: float = 10.0, timeout: float = 3600.0) -> dict:
    """Same wave pattern for Stage 4's qa_score tasks. report_card is
    deterministic (no Claude call -- see app/tasks/report_card.py), so it
    isn't batchable; once every qa_score task is done, its dependency is
    satisfied and it's drained through the normal single-task path."""
    worker_id = f"batch-runner-stage4-{job_id}"
    completed = failed = wave = 0

    while True:
        claimed = repo.claim_ready_tasks(job_id, "qa_score", worker_id, limit=100)
        if not claimed:
            remaining = repo.count_pending_tasks(job_id, "qa_score")
            if remaining:
                log.warning("stage4 batch: %d tasks stuck pending (a dependency likely failed)", remaining)
            break

        wave += 1
        log.info("stage4 batch wave %d: %d ready task(s)", wave, len(claimed))

        report_text = pipeline.render_full_report_text(job_id)
        dataroom_text = pipeline.full_dataroom_text(job_id)

        requests = []
        task_by_custom_id = {}
        for task in claimed:
            spec = _QA_SPEC_BY_TASK_KEY[task["task_key"]]
            params = claude_client.build_qa_request(spec, company_name, report_text, dataroom_text)
            custom_id = str(task["id"])
            requests.append(Request(custom_id=custom_id, params=MessageCreateParamsNonStreaming(**params)))
            task_by_custom_id[custom_id] = task

        for result in _submit_and_await(requests, poll_interval, timeout):
            task = task_by_custom_id[result.custom_id]
            if result.result.type == "succeeded":
                try:
                    output = claude_client.parse_structured_response(result.result.message, QAScoreOutput)
                    pipeline.persist_qa_output(job_id, _QA_SPEC_BY_TASK_KEY[task["task_key"]], output, task["id"])
                    repo.complete_task(task["id"], {"score": output.score})
                    completed += 1
                except Exception as exc:
                    repo.fail_task(task["id"], f"batch result parse/persist failed: {exc}")
                    failed += 1
            else:
                repo.fail_task(task["id"], f"batch request {result.result.type}")
                failed += 1

    # report_card is the only non-batchable Stage 4 task (deterministic, no
    # Claude call). Run it directly for *this job's* task once every
    # qa_score task is done -- not via the generic single-task claim loop,
    # which claims the oldest ready task system-wide and could pick up an
    # unrelated job's work first.
    report_card_task = next(
        (t for t in repo.list_tasks_for_job(job_id) if t["task_type"] == "report_card" and t["status"] == "pending"),
        None,
    )
    if report_card_task:
        handler = get_handler("report_card")
        try:
            result = handler(report_card_task)
            repo.complete_task(report_card_task["id"], result)
        except Exception as exc:
            repo.fail_task(report_card_task["id"], str(exc))
            failed += 1

    return {"waves": wave, "completed": completed, "failed": failed}
