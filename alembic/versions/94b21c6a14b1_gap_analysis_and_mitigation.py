"""gap analysis and mitigation

Revision ID: 94b21c6a14b1
Revises: d93260cb587f
Create Date: 2026-09-28 00:00:00.000000

Phase 6 (Stage 5): Gap Analysis and Mitigation Strategy are each exactly
one result per job (unlike report_sections/qa_scores, which are N-per-job),
so they're stored as JSONB columns on jobs directly rather than a new
table.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '94b21c6a14b1'
down_revision: Union[str, Sequence[str], None] = 'd93260cb587f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN gap_analysis JSONB")
    op.execute("ALTER TABLE jobs ADD COLUMN mitigation_strategy JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN mitigation_strategy")
    op.execute("ALTER TABLE jobs DROP COLUMN gap_analysis")
