"""Dependency Upgrade capability package (plan stage S2)."""

from __future__ import annotations

from upgradelens.capabilities.dependency_upgrade.adapters import (
    actions_from_impact,
    findings_from_impact,
    software_task_to_request,
    verification_from_verified,
)
from upgradelens.capabilities.dependency_upgrade.analyzers import (
    recommend_regression_tests,
)
from upgradelens.capabilities.dependency_upgrade.capability import (
    DEPENDENCY_UPGRADE_STEPS,
    DependencyUpgradeCapability,
    build_dependency_upgrade_capability,
)
from upgradelens.core.capability import TaskCapability

__all__ = [
    "DependencyUpgradeCapability",
    "DEPENDENCY_UPGRADE_STEPS",
    "build_dependency_upgrade_capability",
    "software_task_to_request",
    "findings_from_impact",
    "actions_from_impact",
    "verification_from_verified",
    "recommend_regression_tests",
    "get_default_capabilities",
]


def get_default_capabilities() -> list[TaskCapability]:
    """The capabilities available out of the box.

    Today this is just the dependency-upgrade capability; the PR-review,
    issue-repair and security capabilities from later plan stages register
    themselves here as they land.
    """
    return [build_dependency_upgrade_capability()]
