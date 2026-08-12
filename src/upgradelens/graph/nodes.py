"""Nodes for the minimal assessment graph.

Each node receives the shared :class:`GraphState` plus the :class:`ModelGateway`
bound by the graph builder (the gateway is intentionally *not* stored in the
state, so the graph never has to serialize it). Nodes only ever ask the model
for structured output keyed to evidence already present in the bundle.
"""

from __future__ import annotations

from typing import Any

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


def _code_symbols(bundle: EvidenceBundle) -> str:
    """The API surface the repository actually touches, taken from the AST scan.

    This replaces the hand-curated skill pattern list: the symbols are derived
    from evidence, so a dependency with no Skill Pack gets the same planning
    signal as one with a pack.
    """
    symbols = {str(it.meta.get("symbol", "")) for it in bundle.by_kind("code_usage")}
    symbols.discard("")
    return "\n".join(f"- {symbol}" for symbol in sorted(symbols)) or "(no API symbols found)"


def _summarize_docs(bundle: EvidenceBundle) -> str:
    """Headings of the documentation the shared corpus retrieved for this upgrade."""
    lines = []
    for it in bundle.by_kind("doc_chunk"):
        heading = str(it.meta.get("chunk_title") or "") or it.summary
        lines.append(f"- [{it.evidence_id}] {heading}")
    return "\n".join(lines) or "(no documentation retrieved)"


#: Step 13, #2.3 -- cap the number of breaking-change topics the extractor
#: processes in serial, to keep the extractor's LLM budget bounded.
_MAX_PLAN_ITEMS = 6


def planner(state: GraphState, gateway: ModelGateway) -> dict[str, Any]:
    """Plan the analysis from evidence only -- no dedicated Skill Pack required."""
    bundle = state["bundle"]
    spec: AssessmentSpec = state["spec"]
    prompt = PLANNER.render(
        dependency=spec.dependency,
        source_version=spec.source_version.label if spec.source_version else "",
        code_symbols=_code_symbols(bundle),
        doc_evidence=_summarize_docs(bundle),
        code_evidence=_summarize_code(bundle),
    )
    plan, _ = gateway.complete_structured(prompt=prompt, schema=Plan, name="planner")
    items = list(plan.items)
    if len(items) > _MAX_PLAN_ITEMS:
        kept = items[:_MAX_PLAN_ITEMS]
        note = (
            f"[planner] limited breaking-change analysis to the top "
            f"{len(kept)} of {len(items)} candidate topics; the remaining "
            f"{len(items) - len(kept)} were not examined to bound LLM calls."
        )
        plan = plan.model_copy(update={"items": kept, "note": note})
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
        source_version=spec.source_version.label if spec.source_version else "",
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
    if getattr(plan, "note", ""):
        notes = (notes + " " + plan.note).strip()
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
            "source_version_source": spec.source_version.label if spec.source_version else "",
            "target_version_spec": spec.target_version_spec,
            "risks": validated,
            "evidence_summary": summary,
            "static": False,
            "notes": notes,
        }
    )
    return {"report": report}
