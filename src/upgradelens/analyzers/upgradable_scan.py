"""MVP upgradable dependency scanner (Step 12 increment D).

Scans the root-level ``pyproject.toml`` and ``requirements.txt`` for direct
dependencies, queries PyPI for the latest stable non-yanked version, and
returns a structured result indicating which packages are upgradable.

MVP scope restrictions (explicitly NOT supported):
- Poetry/PDM/uv lock files
- Workspace / monorepo
- Transitive dependencies
- optional/dev dependencies
- Recursive ``-r`` includes in requirements.txt

The result is deterministic and side-effect free (all network goes through
``PyPIClient`` which uses ``RestrictedFetcher``).
"""

from __future__ import annotations

import datetime as _dt
import logging
from pathlib import Path
from typing import Literal

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field

from upgradelens.analyzers.manifests import (
    PYPROJECT_FILENAME,
    REQUIREMENTS_FILENAME,
    AllDepsParseOutcome,
    parse_all_pyproject_toml,
    parse_all_requirements_txt,
)
from upgradelens.domain import DependencyDeclaration, ParseIssue
from upgradelens.tools.pypi import PyPIClient

logger = logging.getLogger(__name__)

__all__ = [
    "DependencyUpdateItem",
    "UpgradableScanResult",
    "scan_upgradable_dependencies",
]


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

UpdateStatus = Literal["upgradable", "up_to_date", "unresolved", "unsupported", "lookup_failed"]


class DependencyUpdateItem(BaseModel):
    """One dependency in the scan result."""

    package: str
    current_version: str | None = None
    current_specifier: str | None = None
    registry_latest: str | None = None
    status: UpdateStatus
    cross_major: bool | None = None
    declarations: list[DependencyDeclaration] = Field(default_factory=list)
    warnings: list[ParseIssue] = Field(default_factory=list)
    cache_hit: bool = False


class UpgradableScanResult(BaseModel):
    """Full scan result returned to the caller."""

    repo: str
    scope: str = "pyproject.toml[project.dependencies] + requirements.txt"
    total_declarations: int = 0
    items: list[DependencyUpdateItem] = Field(default_factory=list)
    errors: list[ParseIssue] = Field(default_factory=list)
    scanned_at: str = Field(default_factory=lambda: _dt.datetime.now(_dt.UTC).isoformat())


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _extract_exact_pin(specifier: str) -> str | None:
    """Return the version string if specifier is a single ``==x.y.z`` pin."""
    stripped = specifier.strip()
    if stripped.startswith("==") and "," not in stripped:
        candidate = stripped[2:].strip()
        try:
            Version(candidate)
            return candidate
        except InvalidVersion:
            return None
    return None


def _is_cross_major(current: str, latest: str) -> bool | None:
    """Determine if the upgrade crosses a major version boundary."""
    try:
        cv = Version(current)
        lv = Version(latest)
        return cv.major != lv.major
    except InvalidVersion:
        return None


def scan_upgradable_dependencies(
    repo_root: Path,
    pypi: PyPIClient,
) -> UpgradableScanResult:
    """Scan MVP manifests and query PyPI for upgradable dependencies.

    Only root-level ``pyproject.toml`` ``[project].dependencies`` and
    ``requirements.txt`` are considered (MVP scope).
    """
    repo_root = repo_root.resolve()
    all_outcomes: list[AllDepsParseOutcome] = []
    all_errors: list[ParseIssue] = []

    # Discover and parse manifests
    pyproject_path = repo_root / PYPROJECT_FILENAME
    if pyproject_path.exists():
        outcome = parse_all_pyproject_toml(repo_root, pyproject_path)
        all_outcomes.append(outcome)
        all_errors.extend(outcome.errors)

    req_path = repo_root / REQUIREMENTS_FILENAME
    if req_path.exists():
        outcome = parse_all_requirements_txt(repo_root, req_path)
        all_outcomes.append(outcome)
        all_errors.extend(outcome.errors)

    # Aggregate declarations by canonical package name
    by_package: dict[str, list[DependencyDeclaration]] = {}
    for outcome in all_outcomes:
        for decl in outcome.declarations:
            cn = canonicalize_name(decl.raw_name)
            by_package.setdefault(cn, []).append(decl)

    total_declarations = sum(len(decls) for decls in by_package.values())

    # For each unique package, determine status
    items: list[DependencyUpdateItem] = []
    for package, declarations in sorted(by_package.items()):
        # Collect all specifiers
        specifiers = [d.specifier for d in declarations if d.specifier]
        warnings: list[ParseIssue] = []

        # Determine current version (only exact pin)
        current_version: str | None = None
        current_specifier: str | None = None
        if specifiers:
            # Use the first non-empty specifier as representative
            current_specifier = specifiers[0]
            # Only exact == pin counts as current_version
            pins = [_extract_exact_pin(s) for s in specifiers]
            exact_pins = [p for p in pins if p is not None]
            if exact_pins:
                current_version = exact_pins[0]

        # Query PyPI for latest stable version
        registry_latest: str | None = None
        status: UpdateStatus
        cross_major: bool | None = None
        try:
            registry_latest = pypi.latest_stable_version(package)
        except Exception:
            logger.debug("PyPI lookup failed for %s", package, exc_info=True)
            items.append(
                DependencyUpdateItem(
                    package=package,
                    current_version=current_version,
                    current_specifier=current_specifier,
                    registry_latest=None,
                    status="lookup_failed",
                    cross_major=None,
                    declarations=declarations,
                    warnings=warnings,
                )
            )
            continue

        if registry_latest is None:
            status = "lookup_failed"
        elif current_version is None:
            # Cannot determine current version → unresolved
            status = "unresolved"
        else:
            try:
                cv = Version(current_version)
                lv = Version(registry_latest)
                if lv > cv:
                    status = "upgradable"
                    cross_major = _is_cross_major(current_version, registry_latest)
                elif lv == cv:
                    status = "up_to_date"
                else:
                    # Current is newer than PyPI latest (dev/local build?)
                    status = "up_to_date"
            except InvalidVersion:
                status = "unresolved"

        items.append(
            DependencyUpdateItem(
                package=package,
                current_version=current_version,
                current_specifier=current_specifier,
                registry_latest=registry_latest,
                status=status,
                cross_major=cross_major,
                declarations=declarations,
                warnings=warnings,
            )
        )

    return UpgradableScanResult(
        repo=str(repo_root),
        total_declarations=total_declarations,
        items=items,
        errors=all_errors,
    )
