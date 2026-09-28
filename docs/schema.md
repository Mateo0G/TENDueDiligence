# Schema — Phase 1

Four tables, defined in `alembic/versions/95a34ed97047_initial_schema.py`
as raw SQL (`op.execute`, no ORM models — this project reasons about its
schema and queries directly, per the brief's "deterministic pipeline"
constraint; Alembic itself still depends on SQLAlchemy internally to run
migrations, but nothing in `app/` does).

## `jobs`

One row per due-diligence report being generated. `vertical` starts `NULL`
and is filled in by the Stage 1 classification task (decides whether the
Life Science Assessment block runs in Stage 4). `dataroom_prefix` /
`pitch_deck_key` / `output_key` are object-storage keys (Phase 2+), never
file bytes. `output_key` is set by the Stage 3 `compile_draft` task and
reused for the Stage 6 final render (the final render simply overwrites the
same key with the finished docx).

**`awaiting_review` status + `draft_approved_at` (added Phase 4)** — the
brief's required human review checkpoint between Stage 3 and Stage 4.
`compile_draft` moves a job straight to `status = 'awaiting_review'` once
every Stage 2 section exists; `draft_approved_at` (nullable timestamp,
separate from `status`) is set by `POST /jobs/{id}/approve-draft` and is
what Stage 5's task-creation endpoint checks before creating any QA tasks —
kept as its own column rather than folded into `status` so a job can stay
visibly `awaiting_review` while carrying an independent
approved-yes/no-and-when signal.

## `tasks`

One row per pipeline node — a Stage 2 section draft, a Stage 4 QA/scoring
prompt, the Stage 5 Gap Analysis/Mitigation pair, the Stage 3 compile step,
etc. `task_key` is a human-readable identifier unique within a job (e.g.
`"draft_section:Sales and Marketing"`, `"qa_score:Comprehensiveness
Validator"`) — useful for debugging and for building a status UI without
needing to know task UUIDs.

**`depends_on UUID[]`** — per the brief's own modeling suggestion. A task is
claimable once every id in this array has `status = 'completed'`. Chosen
over a separate `task_dependencies` join table because:
- most tasks have 0–2 dependencies (see `docs/prompt_inventory.md` §2's
  two-pass and chain patterns) — the join-table normalization win is small.
- the claim query becomes a single `NOT EXISTS` subquery against the same
  table, no extra join.
- a GIN index on `depends_on` keeps the "which tasks depend on task X"
  direction cheap too, if that's ever needed (e.g. to cascade-skip
  downstream tasks when an upstream one fails permanently — not built yet,
  see Deferred below).

**Claiming query** (`app/repo.py::claim_task`):

```sql
UPDATE tasks
SET status = 'running', locked_by = %(worker_id)s, locked_at = now(), updated_at = now()
WHERE id = (
    SELECT id FROM tasks
    WHERE status = 'pending'
      AND NOT EXISTS (
          SELECT 1 FROM tasks dep
          WHERE dep.id = ANY(tasks.depends_on) AND dep.status <> 'completed'
      )
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, job_id, stage, task_type, task_key, payload, depends_on, attempts
```

The `FOR UPDATE SKIP LOCKED` lives in the inner `SELECT`; the outer
`UPDATE ... WHERE id = (...)` makes the pick-and-claim atomic in one
statement, so two workers racing on the same poll can never claim the same
row — the loser's subquery either sees the row already locked (and skips
it, picking the next candidate) or already `running` (and finds nothing).
Proven safe empirically in `scripts/phase1_dag_test.py`'s 4-thread
concurrent drain.

Terminal states are `completed` / `failed` / `skipped` (skipped is reserved
for conditional tasks that don't apply to a given job — e.g. the Life
Science Assessment block for a non-life-sciences company — not yet used by
any code as of Phase 1). `failed` is not automatically retried; `attempts`
is tracked but retry policy is a Phase 3+ decision once real (fallible,
rate-limited) Claude calls exist — a no-op task either always succeeds or
always raises, so there's nothing meaningful to retry yet.

## `report_sections`

One row per Stage 2-drafted section, keyed by `(job_id, section_key)`
(matching a heading from `docs/template_structure.md`'s 25-section
skeleton). `source_task_id` traces a section back to the Stage 2 task that
produced it (`NULL` after a human edit — see below). `content` is `JSONB`,
a list of blocks shaped by `app.models.ContentBlock`
(`paragraph`/`heading`/`table`, resolved in Phase 4) — this is what
`app/docx_builder.py` walks to lay out each section.

**Editable during the Phase 4 review checkpoint**: `PATCH
/jobs/{id}/sections/{section_key}` lets a human rewrite a section's
`title`/`content` directly (the brief calls for the draft to be
"inspectable/editable" before the costlier Stage 4 QA pass runs). `GET
/jobs/{id}/draft.docx` always renders fresh from whatever
`report_sections` currently holds, so an edit is reflected on the very next
download with no re-compile step needed.

## `qa_scores`

