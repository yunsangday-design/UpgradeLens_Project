"""Unit tests for the shared VERIFIED-without-evidence downgrade helper."""

from __future__ import annotations

from upgradelens.core.finding import (
    Finding,
    FindingStatus,
    Severity,
    resolve_finding_status,
)


def test_verified_without_evidence_degrades_to_candidate() -> None:
    assert resolve_finding_status(FindingStatus.VERIFIED, []) is FindingStatus.CANDIDATE
    assert resolve_finding_status(FindingStatus.VERIFIED, ()) is FindingStatus.CANDIDATE


def test_verified_with_evidence_stays_verified() -> None:
    assert resolve_finding_status(FindingStatus.VERIFIED, ["code:a.py"]) is FindingStatus.VERIFIED


def test_non_verified_statuses_pass_through() -> None:
    for status in (FindingStatus.CANDIDATE, FindingStatus.DEGRADED, FindingStatus.REJECTED):
        assert resolve_finding_status(status, []) is status


def test_finding_constructible_after_downgrade() -> None:
    # The scenario live models produce: VERIFIED, no evidence. The helper must
    # make the Finding valid instead of tripping the model validator.
    finding = Finding(
        finding_id="f:1",
        category="security:secret",
        severity=Severity.HIGH,
        confidence=0.8,
        summary="hardcoded secret",
        detail="token in source",
        status=resolve_finding_status(FindingStatus.VERIFIED, []),
        evidence_ids=[],
    )
    assert finding.status is FindingStatus.CANDIDATE
