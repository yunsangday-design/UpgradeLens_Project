"""Static analyzers for UpgradeLens.

Stage 1 only contains manifest analyzers. Nothing here imports, installs or
executes the target repository.
"""

from upgradelens.analyzers.dependency_scan import (
    compare_versions,
    exact_version_of,
    scan_dependency,
    scan_repository,
)
from upgradelens.analyzers.manifests import (
    PYPROJECT_FILENAME,
    REQUIREMENTS_FILENAME,
    ManifestParseOutcome,
    parse_pyproject_toml,
    parse_requirements_txt,
)

__all__ = [
    "PYPROJECT_FILENAME",
    "REQUIREMENTS_FILENAME",
    "ManifestParseOutcome",
    "compare_versions",
    "exact_version_of",
    "parse_pyproject_toml",
    "parse_requirements_txt",
    "scan_dependency",
    "scan_repository",
]
