"""stage3 compile and review checkpoint

Revision ID: d93260cb587f
Revises: fe22c0a8468e
Create Date: 2026-09-28 00:00:00.000000

Phase 4 (compile): adds the 'awaiting_review' job status -- set when Stage 3
compile finishes -- and a draft_approved_at timestamp the human review
checkpoint sets before Stage 5 QA tasks may be created (brief: "Insert a
human review checkpoint after stage 3 and before stage 4").
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd93260cb587f'
down_revision: Union[str, Sequence[str], None] = 'fe22c0a8468e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_status_check")
    op.execute(
        "ALTER TABLE jobs ADD CONSTRAINT jobs_status_check "
        "CHECK (status IN ('pending', 'running', 'awaiting_review', 'completed', 'failed'))"
    )
    op.execute("ALTER TABLE jobs ADD COLUMN draft_approved_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN draft_approved_at")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_status_check")
    op.execute(
        "ALTER TABLE jobs ADD CONSTRAINT jobs_status_check "
        "CHECK (status IN ('pending', 'running', 'completed', 'failed'))"
    )
