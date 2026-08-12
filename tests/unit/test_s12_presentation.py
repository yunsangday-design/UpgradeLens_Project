"""S12: deterministic presentation projection tests (pure, no model/network)."""

from __future__ import annotations

from types import SimpleNamespace

from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport
from upgradelens.plan.upgrade_plan import UpgradePlan, UpgradeStep
from upgradelens.presentation.models import UpgradeAssessmentView
from upgradelens.presentation.projector import project_assessment
from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    VerifiedReport,
    VerifiedRisk,
)


def _code_item(evidence_id: str, *, symbol: str = "foo") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        kind="code_usage",
        summary="usage",
        detail="",
        meta={
            "path": "src/app.py",
            "start_line": 10,
            "end_line": 12,
            "column": 4,
            "symbol": symbol,
            "snippet": "x = foo()",
            "is_test_code": False,
            "confidence": "high",
        },
    )


def _doc_item(evidence_id: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        kind="doc_chunk",
        summary="doc",
        detail="",
        meta={
            "title": "Migration Guide",
            "url": "https://docs.example/migration",
            "heading_path": ["Guide", "Section"],
            "chunk_title": "Section",
            "snapshot_hash": "abc123",
            "snippet": "do this instead",
            "score": 0.91,
            "matched_query": "validator",
            "source_id": "src1",
            "trust_level": "official",
            "source_version": "1.10",
            "target_version": "2.0",
        },
    )


def _risk(risk_id: str, *, code_ids, doc_ids) -> VerifiedRisk:
    return VerifiedRisk(
        risk_id=risk_id,
        title=f"risk {risk_id}",
        severity="high",
        model_severity="high",
        status=EvidenceStatus.VERIFIED,
        rule_score=3,
        recommendation="migrate it",
        problem="uses deprecated API",
        behavior_change="raises TypeError at runtime",
        migration_steps=["replace with new API"],
        verification_steps=["run pytest"],
        before_example="x = old()",
        after_example="x = new()",
        code_evidence_ids=list(code_ids),
        doc_evidence_ids=list(doc_ids),
    )


def _outcome(*, verified, bundle, degradations=()):
    return SimpleNamespace(
        verified=verified,
        bundle=bundle,
        degradations=tuple(degradations),
        report=ImpactReport(
            target_dependency="pydantic",
            source_version_spec="1.10",
            target_version_spec="2.0",
        ),
    )


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(items=[_code_item("code1"), _doc_item("doc1")])


def test_project_resolves_code_and_doc_views():
    risk = _risk("r1", code_ids=["code1"], doc_ids=["doc1"])
    verified = VerifiedReport(
        conclusion=Conclusion.IMPACTED,
        verified_risks=[risk],
    )
    outcome = _outcome(verified=verified, bundle=_bundle())

    view = project_assessment(outcome)

    assert isinstance(view, UpgradeAssessmentView)
    assert view.verdict == "needs_upgrade"
    assert view.is_evidence_insufficient is False
    assert view.target_dependency == "pydantic"
    assert view.source_version_spec == "1.10"
    assert view.target_version_spec == "2.0"

    assert len(view.verified_risks) == 1
    finding = view.verified_risks[0]
    assert finding.risk_id == "r1"
    assert finding.status == "verified"
    assert finding.evidence_status == "verified"

    assert len(finding.code) == 1
    code = finding.code[0]
    assert code.evidence_id == "code1"
    assert code.path == "src/app.py"
    assert code.start_line == 10
    assert code.end_line == 12
    assert code.symbol == "foo"
    assert code.snippet == "x = foo()"
    assert code.is_test_code is False

    assert len(finding.docs) == 1
    doc = finding.docs[0]
    assert doc.evidence_id == "doc1"
    assert doc.title == "Migration Guide"
    assert doc.trust_level == "official"
    assert doc.source_version_spec == "1.10"
    assert doc.target_version_spec == "2.0"

    # RAG resolution mirrors the doc items.
    assert len(finding.rag) == 1
    assert finding.rag[0].source_id == "src1"
    assert finding.rag[0].snapshot_hash == "abc123"
    assert finding.rag[0].version_range == "1.10->2.0"

    # Semantic migration detail is flattened through.
    assert finding.migration.problem == "uses deprecated API"
    assert finding.migration.behavior_change == "raises TypeError at runtime"
    assert finding.migration.steps == ["replace with new API"]
    assert finding.migration.verification_steps == ["run pytest"]
    assert finding.migration.before_example == "x = old()"
    assert finding.migration.after_example == "x = new()"


