"""S8 ablation tests: isolate verifier / supplement / agent value (offline FAKE)."""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.eval import ABLATION_SYSTEMS, ComparisonReport, run_ablation
from upgradelens.eval.cases import load_cases

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"

SELECTED = ["hallucinated_citation", "fastapi_depends", "alias_import"]


def _selected_cases():
    return [c for c in load_cases(CASES_DIR) if c.case_id in SELECTED]


@pytest.fixture()
def cases():
    return _selected_cases()


def test_ablation_runs_all_systems(cases):
    report = run_ablation(cases)
    assert isinstance(report, ComparisonReport)
    assert report.systems == ABLATION_SYSTEMS
    for case_id in SELECTED:
        assert case_id in report.per_case
        for sys in ABLATION_SYSTEMS:
            assert sys in report.per_case[case_id]


def test_ablation_verifier_isolation(cases):
    """Bare LLM must accept fabricated claims; verified systems must quarantine them."""
    report = run_ablation(cases)
    for case_id in ("hallucinated_citation", "fastapi_depends"):
        per = report.per_case[case_id]
        # direct_llm: no verifier → fabrication leaks as verified → detection = 0
        assert per["direct_llm"].verifier_detection_rate == 0.0
        # all verified systems quarantine the fabrication
        for sys in ("fixed_pipeline", "agent_no_supplement", "agent"):
            assert per[sys].verifier_detection_rate == 1.0


def test_ablation_supplement_does_not_reduce_coverage(cases):
    """Disabling supplement must not increase coverage (it can only match or drop)."""
    report = run_ablation(cases)
    for case_id in SELECTED:
        per = report.per_case[case_id]
        no_supp = per["agent_no_supplement"].coverage
        with_supp = per["agent"].coverage
        assert with_supp >= no_supp


def test_ablation_aggregate_has_all_systems(cases):
    report = run_ablation(cases)
    agg = report.aggregate()
    assert set(agg.keys()) == set(ABLATION_SYSTEMS)
    # direct_llm has no plan
    assert "plan_completion_rate" not in agg["direct_llm"]
    # agent systems have plan
    for sys in ("agent_no_supplement", "agent"):
        assert agg[sys]["plan_completion_rate"] is not None


def test_ablation_export_formats(cases):
    report = run_ablation(cases)
    md = report.to_markdown()
    assert "agent_no_supplement" in md
    payload = report.to_json()
    assert payload["systems"] == list(ABLATION_SYSTEMS)
    assert "aggregate" in payload
