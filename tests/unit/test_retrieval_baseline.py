"""Tests for the retrieval evaluation baseline (ROADMAP Step 4, B0).

These assert that the *current* curated FTS5 path surfaces every labelled
expected chunk in the top-5 — a regression guard that any future retrieval
change (sqlite-vec hybrid, query rewriting, ...) must preserve or beat.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.docs.ingest import ingest_skill
from upgradelens.eval.retrieval_baseline import (
    _evaluate_ranking,
    build_pattern_ranking,
    load_retrieval_cases,
    render_retrieval_baseline_markdown,
    run_baseline,
)
from upgradelens.skills import builtin_registry

RETRIEVAL_CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval"


def _make_session(tmp_path: Path) -> Session:
    engine = engine_for(tmp_path / "retrieval_baseline.db")
    init_db(engine)
    return session_for(engine)()


def _builtin(skill_id: str):
    return builtin_registry().get(skill_id)


def test_evaluate_ranking_math():
    ranking = [["A", "X"], ["B", "Y"], ["C", "Z"]]
    top, ranks, best, hit, mrr, recall, tk = _evaluate_ranking(ranking, ["Y"], top_k=5)
    assert ranks == [2]
    assert best == 2
    assert hit is True
    assert mrr == 0.5
    assert recall["1"] == 0.0
    assert recall["3"] == 1.0
    assert tk["3"] is True
    assert tk["5"] is True


def test_evaluate_ranking_missing_expected():
    ranking = [["A", "X"], ["B", "Y"]]
    _, ranks, best, hit, mrr, recall, tk = _evaluate_ranking(ranking, ["Z"], top_k=5)
    assert ranks == [None]
    assert best is None
    assert hit is False
    assert mrr == 0.0
    assert recall["5"] == 0.0


def test_load_retrieval_cases_covers_both_packages(tmp_path: Path):
    cases = load_retrieval_cases(RETRIEVAL_CASES_DIR)
    assert len(cases) == 12
    packages = {c.package for c in cases}
    assert packages == {"pydantic", "sqlalchemy"}


def test_pydantic_validator_ranking_surfaces_expected_chunk(tmp_path: Path):
    session = _make_session(tmp_path)
    skill = _builtin("pydantic_v1_to_v2")
    ingest_skill(session, skill)
    pattern = next(p for p in skill.patterns if p.id == "pydantic_validator")
    ranking = build_pattern_ranking(session, skill, pattern, top_k=5)
    leaves = [hp[-1] for hp in ranking]
    assert "@validator → @field_validator" in leaves[:5]


def test_baseline_over_builtin_fixtures_hits_top5(tmp_path: Path):
    session = _make_session(tmp_path)
    skills = [_builtin("pydantic_v1_to_v2"), _builtin("sqlalchemy_v1_to_v2")]
    cases = load_retrieval_cases(RETRIEVAL_CASES_DIR)
    report = run_baseline(session, skills, cases, top_k=5)

    assert report.n_cases == 12
    assert report.top_k == 5
    for r in report.cases:
        assert r.hit is True
        assert r.best_rank is not None and r.best_rank <= 5
        assert r.top_k_hit["5"] is True
        assert set(r.recall_at_k) == {"1", "3", "5", "10"}
    assert report.summary["avg_mrr"] > 0


def test_markdown_report_is_rendered(tmp_path: Path):
    session = _make_session(tmp_path)
    skills = [_builtin("pydantic_v1_to_v2"), _builtin("sqlalchemy_v1_to_v2")]
    cases = load_retrieval_cases(RETRIEVAL_CASES_DIR)
    report = run_baseline(session, skills, cases, top_k=5)
    md = render_retrieval_baseline_markdown(report)
    assert "Retrieval Baseline" in md
    assert "MRR" in md
    assert str(report.summary["avg_mrr"]) in md
