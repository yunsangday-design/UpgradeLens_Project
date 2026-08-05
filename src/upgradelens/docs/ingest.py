"""Document ingestion: snapshot → cleaned text → chunks → SQLite + FTS5 (stage 4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.docs.chunking import chunk_markdown
from upgradelens.docs.cleaning import clean_document
from upgradelens.domain.doc_evidence import DocSourceRecord
from upgradelens.domain.skill import DocSource, SkillPackage
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


def ingest_skill_source(
    session: Session, skill: SkillPackage, source: DocSource
) -> DocSourceRecord:
    """Clean, chunk and persist one documentation source for a skill.

    Re-ingesting the same source id removes its previous chunks (and their FTS5
    rows) first, so the index stays deduplicated and reflects the latest snapshot.
    """
    fixture = _resolve_fixture(skill, source)
    raw = fixture.read_text(encoding="utf-8")
    snapshot_hash = _sha256(raw)
    cleaned = clean_document(raw, source.fetch_strategy)
    chunks = chunk_markdown(cleaned, source.id)

    # De-duplicate: drop previous chunks and their FTS5 rows for this source.
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
    source_row.trust_level = source.trust_level
    source_row.title = source.id
    target_spec = source.target_version_spec or ""
    source_row.target_version_spec = target_spec
    source_row.snapshot_path = str(fixture)
    source_row.snapshot_hash = snapshot_hash
    session.flush()

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
    return DocSourceRecord(
        id=source.id,
        url=source.url,
        title=source.id,
        snapshot_hash=snapshot_hash,
        target_version_spec=target_spec,
        chunk_count=len(chunks),
    )


def ingest_skill(session: Session, skill: SkillPackage) -> list[DocSourceRecord]:
    """Ingest every source of ``skill`` that has an offline fixture snapshot."""
    records: list[DocSourceRecord] = []
    for source in skill.sources:
        if not source.fixture_snapshot:
            continue
        records.append(ingest_skill_source(session, skill, source))
    return records
