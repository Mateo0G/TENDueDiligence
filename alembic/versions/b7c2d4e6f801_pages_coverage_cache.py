"""per-page extraction, coverage fields, cross-job extraction cache

Revision ID: b7c2d4e6f801
Revises: 8f3a91c4d2e7
Create Date: 2026-11-15 00:00:00.000000

- document_pages: every page (PDF page, sheet, slide) of every file with how
  its text was obtained (text layer / local OCR / vision / blank / none) --
  the basis of the coverage check and of page-level retrieval.
- dataroom_files.page_count / pages_extracted / extraction_method / executed.
- extraction_cache: extracted pages and fact card per file content hash,
  shared across jobs, so re-uploading a data room costs nothing for files
  that haven't changed.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b7c2d4e6f801'
down_revision: Union[str, Sequence[str], None] = '8f3a91c4d2e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE document_pages (
            id BIGSERIAL PRIMARY KEY,
            job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            file_id UUID NOT NULL REFERENCES dataroom_files(id) ON DELETE CASCADE,
            page_no INTEGER NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL,
            chars INTEGER NOT NULL DEFAULT 0,
            confidence REAL,
            is_signature BOOLEAN NOT NULL DEFAULT false,
            text_hash TEXT,
            text TEXT NOT NULL DEFAULT '',
            UNIQUE (file_id, page_no)
        )
        """
    )
    op.execute("CREATE INDEX ix_document_pages_job_hash ON document_pages (job_id, text_hash)")
    op.execute("ALTER TABLE dataroom_files ADD COLUMN page_count INTEGER")
    op.execute("ALTER TABLE dataroom_files ADD COLUMN pages_extracted INTEGER")
    op.execute("ALTER TABLE dataroom_files ADD COLUMN extraction_method TEXT")
    op.execute("ALTER TABLE dataroom_files ADD COLUMN executed TEXT")
    op.execute(
        """
        CREATE TABLE extraction_cache (
            sha256 TEXT NOT NULL,
            extract_version INTEGER NOT NULL,
            card_version INTEGER,
            kind TEXT NOT NULL,
            page_count INTEGER NOT NULL,
            pages JSONB NOT NULL,
            executed TEXT,
            card JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (sha256, extract_version)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE extraction_cache")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN executed")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN extraction_method")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN pages_extracted")
    op.execute("ALTER TABLE dataroom_files DROP COLUMN page_count")
    op.execute("DROP TABLE document_pages")
