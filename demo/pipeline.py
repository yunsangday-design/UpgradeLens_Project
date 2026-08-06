"""Headless assess pipeline used by the Streamlit demo.

Kept free of any Streamlit import so the logic can be smoke-tested offline
without a browser. ``demo/app.py`` is a thin UI layer over :func:`run_assess`.

In ``fake`` mode the model gateway returns empty canned responses, which makes
the closed loop produce a boring, risk-free report. To keep the demo
illustrative while staying fully offline, :func:`_build_fake_responses` crafts
*canned but evidence-anchored* model outputs: the risks reference the **real**
code-evidence ids discovered in the target repo, plus a synthetic
``doc_citation`` so the verifier can promote them to ``VERIFIED`` and the patch
generator can fire on the actual source line. This is clearly an illustrative
fixture, not real model reasoning.
"""

from __future__ import annotations

from pathlib import Path

from upgradelens.analyzers import scan_code_evidence
from upgradelens.domain.skill import SkillPackage
from upgradelens.graph import AssessmentSpec, run_assessment
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import (
    EvidenceBundle,
    EvidenceItem,
    ImpactReport,
    RiskItem,
    build_bundle,
)
from upgradelens.patch import generate_patch_draft
from upgradelens.skills import builtin_registry
from upgradelens.verify import verify_report


def looks_like_spec(value: str) -> bool:
    return any(ch in value for ch in (">", "<", "=", "~", "*"))


def _build_fake_responses(
    bundle: EvidenceBundle, dependency: str, skill: SkillPackage | None
) -> tuple[dict[str, object], list[EvidenceItem]]:
    """Craft canned, evidence-anchored model outputs for fake mode.

    Returns the fake-response dict (keyed by graph node name) and any synthetic
    ``doc_chunk`` evidence items that must be added to the bundle so the verifier
    can promote the crafted risks to VERIFIED (the verifier only treats
    ``doc_chunk`` kind as documentation evidence).
    """
    code_items = [it for it in bundle.items if it.kind == "code_usage"]
    if not code_items:
        return {}, []

    # Anchor the synthetic doc to a *real* official skill source id so the
    # verifier trusts it and treats the risk as VERIFIED (not just partially).
    source_id = ""
    if skill is not None and skill.sources:
        for src in skill.sources:
            if getattr(src, "trust_level", "") == "official":
                source_id = src.id
                break
        else:
            source_id = skill.sources[0].id

    injected_docs: list[EvidenceItem] = []
    risks: list[RiskItem] = []

    def _doc(doc_source_id: str, title: str) -> str:
        doc_id = f"doc:{doc_source_id}:synthetic"
        injected_docs.append(
            EvidenceItem(
                evidence_id=doc_id,
                kind="doc_chunk",
                summary=title,
                detail=title,
                meta={"source_id": doc_source_id, "title": title},
            )
        )
        return doc_id

    # .dict() -> model_dump() (pydantic v2): low-risk mechanical patch.
    for it in code_items:
        if ".dict(" in (it.detail or "") or ".dict(" in (it.summary or ""):
            doc_id = _doc(source_id, "pydantic v2: Model.dict() removed, use model_dump()")
            risks.append(
                RiskItem(
                    risk_id="pyd01",
                    title="pydantic Model.dict() removed in v2",
                    severity="high",
                    confidence="high",
                    evidence_ids=[it.evidence_id, doc_id],
                    recommendation="Replace obj.dict() with obj.model_dump().",
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
        doc_id = _doc(source_id, "pydantic v2: @validator -> @field_validator")
        risks.append(
            RiskItem(
                risk_id="pyd02",
                title="pydantic @validator renamed to @field_validator in v2",
                severity="high",
                confidence="high",
                evidence_ids=[validator_item.evidence_id, doc_id],
                recommendation="Replace @validator with @field_validator (signature changed).",
            )
        )

    if not risks:
        return {}, []

    report = ImpactReport(
        target_dependency=dependency,
        risks=risks,
        notes="Fake-mode illustrative output: risks are anchored to real code evidence.",
    )
    return {"impact_analyzer": report}, injected_docs


def run_assess(
    repo: str,
    dependency: str,
    target_version: str,
    mode: str,
    model: str,
    api_key: str,
    base_url: str,
    allow_quality_patch: bool,
) -> dict[str, object]:
    """Run the full assess pipeline and return a result bundle for rendering."""
    registry = builtin_registry()
    repo_path = Path(repo)

    code_report = scan_code_evidence(repo_path, dependency)

    skill: SkillPackage | None = None
    degradations: list[str] = []
    try:
        selection = registry.select_skill(dependency, target_version)
        skill = registry.get(selection.skill_id)
    except Exception as exc:  # e.g. target_version is a range, not a concrete version
        degradations.append(f"skill 解析失败（目标版本需为具体版本号），按通用依赖处理: {exc}")
        skill = None
    if skill is None:
        degradations.append("未解析到专用 skill，按通用依赖分析（风险面更大）")

    bundle = build_bundle(code_report, dependency=dependency)
    if not bundle.items:
        degradations.append("无代码用法证据，无法评估具体影响")

    fake_responses: dict[str, object] = {}
    if mode == "fake":
        try:
            fake_responses, injected_docs = _build_fake_responses(bundle, dependency, skill)
            for doc in injected_docs:
                bundle.add(doc)
        except Exception:  # never let demo fixtures break the real pipeline
            fake_responses, injected_docs = {}, []

    target_version_spec = (
        target_version if looks_like_spec(target_version) else f"=={target_version}"
    )
    source_version_spec = getattr(code_report, "version", "") or ""
    spec = AssessmentSpec(
        repo=str(repo_path),
        dependency=dependency,
        target_version_spec=target_version_spec,
        source_version_spec=source_version_spec,
    )

    config = ModelConfig(
        mode=ModelMode(mode),
        model=model or "qwen-plus",
        api_key=api_key or "",
        base_url=base_url or "",
    )
    gateway = ModelGateway(config, fake_responses=fake_responses or None)

    report = run_assessment(spec, bundle, gateway, skill=skill)
    verified = verify_report(
        report,
        repo_root=repo_path,
        bundle=bundle,
        code_report=code_report,
        skill=skill,
        degradations=degradations,
    )

    draft = None
    if skill is not None and skill.allow_patch_draft:
        draft = generate_patch_draft(
            repo_path,
            verified.verified_risks,
            skill,
            bundle,
            quality_model_available=allow_quality_patch,
        )

    return {
        "code_report": code_report,
        "skill": skill,
        "bundle": bundle,
        "report": report,
        "verified": verified,
        "draft": draft,
    }
