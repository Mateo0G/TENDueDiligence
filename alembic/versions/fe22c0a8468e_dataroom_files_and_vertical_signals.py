"""dataroom files and vertical signals

Revision ID: fe22c0a8468e
Revises: 95a34ed97047
Create Date: 2026-09-16 13:37:56.522863

Phase 2 (intake & routing): tracks every uploaded file's object-storage key
and classified category, plus records *why* a job got classified into a
given vertical so a human can sanity-check the deterministic classifier.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'fe22c0a8468e'
down_revision: Union[str, Sequence[str], None] = '95a34ed97047'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE dataroom_files (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_id               UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            object_key           TEXT NOT NULL,
            original_filename    TEXT NOT NULL,
            file_kind            TEXT NOT NULL DEFAULT 'dataroom'
                                 CHECK (file_kind IN ('dataroom', 'pitch_deck')),
            category             TEXT,
            category_method      TEXT
                                 CHECK (category_method IN ('deterministic', 'llm')),
            size_bytes           BIGINT,
            content_type         TEXT,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (job_id, object_key)
        )
    """)
    op.execute("CREATE INDEX dataroom_files_job_category_idx ON dataroom_files (job_id, category)")

    # Debugging aid: which keywords/filenames drove the vertical call, so a
    # human can sanity-check the deterministic classifier without re-running it.
    op.execute("ALTER TABLE jobs ADD COLUMN vertical_signals JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN vertical_signals")
    op.execute("DROP TABLE dataroom_files")
