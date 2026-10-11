"""company logo

Revision ID: cc4af2da39ed
Revises: 94b21c6a14b1
Create Date: 2026-10-11 00:00:00.000000

Lets a job carry an optional company logo image, embedded on the generated
report's cover page the same way the reference TEN-authored reports do.
Mirrors pitch_deck_key (object storage key only, R2 holds the bytes) rather
than a dataroom_files row -- a logo isn't dataroom content, so there's
nothing to classify or list alongside the uploaded documents.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'cc4af2da39ed'
down_revision: Union[str, Sequence[str], None] = '94b21c6a14b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN logo_key TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN logo_key")
