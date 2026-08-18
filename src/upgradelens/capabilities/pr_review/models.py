"""PR review domain models (plan stage S4).

A :class:`PRReviewReport` is the structured output of the (fake or live) model
node that classifies a pull request's changes. Each :class:`ReviewComment` is one
review item and carries an explicit ``evidence_refs`` list so it can be converted
to a citable :class:`~upgradelens.core.finding.Finding` and later verified against
the actual change set.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.core.finding import FindingStatus, Severity

__all__ = [
    "ReviewCategory",
    "ReviewComment",
    "PRReviewReport",
]


class ReviewCategory(StrEnum):
    """The six review lenses a PR reviewer applies."""

    LOGIC_RISK = "logic_risk"
    COMPATIBILITY = "compatibility"
    TEST_GAP = "test_gap"
    IMPACT = "impact"
    DOCUMENTATION = "documentation"
    SECURITY = "security"


class ReviewComment(BaseModel):
    """One review item produced by the PR review node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment_id: str
    category: ReviewCategory
    severity: Severity = Severity.MEDIUM
    confidence: float = 0.6
    file_path: str = ""
    line: int | None = None
    summary: str = ""
    detail: str = ""
    recommendation: str = ""
    # ``code:<path>:<line>`` / ``doc:<source>`` / ``test:<path>`` references that
    # the verifier checks against the real change set before a finding is trusted.
    evidence_refs: list[str] = Field(default_factory=list)
    status: FindingStatus = FindingStatus.CANDIDATE


class PRReviewReport(BaseModel):
    """Structured output of the ``pr_review`` model node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str
    pr_title: str = ""
    comments: list[ReviewComment] = Field(default_factory=list)
    summary: str = ""

    __test__ = False
