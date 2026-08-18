"""Security review domain types (plan stage S7).

These are the structured outputs of the security-review capability. They mirror
:class:`~upgradelens.core.finding.Finding` / :class:`FindingStatus` / :class:`Severity`
so the rest of the pipeline (verifiers, renderer, workbench) can treat a security
review like any other capability result.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.core.finding import FindingStatus, Severity

__all__ = [
    "CWE",
    "SecurityCategory",
    "SecurityFinding",
    "SecurityReviewReport",
    "Severity",
]


class CWE(StrEnum):
    """A small, curated CWE taxonomy covering the rules we scan for."""

    CWE_79 = "CWE-79"  # Cross-site scripting
    CWE_89 = "CWE-89"  # SQL injection
    CWE_78 = "CWE-78"  # OS command injection
    CWE_259 = "CWE-259"  # Hard-coded password / secret
    CWE_327 = "CWE-327"  # Broken or risky cryptographic algorithm
    CWE_502 = "CWE-502"  # Unsafe deserialization
    CWE_937 = "CWE-937"  # Using components with known vulnerabilities
    CWE_1104 = "CWE-1104"  # Use of unmaintained third-party components
    UNKNOWN = "unknown"


class SecurityCategory(StrEnum):
    """The high-level class a security finding belongs to."""

    SECRET = "secret"
    INJECTION = "injection"
    DEPENDENCY = "dependency"
    CRYPTO = "crypto"
    ACCESS_CONTROL = "access_control"
    MISCONFIG = "misconfiguration"


class SecurityFinding(BaseModel):
    """One concrete security issue, tied to evidence (a code location or CVE)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str
    title: str
    category: SecurityCategory
    cwe: CWE = CWE.UNKNOWN
    severity: Severity = Severity.MEDIUM
    confidence: float = 0.6
    file_path: str = ""
    line: int | None = None
    description: str = ""
    recommendation: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    #: When True the gate treats this as an explicit false-positive exemption.
    false_positive: bool = False
    status: FindingStatus = FindingStatus.CANDIDATE


class SecurityReviewReport(BaseModel):
    """The structured output of the security-review semantic summarizer.

    Designed to be produced both by a live model node and by the deterministic
    fake fixtures, so the capability runs fully offline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    review_id: str
    summary: str = ""
    findings: list[SecurityFinding] = Field(default_factory=list)
