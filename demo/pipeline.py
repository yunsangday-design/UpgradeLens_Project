"""Headless assess pipeline used by the Streamlit demo.

Kept free of any Streamlit import so the logic can be smoke-tested offline
without a browser. ``demo/app.py`` is a thin UI layer over :func:`run_assess`.

The analysis itself is :mod:`upgradelens.pipeline`, exactly as the CLI and the
MCP server run it -- what lives here is only the part that is genuinely the
demo's own. In ``fake`` mode the model gateway returns empty canned responses,
which makes the closed loop produce a boring, risk-free report. To keep the
demo illustrative while staying fully offline, :func:`_build_fake_responses`
crafts *canned but evidence-anchored* model outputs: the risks reference the
**real** code-evidence ids discovered in the target repo, plus a synthetic
``doc_chunk`` so the verifier can promote them to ``VERIFIED`` and the patch
generator can fire on the actual source line. This is clearly an illustrative
fixture, not real model reasoning.
"""

from __future__ import annotations

from upgradelens.domain.skill import SkillPackage
from upgradelens.llm.gateway import ModelConfig, ModelGateway, ModelMode
from upgradelens.models.impact import EvidenceBundle, EvidenceItem, ImpactReport, RiskItem
from upgradelens.patch import generate_patch_draft
from upgradelens.pipeline import (
    AssessmentRequest,
    EvidenceCollection,
    analyse,
    collect_evidence,
)
from upgradelens.tools.registry import ToolContext


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

    doc_id = f"doc:{source_id}:synthetic" if source_id else "doc:synthetic"
    doc_title = f"{dependency} official migration guide (synthetic)"
    injected_docs.append(
        EvidenceItem(
            evidence_id=doc_id,
            kind="doc_chunk",
            summary=f"{dependency} upgrade (illustrative doc citation)",
            detail=f"{dependency} upgrade (illustrative doc citation)",
            meta={"source_id": source_id, "title": doc_title},
        )
    )

    # Pydantic-specific, nicely worded risks (the shipped default demo target).
    for it in code_items:
        if ".dict(" in (it.detail or "") or ".dict(" in (it.summary or ""):
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

    # Generic fallback: for non-pydantic dependencies (e.g. sqlalchemy) still
    # surface evidence-anchored VERIFIED risks (and any mechanical patch the
    # skill supports) instead of an empty report. Each risk points at a *real*
    # code-usage location, so the patch generator fires only where a rule's
    # regex actually matches the line.
    if not risks:
        for it in code_items:
            symbol = str(it.meta.get("symbol", "")) or it.summary or "usage"
            risks.append(
                RiskItem(
                    risk_id=f"fake:{it.evidence_id}",
                    title=f"{dependency}: {symbol} usage",
                    severity="medium",
                    confidence="high",
                    evidence_ids=[it.evidence_id, doc_id],
                    recommendation=(
                        f"Review this {dependency} usage for the target upgrade; "
                        "see the official migration guide."
                    ),
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
    replay_dir: str | None = None,
    recording_dir: str | None = None,
) -> dict[str, object]:
    """Run the full assess pipeline and return a result bundle for rendering."""
    request = AssessmentRequest(repo=repo, dependency=dependency, target_version=target_version)

    with ToolContext() as ctx:
        collection = collect_evidence(request, ctx)
        # Fake responses must be built *after* collection: they cite the real
        # evidence ids that the scan just discovered.
        fake_responses = _inject_demo_fixtures(collection) if mode == "fake" else {}
        gateway = ModelGateway(
            ModelConfig(
                mode=ModelMode(mode),
                model=model or "qwen-plus",
                api_key=api_key or "",
                base_url=base_url or "",
            ),
            fake_responses=fake_responses or None,
            replay_dir=replay_dir,
            recording_dir=recording_dir,
        )
        outcome = analyse(collection, gateway, ctx)

        draft = None
        skill = outcome.skill
        if skill is not None and skill.allow_patch_draft:
            draft = generate_patch_draft(
                outcome.repo_path,
                outcome.verified.verified_risks,
                skill,
                outcome.bundle,
                quality_model_available=allow_quality_patch,
            )

    return {
        "code_report": outcome.code_report,
        "skill": skill,
        "bundle": outcome.bundle,
        "report": outcome.report,
        "verified": outcome.verified,
        "draft": draft,
    }


def _inject_demo_fixtures(collection: EvidenceCollection) -> dict[str, object]:
    """Add the illustrative doc citation to the bundle and return canned outputs.

    Mutates ``collection.bundle`` so the verifier can see the synthetic
    ``doc_chunk`` the crafted risks cite. Demo fixtures must never be the reason
    a run fails, so any error here degrades to "no canned output" -- the real
    pipeline then produces its (boring but honest) empty report.
    """
    try:
        responses, injected_docs = _build_fake_responses(
            collection.bundle, collection.request.dependency, collection.skill
        )
    except Exception:
        return {}
    for doc in injected_docs:
        collection.bundle.add(doc)
    return responses
