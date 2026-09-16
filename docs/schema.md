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
file bytes.

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

One row per Stage 3-compiled section, keyed by `section_key` (matching a
heading from `docs/template_structure.md`'s 25-section skeleton).
`source_task_id` traces a section back to the Stage 2 task(s) that produced
it. `content` is `JSONB` rather than plain text because a section is more
than a text blob — it may carry an ordered list of paragraph/table blocks
that the Stage 6 docx renderer needs structure to lay out (exact shape
deferred to Phase 4, once the renderer's needs are concrete).

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

## Deferred (not needed until a later phase)

- A `dataroom_files` table (object keys + classified category) — Phase 2.
- Cascading a permanently-failed task's downstream dependents to `skipped`
  rather than leaving them stuck `pending` forever — not needed while
  failures are rare/manual (no-op tasks only); revisit once Stage 2/4 tasks
  can genuinely fail (rate limits, malformed model output).
- Retry/backoff policy keyed off `attempts` — same reasoning.
- Job-level status transitions (`jobs.status` auto-updating as its tasks
  complete) — not wired up yet; Phase 2+ will decide whether that's a
  trigger, a worker-side update, or computed on read.
