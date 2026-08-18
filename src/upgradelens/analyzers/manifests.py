"""Manifest parsers for stage 1 (plan sections 8.5 and 8.6).

Both parsers are pure functions over file content. They never execute the
target repository's code, never import it, and never guess: anything that
cannot be parsed becomes a structured :class:`ParseIssue` with a real location.

Location formats differ on purpose:

- ``requirements.txt`` supports true 1-based line numbers (``line:12``);
- ``pyproject.toml`` is read with :mod:`tomllib`, which does not expose the
  source line of an array element, so the location is the array index
  (``[project].dependencies[3]``). Fabricating a line number there would be a
  lie the rest of the pipeline could not detect.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from upgradelens.domain import (
    DependencyDeclaration,
    IssueCode,
    ManifestType,
    ParseIssue,
)
from upgradelens.platform import read_text_utf8, to_posix_rel_path

__all__ = [
    "ManifestParseOutcome",
    "REQUIREMENTS_FILENAME",
    "PYPROJECT_FILENAME",
    "parse_pyproject_toml",
    "parse_requirements_txt",
    "parse_all_requirements_txt",
    "parse_all_pyproject_toml",
]

REQUIREMENTS_FILENAME = "requirements.txt"
PYPROJECT_FILENAME = "pyproject.toml"

_COMMENT_RE = re.compile(r"(?:^|\s)#.*$")
_HASH_OPTION_RE = re.compile(r"\s--hash=\S+")
_URL_PREFIXES = ("http://", "https://", "git+", "hg+", "svn+", "bzr+", "file:", "./", "../", "/")


@dataclass(frozen=True)
class ManifestParseOutcome:
    """Everything one manifest contributed to the scan.

    ``declarations`` holds only declarations matching the requested dependency;
    ``warnings`` and ``errors`` describe the whole file, including lines that
    are unrelated to the requested dependency but could not be understood.
    """

    manifest_type: ManifestType
    path: str
    declarations: list[DependencyDeclaration] = field(default_factory=list)
    warnings: list[ParseIssue] = field(default_factory=list)
    errors: list[ParseIssue] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    """Remove a pip-style trailing comment.

    A ``#`` only starts a comment at the beginning of the line or after
    whitespace, matching pip's own behaviour.
    """
    return _COMMENT_RE.sub("", line)


def _is_option_line(text: str) -> bool:
    return text.startswith("-")


def _is_unsupported_target(text: str) -> bool:
    """True for URLs and local paths, which stage 1 cannot version-compare."""
    return text.startswith(_URL_PREFIXES)


def _build_declaration(
    requirement: Requirement,
    *,
    manifest_type: ManifestType,
    path: str,
    location: str,
    raw: str,
) -> DependencyDeclaration:
    return DependencyDeclaration(
        manifest_type=manifest_type,
        path=path,
        location=location,
        raw=raw,
        raw_name=requirement.name,
        specifier=str(requirement.specifier),
        extras=sorted(requirement.extras),
        marker=str(requirement.marker) if requirement.marker else None,
    )


def _marker_warning(
    declaration: DependencyDeclaration,
    *,
    manifest_type: ManifestType,
    path: str,
) -> ParseIssue:
    return ParseIssue(
        code=IssueCode.MARKER_CONDITIONAL_DECLARATION,
        message=(
            "Declaration is guarded by an environment marker; "
            "it may not apply to every environment."
        ),
        manifest_type=manifest_type,
        path=path,
        location=declaration.location,
    )


def _join_continuations(lines: list[str]) -> list[tuple[int, str]]:
    """Merge backslash continuations into logical lines.

    Returns ``(line_number, text)`` pairs where ``line_number`` is the 1-based
    number of the line the logical declaration *started* on, so a location
    always points at something a human can find.
    """
    logical: list[tuple[int, str]] = []
    buffer: list[str] = []
    start_line = 0
    for index, line in enumerate(lines, start=1):
        stripped = line.rstrip()
        if not buffer:
            start_line = index
        if stripped.endswith("\\"):
            buffer.append(stripped[:-1].strip())
            continue
        buffer.append(stripped.strip())
        logical.append((start_line, " ".join(part for part in buffer if part)))
        buffer = []
    if buffer:
        logical.append((start_line, " ".join(part for part in buffer if part)))
    return logical


def parse_requirements_txt(
    repo_root: Path, manifest_path: Path, canonical_name: str
) -> ManifestParseOutcome:
    """Parse a ``requirements.txt`` and return declarations of one dependency."""
    path = to_posix_rel_path(repo_root, manifest_path)
    outcome = ManifestParseOutcome(manifest_type=ManifestType.REQUIREMENTS_TXT, path=path)

    try:
        content = read_text_utf8(manifest_path)
    except (OSError, UnicodeDecodeError) as exc:
        outcome.errors.append(
            ParseIssue(
                code=IssueCode.INVALID_DECLARATION,
                message=f"Cannot read manifest: {exc.__class__.__name__}",
                manifest_type=ManifestType.REQUIREMENTS_TXT,
                path=path,
            )
        )
        return outcome

    for line_number, logical_line in _join_continuations(content.splitlines()):
        text = _strip_comment(logical_line).strip()
        if not text:
            continue

        location = f"line:{line_number}"

        if _is_option_line(text) or _is_unsupported_target(text):
            outcome.warnings.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=f"Unsupported requirements entry is ignored: {text!r}",
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                    path=path,
                    location=location,
                )
            )
            continue

        text = _HASH_OPTION_RE.sub("", text).strip()

        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            outcome.errors.append(
                ParseIssue(
                    code=IssueCode.INVALID_DECLARATION,
                    message=f"Cannot parse requirement {text!r}: {exc}",
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                    path=path,
                    location=location,
                )
            )
            continue

        if canonicalize_name(requirement.name) != canonical_name:
            continue

        if requirement.url:
            outcome.warnings.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=(
                        "Dependency is installed from a direct URL reference, "
                        "so no version can be derived from the manifest."
                    ),
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                    path=path,
                    location=location,
                )
            )
            continue

        declaration = _build_declaration(
            requirement,
            manifest_type=ManifestType.REQUIREMENTS_TXT,
            path=path,
            location=location,
            raw=text,
        )
        outcome.declarations.append(declaration)
        if declaration.marker is not None:
            outcome.warnings.append(
                _marker_warning(declaration, manifest_type=ManifestType.REQUIREMENTS_TXT, path=path)
            )

    return outcome


def parse_pyproject_toml(
    repo_root: Path, manifest_path: Path, canonical_name: str
) -> ManifestParseOutcome:
    """Parse ``[project].dependencies`` of a ``pyproject.toml``.

    Only PEP 621 static dependencies are considered. Poetry-style
    ``[tool.poetry.dependencies]`` and PEP 621 ``dynamic`` dependencies are out
    of scope for stage 1 and reported as structured issues instead of being
    silently skipped.
    """
    path = to_posix_rel_path(repo_root, manifest_path)
    outcome = ManifestParseOutcome(manifest_type=ManifestType.PYPROJECT_TOML, path=path)

    try:
        with manifest_path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        outcome.errors.append(
            ParseIssue(
                code=IssueCode.INVALID_TOML,
                message=f"Cannot parse TOML: {exc}",
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
            )
        )
        return outcome
    except OSError as exc:
        outcome.errors.append(
            ParseIssue(
                code=IssueCode.INVALID_TOML,
                message=f"Cannot read manifest: {exc.__class__.__name__}",
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
            )
        )
        return outcome

    project = document.get("project")
    if not isinstance(project, dict):
        outcome.warnings.append(
            ParseIssue(
                code=IssueCode.MISSING_PROJECT_TABLE,
                message="No [project] table; PEP 621 dependencies cannot be read.",
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
                location="[project]",
            )
        )
        return outcome

    dependencies = project.get("dependencies")
    if dependencies is None:
        code = (
            IssueCode.UNSUPPORTED_DECLARATION
            if "dependencies" in project.get("dynamic", [])
            else IssueCode.MISSING_PROJECT_DEPENDENCIES
        )
        message = (
            "[project].dependencies is declared dynamic, so it cannot be read statically."
            if code is IssueCode.UNSUPPORTED_DECLARATION
            else "[project].dependencies is absent."
        )
        outcome.warnings.append(
            ParseIssue(
                code=code,
                message=message,
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
                location="[project].dependencies",
            )
        )
        return outcome

    if not isinstance(dependencies, list):
        outcome.errors.append(
            ParseIssue(
                code=IssueCode.UNSUPPORTED_DEPENDENCIES_TYPE,
                message=(
                    "[project].dependencies must be an array of PEP 508 strings, "
                    f"got {type(dependencies).__name__}."
                ),
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
                location="[project].dependencies",
            )
        )
        return outcome

    for index, entry in enumerate(dependencies):
        location = f"[project].dependencies[{index}]"

        if not isinstance(entry, str):
            outcome.errors.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DEPENDENCIES_TYPE,
                    message=(
                        f"Dependency entry must be a PEP 508 string, got {type(entry).__name__}."
                    ),
                    manifest_type=ManifestType.PYPROJECT_TOML,
                    path=path,
                    location=location,
                )
            )
            continue

        text = entry.strip()
        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            outcome.errors.append(
                ParseIssue(
                    code=IssueCode.INVALID_DECLARATION,
                    message=f"Cannot parse requirement {text!r}: {exc}",
                    manifest_type=ManifestType.PYPROJECT_TOML,
                    path=path,
                    location=location,
                )
            )
            continue

        if canonicalize_name(requirement.name) != canonical_name:
            continue

        if requirement.url:
            outcome.warnings.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=(
                        "Dependency is installed from a direct URL reference, "
                        "so no version can be derived from the manifest."
                    ),
                    manifest_type=ManifestType.PYPROJECT_TOML,
                    path=path,
                    location=location,
                )
            )
            continue

        declaration = _build_declaration(
            requirement,
            manifest_type=ManifestType.PYPROJECT_TOML,
            path=path,
            location=location,
            raw=text,
        )
        outcome.declarations.append(declaration)
        if declaration.marker is not None:
            outcome.warnings.append(
                _marker_warning(declaration, manifest_type=ManifestType.PYPROJECT_TOML, path=path)
            )

    return outcome


# ---------------------------------------------------------------------------
# Full-scan variants: return ALL dependency declarations, not filtered by name.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllDepsParseOutcome:
    """Everything a single manifest contributed — all dependencies."""

    manifest_type: ManifestType
    path: str
    declarations: list[DependencyDeclaration] = field(default_factory=list)
    warnings: list[ParseIssue] = field(default_factory=list)
    errors: list[ParseIssue] = field(default_factory=list)


def parse_all_requirements_txt(repo_root: Path, manifest_path: Path) -> AllDepsParseOutcome:
    """Parse a ``requirements.txt`` and return ALL dependency declarations."""
    path = to_posix_rel_path(repo_root, manifest_path)
    declarations: list[DependencyDeclaration] = []
    warnings: list[ParseIssue] = []
    errors: list[ParseIssue] = []

    try:
        content = read_text_utf8(manifest_path)
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(
            ParseIssue(
                code=IssueCode.INVALID_DECLARATION,
                message=f"Cannot read manifest: {exc.__class__.__name__}",
                manifest_type=ManifestType.REQUIREMENTS_TXT,
                path=path,
            )
        )
        return AllDepsParseOutcome(
            manifest_type=ManifestType.REQUIREMENTS_TXT,
            path=path,
            errors=errors,
        )

    for line_number, logical_line in _join_continuations(content.splitlines()):
        text = _strip_comment(logical_line).strip()
        if not text:
            continue

        location = f"line:{line_number}"

        if _is_option_line(text) or _is_unsupported_target(text):
            warnings.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=f"Unsupported requirements entry is ignored: {text!r}",
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                    path=path,
                    location=location,
                )
            )
            continue

        text = _HASH_OPTION_RE.sub("", text).strip()

        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            errors.append(
                ParseIssue(
                    code=IssueCode.INVALID_DECLARATION,
                    message=f"Cannot parse requirement {text!r}: {exc}",
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                    path=path,
                    location=location,
                )
            )
            continue

        if requirement.url:
            warnings.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=(
                        "Dependency is installed from a direct URL reference, "
                        "so no version can be derived from the manifest."
                    ),
                    manifest_type=ManifestType.REQUIREMENTS_TXT,
                    path=path,
                    location=location,
                )
            )
            continue

        declaration = _build_declaration(
            requirement,
            manifest_type=ManifestType.REQUIREMENTS_TXT,
            path=path,
            location=location,
            raw=text,
        )
        declarations.append(declaration)
        if declaration.marker is not None:
            warnings.append(
                _marker_warning(declaration, manifest_type=ManifestType.REQUIREMENTS_TXT, path=path)
            )

    return AllDepsParseOutcome(
        manifest_type=ManifestType.REQUIREMENTS_TXT,
        path=path,
        declarations=declarations,
        warnings=warnings,
        errors=errors,
    )


def parse_all_pyproject_toml(repo_root: Path, manifest_path: Path) -> AllDepsParseOutcome:
    """Parse ``[project].dependencies`` and return ALL dependency declarations."""
    path = to_posix_rel_path(repo_root, manifest_path)
    declarations: list[DependencyDeclaration] = []
    warnings: list[ParseIssue] = []
    errors: list[ParseIssue] = []

    try:
        with manifest_path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        errors.append(
            ParseIssue(
                code=IssueCode.INVALID_TOML,
                message=f"Cannot parse TOML: {exc}",
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
            )
        )
        return AllDepsParseOutcome(
            manifest_type=ManifestType.PYPROJECT_TOML, path=path, errors=errors
        )
    except OSError as exc:
        errors.append(
            ParseIssue(
                code=IssueCode.INVALID_TOML,
                message=f"Cannot read manifest: {exc.__class__.__name__}",
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
            )
        )
        return AllDepsParseOutcome(
            manifest_type=ManifestType.PYPROJECT_TOML, path=path, errors=errors
        )

    project = document.get("project")
    if not isinstance(project, dict):
        warnings.append(
            ParseIssue(
                code=IssueCode.MISSING_PROJECT_TABLE,
                message="No [project] table; PEP 621 dependencies cannot be read.",
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
                location="[project]",
            )
        )
        return AllDepsParseOutcome(
            manifest_type=ManifestType.PYPROJECT_TOML, path=path, warnings=warnings
        )

    dependencies = project.get("dependencies")
    if dependencies is None:
        code = (
            IssueCode.UNSUPPORTED_DECLARATION
            if "dependencies" in project.get("dynamic", [])
            else IssueCode.MISSING_PROJECT_DEPENDENCIES
        )
        message = (
            "[project].dependencies is declared dynamic, so it cannot be read statically."
            if code is IssueCode.UNSUPPORTED_DECLARATION
            else "[project].dependencies is absent."
        )
        warnings.append(
            ParseIssue(
                code=code,
                message=message,
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
                location="[project].dependencies",
            )
        )
        return AllDepsParseOutcome(
            manifest_type=ManifestType.PYPROJECT_TOML, path=path, warnings=warnings
        )

    if not isinstance(dependencies, list):
        errors.append(
            ParseIssue(
                code=IssueCode.UNSUPPORTED_DEPENDENCIES_TYPE,
                message=(
                    "[project].dependencies must be an array of PEP 508 strings, "
                    f"got {type(dependencies).__name__}."
                ),
                manifest_type=ManifestType.PYPROJECT_TOML,
                path=path,
                location="[project].dependencies",
            )
        )
        return AllDepsParseOutcome(
            manifest_type=ManifestType.PYPROJECT_TOML, path=path, errors=errors
        )

    for index, entry in enumerate(dependencies):
        location = f"[project].dependencies[{index}]"

        if not isinstance(entry, str):
            errors.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DEPENDENCIES_TYPE,
                    message=(
                        f"Dependency entry must be a PEP 508 string, got {type(entry).__name__}."
                    ),
                    manifest_type=ManifestType.PYPROJECT_TOML,
                    path=path,
                    location=location,
                )
            )
            continue

        text = entry.strip()
        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            errors.append(
                ParseIssue(
                    code=IssueCode.INVALID_DECLARATION,
                    message=f"Cannot parse requirement {text!r}: {exc}",
                    manifest_type=ManifestType.PYPROJECT_TOML,
                    path=path,
                    location=location,
                )
            )
            continue

        if requirement.url:
            warnings.append(
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=(
                        "Dependency is installed from a direct URL reference, "
                        "so no version can be derived from the manifest."
                    ),
                    manifest_type=ManifestType.PYPROJECT_TOML,
                    path=path,
                    location=location,
                )
            )
            continue

        declaration = _build_declaration(
            requirement,
            manifest_type=ManifestType.PYPROJECT_TOML,
            path=path,
            location=location,
            raw=text,
        )
        declarations.append(declaration)
        if declaration.marker is not None:
            warnings.append(
                _marker_warning(declaration, manifest_type=ManifestType.PYPROJECT_TOML, path=path)
            )

    return AllDepsParseOutcome(
        manifest_type=ManifestType.PYPROJECT_TOML,
        path=path,
        declarations=declarations,
        warnings=warnings,
        errors=errors,
    )
