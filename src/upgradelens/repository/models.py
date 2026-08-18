"""Repository profile model (plan stage S3).

A :class:`RepositoryProfile` is the deterministic, static view of a codebase: which
languages it uses, which manifests declare dependencies, where the tests live, and the
top-level symbols. It feeds the impact analyzer and the test-intelligence capability
without any model call.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LanguageProfile(BaseModel):
    """Files + lines per language (by file extension)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: str
    file_count: int = 0
    loc: int = 0


class ManifestInfo(BaseModel):
    """A dependency manifest and the dependencies it declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    ecosystem: str  # pypi | npm | gomod | pipenv | ...
    dependencies: list[str] = Field(default_factory=list)


class TestProfile(BaseModel):
    """Where the tests are and which framework they look like."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    test_paths: list[str] = Field(default_factory=list)
    framework: str = ""


class CodeSymbol(BaseModel):
    """One named definition discovered in source (function/class/assignment)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str  # FunctionDef | AsyncFunctionDef | ClassDef | Assign | ...
    path: str
    lineno: int
    end_lineno: int | None = None


class RepositoryProfile(BaseModel):
    """The static profile of a repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str
    languages: list[LanguageProfile] = Field(default_factory=list)
    manifests: list[ManifestInfo] = Field(default_factory=list)
    tests: TestProfile = Field(default_factory=TestProfile)
    symbols: list[CodeSymbol] = Field(default_factory=list)
