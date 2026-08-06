"""The three comparison baselines (plan section 18.3).

- ``static_only``  — deterministic rules, no model, then verification.
- ``llm_only``     — a model report accepted at face value, *no verification*.
- ``hybrid``       — the same model report, put through the verifier.

``llm_only`` exists to quantify what verification is worth: it is the same
input as ``hybrid``, so any difference in the scores is attributable to the
verifier alone.

Everything here runs offline. The "model" output is a synthetic document
shipped with the case, which keeps the evaluation reproducible bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from upgradelens.analyzers import scan_code_evidence
from upgradelens.docs import ingest_skill
from upgradelens.domain.code_evidence import CodeEvidenceReport
from upgradelens.domain.skill import SkillPackage
from upgradelens.eval.cases import EvalCase, resolve_placeholders
from upgradelens.graph import retrieve_skill_evidence
from upgradelens.models.impact import (
    EvidenceBundle,
    ImpactReport,
    RiskItem,
    build_bundle,
    build_static_report,
)
from upgradelens.skills import builtin_registry
from upgradelens.verify import verify_report
from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    VerifiedReport,
    VerifiedRisk,
)

__all__ = ["BASELINES", "CaseArtifacts", "build_artifacts", "run_baseline"]


@dataclass
class CaseArtifacts:
    """Evidence gathered once per case and shared by all baselines."""

    case: EvalCase
    code_report: CodeEvidenceReport
    bundle: EvidenceBundle
    skill: SkillPackage | None
    degradations: list[str]


def build_artifacts(case: EvalCase, session: Session | None) -> CaseArtifacts:
    """Scan the case repository and assemble its evidence bundle."""
    skill = builtin_registry().get(case.skill_id) if case.skill_id else None
    code_report = scan_code_evidence(case.repo, case.dependency)

    degradations: list[str] = []
    doc_runs = []
    if case.with_docs and session is not None and skill is not None:
        ingest_skill(session, skill)
        doc_runs = retrieve_skill_evidence(session, skill)
    else:
        degradations.append(
            "No documentation index was provided; "
            "risks cannot reach 'verified' without doc evidence."
        )

    bundle = build_bundle(code_report, doc_runs, dependency=case.dependency)
    return CaseArtifacts(
        case=case,
        code_report=code_report,
        bundle=bundle,
        skill=skill,
        degradations=degradations,
    )


def _attach_docs(report: ImpactReport, bundle: EvidenceBundle) -> ImpactReport:
    """Link each risk to documentation that actually mentions its symbol.

    This is plain keyword association, not inference: a doc chunk is attached
    only when the symbol named by the risk's own code evidence literally
    appears in it. That keeps the static baseline honest while still letting it
    reach ``verified`` when the documentation really does cover the API.
    """
    docs = bundle.by_kind("doc_chunk")
    if not docs:
        return report

    updated: list[RiskItem] = []
    for risk in report.risks:
        symbols = {
            str(bundle.get(eid).meta.get("symbol", ""))  # type: ignore[union-attr]
            for eid in risk.evidence_ids
            if bundle.get(eid) is not None
        }
        symbols.discard("")
        if not symbols:
            updated.append(risk)
            continue

        matched = [
            doc.evidence_id
            for doc in docs
            if any(sym.lower() in f"{doc.summary} {doc.detail}".lower() for sym in symbols)
        ]
        if matched:
            updated.append(risk.model_copy(update={"evidence_ids": [*risk.evidence_ids, *matched]}))
        else:
            updated.append(risk)
    return report.model_copy(update={"risks": updated})


def _static_report(art: CaseArtifacts) -> ImpactReport:
    report = build_static_report(
        art.bundle,
        art.skill,
        dependency=art.case.dependency,
        source_version_spec=art.case.source_version,
        target_version_spec=art.case.target_version,
        notes="Deterministic static baseline (no model).",
    )
    return _attach_docs(report, art.bundle)


def _model_report(art: CaseArtifacts) -> ImpactReport:
    """The synthetic model output for a case, or a static stand-in.

    When a case ships no ``model_report.json`` there is nothing model-specific
    to evaluate, so both model baselines reuse the static findings. That is the
    honest representation: without a real model, the LLM baseline has no extra
    information.
    """
    raw = art.case.load_model_report()
    if raw is None:
        return _static_report(art)

    risks = [
        RiskItem(
            risk_id=item["risk_id"],
            title=item["title"],
            severity=item.get("severity", "low"),
            confidence=item.get("confidence", "low"),
            evidence_ids=resolve_placeholders(list(item.get("evidence_ids") or []), art.bundle),
            recommendation=item.get("recommendation", ""),
        )
        for item in raw.get("risks") or []
    ]
    return ImpactReport(
        target_dependency=raw.get("target_dependency") or art.case.dependency,
        source_version_spec=raw.get("source_version_spec") or art.case.source_version,
        target_version_spec=raw.get("target_version_spec") or art.case.target_version,
        risks=risks,
        evidence_summary=_summarise(art.bundle),
        static=bool(raw.get("static", False)),
        notes=raw.get("notes", ""),
    )


def _summarise(bundle: EvidenceBundle) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in bundle.items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return counts


def _accept_without_verification(report: ImpactReport, art: CaseArtifacts) -> VerifiedReport:
    """Wrap a model report as-is, trusting every claim.

    This deliberately performs no checks: it models a system that ships raw LLM
    output. Severity and status come straight from the model.
    """
    risks = [
        VerifiedRisk(
            risk_id=risk.risk_id,
            title=risk.title,
            status=EvidenceStatus.VERIFIED,
            severity=str(risk.severity),
            model_severity=str(risk.severity),
            code_evidence_ids=[
                eid
                for eid in risk.evidence_ids
                if (item := art.bundle.get(eid)) is None or item.kind != "doc_chunk"
            ],
            doc_evidence_ids=[
                eid
                for eid in risk.evidence_ids
                if (item := art.bundle.get(eid)) is not None and item.kind == "doc_chunk"
            ],
            recommendation=risk.recommendation,
        )
        for risk in report.risks
    ]
    conclusion = Conclusion.IMPACTED if risks else Conclusion.NO_IMPACT
    return VerifiedReport(
        target_dependency=report.target_dependency,
        source_version_spec=report.source_version_spec,
        target_version_spec=report.target_version_spec,
        conclusion=conclusion,
        verified_risks=risks,
        degraded_risks=[],
        recommended_tests=[],
        evidence_summary=_summarise(art.bundle),
        partial=False,
        static=report.static,
        notes=report.notes,
    )


def _verify(report: ImpactReport, art: CaseArtifacts) -> VerifiedReport:
    return verify_report(
        report,
        repo_root=art.case.repo,
        bundle=art.bundle,
        code_report=art.code_report,
        skill=art.skill,
        degradations=list(art.degradations),
    )


def _run_static_only(art: CaseArtifacts) -> VerifiedReport:
    return _verify(_static_report(art), art)


def _run_llm_only(art: CaseArtifacts) -> VerifiedReport:
    return _accept_without_verification(_model_report(art), art)


def _run_hybrid(art: CaseArtifacts) -> VerifiedReport:
    return _verify(_model_report(art), art)


BASELINES = {
    "static_only": _run_static_only,
    "llm_only": _run_llm_only,
    "hybrid": _run_hybrid,
}


def run_baseline(name: str, art: CaseArtifacts) -> VerifiedReport:
    """Run one named baseline against prepared case artifacts."""
    try:
        fn = BASELINES[name]
    except KeyError:
        raise ValueError(f"unknown baseline '{name}' (known: {sorted(BASELINES)})") from None
    return fn(art)
