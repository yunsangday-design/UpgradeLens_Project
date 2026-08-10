"""Verifier data contracts (plan section 13).

The verifier turns a model-produced :class:`~upgradelens.models.impact.ImpactReport`
into an *auditable* report: every surfaced risk carries an evidence status, the
concrete checks that failed, and a rule-derived severity whose inputs are all
visible. Nothing here calls a model -- these types only describe outcomes.
"""

from __future__ import annotations

import datetime as _dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EvidenceStatus",
    "IssueCode",
    "Conclusion",
    "VerificationIssue",
    "RiskFactor",
    "TestCandidate",
    "VerifiedRisk",
    "VerifiedReport",
    "BLOCKING_ISSUES",
]


def _frozen() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


class EvidenceStatus(StrEnum):
    """Evidence status for one risk (plan section 13.2).

    Only :attr:`VERIFIED` means "code *and* official documentation both
    checked out". Degraded findings must never be promoted to it.
    """

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    NOT_APPLICABLE = "not_applicable"


class IssueCode(StrEnum):
    """A specific rule check that failed (plan section 13.1)."""

    NO_EVIDENCE_IDS = "no_evidence_ids"
    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
    NO_CODE_EVIDENCE = "no_code_evidence"
    FILE_NOT_FOUND = "file_not_found"
    LINE_OUT_OF_RANGE = "line_out_of_range"
    CONTENT_HASH_CHANGED = "content_hash_changed"
    NO_DOC_EVIDENCE = "no_doc_evidence"
    DOC_VERSION_CONFLICT = "doc_version_conflict"
    DOC_SOURCE_UNTRUSTED = "doc_source_untrusted"
    DYNAMIC_ONLY_EVIDENCE = "dynamic_only_evidence"
    SYMBOL_NOT_IN_EVIDENCE = "symbol_not_in_evidence"
    UNKNOWN_TEST_PATH = "unknown_test_path"


#: Issues that make a risk structurally untrustworthy. A risk carrying any of
#: these can never reach :attr:`EvidenceStatus.VERIFIED`.
BLOCKING_ISSUES: frozenset[IssueCode] = frozenset(
    {
        IssueCode.NO_EVIDENCE_IDS,
        IssueCode.UNKNOWN_EVIDENCE_ID,
        IssueCode.NO_CODE_EVIDENCE,
        IssueCode.FILE_NOT_FOUND,
        IssueCode.LINE_OUT_OF_RANGE,
        IssueCode.CONTENT_HASH_CHANGED,
        IssueCode.DYNAMIC_ONLY_EVIDENCE,
    }
)


class Conclusion(StrEnum):
    """Top-level answer for the whole assessment."""

    IMPACTED = "impacted"
    NO_IMPACT = "no_impact"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"


class VerificationIssue(BaseModel):
    """One failed check, always tied to something concrete."""

    model_config = _frozen()

    code: IssueCode
    detail: str
    evidence_id: str | None = None


class RiskFactor(BaseModel):
    """One explainable contribution to the rule-derived severity.

    ``points`` may be negative (e.g. existing test coverage lowers the score).
    """

    model_config = _frozen()

    name: str
    value: str
    points: int


class TestCandidate(BaseModel):
    """A real test file that likely exercises the impacted production code."""

    model_config = _frozen()

    test_path: str
    production_path: str
    matched_by: str
    reason: str = ""


class VerifiedRisk(BaseModel):
    """A risk after verification.

    ``model_severity`` keeps what the model said; ``severity`` is what the rule
    engine decided. Showing both makes disagreement visible instead of hiding
    it behind a single number.
    """

    model_config = _frozen()

    risk_id: str
    title: str
    status: EvidenceStatus
    severity: str
    model_severity: str
    rule_score: int = 0
    factors: list[RiskFactor] = Field(default_factory=list)
    code_evidence_ids: list[str] = Field(default_factory=list)
    doc_evidence_ids: list[str] = Field(default_factory=list)
    unknown_evidence_ids: list[str] = Field(default_factory=list)
    issues: list[VerificationIssue] = Field(default_factory=list)
    recommended_tests: list[TestCandidate] = Field(default_factory=list)
    recommendation: str = ""

    @property
    def is_verified(self) -> bool:
        return self.status is EvidenceStatus.VERIFIED


class VerifiedReport(BaseModel):
    """The auditable output of stage 6.

    ``verified_risks`` and ``degraded_risks`` are deliberately separate lists so
    a reader can never mistake an unproven finding for a confirmed one.
    """

    model_config = _frozen()

    schema_version: str = "verified-report/1"
    target_dependency: str = ""
    source_version_spec: str = ""
    source_version_source: str = ""
    target_version_spec: str = ""
    generated_at: str = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC).isoformat())
    conclusion: Conclusion = Conclusion.IMPACTED
    verified_risks: list[VerifiedRisk] = Field(default_factory=list)
    degraded_risks: list[VerifiedRisk] = Field(default_factory=list)
    recommended_tests: list[TestCandidate] = Field(default_factory=list)
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    partial: bool = False
    degradations: list[str] = Field(default_factory=list)
    static: bool = False
    notes: str = ""

    @property
    def all_risks(self) -> list[VerifiedRisk]:
        return [*self.verified_risks, *self.degraded_risks]

    @property
    def citation_existence_rate(self) -> float:
        """Share of referenced evidence ids that actually exist.

        Returns ``1.0`` when nothing was referenced, so an empty report is not
        punished as if it had hallucinated.
        """
        total = 0
        missing = 0
        for risk in self.all_risks:
            total += (
                len(risk.code_evidence_ids)
                + len(risk.doc_evidence_ids)
                + len(risk.unknown_evidence_ids)
            )
            missing += len(risk.unknown_evidence_ids)
        if total == 0:
            return 1.0
        return (total - missing) / total