def test_verdict_separation():
    bundle = _bundle()

    # evidence insufficient: conclusion is EVIDENCE_INSUFFICIENT (strictly
    # distinct from "no risk").
    ev_ins = _outcome(
        verified=VerifiedReport(conclusion=Conclusion.EVIDENCE_INSUFFICIENT),
        bundle=bundle,
        degradations=["No documentation index was provided (--db)"],
    )
    v = project_assessment(ev_ins)
    assert v.verdict == "evidence_insufficient"
    assert v.is_evidence_insufficient is True
    assert v.is_partial is True
    assert "No documentation index" in v.degradations[0]

    # no risk: the dependency IS used (code evidence present) but no breaking
    # change applies -> strictly "no_risk", not "evidence insufficient".
    no_risk = _outcome(verified=VerifiedReport(conclusion=Conclusion.NO_IMPACT), bundle=_bundle())
    assert project_assessment(no_risk).verdict == "no_risk"

    # no impact: the dependency is NOT used at all (no code evidence) -> "no_impact".
    no_code_bundle = EvidenceBundle(items=[_doc_item("doc_x")])
    no_impact = _outcome(
        verified=VerifiedReport(conclusion=Conclusion.NO_IMPACT), bundle=no_code_bundle
    )
    assert project_assessment(no_impact).verdict == "no_impact"
    assert project_assessment(no_impact).no_impact is True


def test_degraded_risks_surface_separately():
    risk = _risk("r2", code_ids=["code1"], doc_ids=["doc1"])
    verified = VerifiedReport(
        conclusion=Conclusion.IMPACTED,
        degraded_risks=[risk],
    )
    outcome = _outcome(verified=verified, bundle=_bundle())
    view = project_assessment(outcome)
    assert view.verdict == "needs_upgrade"
    assert len(view.verified_risks) == 0
    assert len(view.degraded_risks) == 1
    assert view.degraded_risks[0].risk_id == "r2"


def test_upgrade_plan_association_by_file():
    risk = _risk("r1", code_ids=["code1"], doc_ids=["doc1"])
    verified = VerifiedReport(conclusion=Conclusion.IMPACTED, verified_risks=[risk])
    bundle = _bundle()
    plan = UpgradePlan(
        target_dependency="pydantic",
        source_version_spec="1.10",
        target_version_spec="2.0",
        deploy_contract=True,
        steps=[
            UpgradeStep(
                step_id="s1",
                title="replace foo",
                severity="high",
                evidence_status="verified",
                target_files=["src/app.py"],
                api_symbols=["foo"],
            )
        ],
    )
    outcome = _outcome(verified=verified, bundle=bundle)
    view = project_assessment(outcome, upgrade_plan=plan)

    assert view.upgrade_plan is not None
    assert view.upgrade_plan.deploy_contract is True
    assert view.upgrade_plan.step_count == 1
    assert view.upgrade_plan.steps[0].step_id == "s1"
    # The finding's code path (src/app.py) matches the plan step target file.
    assert view.verified_risks[0].plan_step is not None
    assert view.verified_risks[0].plan_step.step_id == "s1"


def test_missing_evidence_ids_are_skipped():
    # Risk references evidence IDs absent from the bundle.
    risk = _risk("r1", code_ids=["missing_code"], doc_ids=["missing_doc"])
    verified = VerifiedReport(conclusion=Conclusion.IMPACTED, verified_risks=[risk])
    outcome = _outcome(verified=verified, bundle=_bundle())
    view = project_assessment(outcome)
    finding = view.verified_risks[0]
    assert finding.code == []
    assert finding.docs == []
    assert finding.rag == []
