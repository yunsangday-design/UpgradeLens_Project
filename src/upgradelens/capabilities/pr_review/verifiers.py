"""PR review verifier (plan stage S4).

A *verified* finding must cite changed code via a ``code:<path>:<line>``
evidence reference whose path actually appears in the change set. This is a
deterministic, offline gate: it trusts the review node's classification only
when the cited location is real, which prevents fabricated findings from being
escalated to approved actions.
"""

from __future__ import annotations

from upgradelens.change.models import ChangeSet
from upgradelens.core.finding import Finding, FindingStatus
from upgradelens.core.verification import VerificationCheck, VerificationResult

__all__ = ["pr_review_verifier"]

_PREFIX = "code:"


def _cites_changed_code(evidence_ids: list[str], changed_paths: set[str]) -> bool:
    for eid in evidence_ids:
        if eid.startswith(_PREFIX):
            path = eid[len(_PREFIX) :].rsplit(":", 1)[0]
            if path in changed_paths:
                return True
    return False


def pr_review_verifier(findings: list[Finding], change_set: ChangeSet) -> VerificationResult:
    """Verify that every verified finding cites real changed code."""
    changed_paths = {c.path for c in change_set.files}
    checks: list[VerificationCheck] = []
    verified = [f for f in findings if f.status == FindingStatus.VERIFIED]
    for finding in verified:
        citable = _cites_changed_code(finding.evidence_ids, changed_paths)
        checks.append(
            VerificationCheck(
                name=f"evidence:{finding.finding_id}",
                passed=citable,
                detail=(
                    "cites changed code"
                    if citable
                    else "verified finding does not cite changed code"
                ),
                evidence_id=finding.evidence_ids[0] if finding.evidence_ids else None,
            )
        )
    passed = bool(checks) and all(c.passed for c in checks)
    summary = (
        "all verified findings cite changed code"
        if passed
        else "some verified findings lack cited changed code"
    )
    return VerificationResult(
        proposal_id="pr_review",
        checks=checks,
        passed=passed,
        summary=summary,
        evidence_ids=[c.evidence_id for c in checks if c.evidence_id],
    )
