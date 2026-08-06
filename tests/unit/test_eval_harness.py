"""Core evaluation gate (plan section 18.5).

These tests are the project's quality contract. They assert not just that the
harness runs, but that the *hybrid* pipeline actually beats an unverified LLM
on the same inputs — which is the entire thesis of the tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.eval import BASELINES, load_cases, run_evaluation

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"

#: Plan section 18.1 requires at least eight Core fixtures.
MIN_CORE_CASES = 8


@pytest.fixture(scope="module")
def result():
    return run_evaluation(CASES_DIR)


def _summary(result, baseline: str):
    found = next((s for s in result.summaries if s.baseline == baseline), None)
    assert found is not None, f"baseline '{baseline}' missing from summaries"
    return found


def test_core_fixture_count() -> None:
    cases = load_cases(CASES_DIR)
    assert len(cases) >= MIN_CORE_CASES
    assert len({c.case_id for c in cases}) == len(cases), "case ids must be unique"


def test_every_case_repo_exists() -> None:
    for case in load_cases(CASES_DIR):
        assert case.repo.is_dir(), f"{case.case_id}: missing repo/ directory"


def test_all_baselines_are_evaluated(result) -> None:
    assert {s.baseline for s in result.summaries} == set(BASELINES)


def test_hybrid_passes_every_case(result) -> None:
    """The gate: the shipped pipeline must satisfy every Core expectation."""
    hybrid = _summary(result, "hybrid")
    failures = [s.case_id for s in result.scores if s.baseline == "hybrid" and not s.passed]
    assert failures == [], f"hybrid failed cases: {failures}"
    assert hybrid.pass_rate == 1.0


def test_hybrid_never_presents_hallucination_as_verified(result) -> None:
    """No fabricated citation may ever appear in the verified section."""
    assert _summary(result, "hybrid").hallucinated_verified == 0
    assert _summary(result, "static_only").hallucinated_verified == 0


def test_unverified_llm_does_leak_hallucinations(result) -> None:
    """Guards the counterfactual.

    If this ever hits zero the fixtures no longer exercise hallucination, and
    the verifier's headline metric would become vacuous.
    """
    assert _summary(result, "llm_only").hallucinated_verified >= 1


def test_verification_beats_the_unverified_baseline(result) -> None:
    """Same model input, one with verification: hybrid must score strictly higher."""
    hybrid = _summary(result, "hybrid")
    llm_only = _summary(result, "llm_only")
    assert hybrid.pass_rate > llm_only.pass_rate


def test_evaluation_is_deterministic() -> None:
    """Two runs over the same fixtures must agree exactly."""
    first = run_evaluation(CASES_DIR).to_dict()
    second = run_evaluation(CASES_DIR).to_dict()
    assert first == second


def test_result_serialises_to_plain_json_types(result) -> None:
    payload = result.to_dict()
    assert payload["schema_version"] == "eval-result/1"
    assert len(payload["details"]) == len(result.case_ids) * len(BASELINES)


def test_unknown_baseline_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown baseline"):
        run_evaluation(CASES_DIR, baselines=["nope"])
