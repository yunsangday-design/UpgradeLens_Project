"""add doc_ingest_jobs table (S17).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Single source of truth: the ORM metadata already describes the table, so we
    # let SQLAlchemy emit only this one table. checkfirst is on, so on a fresh DB
    # (where 0001's create_schema already materialised it) this is a safe no-op,
    # while on a pre-existing DB it actually creates the table.
    from upgradelens.db.database import Base
    from upgradelens.db.models import DocIngestJob

    Base.metadata.create_all(op.get_bind(), tables=[DocIngestJob.__table__])


def downgrade() -> None:
    from upgradelens.db.models import DocIngestJob

    DocIngestJob.__table__.drop(op.get_bind(), checkfirst=True)
