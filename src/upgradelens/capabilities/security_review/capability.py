"""Security Review capability declaration (plan stage S7).

Mirrors the PR-review capability: a deterministic plan (the five deterministic
tools) plus one fake-able model node (``security_review``).
"""

from __future__ import annotations

from upgradelens.capabilities.security_review.tools import SECURITY_REVIEW_TOOL_NAMES
from upgradelens.core.capability import (
    BaseCapability,
    CapabilityPlan,
    CapabilityRegistry,
    CoveragePolicy,
)
from upgradelens.core.task import SoftwareTask

__all__ = [
    "SecurityReviewCapability",
    "build_security_review_plan",
    "get_security_review_capability",
]


class SecurityReviewCapability(BaseCapability):
    """Reviews code for secrets, injection and dependency vulnerabilities."""


def build_security_review_plan(
    task: SoftwareTask, *, capability_registry: CapabilityRegistry | None = None
) -> CapabilityPlan:
    """Deterministic plan for a security-review task."""
    cap = get_security_review_capability()
    return CapabilityPlan(
        task_id=task.task_id,
        capability_kind=cap.kind,
        steps=list(cap.allowed_tools),
        note=(
            "Deterministic semgrep + dependency CVE analyzers and one "
            "fake-able model node, verified by the security gate."
        ),
    )


def get_security_review_capability() -> SecurityReviewCapability:
    """Return a configured :class:`SecurityReviewCapability`."""
    return SecurityReviewCapability(
        kind="security_review",
        name="Security Review",
        description=(
            "Review code diffs for hardcoded secrets, injection patterns and "
            "dependency CVEs using deterministic analyzers plus one model node."
        ),
        allowed_tools=SECURITY_REVIEW_TOOL_NAMES,
        verifier_names=("security_review_verifier",),
        coverage_policy=CoveragePolicy(
            min_coverage=0.6,
            required_inputs=["repo_root", "unified_diff"],
            min_confidence=0.5,
            forbidden_auto_fix=True,
            notes=(
                "Security review never auto-applies; high/critical findings "
                "require explicit approval after verification."
            ),
        ),
    )
