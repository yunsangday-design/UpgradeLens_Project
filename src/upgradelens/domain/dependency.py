"""Domain models for stage 1 dependency manifest analysis.

Design rules (see plan section 8.4):

- Pydantic models own boundary validation and JSON serialization only;
- parsing logic lives in :mod:`upgradelens.analyzers`, not in validators;
- enum values are stable, lowercase and JSON friendly;
- expected failures are returned as structured issues, not raised as tracebacks;
- ``CodeEvidence`` / ``RiskItem`` / ``AnalysisState`` are intentionally absent —
  they belong to later stages.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"

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


class ManifestType(StrEnum):
    """Manifest kinds supported in stage 1."""

    REQUIREMENTS_TXT = "requirements_txt"
    PYPROJECT_TOML = "pyproject_toml"


class ResolutionStatus(StrEnum):
    """How confidently the current version could be determined.

    Only :attr:`RESOLVED` allows a definite version span to be reported.
    """

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"


class VersionTransitionKind(StrEnum):
    """Direction of the version change, or ``UNKNOWN`` when undecidable."""

    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    SAME = "same"
    UNKNOWN = "unknown"


class IssueCode(StrEnum):
    """Stable codes for structured warnings and errors.

    Codes are part of the output contract: callers may branch on them, so they
    must not be renamed without bumping :data:`SCHEMA_VERSION`.
    """

    AMBIGUOUS_SPECIFIER = "ambiguous_specifier"
    CONFLICTING_DECLARATIONS = "conflicting_declarations"
    DEPENDENCY_NOT_FOUND = "dependency_not_found"
    DUPLICATE_DECLARATION = "duplicate_declaration"
    INVALID_DECLARATION = "invalid_declaration"
    INVALID_REQUEST = "invalid_request"
    INVALID_TARGET_VERSION = "invalid_target_version"
    INVALID_TOML = "invalid_toml"
    MANIFEST_NOT_FOUND = "manifest_not_found"
    MARKER_CONDITIONAL_DECLARATION = "marker_conditional_declaration"
    MISSING_PROJECT_DEPENDENCIES = "missing_project_dependencies"
    MISSING_PROJECT_TABLE = "missing_project_table"
    UNSUPPORTED_DEPENDENCIES_TYPE = "unsupported_dependencies_type"
    UNSUPPORTED_DECLARATION = "unsupported_declaration"


class _Frozen(BaseModel):
    """Base for immutable value objects with a closed field set."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DependencyAnalysisRequest(_Frozen):
    """Validated input for a dependency scan.

    Validation failures raise ``ValidationError`` at the boundary. Callers that
    need a structured result instead (CLI, API) catch it and emit an
    :attr:`ResolutionStatus.INVALID` result.
    """

    repository_root: Path
    dependency_name: str
    target_version: str
    manifest_path: Path | None = None

    @field_validator("dependency_name")
    @classmethod
    def _validate_dependency_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dependency_name must not be empty")
        return stripped

    @field_validator("target_version")
    @classmethod
    def _validate_target_version(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("target_version must not be empty")
        try:
            Version(stripped)
        except InvalidVersion as exc:
            raise ValueError(
                f"target_version is not a valid PEP 440 version: {stripped!r}"
            ) from exc
        return stripped

    @field_validator("repository_root")
    @classmethod
    def _validate_repository_root(cls, value: Path) -> Path:
        # Messages deliberately omit the path itself: they are surfaced in the
        # JSON contract, which must never leak machine-absolute paths.
        if not value.exists():
            raise ValueError("repository_root does not exist")
        if not value.is_dir():
            raise ValueError("repository_root is not a directory")
        return value

    @property
    def canonical_name(self) -> str:
        """The request's dependency name normalised per PEP 503."""
        return canonicalize_name(self.dependency_name)


class DependencyDeclaration(_Frozen):
    """A single place where the dependency is declared."""

    manifest_type: ManifestType
    path: str = Field(description="POSIX path relative to the repository root")
    location: str = Field(description="'line:<n>' or '[project].dependencies[<i>]'")
    raw: str = Field(description="Declaration text with comments stripped")
    raw_name: str = Field(description="Name exactly as spelled in the manifest")
    specifier: str = Field(description="Canonical SpecifierSet string, '' when unconstrained")
    extras: list[str] = Field(default_factory=list)
    marker: str | None = None


class ParseIssue(_Frozen):
    """A structured warning or error, optionally carrying a location."""

    code: IssueCode
    message: str
    manifest_type: ManifestType | None = None
    path: str | None = None
    location: str | None = None


class VersionTransition(_Frozen):
    """Outcome of comparing the current version against the target version."""

    kind: VersionTransitionKind
    cross_major: bool | None = None
    current_version: str | None = None
    target_version: str

    @classmethod
    def unknown(cls, target_version: str) -> VersionTransition:
        """Transition used whenever the current version cannot be determined."""
        return cls(
            kind=VersionTransitionKind.UNKNOWN,
            cross_major=None,
            current_version=None,
            target_version=target_version,
        )


class DependencyScanResult(_Frozen):
    """Full stage 1 output contract.

    Field order matches the emitted JSON so hand-written fixtures stay readable.
    """

    schema_version: str = SCHEMA_VERSION
    requested_name: str
    dependency_name: str
    status: ResolutionStatus
    current_version: str | None = None
    current_specifier: str | None = None
    target_version: str
    transition: VersionTransitionKind = VersionTransitionKind.UNKNOWN
    cross_major: bool | None = None
    declarations: list[DependencyDeclaration] = Field(default_factory=list)
    warnings: list[ParseIssue] = Field(default_factory=list)
    errors: list[ParseIssue] = Field(default_factory=list)
