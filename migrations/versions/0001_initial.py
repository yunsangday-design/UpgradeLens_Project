"""Initial schema: code_evidence, doc_sources, doc_chunks, retrieval_runs, FTS5.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-06

This single migration bootstraps the whole stage 4 schema by delegating to
:func:`upgradelens.db.database.create_schema`, which is also used by the
application at runtime. Keeping one source of truth avoids metadata/SQL drift.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from upgradelens.db.database import Base, create_schema

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    create_schema(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP TABLE IF EXISTS doc_chunks_fts"))
    Base.metadata.drop_all(bind)
