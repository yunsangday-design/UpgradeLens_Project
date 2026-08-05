"""Tests for ingestion + keyword RAG over the offline Pydantic fixture (stage 4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from upgradelens.db import models
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import ingest_skill, retrieve
from upgradelens.skills import builtin_registry

SOURCE_ID = "pydantic_migration_guide"
PYDANTIC_URL = "https://docs.pydantic.dev/latest/migration/"


@pytest.fixture
def session(tmp_path: Path):
    eng = engine_for(tmp_path / "upgradelens.db")
    init_db(eng)
    s = session_for(eng)()
    yield s
    s.close()


@pytest.fixture
def skill():
    return builtin_registry().get("pydantic_v1_to_v2")


@pytest.fixture
def ingested(session, skill):
    records = ingest_skill(session, skill)
    return records


def _boost_terms(skill) -> frozenset[str]:
    return frozenset(t for p in skill.patterns for t in p.match)


def test_ingest_populates_source_and_chunks(session, ingested) -> None:
    assert ingested
    count = session.execute(select(func.count(models.DocChunkRow.id))).scalar_one()
    assert count > 5
    src = session.get(models.DocSourceRow, SOURCE_ID)
    assert src is not None
    assert src.snapshot_hash
    assert src.url == PYDANTIC_URL


def test_key_api_queries_hit_correct_sections(session, ingested, skill) -> None:
    expectations = {
        "validator": "@validator → @field_validator",
        "root_validator": "@root_validator → @model_validator",
        "orm_mode": "Config.orm_mode → from_attributes",
        "model_dump": ".dict() → model_dump()",
        "parse_obj": ".parse_obj() → .model_validate()",
    }
    for query, expected_title in expectations.items():
        run = retrieve(
            session,
            SOURCE_ID,
            query,
            top_k=3,
            boost_terms=_boost_terms(skill),
        )
        assert run.top_doc_evidence, f"no evidence for query '{query}'"
        titles = [ev.chunk_title for ev in run.top_doc_evidence]
        assert expected_title in titles, (query, titles)
        evidence = next(ev for ev in run.top_doc_evidence if ev.chunk_title == expected_title)
        assert evidence.url == PYDANTIC_URL
        assert evidence.snapshot_hash
        assert evidence.heading_path


def test_evidence_backlinks_to_snapshot(session, ingested) -> None:
    run = retrieve(session, SOURCE_ID, "validator", top_k=1)
    evidence = run.top_doc_evidence[0]
    assert evidence.source_id == SOURCE_ID
    assert evidence.heading_path
    assert evidence.snapshot_hash == session.get(models.DocSourceRow, SOURCE_ID).snapshot_hash


def test_retrieval_run_is_recorded(session, ingested) -> None:
    retrieve(session, SOURCE_ID, "validator", top_k=2)
    n = session.execute(select(func.count(models.RetrievalRunRow.id))).scalar_one()
    assert n >= 1


def test_reingest_is_idempotent(session, skill) -> None:
    ingest_skill(session, skill)
    first = session.execute(select(func.count(models.DocChunkRow.id))).scalar_one()
    ingest_skill(session, skill)
    second = session.execute(select(func.count(models.DocChunkRow.id))).scalar_one()
    assert second == first


def test_no_match_returns_empty_run(session, ingested) -> None:
    run = retrieve(session, SOURCE_ID, "zzz_nonexistent_token_zzz", top_k=3)
    assert run.top_doc_evidence == []
    assert run.matched_chunk_ids == []
