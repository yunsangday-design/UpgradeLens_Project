"""SQLite engine/session management and schema bootstrap (stage 4).

The schema is intentionally minimal: four business tables
(``code_evidence``, ``doc_sources``, ``doc_chunks``, ``retrieval_runs``) plus a
FTS5 virtual table over ``doc_chunks``. ``init_db`` is idempotent so it can be
called both by the application/tests and by the Alembic initial migration.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Connection, Engine, MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

DEFAULT_DB_PATH = "upgradelens.db"
FTS5_TABLE = "doc_chunks_fts"

_FTS5_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS5_TABLE} "
    "USING fts5(content, source_id UNINDEXED, title UNINDEXED, heading_path UNINDEXED)"
)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def engine_for(path: str | Path) -> Engine:
    """Build a SQLAlchemy engine for the given SQLite database path."""
    url = str(path)
    if not url.startswith("sqlite"):
        url = f"sqlite:///{Path(path).expanduser()}"
    return create_engine(url, future=True)


def create_schema(bind: Engine | Connection) -> None:
    """Create all regular tables and the FTS5 virtual table (idempotent).

    ``bind`` may be an :class:`~sqlalchemy.Engine` or an active
    :class:`~sqlalchemy.orm.Session`/Connection. Importing :mod:`upgradelens.db.models`
    ensures the metadata is populated before ``create_all`` runs. The FTS5 DDL is
    idempotent (``IF NOT EXISTS``) so the function is safe to call repeatedly and
    is reused by the Alembic initial migration.
    """
    from upgradelens.db import models  # noqa: F401  (populate metadata)

    Base.metadata.create_all(bind)
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            conn.execute(text(_FTS5_DDL))
    else:
        bind.execute(text(_FTS5_DDL))


def init_db(engine: Engine) -> None:
    """Bootstrap the schema on ``engine`` (idempotent)."""
    with engine.begin() as conn:
        create_schema(conn)


def session_for(engine: Engine) -> sessionmaker[Session]:
    """Return a configured session factory bound to ``engine``."""
    return sessionmaker(bind=engine, future=True)
