"""Version comparison and multi-manifest aggregation (plan section 8.7-8.9).

The central rule of this module is that certainty must never be invented. A
manifest states which versions are *allowed*, not which one is *installed*. Only
a single unambiguous ``==`` pin justifies :attr:`ResolutionStatus.RESOLVED`; a
range yields :attr:`ResolutionStatus.AMBIGUOUS` with no transition, because
reporting a confident-but-wrong current version would silently poison every
downstream stage.
"""

from __future__ import annotations

from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from upgradelens.analyzers.manifests import (
    PYPROJECT_FILENAME,
    REQUIREMENTS_FILENAME,
    ManifestParseOutcome,
    parse_pyproject_toml,
    parse_requirements_txt,
)
from upgradelens.domain import (
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

__all__ = ["compare_versions", "exact_version_of", "scan_dependency", "scan_repository"]

# Fixed discovery order. Never derived from directory listing, so results are
# byte-identical on macOS, Windows and Linux.
_DISCOVERY_ORDER: tuple[tuple[str, ManifestType], ...] = (
    (PYPROJECT_FILENAME, ManifestType.PYPROJECT_TOML),
    (REQUIREMENTS_FILENAME, ManifestType.REQUIREMENTS_TXT),
)


def exact_version_of(specifier: str) -> str | None:
    """Return the pinned version, or ``None`` when the specifier is not exact.

    Exact means precisely one clause, using ``==``, without a wildcard.
    ``==1.10.*`` and ``===1.10.13`` are deliberately rejected: the first matches
    a range, the second is arbitrary-equality and not PEP 440 comparable.

    ``SpecifierSet`` already rejects a malformed version after ``==``, so the
    returned string is guaranteed to be parsable by :class:`Version`.
    """
    try:
        parsed = SpecifierSet(specifier)
    except InvalidSpecifier:
        return None
    clauses = list(parsed)
    if len(clauses) != 1:
        return None
    clause = clauses[0]
    if clause.operator != "==" or "*" in clause.version:
        return None
    return clause.version


def compare_versions(current_version: str | None, target_version: str) -> VersionTransition:
    """Compare two PEP 440 versions.

    Returns an ``UNKNOWN`` transition when either side is missing or unparsable,
    rather than raising, so an odd version string degrades one result field
    instead of failing the whole scan.
    """
    if current_version is None:
        return VersionTransition.unknown(target_version)
    try:
        current = Version(current_version)
        target = Version(target_version)
    except InvalidVersion:
        return VersionTransition.unknown(target_version)

    if target > current:
        kind = VersionTransitionKind.UPGRADE
    elif target < current:
        kind = VersionTransitionKind.DOWNGRADE
    else:
        kind = VersionTransitionKind.SAME

    return VersionTransition(
        kind=kind,
        cross_major=current.major != target.major,
        current_version=current_version,
        target_version=target_version,
    )


def _parse_manifest(
    repo_root: Path, manifest_path: Path, manifest_type: ManifestType, canonical_name: str
) -> ManifestParseOutcome:
    if manifest_type is ManifestType.PYPROJECT_TOML:
        return parse_pyproject_toml(repo_root, manifest_path, canonical_name)
    return parse_requirements_txt(repo_root, manifest_path, canonical_name)


def _manifest_type_for(path: Path) -> ManifestType | None:
    if path.name == PYPROJECT_FILENAME:
        return ManifestType.PYPROJECT_TOML
    if path.name == REQUIREMENTS_FILENAME:
        return ManifestType.REQUIREMENTS_TXT
    return None


def _resolve_explicit_manifest(repo_root: Path, manifest_path: Path) -> Path:
    return manifest_path if manifest_path.is_absolute() else repo_root / manifest_path


def _invalid_result(
    request: DependencyAnalysisRequest, status: ResolutionStatus, issue: ParseIssue
) -> DependencyScanResult:
    return DependencyScanResult(
        requested_name=request.dependency_name,
        dependency_name=request.canonical_name,
        status=status,
        target_version=request.target_version,
        errors=[issue],
    )


def _collect_outcomes(
    request: DependencyAnalysisRequest, repo_root: Path
) -> list[ManifestParseOutcome] | DependencyScanResult:
    """Parse every relevant manifest, or return an early terminal result."""
    canonical = request.canonical_name

    if request.manifest_path is not None:
        target = _resolve_explicit_manifest(repo_root, request.manifest_path)
        manifest_type = _manifest_type_for(target)
        if manifest_type is None:
            return _invalid_result(
                request,
                ResolutionStatus.UNSUPPORTED,
                ParseIssue(
                    code=IssueCode.UNSUPPORTED_DECLARATION,
                    message=(
                        f"Unsupported manifest {target.name!r}; "
                        f"expected {PYPROJECT_FILENAME} or {REQUIREMENTS_FILENAME}."
                    ),
                ),
            )
        if not target.is_file():
            return _invalid_result(
                request,
                ResolutionStatus.NOT_FOUND,
                ParseIssue(
                    code=IssueCode.MANIFEST_NOT_FOUND,
                    message=f"Manifest not found: {target.name}",
                    manifest_type=manifest_type,
                ),
            )
        return [_parse_manifest(repo_root, target, manifest_type, canonical)]

    outcomes = [
        _parse_manifest(repo_root, candidate, manifest_type, canonical)
        for filename, manifest_type in _DISCOVERY_ORDER
        if (candidate := repo_root / filename).is_file()
    ]
    if not outcomes:
        return _invalid_result(
            request,
            ResolutionStatus.NOT_FOUND,
            ParseIssue(
                code=IssueCode.MANIFEST_NOT_FOUND,
                message=(
                    f"No supported manifest found in repository root; "
                    f"expected {PYPROJECT_FILENAME} or {REQUIREMENTS_FILENAME}."
                ),
            ),
        )
    return outcomes


def _aggregate_specifier(
    declarations: list[DependencyDeclaration],
) -> tuple[str | None, list[ParseIssue]]:
    """Reduce declarations to one specifier, reporting duplicates/conflicts."""
    issues: list[ParseIssue] = []
    unique = {declaration.specifier for declaration in declarations}

    if len(declarations) > 1:
        if len(unique) == 1:
            specifier = declarations[0].specifier
            issues.extend(
                ParseIssue(
                    code=IssueCode.DUPLICATE_DECLARATION,
                    message=(
                        f"Dependency is declared more than once with the same "
                        f"specifier {specifier!r}."
                    ),
                    manifest_type=duplicate.manifest_type,
                    path=duplicate.path,
                    location=duplicate.location,
                )
                for duplicate in declarations[1:]
            )
        else:
            issues.append(
                ParseIssue(
                    code=IssueCode.CONFLICTING_DECLARATIONS,
                    message=(
                        f"Dependency is declared with conflicting specifiers: {sorted(unique)}."
                    ),
                )
            )

    return (declarations[0].specifier if len(unique) == 1 else None), issues


def scan_dependency(request: DependencyAnalysisRequest) -> DependencyScanResult:
    """Run the full stage 1 scan for one dependency."""
    repo_root = request.repository_root.resolve()

    collected = _collect_outcomes(request, repo_root)
    if isinstance(collected, DependencyScanResult):
        return collected

    declarations: list[DependencyDeclaration] = []
    warnings: list[ParseIssue] = []
    errors: list[ParseIssue] = []
    for outcome in collected:
        declarations.extend(outcome.declarations)
        warnings.extend(outcome.warnings)
        errors.extend(outcome.errors)

    if not declarations:
        unreadable = [
            outcome for outcome in collected if any(e.location is None for e in outcome.errors)
        ]
        status = (
            ResolutionStatus.INVALID
            if len(unreadable) == len(collected)
            else ResolutionStatus.NOT_FOUND
        )
        if status is ResolutionStatus.NOT_FOUND:
            warnings.append(
                ParseIssue(
                    code=IssueCode.DEPENDENCY_NOT_FOUND,
                    message=(
                        f"Dependency {request.canonical_name!r} is not declared in "
                        f"{', '.join(outcome.path for outcome in collected)}."
                    ),
                )
            )
        return DependencyScanResult(
            requested_name=request.dependency_name,
            dependency_name=request.canonical_name,
            status=status,
            target_version=request.target_version,
            warnings=warnings,
            errors=errors,
        )

    current_specifier, aggregation_issues = _aggregate_specifier(declarations)
    warnings.extend(aggregation_issues)

    pinned = {exact_version_of(declaration.specifier) for declaration in declarations}
    current_version = pinned.pop() if len(pinned) == 1 else None

    if current_version is None:
        warnings.append(
            ParseIssue(
                code=IssueCode.AMBIGUOUS_SPECIFIER,
                message=(
                    f"Specifier {current_specifier!r} is a range, so the installed "
                    "version cannot be inferred from the manifest."
                    if current_specifier
                    else "Declarations do not pin a single exact version, so the "
                    "installed version cannot be inferred from the manifest."
                ),
            )
        )
        return DependencyScanResult(
            requested_name=request.dependency_name,
            dependency_name=request.canonical_name,
            status=ResolutionStatus.AMBIGUOUS,
            current_specifier=current_specifier,
            target_version=request.target_version,
            declarations=declarations,
            warnings=warnings,
            errors=errors,
        )

    transition = compare_versions(current_version, request.target_version)
    return DependencyScanResult(
        requested_name=request.dependency_name,
        dependency_name=request.canonical_name,
        status=ResolutionStatus.RESOLVED,
        current_version=current_version,
        current_specifier=current_specifier,
        target_version=request.target_version,
        transition=transition.kind,
        cross_major=transition.cross_major,
        declarations=declarations,
        warnings=warnings,
        errors=errors,
    )


def scan_repository(
    repository_root: Path, dependency_name: str, target_version: str, manifest_path: Path | None
) -> DependencyScanResult:
    """Convenience wrapper used by the CLI; see :func:`scan_dependency`."""
    request = DependencyAnalysisRequest(
        repository_root=repository_root,
        dependency_name=dependency_name,
        target_version=target_version,
        manifest_path=manifest_path,
    )
    return scan_dependency(request)
