"""Breaking Change capability package (plan stage S5)."""

from __future__ import annotations

from upgradelens.capabilities.breaking_change.analyzers import (
    BreakingChangeResult,
    VersionComparison,
    classify_api_change,
    compare_versions,
    detect_breaking_changes,
    extract_public_symbols,
    report_to_findings,
    review_breaking_changes,
)
from upgradelens.capabilities.breaking_change.capability import (
    BreakingChangeCapability,
    build_breaking_change_plan,
    get_breaking_change_capability,
)
from upgradelens.capabilities.breaking_change.models import (
    ApiChangeKind,
    BreakingChange,
    BreakingChangeReport,
)
from upgradelens.capabilities.breaking_change.renderer import render_breaking_change
from upgradelens.capabilities.breaking_change.tools import (
    BREAKING_CHANGE_TOOL_NAMES,
    breaking_change_tools,
)
from upgradelens.capabilities.breaking_change.verifiers import verify_breaking_changes

__all__ = [
    "BreakingChangeCapability",
    "BreakingChangeReport",
    "BreakingChangeResult",
    "BREAKING_CHANGE_TOOL_NAMES",
    "ApiChangeKind",
    "BreakingChange",
    "VersionComparison",
    "build_breaking_change_plan",
    "classify_api_change",
    "compare_versions",
    "detect_breaking_changes",
    "extract_public_symbols",
    "get_breaking_change_capability",
    "breaking_change_tools",
    "render_breaking_change",
    "report_to_findings",
    "review_breaking_changes",
    "verify_breaking_changes",
]
