"""Nodes for the minimal assessment graph.

Each node receives the shared :class:`GraphState` plus the :class:`ModelGateway`
bound by the graph builder (the gateway is intentionally *not* stored in the
state, so the graph never has to serialize it). Nodes only ever ask the model
for structured output keyed to evidence already present in the bundle.
"""

from __future__ import annotations

from typing import Any

from upgradelens.domain.skill import SkillPackage
from upgradelens.graph.state import AssessmentSpec, GraphState
from upgradelens.llm.gateway import ModelGateway
from upgradelens.llm.prompts import BREAKING_CHANGE, IMPACT_REPORT, PLANNER
from upgradelens.models.impact import (
    BreakingChange,
    EvidenceBundle,
    ImpactReport,
    Plan,
    RiskItem,
)


def _summarize_code(bundle: EvidenceBundle) -> str:
    lines = [f"- {it.kind}: {it.summary}" for it in bundle.by_kind("code_usage")]
    return "\n".join(lines) or "(no code usages collected)"


def planner(state: GraphState, gateway: ModelGateway) -> dict[str, Any]:
    bundle = state["bundle"]
    spec: AssessmentSpec = state["spec"]
    skill: SkillPackage | None = state.get("skill")
    patterns = (
        "\n".join(f"- {p.id} ({p.usage_type or '?'}): {p.risk_hint}" for p in skill.patterns)
        if skill
        else "(no skill)"
    )
    prompt = PLANNER.render(
        dependency=spec.dependency,
        patterns=patterns,
        code_evidence=_summarize_code(bundle),
    )
    plan, _ = gateway.complete_structured(prompt=prompt, schema=Plan, name="planner")
    return {"plan": plan}


def breaking_change_extractor(state: GraphState, gateway: ModelGateway) -> dict[str, Any]:
    plan: Plan = state.get("plan") or Plan()
    ctx = state.get("context", "")
    changes: list[BreakingChange] = []
    for item in plan.items:
        prompt = BREAKING_CHANGE.render(
            pattern_id=item.pattern_id,
            question=item.question,
            context=ctx,
        )
        bc, _ = gateway.complete_structured(
            prompt=prompt, schema=BreakingChange, name=f"extractor__{item.pattern_id}"
        )
        changes.append(bc)
    return {"breaking_changes": changes}


def impact_analyzer(state: GraphState, gateway: ModelGateway) -> dict[str, Any]:
    bundle = state["bundle"]
    spec: AssessmentSpec = state["spec"]
    plan: Plan = state.get("plan") or Plan()
    changes: list[BreakingChange] = state.get("breaking_changes") or []
    ctx = state.get("context", "")

    plan_block = "\n".join(f"- {it.pattern_id}" for it in plan.items) or "(none)"
    change_block = "\n".join(f"- {c.title} ({c.severity})" for c in changes) or "(none)"
    prompt = IMPACT_REPORT.render(
        dependency=spec.dependency,
        plan=plan_block,
        breaking_changes=change_block,
        context=ctx,
    )
    draft, _ = gateway.complete_structured(
        prompt=prompt, schema=ImpactReport, name="impact_analyzer"
    )

    validated: list[RiskItem] = [
        r for r in draft.risks if r.evidence_ids and all(bundle.has(eid) for eid in r.evidence_ids)
    ]
    dropped = len(draft.risks) - len(validated)
    notes = draft.notes
    if dropped:
        tag = f"[dropped {dropped} risk(s) with unknown evidence ids]"
        notes = (notes + " " + tag).strip()

    summary: dict[str, int] = {}
    for it in bundle.items:
        summary[it.kind] = summary.get(it.kind, 0) + 1

    report = draft.model_copy(
        update={
            "target_dependency": spec.dependency,
            "source_version_spec": spec.source_version_spec,
            "target_version_spec": spec.target_version_spec,
            "risks": validated,
            "evidence_summary": summary,
            "static": False,
            "notes": notes,
        }
    )
    return {"report": report}
