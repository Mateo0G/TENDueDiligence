"""structured facts computed from spreadsheets

Revision ID: e5f9a3b7c2d1
Revises: d4e8f2a1b6c3
Create Date: 2026-11-20 00:00:00.000000

jobs.structured_facts: cap table, preferred prices, funding rounds, grants,
scenarios and derived figures (conversion price, implied next-round price),
parsed from the data room's spreadsheets in code (app/structured.py).
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'e5f9a3b7c2d1'
down_revision: Union[str, Sequence[str], None] = 'd4e8f2a1b6c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN structured_facts JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN structured_facts")
