"""Document ingestion: snapshot -> cleaned text -> chunks -> SQLite + FTS5 (stage 4).

Since S6 the primary entry point is a *source manifest*
(:func:`ingest_manifest_file` / :func:`ingest_corpus`): a package's documents
enter the shared corpus without any Skill Pack being involved. The
``ingest_skill*`` functions remain as a thin compatibility layer over the same
generic path so the built-in Skills keep working while they are migrated.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.db.vector import EmbeddingBackend, SqliteVecIndex, VectorIndexUnavailable
from upgradelens.docs.chunking import chunk_markdown
from upgradelens.docs.cleaning import clean_document
from upgradelens.docs.source_manifest import (
    DocSourceManifestError,
    discover_manifests,
    load_source_manifest,
    resolve_snapshot,
)
from upgradelens.domain.doc_evidence import DocChunk, DocSourceRecord
from upgradelens.domain.doc_source_spec import DocSourceManifest, DocSourceSpec, TrustLevel
from upgradelens.domain.skill import DocSource, SkillPackage
from upgradelens.skills.compat import skill_source_to_spec
from upgradelens.skills.loader import SkillParseError


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def persist_source_text(
    session: Session,
    spec: DocSourceSpec,
    raw: str,
    snapshot_path: str,
    *,
    trust_level: TrustLevel | None = None,
    embedding: EmbeddingBackend | None = None,
) -> DocSourceRecord:
    """Clean, chunk and persist one documentation source's text.

    Shared by the offline snapshot path (stage 4 / S6 manifests) and the live
    fetch path (stage 7). ``trust_level`` overrides the spec's declaration,
    which the live path uses because a fetched URL can drift from what the
    author assumed.

    Re-ingesting the same source id removes its previous chunks (and their
    FTS5 rows) first, so the index stays deduplicated.
    """
    snapshot_hash = _sha256(raw)
    cleaned = clean_document(raw, spec.fetch_strategy)
    chunks = chunk_markdown(cleaned, spec.id)

    existing = (
        session.execute(select(models.DocChunkRow).where(models.DocChunkRow.source_id == spec.id))
        .scalars()
        .all()
    )
    for row in existing:
        session.execute(text("DELETE FROM doc_chunks_fts WHERE rowid = :rid"), {"rid": row.id})
    for row in existing:
        session.delete(row)

    source_row = session.get(models.DocSourceRow, spec.id)
    if source_row is None:
        source_row = models.DocSourceRow(id=spec.id)
        session.add(source_row)
    source_row.url = spec.url
    source_row.source_type = spec.source_type
    source_row.trust_level = trust_level or spec.trust_level
    source_row.title = spec.display_title
    source_row.target_version_spec = spec.target_version_spec
    source_row.package_name = spec.canonical_package
    source_row.source_version_spec = spec.source_version_spec
    source_row.snapshot_path = snapshot_path
    source_row.snapshot_hash = snapshot_hash
    session.flush()

    new_chunks: list[models.DocChunkRow] = []
    for chunk in chunks:
        crow = models.DocChunkRow(
            source_id=spec.id,
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
                "sid": spec.id,
                "title": chunk.title,
                "hp": json.dumps(chunk.heading_path, ensure_ascii=False),
            },
        )
    session.commit()

    if embedding is not None and embedding.available() and new_chunks:
        _embed_and_index(session, embedding, chunks, new_chunks)

    return DocSourceRecord(
        id=spec.id,
        url=spec.url,
        title=spec.display_title,
        snapshot_hash=snapshot_hash,
        target_version_spec=spec.target_version_spec,
        package_name=spec.canonical_package,
        source_version_spec=spec.source_version_spec,
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


# --------------------------------------------------------------------------- #
# Generic (Skill-free) ingestion -- the S6 main path
# --------------------------------------------------------------------------- #


def ingest_source_spec(
    session: Session,
    spec: DocSourceSpec,
    *,
    base_dir: str | Path,
    embedding: EmbeddingBackend | None = None,
) -> DocSourceRecord:
    """Ingest one snapshot-backed source, tagged for the shared corpus.

    Raises:
        DocSourceManifestError: the snapshot is missing or outside ``base_dir``.
    """
    snapshot = resolve_snapshot(spec, base_dir)
    raw = snapshot.read_text(encoding="utf-8")
    return persist_source_text(session, spec, raw, str(snapshot), embedding=embedding)


def ingest_manifest(
    session: Session,
    manifest: DocSourceManifest,
    *,
    base_dir: str | Path | None = None,
    embedding: EmbeddingBackend | None = None,
) -> list[DocSourceRecord]:
    """Ingest every source of an already-loaded manifest."""
    root = Path(base_dir) if base_dir is not None else Path(manifest.base_dir)
    return [
        ingest_source_spec(session, spec, base_dir=root, embedding=embedding)
        for spec in manifest.sources
    ]


def ingest_manifest_file(
    session: Session, path: str | Path, *, embedding: EmbeddingBackend | None = None
) -> list[DocSourceRecord]:
    """Load a source manifest from disk and ingest everything it declares."""
    manifest = load_source_manifest(path)
    return ingest_manifest(session, manifest, embedding=embedding)


def ingest_corpus(
    session: Session, root: str | Path, *, embedding: EmbeddingBackend | None = None
) -> list[DocSourceRecord]:
    """Ingest every manifest under ``root`` (or ``root`` itself, if it is one).

    This is what "add a dependency to the corpus" looks like end to end: drop a
    manifest plus its snapshots into the corpus tree and point this at it. No
    Skill Pack is created, and none is needed.
    """
    records: list[DocSourceRecord] = []
    for manifest_path in discover_manifests(root):
        records.extend(ingest_manifest_file(session, manifest_path, embedding=embedding))
    return records


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


# --------------------------------------------------------------------------- #
# Skill compatibility layer (deprecated, see upgradelens.skills.compat)
# --------------------------------------------------------------------------- #


def ingest_skill_source(
    session: Session,
    skill: SkillPackage,
    source: DocSource,
    embedding: EmbeddingBackend | None = None,
) -> DocSourceRecord:
    """Ingest one Skill-declared source through the generic path.

    Deprecated since S6: declare a source manifest instead. Kept so built-in
    Skills keep working; snapshot problems are still reported as
    :class:`SkillParseError` for callers that catch it.
    """
    spec = skill_source_to_spec(skill, source)
    try:
        return ingest_source_spec(
            session, spec, base_dir=Path(skill.source_path), embedding=embedding
        )
    except DocSourceManifestError as exc:
        raise SkillParseError(str(exc)) from exc


def ingest_skill(
    session: Session, skill: SkillPackage, embedding: EmbeddingBackend | None = None
) -> list[DocSourceRecord]:
    """Ingest every source of ``skill`` that has an offline fixture snapshot.

    Deprecated since S6 -- see :func:`ingest_corpus`.
    """
    records: list[DocSourceRecord] = []
    for source in skill.sources:
        if not source.fixture_snapshot:
            continue
        records.append(ingest_skill_source(session, skill, source, embedding=embedding))
    return records
