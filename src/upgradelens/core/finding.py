"""The evidence-backed finding (plan stage S1).

A :class:`Finding` is the common output shape for every capability. The single
most important rule is enforced by ``model_validator``: a finding promoted to
``VERIFIED`` must cite at least one real evidence id. This is the structural
anti-hallucination guarantee that made the dependency-upgrade verifier trustworthy,
now generalised to PR review, issue repair and security review.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "FindingStatus",
    "Severity",
    "EvidenceLink",
    "Finding",
    "resolve_finding_status",
]


class FindingStatus(StrEnum):
    """How much a finding has been corroborated.

    Only ``VERIFIED`` may drive an automatic remediation proposal. ``DEGRADED``
    (partially supported) and ``CANDIDATE`` (unverified hypothesis) must be
    surfaced but never silently promoted.
    """

    VERIFIED = "verified"
    DEGRADED = "degraded"
    CANDIDATE = "candidate"
    REJECTED = "rejected"


class Severity(StrEnum):
    """Capability-independent severity ladder."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class EvidenceLink(BaseModel):
    """A reference to one piece of evidence backing a finding.

    ``kind`` records where the evidence came from so the presenter can render it
    appropriately (code location, doc chunk, tool output, test result, scan result).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    kind: str = ""
    summary: str = ""


class Finding(BaseModel):
    """One conclusion produced by a capability, tied to evidence.

    Construction rejects a ``VERIFIED`` finding with no ``evidence_ids`` and bounds
    ``confidence`` to ``[0, 1]``. These checks are model-side: they catch a model
    that "promotes" a guess without citing anything, before any tool call runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str
    category: str
    severity: Severity = Severity.LOW
    confidence: float = 0.0
    summary: str
    detail: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    status: FindingStatus = FindingStatus.CANDIDATE
    requires_approval: bool = False
    action_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Finding:
        if not self.finding_id.strip():
            raise ValueError("Finding.finding_id must not be empty")
        if not self.summary.strip():
            raise ValueError("Finding.summary must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Finding.confidence must be in [0, 1]")
        if self.status is FindingStatus.VERIFIED and not self.evidence_ids:
            raise ValueError("a VERIFIED finding must reference at least one evidence id")
        return self


def resolve_finding_status(
    status: FindingStatus, evidence_ids: list[str] | tuple[str, ...]
) -> FindingStatus:
    """Degrade ``VERIFIED`` to ``CANDIDATE`` when no evidence is referenced.

    Live models occasionally claim VERIFIED without citing any evidence; the
    :class:`Finding` validator would then reject the whole capability report.
    Every capability's ``report_to_findings`` routes its model-provided status
    through this helper so the contract ("VERIFIED implies evidence") holds
    without crashing the run.
    """
    if status is FindingStatus.VERIFIED and not evidence_ids:
        return FindingStatus.CANDIDATE
    return status
