"""B2: shared-corpus retrieval path no longer needs a dedicated Skill.

Verifies that :func:`retrieve_for_package` collects documentation evidence from
the shared corpus using the fused (intent + version + code-symbol) query without
ever taking a :class:`SkillPackage`, and that :func:`collect_evidence` runs the
full link -- code scan plus doc retrieval -- even when no Skill Pack resolves.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs import ingest
from upgradelens.docs.retrieval import retrieve_for_package
from upgradelens.domain.doc_evidence import DocEvidence
from upgradelens.pipeline import AssessmentRequest, collect_evidence
from upgradelens.skills import builtin_registry
from upgradelens.tools.registry import ToolContext, default_registry


def _tmp_db() -> Path:
    return Path(tempfile.mkdtemp()) / "docs.db"


def _session_for(db: Path) -> Session:
    engine = engine_for(db)
    init_db(engine)
    return session_for(engine)()


def _ingest_builtins(session: Session) -> None:
    for skill in builtin_registry().all():
        ingest.ingest_skill(session, skill)


def test_retrieve_for_package_symbol_driven_finds_validator():
    session = _session_for(_tmp_db())
    try:
        _ingest_builtins(session)
        runs = retrieve_for_package(
            session,
            package="pydantic",
            source_version="",
            target_version="2.0",
            user_intent="",
            code_symbols=["validator"],
            top_k=5,
        )
        assert runs, "shared-corpus retrieval must return runs without a Skill"
        evidences = [ev for run in runs for ev in run.top_doc_evidence]
        assert evidences
        assert all(ev.package_name == "pydantic" for ev in evidences)
        assert all(ev.evidence_id.startswith("doc:") for ev in evidences)

        def blob(ev: DocEvidence) -> str:
            return (ev.chunk_title + " " + " ".join(ev.heading_path) + " " + ev.snippet).lower()

        assert any("validator" in blob(ev) for ev in evidences)
    finally:
        session.close()


def test_retrieve_for_package_uses_curated_queries_when_skill_present():
    session = _session_for(_tmp_db())
    try:
        _ingest_builtins(session)
        skill = builtin_registry().get("pydantic_v1_to_v2")
        curated = [q for pattern in skill.patterns for q in pattern.retrieval_queries]
        assert curated, "pydantic skill should expose curated retrieval queries"
        runs = retrieve_for_package(
            session,
            package="pydantic",
            source_version="",
            target_version="2.0",
            user_intent="",
            code_symbols=[],
            curated_queries=curated,
            top_k=5,
        )
        evidences = [ev for run in runs for ev in run.top_doc_evidence]
        assert evidences, "curated boost queries should surface doc evidence"
    finally:
        session.close()


def _prepare_db(tmp_path: Path) -> Path:
    db = tmp_path / "docs.db"
    session = _session_for(db)
    try:
        _ingest_builtins(session)
    finally:
        session.close()
    return db


def test_collect_evidence_collects_doc_without_skill(monkeypatch, tmp_path):
    db = _prepare_db(tmp_path)
    # Force the resolver to report no Skill Pack. The resolver binds
    # ``resolve_skill_package`` by name, so patch that reference directly.
    monkeypatch.setattr(
        "upgradelens.tools.registry.resolve_skill_package",
        lambda *args, **kwargs: None,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(workdir=tmp_path)
    request = AssessmentRequest(repo=str(repo), dependency="pydantic", target_version="2.0", db=db)
    collection = collect_evidence(request, ctx, registry=default_registry())
    assert collection.skill is None
    doc_items = collection.bundle.by_kind("doc_chunk")
    assert doc_items, "doc evidence must be collected even with no Skill Pack"
    ctx.close()


def test_collect_evidence_collects_doc_with_skill(tmp_path):
    db = _prepare_db(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(workdir=tmp_path)
    request = AssessmentRequest(repo=str(repo), dependency="pydantic", target_version="2.0", db=db)
    collection = collect_evidence(request, ctx, registry=default_registry())
    # LS-1: the main flow is skill-free; pydantic docs come from the shared
    # corpus exactly like any other dependency.
    assert collection.skill is None
    doc_items = collection.bundle.by_kind("doc_chunk")
    assert doc_items, "doc evidence must be collected without a legacy Skill Pack"
    ctx.close()
