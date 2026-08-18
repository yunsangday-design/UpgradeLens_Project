"""Adapters between the legacy upgrade models and the new generic task models (S2).

All functions here are *pure* model transforms: they turn the existing
:class:`~upgradelens.models.impact.ImpactReport` / ``VerifiedReport`` produced by the
upgrade pipeline into the generic :class:`~upgradelens.core.finding.Finding`,
:class:`~upgradelens.core.action.ActionProposal` and
:class:`~upgradelens.core.verification.VerificationResult` shapes. No network, no LLM
-- so they run unchanged under ``fake`` mode and are easy to unit test.
"""

from __future__ import annotations

from typing import Any

from upgradelens.core.action import ActionKind, ActionProposal
from upgradelens.core.finding import Finding, FindingStatus, Severity
from upgradelens.core.task import SoftwareTask
from upgradelens.core.verification import VerificationCheck, VerificationResult
from upgradelens.models.impact import ImpactReport
from upgradelens.pipeline import AssessmentRequest
from upgradelens.verify.models import VerifiedReport

__all__ = [
    "software_task_to_request",
    "findings_from_impact",
    "actions_from_impact",
    "verification_from_verified",
]

_SEVERITY_MAP: dict[str, Severity] = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.INFO,
}

_CONFIDENCE_MAP: dict[str, float] = {
    "low": 0.3,
    "medium": 0.6,
    "high": 0.85,
    "certain": 1.0,
}


def _severity(value: Any) -> Severity:
    return _SEVERITY_MAP.get(str(value).lower(), Severity.MEDIUM)


def _confidence(value: Any) -> float:
    return _CONFIDENCE_MAP.get(str(value).lower(), 0.5)


def software_task_to_request(task: SoftwareTask) -> AssessmentRequest:
    """Map a generic upgrade :class:`SoftwareTask` to a legacy ``AssessmentRequest``.

    Suitable for driving the existing pipeline in ``dry_run`` / ``fake`` mode without
    any real execution.
    """
    ctx = task.context
    return AssessmentRequest(
        repo=ctx.repo,
        dependency=ctx.dependency,
        target_version=ctx.target_version or None,
        source_version=ctx.source_version or None,
        user_intent=task.goal,
    )


def findings_from_impact(report: ImpactReport) -> list[Finding]:
    """One :class:`Finding` per :class:`RiskItem`, promoted to VERIFIED when cited."""
    findings: list[Finding] = []
    for idx, risk in enumerate(report.risks):
        evidence_ids = list(risk.evidence_ids)
        findings.append(
            Finding(
                finding_id=risk.risk_id or f"risk-{idx}",
                category="dependency_risk",
                severity=_severity(risk.severity),
                confidence=_confidence(risk.confidence),
                summary=risk.title,
                detail=risk.recommendation or risk.title,
                evidence_ids=evidence_ids,
                status=FindingStatus.VERIFIED if evidence_ids else FindingStatus.CANDIDATE,
            )
        )
    return findings


def actions_from_impact(report: ImpactReport) -> list[ActionProposal]:
    """Emit a manual (approval-required) remediation per risk recommendation."""
    actions: list[ActionProposal] = []
    for idx, risk in enumerate(report.risks):
        if not risk.recommendation:
            continue
        actions.append(
            ActionProposal(
                proposal_id=f"act-{risk.risk_id or idx}",
                kind=ActionKind.MANUAL,
                finding_ids=[risk.risk_id] if risk.risk_id else [],
                title=f"Mitigate: {risk.title}",
                description=risk.recommendation,
                requires_approval=True,
            )
        )
    return actions


def verification_from_verified(verified: VerifiedReport) -> VerificationResult:
    """Aggregate a ``VerifiedReport`` into the generic verification shape."""
    checks: list[VerificationCheck] = []
    for risk in verified.verified_risks:
        checks.append(
            VerificationCheck(
                name=f"verified:{risk.risk_id}",
                passed=risk.is_verified,
                detail=risk.title,
            )
        )
    for risk in verified.degraded_risks:
        checks.append(
            VerificationCheck(
                name=f"degraded:{risk.risk_id}",
                passed=False,
                detail=risk.title,
            )
        )
    return VerificationResult(
        proposal_id="dependency_upgrade",
        checks=checks,
        summary=f"conclusion={verified.conclusion.value}",
    )
