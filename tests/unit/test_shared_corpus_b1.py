"""B1: shared corpus + evidence contract.

Verifies that builtin documentation fixtures become retrievable by dependency
package (and version) *without* reading any Skill's usage patterns, and that the
retrieved :class:`DocEvidence` carries a stable evidence id plus the
package/version/trust/hash contract fields into the :class:`EvidenceBundle`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import ingest
from upgradelens.docs.retrieval import retrieve
from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeEvidenceSummary
from upgradelens.models.impact import build_bundle
from upgradelens.skills import builtin_registry


def _session() -> Session:
    db_path = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)
    engine = engine_for(str(db_path))
    init_db(engine)
    return session_for(engine)()


def _ingest_builtins(session: Session) -> None:
    for skill in builtin_registry().all():
        ingest.ingest_skill(session, skill)


def test_ingest_tags_builtin_fixtures_by_package():
    session = _session()
    try:
        _ingest_builtins(session)
        pydantic = ingest.iter_sources_for_package(session, "pydantic")
        sqlalchemy = ingest.iter_sources_for_package(session, "sqlalchemy")
        assert pydantic, "pydantic fixtures should be in the shared corpus"
        assert sqlalchemy, "sqlalchemy fixtures should be in the shared corpus"
        assert {s.id for s in pydantic}.isdisjoint({s.id for s in sqlalchemy})
        assert all(s.package_name == "pydantic" for s in pydantic)
        assert all(s.package_name == "sqlalchemy" for s in sqlalchemy)
    finally:
        session.close()


def test_retrieve_returns_package_tagged_evidence_with_stable_id():
    session = _session()
    try:
        _ingest_builtins(session)
        pyd_ids = [s.id for s in ingest.iter_sources_for_package(session, "pydantic")]
        run = retrieve(session, pyd_ids[0], "validator field_validator", top_k=3, record=False)
        assert run.top_doc_evidence
        ev = run.top_doc_evidence[0]
        assert ev.package_name == "pydantic"
        assert ev.evidence_id.startswith("doc:")
        assert ev.trust_level
        # Stability: identical inputs must yield an identical evidence id.
        run2 = retrieve(session, pyd_ids[0], "validator field_validator", top_k=3, record=False)
        assert run2.top_doc_evidence[0].evidence_id == ev.evidence_id
    finally:
        session.close()


def test_bundle_carries_stable_doc_evidence_id_and_contract():
    session = _session()
    try:
        _ingest_builtins(session)
        pyd_ids = [s.id for s in ingest.iter_sources_for_package(session, "pydantic")]
        run = retrieve(session, pyd_ids[0], "validator field_validator", top_k=3, record=False)
        code_report = CodeEvidenceReport(
            dependency_name="pydantic",
            scanned_files=0,
            summary=CodeEvidenceSummary(scanned_files=0, usage_count=0),
        )
        bundle = build_bundle(code_report, [run], dependency="pydantic")
        doc_items = bundle.by_kind("doc_chunk")
        assert doc_items, "bundle should include doc_chunk evidence"
        item = doc_items[0]
        assert item.evidence_id == run.top_doc_evidence[0].evidence_id
        assert item.meta.get("package_name") == "pydantic"
        assert "trust_level" in item.meta
        assert "chunk_content_hash" in item.meta
        assert "source_version_spec" in item.meta
    finally:
        session.close()
