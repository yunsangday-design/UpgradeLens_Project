"""Minimal LangGraph loop wiring and the assessment entry point (stage 5)."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph
from sqlalchemy.orm import Session

from upgradelens.domain.doc_evidence import RetrievalRun
from upgradelens.domain.skill import SkillPackage
from upgradelens.graph.nodes import (
    breaking_change_extractor,
    impact_analyzer,
    planner,
)
from upgradelens.graph.state import AssessmentSpec, GraphState
from upgradelens.llm.context import ContextBuilder
from upgradelens.llm.gateway import (
    BudgetExceededError,
    ModelGateway,
    ModelUnavailableError,
)
from upgradelens.models.impact import (
    EvidenceBundle,
    ImpactReport,
    build_static_report,
)


def build_main_graph(gateway: ModelGateway) -> Any:
    """Compile the acyclic assessment graph.

    Edges: planner -> breaking_change_extractor -> impact_analyzer -> END.
    Because there is no back-edge, the loop can never run forever.
    """

    def _planner(state: GraphState) -> dict[str, Any]:
        return planner(state, gateway=gateway)

    def _extractor(state: GraphState) -> dict[str, Any]:
        return breaking_change_extractor(state, gateway=gateway)

    def _analyzer(state: GraphState) -> dict[str, Any]:
        return impact_analyzer(state, gateway=gateway)

    g = StateGraph(GraphState)
    g.add_node("planner", _planner)
    g.add_node("breaking_change_extractor", _extractor)
    g.add_node("impact_analyzer", _analyzer)
    g.add_edge("planner", "breaking_change_extractor")
    g.add_edge("breaking_change_extractor", "impact_analyzer")
    g.add_edge("impact_analyzer", END)
    g.set_entry_point("planner")
    return g.compile()


def retrieve_skill_evidence(
    session: Session | None,
    skill: SkillPackage | None,
    *,
    source_id: str | None = None,
    top_k: int = 3,
) -> list[RetrievalRun]:
    """Pull documentation retrieval runs for every pattern's retrieval queries."""
    if session is None or skill is None:
        return []
    from upgradelens.docs.retrieval import retrieve

    runs: list[RetrievalRun] = []
    for source in skill.sources:
        sid = source_id or source.id
        for pattern in skill.patterns:
            for query in pattern.retrieval_queries:
                run = retrieve(session, sid, query, top_k=top_k, record=False)
                runs.append(run)
    return runs


def run_assessment(
    spec: AssessmentSpec,
    bundle: EvidenceBundle,
    gateway: ModelGateway,
    *,
    skill: SkillPackage | None = None,
    max_context_tokens: int = 6000,
) -> ImpactReport:
    """Run the closed loop and return a structured impact report.

    If the model is unavailable or the token budget is exceeded, a deterministic
    static report is returned instead (``static=True``); risks in that report
    still reference only real evidence ids.
    """
    context = ContextBuilder(gateway.budget).build(
        bundle, None, max_context_tokens=max_context_tokens
    )
    app = build_main_graph(gateway)
    initial: GraphState = {
        "spec": spec,
        "skill": skill,
        "bundle": bundle,
        "context": context,
    }
    try:
        result = app.invoke(initial)
    except (ModelUnavailableError, BudgetExceededError) as exc:
        return build_static_report(
            bundle,
            skill,
            dependency=spec.dependency,
            source_version_spec=spec.source_version_spec,
            target_version_spec=spec.target_version_spec,
            notes=f"Static fallback ({type(exc).__name__}).",
        )

    report = result.get("report") if isinstance(result, dict) else None
    if not isinstance(report, ImpactReport):
        return build_static_report(
            bundle,
            skill,
            dependency=spec.dependency,
            source_version_spec=spec.source_version_spec,
            target_version_spec=spec.target_version_spec,
            notes="Static fallback: impact analyzer produced no report.",
        )
    return report


__all__ = [
    "AssessmentSpec",
    "build_main_graph",
    "retrieve_skill_evidence",
    "run_assessment",
]
