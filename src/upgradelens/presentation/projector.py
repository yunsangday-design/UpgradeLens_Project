"""S12: deterministic projection from assessment outcome to presentation view.

This is the **only** place that joins a ``VerifiedRisk`` to its underlying
``EvidenceBundle`` and (optionally) the ``UpgradePlan``. Callers -- the demo UI,
markdown/mcp reports, the run-store artifact -- must consume the flattened
:class:`UpgradeAssessmentView` and must **never** re-join evidence IDs
themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from upgradelens.models.impact import EvidenceBundle, EvidenceItem
from upgradelens.pipeline import AssessmentOutcome
from upgradelens.plan.upgrade_plan import UpgradePlan
from upgradelens.verify.models import Conclusion, VerifiedReport, VerifiedRisk

from .i18n import (
    conclusion_label,
    evidence_status_label,
    severity_label,
    trust_label,
    verdict_label,
)
from .models import (
    CodeLocationView,
    DocumentReferenceView,
    MigrationAdviceView,
    RagResolutionView,
    UpgradeAssessmentView,
    UpgradeFindingView,
    UpgradePlanRef,
    UpgradePlanStepRef,
)


def _evidence(bundle: EvidenceBundle, evidence_id: str) -> EvidenceItem | None:
    return bundle.get(evidence_id)


def _code_view(item: EvidenceItem) -> CodeLocationView:
    m = item.meta
    return CodeLocationView(
        evidence_id=item.evidence_id,
        path=str(m.get("path", "")),
        start_line=int(m.get("start_line") or 0),
        end_line=int(m.get("end_line") or 0),
        column=int(m.get("column") or 0),
        symbol=str(m.get("symbol", "")),
        snippet=str(m.get("snippet", "")),
        is_test_code=bool(m.get("is_test_code", False)),
        confidence=str(m.get("confidence", "high")),
    )


def _doc_view(item: EvidenceItem, locale: str = "zh-CN") -> DocumentReferenceView:
    m = item.meta
    trust = str(m.get("trust_level", ""))
    return DocumentReferenceView(
        evidence_id=item.evidence_id,
        title=str(m.get("title", "")),
        url=str(m.get("url", "")),
        heading_path=list(m.get("heading_path") or []),
        snippet=str(m.get("snippet", "")),
        trust_level=trust,
        trust_label=trust_label(trust, locale),
        source_version_spec=str(m.get("source_version", "")),
        target_version_spec=str(m.get("target_version", "")),
    )


def _rag_view(item: EvidenceItem, locale: str = "zh-CN") -> RagResolutionView:
    m = item.meta
    src = str(m.get("source_version", ""))
    tgt = str(m.get("target_version", ""))
    trust = str(m.get("trust_level", ""))
    return RagResolutionView(
        evidence_id=item.evidence_id,
        source_id=str(m.get("source_id", "")),
        url=str(m.get("url", "")),
        title=str(m.get("title", "")),
        heading_path=list(m.get("heading_path") or []),
        chunk_title=str(m.get("chunk_title", "")),
        snapshot_hash=str(m.get("snapshot_hash", "")),
        score=float(m.get("score") or 0.0),
        matched_query=str(m.get("matched_query", "")),
        trust_level=trust,
        trust_label=trust_label(trust, locale),
        version_range=f"{src}->{tgt}" if (src or tgt) else "",
    )


def _status_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _plan_step_refs(plan: UpgradePlan, locale: str = "zh-CN") -> list[UpgradePlanStepRef]:
    refs: list[UpgradePlanStepRef] = []
    for step in plan.steps:
        refs.append(
            UpgradePlanStepRef(
                step_id=step.step_id,
                title=step.title,
                severity=_status_value(step.severity),
                status="",  # UpgradeStep carries no per-step status field
                target_files=list(step.target_files),
                api_symbols=list(step.api_symbols),
                evidence_status=_status_value(step.evidence_status),
                severity_label=severity_label(step.severity, locale),
                evidence_status_label=evidence_status_label(step.evidence_status, locale),
            )
        )
    return refs


def _plan_ref(plan: UpgradePlan, steps: list[UpgradePlanStepRef]) -> UpgradePlanRef:
    return UpgradePlanRef(
        schema_version=plan.schema_version,
        target_dependency=plan.target_dependency,
        source_version_spec=plan.source_version_spec,
        target_version_spec=plan.target_version_spec,
        repo_hash=plan.repo_hash,
        mode=_status_value(plan.mode),
        deploy_contract=bool(plan.deploy_contract),
        step_count=len(plan.steps),
        steps=steps,
    )


def _collect_items(
    bundle: EvidenceBundle, evidence_ids: list[str]
) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    for eid in evidence_ids:
        item = _evidence(bundle, eid)
        if item is not None:
            items.append(item)
    return items


def _finding(
    risk: VerifiedRisk,
    bundle: EvidenceBundle,
    plan_steps: list[UpgradePlanStepRef],
    locale: str = "zh-CN",
) -> UpgradeFindingView:
    code_items = _collect_items(bundle, risk.code_evidence_ids)
    doc_items = _collect_items(bundle, risk.doc_evidence_ids)
    code_paths = {str(c.meta.get("path", "")) for c in code_items}

    plan_step: UpgradePlanStepRef | None = None
    if plan_steps and code_paths:
        for ps in plan_steps:
            if set(ps.target_files) & code_paths:
                plan_step = ps
                break

    return UpgradeFindingView(
        risk_id=risk.risk_id,
        title=risk.title,
        severity=risk.severity,
        model_severity=risk.model_severity,
        status=_status_value(risk.status),
        evidence_status=_status_value(risk.status),
        severity_label=severity_label(risk.severity, locale),
        evidence_status_label=evidence_status_label(risk.status, locale),
        rule_score=risk.rule_score,
        recommendation=risk.recommendation,
        code=[_code_view(c) for c in code_items],
        docs=[_doc_view(d, locale) for d in doc_items],
        rag=[_rag_view(d, locale) for d in doc_items],
        migration=MigrationAdviceView(
            problem=risk.problem,
            behavior_change=risk.behavior_change,
            steps=list(risk.migration_steps),
            verification_steps=list(risk.verification_steps),
            before_example=risk.before_example,
            after_example=risk.after_example,
        ),
        plan_step=plan_step,
    )


def project_assessment(
    outcome: AssessmentOutcome,
    *,
    upgrade_plan: UpgradePlan | None = None,
    now: datetime | None = None,
    locale: str = "zh-CN",
) -> UpgradeAssessmentView:
    """Project an assessment outcome into a self-contained presentation view.

    Deterministic and side-effect free: the same outcome + plan always yields
    the same view. All evidence resolution happens here.
    """
    verified: VerifiedReport = outcome.verified or VerifiedReport()
    bundle: EvidenceBundle = outcome.bundle or EvidenceBundle()
    report = outcome.report

    # S12: "no risk" must be strictly separated from "evidence insufficient".
    is_ev = verified.conclusion == Conclusion.EVIDENCE_INSUFFICIENT
    verified_risks = list(verified.verified_risks)
    degraded_risks = list(verified.degraded_risks)
    has_code = any(it.kind == "code_usage" for it in bundle.items)
    verdict = UpgradeAssessmentView._verdict_for(
        verified.conclusion, is_ev, bool(verified_risks), bool(degraded_risks), has_code
    )

    plan_ref: UpgradePlanRef | None = None
    plan_steps: list[UpgradePlanStepRef] = []
    if upgrade_plan is not None:
        plan_steps = _plan_step_refs(upgrade_plan, locale)
        plan_ref = _plan_ref(upgrade_plan, plan_steps)

    gen_at = (now or datetime.now(UTC)).isoformat()

    return UpgradeAssessmentView(
        generated_at=gen_at,
        target_dependency=report.target_dependency if report else "",
        source_version_spec=report.source_version_spec if report else "",
        target_version_spec=report.target_version_spec if report else "",
        static=bool(verified.static),
        verdict=verdict,
        verdict_label=verdict_label(verdict, locale),
        conclusion_label=conclusion_label(verified.conclusion, locale),
        is_evidence_insufficient=is_ev,
        no_impact=verdict == "no_impact",
        is_partial=bool(verified.partial) or bool(outcome.degradations),
        degradations=list(outcome.degradations),
        verified_risks=[_finding(r, bundle, plan_steps, locale) for r in verified_risks],
        degraded_risks=[_finding(r, bundle, plan_steps, locale) for r in degraded_risks],
        recommended_tests=[t.model_dump(mode="json") for t in verified.recommended_tests],
        citation_existence_rate=verified.citation_existence_rate,
        upgrade_plan=plan_ref,
        notes=verified.notes or "",
    )
