"""Domain models for UpgradeLens."""

from upgradelens.domain.code_evidence import (
    CodeEvidenceReport,
    CodeEvidenceSummary,
    CodeUsage,
    DynamicImport,
    ParseError,
    TestProductionLink,
    UsageKind,
)
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
from upgradelens.domain.skill import (
    DocSource,
    PatchRule,
    SkillCatalog,
    SkillCatalogEntry,
    SkillPackage,
    SkillSelection,
    UsagePattern,
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
    "UsageKind",
    "CodeUsage",
    "DynamicImport",
    "ParseError",
    "CodeEvidenceSummary",
    "TestProductionLink",
    "CodeEvidenceReport",
    "DocSource",
    "PatchRule",
    "SkillCatalog",
    "SkillCatalogEntry",
    "SkillPackage",
    "SkillSelection",
    "UsagePattern",
]
