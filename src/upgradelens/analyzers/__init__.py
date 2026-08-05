"""Static analyzers for UpgradeLens.

Stage 1 contains manifest analyzers; stage 2 adds an AST code-evidence
scanner. Nothing here imports, installs or executes the target repository.
"""

from upgradelens.analyzers.code_scan import DEFAULT_EXCLUDE_DIRS, scan_code_evidence
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
    "DEFAULT_EXCLUDE_DIRS",
    "compare_versions",
    "exact_version_of",
    "parse_pyproject_toml",
    "parse_requirements_txt",
    "scan_dependency",
    "scan_repository",
    "scan_code_evidence",
]
