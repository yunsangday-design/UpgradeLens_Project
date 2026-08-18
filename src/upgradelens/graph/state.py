"""Shared state types for the stage 5 assessment graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from upgradelens.llm.gateway import ModelGateway
from upgradelens.models.impact import (
    BreakingChange,
    EvidenceBundle,
    ImpactReport,
    Plan,
    SourceVersion,
)


@dataclass(frozen=True)
class AssessmentSpec:
    repo: str
    dependency: str
    target_version_spec: str
    source_version_spec: str = ""
    source_version: SourceVersion | None = None


class GraphState(TypedDict, total=False):
    """State shared across the linear assessment graph.

    The graph is acyclic (planner -> breaking_change_extractor ->
    impact_analyzer -> END), so there is no path to an infinite loop.

    LS-1: the deprecated ``skill`` key was removed -- planner / extractor /
    impact run on evidence alone (``bundle`` + ``spec``).
    """

    spec: AssessmentSpec
    bundle: EvidenceBundle
    context: str
    plan: Plan
    gateway: ModelGateway
    breaking_changes: list[BreakingChange]
    report: ImpactReport
