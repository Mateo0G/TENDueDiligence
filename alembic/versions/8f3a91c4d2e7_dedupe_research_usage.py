"""dedupe uploads, store research memo, track API usage per call

Revision ID: 8f3a91c4d2e7
Revises: 5d1e7b2c9a40
Create Date: 2026-11-13 12:00:00.000000

Backs the Stage 1-2 cost/speed rebuild:
- dataroom_files.sha256 / source_path: identical files uploaded twice (or
  repeated across folders of one zip) are stored once; the folder path
  inside the zip is kept because it is the strongest hint of what a
  document is.
- jobs.research: the one web-research memo (competitors, comparable
  transactions, acquirers, market data) every drafting call shares,
  replacing per-section web searches.
- api_usage: one row per Claude call with its token counts and dollar
  cost, so a job's real spend is visible instead of estimated.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '8f3a91c4d2e7'
down_revision: Union[str, Sequence[str], None] = '5d1e7b2c9a40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE dataroom_files ADD COLUMN sha256 TEXT")
    op.execute("ALTER TABLE dataroom_files ADD COLUMN source_path TEXT")
    op.execute("CREATE INDEX ix_dataroom_files_job_sha256 ON dataroom_files (job_id, sha256)")
    op.execute("ALTER TABLE jobs ADD COLUMN research JSONB")
    op.execute(
        """
        CREATE TABLE api_usage (
            id BIGSERIAL PRIMARY KEY,
            job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
            task_id UUID,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            web_searches INTEGER NOT NULL DEFAULT 0,
            cost_usd NUMERIC(10, 5) NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_api_usage_job ON api_usage (job_id)")


def downgrade() -> None:
    op.execute("DROP TABLE api_usage")
    op.execute("ALTER TABLE jobs DROP COLUMN research")
    op.execute("DROP INDEX ix_dataroom_files_job_sha256")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN source_path")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN sha256")
