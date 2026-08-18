"""Breaking-change verifier (plan stage S5).

Mirrors the PR-review gate: a *verified* breaking change must cite real changed
code via a ``code:<path>:<line>`` evidence reference. This keeps the model node
honest -- it cannot escalate a fabricated break into an approved remediation.
"""

from __future__ import annotations

from upgradelens.change.models import ChangeSet
from upgradelens.core.finding import Finding, FindingStatus
from upgradelens.core.verification import VerificationCheck, VerificationResult

__all__ = ["verify_breaking_changes"]

_PREFIX = "code:"


def _cites_changed_code(evidence_ids: list[str], changed_paths: set[str]) -> bool:
    for eid in evidence_ids:
        if eid.startswith(_PREFIX):
            path = eid[len(_PREFIX) :].rsplit(":", 1)[0]
            if path in changed_paths:
                return True
    return False


def verify_breaking_changes(findings: list[Finding], change_set: ChangeSet) -> VerificationResult:
    """Verify that every verified breaking change cites real changed code."""
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
                    else "verified breaking change does not cite changed code"
                ),
                evidence_id=finding.evidence_ids[0] if finding.evidence_ids else None,
            )
        )
    passed = bool(checks) and all(c.passed for c in checks)
    summary = (
        "all verified breaking changes cite changed code"
        if passed
        else "some verified breaking changes lack cited changed code"
    )
    return VerificationResult(
        proposal_id="breaking_change",
        checks=checks,
        passed=passed,
        summary=summary,
        evidence_ids=[c.evidence_id for c in checks if c.evidence_id],
    )
