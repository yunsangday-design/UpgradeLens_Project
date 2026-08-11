"""S12: final upgrade-assessment presentation contract.

These are **view models** (DTOs) consumed by the demo UI, the markdown/mcp
reports and any external Coding Agent. They flatten a verified assessment,
its evidence bundle and an optional upgrade plan into a single, self-contained
structure so that **no caller ever has to re-join evidence IDs at the
frontend** -- every code location and document reference is already resolved.

All fields carry defaults so legacy ``report.json`` / ``VerifiedReport`` data
that predates this contract still loads cleanly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CodeLocationView(BaseModel):
    """A resolved code usage behind a risk (from ``EvidenceBundle`` code_evidence)."""

    evidence_id: str = ""
    path: str = ""
    start_line: int = 0
    end_line: int = 0
    column: int = 0
    symbol: str = ""
    snippet: str = ""
    is_test_code: bool = False
    confidence: str = "high"


class DocumentReferenceView(BaseModel):
    """A resolved documentation reference behind a risk."""

    evidence_id: str = ""
    title: str = ""
    url: str = ""
    heading_path: list[str] = []
    snippet: str = ""
    trust_level: str = ""
    trust_label: str = ""
    source_version_spec: str = ""
    target_version_spec: str = ""


class MigrationAdviceView(BaseModel):
    """Human-readable migration guidance flattened from a ``VerifiedRisk``."""

    problem: str = ""
    behavior_change: str = ""
    steps: list[str] = []
    verification_steps: list[str] = []
    before_example: str = ""
    after_example: str = ""


class RagResolutionView(BaseModel):
    """One RAG retrieval resolution (doc chunk) behind a risk."""

    evidence_id: str = ""
    source_id: str = ""
    url: str = ""
    title: str = ""
    heading_path: list[str] = []
    chunk_title: str = ""
    snapshot_hash: str = ""
    score: float = 0.0
    matched_query: str = ""
    trust_level: str = ""
    trust_label: str = ""
    version_range: str = ""


class UpgradePlanStepRef(BaseModel):
    """A read-only reference to one upgrade plan step, anchored by target files."""

    step_id: str = ""
    title: str = ""
    severity: str = ""
    status: str = ""
    target_files: list[str] = []
    api_symbols: list[str] = []
    evidence_status: str = ""
    severity_label: str = ""
    evidence_status_label: str = ""


class UpgradePlanRef(BaseModel):
    """A read-only reference to the deploy contract an assessment maps onto."""

    schema_version: str = ""
    target_dependency: str = ""
    source_version_spec: str = ""
    target_version_spec: str = ""
    repo_hash: str = ""
    mode: str = ""
    deploy_contract: bool = False
    step_count: int = 0
    steps: list[UpgradePlanStepRef] = []


class UpgradeFindingView(BaseModel):
    """One finding: a single verified/degraded risk plus its resolved evidence."""

    risk_id: str = ""
    title: str = ""
    severity: str = "low"
    model_severity: str = "low"
    status: str = ""
    evidence_status: str = ""
    severity_label: str = ""
    evidence_status_label: str = ""
    rule_score: int = 0
    recommendation: str = ""
    code: list[CodeLocationView] = []
    docs: list[DocumentReferenceView] = []
    rag: list[RagResolutionView] = []
    migration: MigrationAdviceView = MigrationAdviceView()
    plan_step: UpgradePlanStepRef | None = None


class UpgradeAssessmentView(BaseModel):
    """Top-level, self-contained upgrade assessment view (S12 contract)."""

    schema_version: str = "assessment-view/1"
    target_dependency: str = ""
    source_version_spec: str = ""
    target_version_spec: str = ""
    generated_at: str = ""
    static: bool = False
    # Verdict strictly separates "no risk" from "evidence insufficient".
    verdict: str = ""  # needs_upgrade | no_risk | no_impact | evidence_insufficient
    verdict_label: str = ""
    conclusion_label: str = ""
    is_evidence_insufficient: bool = False
    no_impact: bool = False
    is_partial: bool = False
    degradations: list[str] = []
    verified_risks: list[UpgradeFindingView] = []
    degraded_risks: list[UpgradeFindingView] = []
    recommended_tests: list[dict[str, Any]] = []
    citation_existence_rate: float = 0.0
    upgrade_plan: UpgradePlanRef | None = None
    notes: str = ""

    @staticmethod
    def _verdict_for(
        conclusion: object,
        is_evidence_insufficient: bool,
        has_verified: bool,
        has_degraded: bool,
        has_code: bool,
    ) -> str:
        """Deterministically derive the verdict.

        "no risk" (the dependency is used but no breaking change applies) is
        kept strictly separate from "evidence insufficient" (the code could not
        be verified against the docs) and from "no impact" (the dependency is
        not used at all).
        """
        if is_evidence_insufficient:
            return "evidence_insufficient"
        if has_verified or has_degraded:
            return "needs_upgrade"
        if has_code:
            return "no_risk"
        return "no_impact"
