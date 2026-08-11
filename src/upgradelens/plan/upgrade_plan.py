"""Stable, externally-consumable upgrade plan schema (S7).

The plan is derived from a verified assessment outcome. It never re-runs a model and
never mutates anything; it is a read-only projection of :class:`VerifiedReport` plus the
deterministic patch draft, reshaped so an external Coding Agent can act on it.

Field sources
-------------
* ``target_files`` -- the code-evidence paths and recommended-test paths behind each risk.
* ``api_symbols`` -- the old dependency API symbols the step is expected to remove.
* ``change_reason`` / ``doc_evidence`` -- straight from the verified risk.
* ``forbidden_regions`` -- every target file owned by *another* step, so one step can
  never silently edit a file another step is responsible for.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.patch.models import PatchDraft
from upgradelens.pipeline import AssessmentOutcome
from upgradelens.verify.models import VerifiedReport, VerifiedRisk

__all__ = [
    "PlanMode",
    "UpgradeStep",
    "UpgradePlan",
    "build_upgrade_plan",
    "export_plan",
    "repo_hash_of",
]


def _frozen() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


class PlanMode(StrEnum):
    """What the plan is allowed to ask an executor to do (phase 1).

    These are the only two actions supported in stage 8 phase 1. Anything beyond
    drafting a patch or applying it inside a throwaway sandbox (auto-commit, push,
    opening a PR, editing unrelated files) is intentionally out of scope.
    """

    PATCH_DRAFT = "patch_draft"
    SANDBOX_APPLY = "sandbox_apply"


class UpgradeStep(BaseModel):
    """One actionable remediation step, derived from a single verified risk."""

    model_config = _frozen()

    step_id: str
    title: str
    severity: str = "low"
    evidence_status: str = ""
    target_files: list[str] = Field(default_factory=list)
    #: Old dependency API symbols this step is expected to remove.
    api_symbols: list[str] = Field(default_factory=list)
    change_reason: str = ""
    doc_evidence: list[str] = Field(default_factory=list)
    suggested_approach: str = ""
    #: Files owned by *other* steps -- this step must never touch them.
    forbidden_regions: list[str] = Field(default_factory=list)
    recommended_tests: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    # S13: before/after code contrast so the modification is self-explanatory.
    before_example: str = ""
    after_example: str = ""


class UpgradePlan(BaseModel):
    """The externally-consumable upgrade plan (plan section 17)."""

    model_config = _frozen()

    schema_version: str = "upgrade-plan/1"
    target_dependency: str = ""
    source_version_spec: str = ""
    target_version_spec: str = ""
    #: Repo state the plan was built against; an executor must refuse to apply on a
    #: different hash so a stale plan can never be applied to a moved target.
    repo_hash: str = ""
    generated_at: str = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC).isoformat())
    mode: PlanMode = PlanMode.PATCH_DRAFT
    # S12: marks whether this plan is a concrete deploy contract (vs a draft).
    deploy_contract: bool = False
    steps: list[UpgradeStep] = Field(default_factory=list)
    patch: PatchDraft | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def step_count(self) -> int:
        """Number of modification steps (exposed for templates/UI convenience)."""
        return len(self.steps)

    def to_execution_contract(self) -> dict[str, Any]:
        """Human/machine readable summary of the apply contract for this plan."""
        return {
            "schema_version": self.schema_version,
            "target_dependency": self.target_dependency,
            "source_version_spec": self.source_version_spec,
            "target_version_spec": self.target_version_spec,
            "repo_hash": self.repo_hash,
            "mode": self.mode.value,
            "step_count": len(self.steps),
            "has_patch": self.patch is not None,
            "rules": [
                "Apply only at the recorded repo_hash.",
                "Edit only files named in a step's target_files.",
                "Never touch a file listed in any step's forbidden_regions.",
                "Phase 1 supports patch_draft or sandbox_apply only.",
                "No auto-commit, no auto-push, no workspace mutation outside the sandbox.",
            ],
        }


def repo_hash_of(repo_root: str | Path) -> str:
    """Return ``git rev-parse HEAD`` for ``repo_root``, or ``""`` if not a git repo.

    The hash anchors the plan to a precise repo state so an executor can refuse to
    apply a stale plan.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return ""


