"""PR Review capability (plan stage S4).

Declares the deterministic tool surface a PR review run may use and the quality
gate (coverage + no auto-fix) before any finding can drive an action. The plan
reuses the seven canonical tool names from :mod:`~upgradelens.capabilities.pr_review.tools`.
"""

from __future__ import annotations

from upgradelens.core.capability import BaseCapability, CapabilityPlan, CoveragePolicy
from upgradelens.core.task import SoftwareTask

from .tools import PR_REVIEW_TOOL_NAMES

__all__ = ["PRReviewCapability", "get_pr_review_capability"]


class PRReviewCapability(BaseCapability):
    """Code review of a pull request via deterministic analyzers + one model node."""


def get_pr_review_capability() -> PRReviewCapability:
    return PRReviewCapability(
        kind="pr_review",
        name="PR Review",
        description=(
            "Review a pull request for logic risk, compatibility, test gaps, "
            "impact, documentation and security issues using deterministic "
            "change/impact analysis backed by one model classification step."
        ),
        allowed_tools=PR_REVIEW_TOOL_NAMES,
        verifier_names=("pr_review_verifier",),
        coverage_policy=CoveragePolicy(
            min_coverage=0.5,
            required_inputs=["repo_root", "unified_diff"],
            min_confidence=0.5,
            forbidden_auto_fix=True,
            notes="PR reviews never auto-apply; every remedy requires approval.",
        ),
    )


def build_pr_review_plan(task: SoftwareTask) -> CapabilityPlan:
    """Return the deterministic PR review plan template for ``task``."""
    cap = get_pr_review_capability()
    return CapabilityPlan(
        task_id=task.task_id,
        capability_kind=cap.kind,
        steps=list(cap.allowed_tools),
        note="PR review via deterministic analyzers and one fake-able model node.",
    )
