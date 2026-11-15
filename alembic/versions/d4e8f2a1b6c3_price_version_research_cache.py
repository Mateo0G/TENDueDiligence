"""usage price version, research cache

Revision ID: d4e8f2a1b6c3
Revises: b7c2d4e6f801
Create Date: 2026-11-15 12:00:00.000000

- api_usage.price_version: rows recorded before prices moved to
  app/model_config.py (version 1) used $3/$15 for Sonnet 5, whose price is
  $2/$10; the cost report corrects them. batch: the call went through the
  Message Batches API (half price).
- research_cache: web research per company and topic, reused for N days so
  re-running a report doesn't search (and pay) again.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd4e8f2a1b6c3'
down_revision: Union[str, Sequence[str], None] = 'b7c2d4e6f801'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_usage ADD COLUMN price_version SMALLINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE api_usage ADD COLUMN batch BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        """
        CREATE TABLE research_cache (
            company_key TEXT NOT NULL,
            topic TEXT NOT NULL,
            memo TEXT NOT NULL,
            sources JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (company_key, topic)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE research_cache")
    op.execute("ALTER TABLE api_usage DROP COLUMN batch")
    op.execute("ALTER TABLE api_usage DROP COLUMN price_version")
