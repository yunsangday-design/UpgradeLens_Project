"""Core evaluation gate (plan section 18.5).

These tests are the project's quality contract. They assert not just that the
harness runs, but that the *hybrid* pipeline actually beats an unverified LLM
on the same inputs — which is the entire thesis of the tool.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.eval import BASELINES, EvaluationResult, compare_runs, load_cases, run_evaluation
from upgradelens.llm.prompts import PROMPTS

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"

#: Plan section 18.1 requires at least eight Core fixtures.
MIN_CORE_CASES = 8


@pytest.fixture(scope="module")
def result():
    # Windows file-lock retries: pytest-xdist and the module-scoped tmp_path
    # occasionally race on the same SQLite db the first time the fixture runs.
    # One or two retries are enough; on Linux/macOS the first attempt succeeds.
    last: Exception | None = None
    for _ in range(3):
        try:
            return run_evaluation(CASES_DIR)
        except PermissionError as exc:  # pragma: no cover - platform only
            last = exc
    raise last  # type: ignore[misc]


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


# --- C4: prompt-version tagging + A/B comparison -------------------------
#
# The eval harness is offline (synthetic model reports), so a prompt edit does
# not change the output here. What it changes is the *label*: every result now
# records which prompt template versions produced it, and two runs can be
# diffed to attribute metric movement to the prompt. These tests pin that loop.


def test_result_records_current_prompt_versions(result) -> None:
    # The eval that C3 tuned must be tagged with the v2 few-shot prompts.
    assert result.prompt_versions["breaking_change"] == PROMPTS["breaking_change"].version
    assert result.prompt_versions["impact_report"] == PROMPTS["impact_report"].version
    # Every registered prompt is captured, so nothing can silently go untracked.
    assert set(result.prompt_versions) == set(PROMPTS)


def test_prompt_versions_round_trip_through_json(result) -> None:
    restored = EvaluationResult.from_dict(result.to_dict())
    assert restored.prompt_versions == result.prompt_versions


def _result_dict(
    pass_rate: float,
    citation: float,
    hallucinated: int,
    versions: dict[str, str] | None = None,
) -> dict:
    """A minimal ``to_dict`` blob with one hybrid baseline row."""
    return {
        "schema_version": "eval-result/1",
        "prompt_versions": versions
        or {
            "breaking_change": "v1",
            "impact_report": "v1",
            "planner": "v2",
            "router": "v1",
            "query_rewriter": "v1",
        },
        "cases": ["c1"],
        "baselines": [
            {
                "baseline": "hybrid",
                "cases": 8,
                "passed_cases": round(pass_rate * 8),
                "cited_total": 10,
                "cited_existing": round(citation * 10),
                "pass_rate": pass_rate,
                "citation_existence_rate": citation,
                "hallucinated_verified": hallucinated,
                "failed_checks": {},
            }
        ],
        "details": [],
    }


def _compare(prev: dict, curr: dict) -> str:
    return compare_runs(EvaluationResult.from_dict(curr), prev).overall_verdict()


def test_compare_flags_no_change_as_unchanged() -> None:
    blob = _result_dict(1.0, 1.0, 0)
    assert _compare(blob, blob) == "unchanged"


def test_compare_flags_improvement() -> None:
    prev = _result_dict(0.5, 0.8, 2)
    curr = _result_dict(1.0, 1.0, 0)
    assert _compare(prev, curr) == "improved"


def test_compare_flags_regression_on_pass_rate() -> None:
    prev = _result_dict(1.0, 1.0, 0)
    curr = _result_dict(0.5, 1.0, 0)
    assert _compare(prev, curr) == "regressed"


def test_compare_flags_regression_when_hallucination_increases() -> None:
    """A single extra fabricated citation is a regression even if pass holds."""
    prev = _result_dict(1.0, 1.0, 0)
    curr = _result_dict(1.0, 1.0, 1)
    assert _compare(prev, curr) == "regressed"


def test_compare_flags_mixed_when_some_metrics_move_both_ways() -> None:
    # Pass rate up but citation down: simultaneous improvement and regression.
    prev = _result_dict(0.5, 0.5, 0)
    curr = _result_dict(1.0, 0.2, 0)
    assert _compare(prev, curr) == "mixed"


def test_compare_reports_prompt_version_change_in_markdown() -> None:
    from upgradelens.eval import render_compare_markdown

    prev = _result_dict(1.0, 1.0, 0)
    curr = _result_dict(
        0.5,
        1.0,
        1,
        versions={
            "breaking_change": "v2",  # C3 bump: few-shot examples added
            "impact_report": "v2",
            "planner": "v2",
            "router": "v1",
            "query_rewriter": "v1",
        },
    )
    comparison = compare_runs(EvaluationResult.from_dict(curr), prev)
    md = render_compare_markdown(comparison)
    assert "Prompt A/B comparison" in md
    assert "breaking_change=v1" in md  # previous versions line
    assert "breaking_change=v2" in md  # current versions line (C3 bump)
    assert "Overall verdict" in md
    assert comparison.overall_verdict() == "regressed"
