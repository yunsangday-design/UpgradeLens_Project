"""S6: the shared corpus serves Skill-less dependencies as well as Skill-backed ones.

Two claims are under test here, and they are the reason S6 exists:

1. Retrieval driven purely by *package + upgrade window + scanned symbols*
   recalls the right documentation, including for ``flask`` / ``httpx`` /
   ``attrs``, which have no Skill Pack at all.
2. The Skills that do exist no longer get a hidden advantage: their
   hand-written ``retrieval_queries`` are off by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.eval.retrieval_baseline import (
    RetrievalBaselineReport,
    RetrievalCaseResult,
    compare_reports,
    ingest_corpus_for_baseline,
    ingest_skills_for_baseline,
    load_retrieval_cases,
    render_comparison_markdown,
    run_shared_corpus_baseline,
)
from upgradelens.pipeline import AssessmentRequest, legacy_skill_boost_queries
from upgradelens.skills import builtin_registry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS_DIR = FIXTURES / "corpus"
CASES_DIR = FIXTURES / "retrieval_shared"

#: Dependencies present only as corpus data -- deliberately no Skill Pack.
CORPUS_ONLY_PACKAGES = {"flask", "httpx", "attrs"}


@pytest.fixture(scope="module")
def corpus_session(tmp_path_factory: pytest.TempPathFactory) -> Session:
    """A store holding both migrated (Skill) and Skill-free (manifest) corpora."""
    db_path = tmp_path_factory.mktemp("s6-corpus") / "corpus.db"
    engine = engine_for(db_path)
    init_db(engine)
    session = session_for(engine)()
    ingest_skills_for_baseline(session, builtin_registry().all())
    ingest_corpus_for_baseline(session, CORPUS_DIR)
    return session


@pytest.fixture(scope="module")
def shared_report(corpus_session: Session) -> RetrievalBaselineReport:
    cases = load_retrieval_cases(CASES_DIR)
    return run_shared_corpus_baseline(corpus_session, cases, top_k=5)


# --------------------------------------------------------------------------- #
# Corpus coverage
# --------------------------------------------------------------------------- #


def test_evaluation_covers_five_dependencies(shared_report: RetrievalBaselineReport) -> None:
    """A two-package fixture set cannot show that the corpus generalises."""
    packages = {case.package for case in shared_report.cases}

    assert packages == {"pydantic", "sqlalchemy", "flask", "httpx", "attrs"}
    assert len(shared_report.cases) >= 10


def test_corpus_only_packages_have_no_skill_pack() -> None:
    """Guard the premise: if someone adds a flask Skill, these tests stop proving anything."""
    registry = builtin_registry()
    skill_packages = {name for skill in registry.all() for name in skill.package_names}

    assert CORPUS_ONLY_PACKAGES.isdisjoint(skill_packages)


# --------------------------------------------------------------------------- #
# Retrieval quality without curated queries
# --------------------------------------------------------------------------- #


def test_shared_corpus_recalls_every_labelled_chunk(
    shared_report: RetrievalBaselineReport,
) -> None:
    # Semantic cases are deliberately FTS5-resistant (their query paraphrases the
    # intent so the literal terms never appear in the chunk); they exist to prove
    # the hybrid path, so they are excluded from the "FTS5 must recall everything"
    # bar.
    literal_missed = [
        case.case_id for case in shared_report.cases if not case.hit and not case.semantic
    ]

    assert literal_missed == []
    literal = [c for c in shared_report.cases if not c.semantic]
    literal_recall = sum(c.recall_at_k["5"] for c in literal) / len(literal)
    assert literal_recall == 1.0


def test_skill_free_packages_retrieve_as_well_as_skill_backed_ones(
    shared_report: RetrievalBaselineReport,
) -> None:
    """The S6 acceptance bar: no Skill must not mean worse answers."""
    corpus_only = [c for c in shared_report.cases if c.package in CORPUS_ONLY_PACKAGES]
    skill_backed = [c for c in shared_report.cases if c.package not in CORPUS_ONLY_PACKAGES]
    assert corpus_only and skill_backed

    # Semantic cases are FTS5-resistant by design, so the S6 acceptance bar
    # (Skill-free packages retrieve as well as Skill-backed ones) only applies to
    # the literal cases.
    corpus_literal = [c for c in corpus_only if not c.semantic]

    def mean_mrr(cases: list[RetrievalCaseResult]) -> float:
        return sum(c.mrr for c in cases) / len(cases)

    assert mean_mrr(corpus_literal) >= mean_mrr(skill_backed)
    assert all(c.best_rank is not None and c.best_rank <= 3 for c in corpus_literal)


def test_semantic_cases_are_resistant_to_fts5(
    shared_report: RetrievalBaselineReport,
) -> None:
    """Semantic cases must fail under FTS5-only, else they do not prove anything.

    Their query is a paraphrase, so the literal terms never appear in the expected
    chunk -- FTS5 has nothing to match on. The hybrid (vector) path is what rescues
    them, which is exactly the claim under test in the S6 retrieval eval.
    """
    semantic_cases = [c for c in shared_report.cases if c.semantic]
    assert semantic_cases, "expected at least one semantic case fixture"

    for case in semantic_cases:
        assert case.best_rank is None, (
            f"{case.case_id} was recalled by FTS5 (rank {case.best_rank}); "
            "it should be FTS5-resistant so the hybrid path is what earns the recall"
        )


def test_shared_baseline_reports_the_path_it_measured(
    shared_report: RetrievalBaselineReport,
) -> None:
    assert "no curated queries" in shared_report.notes
    assert "FTS5-only" in shared_report.notes


def test_shared_baseline_degrades_to_fts5_without_an_embedding_backend(
    corpus_session: Session,
) -> None:
    """A missing vector backend costs ranking quality, never evidence."""
    cases = load_retrieval_cases(CASES_DIR)

    report = run_shared_corpus_baseline(corpus_session, cases, top_k=5, embedding=None)

    assert report.n_cases == len(cases)
    # Semantic cases are FTS5-resistant by design, so without a vector backend they
    # legitimately do not recall -- only the literal cases must still be recalled.
    literal = [case for case in report.cases if not case.semantic]
    assert all(case.hit for case in literal)


# --------------------------------------------------------------------------- #
# Retiring the curated query boost
# --------------------------------------------------------------------------- #


def test_skill_query_boost_is_off_by_default() -> None:
    skill = builtin_registry().get("pydantic_v1_to_v2")
    assert skill is not None

    assert legacy_skill_boost_queries(skill, enabled=False) == []


def test_skill_query_boost_still_available_for_comparison() -> None:
    """Kept switchable so the removal can be measured, not merely asserted."""
    skill = builtin_registry().get("pydantic_v1_to_v2")
    assert skill is not None

    queries = legacy_skill_boost_queries(skill, enabled=True)

    assert queries
    assert queries == [q for p in skill.patterns for q in p.retrieval_queries]


def test_skill_query_boost_tolerates_a_dependency_with_no_skill() -> None:
    assert legacy_skill_boost_queries(None, enabled=True) == []


def test_assessment_request_does_not_opt_into_the_boost() -> None:
    """Callers must have to ask for the legacy path, never inherit it."""
    request = AssessmentRequest(repo="/tmp/repo", dependency="pydantic")

    assert request.legacy_skill_query_boost is False
    assert (
        legacy_skill_boost_queries(
            builtin_registry().get("pydantic_v1_to_v2"), enabled=request.legacy_skill_query_boost
        )
        == []
    )


# --------------------------------------------------------------------------- #
# FTS5 vs hybrid comparison
# --------------------------------------------------------------------------- #


def _report(*, mrr: float, ranks: dict[str, int | None]) -> RetrievalBaselineReport:
    cases = [
        RetrievalCaseResult(
            case_id=case_id,
            package="flask",
            pattern_id="",
            expected_chunks=["x"],
            top_chunks=["x"],
            ranks=[rank],
            best_rank=rank,
            hit=rank is not None,
            mrr=(1.0 / rank) if rank else 0.0,
            recall_at_k={"1": 0.0, "3": 0.0, "5": 1.0 if rank else 0.0, "10": 0.0},
            top_k_hit={"1": False, "3": False, "5": rank is not None, "10": False},
        )
        for case_id, rank in ranks.items()
    ]
    return RetrievalBaselineReport(
        generated_at="2026-08-10T00:00:00+00:00",
        top_k=5,
        n_cases=len(cases),
        cases=cases,
        summary={
            "n_cases": len(cases),
            "avg_mrr": mrr,
            "avg_recall_at_k": {"1": 0.0, "3": 0.0, "5": 1.0, "10": 0.0},
            "avg_top_k_hit_rate": {"1": 0.0, "3": 0.0, "5": 1.0, "10": 0.0},
        },
    )


def test_comparison_attributes_wins_per_case_not_just_on_average() -> None:
    """An unchanged average can hide one package's recall paying for another's."""
    fts = _report(mrr=0.5, ranks={"a": 1, "b": 4})
    hybrid = _report(mrr=0.5, ranks={"a": 4, "b": 1})

    comparison = compare_reports(fts, hybrid)

    assert comparison["n_compared"] == 2
    assert comparison["mrr"]["delta"] == 0.0
    assert comparison["hybrid_wins"] == ["b"]
    assert comparison["fts_wins"] == ["a"]


def test_comparison_counts_a_newly_recalled_case_as_a_hybrid_win() -> None:
    fts = _report(mrr=0.0, ranks={"a": None})
    hybrid = _report(mrr=1.0, ranks={"a": 1})

    comparison = compare_reports(fts, hybrid)

    assert comparison["hybrid_wins"] == ["a"]
    assert comparison["fts_wins"] == []
    assert comparison["mrr"]["delta"] == pytest.approx(1.0)


def test_comparison_renders_a_reviewable_summary() -> None:
    comparison = compare_reports(_report(mrr=0.5, ranks={"a": 2}), _report(mrr=1.0, ranks={"a": 1}))

    markdown = render_comparison_markdown(comparison)

    assert "FTS5-only vs hybrid" in markdown
    assert "+0.500" in markdown
    assert "hybrid wins: a" in markdown
