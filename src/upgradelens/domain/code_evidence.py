"""Domain models for stage 2 Python AST code-evidence scanning (plan section 1652).

These models describe *where* a dependency is used in source, not *what* the
upgrade impact is -- impact reasoning belongs to later stages. Every usage
carries the file (a POSIX-relative path), the exact line, a snippet and a
content hash so the report can always be re-derived and never invents a
location.

Design rules shared with stage 1 (``domain.dependency``):

- Pydantic models own validation/serialization only; scanning logic lives in
  :mod:`upgradelens.analyzers`, not in validators;
- expected failures are structured records, not raised tracebacks;
- paths are POSIX-relative and never machine-absolute.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from upgradelens.domain.dependency import SCHEMA_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "UsageKind",
    "CodeUsage",
    "DynamicImport",
    "ParseError",
    "CodeEvidenceSummary",
    "TestProductionLink",
    "CodeEvidenceReport",
]


class _Frozen(BaseModel):
    """Base for immutable value objects with a closed field set."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class UsageKind(StrEnum):
    """What kind of dependency touch a usage represents.

    Values are stable, lowercase and JSON friendly; they are part of the output
    contract, so renaming them requires bumping :data:`SCHEMA_VERSION`.
    """

    IMPORT = "import"
    CALL = "call"
    DECORATOR = "decorator"
    ATTRIBUTE = "attribute"
    CLASS_BASE = "class_base"
    CLASS_CONFIG = "class_config"


class CodeUsage(_Frozen):
    """One concrete place where the dependency is touched in source.

    ``start_line``/``end_line`` are 1-based and come straight from the AST, so
    they always point at a real line; ``snippet`` is the verbatim source of that
    span (CRLF already normalised), which lets any reader re-check the location.
    """

    path: str = Field(description="POSIX path relative to the repository root")
    start_line: int = Field(description="1-based line where the usage starts")
    end_line: int = Field(description="1-based line where the usage ends (inclusive)")
    column: int = Field(description="0-based column offset on start_line")
    kind: UsageKind
    symbol: str = Field(description="Entity touched, e.g. 'BaseModel', 'validator'")
    snippet: str = Field(description="Verbatim source span, CRLF normalised, no trailing newline")
    content_hash: str = Field(description="sha256 of the normalised file text")
    is_test_code: bool = Field(description="True when the file is test code")
    bound_as: str | None = Field(
        default=None,
        description="Local alias the name was imported as, e.g. 'pyd' or 'BM'",
    )
    confidence: Literal["high", "low"] = Field(
        default="high",
        description=(
            "low when the import alias was re-bound elsewhere in the module "
            "(same-name local variable), so the usage may be shadowed"
        ),
    )


class DynamicImport(_Frozen):
    """An import performed at runtime, which static analysis cannot trust.

    These are recorded separately from normal usages and must not be treated as
    confirmed evidence -- the dependency may or may not actually load.
    """

    path: str = Field(description="POSIX path relative to the repository root")
    line: int = Field(description="1-based line of the dynamic import call")
    snippet: str = Field(description="Verbatim source span, CRLF normalised, no trailing newline")
    mechanism: str = Field(description="'__import__' or 'importlib.import_module'")
    resolved_name: str | None = Field(
        default=None,
        description=(
            "The module name if a string literal was passed and it matches the "
            "dependency; otherwise None because it cannot be known statically"
        ),
    )
    is_test_code: bool = Field(description="True when the file is test code")


class ParseError(_Frozen):
    """A file that could not be parsed as Python source.

    The scan never raises on a bad file; it records the error and moves on so
    one broken module cannot hide every other module's evidence.
    """

    path: str = Field(description="POSIX path relative to the repository root")
    message: str = Field(description="AST error message without the file path")
    is_test_code: bool = Field(description="True when the file is test code")


class CodeEvidenceSummary(_Frozen):
    """Aggregate counts for the report."""

    scanned_files: int
    usage_count: int
    by_kind: dict[UsageKind, int] = Field(
        default_factory=dict, description="Usage count per UsageKind"
    )
    test_code_usages: int = Field(default=0, description="Usages located in test code")
    dynamic_import_count: int = Field(default=0)
    parse_error_count: int = Field(default=0)
    shadowed_binding_count: int = Field(
        default=0, description="Import bindings shadowed by a same-name local reassignment"
    )


class TestProductionLink(_Frozen):
    """A best-effort link from a test file to the production module it exercises.

    This is the *basic* association required by stage 2: a filename-stem
    heuristic only, and only recorded when the inferred production file exists.
    """

    test_path: str = Field(description="POSIX path of the test file")
    production_path: str = Field(description="POSIX path of the inferred production module")
    matched_by: str = Field(description="Heuristic used, e.g. 'filename_stem'")


class CodeEvidenceReport(_Frozen):
    """Full stage 2 output contract.

    Field order matches the emitted JSON so fixture expectations stay readable.
    """

    schema_version: str = SCHEMA_VERSION
    dependency_name: str
    scanned_files: int
    usages: list[CodeUsage] = Field(default_factory=list)
    dynamic_imports: list[DynamicImport] = Field(default_factory=list)
    parse_errors: list[ParseError] = Field(default_factory=list)
    test_production_links: list[TestProductionLink] = Field(default_factory=list)
    summary: CodeEvidenceSummary
