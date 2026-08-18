"""Cross-capability gold-set evaluation (research report M1c)."""

from __future__ import annotations

from upgradelens.eval.capability_eval import (
    load_gold_cases,
    run_capability_eval,
    score_case,
)

EXPECTED_KINDS = {
    "pr_review",
    "security_review",
    "issue_repair",
    "breaking_change",
}


def test_gold_cases_load_with_four_kinds():
    cases = load_gold_cases()
    assert len(cases) >= 8
    assert {c.kind for c in cases} == EXPECTED_KINDS


def test_every_gold_case_passes_in_fake_mode():
    cases = load_gold_cases()
    for case in cases:
        score = score_case(case, mode="fake")
        assert score.passed, f"{case.kind}/{case.name} failed: {score.reasons}"


def test_run_capability_eval_aggregates_per_kind():
    report = run_capability_eval(mode="fake")
    assert report.total_cases == len(load_gold_cases())
    assert report.total_passed == report.total_cases
    assert report.overall_pass_rate == 1.0
    assert report.hallucination_free_rate == 1.0
    assert set(report.per_kind.keys()) == EXPECTED_KINDS
    # Every kind must be fully green in fake mode.
    for kind, agg in report.per_kind.items():
        assert agg["n_passed"] == agg["n_cases"], f"{kind} had failures"
        assert agg["verification_pass"] == agg["n_cases"], f"{kind} verification failed"


def test_run_capability_eval_report_serializes():
    report = run_capability_eval(mode="fake")
    d = report.to_dict()
    assert d["total_cases"] == report.total_cases
    assert "scoreboard_md" in d
