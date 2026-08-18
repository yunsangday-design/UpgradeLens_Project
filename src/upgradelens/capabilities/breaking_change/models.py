"""Breaking-change domain models (plan stage S5).

A :class:`BreakingChangeReport` is the structured output of the (fake or live)
``breaking_change`` model node over an upgrade diff. Each :class:`BreakingChange`
is one API-level break and carries ``evidence_refs`` so it converts to a citable
:class:`~upgradelens.core.finding.Finding` and can be verified against the real
change set.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.core.finding import FindingStatus, Severity

__all__ = [
    "ApiChangeKind",
    "BreakingChange",
    "BreakingChangeReport",
]


class ApiChangeKind(StrEnum):
    """The taxonomy of API-level breaks we surface."""

    DELETION = "deletion"
    RENAME = "rename"
    SIGNATURE_CHANGE = "signature_change"
    TYPE_CHANGE = "type_change"
    BEHAVIOR_CHANGE = "behavior_change"


class BreakingChange(BaseModel):
    """One detected breaking API change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_id: str
    kind: ApiChangeKind
    severity: Severity = Severity.HIGH
    confidence: float = 0.7
    symbol: str = ""
    old_signature: str = ""
    new_signature: str = ""
    summary: str = ""
    detail: str = ""
    recommendation: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    status: FindingStatus = FindingStatus.CANDIDATE


class BreakingChangeReport(BaseModel):
    """Structured output of the ``breaking_change`` model node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str
    from_version: str = ""
    to_version: str = ""
    changes: list[BreakingChange] = Field(default_factory=list)
    summary: str = ""

    __test__ = False
