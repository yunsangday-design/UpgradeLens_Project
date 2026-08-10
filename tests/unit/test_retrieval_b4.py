from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from upgradelens.db import models
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.db.vector import SqliteVecIndex
from upgradelens.docs import ingest
from upgradelens.docs.retrieval import _fuse_source, retrieve_for_package
from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.skills import builtin_registry


class _TestStubEmbedding:
    """TEST-ONLY embedding backend: stands in for a real semantic model.

    Intentionally NOT a real embedding -- it only exercises the vector code
    path (index build, search, RRF fusion) without network access. Production
    code must never use a synthetic vector.
    """

    model = "test-stub"
    dimension = 16

    _VOCAB = [
        "validator",
        "pydantic",
        "v2",
        "field",
        "model",
        "config",
        "migration",
        "basemodel",
        "parse",
        "schema",
        "type",
        "generic",
        "dataclass",
        "root",
        "error",
        "strict",
    ]

    def available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        out = []
        for text in texts:
            vec = [0.0] * len(self._VOCAB)
            low = text.lower()
            for i, tok in enumerate(self._VOCAB):
                if tok in low:
                    vec[i] = 1.0
            out.append(vec)
        return out


@pytest.fixture
def session(tmp_path: Path):
    engine = engine_for(tmp_path / "docs.db")
    init_db(engine)
    s = session_for(engine)()
    for pack in builtin_registry().all():
        ingest.ingest_skill(s, pack)
    s.commit()
    yield s
    s.close()


def _flatten_blobs(runs) -> list[str]:
    blobs: list[str] = []
    for run in runs:
        for ev in run.top_doc_evidence:
            blobs.append(" ".join(ev.heading_path).lower())
    return blobs


def test_fts5_only_finds_validator_without_embedding(session) -> None:
    runs = retrieve_for_package(session, "pydantic", "", "2.0", "", ["validator"], embedding=None)
    assert runs
    assert any("validator" in b for b in _flatten_blobs(runs))


def test_hybrid_retrieval_runs_and_finds_validator(session) -> None:
    stub = _TestStubEmbedding()
    SqliteVecIndex(session, stub.dimension).rebuild(session, stub)
    runs = retrieve_for_package(session, "pydantic", "", "2.0", "", ["validator"], embedding=stub)
    assert runs
    assert any("validator" in b for b in _flatten_blobs(runs))


def test_vector_unavailable_falls_back_to_fts5(session, monkeypatch) -> None:
    monkeypatch.setattr("upgradelens.db.vector.sqlite_vec_available", lambda: False)
    stub = _TestStubEmbedding()
    runs = retrieve_for_package(session, "pydantic", "", "2.0", "", ["validator"], embedding=stub)
    assert runs
    assert any("validator" in b for b in _flatten_blobs(runs))


def test_fuse_source_adds_vector_only_hits(session) -> None:
    # Two chunks in one source: FTS5 found the first, the vector index found the
    # second. RRF fusion must surface both.
    source = models.DocSourceRow(
        id=1,
        package_name="pydantic",
        title="T",
        url="u",
        snapshot_hash="h",
    )
    session.add(source)
    session.flush()
    for cid, title in [(10, "Validators"), (20, "Field types")]:
        session.add(
            models.DocChunkRow(source_id="src-1", title=title, content="x", content_hash=f"c{cid}")
        )
    session.flush()

    rows = (
        session.execute(select(models.DocChunkRow).where(models.DocChunkRow.source_id == "src-1"))
        .scalars()
        .all()
    )
    ids = [r.id for r in rows]
    assert len(ids) == 2
    fts_id, vec_id = ids[0], ids[1]

    fts_run = RetrievalRun(
        run_id="r",
        source_id="src-1",
        query="q",
        matched_chunk_ids=[fts_id],
        top_doc_evidence=[],
        generated_at="now",
    )
    chunk_source = {fts_id: "src-1", vec_id: "src-1"}
    fused = _fuse_source(
        session,
        source_id="src-1",
        query="q",
        fts_run=fts_run,
        vec_hits=[(vec_id, 0.1)],
        chunk_source=chunk_source,
        top_k=5,
    )
    assert set(fused.matched_chunk_ids) == {fts_id, vec_id}
