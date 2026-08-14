"""Tests for S14: zh-CN labels for user-facing enum/status values.

The backend keeps machine-readable enum values in the JSON contracts, but the
presentation layer must expose Chinese ``*_label`` so the demo UI / markdown never
renders a raw ``str(enum)``.
"""

from __future__ import annotations

from upgradelens.domain.code_evidence import CodeEvidenceReport, CodeEvidenceSummary
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport
from upgradelens.pipeline import AssessmentOutcome
from upgradelens.presentation.i18n import (
    conclusion_label,
    evidence_status_label,
    issue_code_label,
    plan_mode_label,
    severity_label,
    trust_label,
    verdict_label,
)
from upgradelens.presentation.projector import project_assessment
from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    VerifiedReport,
    VerifiedRisk,
)


def test_i18n_zh_cn_labels():
    assert verdict_label("needs_upgrade") == "需要升级（存在已验证/已降级的破坏性变更）"
    assert verdict_label("evidence_insufficient") == "证据不足，无法给出定论"
    assert conclusion_label(Conclusion.IMPACTED) == "存在破坏性变更"
    assert severity_label("high") == "高"
    assert evidence_status_label(EvidenceStatus.VERIFIED) == "已验证"
    assert plan_mode_label("patch_draft") == "补丁草稿"
    assert trust_label("official") == "官方"
    assert issue_code_label("no_doc_evidence") == "缺少文档证据"


def test_i18n_en_fallback():
    assert verdict_label("needs_upgrade", "en") == "Upgrade required"
    assert severity_label("high", "en") == "High"
    # Unknown locales fall back to zh-CN, not to the raw enum value.
    assert verdict_label("needs_upgrade", "fr") == "需要升级（存在已验证/已降级的破坏性变更）"


def test_i18n_unknown_value_passthrough():
    assert issue_code_label("some_new_code") == "some_new_code"


def _make_outcome():
    bundle = EvidenceBundle()
    bundle.add(
        EvidenceItem(
            evidence_id="code:e1",
            kind="code_usage",
            summary="old_func used",
            detail="src/app.py uses old_func",
            meta={"path": "src/app.py", "symbol": "old_func"},
        )
    )
    bundle.add(
        EvidenceItem(
            evidence_id="doc:d1",
            kind="doc_reference",
            summary="migration guide",
            detail="guide",
            meta={
                "title": "Pydantic Migration",
                "url": "https://docs.pydantic.dev",
                "trust_level": "official",
                "source_version": "1.10",
                "target_version": "2.0",
            },
        )
    )
    risk = VerifiedRisk(
        risk_id="r1",
        title="Replace old_func with new_func",
        status=EvidenceStatus.VERIFIED,
        severity="high",
        model_severity="high",
        code_evidence_ids=["code:e1"],
        doc_evidence_ids=["doc:d1"],
        recommendation="Use new_func.",
        problem="old_func removed in 2.0",
    )
    verified = VerifiedReport(
        target_dependency="pydantic",
        source_version_spec="1.10",
        target_version_spec="2.0",
        verified_risks=[risk],
        degraded_risks=[],
        conclusion=Conclusion.IMPACTED,
    )
    report = ImpactReport(
        target_dependency="pydantic",
        source_version_spec="1.10",
        target_version_spec="2.0",
        risks=[],
    )
    return AssessmentOutcome(
        report=report,
        verified=verified,
        repo_path=".",
        skill=None,
        bundle=bundle,
        code_report=CodeEvidenceReport(
            dependency_name="pydantic",
            scanned_files=1,
            summary=CodeEvidenceSummary(scanned_files=1, usage_count=0),
        ),
        degradations=(),
    )


def test_projector_populates_labels():
    outcome = _make_outcome()
    view = project_assessment(outcome)

    assert view.verdict_label == "需要升级（存在已验证/已降级的破坏性变更）"
    assert view.conclusion_label == "存在破坏性变更"

    assert view.verified_risks
    finding = view.verified_risks[0]
    assert finding.severity_label == "高"
    assert finding.evidence_status_label == "已验证"

    doc = finding.docs[0]
    assert doc.trust_label == "官方"


def test_projector_locale_default_is_zh_cn():
    outcome = _make_outcome()
    view = project_assessment(outcome)
    assert view.verdict_label.startswith("需要") or "升级" in view.verdict_label
