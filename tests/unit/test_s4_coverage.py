"""ROADMAP Step 4 -- evidence coverage + autonomous supplementary retrieval.

These tests cover:

* the deterministic coverage logic (``compute_coverage`` / ``gap_query``),
* the supplementary-retrieval loop (``_run_supplement``) closing gaps via a mock
  registry both when the doc store can cover a symbol and when it cannot,
* trace/plan attribution of the supplementary calls,
* the end-to-end path through the real ``retrieve_for_package`` tool + a real
  SQLite doc store (exercising the new ``curated_queries`` plumbing).

The detection is deterministic, so ``fake`` and ``live`` behave identically; the
only ``live``-specific piece (LLM query rewrite) is guarded and falls back to the
deterministic template on any error.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import text

from upgradelens.agent.coverage import (
    CoverageGap,
    CoverageResult,
    CoverageSummary,
    compute_coverage,
    gap_query,
    summarize,
)
from upgradelens.agent.loop import (
    _MAX_SUPPLEMENTARY,
    _Accumulator,
    _run_supplement_phase,
    run_agent,
)
from upgradelens.agent.plan import PENDING, SUCCEEDED, AgentPlan, AgentPlanStep
from upgradelens.agent.planner import build_agent_plan
from upgradelens.db import models as db_models
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.domain.code_evidence import (
    SCHEMA_VERSION,
    CodeEvidenceReport,
    CodeEvidenceSummary,
    CodeUsage,
    UsageKind,
)
from upgradelens.domain.doc_evidence import DocEvidence, RetrievalRun
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.pipeline import COVERAGE_INSUFFICIENT, AssessmentRequest
from upgradelens.tools.registry import ToolContext, default_registry
from upgradelens.tools.trace import ToolCallEvent, ToolTrace

# --- fixtures / builders ----------------------------------------------------- #


def _code_report(symbols: list[str]) -> CodeEvidenceReport:
    usages = [
        CodeUsage(
            path=f"{s}.py",
            start_line=1,
            end_line=1,
            column=0,
            kind=UsageKind.CALL,
            symbol=s,
            snippet=f"obj.{s}()",
            content_hash="h",
            is_test_code=False,
            confidence="high",
        )
        for s in symbols
    ]
    return CodeEvidenceReport(
        schema_version=SCHEMA_VERSION,
        dependency_name="pydantic",
        scanned_files=len(symbols),
        usages=usages,
        summary=CodeEvidenceSummary(scanned_files=len(symbols), usage_count=len(usages)),
    )


def _evidence(run_id: str, symbol: str, source_id: str = "src1") -> DocEvidence:
    return DocEvidence(
        evidence_id=f"e-{run_id}",
        source_id=source_id,
        url="http://example.com/docs",
        title="section",
        chunk_title="chunk",
        heading_path=["migration"],
        snapshot_hash="snap",
        snippet=f"Use {symbol} when upgrading pydantic to v2.",
        score=1.0,
        matched_query=symbol,
        package_name="pydantic",
        target_version_spec="2.0",
        chunk_content_hash="h",
    )


def _run(run_id: str, symbol: str, source_id: str = "src1") -> RetrievalRun:
    return RetrievalRun(
        run_id=run_id,
        source_id=source_id,
        query=symbol,
        top_doc_evidence=[_evidence(run_id, symbol, source_id)],
    )


def _build_doc_db(path: Path, chunks: list[tuple[str, str, str]]) -> None:
    """Create a real SQLite doc store with the given (source_id, title, content)."""
    engine = engine_for(str(path))
    init_db(engine)
    session = session_for(engine)()
    seen: set[str] = set()
    for source_id, _title, _content in chunks:
        if source_id not in seen:
            seen.add(source_id)
            session.add(
                db_models.DocSourceRow(
                    id=source_id,
                    package_name="pydantic",
                    url="http://example.com/docs",
                    source_type="official_doc",
                    trust_level="official",
                    title=source_id,
                    target_version_spec="2.0",
                    source_version_spec="1.x",
                    snapshot_hash="snap",
                )
            )
    for source_id, title, content in chunks:
        row = db_models.DocChunkRow(source_id=source_id, title=title, content=content)
        row.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        session.add(row)
        session.flush()
        session.execute(
            text(
                "INSERT INTO doc_chunks_fts(rowid, content, source_id, title, heading_path) "
                "VALUES (:rid, :content, :sid, :title, :hp)"
            ),
            {"rid": row.id, "content": content, "sid": source_id, "title": title, "hp": "[]"},
        )
    session.commit()
    session.close()


def _request(db: str | None) -> AssessmentRequest:
    return AssessmentRequest(
        repo="/repo",
        dependency="pydantic",
        source_version="1.x",
        target_version="2.0",
        user_intent="",
        source_id=None,
        db=db,
    )


def _fake_gateway() -> ModelGateway:
    return ModelGateway(ModelConfig(model="fake", mode=ModelMode.FAKE, api_key="", base_url=""))


def _ctx(gateway: ModelGateway) -> ToolContext:
    return ToolContext(trace=ToolTrace(), gateway=gateway)


# --- pure coverage logic ---------------------------------------------------- #


def test_compute_coverage_finds_gaps() -> None:
    code_report = _code_report(["model_dump", "orm_mode"])
    # Only model_dump is covered by the single evidence blob.
    runs = [_run("r1", "model_dump")]
    result = compute_coverage(code_report, runs)
    assert isinstance(result, CoverageResult)
    assert result.total_symbols == 2
    assert result.covered_symbols == 1
    assert result.uncovered_symbols == 1
    assert result.coverage_rate == 0.5
    assert [g.symbol for g in result.gaps] == ["orm_mode"]
    gap = result.gaps[0]
    assert isinstance(gap, CoverageGap)
    assert gap.usage_count == 1
    assert gap.sample_paths == ["orm_mode.py"]


def test_compute_coverage_full_coverage() -> None:
    code_report = _code_report(["model_dump", "orm_mode"])
    runs = [_run("r1", "model_dump"), _run("r2", "orm_mode")]
    result = compute_coverage(code_report, runs)
    assert result.covered_symbols == 2
    assert result.uncovered_symbols == 0
    assert result.coverage_rate == 1.0
    assert result.gaps == []


def test_compute_coverage_empty_code_report() -> None:
    result = compute_coverage(_code_report([]), [])
    assert result.total_symbols == 0
    assert result.coverage_rate == 0.0
    assert result.gaps == []


def test_gap_query_is_deterministic() -> None:
    gap = CoverageGap(symbol="orm_mode", usage_count=1, sample_paths=[], reason="x")
    q = gap_query(
        gap,
        package="pydantic",
        source_version="1.x",
        target_version="2.0",
        user_intent="",
    )
    assert q == "pydantic orm_mode 2.0 migration upgrade"
    # user_intent is appended when provided
    q2 = gap_query(
        gap,
        package="pydantic",
        source_version="1.x",
        target_version="2.0",
        user_intent="speed up",
    )
    assert q2 == "pydantic orm_mode 2.0 speed up migration upgrade"


def test_summarize() -> None:
    result = compute_coverage(_code_report(["a"]), [])
    summary = summarize(result, supplementary_count=3)
    assert isinstance(summary, CoverageSummary)
    assert summary.total_symbols == 1
    assert summary.covered_symbols == 0
    assert summary.uncovered_symbols == 1
    assert summary.coverage_rate == 0.0
    assert summary.supplementary_count == 3
    assert summary.gaps == ["a"]


def _run_with_snippet(run_id: str, snippet: str) -> RetrievalRun:
    ev = DocEvidence(
        evidence_id=f"e-{run_id}",
        source_id="src1",
        url="http://example.com/docs",
        title="section",
        chunk_title="chunk",
        heading_path=["migration"],
        snapshot_hash="snap",
        snippet=snippet,
        score=1.0,
        matched_query="x",
        package_name="pydantic",
        target_version_spec="2.0",
        chunk_content_hash="h",
    )
    return RetrievalRun(run_id=run_id, source_id="src1", query="x", top_doc_evidence=[ev])


def test_symbol_matches_across_naming_styles() -> None:
    code = _code_report(["declarative_base"])
    # Doc uses CamelCase / kebab-case / space variants of the same identifier.
    for variant in [
        "Use DeclarativeBase for the model.",
        "see declarative-base usage.",
        "the declarative base pattern is common.",
    ]:
        result = compute_coverage(code, [_run_with_snippet("r", variant)])
        assert result.covered_symbols == 1, variant


def test_symbol_semantic_gap_still_unmatched() -> None:
    code = _code_report(["sessionmaker"])
    result = compute_coverage(code, [_run_with_snippet("r", "a session factory is provided")])
    assert result.uncovered_symbols == 1
    assert result.gaps[0].symbol == "sessionmaker"


def test_short_symbol_does_not_match_longer_word() -> None:
    code = _code_report(["get"])
    result = compute_coverage(code, [_run_with_snippet("r", "the target function is called")])
    assert result.uncovered_symbols == 1


def test_components_scattered_in_prose_do_not_match() -> None:
    code = _code_report(["declarative_base"])
    # Unrelated words sit between the two components -> not the same identifier.
    result = compute_coverage(
        code,
        [_run_with_snippet("r", "the declarative pattern is common; the base module handles it")],
    )
    assert result.uncovered_symbols == 1


# --- supplementary loop via mock registry ----------------------------------- #


class _MockRegistry:
    """A registry stub that returns a covering RetrievalRun for ``covered`` symbols."""

    def __init__(self, covered: set[str]) -> None:
        self.covered = covered
        self.calls: list[tuple[str, list[str] | None]] = []

    def run(self, method: str, args: dict, ctx: ToolContext) -> list[dict]:
        assert method == "retrieve_for_package"
        symbol = args["code_symbols"][0]
        self.calls.append((symbol, args.get("curated_queries")))
        ctx.trace.events.append(
            ToolCallEvent(tool=method, target=symbol, status="ok", latency_ms=1, params=args)
        )
        if symbol in self.covered:
            return [_run(f"run-{symbol}", symbol).model_dump(mode="json")]
        return []


def _supplement_setup(
    symbols: list[str],
) -> tuple[AgentPlan, AgentPlanStep, _Accumulator, ToolContext, ModelGateway]:
    gateway = _fake_gateway()
    plan = AgentPlan(request_id="r", intent={})
    step = AgentPlanStep(
        id="s1", tool="supplement_retrieval", seq=1, status=PENDING, phase="collect"
    )
    plan.steps.append(step)
    acc = _Accumulator(
        repo_path=Path("/repo"),
        code_report=_code_report(symbols),
        source_version_spec="1.x",
        target_version_spec="2.0",
    )
    return plan, step, acc, _ctx(gateway), gateway


def test_run_supplement_closes_all_gaps() -> None:
    plan, step, acc, ctx, gateway = _supplement_setup(["model_dump", "orm_mode"])
    registry = _MockRegistry(covered={"model_dump", "orm_mode"})
    _run_supplement_phase(
        plan, acc, _request(str(Path("/db"))), registry, ctx, gateway, None, max_supplementary=2
    )
    observation = step.observation

    assert step.status == SUCCEEDED
    assert plan.coverage is not None
    assert plan.coverage.total_symbols == 2
    assert plan.coverage.covered_symbols == 2
    assert plan.coverage.coverage_rate == 1.0
    assert plan.coverage.supplementary_count == 2
    assert not acc.coverage_insufficient
    # Both supplementary calls were recorded and attributed to the step.
    assert len(ctx.trace.events) == 2
    assert all(e.plan_step_id == "s1" for e in ctx.trace.events)
    assert all(e.evidence_ids for e in ctx.trace.events)
    assert "coverage 100%" in observation


def test_run_supplement_insufficient_flags_degradation() -> None:
    plan, step, acc, ctx, gateway = _supplement_setup(["model_dump", "orm_mode"])
    # DB covers model_dump but not orm_mode -> one gap remains after max tries.
    registry = _MockRegistry(covered={"model_dump"})
    _run_supplement_phase(
        plan, acc, _request(str(Path("/db"))), registry, ctx, gateway, None, max_supplementary=2
    )
    observation = step.observation

    assert step.status == SUCCEEDED
    assert acc.coverage_insufficient is True
    assert plan.coverage is not None
    assert plan.coverage.uncovered_symbols == 1
    assert plan.coverage.gaps == ["orm_mode"]
    assert any("insufficient" in n for n in plan.notes)
    # Exactly max_supplementary attempts, one per distinct gap symbol.
    assert plan.coverage.supplementary_count == 2
    assert "gap(s) remaining" in observation


def test_run_supplement_caps_at_max_supplementary() -> None:
    plan, step, acc, ctx, gateway = _supplement_setup(["a", "b", "c", "d"])
    # Nothing is covered; only max_supplementary attempts happen, not one per symbol.
    registry = _MockRegistry(covered=set())
    _run_supplement_phase(
        plan, acc, _request(str(Path("/db"))), registry, ctx, gateway, None, max_supplementary=2
    )
    assert len(registry.calls) == 2
    assert acc.coverage_insufficient is True
    assert plan.coverage is not None
    assert plan.coverage.supplementary_count == 2


def test_run_supplement_phase_skips_without_db() -> None:
    gateway = _fake_gateway()
    plan = AgentPlan(request_id="r", intent={})
    step = AgentPlanStep(
        id="s1", tool="supplement_retrieval", seq=1, status=PENDING, phase="collect"
    )
    plan.steps.append(step)
    acc = _Accumulator(
        repo_path=Path("/repo"),
        code_report=_code_report(["model_dump"]),
        source_version_spec="1.x",
        target_version_spec="2.0",
    )
    _run_supplement_phase(
        plan,
        acc,
        _request(None),
        default_registry(),
        _ctx(gateway),
        gateway,
        None,
        _MAX_SUPPLEMENTARY,
    )
    assert step.status == "skipped"
    assert plan.coverage is None  # never reached coverage assessment


# --- end-to-end through the real retrieve_for_package tool ------------------- #


def test_run_supplement_phase_real_db_closes_gap(tmp_path: Path) -> None:
    db = tmp_path / "docs.db"
    _build_doc_db(
        db,
        [
            ("src1", "model_dump", "Use pydantic model_dump to serialize models in v2."),
            ("src1", "orm_mode", "Config.orm_mode is replaced by from_attributes in pydantic v2."),
        ],
    )
    gateway = _fake_gateway()
    registry = default_registry()
    plan = AgentPlan(request_id="r", intent={})
    step = AgentPlanStep(
        id="s1", tool="supplement_retrieval", seq=1, status=PENDING, phase="collect"
    )
    plan.steps.append(step)
    acc = _Accumulator(
        repo_path=Path("/repo"),
        code_report=_code_report(["model_dump", "orm_mode"]),
        source_version_spec="1.x",
        target_version_spec="2.0",
    )
    ctx = _ctx(gateway)
    _run_supplement_phase(
        plan, acc, _request(str(db)), registry, ctx, gateway, None, max_supplementary=2
    )

    assert step.status == SUCCEEDED
    assert plan.coverage is not None
    assert plan.coverage.total_symbols == 2
    assert plan.coverage.covered_symbols == 2
    assert plan.coverage.coverage_rate == 1.0
    # The version-broad retrieval can close several gaps with one call, so we
    # only assert that at least one supplementary retrieval happened (<= cap).
    assert 1 <= plan.coverage.supplementary_count <= _MAX_SUPPLEMENTARY
    assert not acc.coverage_insufficient
    # Real tool recorded trace events attributed to the supplement step.
    assert ctx.trace.events
    assert 1 <= len(ctx.trace.events) <= _MAX_SUPPLEMENTARY
    assert all(e.plan_step_id == "s1" for e in ctx.trace.events)
    assert all(e.evidence_ids for e in ctx.trace.events)


def test_run_supplement_phase_real_db_insufficient_degradation(tmp_path: Path) -> None:
    db = tmp_path / "docs.db"
    _build_doc_db(
        db,
        [("src1", "model_dump", "Use pydantic model_dump to serialize models in v2.")],
    )
    gateway = _fake_gateway()
    registry = default_registry()
    plan = AgentPlan(request_id="r", intent={})
    step = AgentPlanStep(
        id="s1", tool="supplement_retrieval", seq=1, status=PENDING, phase="collect"
    )
    plan.steps.append(step)
    acc = _Accumulator(
        repo_path=Path("/repo"),
        code_report=_code_report(["model_dump", "orm_mode"]),
        source_version_spec="1.x",
        target_version_spec="2.0",
    )
    ctx = _ctx(gateway)
    _run_supplement_phase(
        plan, acc, _request(str(db)), registry, ctx, gateway, None, max_supplementary=2
    )

    assert acc.coverage_insufficient is True
    assert plan.coverage is not None
    assert plan.coverage.uncovered_symbols == 1
    assert plan.coverage.gaps == ["orm_mode"]


def test_run_agent_fake_records_coverage_with_doc_db(tmp_path: Path) -> None:
    """Full driven loop: the post-collection supplement phase sets plan.coverage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    (repo / "main.py").write_text("import pydantic\n\nclass M(pydantic.BaseModel):\n    x: int\n")
    db = tmp_path / "docs.db"
    _build_doc_db(
        db,
        [("src1", "model_dump", "Use pydantic model_dump to serialize models in v2.")],
    )
    gateway = _fake_gateway()
    registry = default_registry()
    request = AssessmentRequest(
        repo=str(repo),
        dependency="pydantic",
        source_version="1.x",
        target_version="2.0",
        user_intent="",
        source_id=None,
        db=str(db),
    )
    plan = build_agent_plan(
        gateway=gateway,
        registry=registry,
        repo=str(repo),
        dependency="pydantic",
        target_version="2.0",
        source_version="1.x",
        repo_is_url=False,
    )
    captured: list[AgentPlan] = []
    ctx = _ctx(gateway)

    outcome = run_agent(
        request,
        gateway,
        ctx,
        registry=registry,
        plan=plan,
        plan_writer=captured.append,
        max_turns=24,
    )

    last_plan = captured[-1] if captured else plan
    supplement = next(s for s in last_plan.steps if s.tool == "supplement_retrieval")
    # S4 post-collection phase runs (or is skipped when no code evidence was found).
    assert supplement.status in (SUCCEEDED, "skipped")
    if supplement.status == SUCCEEDED:
        assert last_plan.coverage is not None
        # Self-consistent: degradation present iff there are uncovered symbols.
        has_gap = last_plan.coverage.uncovered_symbols > 0
        assert (COVERAGE_INSUFFICIENT in outcome.degradations) == has_gap
    assert outcome.verified is not None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
