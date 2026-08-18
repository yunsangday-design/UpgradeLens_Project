"""Canonical runtime capability set (plan stages S2-S4+).

Aggregates the general-purpose task capabilities introduced in the software-agent
plan (dependency upgrade, PR review, ...). The existing optional *capability packs*
in :mod:`upgradelens.capabilities.base` are a different, additive mechanism and
are intentionally not registered here -- the runtime capability registry is about
task kinds, not upgrade-only abilities.
"""

from __future__ import annotations

from upgradelens.capabilities.breaking_change import get_breaking_change_capability
from upgradelens.capabilities.dependency_upgrade import (
    build_dependency_upgrade_capability,
)
from upgradelens.capabilities.issue_repair import get_issue_repair_capability
from upgradelens.capabilities.pr_review import get_pr_review_capability
from upgradelens.capabilities.security_review import get_security_review_capability
from upgradelens.core.capability import CapabilityRegistry, TaskCapability

__all__ = ["default_capability_registry", "get_default_capabilities"]


def default_capability_registry() -> CapabilityRegistry:
    """A registry containing every built-in task capability."""
    reg = CapabilityRegistry()
    for cap in get_default_capabilities():
        reg.register(cap)
    return reg


def get_default_capabilities() -> list[TaskCapability]:
    """The task capabilities available out of the box."""
    return [
        build_dependency_upgrade_capability(),
        get_pr_review_capability(),
        get_breaking_change_capability(),
        get_issue_repair_capability(),
        get_security_review_capability(),
    ]
