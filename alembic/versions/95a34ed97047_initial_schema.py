"""initial schema

Revision ID: 95a34ed97047
Revises:
Create Date: 2026-09-04 13:05:33.078583

Raw SQL throughout (no ORM models) -- this project reasons about its schema
and queries directly, per the project brief's "deterministic pipeline"
constraint. See docs/schema.md for the design rationale (depends_on
modeling, status enums, the SELECT ... FOR UPDATE SKIP LOCKED claiming
pattern this schema is built for).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '95a34ed97047'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE jobs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            company_name    TEXT NOT NULL,
            vertical        TEXT,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'running', 'completed', 'failed')),
            dataroom_prefix TEXT,
            pitch_deck_key  TEXT,
            output_key      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE tasks (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id       UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            stage        SMALLINT NOT NULL,
            task_type    TEXT NOT NULL,
            task_key     TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
            depends_on   UUID[] NOT NULL DEFAULT '{}',
            payload      JSONB NOT NULL DEFAULT '{}',
            result       JSONB,
            error        TEXT,
            attempts     SMALLINT NOT NULL DEFAULT 0,
            locked_by    TEXT,
            locked_at    TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (job_id, task_key)
        )
    """)
    op.execute("CREATE INDEX tasks_job_status_idx ON tasks (job_id, status)")
    # Claim-query support: cheap check of "are all of this task's deps done".
    op.execute("CREATE INDEX tasks_depends_on_gin_idx ON tasks USING GIN (depends_on)")

    op.execute("""
        CREATE TABLE report_sections (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id         UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            section_key    TEXT NOT NULL,
            title          TEXT NOT NULL,
            order_index    SMALLINT NOT NULL,
            content        JSONB NOT NULL,
            source_task_id UUID REFERENCES tasks(id),
            status         TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'reviewed', 'final')),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (job_id, section_key)
        )
    """)

    op.execute("""
        CREATE TABLE qa_scores (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id          UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            block           TEXT NOT NULL
                            CHECK (block IN ('dd_assessment', 'life_science_assessment', 'master_validation')),
            assessment_name TEXT NOT NULL,
            scale           TEXT NOT NULL DEFAULT '0-100',
            score           NUMERIC,
            findings        JSONB,
            source_task_id  UUID REFERENCES tasks(id),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (job_id, block, assessment_name)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE qa_scores")
    op.execute("DROP TABLE report_sections")
    op.execute("DROP TABLE tasks")
    op.execute("DROP TABLE jobs")
