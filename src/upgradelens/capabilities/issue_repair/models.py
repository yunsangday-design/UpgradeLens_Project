"""Issue-repair domain models (plan stage S6).

An :class:`IssueRepairReport` is the structured output of the (fake or live)
``issue_repair`` model node: a root-cause finding plus a :class:`PatchProposal`
and suggested tests. The patch is verified deterministically against the repo
before it can be proposed as an action.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.core.action import PatchProposal
from upgradelens.core.finding import FindingStatus

__all__ = ["Issue", "IssueRepairReport"]


class Issue(BaseModel):
    """A parsed bug report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issue_id: str = ""
    title: str = ""
    body: str = ""


class IssueRepairReport(BaseModel):
    """Structured output of the ``issue_repair`` model node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str
    issue_id: str = ""
    root_cause: str = ""
    patch: PatchProposal = Field(default_factory=lambda: PatchProposal(proposal_id=""))
    suggested_tests: list[str] = Field(default_factory=list)
    summary: str = ""
    status: FindingStatus = FindingStatus.CANDIDATE

    __test__ = False
