"""Breaking Change capability (plan stage S5).

Declares the deterministic tool surface a breaking-change analysis may use and the
quality gate before any finding can drive a remediation.
"""

from __future__ import annotations

from upgradelens.core.capability import BaseCapability, CapabilityPlan, CoveragePolicy
from upgradelens.core.task import SoftwareTask

from .tools import BREAKING_CHANGE_TOOL_NAMES

__all__ = ["BreakingChangeCapability", "get_breaking_change_capability"]


class BreakingChangeCapability(BaseCapability):
    """Identify API-level breaks introduced by a dependency upgrade."""


def get_breaking_change_capability() -> BreakingChangeCapability:
    return BreakingChangeCapability(
        kind="breaking_change",
        name="Breaking Change",
        description=(
            "Detect and classify breaking API changes (deletion, rename, signature "
            "and type changes, behavior changes) introduced by a dependency upgrade, "
            "using deterministic symbol/version analysis backed by one model node."
        ),
        allowed_tools=BREAKING_CHANGE_TOOL_NAMES,
        verifier_names=("breaking_change_verifier",),
        coverage_policy=CoveragePolicy(
            min_coverage=0.6,
            required_inputs=["repo_root", "unified_diff", "from_version", "to_version"],
            min_confidence=0.6,
            forbidden_auto_fix=True,
            notes="Breaking changes never auto-fix; remediation requires approval.",
        ),
    )


def build_breaking_change_plan(task: SoftwareTask) -> CapabilityPlan:
    """Return the deterministic breaking-change plan template for ``task``."""
    cap = get_breaking_change_capability()
    return CapabilityPlan(
        task_id=task.task_id,
        capability_kind=cap.kind,
        steps=list(cap.allowed_tools),
        note="Breaking-change detection via deterministic analyzers and one fake-able model node.",
    )
