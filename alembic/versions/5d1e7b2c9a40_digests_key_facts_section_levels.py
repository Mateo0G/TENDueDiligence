"""document digests, key facts, leveled report sections

Revision ID: 5d1e7b2c9a40
Revises: cc4af2da39ed
Create Date: 2026-11-13 00:00:00.000000

Backs the report-quality rebuild modeled on the client-approved CARI Health
report:
- dataroom_files.digest: a per-file structured digest (summary, key facts,
  report tags, execution status) produced by Claude from the file's *full*
  text -- or from the page images for a scanned PDF -- so no file is ever
  silently dropped from drafting the way the old fixed character budget did.
- jobs.key_facts: the cross-document Key Facts sheet (round size, valuation,
  dates, people, conflicts between sources and how they were resolved) that
  every drafting call is grounded on.
- report_sections.heading_level / meta: reports are now Heading 1 parts
  with Heading 2 subsections (CARI's 15-report structure); meta carries a
  report's header line and basis-of-preparation text.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '5d1e7b2c9a40'
down_revision: Union[str, Sequence[str], None] = 'cc4af2da39ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE dataroom_files ADD COLUMN digest JSONB")
    op.execute("ALTER TABLE jobs ADD COLUMN key_facts JSONB")
    op.execute("ALTER TABLE report_sections ADD COLUMN heading_level SMALLINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE report_sections ADD COLUMN meta JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE report_sections DROP COLUMN meta")
    op.execute("ALTER TABLE report_sections DROP COLUMN heading_level")
    op.execute("ALTER TABLE jobs DROP COLUMN key_facts")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN digest")
