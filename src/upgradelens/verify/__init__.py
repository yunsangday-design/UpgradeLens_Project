"""Evidence verification, rule-based risk scoring and test recommendation."""

from upgradelens.verify.models import (
    Conclusion,
    EvidenceStatus,
    IssueCode,
    RiskFactor,
    TestCandidate,
    VerificationIssue,
    VerifiedReport,
    VerifiedRisk,
)
from upgradelens.verify.recommender import rank_tests, recommend_tests
from upgradelens.verify.risk_rules import RiskScoringInput, is_major_bump, score_risk
from upgradelens.verify.verifier import EvidenceVerifier, verify_report

__all__ = [
    "Conclusion",
    "EvidenceStatus",
    "EvidenceVerifier",
    "IssueCode",
    "RiskFactor",
    "RiskScoringInput",
    "TestCandidate",
    "VerificationIssue",
    "VerifiedReport",
    "VerifiedRisk",
    "is_major_bump",
    "rank_tests",
    "recommend_tests",
    "score_risk",
    "verify_report",
]
