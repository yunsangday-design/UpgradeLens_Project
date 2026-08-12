"""Offline, evidence-anchored canned model responses.

These are *illustrative fixtures*, not real model reasoning. They anchor a
handful of risks to the **real** code-usage evidence ids discovered in the
target repo so the closed loop (``seed-replay`` -> ``assess --mode replay``) can
run fully offline and the verifier can still see a cited code location.

Design rule: a canned risk must cite only evidence that exists in *any* run on
that repo. We therefore reference the real ``code_usage`` ids and **never** a
synthetic documentation chunk -- a synthetic doc id would be "unknown" in the
replay-time bundle and the verifier would (correctly) drop the risk. Without a
real documentation index (``--db``) the risks legitimately degrade to
``partially_verified``; that is the anti-hallucination gate working as designed.

A real capture is made with ``assess --mode live --record-replay <dir>``; the
fixtures here are only a placeholder for that.

Lifted out of ``demo/pipeline.py`` so the demo and the CLI share one
implementation instead of duplicating the canned-output logic.
"""

from __future__ import annotations

from pydantic import BaseModel

from upgradelens.domain.skill import SkillPackage
from upgradelens.models.impact import EvidenceBundle, ImpactReport, RiskItem


def build_fake_responses(
    bundle: EvidenceBundle,
    dependency: str,
    skill: SkillPackage | None,
) -> tuple[dict[str, BaseModel], list[object]]:
    """Craft canned, evidence-anchored model outputs for fake/replay seeding.

    Returns the fake-response dict (keyed by graph node name) and an empty list
    (kept for call-site compatibility; no synthetic evidence is injected). Every
    risk cites only real ``code_usage`` evidence ids so it survives replay.
    """
    code_items = [it for it in bundle.items if it.kind == "code_usage"]
    if not code_items:
        return {}, []

    risks: list[RiskItem] = []

    # Pydantic-specific, nicely worded risks (the shipped default demo target).
    for it in code_items:
        if ".dict(" in (it.detail or "") or ".dict(" in (it.summary or ""):
            risks.append(
                RiskItem(
                    risk_id="pyd01",
                    title="pydantic 的 Model.dict() 在 v2 中已移除",
                    severity="high",
                    confidence="high",
                    evidence_ids=[it.evidence_id],
                    recommendation="将 obj.dict() 替换为 obj.model_dump()。",
                )
            )
            break

    # @validator / @root_validator renamed (pydantic v2): high-risk, needs review.
    # Prefer the actual decorator line (`@validator(`) over the import line.
    validator_item = next(
        (
            it
            for it in code_items
            if "@validator" in (it.detail or "") or "@root_validator" in (it.detail or "")
        ),
        None,
    )
    if validator_item is None:
        validator_item = next(
            (
                it
                for it in code_items
                if str(it.meta.get("symbol", "")) in {"validator", "root_validator"}
            ),
            None,
        )
    if validator_item is not None:
        risks.append(
            RiskItem(
                risk_id="pyd02",
                title="pydantic 的 @validator 在 v2 中改名为 @field_validator",
                severity="high",
                confidence="high",
                evidence_ids=[validator_item.evidence_id],
                recommendation="将 @validator 替换为 @field_validator（签名已变更）。",
            )
        )

    # Generic fallback: for non-pydantic dependencies (e.g. sqlalchemy) still
    # surface evidence-anchored risks (and any mechanical patch the skill
    # supports) instead of an empty report. Each risk points at a *real*
    # code-usage location, so the patch generator fires only where a rule's
    # regex actually matches the line.
    if not risks:
        for it in code_items:
            symbol = str(it.meta.get("symbol", "")) or it.summary or "usage"
            risks.append(
                RiskItem(
                    risk_id=f"fake:{it.evidence_id}",
                    title=f"{dependency}：{symbol} 用法",
                    severity="medium",
                    confidence="high",
                    evidence_ids=[it.evidence_id],
                    recommendation=(
                        f"审查该 {dependency} 用法以适配目标升级版本；参见官方迁移指南。"
                    ),
                )
            )

    if not risks:
        return {}, []

    report = ImpactReport(
        target_dependency=dependency,
        risks=risks,
        notes="Fake 模式示意输出：风险锚定在真实代码证据上。",
    )
    return {"impact_analyzer": report}, []
