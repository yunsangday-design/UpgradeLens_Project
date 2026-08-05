"""Domain models for UpgradeLens."""

from upgradelens.domain.dependency import (
    SCHEMA_VERSION,
    DependencyAnalysisRequest,
    DependencyDeclaration,
    DependencyScanResult,
    IssueCode,
    ManifestType,
    ParseIssue,
    ResolutionStatus,
    VersionTransition,
    VersionTransitionKind,
)

__all__ = [
    "SCHEMA_VERSION",
    "DependencyAnalysisRequest",
    "DependencyDeclaration",
    "DependencyScanResult",
    "IssueCode",
    "ManifestType",
    "ParseIssue",
    "ResolutionStatus",
    "VersionTransition",
    "VersionTransitionKind",
]
