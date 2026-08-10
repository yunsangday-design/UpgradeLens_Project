"""Document ingestion: snapshot -> cleaned text -> chunks -> SQLite + FTS5 (stage 4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from packaging.utils import canonicalize_name
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.db.vector import EmbeddingBackend, SqliteVecIndex, VectorIndexUnavailable
from upgradelens.docs.chunking import chunk_markdown
from upgradelens.docs.cleaning import clean_document
from upgradelens.domain.doc_evidence import DocChunk, DocSourceRecord
from upgradelens.domain.skill import DocSource, SkillPackage, TrustLevel
from upgradelens.skills.loader import SkillParseError


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_fixture(skill: SkillPackage, source: DocSource) -> Path:
    if not source.fixture_snapshot:
        raise SkillParseError(
            f"source '{source.id}' has no fixture_snapshot; cannot ingest offline"
        )
    fixture = Path(skill.source_path) / source.fixture_snapshot
    if not fixture.exists():
        raise SkillParseError(f"documentation fixture not found: {fixture}")
    return fixture


def persist_source_text(
    session: Session,
    source: DocSource,
    raw: str,
    snapshot_path: str,
    *,
    trust_level: TrustLevel | None = None,
    title: str | None = None,
    package_name: str = "",
    source_version_spec: str = "",
    embedding: EmbeddingBackend | None = None,
) -> DocSourceRecord:
    """Clean, chunk and persist one documentation source's text.

    Shared by the offline fixture path (stage 4) and the live fetch path
    (stage 7). Re-ingesting the same source id removes its previous chunks
    (and their FTS5 rows) first, so the index stays deduplicated.
    """
    snapshot_hash = _sha256(raw)
    cleaned = clean_document(raw, source.fetch_strategy)
    chunks = chunk_markdown(cleaned, source.id)

    existing = (
        session.execute(select(models.DocChunkRow).where(models.DocChunkRow.source_id == source.id))
        .scalars()
        .all()
    )
    for row in existing:
        session.execute(text("DELETE FROM doc_chunks_fts WHERE rowid = :rid"), {"rid": row.id})
    for row in existing:
        session.delete(row)

    source_row = session.get(models.DocSourceRow, source.id)
    if source_row is None:
        source_row = models.DocSourceRow(id=source.id)
        session.add(source_row)
    source_row.url = source.url
    source_row.source_type = source.source_type
    source_row.trust_level = trust_level or source.trust_level
    source_row.title = source.id
    target_spec = source.target_version_spec or ""
    source_row.target_version_spec = target_spec
    source_row.package_name = package_name
    source_row.source_version_spec = source_version_spec
    source_row.snapshot_path = snapshot_path
    source_row.snapshot_hash = snapshot_hash
    session.flush()

    new_chunks: list[models.DocChunkRow] = []
    for chunk in chunks:
        crow = models.DocChunkRow(
            source_id=source.id,
            title=chunk.title,
            content=chunk.content,
            content_hash=chunk.content_hash,
        )
        crow.heading_path_list = chunk.heading_path
        session.add(crow)
        session.flush()
        new_chunks.append(crow)
        session.execute(
            text(
                "INSERT INTO doc_chunks_fts(rowid, content, source_id, title, heading_path) "
                "VALUES (:rid, :content, :sid, :title, :hp)"
            ),
            {
                "rid": crow.id,
                "content": chunk.content,
                "sid": source.id,
                "title": chunk.title,
                "hp": json.dumps(chunk.heading_path, ensure_ascii=False),
            },
        )
    session.commit()

    if embedding is not None and embedding.available() and new_chunks:
        _embed_and_index(session, embedding, chunks, new_chunks)

    return DocSourceRecord(
        id=source.id,
        url=source.url,
        title=source.id,
        snapshot_hash=snapshot_hash,
        target_version_spec=target_spec,
        package_name=package_name,
        source_version_spec=source_version_spec,
        chunk_count=len(chunks),
    )


def _embed_and_index(
    session: Session,
    embedding: EmbeddingBackend,
    chunks: list[DocChunk],
    new_chunks: list[models.DocChunkRow],
) -> None:
    """Embed freshly persisted chunks and upsert them into the vector index.

    Best-effort: a missing sqlite-vec extension or an embedding failure simply
    skips the vector index for this source, leaving retrieval on FTS5-only.
    """
    try:
        vec_index = SqliteVecIndex(session, embedding.dimension)
    except VectorIndexUnavailable:
        return
    texts = [f"{chunk.title}\n{chunk.content}" for chunk in chunks]
    vectors = embedding.embed(texts)
    if not vectors:
        return
    for crow, vec in zip(new_chunks, vectors, strict=False):
        vec_index.upsert(crow.id, vec)


def ingest_skill_source(
    session: Session,
    skill: SkillPackage,
    source: DocSource,
    embedding: EmbeddingBackend | None = None,
) -> DocSourceRecord:
    """Clean, chunk and persist one documentation source for a skill.

    The source is tagged with the skill's dependency package name (canonicalised)
    and source/target version specifiers so it becomes part of the shared corpus
    retrievable by package alone — independent of any usage pattern.
    """
    fixture = _resolve_fixture(skill, source)
    raw = fixture.read_text(encoding="utf-8")
    package_name = canonicalize_name(skill.package_names[0]) if skill.package_names else ""
    source_version_spec = skill.source_version_spec or ""
    return persist_source_text(
        session,
        source,
        raw,
        str(fixture),
        package_name=package_name,
        source_version_spec=source_version_spec,
        embedding=embedding,
    )


def iter_sources_for_package(session: Session, package_name: str) -> list[DocSourceRecord]:
    """Return every ingested source tagged with ``package_name``.

    This is the shared-corpus entry point: retrieval can be driven by the
    dependency package alone, without reading any Skill's usage patterns.
    """
    rows = (
        session.execute(
            select(models.DocSourceRow).where(models.DocSourceRow.package_name == package_name)
        )
        .scalars()
        .all()
    )
    return [
        DocSourceRecord(
            id=r.id,
            url=r.url,
            title=r.title,
            snapshot_hash=r.snapshot_hash,
            target_version_spec=r.target_version_spec,
            package_name=r.package_name,
            source_version_spec=r.source_version_spec,
            chunk_count=0,
        )
        for r in rows
    ]


def ingest_skill(
    session: Session, skill: SkillPackage, embedding: EmbeddingBackend | None = None
) -> list[DocSourceRecord]:
    """Ingest every source of ``skill`` that has an offline fixture snapshot."""
    records: list[DocSourceRecord] = []
    for source in skill.sources:
        if not source.fixture_snapshot:
            continue
        records.append(ingest_skill_source(session, skill, source, embedding=embedding))
    return records
