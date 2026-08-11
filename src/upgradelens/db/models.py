"""SQLAlchemy ORM models for the stage 4 persistence layer.

Tables
------
* ``code_evidence``  – persisted AST scan usages (stage 2 output)
* ``doc_sources``    – ingested documentation snapshots (with content hash)
* ``doc_chunks``     – heading-aware slices of a source
* ``retrieval_runs`` – recorded keyword retrieval runs
* ``embedding_meta`` – singleton describing the *optional* vector index model

The FTS5 virtual table ``doc_chunks_fts`` is created via raw DDL in
:func:`upgradelens.db.database.init_db` (it cannot be expressed in the ORM
metadata); its ``rowid`` aliases ``doc_chunks.id``. The vector index
(``sqlite-vec``) is likewise created outside the ORM and is entirely optional:
when no embedding backend is configured the index is empty and retrieval falls
back to FTS5-only.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from upgradelens.db.database import Base


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CodeEvidenceRow(Base):
    """Persisted code usage from the AST evidence engine (stage 2)."""

    __tablename__ = "code_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dependency: Mapped[str] = mapped_column(String, index=True)
    path: Mapped[str] = mapped_column(String, index=True)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    column: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String, index=True)
    snippet: Mapped[str] = mapped_column(String, default="")
    content_hash: Mapped[str] = mapped_column(String, default="")
    is_test_code: Mapped[bool] = mapped_column(default=False)
    bound_as: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[str] = mapped_column(String, default="")


class DocSourceRow(Base):
    """An ingested documentation snapshot."""

    __tablename__ = "doc_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    package_name: Mapped[str] = mapped_column(String, default="", index=True)
    url: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String, default="official_doc")
    trust_level: Mapped[str] = mapped_column(String, default="official")
    title: Mapped[str] = mapped_column(String, default="")
    target_version_spec: Mapped[str] = mapped_column(String, default="")
    source_version_spec: Mapped[str] = mapped_column(String, default="")
    snapshot_path: Mapped[str] = mapped_column(String, default="")
    snapshot_hash: Mapped[str] = mapped_column(String, default="")
    fetched_at: Mapped[str] = mapped_column(String, default=_utc_now)


class DocChunkRow(Base):
    """A heading-aware slice of a documentation source."""

    __tablename__ = "doc_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("doc_sources.id"), index=True)
    title: Mapped[str] = mapped_column(String, default="")
    heading_path: Mapped[str] = mapped_column(Text, default="[]")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String, default="")

    @property
    def heading_path_list(self) -> list[str]:
        try:
            return cast(list[str], json.loads(self.heading_path))
        except (json.JSONDecodeError, TypeError):
            return []

    @heading_path_list.setter
    def heading_path_list(self, value: list[str]) -> None:
        self.heading_path = json.dumps(value, ensure_ascii=False)


class RetrievalRunRow(Base):
    """A recorded keyword retrieval run."""

    __tablename__ = "retrieval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    source_id: Mapped[str] = mapped_column(String, index=True)
    query: Mapped[str] = mapped_column(String)
    matched_chunk_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column(String, default=_utc_now)

    @property
    def matched_chunk_ids_list(self) -> list[int]:
        try:
            return cast(list[int], [int(x) for x in json.loads(self.matched_chunk_ids)])
        except (json.JSONDecodeError, TypeError):
            return []

    @matched_chunk_ids_list.setter
    def matched_chunk_ids_list(self, value: list[int]) -> None:
        self.matched_chunk_ids = json.dumps(value, ensure_ascii=False)


class EmbeddingMeta(Base):
    """Singleton describing the embedding model that built the vector index.

    The vector store is optional. When no embedding backend is wired up the
    index is empty and retrieval falls back to FTS5-only, so this row simply
    records *which* model/dimension built whatever vectors exist. A model or
    dimension mismatch is what triggers a clean :meth:`VectorIndex.rebuild`
    instead of silently mixing vectors from different models.
    """

    __tablename__ = "embedding_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    model: Mapped[str] = mapped_column(String, default="")
    dimension: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[str] = mapped_column(String, default=_utc_now)


class DocIngestJobStatus(StrEnum):
    """Lifecycle of a background corpus-backfill job (S17)."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class DocIngestJob(Base):
    """A background job that re-ingests an online-discovered documentation source
    into the shared corpus (S17).

    Enqueued by the retrieval tool when the local corpus misses and the online
    fallback actually fetched something (live mode, ``online_fallback`` network
    policy). A separate worker re-fetches the source through the SSRF-restricted
    fetcher and persists it so the *next* retrieval for the same package hits the
    local corpus instead of the network.
    """

    __tablename__ = "doc_ingest_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: uuid.uuid4().hex)
    package_name: Mapped[str] = mapped_column(String(255), index=True, default="")
    source_url: Mapped[str] = mapped_column(String(2048))
    source_title: Mapped[str] = mapped_column(String(512), default="")
    source_version_spec: Mapped[str] = mapped_column(String(255), default="")
    target_version_spec: Mapped[str] = mapped_column(String(255), default="")
    trust_level: Mapped[str] = mapped_column(String(32), default="community")
    snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    reason: Mapped[str] = mapped_column(String(512), default="")
    discovered: Mapped[str] = mapped_column(Text, default="[]")
    chunks_collected: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32), default=_utc_now)
    updated_at: Mapped[str] = mapped_column(String(32), default=_utc_now)
