from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.db import models
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.db.vector import (
    SqliteVecIndex,
    VectorIndexUnavailable,
    sqlite_vec_available,
)

# sqlite-vec needs a sqlite3 build with extension loading; some CI-provisioned
# Pythons (GitHub Actions) omit ``enable_load_extension`` and the index falls
# back to unavailable there.
requires_sqlite_vec = pytest.mark.skipif(
    not sqlite_vec_available(),
    reason="sqlite3 build lacks extension loading (e.g. GitHub-Actions Python)",
)


def _session(tmp_path: Path):
    engine = engine_for(tmp_path / "docs.db")
    init_db(engine)
    session = session_for(engine)()
    yield session
    session.close()


@requires_sqlite_vec
def test_sqlite_vec_is_available_on_this_platform() -> None:
    assert sqlite_vec_available() is True


def test_negative_dimension_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(VectorIndexUnavailable):
        SqliteVecIndex(next(_session(tmp_path)), 0)


def test_unavailable_when_extension_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("upgradelens.db.vector.sqlite_vec_available", lambda: False)
    with pytest.raises(VectorIndexUnavailable):
        SqliteVecIndex(next(_session(tmp_path)), 4)


class _StubEmbedding:
    """TEST-ONLY embedding backend -- stands in for a real semantic model.

    Production code must never use a synthetic vector; this exists only to
    exercise the vector code path (index build, search, rebuild) offline.
    """

    model = "test-stub"
    dimension = 4

    def available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return [
            [float(len(t) % 7), float(len(t) % 5), float(len(t) % 3), float(len(t) % 2)]
            for t in texts
        ]


@requires_sqlite_vec
def test_upsert_search_and_delete(tmp_path: Path) -> None:
    session = next(_session(tmp_path))
    index = SqliteVecIndex(session, 4)
    assert index.available() is True

    index.upsert(1, [0.1, 0.2, 0.3, 0.4])
    index.upsert(2, [0.9, 0.8, 0.7, 0.6])
    hits = index.search([0.11, 0.21, 0.29, 0.41], top_k=2)
    assert [h.chunk_id for h in hits] == [1, 2]

    index.delete(1)
    hits = index.search([0.11, 0.21, 0.29, 0.41], top_k=2)
    assert [h.chunk_id for h in hits] == [2]


@requires_sqlite_vec
def test_rebuild_from_chunks(tmp_path: Path) -> None:
    session = next(_session(tmp_path))
    source = models.DocSourceRow(
        id=1,
        package_name="pydantic",
        title="Pydantic Docs",
        url="https://docs.pydantic.dev",
        snapshot_hash="h",
    )
    session.add(source)
    session.flush()
    chunk = models.DocChunkRow(
        source_id=source.id, title="Validators", content="validator fields", content_hash="c1"
    )
    session.add(chunk)
    session.flush()
    session.commit()

    index = SqliteVecIndex(session, 4)
    stub = _StubEmbedding()
    assert index.rebuild(session, stub) == 1

    vec = stub.embed([f"{chunk.title}\n{chunk.content}"])[0]
    hits = index.search(vec, top_k=1)
    assert hits[0].chunk_id == chunk.id


@requires_sqlite_vec
def test_rebuild_records_meta(tmp_path: Path) -> None:
    session = next(_session(tmp_path))
    index = SqliteVecIndex(session, 4)
    index.rebuild(session, _StubEmbedding())
    meta = session.get(models.EmbeddingMeta, 1)
    assert meta is not None
    assert meta.model == "test-stub"
    assert meta.dimension == 4
