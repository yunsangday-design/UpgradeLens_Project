"""Dependency Upgrade as the first concrete capability (plan stage S2).

This wraps the *existing* upgrade pipeline behind the generic
:class:`~upgradelens.core.capability.TaskCapability` protocol introduced in S1, so
the router/planner/loop can treat it uniformly with the PR-review, issue-repair and
security capabilities that follow. The plan steps are the deterministic upgrade
steps (clone -> scan -> retrieve -> ... -> verify -> PR); no LLM is needed to
produce them, which is exactly why the project already runs the same list in
``fake`` mode.
"""

from __future__ import annotations

from upgradelens.agent.run_store import DEFAULT_PLAN_STEPS
from upgradelens.core.capability import (
    BaseCapability,
    CapabilityPlan,
    CoveragePolicy,
)
from upgradelens.core.task import SoftwareTask

__all__ = ["DependencyUpgradeCapability", "DEPENDENCY_UPGRADE_STEPS"]


# ``DEFAULT_PLAN_STEPS`` is a sequence of plan-step dicts; the capability plan
# declares the *ordered tool names* (strings), so we project them out.
DEPENDENCY_UPGRADE_STEPS: tuple[str, ...] = tuple(step["tool"] for step in DEFAULT_PLAN_STEPS)


class DependencyUpgradeCapability(BaseCapability):
    """Capability for upgrading a Python dependency to a target version."""

    def build_plan(self, task: SoftwareTask) -> CapabilityPlan:
        return CapabilityPlan(
            task_id=task.task_id,
            capability_kind=self.kind,
            steps=list(DEPENDENCY_UPGRADE_STEPS),
            note="dependency upgrade (deterministic pipeline steps)",
        )


def build_dependency_upgrade_capability() -> DependencyUpgradeCapability:
    """Construct the default dependency-upgrade capability."""
    return DependencyUpgradeCapability(
        kind="dependency_upgrade",
        name="Dependency Upgrade",
        allowed_tools=tuple(DEPENDENCY_UPGRADE_STEPS),
        verifier_names=("upgrade_verifier",),
        coverage_policy=CoveragePolicy(
            min_coverage=0.6,
            required_inputs=["repo", "dependency", "target_version"],
            min_confidence=0.5,
            forbidden_auto_fix=True,
            notes="Dependency upgrades never auto-apply; every remediation requires approval.",
        ),
        description=(
            "Upgrade a Python dependency to a target version with evidence-backed "
            "risk analysis and a verified change plan."
        ),
    )
