"""S8 CI gate tests: assert no-hallucination and minimum-quality thresholds.

These tests are the CI gate for the S8 comparison harness. They run the full
offline FAKE comparison over every case that declares ``must_quarantine_risk_ids``
and assert that the verified systems quarantine every fabricated claim while the
bare LLM baseline does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from upgradelens.eval import SYSTEMS, run_comparison
from upgradelens.eval.cases import load_cases

CASES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "eval"

#: Minimum verifier detection rate for verified systems (1.0 = every fabrication
#: is caught). The gate fails if any verified system trusts a fabricated claim.
MIN_VERIFIER_DETECTION = 1.0

#: Maximum no-evidence rate for verified systems (0.0 = every surfaced risk has
#: at least code or doc evidence). The gate fails if verified systems present
#: more than this fraction of risks with zero evidence.
MAX_NO_EVIDENCE_RATE = 0.05


def _quarantine_cases():
    """Every case that declares known-bad claims to quarantine."""
    return [c for c in load_cases(CASES_DIR) if c.expect.must_quarantine_risk_ids]


@pytest.fixture()
def cases():
    return _quarantine_cases()


def test_gate_verified_systems_quarantine_all_fabrications(cases):
    """Every verified system must quarantine every declared fabrication."""
    report = run_comparison(cases, systems=SYSTEMS)
    for case_id in [c.case_id for c in cases]:
        per = report.per_case[case_id]
        # direct_llm has no verifier → must NOT catch (detection = 0)
        assert per["direct_llm"].verifier_detection_rate == 0.0, (
            f"{case_id}: direct_llm should trust fabrications as verified"
        )
        # verified systems must catch every fabrication
        for sys in ("fixed_pipeline", "agent"):
            det = per[sys].verifier_detection_rate
            assert det is not None, f"{case_id}/{sys}: detection rate is None"
            assert det >= MIN_VERIFIER_DETECTION, (
                f"{case_id}/{sys}: verifier detection {det:.2f} "
                f"< required {MIN_VERIFIER_DETECTION:.2f}"
            )


def test_gate_verified_systems_low_no_evidence_rate(cases):
    """Verified systems must not present unsupported risks as verified."""
    report = run_comparison(cases, systems=SYSTEMS)
    for case_id in [c.case_id for c in cases]:
        per = report.per_case[case_id]
        for sys in ("fixed_pipeline", "agent"):
            rate = per[sys].no_evidence_rate
            assert rate <= MAX_NO_EVIDENCE_RATE, (
                f"{case_id}/{sys}: no-evidence rate {rate:.2f} > limit {MAX_NO_EVIDENCE_RATE:.2f}"
            )


def test_gate_full_corpus_hybrid_passes():
    """The shipped pipeline must pass every case (the existing hybrid gate)."""
    from upgradelens.eval.runner import run_evaluation

    result = run_evaluation(CASES_DIR)
    failures = [s.case_id for s in result.scores if s.baseline == "hybrid" and not s.passed]
    assert failures == [], f"hybrid failed cases: {failures}"
