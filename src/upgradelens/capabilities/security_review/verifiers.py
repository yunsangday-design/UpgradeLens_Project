"""Security review verifier (plan stage S7).

A *verified* finding must cite changed code via a ``code:<path>:<line>`` evidence
reference whose path actually appears in the change set. Additionally, any
high/critical finding must be either an explicit false-positive exemption or a
verified finding that cites changed code -- this is the security gate that blocks
high-severity issues from being escalated to approved actions.
"""

from __future__ import annotations

from upgradelens.change.models import ChangeSet
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.verification import VerificationCheck, VerificationResult

__all__ = ["security_review_verifier"]

_PREFIX = "code:"


def _cites_changed_code(evidence_ids: list[str], changed_paths: set[str]) -> bool:
    for eid in evidence_ids:
        if eid.startswith(_PREFIX):
            path = eid[len(_PREFIX) :].rsplit(":", 1)[0]
            if path in changed_paths:
                return True
    return False


def security_review_verifier(
    findings: list[Finding], change_set: ChangeSet
) -> VerificationResult:
    """Verify evidence and enforce the high/critical security gate."""
    changed_paths = {c.path for c in change_set.files}
    checks: list[VerificationCheck] = []

    for finding in findings:
        if finding.status == FindingStatus.VERIFIED:
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
                    evidence_id=(
                        finding.evidence_ids[0] if finding.evidence_ids else None
                    ),
                )
            )

    for finding in findings:
        if finding.severity not in (Severity.CRITICAL, Severity.HIGH):
            continue
        # Rejected findings are false-positive exemptions and never block the gate.
        if finding.status == FindingStatus.REJECTED:
            continue
        citable = _cites_changed_code(finding.evidence_ids, changed_paths)
        ok = finding.status == FindingStatus.VERIFIED and citable
        checks.append(
            VerificationCheck(
                name=f"gate:{finding.finding_id}",
                passed=ok,
                detail=(
                    "high/critical finding verified against changed code"
                    if ok
                    else "high/critical finding not verified against changed code"
                ),
                evidence_id=(
                    finding.evidence_ids[0] if finding.evidence_ids else None
                ),
            )
        )

    if not checks:
        checks.append(
            VerificationCheck(
                name="gate:no-blocking-findings",
                passed=True,
                detail="no high/critical or verified findings require gating",
            )
        )
    passed = bool(checks) and all(c.passed for c in checks)
    summary = (
        "all verified findings cite changed code and the security gate holds"
        if passed
        else "security gate blocked: findings lack cited/verified evidence"
    )
    return VerificationResult(
        proposal_id="security_review",
        checks=checks,
        passed=passed,
        summary=summary,
        evidence_ids=[c.evidence_id for c in checks if c.evidence_id],
    )
