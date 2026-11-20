"""rosters merged from fact cards

Revision ID: f6a1b8c4d3e2
Revises: e5f9a3b7c2d1
Create Date: 2026-11-20 06:00:00.000000

jobs.rosters: people, agreements, performance metrics and IP events merged
from every current fact card, plus employer-vs-competitor overlaps
(app/rosters.py).
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f6a1b8c4d3e2'
down_revision: Union[str, Sequence[str], None] = 'e5f9a3b7c2d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN rosters JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN rosters")
