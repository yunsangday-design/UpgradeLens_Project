"""Offline S8 architecture-comparison tests (direct LLM vs pipeline vs agent).

Runs entirely in FAKE mode: each case's model output is replayed through the
pipeline/agent via deterministic fakes, so no network access is required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.eval import SYSTEMS, ComparisonReport, run_comparison
from upgradelens.eval.cases import load_cases

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"

# Subset small enough to keep the suite fast; includes two hallucination cases
# (with model_report.json + must_quarantine_risk_ids) and a static-fallback case.
SELECTED = ["hallucinated_citation", "fastapi_depends", "alias_import"]

# Compute the 10x factor once to avoid accidental drift between the two guards.


def _selected_cases():
    return [c for c in load_cases(CASES_DIR) if c.case_id in SELECTED]


@pytest.fixture()
def cases():
    return _selected_cases()


def test_comparison_runs_all_systems(cases):
    report = run_comparison(cases)
    assert isinstance(report, ComparisonReport)
    for case_id in SELECTED:
        assert case_id in report.per_case
        for sys in SYSTEMS:
            assert sys in report.per_case[case_id]


def test_rates_are_bounded(cases):
    report = run_comparison(cases)
    for systems in report.per_case.values():
        for metrics in systems.values():
            for key, val in metrics.as_dict().items():
                if key == "verifier_detection_rate":
                    # None means the case declared no known-bad claims (n/a).
                    assert val is None or 0.0 <= val <= 1.0, f"{key}={val}"
                    continue
                if key in {
                    "breaking_change_recall",
                    "code_location_recall",
                    "doc_accuracy",
                    "no_evidence_rate",
                    "coverage",
                }:
                    assert 0.0 <= val <= 1.0, f"{key}={val} out of range"


def test_verifier_detects_fabricated_claim_hallucinated_case(cases):
    """The fabrication case's fabricated risk must be quarantined by the
    verification-enabled systems but trusted by the bare LLM baseline."""
    report = run_comparison(cases)
    per = report.per_case["hallucinated_citation"]
    direct = per["direct_llm"].verifier_detection_rate
    pipeline = per["fixed_pipeline"].verifier_detection_rate
    agent = per["agent"].verifier_detection_rate

    # Bare LLM accepts the fabricated claim as verified -> nothing detected.
    assert direct == 0.0
    # Pipeline + agent run the verifier, which drops the unsupported claim.
    assert pipeline == 1.0
    assert agent == 1.0


def test_aggregate_excludes_none_plan_rate_for_non_agent(cases):
    report = run_comparison(cases)
    agg = report.aggregate()
    assert "direct_llm" in agg
    assert "fixed_pipeline" in agg
    assert "agent" in agg
    # Non-agent systems have no plan, so plan_completion_rate must be absent.
    assert "plan_completion_rate" not in agg["direct_llm"]
    assert "plan_completion_rate" not in agg["fixed_pipeline"]
    assert agg["agent"]["plan_completion_rate"] is not None


def test_export_formats(cases):
    report = run_comparison(cases)
    md = report.to_markdown()
    assert "# S8 Architecture Comparison" in md
    assert "hallucinated_citation" in md
    payload = report.to_json()
    assert payload["systems"] == list(SYSTEMS)
    assert "hallucinated_citation" in payload["per_case"]
    assert "aggregate" in payload