One row per Stage 4 assessment result, keyed by `(job_id, block,
assessment_name)`. `block` is one of `dd_assessment` /
`life_science_assessment` / `master_validation` — matching
`docs/prompt_inventory.md`'s §7/§8/§9 grouping. `score` is nullable because
one DD Assessment prompt (Key Information Check, per Flag A1) doesn't
return a numeric score at all. `scale` records which convention applies
(`'0-100'` for every prompt except the master validator's `'1-10'`, per
Flag M1) — deliberately *not* normalized into a single column, since
rescaling a 1–10 judgment by ×10 would claim false precision.

## `jobs.gap_analysis` / `jobs.mitigation_strategy` (added Phase 6)

Stage 5's two sequential outputs (docs/prompt_inventory.md §6): Gap
Analysis (reads the compiled report + Stage 4 QA scores) and Mitigation
Strategy (reads Gap Analysis's own output). Unlike `report_sections` and
`qa_scores`, these are exactly one result per job — no natural key beyond
`job_id` — so they're JSONB columns on `jobs` directly rather than a new
table. `app.models.GapAnalysisOutput` / `MitigationOutput` define their
shape; `MitigationItem.gap_title` is expected to exactly match a
`GapItem.title` from the same job's gap analysis, which
`app/docx_builder.py` relies on to pair each gap with its mitigation in the
Stage 6 appendix table.

## `dataroom_files` (added Phase 2)

One row per uploaded file (dataroom document or pitch deck), keyed by
`(job_id, object_key)`. `category` is one of the 8 dataroom categories named
in the brief (`financial`, `legal_corporate`, `ip`, `hr`,
`sales_marketing`, `science_tech`, `contracts`, `regulatory`), `NULL` when
no keyword matched — an honest "unclassified" rather than a forced guess.
`category_method` records `'deterministic'` for every row today; `'llm'` is
reserved for the Haiku fallback pass the brief says to add only if
filename/folder matching proves unreliable in practice (not yet needed).

`jobs.vertical_signals` (JSONB, added alongside this table) records which
keywords drove the vertical call — e.g. `{"matched": {"life_sciences":
["clinical", "fda", "trial", "510k"]}, "scores": {"life_sciences": 4}}` —
so a human can sanity-check a classification without re-running it. See
`app/classify.py` for the keyword lists and scoring/tie-break logic.

## Phase 7 — caching and the Batch API (no schema changes)

Neither prompt caching nor the Batch API needed new columns or tables —
both are request-shaping/execution-path changes, not data model changes.
Worth recording here since `app/claude_client.py` and `app/batch_runner.py`
both reference this section:

- **Caching**: `app/claude_client.py`'s `CACHE_TTL` uses a 1-hour TTL
  (`cache_control: {"type": "ephemeral", "ttl": "1h"}`), not the 5-minute
  default. Measured in practice: a full Stage 2 pass is ~20 sequential
  calls and Stage 4 ~23, both comfortably exceeding 5 minutes end to end
  (see Phase 3/5's timing notes), so the shorter default would only cache-hit
  the first couple of calls in each stage before expiring — the 1-hour TTL
  is what makes the shared reference-report block (Stage 2) and the shared
  compiled-report-plus-dataroom block (Stage 4) pay off across an *entire*
  stage. Verified empirically: a two-call smoke test showed ~69K tokens
  written once and fully re-read (zero re-write) on the next call.
- **Batch API**: `tasks.locked_by` doubles as the "who has this claimed"
  field for both single-task and batch execution — a batch-claimed row's
  `locked_by` is set to a `batch-runner-stage{2,4}-<job_id>` string instead
  of a worker hostname/pid, but it's the same column and the same meaning
  ("claimed, not yet resolved"), so no schema change was needed there
  either. `app/repo.py::claim_ready_tasks` is the multi-row sibling of the
  single-row `claim_task` query, same `FOR UPDATE SKIP LOCKED` pattern,
  just claiming up to N ready rows instead of 1.
- Real infrastructure lesson from getting this working end-to-end: a
  multi-minute Anthropic Batch poll with no Postgres traffic was long
  enough to trip a local dev SSH tunnel's idle timeout more than once
  during testing (see `app/db.py`'s `check=ConnectionPool.check_connection`
  and `app/repo.py::ping`, called — non-fatally — during
  `batch_runner._submit_and_await`'s poll loop). This is a local-tooling
  concern specific to developing against Railway's Postgres over `railway
  connect`'s SSH tunnel; Phase 8's in-network deployment talks to Postgres
  directly and won't have it.

## Deferred (not needed until a later phase)

- Cascading a permanently-failed task's downstream dependents to `skipped`
  rather than leaving them stuck `pending` forever — not needed while
  failures are rare/manual (no-op tasks only); revisit once Stage 2/4 tasks
  can genuinely fail (rate limits, malformed model output).
- Retry/backoff policy keyed off `attempts` — same reasoning.
- Job-level status transitions (`jobs.status` auto-updating as its tasks
  complete) — not wired up yet; Phase 2+ will decide whether that's a
  trigger, a worker-side update, or computed on read.
