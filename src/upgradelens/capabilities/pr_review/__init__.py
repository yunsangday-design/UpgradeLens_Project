"""PR Review capability package (plan stage S4)."""

from __future__ import annotations

from upgradelens.capabilities.pr_review.analyzers import (
    PRReviewResult,
    analyze_change_impact,
    build_repository_context,
    load_change_set,
    recommend_tests,
    report_to_findings,
    retrieve_code_context,
    review_pull_request,
)
from upgradelens.capabilities.pr_review.capability import (
    PRReviewCapability,
    build_pr_review_plan,
    get_pr_review_capability,
)
from upgradelens.capabilities.pr_review.coverage import (
    CoverageSummary,
    compute_pr_review_coverage,
)
from upgradelens.capabilities.pr_review.models import (
    PRReviewReport,
    ReviewCategory,
    ReviewComment,
)
from upgradelens.capabilities.pr_review.renderer import render_pr_review
from upgradelens.capabilities.pr_review.tools import PR_REVIEW_TOOL_NAMES, pr_review_tools
from upgradelens.capabilities.pr_review.verifiers import pr_review_verifier

__all__ = [
    "PRReviewCapability",
    "PRReviewReport",
    "PRReviewResult",
    "PR_REVIEW_TOOL_NAMES",
    "ReviewCategory",
    "ReviewComment",
    "CoverageSummary",
    "analyze_change_impact",
    "build_pr_review_plan",
    "build_repository_context",
    "compute_pr_review_coverage",
    "get_pr_review_capability",
    "load_change_set",
    "pr_review_tools",
    "pr_review_verifier",
    "recommend_tests",
    "render_pr_review",
    "report_to_findings",
    "retrieve_code_context",
    "review_pull_request",
]