def build_upgrade_plan(
    outcome: AssessmentOutcome,
    *,
    repo_root: str | Path,
    mode: PlanMode = PlanMode.PATCH_DRAFT,
    allow_quality_patch: bool = False,
) -> UpgradePlan:
    """Project a verified assessment outcome onto a stable :class:`UpgradePlan`.

    Only *verified* risks become steps -- degraded findings are surfaced as warnings so
    an external agent knows they exist but is not told to act on unproven claims. The
    deterministic patch draft is attached when the requested ``mode`` produces one.
    """
    verified = outcome.verified or VerifiedReport()
    bundle = outcome.bundle

    # Map evidence id -> (relative path, api symbol) from the code-evidence bundle.
    paths_by_evidence: dict[str, str] = {}
    symbols_by_evidence: dict[str, str] = {}
    for item in bundle.by_kind("code_usage"):
        eid = item.evidence_id
        p = str(item.meta.get("path", ""))
        s = str(item.meta.get("symbol", ""))
        if p:
            paths_by_evidence[eid] = p
        if s:
            symbols_by_evidence[eid] = s

    risks: list[VerifiedRisk] = []
    targets: list[list[str]] = []
    symbols: list[list[str]] = []
    for risk in verified.verified_risks:
        target_files: list[str] = sorted(
            {paths_by_evidence[e] for e in risk.code_evidence_ids if e in paths_by_evidence}
            | {t.test_path for t in risk.recommended_tests}
        )
        api_symbols: list[str] = sorted(
            {
                symbols_by_evidence[e]
                for e in risk.code_evidence_ids
                if e in symbols_by_evidence and symbols_by_evidence[e]
            }
        )
        risks.append(risk)
        targets.append(target_files)
        symbols.append(api_symbols)

    # A step must never edit a file another step owns.
    flat_targets = sorted({f for t in targets for f in t})

    steps: list[UpgradeStep] = []
    for risk, target_files, api_symbols in zip(risks, targets, symbols, strict=True):
        others = sorted(set(flat_targets) - set(target_files))
        criteria = [
            f"Remove the old API symbol(s) {', '.join(api_symbols) or 'n/a'} "
            f"from {', '.join(target_files) or 'the impacted files'}.",
            "Confirm no remaining reference to the removed API in the target files.",
        ]
        if target_files:
            criteria.append(
                "Run the recommended tests and confirm they pass against the target version."
            )
        # S13: richer, model-grounded step description.
        change_reason = risk.problem or risk.recommendation
        step_criteria = (
            list(risk.verification_steps)
            if risk.verification_steps
            else criteria
        )
        steps.append(
            UpgradeStep(
                step_id=risk.risk_id,
                title=risk.title,
                severity=risk.severity,
                evidence_status=risk.status.value,
                target_files=target_files,
                api_symbols=api_symbols,
                change_reason=change_reason,
                doc_evidence=list(risk.doc_evidence_ids),
                suggested_approach=risk.recommendation,
                forbidden_regions=others,
                recommended_tests=[t.test_path for t in risk.recommended_tests],
                completion_criteria=step_criteria,
                before_example=risk.before_example or "",
                after_example=risk.after_example or "",
            )
        )

    patch: PatchDraft | None = None
    if mode in (PlanMode.PATCH_DRAFT, PlanMode.SANDBOX_APPLY):
        try:
            from upgradelens.capabilities import TransformationPack
            from upgradelens.patch import generate_patch_draft

            capability = (
                TransformationPack.from_skill(outcome.skill)
                if outcome.skill is not None
                else None
            )
            verified_risks = outcome.verified.verified_risks if outcome.verified else []
            patch = generate_patch_draft(
                Path(repo_root),
                verified_risks,
                capability,
                outcome.bundle,
                quality_model_available=allow_quality_patch,
            )
        except Exception:  # pragma: no cover - drafting is best-effort
            patch = None

    warnings = [
        f"Degraded finding not turned into a step: {r.risk_id} ({r.status.value})"
        for r in verified.degraded_risks
    ]
    if not steps:
        warnings.append("No verified risk produced a step; plan is empty.")

    return UpgradePlan(
        target_dependency=verified.target_dependency,
        source_version_spec=verified.source_version_spec,
        target_version_spec=verified.target_version_spec,
        repo_hash=repo_hash_of(repo_root),
        mode=mode,
        steps=steps,
        patch=patch,
        assumptions=[
            "Plan built against repo_hash "
            f"{repo_hash_of(repo_root) or '<unknown>'} -- apply only at a matching hash.",
            "Steps cover only verified risks; degraded findings are warnings, not actions.",
        ],
        warnings=warnings,
    )


def export_plan(plan: UpgradePlan, destination: str | Path) -> Path:
    """Write ``plan`` as pretty JSON to ``destination`` (the ``plan-only`` artifact)."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return dest
